"""Watched background tasks (the owner, 2026-11-25): a long command an AI client runs on its
own machine, supervised so that a hung, dead or finished command is noticed without asking.

An agent starts a long command and forgets it, cannot tell slow from stuck, has no clock to
check back, and goes idle while the command is dead or finished. So:
- `amsyos bg start [--name N] [--expect 10m] [--quiet-after 5m] [--kill-after-quiet 30m]
  [--out PATH] -- <command…>` returns at once. A detached supervisor (this module, run as
  `python -m amsyos_cli.bg`) runs the command in a process group of its own, all its output
  into a local log that is never sent anywhere, and every 30 s measures its progress: the
  log's bytes, the process tree's CPU seconds and I/O bytes (Linux's /proc; elsewhere the log
  and "alive" only) and the size of `--out`. The task is `quiet` when nothing grew for
  `--quiet-after` (default 5 min), `overdue` past 1.5 times `--expect` (else AMSYOS's guess
  from past runs). It beats to AMSYOS (`bg.beat`) with numbers only, at most once a minute
  and at once on a change of state, and ends with the command's exit code;
  `--kill-after-quiet` kills the tree after that long quiet and says so.
- `bg ls [--all]`, `bg show <task>` (with the local log's last 30 lines), `bg wait <task>`
  (exit 0 done, 1 failed, 3 quiet or timeout), `bg kill <task>`.
- `bg hook-stop`, Claude Code's `Stop` hook: keeps the agent from ending its turn while one of
  its tasks ended unread, or is quiet, overdue or lost, until the agent looked at it (`show`,
  `wait`, `ls`, `kill`) or was told twice in that state. Never when the hook's input says
  `stop_hook_active`, never for a task that runs well, nothing on any error, within 3 s.

AMSYOS gets the task's name only (at most 60 characters; by default the program's base name
and its first word that is not an option, cut at `=`, `:` or `@`), a fingerprint (a hash of
that name and the folder's name) and numbers: never the arguments, never the output.

Each task's spec, state and log live under the CLI's state folder,
`<AMSYOS_HOME>/state/bg/<task>/`. A task AMSYOS could not register (not reached, not logged
in, a Gateway without `bg.start`) is watched on this machine only as `bgl_…`, and registers
when AMSYOS answers. Standard library only at import: the Stop hook's path loads this module;
the HTTP client is loaded only to call.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from amsyos_cli import detach
from amsyos_cli.common import CliError, Out, credentials, obj, seq, short
from amsyos_cli.instance import instance_of
from amsyos_cli.secret import env_specs, fetch, masked_pipe

if TYPE_CHECKING:
    from amsyos_cli.api import Api

START, BEAT, LIST = "bg.start", "bg.beat", "bg.list"
SUPERVISE = "_supervise"
SPEC, STATE, LOG, SEEN, KILL = "spec.json", "state.json", "log", "seen.json", "kill"
TASK_ID = re.compile(r"^bgl?_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
NAME_MOST = 60
TICK_S = 30.0  # progress is measured this often
FIRST_TICK_S = 2.0  # the first measure: the baseline, once the command has loaded
POLL_S = 1.0  # the supervisor looks at the command and a kill request this often
BEAT_EVERY_S = 60.0  # beats at most this often, unless the state changes
BEAT_WAIT_S = 15.0
FINAL_TRIES_S = (0.0, 5.0, 15.0, 30.0, 60.0)  # the last beat, asked again this long after
REGISTER_EVERY_S = 300.0  # a task watched here only asks AMSYOS to register it this often
QUIET_AFTER_S = 300
OVERDUE = 1.5
LOST_AFTER_S = 180.0  # no state written for this long while running: lost
KILL_GRACE_S = 5.0
KILL_WAIT_S = 20.0
WEEK_S = 7 * 86400
KEEP_S = 7 * 86400  # an ended task's folder is kept this long
KEEP_MOST = 200
RUNNING_MOST = 20  # running tasks of one run (AMSYOS refuses a 21st too)
TAIL_LINES = 30
TAIL_BYTES = 64 * 1024
LINE_MOST = 300
OUT_ENTRIES_MOST = 10_000  # files of a folder `--out` measured
NUMBER_MOST = 2**53 - 1
GROWTH = {"log_bytes": 1, "out_bytes": 1, "io_bytes": 4096, "cpu_s": 0.2}  # progress a tick
HOOK_WAIT_S = 2.5  # within the 3 s the hook is allowed, its start included
HOOK_BLOCKS_MOST = 2  # times the hook holds a turn for one task in one state
HOOK_REMOTE_EVERY_S = 300.0
WAIT_REMOTE_S = 15.0
FINAL = frozenset({"done", "failed", "killed"})
ENDED = FINAL | {"lost"}
LIVE = frozenset({"running", "quiet", "overdue"})
# Not reached, not logged in, a Gateway without the service: watched on this machine only.
LOCAL_ONLY = frozenset(
    {"UNREACHABLE", "SERVICE_NOT_FOUND", "NOT_LOGGED_IN", "SESSION_ENDED", "GATEWAY_MISMATCH",
     "RATE_LIMITED", "TIMEOUT"}
)  # fmt: skip
# The end is not asked again after these: nothing would change in two minutes.
GIVE_UP = frozenset({"NOT_LOGGED_IN", "SESSION_ENDED", "SERVICE_NOT_FOUND", "GATEWAY_MISMATCH"})
_CUT = re.compile(r"[=:@]")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")
_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # a terminal's colours and moves
_TIME = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|d)?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}
USAGE = (
    "amsyos bg start [--name N] [--expect 10m] [--quiet-after 5m] [--kill-after-quiet 30m] "
    "[--out PATH] [--env NAME[=VAR]] -- <command...>"
)
PUMP_JOIN_S = 5.0  # the masked output's last bytes, once the command ended


TASKKILL = "taskkill.exe"


def windows() -> bool:
    return sys.platform == "win32"


# ---------------------------------------------------------------- where, and small helpers
def home() -> Path:
    """AMSYOS_HOME, as update.home() says it (that module loads the HTTP client)."""
    told = os.environ.get("AMSYOS_HOME")
    if told:
        return Path(told).expanduser()
    if windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "amsyos"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "amsyos"


def root() -> Path:
    return home() / "state" / "bg"


def _read(path: Path) -> dict[str, Any]:
    try:
        return obj(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    """Whole or not at all: a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        for _ in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:  # Windows: a reader holds it open for a moment
                time.sleep(0.05)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        tmp.unlink()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def dur(value: Any) -> str:
    """`48s`, `2m48s`, `1h05m`."""
    s = _number(value)
    if s is None or s < 0:
        return "?"
    whole = int(s)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def size(value: Any) -> str:
    n = _number(value)
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def seconds_of(text: str, flag: str, least: int = 1, most: int = WEEK_S) -> int:
    """`90`, `90s`, `10m`, `2h`, `1d` in seconds."""
    found = _TIME.match(text or "")
    if not found:
        raise CliError("INVALID_INPUT", f"{flag} takes a time like 90s, 10m or 2h, not {text!r}")
    value = round(float(found[1]) * _UNITS[(found[2] or "").lower()])
    if not least <= value <= most:
        raise CliError("INVALID_INPUT", f"{flag} is {dur(least)} to {dur(most)}, not {dur(value)}")
    return value


