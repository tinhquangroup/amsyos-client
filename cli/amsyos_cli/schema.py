"""`amsyos schema <input>`: the JSON Schema of a structured input, written into the card's
`schemas/` folder and named on that input in amsyos.yaml (sdk/CONTRACT.md §7; the owner,
2026-11-26: a structured input is one JSON input with a schema, so that AI clients call the
service right the first time and its code loads the input straight into its own types).

- `--from-model path.py:Model`: a Pydantic v2 model's `model_json_schema()`, or a dataclass's
  or a TypedDict's through `pydantic.TypeAdapter`, made by the project's own Python (its
  `.venv`, else this one, else python3 on the PATH: the first that has pydantic 2; `--python`
  names one). It imports the module as a module: its `if __name__ == "__main__":` never runs.
  Pydantic's own titles (a key's name, capitalised) are left out: the whole spec, its schemas
  included, is at most 12,000 characters.
- `--from-example [file.json]`: a draft from one value, the card's example of the input when
  no file is given (else its `default`), made as the kernel's own draft is (kernel/schemas.py
  `draft`, the one service.check shows): types, nested objects, lists from their items, every
  number a `number`. Never stricter than the code (CONTRACT.md §7): no `required`, no
  `additionalProperties: false`, no enum; say those where the code checks them.

It writes `schemas/<input>.json` beside amsyos.yaml and `schema: <input>.json` on the input (a
text input becomes `multiline: true`), changing only that input's lines. It runs here and sends
nothing; `card.load` reads the file into the spec `card --check` and `deploy` send.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import yaml

from amsyos_cli import card as cards
from amsyos_cli.common import CliError, Out, obj, seq

SCHEMA_MOST = 20_000  # characters of one schema as JSON (kernel/schemas.py MAX_SCHEMA_CHARS)
SPEC_MOST = 12_000  # the whole spec as JSON, schemas included (kernel/conform.py SPEC_MOST)
TEXT_MAX = 1000  # a text input's most when the card gives no max_length
MODEL_S = 60.0  # importing the model's module: torch and the like take a while
NO_PYDANTIC, OLD_PYDANTIC, NO_NAME = 3, 4, 5  # the helper's exit codes
TEXT_TYPES = frozenset({"string", "str", "text"})
FILE_TYPES = frozenset({"file", "asset"})

# Run by the project's Python: pydantic is looked for first, so a Python without it runs
# nothing of the project's code.
_HELPER = r"""
import importlib, importlib.util, json, os, sys
path, name, root, out = sys.argv[1:5]
try:
    import pydantic
except ImportError:
    sys.exit(3)
if not hasattr(pydantic, "TypeAdapter"):
    sys.exit(4)
for where in (root, os.path.dirname(os.path.abspath(path))):
    if where not in sys.path:
        sys.path.insert(0, where)
