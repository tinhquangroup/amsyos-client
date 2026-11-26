"""`amsyos split` (2026-11-26): one large job as many chunks of one service, spread over the
organisation's nodes by the builtin service `split.run`. Each chunk is an ordinary job of that
service, let in and placed as a direct call would be; the split costs nothing beyond its chunks
(and a rented node's hours).

1. what to split over, one of `--over 1-240` (a range, `1-240:2` with a step), `--items`,
   `--files`, `--rows`, `--product`, with `--chunk N`; local files are uploaded once (sent only
   when the workspace lacks them), an `asset://…` goes as it is;
2. the chunk's params: `--params` (a JSON object, or @file) then each `--param k=v` (k=@path
   uploads a file, as `amsyos run` does); a whole value `$chunk.<name>` is the chunk's own;
3. `split.run` is called with `over`, `params` and a service gather as JSON text (its inputs take
   text, as `mission.run`'s plan); only the options given are sent, the kernel fills the rest;
4. `--wait` follows it (chunks done/total, in flight, per node), then puts `result` and the
   chunk list in `./out/split/<job>/` and says the summary and each failed chunk's hint.

`amsyos split j_…` follows a split already started. Ctrl-C stops waiting, never the split:
`amsyos cancel <job>` stops its chunks in flight and keeps those done.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from amsyos_cli.api import TERMINAL, Api, done, idempotency_key
from amsyos_cli.common import CliError, Out, amount, credentials, is_link, obj, seq, short
from amsyos_cli.files import Saved, download, human, text_of, upload
from amsyos_cli.jobs import (
    POLL_FLOOR_S,
    eta_text,
    fixed_lines,
    parse_value,
    request_lines,
    status_line,
    status_lines,
    with_basis,
    with_id,
)
from amsyos_cli.shell import cli_hint

SERVICE = "split.run"
KINDS = ("bundle", "concat", "rows", "none")  # the builtin gathers, and none
ON_FAILURE = ("auto", "stop", "continue")
MOST_CHUNKS = 1000  # chunks per split (the owner, 2026-11-26)
START_WAIT_S = 3  # with --wait, the run call waits this long (a resume of a done split ends)
FOLLOW_S = 10  # while chunks run, the status is asked with wait_s this long
FAILED_SHOWN = 10  # failed chunks said one by one; the rest counted
NODES_SHOWN = 8
CHUNKS_MOST_BYTES = 32 * 1024 * 1024  # the chunk list read back (1000 chunks are far less)
CHUNK_NAMES = ("start", "end", "index", "count", "item", "items", "file", "files", "rows")
JOB = re.compile(r"j_[0-9a-f]{16}")
CHUNK_STEP = re.compile(r"c\d{1,5}")  # a chunk's step id in `work`
IN_FLIGHT = ("queued", "provisioning", "running")
SERVICE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*(@[A-Za-z0-9_.\-]+)?")
RANGE = re.compile(r"\s*(-?\d+)\s*-\s*(-?\d+)\s*(?::\s*(\d+))?\s*")
WHOLE = re.compile(r"\$chunk\.([A-Za-z_][A-Za-z0-9_]*)")
SPLIT_HELP = """
What to split over, one of (each makes $chunk.<name> values for --param):
  --over 1-240 --chunk 30       a range (1-240:2 takes every second): $chunk.start, $chunk.end
  --items '["a","b"]'           a JSON list, or @list.json (--chunk 1): $chunk.item, $chunk.items
  --files a.png b.png ...       local files (sent once) or asset://...: $chunk.file, $chunk.files
  --rows data.csv --chunk 100   a CSV or JSON-lines file by rows, its header in each: $chunk.rows
  --product '{"lr":[0.1,0.01],"seed":[1,2]}'   every combination, one chunk each: $chunk.lr ...
