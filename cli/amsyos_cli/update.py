"""Install and update the `amsyos` command: one layout and one code path on every OS.

A customer (Windows, Claude Code and Codex), 2026-11-20 and 21: `uv tool install --force`
replaced the tool's folder in place and failed with os error 32 while anything ran from it;
a detached updater was killed with the agent host's job. So nothing here replaces a folder
in use, and nothing runs after this command ends.

The layout, under AMSYOS_HOME (`~/.local/share/amsyos`; on Windows `%LOCALAPPDATA%\\amsyos`;
or `$AMSYOS_HOME`):
- `bin/amsyos` (sh) and `bin/amsyos.cmd`: static shims that run
  `versions/<current>/<python> -m amsyos_cli`. `install` writes them; an update never does.
- `versions/<v>/`: one virtual environment a version; `.ok` once it passed its smoke test;
  `.in-use`, held open by every process that runs that version (entry.py `hold`), so that
  a version in use is never deleted.
- `current`: one line, the version the shims run; `previous`: the one before it (--rollback).
- `state/update-check.json`: when the newest version was last asked (at most once a day).

`amsyos install` (run once with `uvx --from <wheel URL> amsyos install`) makes the layout,
installs the newest version and puts `bin` on PATH. `amsyos self-update` runs in the
foreground, in this process: latest.json (its sha256 checked), a new virtual environment,
the wheel, a smoke test, then `current` replaced in one rename. A failed step leaves
`current` as it was. Old versions not in use are deleted; the one before is kept.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from amsyos_cli import __version__
from amsyos_cli.common import (
    DEFAULT_GATEWAY,
    CliError,
    Out,
    credentials_path,
    gateway_of,
    obj,
    seq,
)

CHECK_EVERY_S = 24 * 3600  # the newest version is asked at most once a day
CHECK_TIMEOUT_S = 3.0  # a command never waits longer than this for the daily check
NO_CHECK = "AMSYOS_NO_UPDATE_CHECK"  # set: no daily check (the smoke test sets it)
OK = ".ok"
IN_USE = ".in-use"
SMOKE_TIMEOUT_S = 120
TOOL = "amsyos-cli"  # the `uv tool` install of 0.3.12 and older


def windows() -> bool:
    return sys.platform == "win32"


def numbers(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit())


def home() -> Path:
    told = os.environ.get("AMSYOS_HOME")
    if told:
        return Path(told).expanduser()
    if windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "amsyos"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "amsyos"


def python_of(version_dir: Path) -> Path:
    """A version's Python, where uv makes it on this OS."""
    return version_dir / ("Scripts/python.exe" if windows() else "bin/python")


# ---------------------------------------------------------------- the shims
SH = """#!/bin/sh
# AMSYOS's command: runs the version named in ../current (written by `amsyos install`;
# an update never rewrites this file).
h="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
v="$(cat "$h/current" 2>/dev/null)"
p="$h/versions/$v/bin/python"
[ -x "$p" ] || p="$h/versions/$v/Scripts/python.exe"
if [ -z "$v" ] || [ ! -x "$p" ]; then
  echo "amsyos: no installed version in $h: uvx --from <wheel URL> amsyos install" >&2
  exit 127
fi
exec "$p" -m amsyos_cli "$@"
"""

