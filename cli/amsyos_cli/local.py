"""`amsyos run --local`: this repository's own service run here, as its space would run
it, before anything is deployed (a customer: a program's mistakes in how
it reads job.json or writes its outputs showed only after a machine was rented).

It runs the organisation's own code on the organisation's own computer, where its agent
already runs it: never on AMSYOS's machines, and nothing is sent to AMSYOS or asked of it.

What the space does (services/space/machine/space_run.py, sdk/CONTRACT.md §3), here:
- a job folder: `job.json` ({"params", "inputs", "input_names", "service", ...}) from the
  card's `example` (params `k=v` change it), its defaults filled in (`service.id` is the
  card's own name: on AMSYOS it begins with `t_<organisation>.`, which nothing here asks
  AMSYOS for); an `@path` file copied to
  `in/<input>.<ext>` as the driver names it (drivers/base.py `InputFile.job_path`);
  `out/`, `tmp/`;
- the card's `command` (or its `serve`, by the protocol of contract 1) started in a copy
  of what deploy packs (a Python service without `setup`; any other in the repository
  itself, where its build is), with the environment's names the space sets: AMSYOS_JOB,
  PYTHONPATH and NODE_PATH to the job module this wheel ships (the same files the space
  gets, amsyos_cli/sdk), `amsyos-job` on PATH, TMPDIR in the job's folder, the cores. No
  AMSYOS key or token: AMSYOS_* and every variable named like a key or token are left out;
- its end read as the space reads it (error.json, the exit code, out/), then what the
  check of a version does (kernel/checks.py, contract 1 §5): every declared output there,
  not empty, its first bytes its format; then the card's tests that read a JSON output.
  A test with a `check` service runs on AMSYOS only (deploy runs it).

The format check and the tests' rules are kernel/conform.py `sniff` and kernel/checks.py
`_look`/`_holds`, said again here: those modules need the server to import.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeGuard, cast

from amsyos_cli import card as cards
from amsyos_cli import deploy
from amsyos_cli import machine as machines
from amsyos_cli.common import CliError, Out, obj, seq
from amsyos_cli.files import human, text_of

LONG_HELP = (
    "run --local: runs your code here, as the space would; nothing is sent. In a repository "
    "with amsyos.yaml: a job folder made as the space makes it (job.json from the card's "
    "example; params k=v or k=@file change it), the card's command run there with the job "
    "module (import amsyos, amsyos-job), then its outputs checked against the card's "
    "outputs and its tests, as deploy's check would. Free. --keep keeps the job's folder "
    "in <out>/<job>/ of the repository (default out/, which deploy never packs)."
)
LIMIT_S = 3600  # a job here runs at most this long, as a space's first hour
STOP_S = 10  # a server, after its job: this long to end by itself
TAIL_LINES = 20
LINE_MAX = 300  # a line of the program's output, as printed
REPORT_MAX = 256 * 1024  # a test's report read, at most (kernel/checks.py)
FOUND_MAX = 80
SPACE_PYTHON = "3.11"
FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")  # space_common.py
OUTPUT_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")  # space_common.py NAME
PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")  # drivers/vast.py
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.I)
THREADS = (
    "AMSYOS_CPUS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "NUMEXPR_MAX_THREADS",
)  # fmt: skip
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MISSING = object()
# Run as a server's first program on POSIX: it is told its own pid (`amsyos-job next` and
# `done` reach its stdin and stdout through it), then becomes the card's `serve`.
_SERVE_SHIM = (
    "import os, sys; os.environ['AMSYOS_SERVE_PID'] = str(os.getpid()); "
    "os.execvp(sys.argv[1], sys.argv[1:])"
)


# ---------------------------------------------------------------- the job module
def sdk_dir() -> Path:
    """The bindings of the job contract this command ships, laid out as on the space's
    /opt/amsyos: `amsyos/` (PYTHONPATH), `node/amsyos/` (NODE_PATH), `bin/amsyos-job`. In
    the wheel they are amsyos_cli/sdk/ (cli/pyproject.toml, force-include of sdk/); from
    the source tree, the repository's sdk/."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "sdk", here.parent.parent / "sdk"):
        if (candidate / "amsyos" / "__init__.py").is_file():
            return candidate
    raise CliError(
        "NO_JOB_MODULE",
        "this amsyos command lacks the job module (amsyos_cli/sdk): amsyos self-update "
        "--force installs it again",
    )


