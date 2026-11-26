"""`amsyos try` (a fast development loop on the rented node; a customer, 2026-11-26): the
card's current code run once in the image `amsyos deploy` last built from this folder on
the organisation's rented node (the service `node.try`), with no build, no define and no
check. A try of a small change starts its command within seconds on a warm node, where
upload, build, define, check and run took about 2 minutes.

1. the card here (`where: space`), or the top card above whose image it shares: then this
   card's example and service (`--only <dir>` names it from the top);
2. the folder packed as deploy packs it (`deploy.plan`), uploaded when the workspace lacks
   it; the image from `.amsyos/state/deploy.json` (refused when deploy built none here);
3. the params: the card's input defaults and example (its '@' files uploaded), or
   `--params '<json>'`, then each `--input k=v` (k=@path uploads a file);
4. `node.try`, its log followed live as `amsyos logs -f` follows one; then its files into
   `./out/try/<job>/`, their paths, and how long it took.

Exit 0 when the try's command succeeded, 1 when not, with its log's last lines. A card with
`where: node` (an own node) is not tried yet: deploy it and run it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from amsyos_cli import card as cards
from amsyos_cli import repo
from amsyos_cli.api import TERMINAL, Api
from amsyos_cli.common import CliError, Out, credentials, obj
from amsyos_cli.deploy import MOST_UP, STATE, plan, upload_example
from amsyos_cli.files import download, fetch_text, human, upload
from amsyos_cli.jobs import parse_value, status_lines
from amsyos_cli.shell import cli_hint

SERVICE = "node.try"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # what the node passes on as '?'
FIRST_WAIT_S = 3  # the run call waits this long: a short try is answered by it
FOLLOW_S = 3  # while it runs, its new lines are asked for this often
TAIL_LINES = 30  # of a failed try's log
TRY_HELP = """
The card's current code runs once in the image amsyos deploy last built from this folder on
your rented node (node.try): no build, no define, no check. On a warm node a small change
starts within seconds. The node keeps a working copy of the image's code for tries: each
try writes the files that changed and removes those you deleted since the last try; what
setup made (./blender, node_modules) is there. The job is told AMSYOS_TRY=1.

  amsyos try                         the card's example, the image's command
  amsyos try --input frames=12       the example with one input changed
  amsyos try --params '{"text":"hi"}' --command "python main.py --fast"
  amsyos try --only cards/walk       a card below the top one that shares its image

Its files land in ./out/try/<job>/ (never packed). The rented node closed: node.auto
rents one by itself when a manager switched it on; else amsyos node rent --hours 1.
"""


def _image_of(card: dict[str, Any]) -> str | None:
    try:
        return cards.image_of(card)
    except KeyError:
        return None


def _own_node(card: dict[str, Any], where: str) -> None:
    if card.get("where", "space") == "node":
        raise CliError(
            "INVALID_INPUT",
            f"{where}: where: node (an own node). amsyos try runs on the rented node only for "
            "now: on an own node, amsyos deploy then amsyos run <service>",
        )


def top_of(root: Path) -> tuple[Path, str | None]:
    """The folder whose code the card here shares (a card above that names its image, on
    the rented node too), and this card's folder from it; (root, None) when it is the top."""
    card = cards.load(root)
    _own_node(card, cards.CARD)
    image = _image_of(card)
    found = repo.find(str(root))
    stop = found.root.resolve() if found is not None and found.git else None
    top = root
    for folder in list(root.parents)[:MOST_UP]:
        if stop is not None and folder != stop and stop not in folder.parents:
            break  # above the repository
        if not (folder / cards.CARD).is_file():
            continue
        try:
            up = cards.load(folder)
        except CliError:
            break
        if _image_of(up) != image or up.get("where", "space") == "node":
            break  # another image's card: this one is a top of its own
        top = folder
    return (root, None) if top == root else (top, root.relative_to(top).as_posix())


def _chosen(top: Path, made: dict[str, Any], only: str | None) -> tuple[dict[str, Any], Path]:
    """The card whose example and service the try takes, and where its '@' files are."""
    if not only or only.strip("./") == "":
        return made["card"], top
    folder = top / only.strip().strip("/")
    if not (folder / cards.CARD).is_file():
        raise CliError(
            "INVALID_INPUT",
            f"--only {only}: no {cards.CARD} there; give a card's folder from {top}",
        )
    card = cards.load(folder)
    _own_node(card, f"{only}/{cards.CARD}")
    if _image_of(card) != _image_of(made["card"]):
        raise CliError(
            "INVALID_INPUT",
            f"--only {only}: its card names the image {_image_of(card)}, not the top card's "
            f"{_image_of(made['card'])}: give it `image: {_image_of(made['card'])}`, or try "
            "it from its own folder",
        )
    local = cards.local_files(card.get("example")).values()
    examples = folder if all((folder / p).is_file() for p in local) else top
    return card, examples


