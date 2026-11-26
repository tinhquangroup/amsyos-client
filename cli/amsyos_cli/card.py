"""`amsyos.yaml`: the file form of a contract-1 card plus how the service is built
(spec v3 §7.1). Proposed by `amsyos init` from the code it finds (amsyos_cli/infer.py),
read by `amsyos deploy` and turned into the `service` of `node.build` ({name, version,
spec}, spec v2 §21, §25). `amsyos card --help` explains every key (CARD_HELP).

The card's inputs may be written in JSON-Schema words (`type: string`, `enum: [...]`,
`type: file`) or in the manifest's own (`type: str`, `type: enum, values: [...]`,
`type: asset`); both become the manifest's. What init could not read from the code is
written `<fill: ...>`; deploy refuses a card that still holds one, or has no example.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from amsyos_cli import infer, naming, standards
from amsyos_cli.common import CliError, obj, seq

CARD = "amsyos.yaml"
NAME_RE = naming.NAME_RE  # area.action or area.group.action (4 words warned, naming.py)
VERSION_RE = re.compile(r"\d{1,6}\.\d{1,6}(\.\d{1,6})?")  # "1.0", or "1.0.3"
FILL = "<fill"  # what init could not read from the code starts so
RUNTIMES = ("python", "node", "java", "go", "r")
# JSON-Schema words → the manifest's input types (kernel/manifest.py InputType).
TYPES = {
    "string": "str", "str": "str", "text": "str",
    "integer": "int", "int": "int",
    "number": "float", "float": "float",
    "boolean": "bool", "bool": "bool",
    "enum": "enum",
    "file": "asset", "asset": "asset",
}  # fmt: skip
INPUT_KEYS = (
    "required", "default", "values", "min", "max", "max_length", "multiline",
    "description", "example", "means", "schema",
)  # fmt: skip
SPEC_KEYS = (
    "tags", "docs", "schemas", "contract", "controls", "pilot", "estimate_per", "tests", "cache",
)  # fmt: skip
# A card's `docs` (a customer, 2026-11-26): what describe shows whole, read without a machine.
DOCS_MAX = 4000
# A schema named by its file (`schema: plan.json`, `schemas: {totals: totals.json}`) is read
# from the card's `schemas/` folder into the spec (`load`), as the kernel reads a package's.
SCHEMA_DIR = "schemas"
SCHEMA_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\.json")
UNREAD = "_schemas_unread"  # a schema file `load` could not read → why; `problems` says it
# What a file of each format is, when the card does not say (services/core/types.yaml).
MEANS_OF_FORMAT = {
    **dict.fromkeys(("png", "jpg", "jpeg", "webp", "gif", "tif", "tiff", "svg"), "image"),
    **dict.fromkeys(("mp4", "mov", "webm", "mkv"), "video"),
    **dict.fromkeys(("wav", "mp3", "ogg", "flac", "m4a"), "audio"),
    **dict.fromkeys(("csv", "xlsx"), "sheet"),
    **dict.fromkeys(("glb", "gltf", "obj"), "mesh"),
    **dict.fromkeys(("srt", "vtt"), "subtitles"),
    **dict.fromkeys(("cube", "3dl"), "lut"),
    **dict.fromkeys(("zip", "tar", "gz", "tgz"), "archive"),
    "json": "report", "txt": "text", "md": "text", "pdf": "document.pdf",
    "html": "web.page", "parquet": "table", "docx": "document", "pptx": "document.slides",
    "patch": "patch", "diff": "patch", **dict.fromkeys(("npy", "npz", "bin"), "data"),
}  # fmt: skip
# The formats an output may have, which AMSYOS's check reads (kernel/conform.py KNOWN_FORMATS):
# what an enum input that chooses an output's format (`controls`) may offer.
OUTPUT_FORMATS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "psd", "pdf", "mp4", "m4a", "mov",
    "webm", "mkv", "wav", "mp3", "ogg", "flac", "zip", "xlsx", "docx", "pptx", "odt", "glb",
    "parquet", "npy", "npz", "gz", "tgz", "tar", "json", "gltf", "geojson", "txt", "md", "csv",
    "srt", "vtt", "html", "obj", "bvh", "cube", "3dl", "bin", "svg",
})  # fmt: skip
# What a file input is, by the words of its name.
MEANS_OF_WORD = (
    (("csv", "table", "sheet", "data", "rows", "spreadsheet"), "sheet"),
    (("image", "photo", "picture", "img", "png", "jpg"), "image"),
    (("video", "clip", "film", "movie", "mp4"), "video"),
    (("audio", "sound", "voice", "speech", "music"), "audio"),
    (("mesh", "model", "glb", "scene"), "mesh"),
    (("pdf", "invoice", "contract", "doc", "document"), "document"),
    (("text", "txt", "notes", "transcript"), "text"),
)
SAMPLE_EXT = {
    "sheet": ".csv", "image": ".png", "video": ".mp4", "audio": ".wav", "mesh": ".glb",
    "document": ".pdf", "text": ".txt",
}  # fmt: skip
# What an example's local file (`@path`) stands as in the spec `service.check` is sent,
# when the workspace does not hold its content yet (deploy uploads it first).
EXAMPLE_FILE = "asset://a_example_file"
# A test of the card (spec v3 §7.1): `{output, check?, pass: [[field, op, value]]}`.
TEST_KEYS = frozenset({"output", "check", "pass"})
OPS = ("==", "!=", "<", "<=", ">", ">=", "in", "contains")
SERVICE_RE = re.compile(
    r"(t_[a-z0-9_]{1,40}\.)?[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}(@[\w.:-]+)?"
)
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", "out",
    "dist", "build", ".amsyos", ".claude", ".cursor", ".agent", ".idea", ".vscode",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "target", ".amsyos-home",
})  # fmt: skip


# ---------------------------------------------------------------- what every key is
PACK_RULES = """\
What deploy packs (deploy prints the list, names and sizes, before it sends anything;
`amsyos deploy --dry-run` prints it and stops):
  - in a git repository, the files git tracks or would add (.gitignore honoured);
    elsewhere every file less what the top .gitignore lists;
  - never a dot-folder (.git, .claude, .cursor, .amsyos, ...), out/, node_modules/,
    __pycache__/, build/, dist/, a virtualenv, *.pyc;
  - never an agent's instruction file: CLAUDE.md, AGENTS.md, GEMINI.md and the like
    (at any depth), *.mdc, .cursorrules, and every *.md at the top except README.md
    and files the card names (in command, setup, example, or include: [notes.md]);
  - never a credentials file (.env, *credentials*.json, a private key): deploy refuses
    until it is listed in .gitignore or moved out.
