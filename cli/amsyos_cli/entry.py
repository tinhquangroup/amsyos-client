"""The `amsyos` command's entry point.

The hooks' commands must take under a second (spec v3 §3.3), so they answer here, before
the MCP client is loaded:
- `amsyos sync --quiet` (SessionStart): the brief cached by the last sync written into the
  host's files, and a refresh started in a detached process (amsyos_cli/brief.py);
- `amsyos work checkpoint --quiet` (PreCompact): a no-op until the session's checkpoint
  needs it. Every other command, `work` included, runs in full.
"""

import contextlib
import os
import sys
from pathlib import Path

QUIET_NOOP = (("work", "checkpoint"),)
_HELD: list[object] = []


def hold() -> None:
    """When this process runs from `<AMSYOS_HOME>/versions/<v>` (amsyos_cli/update.py), it
    holds that version's `.in-use` open until it ends, so that no update deletes the
    version it runs: on POSIX with a shared lock, on Windows by the open file itself.
    The standard library only: the hooks' path runs it too."""
    prefix = Path(sys.prefix)
    if prefix.parent.name != "versions" or not (prefix / ".ok").is_file():
        return
    try:
        handle = (prefix / ".in-use").open("a")  # open until the process ends
    except OSError:
        return
    if sys.platform != "win32":
        import fcntl  # noqa: PLC0415 - POSIX only

        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    _HELD.append(handle)


def utf8_output() -> None:
    """stdout and stderr as UTF-8, a character they cannot hold replaced, wherever they are
    not UTF-8 already: a Windows console or pipe in its code page could not print a
    Vietnamese voice's name (Codex, 2026-11-20: it had to switch the console first)."""
    for stream in (sys.stdout, sys.stderr):
        encoding = str(getattr(stream, "encoding", "") or "").lower().replace("_", "-")
        if encoding in {"utf-8", "utf8"}:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


SHIM_TMP = "AMSYOS_SHIM_TMP"


def own_tmp() -> None:
    """The Windows shim (update.py `CMD`) points TMP at its own folder for uv's launcher,
    which moves its working folder there once Python has started (and warns where it may not
    enter TMP), and passes the TMP before as `+<it>`: put back before anything reads it, so
    that amsyos and what it starts use the person's own."""
    saved = os.environ.pop(SHIM_TMP, None)
    if saved is None or not saved.startswith("+"):
        return
    if saved[1:]:
        os.environ["TMP"] = saved[1:]
    else:
        os.environ.pop("TMP", None)


def main() -> int:
    own_tmp()
    hold()
    utf8_output()
    args = sys.argv[1:]
    words = tuple(a for a in args if not a.startswith("-"))
    quiet = "--quiet" in args and not {"-h", "--help"} & set(args)
    if quiet and words[:1] == ("sync",) and len(words) == 1:
        from amsyos_cli import brief  # noqa: PLC0415 - the standard library and json only

        return brief.quiet(Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()))
    if quiet and any(words[: len(hook)] == hook for hook in QUIET_NOOP):
        return 0
    from amsyos_cli.main import main as full  # noqa: PLC0415 - loaded only when needed

    return full()