rel = os.path.relpath(os.path.abspath(path), root)
dotted = rel[:-3].replace(os.sep, ".").replace("/", ".")
if rel.startswith("..") or not all(p.isidentifier() for p in dotted.split(".")):
    spec = importlib.util.spec_from_file_location("amsyos_schema_source", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
else:
    module = importlib.import_module(dotted.removesuffix(".__init__"))
target = module
for part in name.split("."):
    if not hasattr(target, part):
        sys.stderr.write("no " + name + " in " + path + "\n")
        sys.exit(5)
    target = getattr(target, part)
if hasattr(target, "model_json_schema"):
    schema = target.model_json_schema()
else:
    schema = pydantic.TypeAdapter(target).json_schema()
with open(out, "w", encoding="utf-8") as f:
    json.dump({"schema": schema, "pydantic": pydantic.VERSION}, f)
"""


# ---------------------------------------------------------------- a draft from a value
# The kernel's own draft (kernel/schemas.py `draft`), which service.check and service.define
# show beside a JSON input with no schema: the same here, so both say the same.
DRAFT_DEEPEST = 32  # levels of an example a draft follows; deeper ones take anything


def draft(example: Any) -> dict[str, Any]:
    """A first schema made from one example value: an object's keys with their types, an
    array's items as one schema of every item, every number a `number` (an example's 1 may
    be 1.5 in the next call), null as anything. Never `required`, `additionalProperties:
    false` or an enum: a schema is never stricter than the code (CONTRACT.md §7), and an
    example cannot say what the code refuses."""
    return _drafted([example], 0)


def _draft_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):  # before int: True is an int to Python
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list | tuple):
        return "array"
    return "string"


def _drafted(values: list[Any], level: int) -> dict[str, Any]:
    """One schema that every one of `values` passes."""
    if level > DRAFT_DEEPEST:
        return {}
    kinds: dict[str, list[Any]] = {}
    for value in values:
        kinds.setdefault(_draft_kind(value), []).append(value)
    if set(kinds) <= {"null"}:
        return {}
    forms: list[dict[str, Any]] = []
    for kind, same in kinds.items():
        form: dict[str, Any] = {"type": kind}
        if kind == "object":
            keys: dict[str, list[Any]] = {}
            for value in same:
                for key, inner in obj(value).items():
                    keys.setdefault(str(key), []).append(inner)
            if keys:
                form["properties"] = {k: _drafted(vs, level + 1) for k, vs in keys.items()}
        elif kind == "array":
            items = [x for value in same for x in value]
            if items:
                form["items"] = _drafted(items, level + 1)
        forms.append(form)
    if len(forms) == 1:
        return forms[0]
    plain: list[Any] = [f["type"] for f in forms if len(f) == 1]
    nested: list[Any] = [f for f in forms if len(f) > 1]
    if not nested:
        return {"type": plain}
    return {
        "anyOf": [*nested, *([{"type": plain[0] if len(plain) == 1 else plain}] if plain else [])]
    }


# ---------------------------------------------------------------- from a model
def _pythons(root: Path, given: str | None) -> list[str]:
    """The Pythons that may have the project's packages, the project's own first."""
    if given:
        return [given]
    found: list[str] = []
    venvs = [base / name for base in [root, *list(root.parents)[:3]] for name in (".venv", "venv")]
    if os.environ.get("VIRTUAL_ENV"):
        venvs.append(Path(os.environ["VIRTUAL_ENV"]))
    for venv in venvs:
        for exe in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
            if exe.is_file():
                found.append(str(exe))
    found.append(sys.executable)
    found += [w for name in ("python3", "python") if (w := shutil.which(name))]
    return list(dict.fromkeys(found))


def _auto_title(name: str) -> str:
    """The title pydantic gives a key by itself (`max_items` → "Max Items")."""
    return name.title().replace("_", " ").strip()


def untitled(node: Any, name: str | None = None) -> Any:
    """The schema without the titles pydantic writes by itself: a key's name capitalised,
    a model's or a `$defs` entry's own name. A title someone wrote stays."""
    if isinstance(node, list):
        return [untitled(x) for x in node]  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(node, dict):
        return node
    told: dict[str, Any] = {}
    for key, value in obj(node).items():
        if key == "properties" and isinstance(value, dict):
            told[key] = {p: untitled(s, _auto_title(p)) for p, s in obj(value).items()}
        elif key in ("$defs", "definitions") and isinstance(value, dict):
            told[key] = {d: untitled(s, d) for d, s in obj(value).items()}
        elif key == "title" and name is not None and value in (name, _auto_title(name)):
            continue
        else:
            told[key] = untitled(value)
    return told


def from_model(
    root: Path, given: str, python: str | None = None, *, cwd: Path | None = None,
    timeout: float = MODEL_S,
) -> tuple[dict[str, Any], str]:  # fmt: skip
    """The schema of `path.py:Model` and what made it ("pydantic 2.9.2, .venv/bin/python").
    `cwd`: where its module is imported from (the project by default; init gives an empty
    folder, so that a module that does something when imported does it there)."""
    root = root.resolve()
    path_text, _, name = given.rpartition(":")
    if not path_text or not name or not path_text.endswith(".py"):
        raise CliError(
            "INVALID_INPUT",
            f"--from-model {given[:80]!r}: the model as file.py:Name, like main.py:Plan "
            "(a Pydantic model, a dataclass or a TypedDict)",
        )
    path = (root / path_text) if not Path(path_text).is_absolute() else Path(path_text)
    if not path.is_file():
        path = Path(path_text)
    path = path.resolve()
    if not path.is_file():
        raise CliError("INVALID_INPUT", f"--from-model: no file {path_text} (here or in {root})")
    old: str | None = None
    with tempfile.TemporaryDirectory(prefix="amsyos-schema-") as tmp:
        out = Path(tmp) / "schema.json"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "AMSYOS_JOB": tmp}
        for exe in _pythons(root, python):
            try:
                done = subprocess.run(  # noqa: S603 - the project's own Python, by the person's ask
                    [exe, "-c", _HELPER, str(path), name, str(root), str(out)],
                    cwd=str(cwd or root), env=env, capture_output=True, text=True,
                    timeout=timeout, check=False,
                )  # fmt: skip
            except subprocess.TimeoutExpired:
                raise CliError(
                    "MODEL_NOT_READ",
                    f"importing {path_text} with {exe} took over {timeout:.0f}s: put the model "
                    "in a module that loads nothing heavy when imported, or --from-example",
                ) from None
            except OSError:
                continue
            if done.returncode == NO_PYDANTIC:
                continue
            if done.returncode == OLD_PYDANTIC:
                old = exe
                continue
            if done.returncode != 0 or not out.is_file():
                last = [line for line in done.stderr.splitlines() if line.strip()][-1:] or [
                    f"exit {done.returncode}"
                ]
                raise CliError(
                    "MODEL_NOT_READ",
                    f"{exe} could not read {name} from {path_text}: {last[0][:300]}"
                    + ("" if done.returncode == NO_NAME else " (its imports must work there: "
                       "--python <the project's python>)"),
                )  # fmt: skip
            said = json.loads(out.read_text(encoding="utf-8"))
            schema = untitled(said["schema"], name.rsplit(".", 1)[-1])
            return schema, f"pydantic {said.get('pydantic')}, {exe}"
    raise CliError(
        "NO_PYDANTIC",
        (f"{old} has pydantic 1: --from-model needs pydantic 2 (model_json_schema)"
         if old else "no Python here has pydantic 2 (tried the project's .venv, this one, "
         "python3)")
        + ": install it in the project's environment (pip install 'pydantic>=2'), name one "
        "with --python, or write a draft from a value: amsyos schema <input> --from-example",
    )  # fmt: skip


# ---------------------------------------------------------------- from an example
def example_of(card: dict[str, Any], root: Path, name: str) -> Any:
    """The card's example of the input: its JSON text parsed, a mapping or list as written,
    a local file (`@path`) read; else the input's own `example` or `default`."""
    spec = obj(obj(card.get("inputs")).get(name))
    for where, value in (
        (f"example.{name}", obj(card.get("example")).get(name)),
        (f"inputs.{name}.example", spec.get("example")),
        (f"inputs.{name}.default", spec.get("default")),
    ):
        if value is None or value == "":
            continue
        if isinstance(value, dict | list):
            return cast("dict[str, Any] | list[Any]", value)
        if isinstance(value, str) and value.startswith("@"):
            return read_json(root / value[1:], f"{where} ({value})")
        if isinstance(value, str) and value.startswith("asset://"):
            raise CliError(
                "INVALID_INPUT",
                f"{where} is a file of AMSYOS ({value[:40]}): give a local copy, amsyos schema "
                f"{name} --from-example path/to/{name}.json",
            )
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError as exc:
                raise CliError(
                    "INVALID_INPUT", f"{where} is not JSON text ({exc}): fix it, or give a file: "
                    f"amsyos schema {name} --from-example path/to/{name}.json",
                ) from None  # fmt: skip
    raise CliError(
        "NO_EXAMPLE",
        f"{cards.CARD} gives no value of {name}: give example.{name} one whole JSON value, or a "
        f"file: amsyos schema {name} --from-example path/to/{name}.json (or --from-model "
        "file.py:Model)",
    )