Every chunk has $chunk.index (from 0) and $chunk.count. Only a whole value $chunk.<name> is
replaced; quote it, or the shell takes $chunk for its own: --param 'frame_end=$chunk.end'.
Each chunk's params pass the service's own checks, as a direct call's would.

  amsyos split t_x.render --over 1-240 --chunk 30 --param scene=@scene.blend \\
      --param 'frame_start=$chunk.start' --param 'frame_end=$chunk.end' --wait
  amsyos split t_x.label --files photos/*.jpg --param 'image=$chunk.file' --gather rows --wait
  amsyos split t_x.render ... --gather t_x.join --gather-param fps=24 --max-credit 5 --nodes 2
  amsyos split t_x.render ... --dry-run     the params split.run would get; nothing sent
  amsyos split j_...                         follow a split already started, then its result

The first chunk runs alone first (--first 1; 0: none). A failed chunk of a pure or read service
is tried again (--retry 1, at most 3), on another node when there is one; one that writes or
acts outside never is. --on-failure auto stops when the first 2 chunks to end both failed, else
goes on and keeps what is done; stop, continue. --max-credit: the most the chunks (and extra
rented nodes) may cost, needed when they cost credit; --nodes 1-4: rented nodes at once when
the service runs on a rented node. --gather: bundle (a zip of every chunk's output, the
default), concat (bytes in chunk order, a CSV's header once), rows (one table), none (the
parts only listed), or a service of yours called once with the parts (video: your own join).
--wait follows it (chunks done/total, in flight, per node), then puts the result and
chunks.json in out/split/<job>/ and says the summary and each failed chunk's hint. Ctrl-C
stops waiting only: amsyos cancel <job> stops the chunks in flight and keeps those done; the
same command with --resume <job> reuses that split's succeeded chunks.
"""


# ---------------------------------------------------------------- the params of split.run
def _json_arg(flag: str, text: str) -> Any:
    """A JSON value given on the command line, or `@file` (its JSON)."""
    if text.startswith("@"):
        path = Path(text[1:])
        if not path.is_file():
            raise CliError("INVALID_INPUT", f"{flag} {text}: no such file")
        text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        raise CliError("INVALID_INPUT", f"{flag}: not JSON ({short(text, 60)})") from None


def _local_or_ref(flag: str, given: str, local: dict[str, Path]) -> str:
    """An `asset://…` as it is; a local file kept in `local` to upload, named by its path."""
    if given.startswith("asset://"):
        return given
    path = Path(given)
    if not path.is_file():
        raise CliError("INVALID_INPUT", f"{flag} {given}: no such file (nor an asset://…)")
    local[given] = path
    return given


def over_of(args: argparse.Namespace, local: dict[str, Path]) -> dict[str, Any]:
    """`over` from the one form given; local files are named in `local`, replaced by their
    assets once uploaded (`_with_assets`)."""
    given = [
        flag for flag, value in (
            ("--over", args.over), ("--items", args.items), ("--files", args.files),
            ("--rows", args.rows), ("--product", args.product),
        ) if value
    ]  # fmt: skip
    if len(given) != 1:
        said = f" (given: {', '.join(given)})" if given else ""
        raise CliError(
            "INVALID_INPUT",
            f"give one of --over 1-240, --items '[…]', --files a b …, --rows f.csv, --product "
            f"'{{\"a\": […]}}'{said}; amsyos split --help",
        )
    over: dict[str, Any]
    if args.over:
        text = str(args.over).strip()
        over = obj(_json_arg("--over", text)) if text.startswith(("{", "@")) else {"range": text}
        if not over:
            raise CliError("INVALID_INPUT", '--over: a range like 1-240, or {"range": …}')
    elif args.items:
        items = _json_arg("--items", args.items)
        if not isinstance(items, list) or not items:
            raise CliError("INVALID_INPUT", '--items: a JSON list, like ["a", "b"] or @list.json')
        over = {"items": items}
    elif args.files:
        over = {"files": [_local_or_ref("--files", f, local) for f in args.files]}
    elif args.rows:
        over = {"rows": _local_or_ref("--rows", args.rows, local)}
    else:
        product = obj(_json_arg("--product", args.product))
        if not product or not all(seq(v) for v in product.values()):
            raise CliError(
                "INVALID_INPUT",
                '--product: a JSON object of lists, like {"lr": [0.1], "seed": [1]}',
            )
        if args.chunk is not None:
            raise CliError("INVALID_INPUT", "--product takes no --chunk: each combination is one")
        over = {"product": product}
    if args.chunk is not None:
        if args.chunk < 1:
            raise CliError("INVALID_INPUT", "--chunk: 1 or more")
        over.setdefault("chunk", args.chunk)
    count = chunk_count(over)
    if count is not None and count > MOST_CHUNKS:
        raise CliError(
            "INVALID_INPUT",
            f"{count} chunks: a split takes at most {MOST_CHUNKS}; give a larger --chunk",
        )
    return over


def chunk_count(over: dict[str, Any]) -> int | None:
    """How many chunks `over` makes, where it can be said here (a range with its chunk, a
    list, a product); None for rows, and for a range whose chunk the kernel picks."""
    chunk = over.get("chunk")
    size = chunk if isinstance(chunk, int) and not isinstance(chunk, bool) and chunk > 0 else None
    if isinstance(over.get("range"), str):
        found = RANGE.fullmatch(over["range"])
        if found is None or size is None:
            return None
        first, last, step = int(found[1]), int(found[2]), int(found[3] or 1)
        return math.ceil(len(range(first, last + 1, max(step, 1))) / size)
    for key in ("items", "files"):
        if isinstance(over.get(key), list):
            return math.ceil(len(seq(over[key])) / (size or 1))
    product = obj(over.get("product"))
    if product:
        return math.prod(len(seq(v)) for v in product.values())
    return None


def params_of(
    flag: str, whole: str | None, items: list[str], names: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, Path], dict[str, str]]:
    """A service's params: `whole` (a JSON object, or @file), then each `k=v` (JSON when it
    reads as JSON, else text; k=@path a local file). Returns the params, the local files and
    the k=v read as JSON (their text, for an input that takes text). `names`: the `$chunk.*`
    names this split makes; a value the shell has emptied (`$chunk.end` → `.end`) is refused."""
    params: dict[str, Any] = {}
    if whole:
        told = _json_arg(f"{flag}s", whole)
        if not isinstance(told, dict):
            raise CliError("INVALID_INPUT", f"{flag}s: a JSON object")
        params.update(obj(told))
    local: dict[str, Path] = {}
    texts: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CliError("INVALID_INPUT", f"{flag} is k=v: {item}")
        if value.startswith("@"):
            local[key] = Path(value[1:])
            params.pop(key, None)
            continue
        if value.startswith(".") and value[1:] in names:
            raise CliError(
                "INVALID_INPUT",
                f"{flag} {item}: the shell took $chunk for its own and left {value}; quote it: "
                f"{flag} '{key}=$chunk{value}'",
            )
        params[key] = parse_value(value)
        if not isinstance(params[key], str):
            texts[key] = value
    missing = [f"{k}=@{p}" for k, p in local.items() if not p.is_file()]
    if missing:
        raise CliError("INVALID_INPUT", f"no such file: {', '.join(missing)}")
    return params, local, texts


def chunk_notes(params: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    """What the kernel will not replace: `$chunk.` inside a longer value, or an unknown name."""
    notes: list[str] = []
    for key, value in params.items():
        if not isinstance(value, str) or "$chunk." not in value:
            continue
        found = WHOLE.fullmatch(value)
        if found is None:
            notes.append(
                f"{key}: only a whole value $chunk.<name> is replaced; {value!r} goes as is"
            )
        elif found[1] not in names:
            notes.append(
                f"{key}: this split makes no $chunk.{found[1]} (it makes {', '.join(names)}); it "
                "goes as is"
            )
    return notes


def names_of(over: dict[str, Any]) -> tuple[str, ...]:
    """The `$chunk.*` names a split over `over` makes."""
    if "product" in over:
        return ("index", "count", *obj(over["product"]))
    for key, made in (
        ("range", ("start", "end")), ("items", ("item", "items")), ("files", ("file", "files")),
        ("rows", ("rows",)),
    ):  # fmt: skip
        if key in over:
            return ("index", "count", *made)
    return CHUNK_NAMES


async def _typed(
    api: Api, service: str, params: dict[str, Any], texts: dict[str, str], local: dict[str, Path]
) -> None:
    """As `amsyos run`: a k=v read as JSON goes as its text where the service's input takes
    text, a k=@path of an input that takes no file as the file's text (one describe, only when
    either is given); whatever describe cannot say is left as it was read."""
    if not texts and not local:
        return
    try:
        inputs = obj((await api.describe(service)).get("inputs"))
    except CliError:
        return
    for key, text in texts.items():
        if obj(inputs.get(key)).get("type") in ("str", "enum"):
            params[key] = text
    for key in [k for k in local if k in inputs]:
        kind = str(obj(inputs[key]).get("type") or "asset")
        if kind != "asset":
            path = local.pop(key)
            params[key] = text_of(key, path, kind)
            api.out.note(f"{key}: the text of {path} ({key} takes {kind}, not a file)")


LIST_INLINE_MAX = 11_000  # characters of over's list kept inline (a text input takes 12,000)


async def _long_lists(api: Api, over: dict[str, Any]) -> None:
    """`over.files` or `over.items` too long for a text input travel as a JSON file of the
    workspace, which split.run reads (`"files": "asset://…"`, at most 2 MB)."""
    for key in ("files", "items"):
        value = over.get(key)
        text = json.dumps(value, separators=(",", ":")) if isinstance(value, list) else ""
        if len(text) <= LIST_INLINE_MAX:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"split_{key}.json"
            path.write_text(text, encoding="utf-8")
            (sent,) = await upload(api, [path])
        over[key] = str(sent["asset"])


async def _send(api: Api, out: Out, files: list[Path]) -> dict[Path, str]:
    """Each local file as an asset (sent only when the workspace lacks it): path → asset."""
    paths = list(dict.fromkeys(files))
    if not paths:
        return {}
    sent = await upload(api, paths)
    reused = sum(1 for row in sent if row.get("reused"))
    size = sum(int(row["size"]) for row in sent)
    if len(paths) == 1:
        how = "already in the workspace" if reused else "uploaded"
        out.note(f"{paths[0]}: {sent[0]['asset']} ({how}, {human(size)})")
    else:
        out.note(f"{len(paths)} files ({human(size)}): {len(paths) - reused} uploaded, {reused} "
                 "already in the workspace")  # fmt: skip
    return {path: str(row["asset"]) for path, row in zip(paths, sent, strict=True)}


def build(
    args: argparse.Namespace, over: dict[str, Any], chunk: dict[str, Any],
    gather: dict[str, Any] | str | None,
) -> dict[str, Any]:  # fmt: skip
    """split.run's params: over, params and a service gather as JSON text; an option only
    when it was given (the kernel fills the rest)."""
    params: dict[str, Any] = {
        "service": args.service,
        "over": json.dumps(over, ensure_ascii=False, separators=(",", ":")),
        "params": json.dumps(chunk, ensure_ascii=False, separators=(",", ":")),
    }
    if gather is not None:
        params["gather"] = (
            gather if isinstance(gather, str)
            else json.dumps(gather, ensure_ascii=False, separators=(",", ":"))
        )  # fmt: skip
    for key in ("output", "first", "retry", "on_failure", "max_credit", "max_in_flight",
                "nodes", "resume"):  # fmt: skip
        value = getattr(args, key, None)
        if value is not None:
            params[key] = value
    return params


def gather_of(
    text: str | None, given: dict[str, Any], input_name: str | None
) -> dict[str, Any] | str | None:
    """`--gather`: a builtin kind as it is; a service of yours, or the JSON of one, as
    `{service, input, params}` with `--gather-param`s merged into its params."""
    if text is None:
        if given or input_name:
            raise CliError(
                "INVALID_INPUT", "--gather-param and --gather-input go with --gather <service>"
            )
        return None
    if text in KINDS:
        if given or input_name:
            raise CliError(
                "INVALID_INPUT",
                f"--gather {text} takes no --gather-param or --gather-input (only a service does)",
            )
        return text
    if text.lstrip().startswith(("{", "@")):
        told = obj(_json_arg("--gather", text))
        if not isinstance(told.get("service"), str):
            raise CliError("INVALID_INPUT", '--gather JSON: {"service": "t_x.merge", "input": '
                           '"parts", "params": {…}}')  # fmt: skip
    elif SERVICE_ID.fullmatch(text) and "." in text:
        told: dict[str, Any] = {"service": text}
    else:
        raise CliError(
            "INVALID_INPUT",
            f"--gather {short(text, 60)}: one of {', '.join(KINDS)}, a service of yours "
            '(t_x.merge), or {"service": …, "input": "parts", "params": {…}}',
        )
    told.setdefault("input", input_name or "parts")
    if input_name:
        told["input"] = input_name
    if given:
        told["params"] = {**obj(told.get("params")), **given}
    return told


# ---------------------------------------------------------------- following it
def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def tally(status: dict[str, Any]) -> dict[str, Any]:
    """Chunks done, total, in flight, failed and per node, where the status says them: its
    `split` object, else the chunk steps its `work` lists (at most 60: a partial count)."""
    told = obj(status.get("split"))
    found: dict[str, Any] = {}
    for key, names in (
        ("done", ("done", "succeeded")), ("total", ("total", "chunks")),
        ("in_flight", ("in_flight", "running")), ("failed", ("failed",)),
    ):  # fmt: skip
        number = next((n for n in (_int(told.get(name)) for name in names) if n is not None), None)
        if number is not None:
            found[key] = number
    nodes: dict[str, Any] = next(
        (obj(told[k]) for k in ("nodes", "per_node", "chunks_per_node") if obj(told.get(k))),
        dict[str, Any](),
    )
    if found or nodes:
        return {**found, **({"nodes": nodes} if nodes else {}), "from": "split"}
    chunks = [
        obj(e) for e in seq(status.get("work"))
        if CHUNK_STEP.fullmatch(str(obj(e).get("id") or ""))
    ]  # fmt: skip
    if not chunks:
        return {}
    states = [str(e.get("state")) for e in chunks]
    per: dict[str, dict[str, int]] = {}
    for entry in chunks:
        node, state = entry.get("node"), str(entry.get("state"))
        if isinstance(node, str) and node and state in ("succeeded", "running"):
            counts = per.setdefault(node, {"done": 0, "running": 0})
            counts["done" if state == "succeeded" else "running"] += 1
    return {
        "done": states.count("succeeded"), "in_flight": sum(s in IN_FLIGHT for s in states),
        "failed": states.count("failed"), **({"nodes": per} if per else {}), "from": "work",
    }  # fmt: skip


def _node_said(value: Any) -> str:
    """A node's share: a number of chunks, or `3 done, 1 running`."""
    if _int(value) is not None:
        return str(value)
    told = obj(value)
    bits = [f"{n} {k}" for k, n in told.items() if _int(n)]
    return ", ".join(bits) or short(value, 40)


def progress_line(status: dict[str, Any], job_id: str) -> str:
    """One line of a running split: its state, chunks done of all, in flight, failed, and
    each node's share; the kernel's phase where it says more."""
    told = with_id(status, job_id)
    parts = [f"{job_id}: {told.get('state', '?')}"]
    counts = tally(told)
    phase = told.get("phase") if isinstance(told.get("phase"), str) else None
    says_chunks = bool(phase) and "chunk" in str(phase)
    if counts.get("from") == "split" or not says_chunks:
        if counts.get("done") is not None:
            total = counts.get("total")
            parts.append(f"chunks {counts['done']}/{total} done" if total is not None
                         else f"{counts['done']} chunks done")  # fmt: skip
        if counts.get("in_flight"):
            parts.append(f"{counts['in_flight']} in flight")
        if counts.get("failed"):
            parts.append(f"{counts['failed']} failed")
    if phase and not (counts.get("from") == "split" and says_chunks):
        parts.append(short(phase, 200))
    progress = told.get("progress")
    if counts.get("total") is None and isinstance(progress, int | float) and not says_chunks:
        parts.append(f"{float(progress):.0f}%")
    nodes = obj(counts.get("nodes"))
    if nodes:
        shown = list(nodes.items())[:NODES_SHOWN]
        parts.append("per node: " + "; ".join(f"{k} {_node_said(v)}" for k, v in shown))
    return ", ".join(parts)


async def follow_split(
    api: Api, out: Out, job_id: str, first: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The split's status once it has ended or waits for somebody, a line said each time
    its progress changes (stderr). Its own state moves only at its end, so the status is
    asked every `FOLLOW_S` seconds rather than on the event stream."""
    status = with_id(first, job_id) if first else with_id(await api.status(job_id), job_id)
    said = ""
    while True:
        line = progress_line(status, job_id)
        if line != said:
            out.note(line)
            said = line
        if status.get("state") in TERMINAL or done(status):
            return status
        began = time.monotonic()
        status = with_id(await api.status(job_id, wait_s=FOLLOW_S), job_id)
        left = POLL_FLOOR_S - (time.monotonic() - began)  # a status that did not wait
        if left > 0 and status.get("state") not in TERMINAL:
            await asyncio.sleep(left)


# ---------------------------------------------------------------- its end
def chunk_rows(raw: Any) -> list[dict[str, Any]]:
    """The chunk list (`chunks`): a list of rows, `{chunks: […]}`, or rows by index."""
    told = raw
    if isinstance(told, dict) and isinstance(obj(told).get("chunks"), list | dict):
        told = obj(told)["chunks"]
    if isinstance(told, dict):
        rows = [{"index": k, **obj(v)} for k, v in obj(told).items()]
    else:
        rows = [obj(r) for r in seq(told)]
    return [r for r in rows if r]


def _values_said(values: Any) -> str:
    if isinstance(values, dict):
        return short(", ".join(f"{k} {short(v, 40)}" for k, v in obj(values).items()), 120)
    return short(values, 80) if values not in (None, "", [], {}) else ""


def failed_lines(rows: list[dict[str, Any]], work: list[Any]) -> list[str]:
    """Each failed chunk (at most `FAILED_SHOWN`): its values, node, why, and its log; the
    hint from the chunk list, else from the split's steps."""
    hints = {
        str(obj(w).get("job_id")): obj(w) for w in work if obj(w).get("job_id")
    }  # fmt: skip
    failed = [r for r in rows if r.get("state") == "failed"]
    lines: list[str] = []
    for row in failed[:FAILED_SHOWN]:
        job = row.get("job_id") or row.get("job")
        step = hints.get(str(job), {})
        values = _values_said(row.get("values"))
        index = row.get("index", row.get("id", "?"))  # a step of `work` has its id only
        head = f"  chunk {index}" + (f" ({values})" if values else "") + ": failed"
        if row.get("node") or step.get("node"):
            head += f" on {row.get('node') or step.get('node')}"
        if _int(row.get("attempts")) and int(row["attempts"]) > 1:
            head += f" after {row['attempts']} tries"
        lines.append(head)
        error = row.get("error") or step.get("error")
        hint = row.get("hint") or step.get("hint")
        if error or hint:
            said = [str(error)] if error else []
            if hint:
                said.append(cli_hint(short(str(hint), 600)))
            lines.append(f"      {': '.join(said)}")
        if job:
            lines.append(f"      what it printed: amsyos logs {job}")
    if len(failed) > FAILED_SHOWN:
        lines.append(f"  … and {len(failed) - FAILED_SHOWN} more failed (chunks.json lists them)")
    return lines


def summary_of(value: Any) -> Any:
    """`summary` (JSON text) read as JSON; as it came when it is not."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def summary_lines(summary: Any) -> list[str]:
    """The split's summary, a line a key: counts, wall seconds, chunks per node, cost."""
    if summary in (None, "", {}, []):
        return []
    if not isinstance(summary, dict):
        return [f"summary: {short(summary, 400)}"]
    lines = ["summary:"]
    for key, value in list(obj(summary).items())[:16]:
        if isinstance(value, dict):
            said = ", ".join(
                f"{k} {amount(v) if isinstance(v, int | float) else short(v, 60)}"
                for k, v in list(obj(value).items())[:12]
            )
        elif isinstance(value, int | float) and not isinstance(value, bool):
            said = amount(value)
        else:
            said = short(value, 200)
        lines.append(f"  {key}: {said}")
    return lines


def _read_rows(saved: list[Saved], inline: Any = None) -> list[dict[str, Any]]:
    """The chunk list from its file among `saved` (or given inline), read as data."""
    for s in saved:
        if s.output == "chunks" or s.path.name.startswith("chunks"):
            if s.size > CHUNKS_MOST_BYTES:
                return []
            try:
                return chunk_rows(json.loads(s.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                return []
    if isinstance(inline, str) and not is_link(inline):
        return chunk_rows(summary_of(inline))
    return chunk_rows(inline) if isinstance(inline, list | dict) else []


def _rel(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


async def finish_split(
    api: Api, out: Out, job_id: str, status: dict[str, Any], folder: Path | None,
    *, again: str,
) -> int:  # fmt: skip
    """What a followed split ended with: its result and chunk list in `folder/<job>/`, the
    summary, each failed chunk's hint. Exit 0 succeeded, 1 not, 3 it waits for somebody."""
    status = with_id(status, job_id)
    state = status.get("state")
    work = seq(status.get("work"))
    if state not in TERMINAL:  # waits for somebody (a go-ahead, an approval)
        out.emit(
            {"job_id": job_id, "state": state, "status": status},
            *status_lines(status),
            f"it waits for somebody; once it is decided: {again}",
        )
        return 3
    target = folder / job_id if folder is not None else None
    if state != "succeeded":
        failure = obj(status.get("failure"))
        partial = {k: v for k, v in obj(failure.get("partial")).items() if is_link(v)}
        saved = await download(partial, target) if partial and target is not None else []
        rows = _read_rows(saved)
        if not rows:  # the steps the status lists stand in for the list
            rows = [obj(w) for w in work if CHUNK_STEP.fullmatch(str(obj(w).get("id") or ""))]
        lines = [status_line(status)]
        if failure.get("error"):
            lines.append(f"  error: {failure['error']}: {cli_hint(str(failure.get('hint') or ''))}")
        kept = [r for r in rows if r.get("state") == "succeeded"]
        if kept:
            lines.append(f"  {len(kept)} chunks done and kept: the same command with --resume "
                         f"{job_id} runs only the rest")  # fmt: skip
        lines += failed_lines(rows, work)
        lines += [f"  file: {_rel(s.path)}  {human(s.size)}" for s in saved]
        answer = {
            "job_id": job_id, "state": state,
            "failure": {k: v for k, v in failure.items() if k not in ("log", "partial")},
            "files": [s.row(Path.cwd()) for s in saved],
            "failed_chunks": [r for r in rows if r.get("state") == "failed"],
        }  # fmt: skip
        out.emit(answer, *lines)
        return 1
    result = obj(status.get("result"))
    if "outputs" not in result:
        result = await api.result(job_id)
    outputs = obj(result.get("outputs"))
    links = {k: v for k, v in outputs.items() if is_link(v)}
    saved = await download(links, target) if links and target is not None else []
    summary = summary_of(outputs.get("summary"))
    rows = _read_rows(saved, outputs.get("chunks"))
    failed = [r for r in rows if r.get("state") == "failed"]
    gathered = next((s for s in saved if s.output == "result"), None)
    lines = [
        f"{job_id}: succeeded, {len(rows) or '?'} chunks"
        + (f" ({len(failed)} failed)" if failed else "")
        + f", cost {amount(result.get('cost'))}"
        + (f", {result['total_s']}s in all" if result.get("total_s") is not None else "")
    ]
    if gathered is not None:
        lines.append(f"  result: {_rel(gathered.path)}  {human(gathered.size)}  {gathered.type}")
    elif "result" in links:
        lines.append(f"  result: not fetched (amsyos result {job_id} result)")
    else:
        lines.append("  result: none gathered (gather none): chunks.json lists each chunk's output")
    lines += [f"  {s.output}: {_rel(s.path)}  {human(s.size)}" for s in saved if s is not gathered]
    lines += summary_lines(summary)
    if failed:
        lines.append(f"{len(failed)} chunk{'s' if len(failed) > 1 else ''} failed:")
        lines += failed_lines(rows, work)
        lines.append(f"the same command with --resume {job_id} runs only the chunks not done")
    answer: dict[str, Any] = {
        "job_id": job_id, "state": "succeeded", "cost": result.get("cost"),
        "total_s": result.get("total_s"), "files": [s.row(Path.cwd()) for s in saved],
        "result": gathered.row(Path.cwd()) if gathered else None, "summary": summary,
        "chunks": len(rows), "failed_chunks": failed, "assets": result.get("assets") or {},
    }  # fmt: skip
    if out.show_urls:
        answer["outputs"] = outputs
    out.emit(answer, *lines)
    return 0


# ---------------------------------------------------------------- the command
async def _start(args: argparse.Namespace, out: Out) -> int:
    local: dict[str, Path] = {}
    over = over_of(args, local)
    names = names_of(over)
    chunk, chunk_files, chunk_texts = params_of("--param", args.params, args.param or [], names)
    given, gather_files, gather_texts = params_of(
        "--gather-param", None, args.gather_param or [], ()
    )
    gather = gather_of(args.gather, given, args.gather_input)
    for note in chunk_notes(chunk, names):
        out.note(note)
    count = chunk_count(over)
    if args.dry_run:
        shown_chunk = {**chunk, **{k: f"@{p}" for k, p in chunk_files.items()}}
        if isinstance(gather, dict):
            named = {k: f"@{p}" for k, p in gather_files.items()}
            gather = {**gather, "params": {**obj(gather.get("params")), **named}}
        shown_over = dict(over)
        if "files" in over:
            shown_over["files"] = [f"@{f}" if f in local else f for f in over["files"]]
        if "rows" in over and over["rows"] in local:
            shown_over["rows"] = f"@{over['rows']}"
        params = build(args, over=shown_over, chunk=shown_chunk, gather=gather)
        lines = [
            f"{SERVICE} would get (nothing sent; local files as @path, uploaded on a real run):",
            json.dumps(params, ensure_ascii=False, indent=2),
        ]
        if count is not None:
            lines.append(f"{count} chunks of {args.service}")
        out.emit({"service_id": SERVICE, "params": params, "chunks": count}, *lines)
        return 0
    creds = credentials()
    async with Api(creds, out) as api:
        await _typed(api, args.service, chunk, chunk_texts, chunk_files)
        if isinstance(gather, dict):
            gather_params = obj(gather.get("params"))
            await _typed(api, str(gather["service"]), gather_params, gather_texts, gather_files)
            gather["params"] = gather_params
        paths = [*local.values(), *chunk_files.values(), *gather_files.values()]
        assets = await _send(api, out, paths)
        if "files" in over:
            over["files"] = [assets[local[f]] if f in local else f for f in over["files"]]
        if "rows" in over and over["rows"] in local:
            over["rows"] = assets[local[over["rows"]]]
        await _long_lists(api, over)
        chunk.update({k: assets[p] for k, p in chunk_files.items()})
        if isinstance(gather, dict) and gather_files:
            gather["params"] = {**obj(gather.get("params")),
                                **{k: assets[p] for k, p in gather_files.items()}}  # fmt: skip
        params = build(args, over, chunk, gather)
        key = idempotency_key(SERVICE, params, creds, fresh=args.new)
        started = await api.run(
            SERVICE, params, key=key, wait_s=START_WAIT_S if args.wait else 0,
            workspace=args.workspace,
        )  # fmt: skip
        job_id = str(started.get("job_id"))
        for line in fixed_lines(started):
            out.note(line)
        data = obj(started.get("data"))
        chunks = _int(data.get("chunks")) or count
        eta = with_basis(eta_text(started), started) or "eta ?"
        said = (
            f"{job_id}: started, " + (f"{chunks} chunks of {args.service}, " if chunks else "")
            + f"{eta}, est. cost {amount(started.get('est_cost'))}"
        )  # fmt: skip
        woke = data.get("woke")
        asked = [*([cli_hint(str(woke))] if isinstance(woke, str) and woke else []),
                 *request_lines(started)]  # fmt: skip
        if asked:
            out.note("\n".join(asked))
        follow_cmd = f"amsyos split {job_id}"
        if not args.wait:
            out.emit(
                started, said,
                f"next: {follow_cmd} follows it (chunks done, in flight, per node) and fetches "
                f"its result into {_rel(Path(args.out) / 'split' / job_id)}/; amsyos cancel "
                f"{job_id} stops its chunks in flight and keeps those done",
            )  # fmt: skip
            return 0
        out.note(said)
        return await _wait(api, out, job_id, obj(started.get("status")) or None, args)


async def _wait(
    api: Api, out: Out, job_id: str, first: dict[str, Any] | None, args: argparse.Namespace
) -> int:
    try:
        status = await follow_split(api, out, job_id, first)
    except (KeyboardInterrupt, asyncio.CancelledError):
        out.note(
            f"stopped waiting; the split goes on: amsyos split {job_id} follows it again, "
            f"amsyos cancel {job_id} stops its chunks in flight (those done are kept)"
        )
        return 130
    folder = None if args.no_download else Path(args.out) / "split"
    return await finish_split(api, out, job_id, status, folder, again=f"amsyos split {job_id}")


async def cmd_split(args: argparse.Namespace, out: Out) -> int:
    """`amsyos split <service> …`, or `amsyos split j_…` to follow one started."""
    if JOB.fullmatch(args.service or ""):
        forms = [
            f for f in ("over", "items", "files", "rows", "product", "param") if getattr(args, f)
        ]
        if forms:
            raise CliError(
                "INVALID_INPUT",
                f"amsyos split {args.service} follows that split; --{forms[0]} goes with a "
                "service: amsyos split <service> --over …",
            )
        async with Api(credentials(), out) as api:
            return await _wait(api, out, args.service, None, args)
    return await _start(args, out)


def add_parsers(add: Any, common: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """`amsyos split …` (main.py's `add`)."""
    del common  # one command, no verbs
    p = add(
        "split", cmd_split,
        "run one service as many chunks over your nodes (frames, files, items, rows), then "
        "gather their outputs",
        "one large job as many chunks of one service, spread over your organisation's nodes "
        "(split.run): each chunk is an ordinary job of that service, placed where it has room "
        "and checked as a direct call would be; the split costs nothing beyond its chunks (and "
        "a rented node's hours). Then one gather of their outputs",
        epilog=SPLIT_HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    p.add_argument("service", help="the chunk's service (t_x.name, name: yours, name@1.2.0), or "
                   "j_… to follow a split started")  # fmt: skip
    p.add_argument("--over", metavar="RANGE", help="a range: 1-240, or 1-240:2 with a step")
    p.add_argument("--chunk", type=int, metavar="N", help="values a chunk takes (items, files: 1)")
    p.add_argument("--items", metavar="JSON", help="a JSON list, or @list.json")
    p.add_argument("--files", nargs="+", metavar="FILE", help="local files or asset://…")
    p.add_argument("--rows", metavar="FILE", help="a CSV or JSON-lines file (or asset://…)")
    p.add_argument("--product", metavar="JSON", help='every combination: {"a": [..], "b": [..]}')
    p.add_argument(
        "--param", action="append", metavar="K=V",
        help="a chunk param (again for more): k=@path uploads a file; 'k=$chunk.start' the "
        "chunk's own value (in single quotes)",
    )  # fmt: skip
    p.add_argument("--params", metavar="JSON", help="the chunk params as one object, or @file")
    p.add_argument(
        "--gather", metavar="KIND|SERVICE",
        help="bundle (default), concat, rows, none, or a service of yours called with the parts",
    )  # fmt: skip
    p.add_argument(
        "--gather-param", action="append", metavar="K=V",
        help="a param of the gather service (again for more)",
    )  # fmt: skip
    p.add_argument(
        "--gather-input", metavar="NAME",
        help="the gather service's input that takes the parts (default parts)",
    )  # fmt: skip
    p.add_argument("--output", metavar="NAME", help="the chunk's output to gather (if several)")
    p.add_argument("--first", type=int, metavar="N", help="chunks run first, alone (default 1)")
    p.add_argument(
        "--retry", type=int, metavar="N",
        help="extra tries of a failed pure/read chunk (default 1, at most 3)",
    )  # fmt: skip
    p.add_argument("--on-failure", choices=ON_FAILURE, help="default auto")
    p.add_argument(
        "--max-credit", type=float, metavar="X",
        help="the most the split may cost (needed when chunks cost credit)",
    )  # fmt: skip
    p.add_argument(
        "--max-in-flight", type=int, metavar="N",
        help="chunks running at once (default: the free slots + 1, at most 32)",
    )  # fmt: skip
    p.add_argument(
        "--nodes", type=int, metavar="N",
        help="rented nodes at once, 1-4, for a service of the rented node",
    )  # fmt: skip
    p.add_argument("--resume", metavar="j_…", help="reuse that split's succeeded chunks")
    p.add_argument(
        "--wait", action="store_true",
        help="follow it, then fetch its result into <out>/split/<job>/",
    )  # fmt: skip
    p.add_argument("--out", default="out", help="default ./out")
    p.add_argument("--no-download", action="store_true", help="with --wait: fetch nothing")
    p.add_argument("--workspace", help="run in this workspace of yours")
    p.add_argument("--new", action="store_true", help="a new split even for the same call")
    p.add_argument("--dry-run", action="store_true", help="print split.run's params; send nothing")
    return {"split": p}


__all__ = [
    "SERVICE",
    "add_parsers",
    "build",
    "chunk_count",
    "cmd_split",
    "finish_split",
    "follow_split",
    "gather_of",
    "over_of",
    "params_of",
    "progress_line",
    "tally",
]
