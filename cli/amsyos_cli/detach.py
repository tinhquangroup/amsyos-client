"""One way to start a process that outlives this one, even when an agent host runs us.

A customer, 2026-11-21 (Windows 10, Claude Code and Codex): agent hosts run each command in a
Windows job object and end its whole tree when the command returns. A child started with
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` alone died with it: `self-update`'s script never
wrote a line, and the SessionStart hook's `sync --background` was likely killed too.

- Windows, first: the child leaves the job (`CREATE_BREAKAWAY_FROM_JOB`), with no console of
  its own. A job that forbids breakaway makes that start fail (access denied).
- Windows, then: the Task Scheduler starts it, as a one-time task of the current user, which
  no job of this process holds. The started program removes its task when it ends: the
  caller's own script when it says so (`task=`), else a small hidden PowerShell wrapper that
  runs the command and then removes the task and itself.
- Elsewhere (Linux, macOS): a session of its own (`start_new_session`), as before.

`Started.way` says which: `breakaway`, `scheduler`, `session`, or `none` (`why` says why), so
the caller can tell the person. Standard library only: the SessionStart hook's path imports
this module.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# CreateProcess flags by number: `subprocess` names them only on Windows, and not all of them.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000  # ignored beside DETACHED_PROCESS; kept for a console child
BREAKAWAY = (
    CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
)
TR_MOST = 261  # characters of `schtasks /tr`
SCHTASKS = "schtasks.exe"
# A PowerShell script run with no window and no profile (the scheduler shows a window else).
POWERSHELL_FILE = (
    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden", "-File",
)  # fmt: skip


@dataclass(frozen=True)
class Started:
    way: str  # breakaway | scheduler | session | none
    task: str = ""  # the Task Scheduler task's name (way `scheduler`)
    why: str = ""  # why nothing started (`none`), or why breakaway was refused (`scheduler`)
    process: subprocess.Popen[bytes] | None = None  # the child (`breakaway`, `session`)

    @property
    def ok(self) -> bool:
        return self.way != "none"


def windows() -> bool:
    return sys.platform == "win32"


def task_name(purpose: str) -> str:
    """A task name no other start of this user takes: `amsyos-<purpose>-<pid>-<ms>`."""
    return f"amsyos-{purpose}-{os.getpid()}-{time.time_ns() // 1_000_000}"


def ps_quoted(text: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + text.replace("'", "''") + "'"


def powershell_file(script: Path) -> list[str]:
    """The command that runs a PowerShell script hidden, whichever way it starts."""
    return [*POWERSHELL_FILE, str(script)]


def start(
    argv: Sequence[str], *, cwd: Path | None = None, purpose: str = "run", task: str = ""
) -> Started:
    """`argv` started so that it outlives this process and the host that runs it.

    `task`: on Windows, the scheduler task's name the program removes itself when it ends
    (its script runs `schtasks /delete /tn <task> /f`); without it, a wrapper does."""
    if not windows():
        try:
            child = subprocess.Popen(  # noqa: S603 - the caller's own program, no shell
                list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            )  # fmt: skip
        except OSError as exc:
            return Started("none", why=f"could not start {argv[0]}: {exc}")
        return Started("session", process=child)
    try:
        child = subprocess.Popen(  # noqa: S603 - the caller's own program, no shell
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, creationflags=BREAKAWAY,
        )  # fmt: skip
    except FileNotFoundError as exc:
        return Started("none", why=f"{argv[0]} is not found: {exc}")
    except OSError as exc:  # the job forbids breakaway: access denied
        refused = f"this host's job refused breakaway ({exc})"
    else:
        return Started("breakaway", process=child)
    return _scheduled(list(argv), cwd, purpose, task, refused)


def stop(started: Started) -> None:
    """What `start` began, ended before it did anything (a caller that gave up waiting)."""
    if started.process is not None and started.process.poll() is None:
        started.process.kill()
    if started.way == "scheduler" and started.task:
        _schtasks("/end", "/tn", started.task)
        _schtasks("/delete", "/tn", started.task, "/f")


# ---------------------------------------------------------------- the Task Scheduler
def _scheduled(argv: list[str], cwd: Path | None, purpose: str, task: str, refused: str) -> Started:
    name = task or task_name(purpose)
    command = argv
    if not task:
        wrapper = Path(tempfile.gettempdir()) / f"{name}.ps1"
        wrapper.write_text(_wrapper(argv, cwd, name), encoding="utf-8-sig")  # BOM: PS 5.1
        command = powershell_file(wrapper)
    run = subprocess.list2cmdline(command)
    if len(run) > TR_MOST:
        return Started(
            "none", why=f"{refused}; the command is {len(run)} characters, over the "
            f"{TR_MOST} schtasks /tr takes",
        )  # fmt: skip
    # A start time already gone today: only `/run` starts the task, never its trigger.
    now = time.localtime()
    minutes = max(now.tm_hour * 60 + now.tm_min - 1, 0)
    at = f"{minutes // 60:02d}:{minutes % 60:02d}"
    made, said = _schtasks("/create", "/tn", name, "/tr", run, "/sc", "once", "/st", at, "/f")
    if not made:
        return Started("none", why=f"{refused}; schtasks /create failed: {said}")
    ran, said = _schtasks("/run", "/tn", name)
    if not ran:
        _schtasks("/delete", "/tn", name, "/f")
        return Started("none", why=f"{refused}; schtasks /run failed: {said}")
    return Started("scheduler", task=name, why=refused)


def _schtasks(*args: str) -> tuple[bool, str]:
    try:
        done = subprocess.run(  # noqa: S603 - Windows' own schtasks, fixed arguments
            [SCHTASKS, *args], capture_output=True, text=True, errors="replace", check=False,
            timeout=30, creationflags=CREATE_NO_WINDOW,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    said = " ".join((done.stderr or done.stdout or "").split())[:300]
    return done.returncode == 0, said


def _wrapper(argv: list[str], cwd: Path | None, name: str) -> str:
    """Runs the command, then removes its own task and its own file, whatever happened."""
    where = f"  Set-Location -LiteralPath {ps_quoted(str(cwd))}\n" if cwd is not None else ""
    call = " ".join(ps_quoted(a) for a in argv)
    return f"""$ErrorActionPreference = 'Continue'
try {{
{where}  & {call}
}} finally {{
  {SCHTASKS} /delete /tn {ps_quoted(name)} /f *> $null
  Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}}
"""


__all__ = ["Started", "powershell_file", "ps_quoted", "start", "stop", "task_name", "windows"]