def read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        raise CliError("INVALID_INPUT", f"{what}: no file {path}") from None
    except (OSError, ValueError) as exc:
        raise CliError("INVALID_INPUT", f"{what}: {path} is not JSON ({exc})") from None


# ---------------------------------------------------------------- the card's input
def _kind(spec: Any) -> str:
    """text, file or another type word of a card's input."""
    if isinstance(spec, dict):
        given = obj(spec)
        if "enum" in given:
            return "enum"
        word = str(given.get("type", "string"))
    else:
        word = str(spec)
    return "text" if word in TEXT_TYPES else "file" if word in FILE_TYPES else word


def _wanted(spec: Any, file: str) -> dict[str, Any]:
    """The input's spec with the schema named, and several lines when it is text."""
    told: dict[str, Any] = dict(obj(spec)) if isinstance(spec, dict) else {"type": spec}
    if _kind(told) == "text":
        told["multiline"] = True
    told["schema"] = file
    return told


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _quiet(line: str) -> bool:
    """A blank line or a comment: no key of the card."""
    return not line.strip() or line.lstrip().startswith("#")


def set_on_card(text: str, name: str, file: str) -> str | None:
    """amsyos.yaml's text with `schema: <file>` (and `multiline: true` on text) on the
    input `name`, every other line as it was; None when the input's lines are not of a form
    changed safely here (the caller says what to write by hand)."""
    lines = text.splitlines()
    top = next((i for i, line in enumerate(lines) if re.match(r"inputs:\s*(#.*)?$", line)), None)
    if top is None:
        return None
    at = child = None
    for i in range(top + 1, len(lines)):
        if _quiet(lines[i]):
            continue
        if _indent(lines[i]) == 0:
            break
        child = _indent(lines[i]) if child is None else child
        if _indent(lines[i]) == child and re.match(rf"\s*{re.escape(name)}\s*:", lines[i]):
            at = i
            break
    if at is None or child is None:
        return None
    head, _, rest = lines[at].partition(":")
    rest = rest.strip()
    if rest and not rest.startswith("#"):  # `plan: {type: string, ...}` or `plan: string`
        try:
            spec: Any = yaml.safe_load(rest)
        except yaml.YAMLError:
            return None
        if not isinstance(spec, dict | str):
            return None
        lines[at] = f"{head}: {cards._flow(_wanted(spec, file))}"  # pyright: ignore[reportPrivateUsage]
        return "\n".join(lines) + "\n"
    end = at + 1  # `plan:` with its keys indented below
    while end < len(lines) and (_quiet(lines[end]) or _indent(lines[end]) > child):
        end += 1
    keys = [i for i in range(at + 1, end) if not _quiet(lines[i])]
    if not keys:
        return None
    inner = _indent(lines[keys[0]])
    wanted: dict[str, str] = {}
    spec = obj(yaml.safe_load("\n".join(lines[at:end])) or {}).get(name)
    if _kind(spec) == "text":
        wanted["multiline"] = "true"
    wanted["schema"] = file
    there = [k for k in wanted if any(re.match(rf"\s*{k}\s*:", lines[i]) for i in keys)]
    for key in [*there, *(k for k in wanted if k not in there)]:  # replaced, then added
        value = wanted[key]
        found = next(
            (i for i in keys if _indent(lines[i]) == inner
             and re.match(rf"\s*{key}\s*:", lines[i])), None,
        )  # fmt: skip
        if found is None:  # after the input's last key (and what that key holds below)
            lines.insert(keys[-1] + 1, f"{' ' * inner}{key}: {value}")
            keys.append(keys[-1] + 1)
            continue
        lines[found] = f"{' ' * inner}{key}: {value}"
        gone = found + 1  # an inline schema written as a block below its key
        while gone < len(lines) and (
            _indent(lines[gone]) > inner
            if not _quiet(lines[gone])
            else gone + 1 < len(lines)
            and not _quiet(lines[gone + 1])
            and _indent(lines[gone + 1]) > inner
        ):
            gone += 1
        del lines[found + 1 : gone]
        keys = [k for k in keys if not found < k < gone]
        keys = [k - (gone - found - 1) if k >= gone else k for k in keys]
    return "\n".join(lines) + "\n"