def _base(word: str) -> str:
    return re.split(r"[\\/]", word.rstrip("\\/"))[-1] or word


def name_of(argv: list[str], given: str | None = None) -> str:
    """What AMSYOS is told the task is: `--name`, else the program's base name and its first
    word that is not an option, cut at `=`, `:` or `@` (a command line can hold a secret)."""
    if given is not None:
        name = " ".join(_CONTROL.sub(" ", given).split())
        if not name:
            raise CliError("INVALID_INPUT", "--name takes a few words, e.g. --name backup")
        if len(name) > NAME_MOST:
            raise CliError(
                "INVALID_INPUT", f"--name is at most {NAME_MOST} characters, not {len(name)}"
            )
        return name
    parts: list[str] = []
    if argv:
        parts.append(_CUT.split(_base(argv[0]), 1)[0])
    word = next((a for a in argv[1:] if a.strip() and not a.startswith("-")), "")
    first = word.split()[0] if word.split() else ""
    if _DRIVE.match(first):
        first = _base(first)
    first = _base(_CUT.split(first, 1)[0]) if first else ""
    if first:
        parts.append(first)
    name = " ".join(_CONTROL.sub("", p) for p in parts if p).strip()
    return name[:NAME_MOST] or "task"


def fingerprint_of(name: str, folder: Path) -> str:
    return hashlib.sha256(f"{name}\n{folder.name}".encode()).hexdigest()[:16]


def task_id(given: str) -> str:
    if not TASK_ID.match(given or ""):
        raise CliError("INVALID_INPUT", f"a task is bg_… (amsyos bg ls), not {short(given, 40)!r}")
    return given


# ---------------------------------------------------------------- the local tasks
def _alive(pid: Any) -> bool:
    """POSIX only: a process with this pid exists (Windows' os.kill would end it)."""
    if windows() or not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def effective(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """The state as it is now: `lost` when the supervisor stopped writing it while the task
    ran (the machine slept, or the supervisor died); a running task's elapsed time counted on."""
    now = time.time() if now is None else now
    told = dict(state)
    at = _number(told.get("at")) or 0.0
    if told.get("state") in FINAL:
        return told
    if now - at > LOST_AFTER_S or not _alive(told.get("pid")):
        told["lost_s"] = round(max(0.0, now - at))
        told["state"] = "lost"
        return told
    elapsed = _number(told.get("elapsed_s"))
    if elapsed is not None:
        told["elapsed_s"] = round(elapsed + max(0.0, now - at))
    return told


def local_tasks() -> list[tuple[Path, dict[str, Any]]]:
    """Every task folder on this machine, newest first, with its effective state."""
    try:
        entries = [e for e in os.scandir(root()) if TASK_ID.match(e.name) and e.is_dir()]
    except OSError:
        return []
    now = time.time()
    found: list[tuple[Path, dict[str, Any]]] = []
    for entry in entries:
        state = _read(Path(entry.path) / STATE)
        if state:
            found.append((Path(entry.path), effective(state, now)))
    found.sort(key=lambda fs: _number(fs[1].get("started")) or 0.0, reverse=True)
    return found


def find_local(task: str) -> tuple[Path, dict[str, Any]] | None:
    folder = root() / task
    if (folder / STATE).is_file():
        return folder, effective(_read(folder / STATE))
    for found in local_tasks():
        if found[1].get("remote") == task:
            return found
    return None


def the_id(folder: Path, state: dict[str, Any]) -> str:
    """AMSYOS's id once it has one, else the local one."""
    remote = state.get("remote")
    return remote if isinstance(remote, str) and remote else folder.name


def episode(state: dict[str, Any]) -> str:
    """One spell of a state: `quiet:<since>` (quiet again later is another one)."""
    st = state.get("state")
    return f"{st}:{state.get('at') if st == 'lost' else state.get('since', '')}"


def mark_seen(folder: Path, state: dict[str, Any]) -> None:
    """The agent looked at the task in this state: the Stop hook stops holding turns for it."""
    seen = _read(folder / SEEN)
    if seen.get("seen") != episode(state):
        _write(folder / SEEN, {**seen, "seen": episode(state)})


def prune() -> None:
    """Ended tasks' folders older than a week go, and the oldest beyond 200."""
    now = time.time()
    tasks = local_tasks()
    for i, (folder, state) in enumerate(tasks):
        old = now - (_number(state.get("at")) or 0.0) > KEEP_S
        if state.get("state") in ENDED and (old or i >= KEEP_MOST):
            shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------- lines
def summary(state: dict[str, Any]) -> str:
    """One state as a few words: `quiet: no output, CPU or I/O for 6m00s (expected 10m00s)`."""
    st = state.get("state")
    elapsed, quiet, expect = state.get("elapsed_s"), state.get("quiet_s"), state.get("expect_s")
    expected = f" (expected {dur(expect)})" if _number(expect) else ""
    if st == "running":
        return f"running {dur(elapsed)}" + (f" of about {dur(expect)}" if _number(expect) else "")
    if st == "quiet":
        return f"quiet: no output, CPU or I/O for {dur(quiet)}{expected}"
    if st == "overdue":
        return f"overdue: running {dur(elapsed)}, expected {dur(expect)}"
    if st in {"done", "failed"}:
        return f"ended: exit {state.get('exit')} after {dur(elapsed)}"
    if st == "killed":
        why = state.get("why")
        return f"killed after {dur(elapsed)}" + (f": {short(why, 120)}" if why else "")
    if st == "lost":
        if _number(state.get("lost_s")) is not None:
            ago = dur(state.get("lost_s"))
            return f"lost: no beat for {ago} (the machine slept or the supervisor died)"
        last = short(state.get("last_beat") or "?", 40)
        return f"lost: no beat since {last} (the machine slept or the supervisor died)"
    return str(st or "?")


def line_of(task: str, state: dict[str, Any], *, other: bool = False) -> str:
    name = short(state.get("name") or "?", NAME_MOST)
    where = "" if state.get("here", True) else ", on another machine"
    return f"{task}  {name}: {summary(state)}{where}{' (another run)' if other else ''}"


def log_tail(path: Path, lines: int = TAIL_LINES) -> list[str]:
    """The log's last lines, each as a terminal would leave it (after its last `\\r`)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return []
    text = _ESCAPE.sub("", raw.decode("utf-8", errors="replace"))
    kept = [_CONTROL.sub("", line.rsplit("\r", 1)[-1])[:LINE_MOST] for line in text.split("\n")]
    while kept and not kept[-1].strip():
        kept.pop()
    return kept[-lines:]


# ---------------------------------------------------------------- AMSYOS
def _api(instance: str | None = None) -> Api:
    """The CLI's own Api; `instance`: the run that started the task (the supervisor's own
    process would say another)."""
    from amsyos_cli.api import Api  # noqa: PLC0415 - the hook's path loads it only to call

    class _RunApi(Api):
        async def __aenter__(self) -> _RunApi:
            await super().__aenter__()
            client = self._client  # pyright: ignore[reportPrivateUsage]
            if instance and client is not None:
                client.headers["X-Amsyos-Instance"] = instance
            return self

    return _RunApi(credentials(), Out(json=True))


async def ask(
    service: str, params: dict[str, Any], *, instance: str | None = None
) -> dict[str, Any]:
    """`amsyos.run` of one bg service, its `data`; without git's `source` (nothing to say)."""
    async with _api(instance) as api:
        answer = await api.call("amsyos.run", {"service_id": service, "params": params})
    return obj(answer.get("data"))


