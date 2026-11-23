"""What `amsyos init` writes so a host knows AMSYOS (spec v3 §3.3), from one source: the
templates of the repository's `adapters/`, a copy of which the wheel carries. The four
hosts are told the same brief (`adapters/brief.md`); each MCP entry names the URL only,
and no token is ever written into the project.

`amsyos init --global` writes the same brief where a host reads it for every project
(`write_global`): Claude Code's personal skill, Codex's and Antigravity's own instruction
files in the person's home. Rules only: no hooks, no MCP entry, no token, no code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from amsyos_cli import __version__
from amsyos_cli.common import MCP_PATH, CliError

HOSTS = ("claude", "codex", "cursor", "antigravity")
MARKETPLACE = "amsyos-local"  # the project's own marketplace of one plugin
PLUGINS = Path(".amsyos") / "plugins"
BEGIN, END = "<!-- amsyos -->", "<!-- /amsyos -->"
IGNORE_BEGIN = "# amsyos: tokens never in the repository; outputs stay local (amsyos init)"
IGNORED = (".amsyos/credentials.json", ".amsyos/state/", ".env.amsyos", "/out/")


def templates() -> Path:
    """The wheel's copy, or the repository's `adapters/` when run from a checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "adapters", here.parent.parent / "adapters"):
        if (candidate / "brief.md").is_file():
            return candidate
    raise CliError("BROKEN_INSTALL", "the adapters' templates are missing; install amsyos again")


@dataclass
class Written:
    files: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])


class Render:
    def __init__(self, gateway: str) -> None:
        self.src = templates()
        self.values = {
            "{{GATEWAY}}": gateway,
            "{{MCP_URL}}": gateway + MCP_PATH,
            "{{VERSION}}": __version__,
        }
        self.values["{{BRIEF}}"] = self.text("brief.md").rstrip("\n")

    def text(self, rel: str) -> str:
        body = (self.src / rel).read_text(encoding="utf-8")
        for key, value in self.values.items():
            body = body.replace(key, value)
        return body


def _put(root: Path, rel: Path | str, body: str, done: Written) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    done.files.append(str(Path(rel)))


def _merge_json(root: Path, rel: str, update: dict[str, Any], done: Written) -> None:
    """Add AMSYOS's keys to a JSON file of the project, keeping everything else."""
    path = root / rel
    try:
        current: Any = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        raise CliError(
            "INVALID_INPUT", f"{rel} is not JSON; fix it, then amsyos init again"
        ) from None
    if not isinstance(current, dict):
        current = {}
    merged: dict[str, Any] = current  # pyright: ignore[reportUnknownVariableType]
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    _put(root, rel, json.dumps(merged, indent=2) + "\n", done)


def _section(
    root: Path, rel: str, section: str, done: Written, marks: tuple[str, str] = (BEGIN, END)
) -> bool:
    """The text between the markers in a shared file (AGENTS.md), replaced or appended;
    True when it was there and is replaced. A file that is not UTF-8 is left as it is."""
    path = root / rel
    try:
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
    except UnicodeDecodeError:
        raise CliError(
            "INVALID_INPUT",
            f"{path} is not UTF-8: save it as UTF-8 (nothing was written), then amsyos init again",
        ) from None
    pattern = re.compile(re.escape(marks[0]) + r".*?" + re.escape(marks[1]) + r"\n?", re.S)
    found = bool(pattern.search(current))
    if found:
        body = pattern.sub(lambda _m: section, current, count=1)
    else:
        body = current + ("\n" if current and not current.endswith("\n\n") else "") + section
    _put(root, rel, body, done)
    return found