CMD = """@echo off
rem AMSYOS's command: runs the version named in ..\\current (written by `amsyos install`;
rem an update never rewrites this file).
setlocal
set "AMSYOS_SHIM_HOME=%~dp0.."
set "AMSYOS_SHIM_V="
set /p AMSYOS_SHIM_V=<"%AMSYOS_SHIM_HOME%\\current"
if not defined AMSYOS_SHIM_V goto none
if not exist "%AMSYOS_SHIM_HOME%\\versions\\%AMSYOS_SHIM_V%\\Scripts\\python.exe" goto none
rem The version's python.exe is uv's launcher: once Python has started, it moves its own
rem working folder to TMP and warns Failed to set cwd to temp dir where it may not enter
rem it. TMP names this folder for it; amsyos puts the one before back as it starts.
set "AMSYOS_SHIM_TMP=+%TMP%"
set "TMP=%~dp0"
"%AMSYOS_SHIM_HOME%\\versions\\%AMSYOS_SHIM_V%\\Scripts\\python.exe" -m amsyos_cli %*
exit /b %ERRORLEVEL%
:none
echo amsyos: no version installed in %AMSYOS_SHIM_HOME%: run its install again 1>&2
exit /b 127
"""


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def versions(self) -> Path:
        return self.root / "versions"

    def dir(self, version: str) -> Path:
        return self.versions / version

    def _read(self, name: str) -> str | None:
        try:
            text = (self.root / name).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def current(self) -> str | None:
        return self._read("current")

    def previous(self) -> str | None:
        return self._read("previous")

    def ok(self, version: str | None) -> bool:
        """A version installed whole: its Python is there and it passed its smoke test."""
        if not version:
            return False
        where = self.dir(version)
        return (where / OK).is_file() and python_of(where).exists()

    def installed(self) -> list[str]:
        if not self.versions.is_dir():
            return []
        found = [p.name for p in self.versions.iterdir() if p.is_dir() and self.ok(p.name)]
        return sorted(found, key=numbers)

    def shims(self) -> list[str]:
        """The shims, written where they are missing or differ; they name no version."""
        self.bin.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for name, text in (("amsyos", SH), ("amsyos.cmd", CMD.replace("\n", "\r\n"))):
            path = self.bin / name
            with contextlib.suppress(OSError):
                if path.read_bytes() == text.encode("utf-8"):
                    continue
            path.write_bytes(text.encode("utf-8"))
            if name == "amsyos":
                path.chmod(0o755)
            written.append(str(path))
        return written


# ---------------------------------------------------------------- latest.json
def gateway() -> str:
    """AMSYOS_GATEWAY, else the Gateway of the saved login, else AMSYOS's own."""
    told = os.environ.get("AMSYOS_GATEWAY")
    if told:
        return gateway_of(told)
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        return gateway_of(str(obj(data).get("gateway") or DEFAULT_GATEWAY))
    except (OSError, ValueError, CliError):
        return DEFAULT_GATEWAY


def latest(where: str, timeout: float = 20.0) -> dict[str, Any]:
    try:
        answer = httpx.get(f"{where}/cli/latest.json", timeout=timeout)
    except httpx.HTTPError as exc:
        raise CliError("UNREACHABLE", f"cannot reach {where}: {exc}") from None
    if answer.status_code != 200:
        raise CliError(
            "NOT_FOUND",
            f"{where}/cli/latest.json answered {answer.status_code}: this Gateway publishes "
            f"no newest wheel; see {where}/cli",
        )
    told = obj(answer.json())
    if not told.get("version") or not told.get("wheel"):
        raise CliError("NOT_FOUND", f"{where}/cli/latest.json names no wheel")
    return told


def install_line(told: dict[str, Any]) -> str:
    return str(told.get("install") or f"uvx --from {told.get('wheel')} amsyos install")


# ---------------------------------------------------------------- one version
def _uv() -> str:
    """uv: the one that runs us (`uvx` sets UV), else the one on PATH."""
    told = os.environ.get("UV")
    if told and Path(told).is_file():
        return told
    found = shutil.which("uv")
    if found is None:
        raise CliError(
            "NOT_FOUND",
            "uv is not on PATH: install it (https://docs.astral.sh/uv/), then run this again",
        )
    return found