async def ask_within(
    service: str, params: dict[str, Any], wait_s: float, *, instance: str | None = None
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(ask(service, params, instance=instance), wait_s)
    except TimeoutError:
        raise CliError("TIMEOUT", f"AMSYOS did not answer {service} within {wait_s:g} s") from None


async def remote_rows(all_: bool) -> list[dict[str, Any]]:
    data = await ask_within(LIST, {"all": True} if all_ else {}, 20.0)
    return [obj(t) for t in seq(data.get("tasks")) if isinstance(obj(t).get("task"), str)]


# ---------------------------------------------------------------- the supervisor
class _Measure:
    """Progress of a process tree: its log's bytes, `--out`'s, and on Linux its CPU seconds
    and I/O bytes (all read and written bytes, sockets and pipes too)."""

    def __init__(self, pid: int, log: Path, out: Path | None) -> None:
        self.pid = pid
        self.log = log
        self.out = out
        self.linux = Path("/proc/self/stat").is_file()
        self.clock = float(os.sysconf("SC_CLK_TCK")) if self.linux else 100.0
        self.last: dict[str, float] = {}

    def tree(self) -> tuple[dict[int, list[str]], set[int]]:
        """Every process's stat fields after its name, and the command's tree: its children
        down, and its session (orphans adopted by init stay in it)."""
        stats: dict[int, list[str]] = {}
        children: dict[int, list[int]] = {}
        with contextlib.suppress(OSError):
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                try:
                    raw = Path(entry.path, "stat").read_text()
                    rest = raw[raw.rindex(")") + 2 :].split()
                    pid, parent = int(entry.name), int(rest[1])
                except (OSError, ValueError, IndexError):
                    continue
                stats[pid] = rest
                children.setdefault(parent, []).append(pid)
        found = {p for p, rest in stats.items() if rest[3] == str(self.pid)}
        todo = [self.pid]
        while todo:
            pid = todo.pop()
            if pid in stats:
                found.add(pid)
            todo.extend(c for c in children.get(pid, []) if c not in found)
        return stats, found

    def signals(self) -> dict[str, float]:
        told: dict[str, float] = {"log_bytes": float(_size_of(self.log))}
        if self.out is not None:
            told["out_bytes"] = float(_size_of(self.out))
        if self.linux:
            stats, tree = self.tree()
            ticks = io = 0
            for pid in tree:
                rest = stats[pid]
                with contextlib.suppress(ValueError, IndexError):
                    ticks += sum(int(rest[i]) for i in (11, 12, 13, 14))
                with contextlib.suppress(OSError, ValueError):
                    for row in Path(f"/proc/{pid}/io").read_text().splitlines():
                        key, _, value = row.partition(":")
                        if key in {"rchar", "wchar"}:
                            io += int(value)
            told["cpu_s"] = ticks / self.clock
            told["io_bytes"] = float(io)
        return told

    def grew(self, told: dict[str, float]) -> bool:
        """Did anything grow by its least step since the last measure? (A tree that only
        idles, polling a socket, grows a little: that is no progress.)"""
        grew = any(
            key in self.last and value - self.last[key] >= GROWTH.get(key, 1)
            for key, value in told.items()
        )
        self.last = dict(told)
        return grew


def _size_of(path: Path) -> int:
    """A file's size, or a folder's files' (at most 10,000 of them); links not followed."""
    try:
        if not path.is_dir():
            return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0
    total = seen = 0
    todo = [path]
    while todo and seen < OUT_ENTRIES_MOST:
        with contextlib.suppress(OSError), os.scandir(todo.pop()) as entries:
            for entry in entries:
                seen += 1
                with contextlib.suppress(OSError):
                    if entry.is_dir(follow_symlinks=False):
                        todo.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
    return total


def _kill_tree(proc: subprocess.Popen[bytes], extra: set[int]) -> None:
    """The command's process group, then what left it (Linux), or its tree (Windows)."""
    if windows():
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603 - Windows' own taskkill, fixed arguments
                [TASKKILL, "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                check=False, timeout=30, creationflags=detach.CREATE_NO_WINDOW,
            )  # fmt: skip
        return
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE_S)
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    for pid in extra - {proc.pid}:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