# ---------------------------------------------------------------- the job's params
def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _enum(value: Any, values: list[Any]) -> tuple[Any, bool]:
    """(the value of `values` it is, matched by value: 90 and 90.0 are 90, "90" is the
    number 90), else (_MISSING, False); True when it was changed to match."""
    for allowed in values:
        if _number(allowed) and _number(value) and float(allowed) == float(value):
            return allowed, allowed != value or type(allowed) is not type(value)
        if allowed == value and type(allowed) is type(value):
            return allowed, False
    for allowed in values:
        if isinstance(value, str) and _number(allowed):
            with contextlib.suppress(ValueError):
                if float(value) == float(allowed):
                    return allowed, True
        if isinstance(allowed, str) and _number(value) and allowed == str(value):
            return allowed, True
    return _MISSING, False


def _typed(name: str, kind: str, value: Any, spec: dict[str, Any]) -> tuple[Any, str | None]:
    """The value as the input's type takes it, or a problem; a value changed to fit is
    noted by the caller (as AMSYOS says it in `fixed`)."""
    if kind == "int":
        if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            return value, f"{name}: a whole number, not {json.dumps(value)[:40]}"
    elif kind == "float":
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                value = float(value)
        if not _number(value):
            return value, f"{name}: a number, not {json.dumps(value)[:40]}"
    elif kind == "bool":
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        if not isinstance(value, bool):
            return value, f"{name}: true or false, not {json.dumps(value)[:40]}"
    elif kind == "str":
        if not isinstance(value, str):
            value = json.dumps(value) if isinstance(value, dict | list) else str(value)
        most = spec.get("max_length")
        if isinstance(most, int) and len(value) > most:
            return value, f"{name}: at most {most} characters, not {len(value)}"
    if _number(value):
        low, high, at = spec.get("min"), spec.get("max"), float(value)
        if _number(low) and at < float(low):
            return value, f"{name}: at least {low}, not {value}"
        if _number(high) and at > float(high):
            return value, f"{name}: at most {high}, not {value}"
    return value, None


def _params(
    card: dict[str, Any], root: Path, given: dict[str, Any], files: dict[str, Path]
) -> tuple[dict[str, Any], dict[str, Path], list[str]]:
    """The job's params (defaults filled in), its input files (input → local file) and
    what was changed to fit; every problem at once as INVALID_PARAMS."""
    raw_inputs = obj(card.get("inputs"))
    example = obj(card.get("example"))
    asked = {**example, **given}
    problems: list[str] = []
    notes: list[str] = []
    params: dict[str, Any] = {}
    sources: dict[str, Path] = {}
    unknown = [k for k in asked if k not in raw_inputs]
    if unknown:
        known = ", ".join(raw_inputs) or "none"
        problems += [f"{k}: not an input of the card (its inputs: {known})" for k in unknown]
    for name, raw in raw_inputs.items():
        spec = cards.input_of(raw)
        kind = str(spec.get("type"))
        if name not in asked:
            if "default" in spec:
                params[name] = spec["default"]
            elif spec.get("required"):
                problems.append(f"{name}: required; give it in the example or as {name}=…")
            continue
        value = asked[name]
        if kind == "asset":
            if name in files:
                source = files[name].resolve()
            elif isinstance(value, str) and value.startswith("@"):
                source = root / value[1:]
            else:
                problems.append(
                    f"{name}: {str(value)[:60]} is no local file; run --local reads no file "
                    f"of AMSYOS (nothing is sent or asked): give one as {name}=@path"
                )
                continue
            if not source.is_file():
                problems.append(f"{name}: no file {source}")
                continue
            sources[name] = source
            params[name] = f"asset://a_local_{name}"  # what a call gives: its file's ref
            continue
        if name in files:  # `k=@path` of an input that takes no file: its text, as `run` sends
            value = text_of(name, files[name], kind)
        if kind == "enum":
            values = seq(obj(raw).get("enum")) or seq(spec.get("values"))
            matched, changed = _enum(value, values)
            if matched is _MISSING:
                listed = ", ".join(json.dumps(v) for v in values)
                problems.append(f"{name}: one of [{listed}], not {json.dumps(value)[:40]}")
                continue
            if changed:
                notes.append(f"{name}: {json.dumps(value)} taken as {json.dumps(matched)}")
            params[name] = matched
            continue
        typed, wrong = _typed(name, kind, value, spec)
        if wrong:
            problems.append(wrong)
            continue
        if typed != value or type(typed) is not type(value):
            notes.append(f"{name}: {json.dumps(value)[:40]} taken as {json.dumps(typed)[:40]}")
        params[name] = typed
    if problems:
        raise CliError(
            "INVALID_PARAMS",
            "; ".join(problems) + " (the card's example, and params k=v given here)",
            {"fields": [p.split(":", 1)[0] for p in problems]},
        )
    return params, sources, notes