"""
CARD_HELP_TEXT = """\
amsyos.yaml: this repository as an AMSYOS service (spec v3 §7.1). One file at the
repository's top; `amsyos init` proposes it from the code, `amsyos deploy` builds it.
Values init could not read from the code are written <fill: ...>: deploy refuses them.

  service     area.action, or area.group.action: snake_case words with dots, like
              blender.render or blender.template.logo. Becomes
              t_<organisation>.blender.render; it is what `amsyos run` names. area: the
              tool or the domain a client looks under (blender, video, sound, invoice),
              never your organisation's name nor a word all its services share; group: a
              product line inside the area; action: what it does (render, find,
              apply_plan, make; a catalogue's is list). No version, model, date or round
              (version and the channels dev and stable carry them; experiments go on
              dev). card --check warns of a name off the standard and suggests one; a
              deployed service moves with amsyos rename <old> <new>.
  version     "major.minor" ("1.0") or "major.minor.patch" ("1.0.3"), in quotes. With
              two numbers deploy counts the patch: one more than the newest defined of
              that major.minor. Raise the minor for a change callers should notice.
  summary     What one call takes and makes, in English, 3 to 120 characters. discover
              finds the service by it.
  tags        Up to 20 words discover also finds it by: [csv, chart, plot].
  docs        What a caller should read before a call that no input's description (at
              most 200 characters) holds, like every word a plain-words input takes:
              text, Markdown allowed, at most 4000 characters, in English; the words the
              service reads stay as they are in `backticks`. `amsyos describe` shows it
              whole, without starting a machine. Several lines: docs: | and the text
              indented below.
  where       space: the organisation's rented node (default). node: an own node, a
              Linux computer of the organisation's joined with AMSYOS Node, at full:
              deploy builds there by node.exec (an admin), --node n_… when there are
              several; free (`amsyos deploy --help` says the steps).
  image       The image's name (default: the service's, dots as _). Cards naming the same
              image share it. On a rented node each build replaces what it runs for all of
              them, and AMSYOS runs the others' examples again; the version is the card's.
              On an own node (where: node), deploy in the top card's folder builds it once
              and deploys every card below it that names it with where: node, each with its
              own name, version, inputs and tests, its command run in the top card's folder
              (--only services/a,. picks some; job.service tells their jobs apart).
              A top card of where: space deploys alone; --only <its own-node cards> builds
              them on the own node. No top card: deploy in the folder above cards side by
              side builds it there once (the cards agree on runtime, system, packages,
              models and setup; --image picks one image). When only cards changed since
              the node's build, deploy defines them again with no build.
  runtime     python | node | java | go | r. python is 3.11 with PyTorch.
  system      Programs from conda-forge, name or name=version: [ffmpeg, imagemagick=7.1.2].
              init proposes those the code runs (subprocess, os.system, shutil.which:
              ffmpeg and ffprobe → ffmpeg). Blender is not on conda-forge: setup fetches it.
              On an own node their libraries are in $CONDA_PREFIX/lib, first on
              LD_LIBRARY_PATH for setup and jobs: start a system program that must keep its
              own libraries with LD_LIBRARY_PATH unset.
  packages    pip packages (Python): [numpy==2.1.3]. Empty: requirements.txt is used;
              without one, init proposes the packages the code imports.
  setup       A command run once when the image is made: "npm ci", "go build -o app .".
              A big download: "amsyos-fetch <url> --sha256 <hex> --to <path> [--unpack]
              [--strip 1]": once a machine, one copy on the disk for all your images.
              It runs in a folder of its own that is then moved to the image's: write
              paths relative to the code's folder, never where setup ran.
  command     The command started once a job, in the code's folder: python main.py.
  serve       Instead of command or beside it: a server kept warm between jobs
              (python server.py); see `amsyos describe node.build`, input serve.
  models      Public Hugging Face models the code loads, pinned: ["org/name@<40 hex>"].
  resources   What one job takes of the machine: {gpu: none|optional|required,
              min_vram_gb: 12, cpu: 2, ram_gb: 8, disk_gb: 4}.
  effect      pure (outputs from inputs alone) | read (reads data elsewhere) | write
              (changes data kept here) | external (acts outside AMSYOS; never repeated
              by AMSYOS by itself).
  estimate    How long one call takes, until runs measure it: 30s, 5m.
  inputs      What a call gives, by name (snake_case):
                name: {type: string|integer|number|boolean|file, required: true,
                       default: ..., description: "...", max_length: 200, min: 0, max: 9}
                name: {enum: [line, bar], default: line}
                name: {type: string, default: ''}   (optional; empty text when left out)
                name: {type: string, multiline: true, max_length: 12000}   (several lines:
                       a script, subtitles, a prompt; else one line)
                name: {type: file, means: sheet}    (means: what the file is; below)
                name: {type: string, multiline: true, schema: name.json}   (structured:
                       a plan, a scene, nested options. One JSON input, its shape a JSON
                       Schema in schemas/name.json: AMSYOS refuses a call that does not fit
                       before a machine starts, and clients see the shape. `amsyos schema
                       name --from-model main.py:Plan` (or --from-example) writes it; the
                       code reads it with job.json("name"). Also on a file of JSON.)
              The program reads them from $AMSYOS_JOB/job.json ({"params": {...},
              "inputs": {"name": "in/<file>"}}), or `from amsyos import job` (job.params,
              job.input("name"), job.json("name"): a JSON input parsed; job.service, the
              id of the service the job is for, which tells apart the cards that share an
              image; job.service_version; job.folder, the job's folder), or `amsyos-job
              param name` in any language.
  outputs     What a call makes, one file each: name: {type: file, format: png,
              means: image}. The program writes out/<name>.<format> in its job folder.
              Data a program reads back: format npy, npz, bin or parquet, means data.
              JSON another service reads: means motion, timeline, plan, report or geo,
              by what it is, or a type of your own, t_<organisation>.<name> (any format),
              which the reading service's input names too.
              A format the call chooses: audio: {type: file, format: wav, controls:
              format, means: audio} with the input format: {enum: [wav, mp3], default:
              wav}; the program writes out/audio.<the chosen value>, one file.
              Every output is required: the version's check fails when the example
              made no file for it. One only some calls make says optional: true:
              preview: {type: file, format: mp4, means: video, optional: true}.
              An output of means patch (a unified diff) is applied by
              `amsyos result <job> --apply`.
  means       What a file is, with the formats it may have: image png jpg webp gif tif
              psd; video mp4 mov webm mkv; audio wav mp3 ogg flac m4a; subtitles srt vtt;
              text txt md; document pdf docx pptx xlsx odt html md txt; report json txt
              md html pdf; sheet csv xlsx; table parquet; data npy npz bin parquet; plan
              json; timeline json; motion json bvh; geo geojson json; mesh glb gltf obj;
              web html; archive zip tar gz tgz. A narrower one (image.portrait) as
              `amsyos discover` names it; your own, t_<organisation>.<name>, any format.
  example     One whole call's inputs: {csv: "@samples/sales.csv", x: month}. A value
              starting with @ is a local file deploy uploads first. deploy runs it once
              as the check of the new version; every required input needs a value.
  tests       Deterministic checks of the example's outputs:
                - {output: report, pass: [["rows_read", "==", 12]]}
                - {output: video, check: t_x.video.check, pass: [["frames", "==", 24]]}
              output: one of outputs. check: a service whose JSON report the test
              reads; it takes that output as its file input (a service of yours
              that reports on a file; `amsyos discover check` finds those you may call).
              Left out, the output itself is read (a json output). pass: [field, op,
              value]: field a key of the report (a.b inside one), op one of
              == != < <= > >= in contains. `amsyos card --check` checks each test
              against the services you may call; AMSYOS runs them after the
              example succeeds, and a failing test fails the version's check.
  include     Files deploy would leave out but the service needs: [notes.md].
  schemas, contract, controls, pilot, estimate_per: as service.check's spec "{}"
              lists them (`amsyos run service.check spec='{}' --wait`). A schema may be
              named by its file in schemas/ (schemas: {totals: totals.json}).

{pack}
Check a card: `amsyos card --check`, here and then by AMSYOS's service.check when
logged in (free; nothing is defined). `amsyos card --spec` prints the spec the card
becomes. See what deploy would send: `amsyos deploy --dry-run`. In open standards:
`amsyos card --export mcp|a2a` (an MCP tool definition, an A2A Agent Card); `amsyos init
--from-mcp tool.json` starts a card from an MCP tool's definition.
"""


CARD_HELP = CARD_HELP_TEXT.replace("{pack}", PACK_RULES)


# ---------------------------------------------------------------- reading
def load(root: Path, *, inline: bool = True) -> dict[str, Any]:
    """amsyos.yaml as a mapping; with `inline`, each schema it names by file read into it
    (`inline_schemas`), so that the spec carries it as the kernel takes it."""
    path = root / CARD
    try:
        text = path.read_text(encoding="utf-8")
        raw: Any = yaml.safe_load(text)
    except FileNotFoundError:
        raise CliError("NO_CARD", f"no {CARD} in {root}: amsyos init writes one") from None
    except yaml.YAMLError as exc:
        raise CliError("INVALID_CARD", f"{CARD} is not YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise CliError("INVALID_CARD", f"{CARD} is a mapping of fields (service, version, …)")
    card: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    if isinstance(card.get("version"), int | float):
        # `version: 1.10` unquoted reads as the number 1.1: the file's own text is meant.
        found = re.search(r"(?m)^version:\s*([0-9][0-9.]*)\s*(?:#.*)?$", text)
        card["version"] = found.group(1) if found else str(card["version"])
    if inline:
        inline_schemas(card, root)
    return card


def schema_file(root: Path, named: str) -> tuple[dict[str, Any] | None, str | None]:
    """The schema a card names by its file, read from its `schemas/` folder, or why not."""
    if not SCHEMA_FILE.fullmatch(named):
        return None, f"{named[:60]!r}: a plain file name ending .json, in {SCHEMA_DIR}/"
    path = root / SCHEMA_DIR / named
    try:
        data: Any = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None, f"no file {SCHEMA_DIR}/{named}"
    except (OSError, ValueError) as exc:
        return None, f"{SCHEMA_DIR}/{named} is not JSON ({str(exc)[:120]})"
    if not isinstance(data, dict):
        return None, f"{SCHEMA_DIR}/{named}: a JSON Schema is a JSON object"
    return obj(data), None


def inline_schemas(card: dict[str, Any], root: Path) -> None:
    """`schema: plan.json` on an input and `schemas: {totals: totals.json}`: each file of the
    card's `schemas/` folder read in its place; one that cannot be is kept as written, and
    why is kept under UNREAD for `problems`."""
    unread: dict[str, str] = {}
    for in_name, spec in obj(card.get("inputs")).items():
        named = obj(spec).get("schema")
        if isinstance(named, str):
            data, why = schema_file(root, named)
            if data is None:
                unread[f"inputs.{in_name}.schema"] = why or named
            else:
                spec["schema"] = data
    outputs = obj(card.get("schemas"))
    for out_name, named in list(outputs.items()):
        if isinstance(named, str):
            data, why = schema_file(root, named)
            if data is None:
                unread[f"schemas.{out_name}"] = why or named
            else:
                outputs[out_name] = data
    if unread:
        card[UNREAD] = unread


def problems(card: dict[str, Any]) -> list[str]:
    """Every problem of the card's form, at once (AMSYOS need not be asked about these)."""
    found: list[str] = []
    name = card.get("service")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        found.append(
            "service: area.action or area.group.action, snake_case words with dots, like "
            "csv.chart (`amsyos card --help`, service)"
        )
    if not VERSION_RE.fullmatch(str(card.get("version", ""))):
        found.append('version: "1.0" (deploy counts the patch) or "1.0.3", in quotes')
    if "image" in card and not (
        isinstance(card["image"], str) and IMAGE_RE.fullmatch(card["image"])
    ):
        found.append("image: a lowercase letter, then letters, digits or _ (at most 40)")
    summary = card.get("summary")
    if not isinstance(summary, str) or not 3 <= len(summary) <= 120:
        found.append("summary: what one call takes and makes, in English, 3 to 120 characters")
    where = card.get("where", "space")
    if where not in {"space", "node"}:
        found.append("where: space (the organisation's rented node) or node (an own node)")
    if card.get("runtime", "python") not in RUNTIMES:
        found.append(f"runtime: one of {', '.join(RUNTIMES)}")
    if not card.get("command") and not card.get("serve"):
        found.append("command (started once a job) or serve (a server kept warm), or both")
    outputs = card.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        found.append(
            "outputs: at least one, like {chart: {type: file, format: png, means: image}}; "
            "the program writes out/<name>.<format>"
        )
    else:
        for out_name, out in obj(outputs).items():
            if _format(out) is None:
                found.append(f"outputs.{out_name}: a format, like png or {{format: png}}")
            if obj(out).get("optional") not in (None, True, False):
                found.append(
                    f"outputs.{out_name}.optional: true (some calls make no such file) or false"
                )
        found += chosen_problems(card)
    inputs: Any = card.get("inputs") or {}
    if not isinstance(inputs, dict):
        found.append("inputs: a mapping of input name to its type")
    else:
        for in_name, given in obj(inputs).items():
            spec = obj(given)
            kind = spec.get("type") if spec else given
            if "enum" not in spec and str(kind) not in TYPES:
                found.append(f"inputs.{in_name}.type: one of {', '.join(sorted(set(TYPES)))}")
            found += enum_problems(str(in_name), spec)
            # `default: ''` says an optional text input is empty when left out
            # (a customer's feedback); the manifest gets no default (`input_of`).
            if spec.get("default") == "" and "enum" not in spec:
                if TYPES.get(str(kind or "string")) != "str":
                    found.append(
                        f"inputs.{in_name}.default: '' is no {kind}: drop it (`default: ''` "
                        "is for a text input that may be left out)"
                    )
                elif spec.get("required"):
                    found.append(
                        f"inputs.{in_name}.default: '' on a required input: drop one of them "
                        "(`default: ''` says it may be left out, as empty text)"
                    )
    for field_, why in obj(card.get(UNREAD)).items():
        name = field_.split(".")[1]
        found.append(
            f"{field_}: {why}: amsyos schema {name} --from-model <file.py>:<Model> or "
            f"--from-example writes it (or drop the line: a card without a schema still works)"
            if field_.startswith("inputs.")
            else f"{field_}: {why}"
        )
    for key in ("system", "packages", "models", "tags", "include"):
        if key in card and not isinstance(card[key], list | str):
            found.append(f"{key}: a list, like [ffmpeg]")
    docs = card.get("docs")
    if docs is not None and not (isinstance(docs, str) and 0 < len(docs.strip()) <= DOCS_MAX):
        had = f" (it has {len(docs)})" if isinstance(docs, str) and docs.strip() else ""
        found.append(
            f"docs: text of at most {DOCS_MAX} characters{had}, Markdown allowed; several "
            "lines as docs: | with the text indented below"
        )
    return found + test_problems(card)


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def in_enum(value: Any, values: list[Any]) -> bool:
    """`value` is one of `values` by value: 90 and 90.0 are int 90, and "90" is too when
    the values are numbers (as the kernel matches a param, kernel/manifest.py)."""
    if _number(value):
        return any(_number(v) and v == value for v in values)
    if isinstance(value, str) and values and all(_number(v) for v in values):
        try:
            number = float(value)
        except ValueError:
            return False
        return any(v == number for v in values)
    return value in values


def enum_problems(name: str, spec: dict[str, Any]) -> list[str]:
    """An enum's values all text or all numbers, never mixed, and its default one of them:
    what `service.check` says of them, said here first (a customer's feedback: the local check
    passed what the server refused)."""
    key = "enum" if "enum" in spec else "values" if spec.get("type") == "enum" else None
    if key is None:
        return []
    values = spec.get(key)
    if not isinstance(values, list) or not values:
        return [f"inputs.{name}.{key}: a list of the values a caller may give, like [a, b]"]
    listed = seq(values)
    texts, numbers = all(isinstance(v, str) for v in listed), all(_number(v) for v in listed)
    if not texts and not numbers:
        return [
            f"inputs.{name}.{key}: all text or all numbers, not mixed ({_flow(listed)}); "
            "quote each to make them text"
        ]
    default = spec.get("default")
    if default is not None and not in_enum(default, listed):
        return [f"inputs.{name}.default: {_flow(default)} is not one of {_flow(listed)}"]
    return []


def _enum_of(spec: dict[str, Any]) -> list[Any]:
    """An input's enum values as the card writes them: `enum: [...]` or `type: enum, values`."""
    return seq(spec["enum"] if "enum" in spec else spec.get("values"))


def chosen_problems(card: dict[str, Any]) -> list[str]:
    """Each output whose format an input chooses (`controls`), as AMSYOS checks it
    (kernel/manifest.py `_check_chosen`): an enum input of formats an output may have, its
    default the output's format (or, without one, the output's format one of its values)."""
    inputs = obj(card.get("inputs"))
    found: list[str] = []
    for out_name, out in obj(card.get("outputs")).items():
        by = obj(out).get("controls")
        if by is None:
            continue
        where = f"outputs.{out_name}.controls"
        given = obj(inputs.get(by)) if isinstance(by, str) else {}
        values = _enum_of(given)
        if not given or not values or not all(isinstance(v, str) for v in values):
            found.append(
                f"{where}: {_flow(by)} must be an enum input of the card whose values are formats, "
                f"like format: {{enum: [wav, mp3], default: wav}}"
            )
            continue
        bad = [str(v) for v in values if v not in OUTPUT_FORMATS]
        if bad:
            found.append(
                f"inputs.{by}: {', '.join(bad)}: no format an output may have (png, mp4, wav, mp3, "
                "json, txt, npy, ...)"
            )
        fmt, default = _format(out), given.get("default")
        if default is not None and default != fmt:
            found.append(f"outputs.{out_name}.format: {default}, the default of input {by}")
        elif fmt not in values:
            found.append(
                f"outputs.{out_name}.format: one of the values of input {by} ({_flow(values)})"
            )
    return found


def optional_outputs(card: dict[str, Any]) -> set[str]:
    """The outputs a call may leave unmade (`optional: true`): every other one is required,
    and the version's check (a job on an own node too) fails without its file
    (kernel/manifest.py `output_required`)."""
    return {
        str(n) for n, out in obj(card.get("outputs")).items() if obj(out).get("optional") is True
    }


def chosen_formats(card: dict[str, Any], params: Any) -> dict[str, str]:
    """Each output's format in a call of these params (`run --local` checks the file by it):
    its own, or the value the call gave the input that chooses it (`controls`), that input's
    default when left out."""
    inputs = obj(card.get("inputs"))
    told: dict[str, str] = {}
    for out_name, out in obj(card.get("outputs")).items():
        fmt, by = _format(out), obj(out).get("controls")
        if isinstance(by, str):
            value = obj(params).get(by, obj(inputs.get(by)).get("default"))
            if isinstance(value, str) and value in OUTPUT_FORMATS:
                fmt = value
        if fmt:
            told[out_name] = fmt
    return told


def test_problems(card: dict[str, Any]) -> list[str]:
    """The form of `tests` (spec v3 §7.1): each `{output, check?, pass}`, its output one of
    the card's, its rules `[field, op, value]`. Whether each check service exists for the
    caller and reads that output is asked of AMSYOS (`amsyos card --check`, logged in)."""
    tests = card.get("tests")
    if tests is None:
        return []
    if not isinstance(tests, list):
        return ["tests: a list of {output, check, pass}"]
    outputs = obj(card.get("outputs"))
    found: list[str] = []
    for i, given in enumerate(seq(tests)):
        where = f"tests[{i}]"
        test = obj(given)
        if not test:
            found.append(f"{where}: {{output: <name>, check: <service>, pass: [[field, '==', 1]]}}")
            continue
        extra = sorted(set(test) - TEST_KEYS)
        if extra:
            found.append(
                f"{where}: unknown keys {', '.join(extra)} (a test has output, check, pass)"
            )
        out_name, check = test.get("output"), test.get("check")
        if not isinstance(out_name, str) or out_name not in outputs:
            found.append(
                f"{where}.output: one of the card's outputs ({', '.join(outputs) or 'none'})"
            )
        elif check is None and _format(outputs[out_name]) != "json":
            found.append(
                f"{where}.check: a service whose JSON report the test reads, like t_x.video.check; "
                f"left out, the output itself is read, and outputs.{out_name} is no json"
            )
        if check is not None and (not isinstance(check, str) or not SERVICE_RE.fullmatch(check)):
            found.append(f"{where}.check: a service's id, like t_x.video.check")
        rules = test.get("pass")
        if not isinstance(rules, list) or not rules:
            found.append(
                f"{where}.pass: at least one [field, op, value], like [rows_read, '==', 12]"
            )
            continue
        for j, given_rule in enumerate(seq(rules)):
            rule = seq(given_rule)
            if len(rule) != 3 or not isinstance(rule[0], str) or not rule[0] or rule[1] not in OPS:
                found.append(f"{where}.pass[{j}]: [field, op, value], op one of {' '.join(OPS)}")
    return found


def means_of_output(spec: Any) -> str:
    """What an output of the card is: its `means`, else what its format says."""
    return str(obj(spec).get("means") or MEANS_OF_FORMAT.get(str(_format(spec)), "document"))


def example_problems(card: dict[str, Any]) -> list[str]:
    """What the example lacks: deploy runs it as the check of the new version."""
    inputs = obj(card.get("inputs"))
    required = [k for k, v in inputs.items() if obj(v).get("required")]
    example = card.get("example")
    # A customer, 2026-11-23: a service of no input passed card --check and service.check,
    # then deploy refused `example: {}`. With no required input it is one whole call.
    if not isinstance(example, dict) or (not example and required):
        needs = f": give {', '.join(required)}" if required else ""
        return [f"example: none; one whole call's inputs{needs} (deploy runs it as the check)"]
    given = obj(example)
    found = [f"example.{k}: missing (a required input)" for k in required if k not in given]
    found += [f"example.{k}: not an input of the card" for k in given if inputs and k not in inputs]
    found += [
        f"example.{k}: still a placeholder ({v})"
        for k, v in given.items()
        if isinstance(v, str) and v.startswith(FILL)
    ]
    return found


def left_to_fill(card: dict[str, Any]) -> list[str]:
    """Every value init could not read from the code (`<fill: ...>`), with the example's
    and the form's problems: what a person or agent still has to write."""
    said = problems(card)
    for key, value in card.items():
        if key == "example":
            continue
        for path, text in _strings(value, key):
            if text.startswith(FILL):
                said.append(f"{path}: {text}")
    return said + [p for p in example_problems(card) if p not in said]


# ---------------------------------------------------------------- AMSYOS's answer, read
# T2, 2026-11-20: `card --check` printed "outputs: Dictionary should have at least 1 item
# after validation, not 0" under the same problem found here. A server's problem of a field
# already said here is printed once; Pydantic's words become the card's.
NOT_VALID = "The spec is not a valid service: "
_FIELD = re.compile(r"([a-z_][a-z0-9_]*(?:(?:\.[A-Za-z0-9_]+)|\[\d+\])*): (.+)", re.S)
_PYDANTIC: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), words)
    for pattern, words in (
        (r"^(?:Dictionary|List|Set|Tuple) should have at least (\d+) items? after validation.*$",
         "give at least \\1"),
        (r"^(?:Dictionary|List|Set|Tuple) should have at most (\d+) items? after validation.*$",
         "give at most \\1"),
        (r"^String should have at least (\d+) characters?$", "at least \\1 characters"),
        (r"^String should have at most (\d+) characters?$", "at most \\1 characters"),
        (r"^Field required$", "missing: give it"),
        (r"^Extra inputs are not permitted$", "not a field here: leave it out"),
        (r"^Input should be a valid dictionary.*$", "a mapping (name: value)"),
        (r"^Input should be a valid list.*$", "a list, like [a, b]"),
        (r"^Input should be a valid string.*$", "text"),
        (r"^Input should be a valid integer.*$", "a whole number"),
        (r"^Input should be a valid number.*$", "a number"),
        (r"^Input should be a valid boolean.*$", "true or false"),
        (r"^Input should be greater than or equal to (.+)$", "at least \\1"),
        (r"^Input should be less than or equal to (.+)$", "at most \\1"),
        (r"^Input should be greater than (.+)$", "above \\1"),
        (r"^Input should be less than (.+)$", "below \\1"),
        (r"^Input should be ('[^']*'(?:, '[^']*')*(?: or '[^']*')?)$", "one of \\1"),
        (r"^Value error, (.+)$", "\\1"),
        (r"^Assertion failed, (.+)$", "\\1"),
    )
)  # fmt: skip


def _field_of(problem: str) -> tuple[str | None, str]:
    """(field, what is wrong) of `field: what`; (None, problem) when it names no field."""
    found = _FIELD.fullmatch(problem.strip())
    return (found.group(1), found.group(2).strip()) if found else (None, problem.strip())


def card_words(message: str) -> str:
    """Pydantic's message in the card's words ("at least 1 item" → "give at least one")."""
    for pattern, words in _PYDANTIC:
        if pattern.match(message):
            said = pattern.sub(words, message, count=1)
            return re.sub(r"\bat least 1\b", "at least one", said)
    return message


def server_problems(problems: list[str], here: list[str]) -> list[str]:
    """`service.check`'s problems to print under those found here: one line a field (a
    "The spec is not a valid service: a: x; b: y" answer split), Pydantic's words in the
    card's, and none whose field a problem found here already names (a field or a part of
    it: `outputs` here covers `outputs.chart` there)."""
    said_here = {f for p in here if (f := _field_of(p)[0])}
    lines: list[str] = []
    for problem in problems:
        text = str(problem).strip()
        parts = text[len(NOT_VALID) :].split("; ") if text.startswith(NOT_VALID) else [text]
        for part in parts:
            field, what = _field_of(part.rstrip("."))
            if field is None:
                line = part
            elif any(field == f or field.startswith((f + ".", f + "[")) for f in said_here):
                continue
            else:
                line = f"{field}: {card_words(what)}"
            if line and line not in lines:
                lines.append(line)
    return lines


FILLER = frozenset({
    "the", "a", "an", "file", "input", "value", "param", "parameter", "of", "to", "for",
    "name", "this", "its", "given", "string", "text", "number", "integer", "path",
})  # fmt: skip


def description_warnings(card: dict[str, Any]) -> list[str]:
    """Each input whose description only repeats its name ("The x", "The csv file"): it
    tells a caller nothing (v3 T2). A warning: `card --check` still passes."""
    found: list[str] = []
    for name, given in obj(card.get("inputs")).items():
        said = obj(given).get("description")
        if not isinstance(said, str) or said.startswith(FILL):
            continue
        words = set(re.findall(r"[a-z0-9]+", said.lower()))
        if not words - set(str(name).lower().split("_")) - FILLER:
            found.append(
                f'inputs.{name}.description: "{said[:60]}" only repeats the name; say what a '
                "caller gives (its meaning, unit, format, or what it changes)"
            )
    return found


def _strings(value: Any, path: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        return [s for k, v in obj(value).items() for s in _strings(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [s for i, v in enumerate(value) for s in _strings(v, f"{path}[{i}]")]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    return []


def named_files(card: dict[str, Any]) -> set[str]:
    """Every word of the card that could name a file (`python main.py`, `@notes.md`,
    `include: [notes.md]`): deploy packs a top-level .md it names."""
    names: set[str] = set()
    for _, text in _strings(card, "card"):
        for given in re.split(r"[\s,;=:'\"()\[\]{}]+", text):
            word = given.lstrip("@").removeprefix("./")
            if word:
                names.add(word)
    return names


def _format(out: Any) -> str | None:
    if isinstance(out, str) and re.fullmatch(r"[a-z0-9]{1,10}", out):
        return out
    fmt = obj(out).get("format")
    if isinstance(fmt, str) and re.fullmatch(r"[a-z0-9]{1,10}", fmt):
        return fmt
    return None


# ---------------------------------------------------------------- to the manifest's words
def input_of(spec: Any) -> dict[str, Any]:
    given = obj(spec) if isinstance(spec, dict) else {"type": spec}
    told: dict[str, Any] = {}
    if "enum" in given:
        # As written: `[0, 90, 180, 270]` stays numbers (a customer's feedback: they went as text,
        # and the default 0 was then "not one of" them).
        told["type"] = "enum"
        told["values"] = values = list(seq(given["enum"]))
        if all(_number(v) for v in values) and any(isinstance(v, float) for v in values):
            told["values"] = [float(v) for v in values]  # [0.5, 1, 2]: all floats, not mixed
    else:
        told["type"] = TYPES.get(str(given.get("type", "str")), "str")
    for key in INPUT_KEYS:
        if key in given:
            told[key] = given[key]
    if "minimum" in given:
        told["min"] = given["minimum"]
    if "maximum" in given:
        told["max"] = given["maximum"]
    if "example" in told and not isinstance(told["example"], str):
        told["example"] = json.dumps(told["example"])
    if told["type"] == "asset" and isinstance(told.get("means"), str):
        told["means"] = [told["means"]]
    if told["type"] == "str" and told.get("default") == "" and not told.get("required"):
        # `default: ''` on an optional text input: left out, the code takes its own empty
        # text; AMSYOS takes no blank text value, so the manifest has no default.
        del told["default"]
    return told


def spec_of(card: dict[str, Any]) -> dict[str, Any]:
    """The card as the spec of a build's service: {summary, inputs, outputs, means, …}."""
    outputs = obj(card.get("outputs"))
    optional = optional_outputs(card)
    spec: dict[str, Any] = {
        "summary": card.get("summary"),
        "inputs": {k: input_of(v) for k, v in obj(card.get("inputs")).items()},
        # An output a call may leave unmade keeps the word (`{format, optional: true}`).
        "outputs": {
            k: {"format": _format(v), "optional": True} if k in optional else _format(v)
            for k, v in outputs.items()
        },
        "means": {
            k: obj(v).get("means") or MEANS_OF_FORMAT.get(str(_format(v)), "document")
            for k, v in outputs.items()
        },
    }
    # An output whose format an input chooses: said on that input, as AMSYOS keeps it
    # (kernel/manifest.py `InputSpec.format_of`); the output keeps its own format.
    for out_name, out in outputs.items():
        by = obj(out).get("controls")
        if isinstance(by, str) and isinstance(spec["inputs"].get(by), dict):
            chooser = spec["inputs"][by]
            chooser["format_of"] = [*seq(chooser.get("format_of")), out_name]
    if card.get("example") is not None:
        spec["example"] = card["example"]
    if card.get("effect"):
        spec["effect"] = card["effect"]
    estimate = card.get("estimate")
    if estimate:
        spec["estimate"] = estimate if isinstance(estimate, dict) else {"default": str(estimate)}
    resources = card.get("resources")
    if isinstance(resources, dict):
        spec["resources"] = {
            ("vram_gb" if k == "min_vram_gb" else k): v for k, v in obj(resources).items()
        }
    for key in SPEC_KEYS:
        if key in card:
            spec[key] = card[key]
    return spec


def service_of(card: dict[str, Any], version: str | None) -> dict[str, Any]:
    told: dict[str, Any] = {"name": card["service"], "spec": spec_of(card)}
    if version:
        told["version"] = version
    return told


IMAGE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def image_of(card: dict[str, Any]) -> str:
    """The image a build makes: the card's `image` (a customer, 2026-11-23: a short name several
    services may share), else the service's name in letters, digits and underscores."""
    named = card.get("image")
    if isinstance(named, str) and IMAGE_RE.fullmatch(named):
        return named
    return re.sub(r"[^a-z0-9_]", "_", str(card["service"]).lower())[:40]


def build_params(card: dict[str, Any]) -> dict[str, Any]:
    """`node.build`'s params from the card, beside name, version, files and service."""
    told: dict[str, Any] = {"runtime": card.get("runtime", "python")}
    for key, joiner in (("system", " "), ("packages", "\n"), ("models", " ")):
        if card.get(key):
            told[key] = (
                joiner.join(str(v) for v in card[key])
                if isinstance(card[key], list)
                else str(card[key])
            )
    for key in ("setup", "command", "serve"):
        if card.get(key):
            told[key] = str(card[key])
    return told


def local_files(example: Any) -> dict[str, str]:
    """Inputs of the example given as `@path`: files deploy uploads first."""
    return {k: v[1:] for k, v in obj(example).items() if isinstance(v, str) and v.startswith("@")}


def full_version(version: str) -> str:
    return version if version.count(".") == 2 else version + ".0"


def for_check(card: dict[str, Any]) -> dict[str, Any]:
    """The card as `service.check` reads it: local example files stand as a placeholder
    reference (the check of a definition runs the real example; deploy uploads it)."""
    service = service_of(card, None)
    example = service["spec"].get("example")
    if isinstance(example, dict):
        service["spec"]["example"] = {
            k: (EXAMPLE_FILE if k in local_files(example) else v) for k, v in obj(example).items()
        }
    version = str(card.get("version", ""))
    if VERSION_RE.fullmatch(version):
        service["version"] = full_version(version)
    return service


# ---------------------------------------------------------------- proposing one
_NETWORK = re.compile(r"^\s*(?:import|from)\s+(requests|httpx|urllib|aiohttp|boto3)\b", re.M)
_TORCH = re.compile(r"^\s*(?:import|from)\s+(torch|tensorflow|jax|cupy)\b", re.M)
SOURCE_EXTS = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".java", ".R", ".sh"})


def detect_runtime(root: Path) -> tuple[str, str | None, str | None, str | None]:
    """(runtime, command, serve, setup) from the files at the folder's top."""
    names = {p.name for p in root.iterdir()}
    if "package.json" in names:
        try:
            main: Any = json.loads((root / "package.json").read_text(encoding="utf-8")).get("main")
        except (OSError, ValueError, AttributeError):
            main = None
        entry = (
            main
            if isinstance(main, str)
            else next(
                (n for n in ("main.js", "index.js", "main.mjs", "app.js") if n in names), "main.js"
            )
        )
        serve = "node server.js" if "server.js" in names else None
        return "node", f"node {entry}", serve, None
    if "go.mod" in names:
        return "go", "./app", None, "go build -o app ."
    if "pom.xml" in names:
        return "java", "java -jar target/app.jar", None, "mvn -q package"
    if any(n.endswith(".java") for n in names):
        return "java", "java -cp build Main", None, "javac -d build *.java"
    if any(n.endswith(".R") for n in names):
        entry = "main.R" if "main.R" in names else sorted(n for n in names if n.endswith(".R"))[0]
        return "r", f"Rscript {entry}", None, None
    pys = sorted(n for n in names if n.endswith(".py") and (root / n).is_file())
    # A customer, 2026-11-23: server.py called serve(handle) and imported maskedit.py, and
    # init wrote `command: python maskedit.py`. A file that calls the job SDK's serve is the
    # server; a module another file of the folder imports is never the entry point.
    texts = {n: _text(root / n) for n in pys}
    serving = sorted(n for n, t in texts.items() if _SDK.search(t) and _SERVE_CALL.search(t))
    serving.sort(key=lambda n: n != "server.py")
    server = serving[0] if serving else "server.py" if "server.py" in names else None
    serve = f"python {server}" if server else None
    ruled_out = {"server.py", "setup.py", "conftest.py", *serving, *_imported(texts)}
    for entry in ("main.py", "app.py", "run.py", "service.py", "__main__.py"):
        if entry in names and entry not in ruled_out:
            return "python", f"python {entry}", serve, None
    others = [n for n in pys if n not in ruled_out and not n.startswith("test")]
    if others and not serving:
        return "python", f"python {others[0]}", serve, None
    return "python", None if serve else f"{FILL}: the command that starts one job>", serve, None


_SDK = re.compile(r"^\s*(?:from\s+amsyos\s+import\b|import\s+amsyos\b)", re.M)
_SERVE_CALL = re.compile(r"(?<!def )\bserve\s*\(")


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _imported(texts: dict[str, str]) -> set[str]:
    """The files of the folder's top another of its files imports (`import maskedit`, `from
    maskedit import edit`, `from . import maskedit`): modules, never the entry point. A
    test or setup.py importing one says nothing."""
    found: set[str] = set()
    for name, text in texts.items():
        if name.startswith("test") or name in {"setup.py", "conftest.py"}:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        mine: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mine |= {f"{a.name.split('.')[0]}.py" for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mine.add(f"{node.module.split('.')[0]}.py")
            elif isinstance(node, ast.ImportFrom) and node.level:
                mine |= {f"{a.name}.py" for a in node.names}
        found |= mine - {name}
    return found & texts.keys()


# Folders of a repository whose code is not the service's (Claude Code for a customer,
# 2026-11-20: init read an output from a script in dev/): packed, never read for the card.
NOT_THE_SERVICE = frozenset({
    "dev", "tests", "test", "docs", "examples", "example", "notebooks", "bench", "benchmarks",
})  # fmt: skip


def _sources(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).parts
        if any(
            part in SKIP_DIRS or part in NOT_THE_SERVICE or part.startswith(".")
            for part in rel[:-1]
        ):
            continue
        if path.is_file() and path.suffix in SOURCE_EXTS and not rel[-1].startswith("test"):
            found.append(path)
        if len(found) >= 200:
            break
    return found


def _means_of_name(name: str) -> str:
    words = name.lower()
    for keys, means in MEANS_OF_WORD:
        if any(k in words for k in keys):
            return means
    return "document"


def _sample_for(root: Path, means: str) -> str | None:
    """A file in the folder an example could use for an input of this type."""
    exts = {
        "sheet": (".csv", ".xlsx"), "image": (".png", ".jpg", ".jpeg", ".webp"),
        "video": (".mp4", ".mov", ".webm"), "audio": (".wav", ".mp3"),
        "mesh": (".glb", ".obj"), "document": (".pdf", ".docx", ".txt"), "text": (".txt", ".md"),
    }.get(means, ())  # fmt: skip
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if rel.name.lower() in {"readme.md", "requirements.txt"}:
            continue
        if path.is_file() and path.suffix.lower() in exts and path.stat().st_size < 20_000_000:
            return rel.as_posix()  # samples/x.jpg on Windows too: the card is read anywhere
    return None


# ---------------------------------------------------------------- its name and summary
# T2, 2026-11-20: a folder `amsyos-trial-demo` whose README is titled "csv-rowcount" was
# named `amsyos.trial_demo`, and its docstring's "csv-rowcount:" became the summary
# "Csv-rowcount:". The name comes from the README's title, then the entry file's docstring,
# then the folder (naming.propose makes it area.action, the owner's standard of 2026-11-26);
# the summary is the first sentence of the README, then of the docstring.
PLATFORM = "amsyos"  # a name that starts with it (amsyos-trial-demo) drops it
NOT_TITLES = frozenset({
    "readme", "usage", "note", "notes", "example", "examples", "summary", "description",
    "todo", "warning", "args", "arguments", "returns", "input", "inputs", "output",
    "outputs", "about", "overview", "introduction", "main",
})  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*")
_PUNCTUATION = str.maketrans({
    "—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ",
})  # fmt: skip
MOST_TITLE_WORDS = 4  # a title of more words is a phrase, not a name


def fold(text: str) -> str:
    """Accents folded (NFKD, combining marks dropped, đ → d): `Xóa nền` → `Xoa nen`."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def name_words(text: str, *, most: int | None = None) -> list[str] | None:
    """The words a card's service is named from, of a title or a folder's name:
    `csv-rowcount` → [csv, rowcount], `amsyos-trial-demo` → [trial, demo] (naming.propose
    makes the name). None when nothing is left, or more than `most` words."""
    words = [w for w in re.split(r"[^a-z0-9]+", fold(text).lower()) if w]
    if len(words) > 1 and words[0] == PLATFORM:
        words = words[1:]
    if not words or (most is not None and len(words) > most):
        return None
    return words


def _a_title(text: str | None) -> str | None:
    """A heading or a docstring's head that names the program, not a section."""
    if not text:
        return None
    text = text.strip().rstrip(":").strip()
    if not text or fold(text).lower() in NOT_TITLES or len(text) > 60:
        return None
    return text


_SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z(\"']|$)")
# A word that names a file: `edit.py`, `src/main.py`, `config.yaml`.
_FILE_WORD = re.compile(r"(?<![\w/.-])[\w./-]*\w\.([A-Za-z][A-Za-z0-9]{0,9})(?![\w.-]*\w)")
# Code and its setup by the extension alone; a file a job writes (`report.json`) is no file of
# the repository unless it is there (`names`).
CODE_EXTS = frozenset(e.lstrip(".").lower() for e in SOURCE_EXTS) | {
    "pyw", "ipynb", "toml", "cfg", "ini", "bat", "ps1",
}  # fmt: skip


def names_a_file(sentence: str, names: frozenset[str] = frozenset()) -> bool:
    """The sentence names a file of the repository (`names`), or a code or setup file by
    its extension: a card's summary is read by callers, who see no repository (a customer,
    2026-11-20: "... the edits are in edit.py" became the summary)."""
    for match in _FILE_WORD.finditer(sentence):
        word = match.group(0)
        if word.rsplit("/", 1)[-1] in names:
            return True
        if match.group(1).lower() in CODE_EXTS and not re.fullmatch(r"[A-Z]\w*\.js", word):
            return True  # a file by its extension; Node.js, Vue.js name no file
    return False


def first_sentence(text: str, names: frozenset[str] | None = None) -> str | None:
    """The first sentence of a paragraph that can be a card's summary: Markdown dropped, no
    trailing `.` or `:`, at most 120 characters cut between words, English letters only,
    and no file of the repository named (`names`, `names_a_file`): such a sentence is
    dropped for the next one. With `names` None, the first sentence only, as before."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text.translate(_PUNCTUATION))
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*`]+|(?<!\w)_+|_+(?!\w)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    while text:
        end = _SENTENCE_END.search(text)
        raw = text[: end.start() + 1] if end else text
        text = text[len(raw) :].strip() if end else ""
        line = raw.strip().rstrip(".:;,").strip()
        if len(line) > 120:
            line = line[:117].rsplit(" ", 1)[0].rstrip(".,;:") + "..."
        line = line[:1].upper() + line[1:]
        fit = len(line) >= 3 and line.isascii()
        if names is None:
            return line if fit else None
        if fit and not names_a_file(raw, names):
            return line
    return None