class _Supervisor:
    """One task: runs its command, measures it, writes its state, beats to AMSYOS."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.spec = _read(folder / SPEC)
        self.state = _read(folder / STATE)
        self.instance: str | None = self.spec.get("instance") or None
        self.remote: str | None = self.state.get("remote") or None
        self.quiet_after = _number(self.spec.get("quiet_after_s")) or QUIET_AFTER_S
        self.kill_after = _number(self.spec.get("kill_after_quiet_s"))
        self.expect = _number(self.state.get("expect_s"))
        self.stop_why: str | None = None
        self.beaten: str | None = None  # the state AMSYOS was last told
        self.beat_ok = float("-inf")
        self.registered_at = float("-inf")
        self.pumping: threading.Thread | None = None  # `--env`: the output through a masker

    # -------------------------------------------------- state and beats
    def save(self, **changes: Any) -> None:
        self.state.update(changes, at=time.time(), pid=os.getpid(), remote=self.remote)
        _write(self.folder / STATE, self.state)

    def params(self) -> dict[str, Any]:
        told: dict[str, Any] = {
            "task": self.remote,
            "state": self.state.get("state"),
            "elapsed_s": min(int(_number(self.state.get("elapsed_s")) or 0), NUMBER_MOST),
            "quiet_s": min(int(_number(self.state.get("quiet_s")) or 0), NUMBER_MOST),
        }
        signals = {
            k: min(max(0, round(v)), NUMBER_MOST)
            for k, v in obj(self.state.get("signals")).items()
            if _number(v) is not None
        }
        if signals:
            told["signals"] = signals
        if self.state.get("exit") is not None:
            told["exit"] = self.state.get("exit")
        return told

    def register(self) -> None:
        """A task watched here only: AMSYOS asked again, at most every 5 minutes."""
        now = time.monotonic()
        if self.remote or now - self.registered_at < REGISTER_EVERY_S:
            return
        self.registered_at = now
        params = obj(self.spec.get("register"))
        try:
            told = asyncio.run(ask_within(START, params, BEAT_WAIT_S, instance=self.instance))
        except Exception as exc:
            self.state["beat_error"] = _code(exc)
            return
        remote = told.get("task")
        if isinstance(remote, str) and TASK_ID.match(remote) and not remote.startswith("bgl_"):
            self.remote = remote
            self.state.pop("beat_error", None)
            if self.expect is None and _number(told.get("expect_s")):
                self.expect = _number(told.get("expect_s"))
                self.state["expect_s"] = int(self.expect or 0)
            self.save()

    def beat(self, *, force: bool = False) -> bool:
        """Told to AMSYOS: on a change of state, else at most once a minute. True once said."""
        self.register()
        if not self.remote:
            return False
        state = self.state.get("state")
        due = force or state != self.beaten or time.monotonic() - self.beat_ok >= BEAT_EVERY_S
        if not due:
            return True
        try:
            told = asyncio.run(ask_within(BEAT, self.params(), BEAT_WAIT_S, instance=self.instance))
        except Exception as exc:
            self.state["beat_error"] = _code(exc)
            # Refused by AMSYOS (not a network's trouble): asking again changes nothing.
            return isinstance(exc, CliError) and exc.code not in LOCAL_ONLY
        self.state.pop("beat_error", None)
        self.beaten, self.beat_ok = str(state), time.monotonic()
        self.state["beat_at"] = time.time()
        if told.get("stop") is True and self.stop_why is None:
            self.stop_why = "AMSYOS asked to stop it"
        return True

    # -------------------------------------------------- the run
    def run(self) -> int:
        argv = [str(a) for a in seq(self.spec.get("argv"))]
        cwd = str(self.spec.get("cwd") or self.folder)
        out = self.spec.get("out")
        log_path = self.folder / LOG
        began = time.monotonic()
        with log_path.open("ab") as log:
            try:
                proc = self.start(argv, cwd, log)
            except OSError as exc:
                log.write(f"amsyos bg: could not start {_base(argv[0]) if argv else '?'}: "
                          f"{exc.strerror or exc}\n".encode())  # fmt: skip
                log.flush()  # read at once by bg start (secrets not fetched: why)
                now = time.time()
                self.save(state="failed", exit=127, elapsed_s=0, quiet_s=0, ended=now, since=now)
                self.final()
                return 127
            child_start = None
            with contextlib.suppress(OSError, ValueError, IndexError):
                raw = Path(f"/proc/{proc.pid}/stat").read_text()
                child_start = raw[raw.rindex(")") + 2 :].split()[19]
            self.save(state="running", child=proc.pid, child_start=child_start,
                      since=time.time())  # fmt: skip
            measure = _Measure(proc.pid, log_path, Path(out) if isinstance(out, str) else None)
            progress = began
            next_tick = began + FIRST_TICK_S
            wall, mono = time.time(), time.monotonic()
            code: int | None = None
            while code is None:
                try:
                    code = proc.wait(timeout=POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                # A wall clock that jumped past the monotonic one: the machine slept. Measure
                # and write at once, so that nobody reads the task as lost a moment longer.
                if (time.time() - wall) - (now - mono) > 60:
                    next_tick = now
                wall, mono = time.time(), now
                if (self.folder / KILL).exists() and self.stop_why is None:
                    self.stop_why = "asked: amsyos bg kill"
                if self.stop_why is not None:
                    self.kill(proc, measure, log)
                    continue
                if now < next_tick:
                    continue
                next_tick = now + TICK_S
                signals = measure.signals()
                if measure.grew(signals):
                    progress = now
                quiet, elapsed = now - progress, now - began
                if quiet >= self.quiet_after:
                    state = "quiet"
                elif self.expect and elapsed > self.expect * OVERDUE:
                    state = "overdue"
                else:
                    state = "running"
                if state != self.state.get("state"):
                    self.state["since"] = time.time()
                kept = {k: round(v, 1) if k == "cpu_s" else round(v) for k, v in signals.items()}
                self.state.update(
                    state=state, elapsed_s=round(elapsed), quiet_s=round(quiet), signals=kept
                )
                if self.kill_after and quiet >= self.kill_after and self.stop_why is None:
                    self.stop_why = (
                        f"quiet for {dur(quiet)} (--kill-after-quiet {dur(self.kill_after)})"
                    )
                self.beat()
                self.save()
            if self.pumping is not None:
                self.pumping.join(PUMP_JOIN_S)
            elapsed = time.monotonic() - began
            exit_code = code if code >= 0 else 128 - code
            if self.stop_why is not None:
                state = "killed"
                log.write(f"\namsyos bg: killed: {self.stop_why}\n".encode())
            else:
                state = "done" if code == 0 else "failed"
        signals = obj(self.state.get("signals"))
        signals["log_bytes"] = _size_of(log_path)
        self.save(
            state=state, exit=exit_code, elapsed_s=round(elapsed), ended=time.time(),
            since=time.time(), why=self.stop_why, signals=signals,
        )  # fmt: skip
        self.final()
        return exit_code

    def start(self, argv: list[str], cwd: str, log: Any) -> subprocess.Popen[bytes]:
        if not argv:
            raise OSError("no command")
        env = {**os.environ, "AMSYOS_BG_TASK": self.remote or self.folder.name}
        # `--env`: the workspace's secrets, fetched now with the run's login (the spec file
        # keeps their names only), given to the command and masked in its log (secret.py).
        # TODO(review): credentials — values held by this process, never written unmasked.
        pairs = [(str(p[0]), str(p[1])) for p in seq(self.spec.get("env")) if len(seq(p)) == 2]
        values: dict[str, str] = {}
        if pairs:
            try:
                values = asyncio.run(fetch(list(dict.fromkeys(n for n, _ in pairs))))
            except Exception as exc:
                said = f": {short(exc.hint, 200)}" if isinstance(exc, CliError) else ""
                raise OSError(f"its secrets were not fetched ({_code(exc)}{said})") from None
            env.update({var: values[name] for name, var in pairs})
        flags = detach.CREATE_NEW_PROCESS_GROUP | detach.CREATE_NO_WINDOW if windows() else 0
        proc = subprocess.Popen(  # noqa: S603 - the agent's own command, on its own machine
            argv, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if values else log, stderr=subprocess.STDOUT,
            env=env, close_fds=True, start_new_session=not windows(), creationflags=flags,
        )  # fmt: skip
        if values:
            self.pumping = masked_pipe(proc.stdout, log.write, values, log.flush)
        return proc

    def kill(self, proc: subprocess.Popen[bytes], measure: _Measure, log: Any) -> None:
        extra = measure.tree()[1] if measure.linux else set[int]()
        log.flush()
        _kill_tree(proc, extra)

    def final(self) -> None:
        """The end told to AMSYOS, asked again for about two minutes when it is not reached."""
        for delay in FINAL_TRIES_S:
            time.sleep(delay)
            if not self.remote:
                self.registered_at = float("-inf")
            if self.beat(force=True) or self.state.get("beat_error") in GIVE_UP:
                break
        self.save()


def _code(exc: BaseException) -> str:
    return exc.code if isinstance(exc, CliError) else type(exc).__name__


def supervise(folder: Path) -> int:
    """`python -m amsyos_cli.bg _supervise <folder>`, started detached by `bg start`."""
    sup = _Supervisor(folder)
    if not windows():
        # Stopped (a shutdown, a person's kill): the command's tree goes with it, said so.
        def stopped(_signum: int, _frame: Any) -> None:
            sup.stop_why = sup.stop_why or "its supervisor was stopped"

        signal.signal(signal.SIGTERM, stopped)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        return sup.run()
    except Exception as exc:
        sup.save(state="failed", why=f"supervisor: {type(exc).__name__}: {short(str(exc), 200)}")
        return 1


# ---------------------------------------------------------------- the commands
async def cmd_bg(args: argparse.Namespace, out: Out) -> int:
    verb = args.bg_cmd
    if verb == "hook-stop":
        return await run_hook_stop()
    command = {"start": cmd_start, "ls": cmd_ls, "show": cmd_show, "wait": cmd_wait}
    return await command.get(verb, cmd_kill)(args, out)


async def cmd_start(args: argparse.Namespace, out: Out) -> int:
    argv = [str(a) for a in seq(args.argv)]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise CliError("INVALID_INPUT", USAGE)
    name = name_of(argv, args.name)
    expect = seconds_of(args.expect, "--expect") if args.expect else None
    quiet_after = (
        seconds_of(args.quiet_after, "--quiet-after") if args.quiet_after else QUIET_AFTER_S
    )
    kill_after = (
        seconds_of(args.kill_after_quiet, "--kill-after-quiet", 10)
        if args.kill_after_quiet
        else None
    )
    secrets_env = env_specs(list(args.env or []))  # names only; the supervisor fetches values
    cwd = Path.cwd()
    out_path = str((cwd / args.out).resolve()) if args.out else None
    instance = instance_of()
    prune()
    running = [
        the_id(f, s) for f, s in local_tasks()
        if s.get("instance") == instance and s.get("state") in LIVE
    ]  # fmt: skip
    if len(running) >= RUNNING_MOST:
        raise CliError(
            "TOO_MANY_TASKS",
            f"this run has {len(running)} tasks running, at most {RUNNING_MOST}: "
            f"{', '.join(running)}; amsyos bg kill <task>, or amsyos bg wait <task> first",
        )
    register: dict[str, Any] = {
        "name": name, "fingerprint": fingerprint_of(name, cwd), "quiet_after_s": quiet_after,
    }  # fmt: skip
    if expect:
        register["expect_s"] = expect
    told, local_why = await _register(register, out)
    remote = told.get("task")
    if not (isinstance(remote, str) and TASK_ID.match(remote) and not remote.startswith("bgl_")):
        remote = None
    task = remote or "bgl_" + secrets.token_hex(6)
    folder = root() / task
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    expect_s = expect or (int(_number(told.get("expect_s")) or 0) or None)
    _write(
        folder / SPEC,
        {"argv": argv, "cwd": str(cwd), "out": out_path, "instance": instance,
         "quiet_after_s": quiet_after, "kill_after_quiet_s": kill_after, "register": register,
         "env": [[name, var] for name, var in secrets_env]},
    )  # fmt: skip
    now = time.time()
    state: dict[str, Any] = {
        "task": task, "remote": remote, "name": name, "instance": instance, "cwd": str(cwd),
        "state": "running", "started": now, "at": now, "elapsed_s": 0, "quiet_s": 0,
        "expect_s": expect_s, "quiet_after_s": quiet_after, "signals": {}, "exit": None,
    }  # fmt: skip
    _write(folder / STATE, state)
    started = detach.start(
        [sys.executable, "-m", "amsyos_cli.bg", SUPERVISE, str(folder)],
        cwd=folder, purpose="bg",
    )  # fmt: skip
    if not started.ok:
        state.update(state="failed", exit=127, why=f"not started: {started.why}")
        _write(folder / STATE, state)
        if remote:
            with contextlib.suppress(Exception):
                await ask_within(BEAT, {"task": remote, "state": "failed", "elapsed_s": 0,
                                        "quiet_s": 0, "exit": 127}, 10.0)  # fmt: skip
        raise CliError(
            "NOT_STARTED",
            f"the supervisor did not start ({short(started.why, 200)}): run the command in the "
            "foreground, or tell the person",
        )
    # A moment for the command to start (or fail to: not found, exit 127).
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        written = _read(folder / STATE)
        state = effective(written) if written else state
        if state.get("child") or state.get("state") in FINAL:
            break
        await asyncio.sleep(0.1)
    history = obj(told.get("history"))
    answer: dict[str, Any] = {
        "task": task, "name": name, "state": state.get("state"), "log": str(folder / LOG),
        "expect_s": expect_s, "quiet_after_s": quiet_after, "history": history or None,
        "registered": remote is not None,
    }  # fmt: skip
    if local_why:
        answer["note"] = local_why
        out.note(f"note: {local_why}")
    if state.get("state") in FINAL:
        answer["exit"] = state.get("exit")
        mark_seen(folder, state)
        out.emit(answer, line_of(task, state), *log_tail(folder / LOG, 10))
        return 0 if state.get("state") == "done" else 1
    expected = ""
    if expect_s:
        expected = f"expected about {dur(expect_s)}"
        if not expect and _number(history.get("runs")):
            expected += (
                f" (from {history.get('runs')} past runs: median {dur(history.get('median_s'))},"
                f" 1 in 10 slower than {dur(history.get('p90_s'))})"
            )
        expected += "; "
    out.emit(
        answer,
        f"{task}  {name}: started; it runs on after this command",
        f"  {expected}quiet after {dur(quiet_after)} with no output, CPU or I/O"
        + (f"; killed after {dur(kill_after)} quiet" if kill_after else ""),
        f"  log (this machine only): {folder / LOG}",
        f"  look: amsyos bg show {task}; wait: amsyos bg wait {task}; stop: amsyos bg kill {task}",
    )
    return 0


async def _register(params: dict[str, Any], out: Out) -> tuple[dict[str, Any], str]:
    """`bg.start`'s answer; or nothing and why, when the task is watched here only."""
    from amsyos_cli.api import Api  # noqa: PLC0415

    try:
        async with Api(credentials(), out) as api:
            told = await asyncio.wait_for(api.instant(START, params), 20.0)
    except TimeoutError:
        code = "TIMEOUT"
    except CliError as exc:
        if exc.code not in LOCAL_ONLY:
            raise
        code = exc.code
    else:
        return told, ""
    why = {
        "SERVICE_NOT_FOUND": "this Gateway has no bg.start yet",
        "NOT_LOGGED_IN": "not logged in to AMSYOS",
        "SESSION_ENDED": "the AMSYOS session ended",
    }.get(code, f"AMSYOS was not reached ({code})")
    return {}, (
        f"{why}: the task is watched on this machine only (bg ls, show, wait, kill and the Stop "
        "hook work); it registers with AMSYOS when AMSYOS answers"
    )