def _run(argv: list[str], what: str, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    try:
        done = subprocess.run(  # noqa: S603 - uv or a version's own Python, fixed arguments
            argv, capture_output=True, text=True, errors="replace", timeout=timeout,
            check=False, env={**os.environ, NO_CHECK: "1"},
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError("UPDATE_FAILED", f"{what} could not run: {exc}") from None
    if done.returncode != 0:
        said = " ".join((done.stderr or done.stdout or "").split())[-600:]
        raise CliError("UPDATE_FAILED", f"{what} failed (exit {done.returncode}): {said}")
    return done


def _download(url: str, sha256: str, into: Path) -> Path:
    """The wheel, under its own file name (uv reads the version from it), its sha256
    checked against latest.json's before anything is installed."""
    path = into / url.rsplit("/", 1)[-1]
    digest = hashlib.sha256()
    try:
        with httpx.stream("GET", url, timeout=60.0) as answer:
            if answer.status_code != 200:
                raise CliError("UPDATE_FAILED", f"{url} answered {answer.status_code}")
            with path.open("wb") as sink:
                for chunk in answer.iter_bytes():
                    digest.update(chunk)
                    sink.write(chunk)
    except httpx.HTTPError as exc:
        raise CliError("UNREACHABLE", f"cannot download {url}: {exc}") from None
    if not sha256:
        raise CliError(
            "UPDATE_BAD_HASH",
            f"latest.json gives no sha256 for {path.name} (this Gateway is older than the "
            "CLI; tell AMSYOS's team: amsyos feedback)",
        )
    if digest.hexdigest() != sha256.lower():
        raise CliError(
            "UPDATE_BAD_HASH",
            f"{path.name}: its sha256 is {digest.hexdigest()}, latest.json says {sha256}: "
            "try again later, or tell AMSYOS's team (amsyos feedback)",
        )
    return path


def _rmtree(path: Path) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def install_version(layout: Layout, told: dict[str, Any]) -> str:
    """`versions/<v>` made whole from latest.json's wheel; `current` is not touched. A
    version without `.ok` is a failed try (killed, a step failed): never current, made
    again."""
    version, url = str(told["version"]), str(told["wheel"])
    if layout.ok(version):
        return version
    where = layout.dir(version)
    if where.exists() and not _rmtree(where):
        raise CliError(
            "UPDATE_FAILED",
            f"{where} is left from a failed install and cannot be removed: close what uses "
            "it, then run this again",
        )
    layout.versions.mkdir(parents=True, exist_ok=True)
    uv = _uv()
    with tempfile.TemporaryDirectory(prefix="amsyos-wheel-") as scratch:
        try:
            wheel = _download(url, str(told.get("sha256") or ""), Path(scratch))
            py = f"{sys.version_info.major}.{sys.version_info.minor}"
            _run([uv, "venv", str(where), "--python", py, "--quiet"], "uv venv")
            _run(
                [uv, "pip", "install", "--python", str(python_of(where)), str(wheel), "--quiet"],
                "uv pip install",
            )
            _smoke(where, version)
        except CliError as exc:
            gone = _rmtree(where)
            raise CliError(exc.code, f"{exc.hint}; {_unchanged(layout, version, gone)}",
                           exc.extra) from None  # fmt: skip
        except BaseException:
            _rmtree(where)
            raise
    (where / OK).write_text(f"{version}\n", encoding="utf-8")
    return version


def _unchanged(layout: Layout, version: str, gone: bool) -> str:
    """What a failed install left, said after the step's own error (a customer's feedback: the
    state was right, but the message ended at uv's error, so nobody could tell whether
    amsyos still worked without checking)."""
    current = layout.current()
    if current and layout.ok(current):
        said = f"nothing was switched: amsyos still runs {current}"
    else:
        said = "nothing was installed"
    if gone:
        return f"{said}, and no part of {version} was left in {layout.versions}"
    return (
        f"{said}; {layout.dir(version)} is left half made and could not be removed: close "
        "what uses it and delete it (the next try makes it again)"
    )


def _smoke(where: Path, version: str) -> None:
    done = _run([str(python_of(where)), "-m", "amsyos_cli", "--version"], "the smoke test",
                timeout=SMOKE_TIMEOUT_S)  # fmt: skip
    said = (done.stdout or "").strip()
    if said != f"amsyos {version}":  # install_version says what was left
        raise CliError("UPDATE_FAILED", f"the smoke test of {version} said {said!r}")


def switch(layout: Layout, version: str) -> str | None:
    """`current` names `version`, replaced in one rename; `previous` the one before."""
    before = layout.current()
    if before and before != version:
        _write(layout.root / "previous", before)
    _write(layout.root / "current", version)
    return before


def _write(path: Path, text: str) -> None:
    """A file replaced in one rename. On Windows a shim may be reading it at that very
    moment: a few tries."""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")  # one line, no newline: `set /p` reads it whole
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


def in_use(version_dir: Path) -> bool:
    """A process runs this version: it holds `.in-use` open (entry.py `hold`). POSIX: by
    its shared lock; Windows: an open file cannot be deleted."""
    marker = version_dir / IN_USE
    if not marker.exists():
        return False
    if windows():
        try:
            marker.unlink()
        except PermissionError:
            return True
        except OSError:
            return False
        return False
    import fcntl  # noqa: PLC0415 - POSIX only

    try:
        with marker.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def prune(layout: Layout) -> tuple[list[str], list[str]]:
    """Every version but `current` and `previous` deleted, unless a process runs it:
    (deleted, kept because in use)."""
    keep = {layout.current(), layout.previous()}
    gone: list[str] = []
    busy: list[str] = []
    if not layout.versions.is_dir():
        return gone, busy
    for where in sorted(layout.versions.iterdir()):
        if not where.is_dir() or where.name in keep:
            continue
        if in_use(where):
            busy.append(where.name)
            continue
        (gone if _rmtree(where) else busy).append(where.name)
    return gone, busy


# ---------------------------------------------------------------- PATH
MARK = "# added by amsyos install"


def on_path(where: Path) -> bool:
    wanted = os.path.normcase(str(where))
    return any(
        os.path.normcase(p.rstrip("/\\")) == wanted
        for p in os.environ.get("PATH", "").split(os.pathsep)
    )


def add_to_path(where: Path) -> list[str]:
    """`bin` first on the user's PATH for new shells: Windows' user Path (the registry),
    else a line in the shell's start files. What was changed."""
    if windows():
        return _windows_path(where)
    line = f'export PATH="{where}:$PATH"  {MARK}'
    files = [Path.home() / ".profile"]
    shell = os.environ.get("SHELL", "")
    for name, wanted in ((".bashrc", "bash"), (".zshrc", "zsh")):
        path = Path.home() / name
        if path.exists() or shell.endswith(wanted):
            files.append(path)
    changed: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            continue
        if MARK in text:
            continue
        with path.open("a", encoding="utf-8") as sink:
            sink.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")
        changed.append(str(path))
    return changed


def _windows_path(where: Path) -> list[str]:
    # Windows only, read as Any: the type checker runs on Linux, where winreg has no names.
    reg: Any = importlib.import_module("winreg")
    with reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment", 0, reg.KEY_READ | reg.KEY_WRITE) as key:
        try:
            value, kind = reg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", reg.REG_EXPAND_SZ
        parts = [p for p in str(value).split(";") if p]
        wanted = os.path.normcase(str(where))
        if any(os.path.normcase(os.path.expandvars(p)).rstrip("\\") == wanted for p in parts):
            return []
        reg.SetValueEx(key, "Path", 0, kind, ";".join([str(where), *parts]))
    with contextlib.suppress(Exception):  # tell running programs the environment changed
        user32: Any = importlib.import_module("ctypes").windll.user32
        user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
    return ["the user Path (HKCU\\Environment)"]


# ---------------------------------------------------------------- the daily check
def checked(layout: Layout, *, force: bool = False) -> dict[str, Any] | None:
    """The newest version and min_version, asked at most once a day (a short timeout; a
    failure is no answer), kept in state/update-check.json."""
    state = layout.root / "state" / "update-check.json"
    with contextlib.suppress(OSError, ValueError, TypeError):
        kept = obj(json.loads(state.read_text(encoding="utf-8")))
        if not force and time.time() - float(kept.get("checked_at") or 0) < CHECK_EVERY_S:
            return kept
    try:
        told = latest(gateway(), timeout=CHECK_TIMEOUT_S)
    except CliError:
        return None
    kept = {
        "checked_at": time.time(), "newest": str(told["version"]),
        "min_version": str(told.get("min_version") or ""), "install": install_line(told),
    }  # fmt: skip
    with contextlib.suppress(OSError):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(kept), encoding="utf-8")
    return kept


# A customer's feedback (2026-11-21): an agent in auto mode was refused `amsyos self-update`
# by its host's permission check, as replacing its own tools. So the notice names the one
# command for the person, and what waiting costs: AMSYOS's own fixes are on its server and
# reach every version at once; only fixes in this command wait for the update.
FOR_PERSON = "if your agent's host refuses it, the person runs it in a terminal"
MISSES = (
    "until then only fixes in the amsyos command itself wait; AMSYOS's server-side fixes "
    "need no update"
)


def update_lines(told: dict[str, Any]) -> list[str]:
    """A notice (`notice`) in two lines: the version and the one command, then who runs it
    and what staying on this version misses."""
    return [
        f"update: {told['newest']} available (this is {told['installed']}): {told['command']}",
        f"  {FOR_PERSON}; {MISSES}",
    ]


def notice(command: str | None) -> dict[str, Any] | None:
    """For each command (main.py): `{installed, newest, command, who, misses}` when a newer
    version is out, from the daily check; None when current, when told not to, for the
    update commands themselves, or when the check fails."""
    if os.environ.get(NO_CHECK) or command in {"self-update", "install", None}:
        return None
    layout = Layout(home())
    kept = checked(layout)
    if not kept or numbers(str(kept.get("newest") or "")) <= numbers(__version__):
        return None
    how = "amsyos self-update" if layout.ok(__version__) else str(kept.get("install"))
    return {
        "installed": __version__, "newest": kept["newest"], "command": how,
        "who": FOR_PERSON, "misses": MISSES,
    }  # fmt: skip


# ---------------------------------------------------------------- the commands
def install(out: Out, *, path: bool = True, keep_old: bool = False) -> int:
    """`uvx --from <wheel URL> amsyos install`: the layout, the newest version, PATH, and
    the `uv tool` install of 0.3.12 and older removed."""
    layout = Layout(home())
    told = latest(gateway())
    layout.root.mkdir(parents=True, exist_ok=True)
    written = layout.shims()
    version = install_version(layout, told)
    before = switch(layout, version)
    gone, busy = prune(layout)
    changed = add_to_path(layout.bin) if path else []
    old = None if keep_old else remove_old_tool()
    # A customer, 2026-11-21: the Claude app, and the shells and hooks it runs, kept the PATH
    # they started with and found no `amsyos` until restarted. A forwarder in a folder
    # already on that PATH (uv's tool bin, where the old `amsyos` was) works at once.
    # Written whatever this process's PATH says (a customer's feedback: an agent had put `bin`
    # on its own PATH by hand to run install, and none were written though the app's
    # shells still lacked it); harmless where `bin` is on PATH too, since it comes first.
    forwarders = forward(layout) if path else []
    answer: dict[str, Any] = {
        "home": str(layout.root), "version": version, "before": before, "shims": written,
        "path_changed": changed, "forwarders": forwarders, "removed": gone,
        "kept_in_use": busy, "old_tool": old,
    }  # fmt: skip
    lines = [f"amsyos {version} installed in {layout.root}"]
    if changed:
        lines.append(f"  {layout.bin} added to PATH ({', '.join(changed)})")
    elif not path:
        lines.append(f"  PATH not changed: put {layout.bin} first on it yourself")
    if forwarders:
        lines.append(
            f"  {', '.join(forwarders)}: forwards to it, so terminals and apps already open "
            "find amsyos now"
        )
    if old:
        lines += [f"  the old uv tool install removed: {p}" for p in old["removed"]]
        lines += [f"  in use, not removed: {p} (close what runs it; amsyos install again "
                  "removes it)" for p in old["left"]]  # fmt: skip
    lines.append("Next:")
    if not on_path(layout.bin) and not forwarders:
        lines.append(
            "  restart the Claude app (or Codex) and open terminals: they read PATH when they "
            "start; then: amsyos --version"
        )
    if credentials_path().is_file():
        lines.append("  already logged in: amsyos whoami says who")
    else:
        lines.append("  amsyos login (once)")
    lines.append("  later, to update: amsyos self-update")
    out.emit(answer, *lines)
    return 0


def _uv_dirs() -> tuple[Path | None, Path | None]:
    """uv's tool folder and its bin folder (`uv tool dir [--bin]`)."""
    found: list[Path | None] = []
    for extra in ([], ["--bin"]):
        try:
            told = subprocess.run(  # noqa: S603 - uv, fixed arguments
                [_uv(), "tool", "dir", *extra], capture_output=True, text=True, timeout=20,
                check=False,
            )  # fmt: skip
        except (CliError, OSError, subprocess.SubprocessError):
            found.append(None)
            continue
        said = told.stdout.strip()
        found.append(Path(said) if told.returncode == 0 and said else None)
    return found[0], found[1]


def _aside(path: Path, stamp: str) -> Path | None:
    """`path` renamed beside itself (`<name>.old-<stamp>`); None when it cannot be."""
    aside = path.with_name(f"{path.name}.old-{stamp}")
    try:
        os.replace(path, aside)
    except OSError:
        return None
    return aside


def remove_old_tool() -> dict[str, list[str]] | None:
    """The `uv tool` install of 0.3.12 and older removed, the way that works on Windows.
    A customer, 2026-11-21: `uv tool uninstall amsyos-cli` failed twice with os error 32 on a
    handle to the folder itself, renaming the folder first worked, and `~\\.local\\bin\\amsyos.exe`
    stayed behind. So each entry point uv's receipt names for amsyos-cli, then the tool's
    folder, is renamed aside and deleted; what is in use is left and said. Leftovers of an
    earlier try go too. None when there is no old install."""
    tools, _bin = _uv_dirs()
    if tools is None:
        return None
    folder = tools / TOOL
    leftovers = [p for p in tools.glob(f"{TOOL}.old-*") if p.is_dir()]
    if not folder.is_dir() and not leftovers:
        return None
    removed: list[str] = []
    left: list[str] = []
    stamp = time.strftime("%Y%m%d%H%M%S")
    for entry in _entry_points(folder):
        for leftover in entry.parent.glob(f"{entry.name}.old-*"):
            with contextlib.suppress(OSError):
                leftover.unlink()
        if not entry.exists() and not entry.is_symlink():
            continue
        aside = _aside(entry, stamp)  # a running .exe can be renamed, not deleted
        if aside is None:
            left.append(str(entry))
            continue
        try:
            aside.unlink()
        except OSError:
            left.append(f"{aside} (renamed; deleted by the next install)")
        removed.append(str(entry))
    if folder.is_dir():
        aside = _aside(folder, stamp)
        if aside is None:
            left.append(str(folder))
        else:
            (removed if _rmtree(aside) else left).append(str(folder))
    for leftover in leftovers:
        _rmtree(leftover)
    return {"removed": removed, "left": left}


def _entry_points(folder: Path) -> list[Path]:
    """The commands uv installed for amsyos-cli, from its receipt; `amsyos` only."""
    import tomllib  # noqa: PLC0415 - only for a move from 0.3.12

    try:
        receipt = tomllib.loads((folder / "uv-receipt.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tool = obj(receipt.get("tool"))
    found: list[Path] = []
    for raw in seq(tool.get("entrypoints")):
        entry = obj(raw)
        where = str(entry.get("install-path") or "")
        if entry.get("name") == "amsyos" and entry.get("from") == TOOL and where:
            found.append(Path(where))
    return found


def _sh_quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def forward(layout: Layout) -> list[str]:
    """Forwarders to the shims in uv's tool bin, the folder the old `amsyos` was in and so
    on the PATH of programs started before the install, so that they find `amsyos`; they
    name AMSYOS_HOME, never a version, and an update never rewrites them. Written whenever
    that folder exists and holds no other `amsyos`, whatever this process's PATH says."""
    _tools, where = _uv_dirs()
    if where is None or not where.is_dir():
        return []
    sh = (
        "#!/bin/sh\n# Forwards to AMSYOS's command (written by `amsyos install`).\n"
        f'exec {_sh_quoted(str(layout.bin / "amsyos"))} "$@"\n'
    )
    cmd = (
        "@echo off\r\nrem Forwards to AMSYOS's command (written by `amsyos install`).\r\n"
        f'call "{layout.bin / "amsyos.cmd"}" %*\r\nexit /b %ERRORLEVEL%\r\n'
    )
    written: list[str] = []
    for name, text in (("amsyos", sh), ("amsyos.cmd", cmd)):
        path = where / name
        if path.exists() or path.is_symlink():
            with contextlib.suppress(OSError):
                if path.read_bytes() == text.encode("utf-8"):
                    written.append(str(path))
            continue  # another amsyos is there (an old install still in use): left as it is
        try:
            path.write_bytes(text.encode("utf-8"))
            if name == "amsyos":
                path.chmod(0o755)
        except OSError:
            continue
        written.append(str(path))
    return written


def self_update(
    out: Out, *, plan: bool = False, rollback: bool = False, if_needed: bool = False,
    check: bool = False, force: bool = False,
) -> int:  # fmt: skip
    """`amsyos self-update`, in the foreground: see the module's docstring."""
    layout = Layout(home())
    if rollback:
        return _rollback(out, layout)
    current = layout.current() or ""
    if not layout.ok(current):
        how = "uvx --from <the wheel's URL> amsyos install"
        with contextlib.suppress(CliError):
            how = install_line(latest(gateway(), timeout=CHECK_TIMEOUT_S))
        raise CliError(
            "NOT_INSTALLED",
            f"amsyos is not installed in {layout.root} yet (an install of 0.3.12 or older, "
            f"or a run through uvx): install it once with {how} (it removes the old one "
            "too)",
        )
    if if_needed and not _needed(layout, current):
        return 0  # silent when current (the SessionStart hook)
    told = latest(gateway())
    version = str(told["version"])
    newer = numbers(version) > numbers(current)
    answer: dict[str, Any] = {
        "current": current,
        "newest": version,
        "update": newer,
        "home": str(layout.root),
    }
    # Before it runs: the one command for the person, and what waiting misses (`FOR_PERSON`).
    due = {"command": "amsyos self-update", "who": FOR_PERSON, "misses": MISSES} if newer else {}
    if check:
        said = [
            f"amsyos {current}; the newest is {version}: amsyos self-update installs it",
            f"  {FOR_PERSON}; {MISSES}",
        ]
        out.emit({**answer, **due}, *(said if newer else [f"amsyos {current} is the newest"]))
        return 0
    doing = newer or force
    if plan:
        steps = _steps(layout, told) if doing else []
        lines = [f"{i}. {s}" for i, s in enumerate(steps, 1)] or [f"amsyos {current} is the newest"]
        if newer:
            lines.append(f"run: amsyos self-update ({FOR_PERSON}; {MISSES})")
        out.emit({**answer, **due, "steps": steps, "done": False}, *lines)
        return 0
    if not doing:
        if not if_needed:  # --if-needed: silent when nothing newer is out to install
            out.emit(answer, f"amsyos {current} is the newest")
        return 0
    if version == current:  # --force on the newest: the running version is not rebuilt
        out.emit(
            {**answer, "now": version},
            f"amsyos {version} is installed, checked and current: nothing to do",
        )
        return 0
    install_version(layout, told)
    before = switch(layout, version)
    gone, busy = prune(layout)
    kept = [v for v in layout.installed() if v != version]
    answer |= {"now": version, "before": before, "kept": kept, "removed": gone,
               "kept_in_use": busy}  # fmt: skip
    line = f"now runs {version} ({', '.join(kept) or 'none'} kept)"
    if busy:
        line += f"; in use now, deleted by a later update: {', '.join(busy)}"
    out.emit(answer, line)
    return 0


def _needed(layout: Layout, current: str) -> bool:
    """--if-needed: a newer version out, or this one below the Gateway's min_version, as
    the daily check says (no network when it asked in the last day)."""
    kept = checked(layout)
    if not kept:
        return False
    newest, least = str(kept.get("newest") or ""), str(kept.get("min_version") or "")
    return numbers(newest) > numbers(current) or numbers(current) < numbers(least)


def _steps(layout: Layout, told: dict[str, Any]) -> list[str]:
    """The steps self-update would take, only those it would take (a customer: `--plan
    --force` on the newest listed a download and an install that never happen)."""
    version = str(told["version"])
    where = layout.dir(version)
    current = layout.current()
    old = [v for v in layout.installed() if v not in {version, current}]
    if version == current:
        return [f"none: amsyos {version} is installed, checked and current"]
    if layout.ok(version):
        return [
            f"{where} is installed and checked already: nothing to download or install",
            f"write {layout.root / 'current.tmp'} and rename it to current ({version}); "
            f"previous: {current}",
            f"delete the versions not in use: {', '.join(old) or 'none'}",
        ]
    return [
        f"download {told['wheel']} and check its sha256 ({told.get('sha256') or 'none given'})",
        f"uv venv {where}",
        f"uv pip install the wheel into {where}",
        f"smoke test: {python_of(where)} -m amsyos_cli --version says amsyos {version}",
        f"write {layout.root / 'current.tmp'} and rename it to current ({version}); "
        f"previous: {current}",
        f"delete the versions not in use: {', '.join(old) or 'none'}",
    ]


def _rollback(out: Out, layout: Layout) -> int:
    back, now = layout.previous(), layout.current()
    if back is None or not layout.ok(back) or back == now:
        raise CliError(
            "NOTHING_TO_ROLL_BACK",
            f"no earlier version is kept in {layout.versions} (installed: "
            f"{', '.join(layout.installed()) or 'none'})",
        )
    switch(layout, back)
    # --rollback toggles: the second call moves forward again (a customer's feedback: "rolled
    # back from 0.4.0" to 0.4.1 read as if 0.4.0 were the newer one).
    newer = numbers(back) > numbers(now or "")
    said = f"back to the newer one, from {now}" if newer else f"rolled back from {now}"
    out.emit({"now": back, "before": now, "newer": newer}, f"now runs {back} ({said})")
    return 0


__all__ = [
    "Layout", "add_to_path", "checked", "home", "in_use", "install", "install_version",
    "latest", "notice", "prune", "python_of", "self_update", "switch", "update_lines",
]  # fmt: skip