def repo_names(root: Path, most: int = 2000) -> frozenset[str]:
    """The names of the repository's files (outside dot-folders and virtualenvs)."""
    names: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel[:-1]):
            continue
        names.add(path.name)
        if len(names) >= most:
            break
    return frozenset(names)


def _readme(root: Path) -> tuple[str | None, str | None]:
    """(title, first paragraph) of the README at the folder's top."""
    path = next(
        (p for p in sorted(root.iterdir()) if p.is_file() and p.name.lower() in READMES), None
    )
    if path is None:
        return None, None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            lines = fh.read(200_000).splitlines()
    except OSError:
        return None, None
    title: str | None = None
    paragraph: list[str] = []
    fenced = False
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        below = lines[i + 1].strip() if i + 1 < len(lines) else ""
        i += 1
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            if paragraph:
                break
            continue
        if fenced:
            continue
        if not line:
            if paragraph:
                break
            continue
        heading = line.lstrip("#").strip() if line.startswith("#") else None
        if heading is None and below and re.fullmatch(r"=+|-+", below):
            heading = line  # a heading underlined (Markdown's setext, reStructuredText)
            i += 1
        if heading is not None:
            if paragraph:
                break
            title = title or _a_title(heading)
            continue
        if line.startswith(("<", "![", "[![", ">", "|", "---", "===", "..")):
            continue  # HTML, badges, quotes, tables, rules, reStructuredText directives
        paragraph.append(line)
    return title, " ".join(paragraph) or None