async def cmd_ls(args: argparse.Namespace, out: Out) -> int:
    me = instance_of()
    mine = [(f, s) for f, s in local_tasks() if s.get("instance") == me]
    rows: dict[str, dict[str, Any]] = {}
    for folder, state in mine:
        rows[the_id(folder, state)] = {**state, "task": the_id(folder, state), "here": True,
                                       "mine": True, "log": str(folder / LOG)}  # fmt: skip
        mark_seen(folder, state)
    note = ""
    try:
        for row in await remote_rows(bool(args.all)):
            tid = str(row["task"])
            if tid in rows:
                rows[tid]["mine"] = row.get("mine", True)
                continue
            rows[tid] = {**row, "here": False}
    except CliError as exc:
        note = f"AMSYOS's list was not read ({exc.code}): this machine's tasks of this run only"
        out.debug(f"{LIST}: {exc.code}: {short(exc.hint, 160)}")
    listed = sorted(rows.values(), key=_started_key, reverse=True)
    lines = [line_of(str(r["task"]), r, other=r.get("mine") is False) for r in listed]
    if note and args.all:
        out.note(f"note: {note}")
    answer: dict[str, Any] = {"tasks": listed}
    if note:
        answer["note"] = note
    out.emit(answer, *(lines or ["no background task" + (" in this workspace" if args.all
                                                          else " of this run")]))  # fmt: skip
    return 0


