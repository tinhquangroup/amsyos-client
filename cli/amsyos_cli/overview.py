"""`amsyos whoami`: where the caller stands, in one answer (Codex, 2026-11-20: it had to put
`whoami`, `space show`, `sync` and several `discover`s together to see it).

Who, the workspace and the budget (`session.show`), then one line each for the space
(`space.show`), the project bound here (read from this folder, no call), and "Try now:" a
few services of the kernel's starting list (`amsyos.discover` with no query), and a line
when AMSYOS's team replied lately to the workspace's feedback (`feedback.list`; silent when
it fails). Free calls only, all at once; one that fails is said on its line and the rest
goes on. `login` ends with it, and `sync` says it where no project is bound.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from amsyos_cli import repo
from amsyos_cli.api import Api
from amsyos_cli.common import CliError, Credentials, amount, obj, seq, short
from amsyos_cli.feedback import replied_line
from amsyos_cli.shell import cli_hint
from amsyos_cli.space import overview_line

TRY_NOW = 5  # services of the starting list said under "Try now:"
LISTED = 10  # read of that list, to leave out what follows
# What this command calls by itself behind one of its own commands (whoami, space show,
# files, init, sync, why, card): the workspace "used" them through the CLI, but they are no
# services to try (the t4 desk's list began with session.show, whoami's own call).
CLI_OWN = frozenset({
    "session.show", "session.revoke", "workspace.list", "space.show", "asset.has",
    "asset.url", "asset.list", "project.bind", "project.brief", "project.why",
    "service.check", "capability.contract",
})  # fmt: skip
NOT_BOUND = "not bound: `amsyos init` in a repository binds it (its memory, its brief)"


async def session_show(api: Api) -> dict[str, Any]:
    """`session.show`; before sessions exist on this Gateway, who `workspace.list` says."""
    try:
        return await api.instant("session.show")
    except CliError as exc:
        if exc.code != "SERVICE_NOT_FOUND":
            raise
    listed = await api.instant("workspace.list")
    you = obj(listed.get("you"))
    return {"session": None, "actor": you.get("id"), "you": you, "workspace": listed.get("current")}


def public(shown: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in shown.items() if k not in {"outputs", "job_id"}}


NO_CREDIT = "only free services run until credit is added"


def _short_of_credit(shown: dict[str, Any], credit: dict[str, Any]) -> bool:
    """Too little credit for a paid job: `credit_note` is said (less is available than a
    short GPU job holds as it starts, kernel/sessions.py), or nothing is available."""
    note = shown.get("credit_note")
    if isinstance(note, str) and note:
        return True
    available = credit.get("available")
    if isinstance(available, bool) or not isinstance(available, int | float):
        return False
    return available <= 0


def credit_lines(shown: dict[str, Any]) -> list[str]:
    """The organisation's credit (`credit`, read as account.balance reads it) and what to
    do when it has none for paid services (`credit_note`), where `session.show` says them:
    ChatGPT (2026-11-20) read the session's "5.0 left" as money its organisation had none of.
    Short of credit, the line says so at a glance (Claude Code, 2026-11-20: a session's
    budget of 5 in an organisation with 0): `organisation credit: 0 available: only free
    services run until credit is added`, the kernel's note after it."""
    credit = obj(shown.get("credit"))
    note = shown.get("credit_note")
    lines: list[str] = []
    if _short_of_credit(shown, credit):
        if credit:
            figure = amount(credit.get("available"))
        else:  # not a manager: no figure, only the kernel's note ("no" or "too little")
            figure = "0" if isinstance(note, str) and "has no credit" in note else "too little"
        lines.append(f"organisation credit: {figure} available: {NO_CREDIT}")
    elif credit:
        lines.append(
            f"organisation credit: {amount(credit.get('available'))} available (balance "
            f"{amount(credit.get('balance'))}, held {amount(credit.get('held'))})"
        )
    if isinstance(note, str) and note:
        lines.append(f"{'  ' if lines else ''}{short(note, 300)}")
    return lines


