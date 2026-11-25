"""git repositories on AMSYOS from the terminal: plain git, with the AMSYOS login.

- `amsyos repo setup`: git asks this command for the login of the Gateway's host (a git
  credential helper in the global git config, for that host alone);
- `amsyos repo create <name> [--push]`: a private repository of the organisation
  (`repo.create`), this folder's `origin` when it has none, its branch pushed with `--push`;
- `amsyos repo clone <repo> [dir]`: `git clone` of one of the organisation's, or of another
  organisation's that granted this workspace (`t_x/name`);
- `amsyos repo ls`: the repositories this workspace can use (`repo.list`);
- `amsyos repo pr --title T`: this branch pushed, then a pull request of it (`repo.pr_open`).
Everything else is `amsyos run repo.<verb> …` (`amsyos discover repo`).

`amsyos git-credential get` is what git runs: it answers the token of the login for the
Gateway's own host, and nothing for any other; it never writes the token anywhere. git keeps it
only as long as the command runs.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from amsyos_cli.api import Api
from amsyos_cli.common import CliError, Out, credentials, obj, seq, short

GIT_USER = "amsyos"  # git's user name for the login; the Gateway reads only the password


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - git, with arguments of ours
            ["git", *args],  # noqa: S607 - the git on PATH
            cwd=cwd, capture_output=True, text=True, check=False, timeout=600,
        )  # fmt: skip
    except FileNotFoundError:
        raise CliError("NO_GIT", "git is not installed here: install git, then retry") from None
    if check and done.returncode != 0:
        raise CliError("GIT_FAILED", f"git {args[0]}: {short(done.stderr.strip(), 600)}")
    return done.stdout.strip()


def _host(gateway: str) -> str:
    """`https://host[:port]` of the Gateway: what git's credential context names."""
    parts = urlsplit(gateway)
    return f"{parts.scheme}://{parts.netloc}"


def _helper() -> str:
    """How git runs this command: by name when it is on PATH, else by its full path."""
    if shutil.which("amsyos"):
        return "!amsyos git-credential"
    me = Path(sys.argv[0]).resolve().as_posix()
    return f'!"{me}" git-credential'


def setup_git(gateway: str) -> str:
    """The credential helper for the Gateway's host, in the global git config: any other
    helper is reset for that host alone (none of them keeps the token). Idempotent."""
    key = f"credential.{_host(gateway)}.helper"
    helper = _helper()
    had = _git("config", "--global", "--get-all", key, check=False).splitlines()
    if had != ["", helper]:
        _git("config", "--global", "--unset-all", key, check=False)
        _git("config", "--global", "--add", key, "")
        _git("config", "--global", "--add", key, helper)
    return key


# ---------------------------------------------------------------- git credential helper
async def cmd_git_credential(args: argparse.Namespace, _out: Out) -> int:
    """git's credential protocol: `get` answers the login for the Gateway's host only;
    `store` and `erase` keep nothing."""
    if args.action != "get":
        sys.stdin.read()
        return 0
    asked: dict[str, str] = {}
    for line in sys.stdin.read().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            asked[key.strip()] = value.strip()
    try:
        creds = credentials()
    except CliError as err:
        sys.stderr.write(f"amsyos: {err.hint}\n")
        return 0
    if f"{asked.get('protocol')}://{asked.get('host')}" != _host(creds.gateway):
        return 0  # another host's: git asks its other helpers
    sys.stdout.write(f"username={GIT_USER}\npassword={creds.token}\n")
    return 0


# ---------------------------------------------------------------- amsyos repo
def _remote_repo(cwd: Path, gateway: str) -> tuple[str, str] | None:
    """(remote, `t_x/name`) of this clone's remote on the Gateway, origin first."""
    names = _git("remote", cwd=cwd, check=False).split()
    for name in sorted(names, key=lambda n: n != "origin"):
        url = _git("remote", "get-url", name, cwd=cwd, check=False)
        if url.startswith(_host(gateway) + "/git/"):
            path = url.split("/git/", 1)[1].removesuffix("/").removesuffix(".git")
            if path.count("/") == 1:
                return name, path
    return None


def _repo_lines(one: dict[str, Any]) -> str:
    return (
        f"{one.get('repo')}  {one.get('access')}  {one.get('size_mb')} MB  "
        f"{one.get('open_prs')} open PR(s)  {one.get('clone_url')}"
    )


async def cmd_repo(args: argparse.Namespace, out: Out) -> int:
    creds = credentials()
    verb = args.repo_cmd
    if verb == "setup":
        key = setup_git(creds.gateway)
        out.emit(
            {"git_config": key, "helper": "amsyos git-credential"},
            f"git logs in to {_host(creds.gateway)} with your AMSYOS login ({key}); nothing is "
            "stored. Now plain git clone, fetch and push work.",
        )
        return 0
    async with Api(creds, out) as api:
        if verb == "ls":
            data = await api.instant("repo.list")
            rows = [obj(r) for r in seq(data.get("repos"))]
            org = obj(data.get("your_organisation"))
            lines = [_repo_lines(r) for r in rows] or [
                "no repository yet: amsyos repo create <name>"
            ]
            lines.append(f"your organisation: {org.get('used_mb')} of {org.get('quota_mb')} MB")
            out.emit(data, *lines)
            return 0
        if verb == "create":
            return await _create(api, args, out, creds.gateway)
        if verb == "clone":
            data = await api.instant("repo.show", {"repo": args.repo})
            url = str(data.get("clone_url") or "")
            setup_git(creds.gateway)
            target = args.dir or url.rsplit("/", 1)[-1].removesuffix(".git")
            _git("clone", "--quiet", url, target)
            out.emit(
                {**data, "dir": target},
                f"cloned {data.get('repo')} into {target}/ ({data.get('access')}; default "
                f"branch {data.get('default_branch')})",
            )
            return 0
        if verb == "pr":
            return await _pr(api, args, out, creds.gateway)
    raise CliError("INVALID_INPUT", "amsyos repo setup|create|clone|ls|pr")