def _started_key(row: dict[str, Any]) -> float:
    started = row.get("started")
    if isinstance(started, int | float):
        return float(started)
    with contextlib.suppress(ValueError, TypeError):
        from datetime import datetime  # noqa: PLC0415

        return datetime.fromisoformat(str(started)).timestamp()
    return 0.0


async def _remote_one(task: str) -> dict[str, Any] | None:
    """A task not on this machine, from AMSYOS; None when AMSYOS does not know it."""
    try:
        rows = await remote_rows(True)
    except CliError as exc:
        if exc.code not in LOCAL_ONLY:
            raise
        raise CliError(
            "NOT_FOUND",
            f"no task {task} on this machine, and AMSYOS was not asked ({exc.code}): amsyos bg ls",
        ) from None
    return next(({**row, "here": False} for row in rows if row.get("task") == task), None)


def _seconds(value: Any) -> str:
    return f"{value:g} s"


def _detail(task: str, folder: Path, state: dict[str, Any]) -> list[str]:
    signals = obj(state.get("signals"))
    said = [
        f"{label} {fmt(signals[key])}"
        for key, label, fmt in (
            ("log_bytes", "log", size), ("cpu_s", "CPU", _seconds),
            ("io_bytes", "I/O", size), ("out_bytes", "out", size),
        )
        if _number(signals.get(key)) is not None
    ]  # fmt: skip
    started = _number(state.get("started"))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "?"
    error = f"; AMSYOS: {state['beat_error']}" if state.get("beat_error") else ""
    lines = [
        line_of(task, state),
        f"  started {when} in {state.get('cwd') or '?'}; quiet after "
        f"{dur(state.get('quiet_after_s'))}{error}",
    ]
    if said:
        lines.append("  measured: " + ", ".join(said))
    tail = log_tail(folder / LOG)
    lines.append(f"  log: {folder / LOG}" + (f" (last {len(tail)} lines)" if tail else " (empty)"))
    return lines + tail


async def cmd_show(args: argparse.Namespace, out: Out) -> int:
    task = task_id(args.task)
    found = find_local(task)
    if found is not None:
        folder, state = found
        mark_seen(folder, state)
        answer = {**state, "task": task, "here": True, "log": str(folder / LOG),
                  "tail": log_tail(folder / LOG)}  # fmt: skip
        out.emit(answer, *_detail(task, folder, state))
        return 0
    row = await _remote_one(task)
    if row is None:
        raise CliError("NOT_FOUND", f"no task {task} here or in this workspace: amsyos bg ls --all")
    out.emit(row, line_of(task, row), "  its log is on the machine that started it")
    return 0