READMES = frozenset({"readme.md", "readme.rst", "readme.txt", "readme", "readme.markdown"})


def _docstring(root: Path, command: str | None) -> tuple[str | None, str | None]:
    """(title, first paragraph) of the entry file's docstring: `csv-rowcount: counts ...` or
    a first line `csv-rowcount` alone names the program; the rest is its summary."""
    entry = command.split()[-1] if command else None
    if not entry or not entry.endswith(".py") or not (root / entry).is_file():
        return None, None
    try:
        doc = ast.get_docstring(ast.parse((root / entry).read_text(encoding="utf-8")))
    except (SyntaxError, ValueError, OSError):
        return None, None
    lines = (doc or "").strip().splitlines()
    if not lines:
        return None, None
    head, sep, rest = lines[0].partition(":")
    title: str | None = None
    # A name has a joint (csv-rowcount, csv.chart): "Usage:" or "Run:" heads no program.
    if _TOKEN.fullmatch(head.strip()) and re.search(r"[-_.]", head) and (sep or len(lines) > 1):
        title = _a_title(head)
    body = ([rest] if title and sep else [] if title else [lines[0]]) + lines[1:]
    paragraphs: list[list[str]] = [[]]
    for line in (b.strip() for b in body):
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    # The paragraphs that are no section of their own ("Usage: python main.py ..."), one
    # to a line pair: the summary is the first sentence of them that names no file.
    told = [" ".join(p) for p in paragraphs if p and not _section(p[0])]
    return title, "\n\n".join(told) or None