# ---------------------------------------------------------------- the job's folder
def _base(job_id: str, keep: Path | None) -> Path:
    """The run's folder: with --keep, `<keep>/<job>/` (a customer, 2026-11-23: the desk keeps
    its files in its own folder, not in /tmp); else a temporary one, removed after."""
    if keep is None:
        return Path(tempfile.mkdtemp(prefix="amsyos-local-"))
    base = keep.resolve() / job_id
    try:
        base.mkdir(parents=True)
    except OSError as exc:
        raise CliError(
            "INVALID_INPUT", f"--keep: cannot make {base} ({exc}): give --out DIR"
        ) from None
    return base


def _job_folder(
    base: Path, job_id: str, card: dict[str, Any], params: dict[str, Any], sources: dict[str, Path]
) -> Path:
    """`job/` as space_run.py `hand_over` gives it to the organisation's code."""
    job = base / "job"
    for folder in (job, job / "in", job / "out", job / "tmp"):
        folder.mkdir()
    inputs: dict[str, str] = {}
    names: dict[str, str] = {}
    for name, source in sources.items():
        suffix = source.suffix[:12].lower()
        rel = f"in/{name}{suffix}"
        shutil.copyfile(source, job / rel)
        inputs[name] = rel
        if PLAIN_NAME.fullmatch(source.name):
            names[name] = source.name
    version = cards.full_version(str(card.get("version") or "0.0"))
    service = str(card.get("service") or "")
    told: dict[str, Any] = {
        "job_id": job_id, "service_id": service,
        "service_version": version, "params": params, "max_runtime_s": LIMIT_S,
        "inputs": inputs, "image": f"{cards.image_of(card)}@{version}",
        "service": {"id": service, "version": version},
    }  # fmt: skip
    if names:
        told["input_names"] = names
    (job / "job.json").write_text(json.dumps(told), encoding="utf-8")
    return job


def _code_folder(base: Path, root: Path, card: dict[str, Any]) -> tuple[Path, str]:
    """Where the command runs: a copy of what deploy packs, as the space's code folder is,
    for a Python service without `setup`; else the repository itself (what its setup built
    is here, not in a copy)."""
    runtime = str(card.get("runtime") or "python")
    if runtime != "python" or card.get("setup"):
        return root, f"in {root} itself (runtime {runtime}, setup as built here)"
    files = deploy.files_of(root)
    files = [f for f in files if f not in deploy.agent_files(files, card)]
    left = set(deploy.secrets_in(root, files))
    code = base / "code"
    for rel in files:
        if rel in left:
            continue
        target = code / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    code.mkdir(exist_ok=True)
    said = f"in a copy of what deploy packs ({len(files) - len(left)} files)"
    if left:
        said += f"; left out as credentials: {', '.join(sorted(left)[:4])}"
    return code, said


def _bin(base: Path, sdk: Path) -> Path:
    """`amsyos-job` on PATH, run by this command's own Python (the standard library only)."""
    folder = base / "bin"
    folder.mkdir()
    program = sdk / "bin" / "amsyos-job"
    if os.name == "nt":
        (folder / "amsyos-job.cmd").write_text(
            f'@"{sys.executable}" -I "{program}" %*\r\n', encoding="utf-8"
        )
    else:
        script = folder / "amsyos-job"
        script.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(program))} "$@"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
    return folder


def _python(root: Path) -> tuple[str, str]:
    """The Python a `python …` command runs with: the active virtualenv, the repository's
    .venv or venv, else the first on PATH (the space's is its image's venv)."""
    scripts = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for folder, said in (
        (os.environ.get("VIRTUAL_ENV"), "the active virtualenv"),
        (str(root / ".venv"), ".venv"),
        (str(root / "venv"), "venv"),
    ):
        if folder and (exe := Path(folder, *scripts)).is_file():
            return str(exe), said
    names = ("python", "python3", "py") if os.name == "nt" else ("python3", "python")
    for name in names:
        found = shutil.which(name)
        if found and "WindowsApps" not in found:  # the Store's stub, not a Python
            return found, "PATH"
    return sys.executable, "the amsyos command's own Python"