def claude(root: Path, r: Render, done: Written) -> None:
    plugin = PLUGINS / "amsyos"
    base = r.src / "claude"
    for path in sorted(base.rglob("*")):
        if path.is_file():
            rel = path.relative_to(base)
            _put(root, plugin / rel, r.text(f"claude/{rel}"), done)
    marketplace = {
        "name": MARKETPLACE,
        "owner": {"name": "AMSYOS"},
        "plugins": [
            {"name": "amsyos", "source": "./amsyos", "description": "AMSYOS for Claude Code"}
        ],
    }
    _put(
        root,
        PLUGINS / ".claude-plugin" / "marketplace.json",
        json.dumps(marketplace, indent=2) + "\n",
        done,
    )
    # A plugin brings hooks, commands that run in the person's environment: it is the
    # person who adds and enables it, never `init` editing `.claude/settings.json` (v3 §12).
    done.notes.append(
        f"Claude Code: `claude plugin marketplace add ./{PLUGINS.as_posix()}`, then "
        f"`claude plugin install amsyos@{MARKETPLACE} --scope project` (or /plugin); "
        "the MCP server signs in with /mcp"
    )


def codex(root: Path, r: Render, done: Written) -> None:
    _section(root, "AGENTS.md", r.text("codex/AGENTS.section.md"), done)
    done.notes.append(
        "Codex: add this MCP entry yourself (no token in it):\n"
        + _indent(r.text("codex/config.toml"))
    )


def cursor(root: Path, r: Render, done: Written) -> None:
    _put(root, ".cursor/rules/amsyos.mdc", r.text("cursor/amsyos.mdc"), done)
    entry: Any = json.loads(r.text("cursor/mcp.json"))
    _merge_json(root, ".cursor/mcp.json", entry, done)
    done.notes.append("Cursor: enable the amsyos MCP server in Settings > MCP; it signs in there")


def antigravity(root: Path, r: Render, done: Written) -> None:
    _put(root, ".agent/rules/amsyos.md", r.text("antigravity/amsyos.md"), done)
    done.notes.append(
        "Antigravity (unverified): merge this into ~/.gemini/antigravity/mcp_config.json:\n"
        + _indent(r.text("antigravity/mcp_config.json"))
    )


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.rstrip("\n").splitlines())


WRITERS = {"claude": claude, "codex": codex, "cursor": cursor, "antigravity": antigravity}


def ignore_tokens(root: Path, done: Written) -> None:
    """The places a token or an output could land, in the project's .gitignore."""
    path = root / ".gitignore"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = set(current.splitlines())
    missing = [line for line in IGNORED if line not in have]
    if not missing:
        return
    block = ([IGNORE_BEGIN] if IGNORE_BEGIN not in have else []) + missing
    sep = "" if not current or current.endswith("\n") else "\n"
    path.write_text(
        current + sep + ("\n" if current else "") + "\n".join(block) + "\n", encoding="utf-8"
    )
    done.files.append(".gitignore")


def write(root: Path, hosts: list[str], gateway: str) -> Written:
    r = Render(gateway)
    done = Written()
    for host in hosts:
        WRITERS[host](root, r, done)
    ignore_tokens(root, done)
    return done


# ---------------------------------------------------------------- a desk (init --type)
# `amsyos init --type studio|work` (the owner, 2026-11-23): a folder an organisation's agent
# works from, as AMSYOS's own media organisation's desk showed: the rules (DESK.md, the
# person's), a log, and in each host's own instruction file a pointer to the rules.
KINDS = ("service", "studio", "work")
DESK, LOG = "DESK.md", "log.md"
DESK_MARKS = ("<!-- amsyos desk -->", "<!-- /amsyos desk -->")
DESK_SIGN = "amsyos init --type"  # in every DESK.md init writes: how a desk is known


def desk_root(start: Path) -> Path | None:
    """The desk a folder is in: the nearest folder up with a DESK.md that init wrote."""
    for folder in (start, *start.parents):
        path = folder / DESK
        if not path.is_file():
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        if DESK_SIGN in head:
            return folder
    return None