def _section(line: str) -> bool:
    head, sep, _ = line.partition(":")
    return bool(sep) and fold(head).strip().lower() in NOT_TITLES


def described(root: Path, command: str | None) -> tuple[list[str] | None, str | None]:
    """(the words of its name, summary) the README and the entry file's docstring give,
    each None when neither says it."""
    readme_title, readme_text = _readme(root)
    doc_title, doc_text = _docstring(root, command)
    name = next(
        (n for t in (readme_title, doc_title) if t and (n := name_words(t, most=MOST_TITLE_WORDS))),
        None,
    )
    names = repo_names(root)
    paragraphs = [p for t in (readme_text, doc_text) if t for p in t.split("\n\n")]
    summary = next((s for p in paragraphs if (s := first_sentence(p, names))), None)
    return name, summary


MOST_MODULES = 20  # the entry file and the local modules it imports, read for the card


def _entry(root: Path, command: str | None) -> Path | None:
    """The Python file the card's command starts (`python main.py`, `python src/run.py`)."""
    word = command.split()[-1] if command else ""
    path = root / word
    return path if word.endswith(".py") and path.is_file() else None


def _module_files(base: Path, parts: list[str], names: list[str]) -> list[Path]:
    """`a.b` under `base`: a/b.py or a/b/__init__.py; with `from a.b import c`, a/b/c.py too."""
    found: list[Path] = []
    where = base.joinpath(*parts) if parts else base
    if parts:
        found += [where.with_suffix(".py"), where / "__init__.py"]
    found += [where / f"{name}.py" for name in names if name != "*"]
    found += [where / name / "__init__.py" for name in names if name != "*"]
    return found


