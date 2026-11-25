"""Which coding agent runs this command, and which run of it: standard library only, so the
prompt hook (`amsyos around --hook`) loads it in a few milliseconds."""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path

from amsyos_cli import __version__


def host_of() -> tuple[str, str]:
    """The coding agent that runs this command, as its environment says (shown to the
    person on the device page; never used to decide anything, spec v3 §4.1)."""
    env = os.environ
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code", env.get("CLAUDE_CODE_VERSION", "")
    if any(k.startswith("CODEX_") for k in env):
        return "codex", env.get("CODEX_VERSION", "")
    if env.get("CURSOR_TRACE_ID") or env.get("CURSOR_AGENT"):
        return "cursor", ""
    if env.get("ANTIGRAVITY_AGENT") or env.get("ANTIGRAVITY"):
        return "antigravity", ""
    return "terminal", f"amsyos-cli {__version__} on {platform.system().lower()}"


# Shells and launchers between a coding agent and this command: skipped when looking for
# the agent's own process.
_LAUNCHERS = frozenset(
    {"sh", "bash", "zsh", "dash", "fish", "ksh", "env", "uv", "uvx", "python", "python3",
     "amsyos", "timeout", "nice", "sudo", "su", "script", "tmux", "screen"}
)  # fmt: skip


def _agent_process() -> str | None:
    """The first process above this one that is not a shell or a launcher, as "pid:start"
    (Linux's /proc only; elsewhere None)."""
    pid = os.getppid()
    for _ in range(8):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        rest = stat[stat.rindex(")") + 2 :].split()
        if Path(name).name.lower() not in _LAUNCHERS:
            return f"{pid}:{rest[19]}"  # starttime: a pid reused later is another process
        pid = int(rest[1])
        if pid <= 1:
            return None
    return None


def instance_of() -> str | None:
    """Which run of a coding agent this command belongs to, for presence only (the owner,
    2026-11-25: who else works in the workspace). Several runs on one login, such as two
    Claude Code sessions and a Codex on one machine, are told apart by it. It is a short
    hash of the host's own session id (Claude Code's), else of the agent's process. It never
    decides anything."""
    env = os.environ
    host, _ = host_of()
    raw = (
        env.get("CLAUDE_CODE_SESSION_ID")
        or env.get("CODEX_SESSION_ID")
        or env.get("CURSOR_TRACE_ID")
        or _agent_process()
    )
    if not raw:
        return None
    digest = hashlib.sha256(f"{host}:{raw}".encode()).hexdigest()[:12]
    return f"{host[:2]}-{digest}"