def _version_of(python: str) -> str | None:
    try:
        said = subprocess.run(  # noqa: S603 - the Python the organisation's command runs with
            [python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return said.stdout.strip() or None


def _env(
    job: Path, sdk: Path, bin_dir: Path, card: dict[str, Any], python_bin: Path | None
) -> dict[str, str]:
    """This machine's environment less AMSYOS's and anything named like a secret, with the
    names the space sets (space_common.py `env_for`, space_run.py)."""
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith("AMSYOS_") and not SECRET_NAME.search(k)
    }  # fmt: skip
    first = [str(python_bin)] if python_bin is not None else []
    key = path_key(env)
    env[key] = os.pathsep.join([*first, str(bin_dir), env.get(key, "")])
    cpu = obj(card.get("resources")).get("cpu")
    machine = os.cpu_count() or 1
    cores = cpu if isinstance(cpu, int) and not isinstance(cpu, bool) and cpu > 0 else machine
    env.update(dict.fromkeys(THREADS, str(min(cores, machine))))
    env.update(dict.fromkeys(("TMPDIR", "TEMP", "TMP"), str(job / "tmp")))
    env.update({
        "AMSYOS_JOB": str(job), "PYTHONPATH": str(sdk), "NODE_PATH": str(sdk / "node"),
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
    })  # fmt: skip
    return env


def path_key(env: dict[str, str]) -> str:
    """PATH's own name in `env` (Windows keeps it as `Path`)."""
    return next((k for k in env if k.upper() == "PATH"), "PATH")


# ---------------------------------------------------------------- running it
def _how_ended(code: int | None) -> str:
    if code is not None and code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = "a signal"
        return f"was killed by {name} (signal {-code})"
    return f"ended with exit code {code}"


Ran = tuple[int | None, str | None]  # (exit code, None past LIMIT_S; what to say of it)


def _command(words: list[str], cwd: Path, env: dict[str, str], log: Path) -> Ran:
    """The card's command, once."""
    with log.open("ab") as out:
        process = subprocess.Popen(  # noqa: S603 - the organisation's own command, here
            words, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT,
        )  # fmt: skip
        try:
            return process.wait(timeout=LIMIT_S), None
        except subprocess.TimeoutExpired:
            return None, None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _served(  # noqa: PLR0917 - the job's parts, passed on
    words: list[str], cwd: Path, env: dict[str, str], log: Path, job_id: str, job: Path
) -> Ran:
    """The card's `serve`, given the one job as the space's supervisor gives it: a line
    {"job", "folder"} on its stdin, until it writes {"done": job} on its stdout (anything
    else there goes to the log); then its stdin ends, as when the machine ends it."""
    env = {k: v for k, v in env.items() if k != "AMSYOS_JOB"}
    state = job.parent / "serve"
    state.mkdir()
    env["AMSYOS_SERVE_STATE"] = str(state / "job.json")
    if os.name != "nt":
        words = [sys.executable, "-c", _SERVE_SHIM, *words]
    lines: queue.Queue[bytes | None] = queue.Queue()
    done = False
    end = time.monotonic() + LIMIT_S
    with log.open("ab") as out:
        process = subprocess.Popen(  # noqa: S603 - the organisation's own server, here
            words, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=out,
        )  # fmt: skip
        given, said = process.stdin, process.stdout
        if given is None or said is None:
            raise OSError("the server's stdin and stdout were not given")

        def read() -> None:
            for line in iter(said.readline, b""):
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        try:
            given.write((json.dumps({"job": job_id, "folder": str(job)}) + "\n").encode())
            given.flush()
            while not done and (left := end - time.monotonic()) > 0:
                try:
                    line = lines.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    told: Any = json.loads(line)
                except ValueError:
                    told = None
                if isinstance(told, dict) and cast("dict[str, Any]", told).get("done") == job_id:
                    done = True
                else:
                    out.write(line)
                    out.flush()
            with contextlib.suppress(OSError):
                given.close()  # the machine ends the server: its stdin ends
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=STOP_S if done else 1)
        except OSError:
            pass  # it ended before it took its job: said below
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if done:
        return 0, None
    if time.monotonic() >= end:
        return None, None
    code = process.returncode
    return code or 1, (
        f'your server {_how_ended(code)} before it wrote {{"done": "{job_id}"}} on its stdout '
        "(from amsyos import serve; serve(handle) writes it)"
    )


def _tail(log: Path) -> list[str]:
    try:
        text = log.read_bytes()[-16000:].decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [_CONTROL.sub("?", line.rstrip())[:LINE_MAX] for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines[-TAIL_LINES:]


# ---------------------------------------------------------------- judging what it left
def _at(offset: int, *signatures: bytes) -> Callable[[bytes], bool]:
    return lambda head: any(head[offset : offset + len(s)] == s for s in signatures)


def _riff(kind: bytes) -> Callable[[bytes], bool]:
    return lambda head: head[:4] == b"RIFF" and head[8:12] == kind


def _mp3(head: bytes) -> bool:
    return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)