async def cmd_wait(args: argparse.Namespace, out: Out) -> int:
    task = task_id(args.task)
    deadline = time.monotonic() + max(0.0, float(args.timeout))
    found = find_local(task)
    folder = found[0] if found is not None else None
    while True:
        if folder is not None:
            state = effective(_read(folder / STATE))
        else:
            got = await _remote_one(task)
            if got is None:
                raise CliError(
                    "NOT_FOUND", f"no task {task} here or in this workspace: amsyos bg ls --all"
                )
            state = got
        st = state.get("state")
        if st in ENDED or st == "quiet" or time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_S if folder is not None else WAIT_REMOTE_S)
    lines = [line_of(task, state)]
    if folder is not None:
        mark_seen(folder, state)
        if st in {"failed", "killed", "lost"}:
            lines += log_tail(folder / LOG, 10)
    if st == "done":
        code = 0
    elif st in ENDED:
        code = 1
    else:
        code = 3
        if st == "quiet":
            lines.append(
                f"  look: amsyos bg show {task}; then amsyos bg kill {task}, or wait again"
            )
        else:
            lines.append(f"  still running: amsyos bg show {task} (its log), or wait again")
    out.emit({**state, "task": task, "timed_out": code == 3 and st != "quiet"}, *lines)
    return code


async def cmd_kill(args: argparse.Namespace, out: Out) -> int:
    task = task_id(args.task)
    found = find_local(task)
    if found is None:
        raise CliError(
            "NOT_HERE",
            f"{task} is not on this machine: kill it where it was started "
            "(amsyos bg ls --all says which run)",
        )
    folder, state = found
    if state.get("state") in FINAL:
        mark_seen(folder, state)
        out.emit({**state, "task": task}, f"{line_of(task, state)} (it had ended already)")
        return 0
    if state.get("state") != "lost":
        (folder / KILL).write_text("kill\n", encoding="utf-8")
        deadline = time.monotonic() + KILL_WAIT_S
        while time.monotonic() < deadline:
            state = effective(_read(folder / STATE))
            if state.get("state") in ENDED:
                break
            await asyncio.sleep(0.25)
    if state.get("state") not in FINAL:
        state = await _kill_orphan(folder, state)
    mark_seen(folder, state)
    out.emit({**state, "task": task}, line_of(task, state))
    return 0 if state.get("state") == "killed" else 1


async def _kill_orphan(folder: Path, state: dict[str, Any]) -> dict[str, Any]:
    """The supervisor is gone: its command killed from here, on Linux only once its process
    is checked to be the one started (a pid can be reused)."""
    child = state.get("child")
    if not isinstance(child, int) or child <= 0:
        raise CliError("NOT_STARTED", "the command never started: nothing to kill")
    if not Path("/proc/self/stat").is_file():
        raise CliError(
            "NOT_KILLED",
            f"its supervisor stopped and its command (process {child}) cannot be checked here: "
            f"look at process {child}'s group and end it yourself if it is this task's",
        )
    alive = False
    with contextlib.suppress(OSError, ValueError, IndexError):
        raw = Path(f"/proc/{child}/stat").read_text()
        alive = raw[raw.rindex(")") + 2 :].split()[19] == state.get("child_start")
    if alive:
        with contextlib.suppress(OSError):
            os.killpg(child, signal.SIGKILL)
        why = "asked: amsyos bg kill (its supervisor had stopped)"
    else:
        why = "its supervisor had stopped; the command was gone"
    now = time.time()
    state = {**state, "state": "killed", "why": why, "ended": now, "since": now, "at": now,
             "exit": 137 if alive else None}  # fmt: skip
    state.pop("lost_s", None)
    _write(folder / STATE, state)
    remote = state.get("remote")
    if isinstance(remote, str) and remote:
        with contextlib.suppress(Exception):
            params = {
                "task": remote,
                "state": "killed",
                "elapsed_s": int(_number(state.get("elapsed_s")) or 0),
                "quiet_s": int(_number(state.get("quiet_s")) or 0),
            }
            if alive:
                params["exit"] = 137
            await ask_within(BEAT, params, 10.0, instance=state.get("instance") or None)
    return state


# ---------------------------------------------------------------- Claude Code's Stop hook
def _hook_input() -> dict[str, Any]:
    """The hook's JSON on stdin, read within a second (a terminal gives none)."""
    stdin = sys.stdin
    if stdin is None or stdin.isatty():
        return {}
    got: list[bytes] = []

    def read() -> None:
        with contextlib.suppress(Exception):
            got.append(stdin.buffer.read(1_000_000))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(0.5)
    try:
        return obj(json.loads(got[0].decode("utf-8"))) if got and got[0].strip() else {}
    except ValueError:
        return {}


def _runs(given: dict[str, Any]) -> set[str]:
    """This run of the agent, as its tasks were marked: the CLI's own way, and Claude Code's
    session id from the hook's input (the same hash, if its environment lacks it)."""
    runs = {instance_of() or ""}
    session = given.get("session_id")
    if isinstance(session, str) and session:
        runs.add("cl-" + hashlib.sha256(f"claude-code:{session}".encode()).hexdigest()[:12])
    runs.discard("")
    return runs


_PRIORITY = {"failed": 0, "lost": 1, "killed": 2, "quiet": 3, "overdue": 4, "done": 5}


def reason_of(task: str, state: dict[str, Any], more: int) -> str:
    """One line: the task, its state, the command to look."""
    name = short(state.get("name") or "?", NAME_MOST)
    look = f"Look: amsyos bg show {task}"
    if state.get("state") in {"quiet", "overdue"}:
        look += f", then amsyos bg wait {task} or amsyos bg kill {task}"
    unread = ", not read yet" if state.get("state") in FINAL else ""
    told = f"amsyos bg: your task {name} ({task}) is {summary(state)}{unread}. {look}"
    told = told.replace(" is ended:", " ended:").replace(" is killed", " was killed")
    return told + (f" ({more} more: amsyos bg ls)" if more else "")


def _holds(folder: Path, state: dict[str, Any]) -> bool:
    """Not running well, not looked at in this spell of its state, held twice at most."""
    if state.get("state") not in _PRIORITY:
        return False
    seen, now = _read(folder / SEEN), episode(state)
    if seen.get("seen") == now:
        return False
    times = _number(seen.get("times")) if seen.get("held") == now else 0
    return (times or 0) < HOOK_BLOCKS_MOST


def _held(folder: Path, state: dict[str, Any]) -> None:
    seen, now = _read(folder / SEEN), episode(state)
    times = int(_number(seen.get("times")) or 0) if seen.get("held") == now else 0
    _write(folder / SEEN, {**seen, "held": now, "times": times + 1})


def hook_stop() -> int:
    """`amsyos bg hook-stop` from entry.py, before the full CLI is loaded."""
    with contextlib.suppress(BaseException):
        return asyncio.run(run_hook_stop())
    return 0


