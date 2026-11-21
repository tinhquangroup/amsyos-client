"""`amsyos result <job> --apply` (spec v3 §7.4): an output of means `patch` is a unified
diff against the commit the job was given. AMSYOS never writes into a working tree: the
CLI names the files the patch touches, then runs `git apply --3way` in this repository.
Conflicts are the agent's to resolve. A patch that reaches outside the repository or into
`.git` is refused before git sees it.
"""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath
from typing import Any

from amsyos_cli import repo
from amsyos_cli.api import Api
from amsyos_cli.common import CliError, Out, obj, short
from amsyos_cli.files import Saved

PATCH_SUFFIXES = (".patch", ".diff")
PATCH_TYPES = ("text/x-diff", "text/x-patch")
MOST_PATCH_BYTES = 20 * 1024 * 1024


async def means_of(api: Api, result: dict[str, Any]) -> dict[str, str]:
    """What each output of the job is, from its service's card: `asset.trace` names the
    service@version of an output, `describe` its `means`. Empty where either is missing."""
    assets = obj(result.get("assets"))
    first = next((v for v in assets.values() if isinstance(v, str)), None)
    if first is None:
        return {}
    traced = await api.maybe("asset.trace", {"asset": first})
    if not traced:
        return {}
    service = traced.get("service") or obj(traced.get("job")).get("service")
    version = traced.get("version") or obj(traced.get("job")).get("version")
    if not isinstance(service, str):
        return {}
    ref = service if "@" in service or not isinstance(version, str) else f"{service}@{version}"
    try:
        described = await api.describe(ref)
    except CliError:
        return {}
    return {k: str(v) for k, v in obj(described.get("means")).items()}


def patches(saved: list[Saved], means: dict[str, str]) -> list[Saved]:
    return [
        s
        for s in saved
        if means.get(s.output, "").split(".")[0] == "patch"
        or s.path.suffix.lower() in PATCH_SUFFIXES
        or s.type in PATCH_TYPES
    ]


def touched(text: str) -> list[str]:
    """The paths a unified diff names, old and new, in order."""
    found: list[str] = []
    for line in text.splitlines():
        path: str | None = None
        if line.startswith(("--- ", "+++ ")):
            path = line[4:].split("\t", 1)[0].strip().strip('"')
            if path == "/dev/null":
                continue
            if path.startswith(("a/", "b/")):
                path = path[2:]
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            path = line.split(" ", 2)[2].strip()
        if path and path not in found:
            found.append(path)
    return found


def unsafe(paths: list[str]) -> list[str]:
    bad: list[str] = []
    for path in paths:
        parts = PurePosixPath(path).parts
        if path.startswith(("/", "\\")) or ".." in parts or (parts and parts[0] == ".git"):
            bad.append(path)
    return bad


def apply(out: Out, found: list[Saved]) -> tuple[int, dict[str, Any], list[str]]:
    """Each patch applied with `git apply --3way` at the repository's top."""
    where = repo.find(".")
    if where is None or not where.git:
        raise CliError(
            "NOT_A_REPOSITORY", "--apply runs git apply in a git repository: cd into it first"
        )
    if not found:
        raise CliError(
            "NO_PATCH",
            "no output of this job is a patch (means patch, or a .patch/.diff file); "
            "amsyos result <job> --list names its outputs",
        )
    lines: list[str] = []
    applied: list[dict[str, Any]] = []
    code = 0
    for saved in found:
        if saved.size > MOST_PATCH_BYTES:
            raise CliError("PATCH_TOO_LARGE", f"{saved.path.name} is over 20 MB: apply it by hand")
        paths = touched(saved.path.read_text(encoding="utf-8", errors="replace"))
        bad = unsafe(paths)
        if bad:
            raise CliError(
                "UNSAFE_PATCH",
                f"{saved.path.name} reaches outside the repository or into .git "
                f"({', '.join(bad[:5])}): not applied",
            )
        lines.append(f"patch {saved.output} ({saved.path.name}) touches {len(paths)} files:")
        lines += [f"  {p}" for p in paths]
        done = subprocess.run(  # noqa: S603 - a fixed command, no shell
            ["git", "-C", str(where.root), "apply", "--3way", str(saved.path.resolve())],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=120,
        )
        said = (done.stderr or done.stdout).decode("utf-8", "replace").strip()
        state = "applied" if done.returncode == 0 else "conflicts or refused"
        if done.returncode != 0:
            code = 1
        lines.append(f"  git apply --3way: {state}" + (f": {short(said, 600)}" if said else ""))
        applied.append({"output": saved.output, "files": paths, "state": state, "git": said})
    if code:
        lines.append("resolve the conflicts (git status), or git apply --3way by hand")
    else:
        out.debug(f"applied in {where.root}")
    return code, {"applied": applied, "repository": str(where.root)}, lines