def _changed_only(before: Any, after: Any, name: str, file: str) -> bool:
    """The card read again is the card it was, but the input names the schema."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    was, now = obj(before), obj(after)
    if {k: v for k, v in was.items() if k != "inputs"} != {
        k: v for k, v in now.items() if k != "inputs"
    }:
        return False
    old, new = obj(was.get("inputs")), obj(now.get("inputs"))
    if {k: v for k, v in old.items() if k != name} != {k: v for k, v in new.items() if k != name}:
        return False
    return _wanted(old.get(name), file) == new.get(name)


# ---------------------------------------------------------------- the command
async def cmd_schema(args: argparse.Namespace, out: Out) -> int:
    root = Path(args.dir).resolve()
    name: str = args.input
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
        raise CliError("INVALID_INPUT", f"{name[:60]!r}: an input's name, snake_case, like plan")
    if bool(args.from_model) == (args.from_example is not None):
        raise CliError(
            "INVALID_INPUT",
            "say where the schema comes from: --from-model file.py:Model (a Pydantic model, "
            "a dataclass or a TypedDict) or --from-example [file.json] (a draft from a value)",
        )
    path = root / cards.CARD
    card = cards.load(root, inline=False) if path.is_file() else None
    inputs = obj((card or {}).get("inputs"))
    if card is not None and name not in inputs:
        raise CliError(
            "INVALID_INPUT",
            f"no input {name} in {cards.CARD} (its inputs: {', '.join(inputs) or 'none'}): "
            f"add it (inputs: {{{name}: {{type: string, required: true, description: ...}}}}), "
            "or name one of them",
        )
    kind = _kind(inputs.get(name)) if card is not None else "text"
    if kind not in ("text", "file"):
        raise CliError(
            "INVALID_INPUT",
            f"inputs.{name} is {kind}: a schema is the shape of JSON, given as text (type: "
            "string) or as a file (type: file); a structured input is one of those",
        )
    notes: list[str] = []
    if args.from_model:
        schema, how = from_model(root, args.from_model, args.python)
        source = f"{args.from_model} ({how})"
    else:
        if args.from_example:
            value = read_json(Path(args.from_example).resolve(), "--from-example")
            source = f"the example {args.from_example}"
        elif card is None:
            raise CliError(
                "NO_CARD", f"no {cards.CARD} in {root}: give a file, amsyos schema {name} "
                f"--from-example path/to/{name}.json, or amsyos init first",
            )  # fmt: skip
        else:
            value = example_of(card, root, name)
            source = f"the card's example of {name}"
        if not isinstance(value, dict | list):
            raise CliError(
                "INVALID_INPUT",
                f"{source} is {type(value).__name__}, not an object or a list: a structured "
                "input is JSON with keys (or a list of such)",
            )
        schema = draft(value)
        compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        most = obj(inputs.get(name)).get("max_length") or TEXT_MAX
        if kind == "text" and isinstance(most, int) and len(compact) > most:
            notes.append(
                f"the example is {len(compact):,} characters as JSON, over inputs.{name}'s "
                f"max_length {most}: raise it (at most 12000)"
            )
        notes.append(
            "a draft from one value: every key optional, any other key let in. Say "
            "required: [...] for the keys your code needs, and the enums and bounds it checks; "
            "never stricter than your code (a call it would take is refused before it runs)"
        )
    size = len(json.dumps(schema, ensure_ascii=False, sort_keys=True))
    if size > SCHEMA_MOST:
        raise CliError(
            "SCHEMA_TOO_LARGE",
            f"the schema is {size:,} characters as JSON: at most {SCHEMA_MOST:,}; drop "
            "descriptions or examples from the model, or split the input",
        )
    file = f"{name}.json"
    target = root / cards.SCHEMA_DIR / file
    text = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    shown = f"{cards.SCHEMA_DIR}/{file}"
    was = target.read_text(encoding="utf-8") if target.is_file() else None
    if was is not None and was != text and not args.force:
        raise CliError(
            "SCHEMA_EXISTS",
            f"{shown} is there and differs from the schema made from {source}: --force replaces it",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if was != text:
        target.write_text(text, encoding="utf-8")
    keys = list(obj(schema.get("properties")))
    needed = seq(schema.get("required"))
    wrote = "unchanged" if was == text else "replaced" if was is not None else "wrote"
    lines = [
        f"{wrote} {shown} from {source}: {size:,} characters"
        + (f"; keys {', '.join(keys[:12])}{' …' if len(keys) > 12 else ''}" if keys else "")
        + (f" ({len(needed)} required)" if needed else "")
    ]
    answer: dict[str, Any] = {
        "input": name, "schema_file": str(target), "from": source, "chars": size,
        "written": wrote,
    }  # fmt: skip
    if card is None:
        answer["card"] = "none"
        lines.append(
            f"no {cards.CARD} here: amsyos init writes one; then schema: {file} on inputs.{name}"
        )
    else:
        answer["card"] = _on_card(path, card, name, file, lines)
        spec_size = len(json.dumps(cards.spec_of(cards.load(root)), separators=(",", ":")))
        answer["spec_chars"] = spec_size
        if spec_size > SPEC_MOST:
            notes.append(
                f"the spec is now {spec_size:,} characters as JSON, schemas included: at most "
                f"{SPEC_MOST:,} (service.define refuses more): shorten the schemas' descriptions"
            )
    answer["notes"] = notes
    lines += [f"  {n}" for n in notes]
    lines.append(
        f"next: amsyos card --check (AMSYOS checks the schema), then amsyos deploy: a call whose "
        f"{name} does not fit is refused in seconds, before a machine starts; your code reads it "
        f'with job.json("{name}")'
    )
    out.emit(answer, *lines)
    return 0


def _on_card(path: Path, card: dict[str, Any], name: str, file: str, lines: list[str]) -> str:
    """`schema: <file>` set on the input in amsyos.yaml: set, already, or by hand."""
    spec = obj(card.get("inputs")).get(name)
    wanted = _wanted(spec, file)
    if isinstance(spec, dict) and all(obj(spec).get(k) == v for k, v in wanted.items()):
        lines.append(f"{cards.CARD}: inputs.{name} names {file} already")
        return "already"
    text = path.read_text(encoding="utf-8")
    changed = set_on_card(text, name, file)
    after: Any = None
    if changed is not None:
        with contextlib.suppress(yaml.YAMLError):
            after = yaml.safe_load(changed)
    if changed is None or not _changed_only(yaml.safe_load(text), after, name, file):
        lines.append(
            f"{cards.CARD}: not changed here (its inputs are written in a form this does not "
            f"edit): add schema: {file}"
            + (" and multiline: true" if "multiline" in wanted else "")
            + f" to inputs.{name} yourself"
        )
        return "by_hand"
    path.write_text(changed, encoding="utf-8")
    extra = ", multiline: true" if "multiline" in wanted else ""
    lines.append(f"{cards.CARD}: inputs.{name} now has schema: {file}{extra}")
    return "set"


def add_parser(add: Any) -> None:
    """`amsyos schema` in the parser (`main.parser`'s `add`)."""
    p = add(
        "schema", cmd_schema,
        "a structured input's JSON Schema, from your model or an example, into the card",
        "A structured input (a plan, a scene, options with nested keys) is one JSON input\n"
        "with a schema: AI clients then call the service right the first time, AMSYOS refuses\n"
        "a value that does not fit in seconds, before a machine starts, and your code loads it\n"
        "straight into its own types (job.json(name)). This writes schemas/<input>.json beside\n"
        "amsyos.yaml and schema: <input>.json on that input (a text input becomes multiline).\n"
        "Runs here; nothing is sent.",
        epilog="examples:\n"
        "  amsyos schema plan --from-model main.py:Plan     a Pydantic model (v2), a dataclass "
        "or a TypedDict\n"
        "  amsyos schema plan --from-example                a draft from the card's example of "
        "plan\n"
        "  amsyos schema scene --from-example scene.json    a draft from a file\n"
        "then: amsyos card --check, amsyos deploy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    p.add_argument("input", help="the input's name in amsyos.yaml, like plan")
    p.add_argument(
        "--from-model", metavar="FILE.py:NAME",
        help="the model the code validates the input with; its module is imported by the "
        "project's Python (its .venv first), never run as __main__",
    )  # fmt: skip
    p.add_argument(
        "--from-example", nargs="?", const="", default=None, metavar="FILE.json",
        help="a draft from a value: this file, else the card's example of the input",
    )  # fmt: skip
    p.add_argument("--python", help="with --from-model: the Python to import the model with")
    p.add_argument("--force", action="store_true", help="replace schemas/<input>.json")
    p.add_argument("--dir", default=".", help="the card's folder (default here)")