async def run_hook_stop() -> int:
    began = time.monotonic()
    try:
        given = _hook_input()
        if given.get("stop_hook_active") is True:
            return 0
        left = HOOK_WAIT_S - (time.monotonic() - began)
        reason = await asyncio.wait_for(_hook_reason(given, began), max(0.1, left))
    except BaseException:
        return 0
    if reason:
        with contextlib.suppress(Exception):
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
            sys.stdout.flush()
    return 0


async def _hook_reason(given: dict[str, Any], began: float) -> str | None:
    runs = _runs(given)
    cwd = str(given.get("cwd") or Path.cwd())
    known: set[str] = set()
    holding: list[tuple[Path, dict[str, Any]]] = []
    for folder, state in local_tasks():
        known.update({folder.name, the_id(folder, state)})
        instance = state.get("instance")
        ours = instance in runs if instance else state.get("cwd") == cwd
        if ours and _holds(folder, state):
            holding.append((folder, state))
    if holding:
        holding.sort(key=lambda fs: _PRIORITY.get(str(fs[1].get("state")), 9))
        folder, state = holding[0]
        _held(folder, state)
        return reason_of(the_id(folder, state), state, len(holding) - 1)
    if not root().is_dir() or time.monotonic() - began > 1.0:
        return None  # bg never used on this machine, or no time left to ask
    return await _remote_reason(runs, known)


async def _remote_reason(runs: set[str], known: set[str]) -> str | None:
    """This run's tasks started elsewhere, from AMSYOS at most every 5 minutes: each quiet,
    overdue or lost one told once in that state."""
    mark_path = (
        root() / f".hook-{hashlib.sha256(','.join(sorted(runs)).encode()).hexdigest()[:12]}.json"
    )
    mark = _read(mark_path)
    now = time.time()
    if now - (_number(mark.get("at")) or 0.0) < HOOK_REMOTE_EVERY_S:
        return None
    told = {k: v for k, v in obj(mark.get("told")).items() if now - (_number(v) or 0) < 86400}
    _write(mark_path, {"at": now, "told": told})  # first: a slow or refused call waits too
    credentials()  # not logged in: no call at all
    for row in await remote_rows(False):
        task, st = str(row["task"]), row.get("state")
        key = f"{task}:{st}"
        if task in known or st not in {"quiet", "overdue", "lost"} or key in told:
            continue
        told[key] = now
        _write(mark_path, {"at": now, "told": told})
        return reason_of(task, {**row, "here": False}, 0)
    return None


# ---------------------------------------------------------------- the parser
def add_parsers(add: Any, common: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """`amsyos bg …` (main.py's `add`)."""
    p = add(
        "bg", cmd_bg,
        "watched background tasks: start a long command, ls, show, wait, kill",
        "run anything that takes more than a minute with amsyos bg start: it returns at once, "
        "a supervisor on this machine runs the command, keeps its output in a local log (never "
        "sent anywhere), measures its progress every 30 s (output, CPU, I/O, --out's size) and "
        "tells AMSYOS numbers only. A task is quiet when nothing grew for --quiet-after, "
        "overdue past 1.5 times --expect (else AMSYOS's guess from past runs). The run that "
        "started it is told in its answers' around lines when a task is quiet, overdue, lost "
        "or ended; Claude Code's Stop hook (bg hook-stop) holds a turn until it looked",
        epilog="examples:\n  amsyos bg start --name backup --expect 10m -- ./backup.sh\n"
        "  amsyos bg start --quiet-after 2m --kill-after-quiet 20m -- python train.py\n"
        "  amsyos bg ls\n  amsyos bg show bg_...\n  amsyos bg wait bg_... --timeout 600\n"
        "  amsyos bg kill bg_...",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    verbs = p.add_subparsers(
        dest="bg_cmd", metavar="start|ls|show|wait|kill|hook-stop", required=True
    )
    ps = verbs.add_parser(
        "start", parents=[common], help="run a command watched, in the background",
        description=f"{USAGE}. Only --name (else the program and its first word) goes to "
        "AMSYOS, never the arguments or the output. Times: 90s, 10m, 2h",
    )  # fmt: skip
    ps.add_argument("--name", help=f"what to call it (at most {NAME_MOST} characters)")
    ps.add_argument("--expect", metavar="TIME", help="how long it should take: overdue at 1.5x")
    ps.add_argument(
        "--quiet-after", metavar="TIME", help="quiet after this long with nothing growing (5m)"
    )
    ps.add_argument(
        "--kill-after-quiet", metavar="TIME", help="kill its tree after this long quiet"
    )
    ps.add_argument(
        "--out", metavar="PATH", help="a file or folder it writes: its size is progress"
    )
    ps.add_argument(
        "--env", action="append", metavar="NAME[=VAR]",
        help="a secret of the workspace in its environment (as NAME, or VAR), fetched by the "
        "supervisor at start and masked in the log, as amsyos secret run does: against "
        "accidents, not an agent that means to print it; repeat for more",
    )  # fmt: skip
    ps.add_argument(  # not `command`: the top parser names the subcommand so (main.py)
        "argv",
        metavar="command",
        nargs=argparse.REMAINDER,
        help="-- then the command and its arguments",
    )
    pl = verbs.add_parser("ls", parents=[common], help="this run's tasks")
    pl.add_argument("--all", action="store_true", help="the workspace's tasks (at most 50)")
    pw = verbs.add_parser(
        "show", parents=[common], help="a task's state, numbers and its log's last 30 lines"
    )
    pw.add_argument("task")
    pt = verbs.add_parser(
        "wait", parents=[common],
        help="until it ends or turns quiet: exit 0 done, 1 failed, 3 quiet or timeout",
    )  # fmt: skip
    pt.add_argument("task")
    pt.add_argument("--timeout", type=float, default=600, help="seconds at most (default 600)")
    pk = verbs.add_parser("kill", parents=[common], help="kill its process tree")
    pk.add_argument("task")
    ph = verbs.add_parser(
        "hook-stop", parents=[common],
        help="Claude Code's Stop hook: holds the turn while a task ended unread, is quiet, "
        "overdue or lost",
    )  # fmt: skip
    return {"bg": p, "bg start": ps, "bg ls": pl, "bg show": pw, "bg wait": pt,
            "bg kill": pk, "bg hook-stop": ph}  # fmt: skip


def _main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != SUPERVISE:
        sys.stderr.write("amsyos bg: started by amsyos bg start only\n")
        return 2
    from amsyos_cli.entry import hold  # noqa: PLC0415 - this version is not deleted under us

    hold()
    with contextlib.suppress(Exception):
        importlib.import_module("amsyos_cli.api")  # loaded now, while its version is here
    return supervise(Path(argv[1]))


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