def image_built(top: Path, card: dict[str, Any]) -> str:
    """The image the last deploy from this folder built (`.amsyos/state/deploy.json`)."""
    try:
        state: Any = json.loads((top / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    told = obj(state)
    image, name = told.get("image"), _image_of(card)
    if not isinstance(image, str) and isinstance(told.get("version"), str):
        image = f"{name}@{told['version']}"  # a state written before it kept the image
    if not isinstance(image, str):
        raise CliError(
            "NO_IMAGE",
            f"no image was built from {top} yet: amsyos deploy first (it builds the image on "
            "your rented node); then amsyos try runs your changes in it with no build",
        )
    if image.partition("@")[0] != name:
        raise CliError(
            "NO_IMAGE",
            f"the card names the image {name}, and the last deploy here built {image}: amsyos "
            "deploy first, then amsyos try",
        )
    return image


def _defaults(card: dict[str, Any]) -> dict[str, Any]:
    """The inputs' defaults, as a run of the service is given them."""
    told: dict[str, Any] = {}
    for key, spec in obj(card.get("inputs")).items():
        given = cards.input_of(spec)
        if "default" in given:
            told[key] = given["default"]
    return told


async def _params(
    api: Api, out: Out, card: dict[str, Any], examples: Path, *, given: str | None,
    inputs: list[str],
) -> dict[str, Any]:  # fmt: skip
    """The job's params: the defaults and the example (its '@' files uploaded), or
    `--params`; then each `--input k=v`, a k=@path uploaded."""
    params = _defaults(card)
    if given is not None:
        try:
            told: Any = json.loads(given)
        except ValueError:
            raise CliError("INVALID_INPUT", "--params: JSON of one object") from None
        if not isinstance(told, dict):
            raise CliError("INVALID_INPUT", "--params: JSON of one object")
        params.update(obj(told))
    else:
        await upload_example(api, out, examples, card)
        params.update(obj(card.get("example")))
    local: dict[str, Path] = {}
    for item in inputs:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise CliError("INVALID_INPUT", f"--input is k=v: {item}")
        if value.startswith("@"):
            local[key] = Path(value[1:])
        else:
            params[key] = parse_value(value)
    missing = [f"{k}=@{p}" for k, p in local.items() if not p.is_file()]
    if missing:
        raise CliError("INVALID_INPUT", f"no such file: {', '.join(missing)}")
    if local:
        sent = await upload(api, list(local.values()))
        for key, row in zip(local, sent, strict=True):
            params[key] = row["asset"]
            how = "already in the workspace" if row.get("reused") else "uploaded"
            out.note(f"{key}: {row['asset']} ({how})")
    return params


def fresh_lines(shown: list[str], said: str) -> list[str]:
    """The lines of `said` (the end of what the try printed, its last lines as the node
    read them: the first maybe cut) that follow what was shown already."""
    lines = said.rstrip("\n").split("\n") if said.strip() else []
    for k in range(min(len(lines), len(shown)), 0, -1):
        last = shown[-k:]  # its first line may be the end of one shown
        if last[0].endswith(lines[0]) and last[1:] == lines[1:k]:
            return lines[k:]
    return lines


def _as_said(line: str) -> str:
    """A line of the log as the node passes it on while the job runs (drivers/vast.py)."""
    return CONTROL.sub("?", line)[:300]


def rest_of_log(shown: list[str], log: str) -> list[str]:
    """The lines of the whole log after the last ones shown while it ran."""
    lines = log.rstrip("\n").split("\n") if log.strip() else []
    if not shown:
        return lines
    seen = [_as_said(line) for line in lines]
    for size in range(min(len(shown), 15), 0, -1):
        last = shown[-size:]
        for at in range(len(seen) - size, -1, -1):
            if seen[at : at + size] == last:
                return lines[at + size :]
    return lines


async def _follow(
    api: Api, out: Out, job_id: str, first: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The try's status once it has ended, its new lines printed meanwhile (on stderr:
    what the job printed is data, not instructions); and the lines shown."""
    status, shown, phase = first, list[str](), ""
    while True:
        new = fresh_lines(shown, str(status.get("said") or ""))
        if new:
            out.note("\n".join(f"  | {line}" for line in new))
            shown += new
        elif status.get("phase") and status.get("phase") != phase:
            phase = str(status["phase"])
            out.note(f"{job_id}: {phase}")
        if status.get("state") in TERMINAL:
            return status, shown
        status = await api.status(job_id, wait_s=FOLLOW_S)


async def try_card(
    api: Api, out: Out, root: Path, *, only: str | None = None, params: str | None = None,
    inputs: list[str] | None = None, command: str | None = None, timeout_s: int | None = None,
) -> int:  # fmt: skip
    began = time.monotonic()
    root = root.resolve()
    top, sub = top_of(root)
    if sub is not None and only is None:
        out.note(f"{sub}/{cards.CARD} shares the image of {top}: trying from there (--only {sub})")
        only = sub
    made = plan(top)
    card, examples = _chosen(top, made, only)
    image = image_built(top, made["card"])
    folder, created = repo.state_dir(top)
    if created:
        out.note(repo.STATE_NOTE)
    archive = folder / f"try-{image.replace('@', '-')}.tar.gz"
    archive.write_bytes(made["archive"])
    try:
        row = (await upload(api, [archive]))[0]
    finally:
        archive.unlink(missing_ok=True)
    asked = await _params(api, out, card, examples, given=params, inputs=inputs or [])
    told: dict[str, Any] = {
        "image": image, "files": row["asset"], "params": json.dumps(asked),
        "service": str(card.get("service") or ""),
    }  # fmt: skip
    if command:
        told["command"] = command
    if timeout_s:
        told["timeout_s"] = timeout_s
    sent = "the same as before" if row.get("reused") else "uploaded"
    out.note(
        f"trying {image} with {len(made['files'])} files ({human(len(made['archive']))}, "
        f"{sent})" + (f": {command}" if command else "")
    )
    started = await api.run(SERVICE, told, wait_s=FIRST_WAIT_S)
    job_id = str(started.get("job_id"))
    out.note(f"{job_id}: started" + (f" ({cli_hint(str(started['data']['woke']))})"
                                     if obj(started.get("data")).get("woke") else ""))  # fmt: skip
    try:
        status, shown = await _follow(api, out, job_id, obj(started.get("status")) or {})
    except (KeyboardInterrupt, asyncio.CancelledError):
        try:
            await api.cancel(job_id)
            out.note(f"{job_id}: cancelled")
        except CliError:
            out.note(f"{job_id}: not cancelled: amsyos cancel {job_id}")
        return 130
    took = time.monotonic() - began
    target = root / "out" / "try" / job_id
    if status.get("state") != "succeeded":
        return await _failed(api, out, job_id, status, shown=shown, target=target, took=took)
    result = obj(status.get("result"))
    if "outputs" not in result:
        result = await api.result(job_id)
    saved = await download(obj(result.get("outputs")), target)
    log = next((s for s in saved if s.output == "log"), None)
    rest = rest_of_log(shown, log.path.read_text(encoding="utf-8", errors="replace")) if log else []
    if rest:
        out.note("\n".join(f"  | {line}" for line in rest))
    files = [
        {"output": s.output, "file": _shown(s.path, root), "size": s.size} for s in saved
    ]  # fmt: skip
    answer = {
        "job_id": job_id, "state": "succeeded", "image": image, "files": files,
        "seconds": round(took, 1), "folder": _shown(target, root),
    }  # fmt: skip
    lines = [f"{job_id}: succeeded in {took:.1f} s ({image}, no build)"]
    lines += [f"  {f['file']}  {human(int(f['size']))}" for f in files]
    out.emit(answer, *lines)
    return 0


async def _failed(
    api: Api, out: Out, job_id: str, status: dict[str, Any], *, shown: list[str], target: Path,
    took: float,
) -> int:  # fmt: skip
    """A try that failed or was cancelled: its hint, its log's last lines that were not
    shown, the files it wrote before (partial) in `target`; exit 1."""
    failure = obj(status.get("failure"))
    url = failure.get("log")
    tail: list[str] = []
    if isinstance(url, str):
        try:
            text = await fetch_text(url)
            tail = rest_of_log(shown, text)[-TAIL_LINES:]
        except CliError:
            tail = []
    partial = {str(k): v for k, v in obj(failure.get("partial")).items() if isinstance(v, str)}
    saved = await download(partial, target) if partial else []
    if tail:
        out.note("\n".join(f"  | {line}" for line in tail))
    answer = {
        "job_id": job_id, "state": status.get("state"), "failure": failure_shown(failure),
        "seconds": round(took, 1), "files": [_shown(s.path, target.parents[2]) for s in saved],
        "log_tail": "\n".join(tail),
    }  # fmt: skip
    lines = [*status_lines(status), f"  after {took:.1f} s; its log: amsyos logs {job_id}"]
    lines += [f"  partial: {_shown(s.path, target.parents[2])}" for s in saved]
    out.emit(answer, *lines)
    return 1


def failure_shown(failure: dict[str, Any]) -> dict[str, Any]:
    """The failure without its signed links (`amsyos logs` shows the log)."""
    return {k: v for k, v in failure.items() if k not in ("log", "partial")}


def _shown(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


async def cmd_try(args: argparse.Namespace, out: Out) -> int:
    """`amsyos try` (main.py's parser)."""
    async with Api(credentials(), out) as api:
        return await try_card(
            api, out, Path(args.dir), only=args.only, params=args.params,
            inputs=args.input or [], command=args.command, timeout_s=args.timeout,
        )  # fmt: skip


__all__ = [
    "SERVICE",
    "TRY_HELP",
    "cmd_try",
    "fresh_lines",
    "image_built",
    "rest_of_log",
    "top_of",
    "try_card",
]