def whoami_lines(shown: dict[str, Any], creds: Credentials) -> list[str]:
    lines: list[str] = []
    if shown.get("session"):
        lines.append(
            f"session {shown['session']}: agent {shown.get('agent')} of {shown.get('person')}, "
            f"workspace {shown.get('workspace')}, host {shown.get('host')}"
        )
        if shown.get("budget") is not None:
            left = f" ({amount(shown['left'])} left)" if shown.get("left") is not None else ""
            lines.append(
                f"session budget {amount(shown.get('budget'))} (a cap on spending, not credit), "
                f"spent {amount(shown.get('spent'))}{left}, at most "
                f"{amount(shown.get('per_hour'))} an hour; until {shown.get('expires_at')}"
            )
        lines += credit_lines(shown)
        grant = obj(shown.get("grant"))
        if grant:
            lines.append(
                f"may: {', '.join(map(str, seq(grant.get('may')))) or 'run services only'}"
            )
            lines.append(f"services: {', '.join(map(str, seq(grant.get('services')))) or '*'}")
    else:
        you = obj(shown.get("you"))
        who = shown.get("actor") or you.get("id")
        kind = you.get("kind", "principal")
        lines.append(f"{who} ({kind}), workspace {shown.get('workspace')}; no session")
        lines += credit_lines(shown)
    lines.append(
        f"gateway {creds.gateway}" + (" (token from AMSYOS_TOKEN)" if creds.from_env else "")
    )
    return lines


def project_here(root: Path, gateway: str) -> dict[str, Any]:
    """The project `amsyos init` bound this folder's repository to, read here (no call)."""
    found = repo.find(str(root))
    bound = repo.read_project(found.root, gateway) if found else None
    if bound:
        return {"bound": True, **bound, "root": str(found.root if found else root)}
    return {"bound": False, "repository": str(found.root) if found else None}


def project_line(here: dict[str, Any]) -> str:
    if here.get("bound"):
        return (
            f"project here: {here.get('project')} ({here.get('name')}), bound: amsyos sync "
            "prints its brief"
        )
    return f"project here: {NOT_BOUND}"


async def _given(shown: dict[str, Any]) -> dict[str, Any]:
    return shown


def _failed(what: str, exc: BaseException) -> tuple[dict[str, Any], str]:
    if isinstance(exc, CliError):
        return exc.answer(), f"{what}: not shown ({exc.code}: {short(cli_hint(exc.hint), 160)})"
    return {"error": type(exc).__name__}, f"{what}: not shown ({type(exc).__name__})"


async def overview(
    api: Api, root: Path, *, shown: dict[str, Any] | None = None, project: bool = True
) -> tuple[dict[str, Any], list[str]]:
    """Who and where (`session.show`, unless `shown` already is), the space, the project
    here (`project=False` leaves its line to the caller), and services to try now: the JSON
    answer and its lines. `session.show` failing fails the command, as `whoami` always did;
    the space's and the list's failures are said on their own lines."""
    who = session_show(api) if shown is None else _given(shown)
    told, space, listed, heard = await asyncio.gather(
        who,
        api.instant("space.show"),
        api.discover("", limit=LISTED),
        api.instant("feedback.list", {"limit": 5}),
        return_exceptions=True,
    )
    if isinstance(told, BaseException):
        raise told
    answer: dict[str, Any] = {"gateway": api.creds.gateway, **public(told)}
    lines = whoami_lines(told, api.creds)
    if isinstance(space, BaseException):
        answer["space"], line = _failed("space", space)
    else:
        answer["space"], line = space, overview_line(space)
    lines.append(line)
    here = project_here(root, api.creds.gateway)
    answer["project_here"] = here
    if project:
        lines.append(project_line(here))
    replied = replied_line(heard)  # silent when feedback.list fails or nothing was replied
    if replied:
        answer["feedback_replied"] = obj(heard).get("replied_recently")
        lines.append(replied)
    elif isinstance(heard, BaseException):
        api.out.debug(f"feedback.list failed ({type(heard).__name__}): no feedback line")
    if isinstance(listed, BaseException):
        answer["try_now"], line = _failed("Try now", listed)
        lines.append(line)
    else:
        hits = [obj(h) for h in seq(listed.get("results"))]
        hits = [h for h in hits if h.get("id") not in CLI_OWN][:TRY_NOW]
        answer["try_now"] = hits
        lines.append("Try now:" if hits else "Try now: nothing listed; amsyos discover <words>")
        lines += [f"  {h.get('id')}: {short(str(h.get('summary') or ''), 120)}" for h in hits]
        if hits:
            lines.append("  (amsyos describe <service> says how to call one; amsyos discover "
                         "<words> searches)")  # fmt: skip
    return answer, lines