def local_modules(root: Path, entry: Path, most: int = MOST_MODULES) -> list[Path]:
    """The entry file, then the repository's own modules it imports, and theirs, at most
    `most` files: the code a job runs (a customer, 2026-11-20: its params were read in an
    imported module, and a helper in dev/ added an output). Never a module in a virtualenv
    or a dot-folder; an import of a package from pip is not a file here, so not read."""
    top = root.resolve()
    order: list[Path] = [entry.resolve()]
    queue = list(order)
    while queue and len(order) < most:
        path = queue.pop(0)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        bases = list(dict.fromkeys([entry.resolve().parent, top, path.parent]))
        wanted: list[Path] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for base in bases:
                        wanted += _module_files(base, alias.name.split("."), [])
            elif isinstance(node, ast.ImportFrom):
                parts = node.module.split(".") if node.module else []
                names = [alias.name for alias in node.names]
                if node.level:
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    wanted += _module_files(base, parts, names)
                else:
                    for base in bases:
                        wanted += _module_files(base, parts, names)
        for candidate in wanted:
            if len(order) >= most:
                break
            try:
                real = candidate.resolve()
                rel = real.relative_to(top).parts
            except (OSError, ValueError):
                continue  # outside the repository
            if real in order or not real.is_file():
                continue
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel[:-1]):
                continue
            order.append(real)
            queue.append(real)
    return order


def _without_main(text: str) -> str:
    """The module's text with its top-level `if __name__ == "__main__":` blocks blanked
    (their lines kept, empty): what an import of it runs."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return text
    lines = text.splitlines()
    for node in tree.body:
        test = node.test if isinstance(node, ast.If) and not node.orelse else None
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            for at in range(node.lineno - 1, node.end_lineno or node.lineno):
                lines[at] = ""
    return "\n".join(lines) + "\n"


def read_code(root: Path, command: str | None = None) -> tuple[infer.Found, str]:
    """What the code takes and makes, and its text (for the effect and the GPU). With a
    Python entry file, the code is that file and the local modules it imports (what one
    hands another's functions followed, infer.py `passes`); else every source file of the
    service's folders (`_sources`: never dev/, tests/ and the like)."""
    found = infer.Found()
    entry = _entry(root, command)
    paths = local_modules(root, entry) if entry is not None else _sources(root)
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in paths]
    if entry is not None:  # an imported module's `if __name__ == "__main__":` never runs
        texts[1:] = [
            _without_main(t) if p.suffix == ".py" else t
            for p, t in zip(paths[1:], texts[1:], strict=True)
        ]
    handed: infer.Passes = {}
    for path, text in zip(paths, texts, strict=True):
        if path.suffix == ".py":
            for name, slots in infer.passes(text, handed).items():
                handed.setdefault(name, {}).update(slots)
    # The functions that start a program with what they are given (`_tool("ffmpeg")`),
    # of every module read: twice, so that one module's call of another's is seen whatever
    # their order (a customer, 2026-11-21).
    helpers: infer.Helpers = {}
    for _ in range(2):
        for path, text in zip(paths, texts, strict=True):
            if path.suffix == ".py":
                for name, slots in infer.helpers(text, helpers).items():
                    helpers.setdefault(name, set()).update(slots)
    top = root.resolve()
    known = SYSTEM_OF_PROGRAM.keys() | NOT_PACKAGED.keys()
    for i, (path, text) in enumerate(zip(paths, texts, strict=True)):
        found.base = i << 20  # inputs in the order read: the entry's, then each module's
        try:
            found.module = path.resolve().relative_to(top).as_posix()
        except ValueError:
            found.module = path.name
        if path.suffix == ".py":
            infer.python(text, found, handed, known=known, helpers=helpers)
        else:
            infer.programs_by_patterns(text, found, known, shell=path.suffix == ".sh")
        infer.by_patterns(text, found)
    return found, "\n".join(texts)


# The conda-forge package that has a program the code runs, proposed as the card's
# `system` (each checked on the VPS as kernel/spaces.py `_SYSTEM` reads it). a customer,
# 2026-11-20: a service that ran ffmpeg and ffprobe was proposed with none; its image built,
# then a job failed at run time.
SYSTEM_OF_PROGRAM = {
    "ffmpeg": "ffmpeg", "ffprobe": "ffmpeg", "ffplay": "ffmpeg",
    "convert": "imagemagick", "magick": "imagemagick", "identify": "imagemagick",
    "mogrify": "imagemagick", "composite": "imagemagick", "montage": "imagemagick",
    "exiftool": "exiftool", "sox": "sox", "soxi": "sox", "tesseract": "tesseract",
    "pandoc": "pandoc", "gs": "ghostscript", "pdftoppm": "poppler", "pdftotext": "poppler",
    "pdfinfo": "poppler", "pdftocairo": "poppler", "gdalinfo": "gdal",
    "gdal_translate": "gdal", "ogr2ogr": "gdal", "rsvg-convert": "librsvg",
    "optipng": "optipng", "lame": "lame", "flac": "flac", "jq": "jq", "git": "git",
}  # fmt: skip
# Python modules that run one of those programs themselves (pydub runs ffmpeg).
SYSTEM_OF_MODULE = {
    "pydub": "ffmpeg", "ffmpeg": "ffmpeg", "wand": "imagemagick", "pytesseract": "tesseract",
    "pdf2image": "poppler",
}  # fmt: skip
# Programs conda-forge does not package: how the image gets one instead.
NOT_PACKAGED = {
    "blender": (
        "conda-forge does not package Blender: fetch it pinned when the image is made "
        "(setup: amsyos-fetch <url> --sha256 <hex> --unpack --strip 1 --to blender; "
        "`amsyos describe node.build`, setup's example)"
    ),
}
# On every space already (Ubuntu 22.04, Python 3.11, the runtimes, amsyos-job).
ON_THE_MACHINE = frozenset({
    "python", "python3", "pip", "pip3", "sh", "bash", "env", "ls", "cp", "mv", "rm", "mkdir",
    "cat", "echo", "tar", "gzip", "gunzip", "find", "grep", "sed", "awk", "which", "chmod",
    "touch", "head", "tail", "sort", "xargs", "true", "false", "sleep", "kill", "nproc",
    "nvidia-smi", "amsyos-job", "node", "npm", "npx", "java", "javac", "go", "rscript", "uv",
})  # fmt: skip
_MODULE_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def program_name(raw: str) -> str:
    """`./blender/blender` → blender, `C:\\ffmpeg\\bin\\ffmpeg.exe` → ffmpeg."""
    return re.split(r"[\\/]", raw)[-1].lower().removesuffix(".exe")


def system_for(found: infer.Found, text: str) -> tuple[list[str], list[str]]:
    """The conda-forge packages (`system`) of the programs the code runs, and notes: which
    programs need them, a program conda-forge does not package, one AMSYOS does not know."""
    packages: dict[str, None] = {}
    programs: list[str] = []
    where: list[str] = []
    notes: list[str] = []
    unknown: list[str] = []
    for raw, files in found.commands.items():
        name = program_name(raw)
        local = ("/" in raw or "\\" in raw) and not raw.startswith(("/", "\\"))
        if name in ON_THE_MACHINE:
            continue
        if name in SYSTEM_OF_PROGRAM:
            packages.setdefault(SYSTEM_OF_PROGRAM[name], None)
            programs += [name] if name not in programs else []
            where += [f for f in files if f not in where]
        elif name in NOT_PACKAGED:
            notes.append(f"the code runs {raw} ({', '.join(files)}): {NOT_PACKAGED[name]}")
        elif not local:  # `./app`: the repository's own program
            unknown.append(f"{name} ({', '.join(files)})" if files else name)
    for module in dict.fromkeys(_MODULE_IMPORT.findall(text)):
        package = SYSTEM_OF_MODULE.get(module)
        if package and package not in packages:
            packages[package] = None
            programs.append(f"{package} (by the Python module {module})")
    if packages:
        said = ", ".join(sorted(programs)) + (f" ({', '.join(where)})" if where else "")
        them = "it" if len(packages) == 1 else "them"
        notes.insert(
            0,
            f"system: {', '.join(packages)} from conda-forge, for what the code runs: {said}; "
            f"without {them} the image builds and a job fails when it runs one",
        )
    if unknown:
        lacks = "it" if len(unknown) == 1 else "one"
        notes.append(
            f"the code runs {', '.join(unknown)}: if the machine lacks {lacks}, add the "
            "conda-forge package that has it to system"
        )
    return list(packages), notes


