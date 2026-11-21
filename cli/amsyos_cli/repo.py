"""The repository the command runs in (spec v3 §8.1): where a job came from, never what is
in it. Every call carries `source: {project, branch, commit, dirty}` when there is a git
repository; `amsyos init` binds it to the workspace (`project.bind`) by the SHA-256 of its
normalised remote URL (of the folder's absolute path when it has no remote).

Never sent: the contents of files beyond what a call uploads, the history, the remote's URL
itself (only its hash), the conversation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from amsyos_cli.common import obj

STATE_DIR = Path(".amsyos") / "state"
PROJECT_FILE = STATE_DIR / "project.json"
GIT_TIMEOUT_S = 5
# What `.amsyos/` is, said once by the command that makes it (v3 T2: deploy made it silently).
STATE_NOTE = (
    "made .amsyos/ here: the amsyos command's record of this folder (the last deploy, the "
    "bound project, the brief, the Claude Code plugin init writes). .amsyos/state/ ignores "
    "itself in git (its own .gitignore); the plugin under .amsyos/plugins/ may be committed"
)
_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")  # git@host:org/repo.git


def git(root: Path, *args: str) -> str | None:
    """One git command's output, or None (no git, not a repository, too slow)."""
    try:
        done = subprocess.run(  # noqa: S603 - a fixed command, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 - the repository's own git
            capture_output=True,
            check=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8", "replace").strip()


@dataclass(frozen=True)
class Repo:
    root: Path
    git: bool
    branch: str | None = None
    commit: str | None = None
    dirty: bool | None = None
    remote: str | None = None
    default_branch: str | None = None

    @property
    def repo_hash(self) -> str:
        material = normalise_remote(self.remote) if self.remote else str(self.root.resolve())
        return hashlib.sha256(material.encode()).hexdigest()


def normalise_remote(url: str) -> str:
    """`https://user@Host/org/repo.git/`, `git@host:org/repo`, `ssh://git@host/org/repo`:
    one form, `host/org/repo`, so that every clone of a repository binds the same project."""
    url = url.strip()
    scp = _SCP_LIKE.match(url) if "://" not in url else None
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        rest = url.split("://", 1)[-1]
        rest = rest.rsplit("@", 1)[-1] if "@" in rest.split("/", 1)[0] else rest
        host, _, path = rest.partition("/")
        host = host.split(":", 1)[0] if host.count(":") == 1 else host  # a port
    path = path.strip("/").removesuffix(".git").strip("/")
    return f"{host.lower()}/{path}"


@cache
def find(start: str = ".") -> Repo | None:
    """The repository around `start`: git's top, or the nearest folder with a bound project
    or a card; None outside both."""
    here = Path(start).resolve()
    top = git(here, "rev-parse", "--show-toplevel")
    if top:
        root = Path(top)
        head = git(root, "rev-parse", "--abbrev-ref", "HEAD", "HEAD") or ""
        parts = head.splitlines()
        branch = parts[0] if parts and parts[0] != "HEAD" else None
        commit = parts[1] if len(parts) > 1 and re.fullmatch(r"[0-9a-f]{40}", parts[1]) else None
        status = git(root, "status", "--porcelain")
        remotes = (git(root, "remote") or "").split()
        name = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
        remote = git(root, "remote", "get-url", name) if name else None
        default = (
            git(root, "symbolic-ref", "--short", f"refs/remotes/{name}/HEAD") if name else None
        )
        if default and name and default.startswith(f"{name}/"):
            default = default[len(name) + 1 :]
        return Repo(
            root, True, branch, commit, None if status is None else bool(status), remote or None,
            default or branch,
        )  # fmt: skip
    for folder in (here, *here.parents):
        if (folder / PROJECT_FILE).is_file() or (folder / "amsyos.yaml").is_file():
            return Repo(folder, False)
    return None


# ---------------------------------------------------------------- the CLI's own folder
def state_dir(root: Path) -> tuple[Path, bool]:
    """`<root>/.amsyos/state/`, made with a `.gitignore` of its own so that git leaves it
    out without an edit of the project's; and whether `.amsyos/` was made just now."""
    made = not (root / ".amsyos").exists()
    folder = root / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        with contextlib.suppress(OSError):
            ignore.write_text(
                "# the amsyos command's own state: never committed\n*\n", encoding="utf-8"
            )
    return folder, made


# ---------------------------------------------------------------- the bound project
def read_project(root: Path, gateway: str) -> dict[str, Any] | None:
    """What `project.bind` answered for this repository at this Gateway."""
    try:
        told: Any = json.loads((root / PROJECT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = obj(obj(told).get(gateway))
    return entry if isinstance(entry.get("project"), str) else None


def write_project(root: Path, gateway: str, entry: dict[str, Any]) -> bool:
    """Kept per Gateway; True when `.amsyos/` was made by this write."""
    path = root / PROJECT_FILE
    current: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        current = obj(json.loads(path.read_text(encoding="utf-8")))
    current[gateway] = entry
    _, made = state_dir(root)
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return made


def source(gateway: str, start: str = ".") -> dict[str, Any] | None:
    """`source: {project, branch, commit, dirty}` of a call made here; None outside a
    repository. Provenance only: AMSYOS trusts it for nothing else."""
    repo = find(start)
    if repo is None:
        return None
    told: dict[str, Any] = {}
    bound = read_project(repo.root, gateway)
    if bound:
        told["project"] = str(bound["project"])[:40]
    if repo.branch:
        told["branch"] = repo.branch[:128]
    if repo.commit:
        told["commit"] = repo.commit
    if repo.dirty is not None:
        told["dirty"] = repo.dirty
    return told or None