def _desk_section(pointer: str, *, import_line: bool = False) -> str:
    begin, end = DESK_MARKS
    body = ("@DESK.md\n" if import_line else "") + pointer
    return f"{begin}\n{body}\n{end}\n"


def _desk_claude(root: Path, pointer: str, done: Written) -> None:
    # Claude Code reads CLAUDE.md in every session and imports `@DESK.md` into it.
    _section(root, "CLAUDE.md", _desk_section(pointer, import_line=True), done, DESK_MARKS)


def _desk_codex(root: Path, pointer: str, done: Written) -> None:
    _section(root, "AGENTS.md", _desk_section(pointer), done, DESK_MARKS)


def _desk_cursor(root: Path, pointer: str, done: Written) -> None:
    rule = (
        "---\ndescription: The rules of this AMSYOS desk (DESK.md)\nalwaysApply: true\n---\n\n"
        f"@DESK.md\n{pointer}\n"
    )
    _put(root, ".cursor/rules/amsyos-desk.mdc", rule, done)


def _desk_antigravity(root: Path, pointer: str, done: Written) -> None:
    _put(root, ".agent/rules/amsyos-desk.md", pointer + "\n", done)


DESK_WRITERS = {
    "claude": _desk_claude, "codex": _desk_codex, "cursor": _desk_cursor,
    "antigravity": _desk_antigravity,
}  # fmt: skip


def desk(root: Path, kind: str, hosts: list[str], gateway: str, values: dict[str, str]) -> Written:
    """DESK.md and log.md, each written once and never overwritten (they are the person's
    and the agent's), and each host's pointer to DESK.md, replaced on every init."""
    r = Render(gateway)
    r.values.update(values)
    done = Written()
    for name, template in ((DESK, f"desk/{kind}.md"), (LOG, "desk/log.md")):
        if (root / name).exists():
            done.notes.append(f"{name} is there already: kept as it is")
        else:
            _put(root, name, r.text(template), done)
    pointer = r.text("desk/pointer.md").rstrip("\n")
    for host in hosts:
        DESK_WRITERS[host](root, pointer, done)
    return done


# ---------------------------------------------------------------- for every project
# `login.host_of()`'s names, as `init` names the hosts.
HOST_NAMES = {
    "claude-code": "claude", "codex": "codex", "cursor": "cursor", "antigravity": "antigravity"
}  # fmt: skip
CURSOR_GLOBAL = "Cursor reads rules per project: run `amsyos init` in each repository"


def global_homes() -> dict[str, Path]:
    """Where each host reads its rules for every project (Cursor: nowhere)."""
    home = Path.home()
    env = os.environ
    return {
        "claude": Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
        "codex": Path(env.get("CODEX_HOME") or home / ".codex"),
        "antigravity": home / ".gemini",
    }


def write_global(host: str, gateway: str) -> Written:
    """The brief where `host` reads it in every project; the marked section replaced on a
    second run, the rest of a shared file kept. Never a hook, an MCP entry or a token."""
    done = Written()
    if host == "cursor":
        done.notes.append(CURSOR_GLOBAL)
        return done
    r = Render(gateway)
    root = global_homes()[host]
    if host == "claude":
        rel = Path("skills") / "amsyos" / "SKILL.md"
        replaced = (root / rel).is_file()
        _put(root, rel, r.text("claude/skills/amsyos/SKILL.md"), done)
        what = "Claude Code's personal skill amsyos,"
    else:
        rel = Path("AGENTS.md" if host == "codex" else "GEMINI.md")
        template = "codex/AGENTS.section.md" if host == "codex" else "antigravity/GEMINI.section.md"
        replaced = _section(root, str(rel), r.text(template), done)
        what = "the amsyos section of"
    done.files = [str(root / f) for f in done.files]
    done.notes.append(
        f"{'replaced' if replaced else 'wrote'} {what} {root / rel}: read in every project "
        "from the host's next session on; in a repository, amsyos init adds its own rules"
    )
    return done