_ZIP = _at(0, b"PK\x03\x04", b"PK\x05\x06")
_ISO = _at(4, b"ftyp")
# kernel/conform.py MAGIC: what the first bytes of a file of each format are.
MAGIC: dict[str, Callable[[bytes], bool]] = {
    "png": _at(0, b"\x89PNG\r\n\x1a\n"), "jpg": _at(0, b"\xff\xd8\xff"),
    "jpeg": _at(0, b"\xff\xd8\xff"), "gif": _at(0, b"GIF87a", b"GIF89a"),
    "webp": _riff(b"WEBP"), "tif": _at(0, b"II*\x00", b"MM\x00*"),
    "tiff": _at(0, b"II*\x00", b"MM\x00*"), "psd": _at(0, b"8BPS"), "pdf": _at(0, b"%PDF-"),
    "mp4": _ISO, "m4a": _ISO, "mov": _at(4, b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"),
    "webm": _at(0, b"\x1a\x45\xdf\xa3"), "mkv": _at(0, b"\x1a\x45\xdf\xa3"),
    "wav": _riff(b"WAVE"), "mp3": _mp3, "ogg": _at(0, b"OggS"), "flac": _at(0, b"fLaC"),
    "zip": _ZIP, "xlsx": _ZIP, "docx": _ZIP, "pptx": _ZIP, "odt": _ZIP,
    "glb": _at(0, b"glTF"), "parquet": _at(0, b"PAR1"), "gz": _at(0, b"\x1f\x8b"),
    "tgz": _at(0, b"\x1f\x8b"), "tar": _at(257, b"ustar"),
}  # fmt: skip
JSON_FORMATS = frozenset({"json", "gltf", "geojson"})
TEXT_FORMATS = frozenset({"txt", "md", "csv", "srt", "vtt", "html", "obj", "bvh"})


def _seen_as(head: bytes) -> str:
    for fmt, matches in MAGIC.items():
        if fmt not in ("jpeg", "tiff", "m4a", "mov", "mkv", "tgz") and matches(head):
            return f"a {fmt} file"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "no format a check knows"
    return "text"


def sniff(path: Path, fmt: str, size: int) -> str | None:
    """None when the file's bytes are a `fmt` file; else what is wrong with it."""
    if size <= 0:
        return "is empty"
    with path.open("rb") as handle:
        head = handle.read(512)
        matches = MAGIC.get(fmt)
        if matches is not None:
            return None if matches(head) else f"is not a {fmt} file (it is {_seen_as(head)})"
        handle.seek(0)
        if fmt in JSON_FORMATS:
            try:
                json.loads(handle.read(16 * 1024 * 1024).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return f"is not valid JSON (it is {_seen_as(head)})"
            return None
        if fmt in TEXT_FORMATS:
            try:
                handle.read(1024 * 1024).decode("utf-8")
            except UnicodeDecodeError:
                return f"is not UTF-8 text (it is {_seen_as(head)})"
            return None if b"\x00" not in head else "is not text (it has NUL bytes)"
    return None  # a format the check does not read: its name is enough


def _look(report: object, path: str) -> object:
    """kernel/checks.py `_look`: a key, an index of a list, or that key of each item."""
    here = report
    for part in path.split("."):
        if isinstance(here, list):
            items = cast("list[object]", here)
            if part.isdigit():
                if int(part) >= len(items):
                    return _MISSING
                here = items[int(part)]
                continue
            here = [
                cast("dict[str, object]", e)[part]
                for e in items
                if isinstance(e, dict) and part in e
            ]
            continue
        if not isinstance(here, dict) or part not in here:
            return _MISSING
        here = cast("dict[str, object]", here)[part]
    return here


def _holds(found: object, op: str, wanted: object) -> bool:
    """kernel/checks.py `_holds`."""
    if op in ("empty", "not_empty"):
        nothing = found is _MISSING or found is None or found in ("", [], {})
        return nothing if op == "empty" else not nothing
    if op in ("has", "lacks"):
        items = cast("list[object]", found) if isinstance(found, list) else []
        said = str(wanted).casefold()
        has = any(e == wanted or (isinstance(e, str) and said in e.casefold()) for e in items)
        return has if op == "has" else not has
    if found is _MISSING:
        return False
    if op == "in":
        if isinstance(wanted, list):
            return found in cast("list[object]", wanted)
        return isinstance(wanted, str) and isinstance(found, str) and found in wanted
    if op == "contains":
        if isinstance(found, list):
            return wanted in cast("list[object]", found)
        if isinstance(found, dict):
            return isinstance(wanted, str) and wanted in found
        return isinstance(found, str) and isinstance(wanted, str) and wanted in found
    if op == "==":
        return found == wanted
    if op == "!=":
        return found != wanted
    if not (_number(found) and _number(wanted)):
        return False
    a, b = float(cast("float", found)), float(cast("float", wanted))
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}.get(op, False)


def _rules_said(rules: list[Any]) -> str:
    return "; ".join(
        " ".join([str(r[0]), str(r[1]), *([json.dumps(r[2])[:40]] if len(r) > 2 else [])])
        for r in (seq(x) for x in rules)
        if len(r) >= 2
    )[:120]


def _tests(
    card: dict[str, Any], made: dict[str, Path], why_not: str | None
) -> list[dict[str, Any]]:
    """Each test of the card, as the check of a version says it: `{name, output, check?,
    passed, found?, why?}`; `passed` None for a test that runs on AMSYOS only."""
    told: list[dict[str, Any]] = []
    for given in seq(card.get("tests")):
        test = obj(given)
        rules = seq(test.get("pass"))
        output, check = str(test.get("output")), test.get("check")
        row: dict[str, Any] = {"name": _rules_said(rules), "output": output}
        if check:
            row["check"] = check
        if why_not is not None:
            why = why_not.split("; ", 1)[0][:200]
            told.append({**row, "passed": False, "why": f"not run: {why}"})
            continue
        if check:
            told.append({**row, "passed": None, "why": f"runs on AMSYOS ({check}): deploy runs it"})
            continue
        path = made.get(output)
        if path is None:
            told.append({**row, "passed": False, "why": f"the example made no {output}"})
            continue
        try:
            report: object = json.loads(path.read_bytes()[:REPORT_MAX])
        except (OSError, ValueError):
            told.append({**row, "passed": False, "why": "its report is not JSON"})
            continue
        failed: list[str] = []
        found: dict[str, Any] = {}
        for rule in (seq(r) for r in rules):
            if len(rule) < 2:
                continue
            at, op = str(rule[0]), str(rule[1])
            wanted = rule[2] if len(rule) > 2 else None
            value = _look(report, at)
            found[at] = None if value is _MISSING else value
            if not _holds(value, op, wanted):
                had = "nothing" if value is _MISSING else json.dumps(value, default=str)
                value_said = "" if len(rule) < 3 else " " + json.dumps(wanted, default=str)
                failed.append(f"{at} {op}{value_said[:FOUND_MAX]}: found {had[:FOUND_MAX]}")
        row.update(passed=not failed, found=found)
        if failed:
            row["why"] = "; ".join(failed)
        told.append(row)
    return told


class Judged:
    """What the space and the check of a version say of a job's end."""

    def __init__(self) -> None:
        self.error: str | None = None
        self.hint: str | None = None
        self.files: list[dict[str, Any]] = []  # what out/ holds, with sizes
        self.made: dict[str, Path] = {}  # each declared output's file
        self.problems: list[str] = []  # the check's: each fails a version
        self.notes: list[str] = []  # what the space leaves aside, and why it may have


def _misfit(path: Path, fmt: str) -> str | None:
    """The file against the card's format, then its own name's (drivers/vast.py
    `_check_format`: a file named .jpg that holds a PNG fails as much)."""
    size = path.stat().st_size
    own = path.suffix[1:13].lower()
    known = MAGIC.keys() | JSON_FORMATS | TEXT_FORMATS
    for each in dict.fromkeys(f for f in (fmt, own) if f in known):
        wrong = sniff(path, each, size)
        if wrong:
            return wrong if each == fmt else f"(named .{own}) {wrong}"
    return None if size > 0 else "is empty"


def _judge(
    job: Path, ran: Ran, declared: dict[str, str], log: Path, code_dir: Path | None
) -> Judged:
    """The job's end as the space reads it (space_run.py `take_back`), then what the check
    of a version finds wrong with its outputs (kernel/checks.py `_judge`)."""
    told = Judged()
    out = job / "out"
    for path in sorted(out.iterdir()) if out.is_dir() else []:
        if path.is_dir() and not path.is_symlink():
            told.notes.append(f"out/{path.name}/ is a folder: the space takes files only")
            continue
        if path.is_symlink() or not path.is_file() or not FILE_NAME.fullmatch(path.name):
            told.notes.append(f"out/{path.name}: not taken (a link, or a name the space refuses)")
            continue
        row: dict[str, Any] = {"file": f"out/{path.name}", "size": path.stat().st_size}
        if path.stem in declared:
            row["output"] = path.stem
            told.made[path.stem] = path
        else:
            row["note"] = "not an output of the card"
        told.files.append(row)
    if code_dir is not None and (code_dir / "out").is_dir():
        wrote = sorted(p.name for p in (code_dir / "out").iterdir())[:4]
        if wrote:
            told.notes.append(
                f"it wrote out/{', out/'.join(wrote)} in the folder it ran in, not in "
                '$AMSYOS_JOB/out/: write job.output("<name>", ".<ext>")'
            )
    code, said = ran
    error_json = job / "error.json"
    if error_json.is_file():
        try:
            given = obj(json.loads(error_json.read_text(encoding="utf-8", errors="replace")))
            error = str(given.get("error") or "SERVICE_ERROR")
            hint = str(given.get("hint") or "The service reported an error.")
        except ValueError:
            error, hint = "SERVICE_ERROR", "The service wrote an error.json that is not JSON."
        known = ("INVALID_PARAMS", "SERVICE_ERROR", "OUT_OF_MEMORY", "TIMEOUT")
        told.error = error if error in known else "SERVICE_ERROR"
        told.hint = f"the service ended the job: {_CONTROL.sub('?', hint)[:480]}"
    elif code is None:
        told.error, told.hint = "TIMEOUT", f"it ran past {LIMIT_S // 60} minutes and was stopped"
    elif code != 0:
        last = next((line for line in reversed(_tail(log)) if line.strip()), "")
        told.error = "SERVICE_ERROR"
        told.hint = (said or f"your service {_how_ended(code)}") + (f": {last}" if last else "")
    elif not told.files:
        told.error = "SERVICE_ERROR"
        told.hint = (
            "your service ended without a file in $AMSYOS_JOB/out/ (one file for each output, "
            "named <output>.<ext>)"
        )
    if told.error is not None:
        return told
    there = ", ".join(str(f["file"])[4:] for f in told.files) or "nothing"
    for name, fmt in declared.items():
        path = told.made.get(name)
        if path is None:
            told.problems.append(
                f"outputs.{name}: no such file; write out/{name}.{fmt} "
                f'(job.output("{name}", ".{fmt}")); out/ has {there}'
            )
        elif wrong := _misfit(path, fmt):
            told.problems.append(f"outputs.{name} ({fmt}) {wrong}")
    return told


# ---------------------------------------------------------------- the command
def _card_for_here(card: dict[str, Any], given: dict[str, Any]) -> None:
    """What of the card stops a run here: how it runs, its inputs, outputs and tests, its
    example (an input given here stands for the example's). The rest (summary, version)
    is deploy's to refuse."""
    blocking = [
        p for p in cards.problems(card)
        if p.startswith(("command", "outputs", "inputs", "tests", "runtime"))
    ]  # fmt: skip
    blocking += [
        p for p in cards.example_problems(card)
        if p.split(":", 1)[0].removeprefix("example.") not in given
    ]  # fmt: skip
    if blocking:
        raise CliError(
            "INVALID_CARD",
            f"fix {cards.CARD}, then run --local again: " + "; ".join(blocking),
            {"fields": [p.split(":", 1)[0] for p in blocking]},
        )


def _lines(answer: dict[str, Any], judged: Judged, serving: bool) -> list[str]:
    said = f"ran here in {answer['seconds']:.1f}s: {answer['command']}"
    lines = [said + (" (as a server: serve)" if serving else "")]
    if judged.error:
        lines.append(f"failed: {judged.error}: {judged.hint}")
    lines += ["outputs:"] if judged.files else []
    for row in judged.files:
        mark = row.get("output") or row.get("note") or ""
        lines.append(f"  {row['file']}  {human(int(row['size']))}  {mark}".rstrip())
    lines += [f"problem: {p}" for p in judged.problems]
    lines += [f"note: {n}" for n in judged.notes]
    for test in answer["tests"]:
        state = {True: "passed", False: "failed", None: "not run here"}[test.get("passed")]
        on = f"{test['output']}" + (f", {test['check']}" if test.get("check") else "")
        lines.append(
            f"test {state}: {test['name']} ({on})" + (f": {test['why']}" if test.get("why") else "")
        )
    tail = [str(line) for line in seq(answer.get("log_tail"))]
    if tail:
        lines.append(f"the program's last {len(tail)} lines:")
        lines += [f"  {line}" for line in tail]
    if answer["passed"]:
        lines.append("passed, as deploy's check would judge it; nothing was sent to AMSYOS")
    else:
        lines.append("failed: deploy's check would fail the same way; fix it and run --local again")
    return lines


def run_here(
    root: Path,
    out: Out,
    *,
    named: str | None = None,
    given: dict[str, Any] | None = None,
    files: dict[str, Path] | None = None,
    keep: Path | None = None,
    force: bool = False,
) -> int:
    """`amsyos run --local`: 0 when the job succeeded and the check and its tests pass, 1
    when not; a card or params it cannot run with are an error before anything starts.
    `keep`: the folder the job's folder is kept in, as `<keep>/<job>/`."""
    root = root.resolve()
    card = cards.load(root)
    service = str(card.get("service") or "")
    asked = (named or "").split("@", 1)[0]
    if asked and asked != service and not asked.endswith(f".{service}"):
        raise CliError(
            "INVALID_INPUT",
            f"run --local runs this repository's own service ({service or 'none'}, from "
            f"{cards.CARD}); leave the name out, or run it on AMSYOS: amsyos run {named} …",
        )
    # The owner, 2026-11-20: this machine is checked against the card's resources first;
    # one not strong enough sends the work to the space (a rented machine) instead.
    here = machines.probe(Path(tempfile.gettempdir()))
    judged_here = machines.fits(card, here)
    if not judged_here["fits"] and not force:
        raise CliError(
            "MACHINE_TOO_SMALL",
            machines.too_small_hint(service, judged_here),
            {"machine": here, "lacks": judged_here["lacks"], "space": judged_here["space"]},
        )
    for note in judged_here["notes"]:
        out.note(note)
    given = given or {}
    _card_for_here(card, given)
    params, sources, fixed = _params(card, root, given, files or {})
    # Each output's format in this call: an input may choose it (`controls`).
    declared = cards.chosen_formats(card, params)
    for line in fixed:
        out.note(line)
    sdk = sdk_dir()
    serving = not card.get("command")
    said = str(card.get("serve") if serving else card.get("command"))
    try:
        words = shlex.split(said)
    except ValueError as exc:
        raise CliError("INVALID_CARD", f"{cards.CARD}: its command does not read: {exc}") from None
    if not words:
        raise CliError("INVALID_CARD", f"{cards.CARD}: command (started once a job) is empty")
    job_id = f"j_local_{secrets.token_hex(6)}"
    base = _base(job_id, keep)
    try:
        job = _job_folder(base, job_id, card, params, sources)
        code_dir, where = _code_folder(base, root, card)
        bin_dir = _bin(base, sdk)
        python_bin: Path | None = None
        runtime = ""
        if words[0] in ("python", "python3") and card.get("runtime", "python") == "python":
            python, whose = _python(root)
            words[0] = python
            python_bin = Path(python).parent
            version = _version_of(python)
            runtime = f"Python {version or '?'} from {whose}"
            if version and version != SPACE_PYTHON:
                runtime += f" (the space runs {SPACE_PYTHON})"
        env = _env(job, sdk, bin_dir, card, python_bin)
        if not Path(words[0]).is_absolute() and "/" not in words[0]:
            words[0] = shutil.which(words[0], path=env[path_key(env)]) or words[0]
        log = base / "job.log"
        out.note(f"running {said} {where}" + (f"; {runtime}" if runtime else ""))
        began = time.monotonic()
        try:
            ran = (
                _served(words, code_dir, env, log, job_id, job)
                if serving
                else _command(words, code_dir, env, log)
            )
        except OSError as exc:
            ran = 127, f"the command could not be started: {exc}"
        seconds = time.monotonic() - began
        judged = _judge(job, ran, declared, log, code_dir if code_dir != root else None)
        why_not = judged.hint if judged.error else next(iter(judged.problems), None)
        tests = _tests(card, judged.made, why_not)
        passed = judged.error is None and not judged.problems
        passed = passed and not any(t.get("passed") is False for t in tests)
        answer: dict[str, Any] = {
            "local": True, "passed": passed, "seconds": round(seconds, 2), "command": said,
            "ran": where, "job": job_id, "outputs": judged.files, "problems": judged.problems,
            "notes": judged.notes, "tests": tests, "machine": here,
        }  # fmt: skip
        if runtime:
            answer["python"] = runtime
        if judged.error:
            answer.update(error=judged.error, hint=judged.hint)
        if not passed:
            answer["log_tail"] = _tail(log)
        lines = _lines(answer, judged, serving)
        if judged.error and answer["log_tail"] and code_dir != root:
            lines.insert(
                -1,
                "(it ran in a copy of what deploy packs: a file .gitignore leaves out is not "
                "there, as on the space)",
            )
        if keep is not None:
            answer["folder"] = str(base)
            lines.append(
                f"kept: {base} (job/ is $AMSYOS_JOB, job/out/ its outputs, job.log what it printed)"
            )
            lines.append(f"again by hand: {_by_hand(job, sdk, code_dir, words)}")
        elif judged.files:
            lines.append("its files go with the job's folder: --keep keeps them in out/<job>/")
        out.emit(answer, *lines)
        return 0 if passed else 1
    finally:
        if keep is None:
            shutil.rmtree(base, ignore_errors=True)


def _by_hand(job: Path, sdk: Path, code: Path, words: list[str]) -> str:
    command = " ".join(shlex.quote(w) for w in words)
    if os.name == "nt":
        return (
            f"cd '{code}'; $env:AMSYOS_JOB='{job}'; $env:PYTHONPATH='{sdk}'; "
            f"$env:PYTHONUTF8='1'; {command}"
        )
    return (
        f"cd {shlex.quote(str(code))} && AMSYOS_JOB={shlex.quote(str(job))} "
        f"PYTHONPATH={shlex.quote(str(sdk))} {command}"
    )