async def _create(api: Api, args: argparse.Namespace, out: Out, gateway: str) -> int:
    params: dict[str, Any] = {"name": args.name}
    if args.description:
        params["description"] = args.description
    data = await api.instant("repo.create", params)
    url = str(data.get("clone_url") or obj(data.get("outputs")).get("clone_url") or "")
    setup_git(gateway)
    lines = [f"created {data.get('repo')} (private, in {data.get('workspace')}): {url}"]
    here = Path.cwd()
    if _git("rev-parse", "--is-inside-work-tree", cwd=here, check=False) == "true":
        names = _git("remote", cwd=here, check=False).split()
        remote = "origin" if "origin" not in names else "amsyos"
        if remote in names:
            lines.append(f"this folder has remotes {names}: add it yourself (git remote add)")
        else:
            _git("remote", "add", remote, url, cwd=here)
            lines.append(f"this folder's remote '{remote}' is it")
            if args.push:
                _git("push", "--quiet", "-u", remote, "HEAD", cwd=here)
                lines.append(f"pushed {_git('branch', '--show-current', cwd=here)} to it")
    lines.append(
        "git works with your AMSYOS login; pull requests: amsyos repo pr, then amsyos run "
        "repo.pr_review / repo.pr_merge"
    )
    out.emit(data, *lines)
    return 0


async def _pr(api: Api, args: argparse.Namespace, out: Out, gateway: str) -> int:
    here = Path.cwd()
    found = _remote_repo(here, gateway)
    if found is None:
        raise CliError(
            "NOT_A_CLONE",
            "this folder is no clone of an AMSYOS repository: amsyos repo clone <repo>, or "
            "amsyos run repo.pr_open repo=<repo> head=<branch> title=…",
        )
    remote, repo = found
    head = args.head or _git("branch", "--show-current", cwd=here)
    if not head:
        raise CliError("NO_BRANCH", "check out the branch with your changes first (git switch)")
    setup_git(gateway)
    if not args.no_push:
        _git("push", "--quiet", "-u", remote, head, cwd=here)
    params: dict[str, Any] = {"repo": repo, "head": head, "title": args.title}
    for key in ("base", "body", "reviewers"):
        if getattr(args, key):
            params[key] = getattr(args, key)
    data = await api.instant("repo.pr_open", params)
    out.emit(
        data,
        f"pull request #{data.get('pr')} of {repo}: {head} → {data.get('base')}"
        + (
            f"; review asked of {', '.join(map(str, seq(data.get('review_asked'))))}"
            if seq(data.get("review_asked"))
            else ""
        ),
        f"next: another principal runs amsyos run repo.pr_review repo={repo} pr={data.get('pr')} "
        "verdict=approve, then repo.pr_merge",
    )
    return 0


def add_parsers(add: Any, common: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """`amsyos repo …` and `amsyos git-credential` (main.py's `add`)."""
    made: dict[str, argparse.ArgumentParser] = {}
    p = add(
        "repo", cmd_repo,
        "git repositories on AMSYOS: setup (git logs in), create, clone, ls, pr",
        "git repositories your organisation's AI clients work on together (and other "
        "organisations, by repo.grant): plain git with your AMSYOS login. setup: git asks "
        "amsyos for the login of the Gateway's host; create: a private repository, this "
        "folder's origin; clone: one of yours, or t_x/name granted to you; ls; pr: push this "
        "branch and open a pull request. Reviews, merges, issues, grants: amsyos run repo.<verb> "
        "(amsyos discover repo)",
    )  # fmt: skip
    verbs = p.add_subparsers(dest="repo_cmd", metavar="setup|create|clone|ls|pr", required=True)
    verbs.add_parser("setup", parents=[common], help="let git log in with your AMSYOS login")
    pc = verbs.add_parser("create", parents=[common], help="a new private repository")
    pc.add_argument("name", help="letters, digits, . _ -: e.g. shop-api")
    pc.add_argument("--description", help="one line on what it holds")
    pc.add_argument("--push", action="store_true", help="push this folder's branch to it")
    pk = verbs.add_parser("clone", parents=[common], help="git clone a repository you can use")
    pk.add_argument("repo", help="name (your organisation's) or t_x/name (granted to you)")
    pk.add_argument("dir", nargs="?", help="the folder (default: its name)")
    verbs.add_parser("ls", parents=[common], help="the repositories this workspace can use")
    pp = verbs.add_parser("pr", parents=[common], help="push this branch, open a pull request")
    pp.add_argument("--title", required=True, help="what it changes, in one line")
    pp.add_argument("--body", help="why and how; what to check")
    pp.add_argument("--base", help="the branch to merge into (default: the default branch)")
    pp.add_argument("--head", help="the branch (default: the one checked out)")
    pp.add_argument("--reviewers", help="u_…/a_… asked to review, comma-separated")
    pp.add_argument("--no-push", action="store_true", help="the branch is pushed already")
    made["repo"] = p
    g = add(
        "git-credential",
        cmd_git_credential,
        "git's credential helper for AMSYOS's repositories (git runs it; amsyos repo setup)",
    )
    g.add_argument("action", choices=["get", "store", "erase"])
    made["git-credential"] = g
    return made


__all__ = ["add_parsers", "cmd_git_credential", "cmd_repo", "setup_git"]