def propose(root: Path, name: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """A card from what the code says, and notes on how it was read. What the code does
    not say is written `<fill: ...>` (`left_to_fill` lists it)."""
    runtime, command, serve, setup = detect_runtime(root)
    found, text = read_code(root, command or serve)
    named, summary = described(root, command or serve)
    notes: list[str] = []
    inputs: dict[str, Any] = {}
    files, params = found.ordered()
    # What the code does not say is a placeholder card --check and deploy refuse, never a
    # description that only repeats the name (v3 T2: "The x" passed).
    optional: list[str] = []
    for in_name in files:
        facts = found.files[in_name]
        where = facts.get("check")
        # The code runs without it (it tests the value for None, reads it with .get, or a
        # function takes it with a default) and no test of it ends the job.
        maybe = bool(facts.get("optional")) and not facts.get("needed") and not where
        if maybe:
            optional.append(in_name)
        inputs[in_name] = {
            "type": "file",
            "means": facts.get("means") or _means_of_name(f"{in_name} {facts.get('words') or ''}"),
            "required": not maybe,
            "description": (
                f"{FILL}: check this: the code reads it from {where}, but a job gets no "
                f"command line: read it with job.input('{in_name}') (or job.json's inputs), "
                "then say what the file holds>"
                if where
                else f"{FILL}: what the {in_name} file holds, for a caller; optional: the "
                "code runs without it>"
                if maybe
                else f"{FILL}: what the {in_name} file holds, for a caller>"
            ),
        }
    as_json: list[str] = []
    for param in params:
        facts = found.params[param]
        spec: dict[str, Any] = {}
        values = facts.get("values")
        default = facts.get("default")
        # The code's own default is a value it takes, though its test may not list it
        # (`if p["crop"] != "none" and p["crop"] not in CROPS`): of the same kind, it leads.
        if (
            values
            and default not in (None, "")
            and not in_enum(default, values)
            and _number(default) == all(_number(v) for v in values)
            and isinstance(default, str) == all(isinstance(v, str) for v in values)
        ):
            values = [default, *values]
        if values:
            spec["enum"] = values
        else:
            spec["type"] = facts.get("type") or "string"
        if facts.get("required") and default in (None, ""):
            spec["required"] = True  # `.get("x", "")` then `if not x: fail`: it is needed
        elif default is not None and (not values or in_enum(default, values)):
            # `default: ''`: optional text, empty when left out (a customer's feedback).
            spec["default"] = default
        elif facts.get("required"):
            spec["required"] = True
        said = facts.get("description")
        if facts.get("check"):
            said = (
                f"{FILL}: check this: the code reads it from {facts['check']}, but a job gets "
                f"no command line: read it with job.params['{param}'] (or job.json's "
                f"params), then say what {param} means for a caller>"
            )
        elif not isinstance(said, str) or description_warnings(
            {"inputs": {param: {"description": said}}}
        ):
            said = f"{FILL}: what {param} means for a caller>"
        spec["description"] = said
        if facts.get("json") and "enum" not in spec:
            # Read as JSON (the owner, 2026-11-26): one JSON input with its schema.
            spec["type"] = "string"
            spec["multiline"] = True
            spec["schema"] = f"{param}.json"
            as_json.append(param)
        inputs[param] = spec
    outputs: dict[str, Any] = {}
    for out_name, fmt in found.outputs.items():
        means = MEANS_OF_FORMAT.get(fmt, "document")
        if means == "image" and any(w in out_name for w in ("chart", "plot", "graph")):
            means = "image.chart"
        outputs[out_name] = {"type": "file", "format": fmt, "means": means}
    for out_name, param in found.chosen.items():
        given = inputs.get(param)
        if given is None:
            continue  # named by something that is no input: nothing a call chooses
        values = seq(given.get("enum"))
        formats = [v for v in values if isinstance(v, str) and v in OUTPUT_FORMATS]
        if values and len(formats) == len(values):
            fmt = given["default"] if given.get("default") in formats else formats[0]
            means = MEANS_OF_FORMAT.get(fmt, "document")
            outputs[out_name] = {"type": "file", "format": fmt, "controls": param, "means": means}
            continue
        outputs[out_name] = {
            "type": "file", "format": f"{FILL}: its format when {param} is left out>",
            "controls": param, "means": f"{FILL}: what the file is: audio, image, data...>",
        }  # fmt: skip
        notes.append(
            f"outputs.{out_name}: the code writes it in the format {param} names: make {param} "
            f"an enum of formats ({param}: {{enum: [wav, mp3], default: wav}})"
        )
    for param in as_json:
        notes.append(schema_for(root, param, found.params[param].get("model"), command or serve))
    example = example_for(root, inputs)
    read = sorted(found.how) or ["file names only"]
    notes.append(
        f"read from the code ({', '.join(read)}): {len(inputs)} inputs, {len(outputs)} outputs"
    )
    if optional:
        notes.append(
            f"optional (the code runs without it, so required: false): {', '.join(optional)}"
        )
    system, said = system_for(found, text)
    notes += said
    if "argparse" in found.how or "command line" in found.how:
        notes.append(
            "the code reads its params from its command line (argparse, sys.argv), but a job "
            "gets them in $AMSYOS_JOB/job.json (or `from amsyos import job`: job.params, "
            "job.input(name)), not on its command line: read them there (each such input "
            "says check this)"
        )
    if found.computed:
        notes.append(
            "job.input is called with a name the code builds ("
            + ", ".join(found.computed[:5])
            + "): no card can be read from it; declare each of those inputs yourself"
        )
    if summary and summary.endswith("..."):
        # A customer, 2026-11-23: a sentence cut at 120 characters passed card --check.
        notes.append(
            f"the first sentence is longer than 120 characters: write the summary ({summary})"
        )
        summary = None
    if not inputs:
        notes.append(
            "no input found in the code (no job.json, amsyos module, amsyos-job, argparse "
            "or sys.argv read): add each input a call gives, or leave inputs empty"
        )
    # By the naming standard (naming.py): area.action, never the organisation or a version.
    service = name or naming.propose(
        named or name_words(root.resolve().name) or [],
        [program_name(c) for c in found.commands] + _MODULE_IMPORT.findall(text),
        {n: str(obj(o).get("means") or "") for n, o in outputs.items()},
    )
    card: dict[str, Any] = {
        "service": service,
        "version": "1.0",
        "summary": summary or f"{FILL}: what one call takes and makes, at most 120 characters>",
        "where": "space",
        "runtime": runtime,
    }
    if system:
        card["system"] = system
    if setup:
        card["setup"] = setup
    if command:
        card["command"] = command
    if serve:
        card["serve"] = serve
    if runtime == "python" and not (root / "requirements.txt").is_file():
        deps = _pyproject_deps(root)
        if not deps:  # v3 T2: `import matplotlib` was missed
            deps = imported_packages(root)
            if deps:
                notes.append(
                    f"packages read from the code's imports: {', '.join(deps)} (pin a version, "
                    "like matplotlib==3.9.2, for a build that does not change)"
                )
        if deps:
            card["packages"] = deps
    gpu = "optional" if _TORCH.search(text) else "none"
    card["resources"] = {"gpu": gpu, "cpu": 1, "ram_gb": 2}
    card["effect"] = "read" if _NETWORK.search(text) else "pure"
    card["estimate"] = "30s"
    card["inputs"] = inputs
    card["outputs"] = outputs
    card["example"] = example
    return card, notes


def importable(path: Path) -> bool:
    """A module whose top level only defines (imports, functions, classes, constants without
    a call, an `if __name__ == "__main__":`): importing it runs nothing of a job."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return False
    defines = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in tree.body:
        if isinstance(node, defines) or (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        ):
            continue
        if isinstance(node, ast.Assign | ast.AnnAssign) and not any(
            isinstance(n, ast.Call) for n in ast.walk(node)
        ):
            continue
        if isinstance(node, ast.Try) and all(
            isinstance(s, ast.Import | ast.ImportFrom) for s in node.body
        ):
            continue
        test = node.test if isinstance(node, ast.If) else None
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            continue
        return False
    return True


def model_files(cls: str, paths: list[Path]) -> list[Path]:
    """Of these files, those that define the class `cls` at their top."""
    found: list[Path] = []
    for path in paths:
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        if any(isinstance(n, ast.ClassDef) and n.name == cls for n in tree.body):
            found.append(path)
    return found


def schema_for(root: Path, param: str, model: str | None, command: str | None) -> str:
    """For an input the code reads as JSON: its schema written from the model the code loads
    it into, when that model's module imports cleanly (in an empty folder, by the project's
    Python); else the note of how to write it."""
    file = f"{SCHEMA_DIR}/{param}.json"
    target = root / file
    if target.is_file():
        return f"inputs.{param}: the code reads it as JSON; its schema is {file} (there already)"
    cls = model.rsplit(".", 1)[-1] if model else None
    entry = _entry(root, command)
    paths = local_modules(root, entry) if entry is not None else _sources(root)
    files = model_files(cls, paths) if cls else []
    if cls is None or len(files) != 1:
        return (
            f"inputs.{param}: the code reads it as JSON: write its shape into {file} with "
            f"amsyos schema {param} --from-model <file.py>:<Model> (the model your code "
            f"validates it with), or --from-example once example.{param} holds a whole value; "
            "card --check says it is missing until then"
        )
    rel = files[0].resolve().relative_to(root.resolve()).as_posix()
    how = f"amsyos schema {param} --from-model {rel}:{cls}"
    if not importable(files[0]):
        return (
            f"inputs.{param}: the code reads it as JSON into {cls}: write {file} with {how} "
            f"(not imported here: {rel} does more than define when imported)"
        )
    from amsyos_cli import schema as schemas  # noqa: PLC0415 - it imports this module

    try:
        with tempfile.TemporaryDirectory(prefix="amsyos-init-") as empty:
            made, by = schemas.from_model(root, f"{rel}:{cls}", cwd=Path(empty), timeout=20)
    except CliError as exc:
        said = exc.hint[:240]
        return f"inputs.{param}: read as JSON into {cls}: write {file} with {how} ({said})"
    if len(json.dumps(made, ensure_ascii=False, sort_keys=True)) > schemas.SCHEMA_MOST:
        most = f"{schemas.SCHEMA_MOST:,}"
        return f"inputs.{param}: the schema of {cls} is over {most} characters: write {file}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(made, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return (
        f"inputs.{param}: the code reads it as JSON into {cls}: wrote {file} from {rel}:{cls} "
        f"({by}); AMSYOS refuses a call whose {param} does not fit before it runs. Changed "
        f"the model? {how} --force"
    )


def example_for(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    """An example call of the card's inputs: a sample file of the folder for a file, each
    default, an enum's first value, `<fill: ...>` for a required value nothing gives."""
    example: dict[str, Any] = {}
    for in_name, spec in inputs.items():
        if spec.get("type") == "file":
            means = str(spec.get("means") or _means_of_name(in_name))
            sample = _sample_for(root, means)
            ext = SAMPLE_EXT.get(means, "")
            if sample or spec.get("required"):  # an optional file with no sample: left out
                example[in_name] = f"@{sample}" if sample else f"{FILL}: @path/to/a/sample{ext}>"
        elif spec.get("default") == "":
            continue  # empty text is no value a call may give: left out, as the default
        elif "default" in spec:
            example[in_name] = spec["default"]
        elif "enum" in spec:
            example[in_name] = spec["enum"][0]
        elif spec.get("required"):
            example[in_name] = f"{FILL}: a value for {in_name}>"
    return example


# ---------------------------------------------------------------- open standards
EXPORTS = ("mcp", "a2a")


def service_view(card: dict[str, Any]) -> dict[str, Any]:
    """The card as amsyos_cli/standards.py reads a service: its spec in the manifest's
    words, with its name and version."""
    version = str(card.get("version", ""))
    told = {"service": card.get("service"), **spec_of(card)}
    if VERSION_RE.fullmatch(version):
        told["version"] = full_version(version)
    return told


def export(card: dict[str, Any], kind: str, url: str) -> dict[str, Any]:
    """The card as an MCP tool definition (`mcp`) or an A2A Agent Card of one skill (`a2a`,
    its one interface the Gateway's MCP URL), made here: nothing is sent."""
    service = service_view(card)
    if kind == "mcp":
        return standards.mcp_tool(service)
    return standards.a2a_card(
        str(card["service"]), url, [standards.a2a_skill(service)],
        description=str(card.get("summary") or "") or None,
        version=str(service.get("version") or "1.0.0"),
    )  # fmt: skip


