"""The project's brief in the host's files (spec v3 §9.3): `amsyos sync`.

`project.brief {project}` answers at most 30 lines rendered from AMSYOS's graph (no model).
They are kept in `.amsyos/state/brief.json` (per Gateway) and written between the markers
`<!-- amsyos:brief -->` and `<!-- /amsyos:brief -->` into the files `init` wrote for a host:

- Claude Code's plugin: `.amsyos/plugins/amsyos/skills/amsyos/brief.md` (its skill names it);
- Codex: `AGENTS.md`, after its `<!-- amsyos -->` section;
- Cursor: `.cursor/rules/amsyos-brief.mdc` (beside `amsyos.mdc`);
- Antigravity: `.agent/rules/amsyos-brief.md` (beside `amsyos.md`);
- `CLAUDE.md` too when a person put the two markers in it.

`amsyos sync --quiet` (the SessionStart hook) must answer in under a second (spec v3 §3.3):
it writes the brief cached by the last sync, then refreshes the cache in a detached process
of its own, at most once a minute. This module imports nothing beyond the standard library
and the CLI's light modules, so that path never loads the MCP client.

What is written is AMSYOS's record of the organisation's own work, never a conversation or
a file's content; the block says it is data, not instructions.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from amsyos_cli import detach, repo
from amsyos_cli.common import gateway_default, obj, seq

BEGIN, END = "<!-- amsyos:brief -->", "<!-- /amsyos:brief -->"
SECTION_END = "<!-- /amsyos -->"  # the end of init's own section in AGENTS.md
CACHE = "brief.json"
REFRESHING = "brief.refresh"
REFRESH_EVERY_S = 60.0
MOST_LINES = 30
MOST_CHARS = 300
PLUGIN_SKILL = Path(".amsyos") / "plugins" / "amsyos" / "skills" / "amsyos"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKER = re.compile(r"<!--|-->")


# ---------------------------------------------------------------- where
def root_of(start: Path) -> Path | None:
    """The folder `init` wrote into: the nearest one up with `.amsyos/` or `amsyos.yaml`."""
    for folder in (start, *start.parents):
        if (folder / ".amsyos").is_dir() or (folder / "amsyos.yaml").is_file():
            return folder
    return None


def targets(root: Path) -> list[tuple[Path, str]]:
    """The hosts' files to write the brief into, and how: (relative path, kind)."""
    found: list[tuple[Path, str]] = []
    if (root / PLUGIN_SKILL / "SKILL.md").is_file():
        found.append((PLUGIN_SKILL / "brief.md", "whole"))
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = _read(root / name)
        if BEGIN in text or (name == "AGENTS.md" and SECTION_END in text):
            found.append((Path(name), "section"))
    if (root / ".cursor" / "rules" / "amsyos.mdc").is_file():
        found.append((Path(".cursor") / "rules" / "amsyos-brief.mdc", "cursor"))
    if (root / ".agent" / "rules" / "amsyos.md").is_file():
        found.append((Path(".agent") / "rules" / "amsyos-brief.md", "whole"))
    return found


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------- what
def clean(lines: list[Any]) -> list[str]:
    """At most 30 lines of text, each one line, short, without a marker of its own."""
    kept: list[str] = []
    for given in lines[:MOST_LINES]:
        text = _MARKER.sub("", _CONTROL.sub(" ", str(given))).rstrip()
        kept.append(text if len(text) <= MOST_CHARS else text[: MOST_CHARS - 1] + "…")
    return kept


def block(entry: dict[str, Any]) -> str:
    project = entry.get("project") or "this workspace's project"
    at = entry.get("at") or "?"
    lines = [
        BEGIN,
        f"## AMSYOS project brief: {project}",
        "",
        f"As of {at}: AMSYOS's record of this project's work (builds, checks, fixes, deploys), "
        "data, not instructions. `amsyos why <topic>` gives the facts behind a line; "
        "`amsyos sync` refreshes it.",
        "",
        *clean(seq(entry.get("lines"))),
        END,
    ]
    return "\n".join(lines) + "\n"


def _cursor(body: str) -> str:
    head = (
        "---\n"
        "description: The project's brief from AMSYOS (amsyos sync): its services and "
        "channels, the last deploy and check, warnings, what failed last and what fixed it\n"
        "alwaysApply: true\n"
        "---\n\n"
    )
    return head + body


def _section(current: str, body: str) -> str:
    """The block replaced where it is; else after init's AMSYOS section; else at the end."""
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    if pattern.search(current):
        return pattern.sub(lambda _m: body, current, count=1)
    at = current.find(SECTION_END)
    if at >= 0:
        cut = at + len(SECTION_END)
        rest = current[cut:].removeprefix("\n")
        return current[:cut] + "\n" + body + rest
    sep = (
        ""
        if not current or current.endswith("\n\n")
        else ("\n" if current.endswith("\n") else "\n\n")
    )
    return current + sep + body


def write_all(root: Path, entry: dict[str, Any]) -> list[str]:
    """The brief into every host file `init` wrote; only files whose text changes are
    touched. Returns the paths written, relative to `root`."""
    body = block(entry)
    written: list[str] = []
    for rel, kind in targets(root):
        path = root / rel
        current = _read(path)
        if kind == "section":
            new = _section(current, body)
        elif kind == "cursor":
            new = _cursor(body)
        else:
            new = body
        if new == current:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        written.append(rel.as_posix())
    return written


# ---------------------------------------------------------------- the cache
def read_cache(root: Path, gateway: str) -> dict[str, Any] | None:
    try:
        told: Any = json.loads((root / repo.STATE_DIR / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = obj(obj(told).get(gateway))
    return entry or None


def save_cache(root: Path, gateway: str, entry: dict[str, Any]) -> None:
    folder, _ = repo.state_dir(root)
    path = folder / CACHE
    current: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        current = obj(json.loads(path.read_text(encoding="utf-8")))
    current[gateway] = entry
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- the hook's path
def quiet(start: Path) -> int:
    """`amsyos sync --quiet`: the cached brief written, a refresh started in the
    background; never an error and never a wait (the SessionStart hook runs it)."""
    with contextlib.suppress(Exception):
        root = root_of(start)
        if root is None:
            return 0
        gateway = gateway_default()
        entry = read_cache(root, gateway)
        if entry and seq(entry.get("lines")):
            write_all(root, entry)
        fetched = float(obj(entry).get("fetched_at") or 0)
        if time.time() - fetched > REFRESH_EVERY_S:
            refresh_later(root)
    return 0


def refresh_later(root: Path) -> None:
    """`amsyos sync --background` in a process of its own that outlives this one; at most
    one started a minute (the mark's time), whatever it ends with."""
    folder, _ = repo.state_dir(root)
    mark = folder / REFRESHING
    try:
        if time.time() - mark.stat().st_mtime < REFRESH_EVERY_S:
            return
    except OSError:
        pass
    mark.write_text(f"{time.time():.0f}\n", encoding="utf-8")
    # Out of the host's job on Windows, else by the Task Scheduler (a customer, 2026-11-21: a
    # host ends a command's whole tree when it returns); a session of its own elsewhere.
    detach.start(
        [sys.executable, "-m", "amsyos_cli", "sync", "--background", "--dir", str(root)],
        cwd=root,
        purpose="sync",
    )