def tool_of(given: Any, source: str) -> dict[str, Any]:
    """The one MCP tool definition in a file: a tool, or a tools/list answer of one tool;
    every problem with it at once."""
    tool: Any = given
    tools = seq(obj(given).get("tools")) if isinstance(given, dict) else []
    if tools:
        if len(tools) != 1:
            names = ", ".join(str(obj(t).get("name")) for t in tools[:20])
            raise CliError(
                "INVALID_INPUT",
                f"{source} lists {len(tools)} tools ({names}): save the one this repository "
                "is as a file of its own and give that",
            )
        tool = tools[0]
    found: list[str] = []
    if not isinstance(tool, dict):
        found.append("not an MCP tool definition (an object with name and inputSchema)")
    else:
        if not isinstance(obj(tool).get("name"), str):
            found.append("name: the tool's name, a string")
        if not isinstance(obj(tool).get("inputSchema"), dict):
            found.append("inputSchema: a JSON Schema object of the tool's arguments")
        for key in ("annotations", "outputSchema", "_meta"):
            if key in obj(tool) and not isinstance(obj(tool)[key], dict):
                found.append(f"{key}: an object")
    if found:
        raise CliError("INVALID_INPUT", f"{source}: " + "; ".join(found), {"problems": found})
    return obj(tool)


def from_mcp(
    card: dict[str, Any], tool: dict[str, Any], root: Path, *, named: bool = False
) -> list[str]:
    """A card init proposed from the code, started instead from an MCP tool definition: its
    name (unless `named`), description, inputs and effect; the code's runtime, command,
    packages and outputs stay, and its inputs the tool does not list. Notes on what was
    taken and on what a card cannot carry."""
    name = None if named else standards.name_of(tool)
    if name:
        card["service"] = name
    summary = first_sentence(str(tool.get("description") or ""))
    if summary:
        card["summary"] = summary
    read = standards.card_inputs(obj(tool.get("inputSchema")))
    code_inputs = obj(card.get("inputs"))
    inputs: dict[str, Any] = {}
    for in_name, spec in obj(read["inputs"]).items():
        code = obj(code_inputs.get(in_name))
        told = dict(obj(spec))
        if told.get("type") == "file" and not told.get("means"):
            said = f"{in_name} {told.get('description') or ''}"
            told["means"] = code.get("means") or _means_of_name(said)
        if not told.get("description") and code.get("description"):
            told["description"] = code["description"]
        inputs[in_name] = told
    only_code = [n for n in code_inputs if n not in inputs]
    for in_name in only_code:
        inputs[in_name] = code_inputs[in_name]
    card["inputs"] = inputs
    card["effect"] = standards.effect_of(tool)
    outputs = obj(card.get("outputs"))
    said_out = standards.outputs_of(obj(tool.get("outputSchema")))
    card["outputs"] = {**said_out, **outputs}  # the code's own word on a name wins
    card["example"] = example_for(root, inputs)
    exported = obj(tool.get("_meta")).get(standards.EFFECT_KEY) is not None
    taken = [
        *(["its name"] if name else []), *(["its description"] if summary else []),
        f"{len(obj(read['inputs']))} inputs",
        f"effect {card['effect']} (from its {'AMSYOS export' if exported else 'hints'})",
    ]  # fmt: skip
    notes = [f"started from the MCP tool {tool.get('name')}: {', '.join(taken)}"]
    if only_code:
        notes.append(
            f"the code also reads {', '.join(only_code)}, which the tool does not list: kept"
        )
    if said_out and not outputs:
        notes.append(f"outputs from the tool's outputSchema: {', '.join(said_out)}")
    notes += [f"not carried by a card: {n}" for n in seq(read["notes"])]
    return notes


# On the space's Python already (node.build: torch, torchvision, numpy, pillow, requests)
# and the job's own module: never asked of pip again.
PREINSTALLED = frozenset({"torch", "torchvision", "numpy", "PIL", "requests", "amsyos"})
# Where a module's name is not its pip package's.
PIP_NAMES = {
    "cv2": "opencv-python-headless", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "yaml": "pyyaml", "bs4": "beautifulsoup4", "dateutil": "python-dateutil",
    "docx": "python-docx", "pptx": "python-pptx", "fitz": "pymupdf", "Crypto": "pycryptodome",
    "dotenv": "python-dotenv", "jwt": "pyjwt", "magic": "python-magic", "attr": "attrs",
    "serial": "pyserial", "OpenGL": "PyOpenGL", "zmq": "pyzmq", "mpl_toolkits": "matplotlib",
    "Levenshtein": "python-Levenshtein", "sentence_transformers": "sentence-transformers",
}  # fmt: skip
_OPTIONAL = frozenset({"ImportError", "ModuleNotFoundError", "Exception"})


def _catches_import(handler: ast.ExceptHandler) -> bool:
    kind = handler.type
    if kind is None:
        return True
    names = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    return any(isinstance(n, ast.Name) and n.id in _OPTIONAL for n in names)


def imported_packages(root: Path) -> list[str]:
    """The pip packages the Python code imports, in the order first imported: neither the
    standard library, nor the folder's own modules, nor what the space has already, nor
    an import tried inside `try: ... except ImportError`."""
    sources = _sources(root)
    local = {part.removesuffix(".py") for p in sources for part in p.relative_to(root).parts}
    found: dict[str, None] = {}
    for path in sources:
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        optional: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(_catches_import(h) for h in node.handlers):
                optional.update(id(inner) for sub in node.body for inner in ast.walk(sub))
        for node in ast.walk(tree):
            if id(node) in optional:
                continue
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if (
                    top in sys.stdlib_module_names or top in local or top in PREINSTALLED
                    or top.startswith("_")
                ):  # fmt: skip
                    continue
                found.setdefault(PIP_NAMES.get(top, top), None)
    return list(found)


def _pyproject_deps(root: Path) -> list[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return []
    import tomllib  # noqa: PLC0415 - only for a Python project without requirements.txt

    try:
        deps: Any = (
            tomllib.loads(path.read_text(encoding="utf-8"))
            .get("project", {})
            .get("dependencies", [])
        )
    except (tomllib.TOMLDecodeError, OSError, AttributeError):
        return []
    return [str(d) for d in deps] if isinstance(deps, list) else []  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


# ---------------------------------------------------------------- writing one
def _flow(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, width=10_000, sort_keys=False)
    return text.removesuffix("\n...\n").strip()  # a lone scalar ends its document with "..."


# Each top-level key: what it is, in a comment on its line, and a sample when it is unset.
TEMPLATE: tuple[tuple[str, str, str], ...] = (
    ("service", "area.action or area.group.action; called t_<organisation>.<service>", ""),
    ("version", '"1.0" (deploy counts the patch) or "1.0.3", in quotes', '"1.0"'),
    ("summary", "what one call takes and makes, English, 3 to 120 characters", ""),
    ("tags", "words discover also finds it by", "[csv, chart]"),
    ("where", "space (the organisation's rented node) | node (an own node)", "space"),
    ("runtime", "python | node | java | go | r", "python"),
    ("system", "programs from conda-forge, name or name=version", "[ffmpeg]"),
    ("packages", "pip packages; unset: requirements.txt", "[numpy==2.1.3]"),
    ("setup", "run once when the image is made", '"npm ci"'),
    ("command", "started once a job, in the code's folder", "python main.py"),
    ("serve", "or beside command: a server kept warm between jobs", "python server.py"),
    ("models", "public Hugging Face models the code loads, pinned", '["org/name@<40 hex>"]'),
    ("resources", "gpu: none|optional|required; min_vram_gb, cpu, ram_gb, disk_gb", ""),
    ("effect", "pure | read | write | external (acts outside AMSYOS)", "pure"),
    ("estimate", "how long one call takes, until runs measure it", "30s"),
)


def render(card: dict[str, Any]) -> str:
    """The card as YAML with every key of spec v3 §7.1: what each is for in a comment on
    its line, the unset ones commented out with a sample (`amsyos card --help`)."""
    lines = [
        "# amsyos.yaml: this repository as an AMSYOS service (spec v3 §7.1).",
        "# `amsyos card --help` explains every key; a line starting with # is not read.",
        "# Values written <fill: ...> are what init could not read from the code: replace",
        "# them, then `amsyos card --check`, `amsyos deploy --dry-run`, `amsyos deploy`.",
        "",
    ]

    def line(key: str, value: str, why: str, *, unset: bool = False) -> None:
        head = f"{'# ' if unset else ''}{key}: {value}"
        lines.append(f"{head:<44} # {why}" if len(head) < 44 else f"{head}  # {why}")

    for key, why, sample in TEMPLATE:
        if key in card:
            value = f'"{card[key]}"' if key == "version" else _flow(card[key])
            line(key, value, why)
        elif sample:
            line(key, sample, why, unset=True)
    lines += [
        "",
        "# inputs: what a call gives, by name: {type: string|integer|number|boolean|file,",
        "#   required, default, description, max_length, min, max, multiline: true (text of",
        "#   several lines, max_length up to 12000)}, or {enum: [a, b]};",
        "#   a file also says what it is: means: image|video|audio|sheet|mesh|document|text.",
        "#   Structured (a plan, a scene): one JSON input with schema: <name>.json, its shape",
        "#   in schemas/ (`amsyos schema <name> --from-model main.py:Plan` writes it).",
        "#   The program reads them from $AMSYOS_JOB/job.json, `from amsyos import job`",
        "#   (job.params, job.input(name), job.json(name)) or `amsyos-job param <name>`.",
    ]
    inputs = obj(card.get("inputs"))
    lines.append("inputs:" + ("" if inputs else " {}"))
    lines += [f"  {name}: {_flow(spec)}" for name, spec in inputs.items()]
    lines += [
        "",
        "# outputs: what a call makes, one file each: {type: file, format: png, means: image};",
        "#   the program writes out/<name>.<format>. means patch: a unified diff (--apply).",
        "#   controls: <an enum input of formats>: the call chooses the file's format.",
        "#   Every output is required unless it says optional: true (some calls make none).",
    ]
    outputs = obj(card.get("outputs"))
    lines.append("outputs:" + ("" if outputs else " {}"))
    lines += [f"  {name}: {_flow(spec)}" for name, spec in outputs.items()]
    if not outputs:
        lines.append(f"#  result: {{type: file, format: {FILL}: png|json|txt...>, means: report}}")
    lines += [
        "",
        "# example: one whole call's inputs; @path is a local file deploy uploads first.",
        "#   deploy runs it once as the check of the new version: every required input.",
    ]
    example = obj(card.get("example"))
    lines.append("example:" + ("" if example else " {}"))
    lines += [f"  {name}: {_flow(value)}" for name, value in example.items()]
    lines += [
        "",
        "# tests: deterministic checks of the example's outputs (`amsyos card --help`):",
        "#   - {output: <name>, check: <service whose JSON report is read>, pass: [[field,",
        "#     '==', value]]}; no check: the output itself (json) is read.",
    ]
    if card.get("tests"):
        lines.append(f"tests: {_flow(card['tests'])}")
    else:
        lines.append("# tests: []")
    known = {k for k, _, _ in TEMPLATE} | {"inputs", "outputs", "example", "tests"}
    rest = {k: v for k, v in card.items() if k not in known and k != UNREAD}
    if rest:
        lines.append("")
        lines += [f"{k}: {_flow(v)}" for k, v in rest.items()]
    return "\n".join(lines) + "\n"
