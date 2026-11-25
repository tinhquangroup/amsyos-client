"""`amsyos`: the commands of stages A to C (spec v3 §3.2)."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, NoReturn, cast

from amsyos_cli import __version__, around, brief, repo, standards
from amsyos_cli import adapters as hosts
from amsyos_cli import card as cards
from amsyos_cli import local as local_run
from amsyos_cli import machine as machines
from amsyos_cli import patch as patches
from amsyos_cli import setup as setups
from amsyos_cli import update as updates
from amsyos_cli.api import TERMINAL, Api, done, held_up, idempotency_key, refused_field
from amsyos_cli.common import (
    CliError,
    Credentials,
    Out,
    amount,
    credentials,
    forget_credentials,
    gateway_default,
    gateway_of,
    obj,
    plain_json,
    read_credentials_file,
    save_credentials,
    seq,
    session_ending,
    short,
)
from amsyos_cli.compare import cmd_compare
from amsyos_cli.deploy import DEPLOY_NODE_HELP, check_card, check_of, deploy, dry_run, tenant_of
from amsyos_cli.feedback import add_parser as add_feedback
from amsyos_cli.feedback import logout_line
from amsyos_cli.files import (
    describe_saved,
    download,
    face_note,
    human,
    kept_for,
    sha256_of,
    text_of,
    upload,
)
from amsyos_cli.gitrepo import add_parsers as add_git
from amsyos_cli.jobs import (
    check_lines,
    ended_well,
    eta_text,
    failed_result,
    finish,
    fixed_lines,
    follow,
    hide_left,
    parse_value,
    request_lines,
    show_full_log,
    show_logs,
    show_result,
    space_closed,
    status_lines,
    version_check_lines,
    with_basis,
    with_id,
)
from amsyos_cli.login import host_of, login
from amsyos_cli.overview import overview, session_show
from amsyos_cli.pool import cmd_pool, cmd_restore, cmd_uninstall
from amsyos_cli.shell import cli_hint, quoted
from amsyos_cli.space import cmd_node
from amsyos_cli.watch import cmd_watch
from amsyos_cli.work import cmd_promote, cmd_work

Command = Callable[[argparse.Namespace, Out], Coroutine[Any, Any, int]]
WAITING = 3  # exit code: the job waits for you, or did not end within --timeout
# `run --wait`: a job that ends this soon is answered by the run call itself; a longer one's
# estimate is said within seconds, before the wait (Codex, 2026-11-20), not after 50 s.
RUN_WAIT_S = 5
# What a started job's answer may carry in `data` (mission.run: how its ETA was reached, from
# its plan's steps): an account of the job to come, not an answer given at once.
ETA_KEYS = frozenset({"eta_basis", "eta_steps", "eta_leaves_out"})


# ---------------------------------------------------------------- identity
async def cmd_login(args: argparse.Namespace, out: Out) -> int:
    gateway = gateway_of(args.gateway) if args.gateway else gateway_default()
    creds = await asyncio.to_thread(
        login,
        gateway,
        workspace=args.workspace,
        budget=args.budget,
        hours=args.hours,
        host=args.host,
    )
    async with Api(creds, out) as api:
        shown = await session_show(api)
        if isinstance(shown.get("session"), str):
            creds.session = shown["session"]
            save_credentials(creds)
        # Codex, 2026-11-20: one overview right after login, as `whoami` gives it.
        answer, lines = await overview(api, Path.cwd(), shown=shown)
    out.emit({"logged_in": True, **answer}, *lines)
    return 0


async def cmd_setup(args: argparse.Namespace, out: Out) -> int:
    """Log in with the setup file a person downloaded at `{gateway}/start` (amsyos_cli/
    setup.py): its one code redeemed once, the token kept as `login` keeps it, then the
    overview as `whoami` gives it. Neither the code nor the token is ever printed."""
    setup = setups.given(args.file, args.code, args.gateway)
    folder = setups.ready(setup)
    try:
        creds = await asyncio.to_thread(setups.redeem_once, setup)
    except CliError as exc:  # this file, used on this machine and still its login: no error
        kept = setups.kept_for(setup) if exc.code == "SETUP_FILE_USED" else None
        if kept is None:
            raise
        return await setups.again(setup, kept, exc, out)
    try:
        path = save_credentials(creds)
    except OSError as exc:
        raise CliError(
            "SETUP_NOT_SAVED",
            f"the login worked, but its token could not be kept in {folder} "
            f"({type(exc).__name__}) and the code is used: fix that folder (or set "
            f"AMSYOS_CONFIG_DIR), then {setup.new_file}",
        ) from None
    answer: dict[str, Any] = {"logged_in": True, "gateway": creds.gateway, "kept_in": str(path)}
    lines = [
        f"logged in at {creds.gateway} with {'the setup file' if args.file else 'the code'}: "
        f"its code is used up (one file for each AI client); the session's token is in {path}"
    ]
    if os.environ.get("AMSYOS_TOKEN"):
        lines.append("AMSYOS_TOKEN is set in this environment and wins over this login: unset it")
    try:
        async with Api(creds, out) as api:
            shown = await session_show(api)
            if isinstance(shown.get("session"), str) and shown["session"] != creds.session:
                creds.session = shown["session"]
                save_credentials(creds)
            told, more = await overview(api, Path.cwd(), shown=shown)
    except CliError as exc:  # logged in all the same: the code is spent, the token kept
        answer["overview"] = exc.answer()
        said = short(cli_hint(exc.hint), 200)
        lines.append(f"overview: not shown ({exc.code}: {said}); amsyos whoami")
    else:
        answer.update(told)
        lines += more
    setups.remember(setup, creds)  # the code's hash beside its token, for `setup` again
    out.emit(answer, *lines)
    return 0


async def cmd_logout(args: argparse.Namespace, out: Out) -> int:
    creds = read_credentials_file()
    revoked = False
    if creds is not None and creds.session:
        try:
            async with Api(creds, out) as api:
                await api.instant("session.revoke", {"session": creds.session})
            revoked = True
        except CliError as exc:
            out.note(f"the session was not revoked ({exc.code}); the token ends by itself")
    removed = forget_credentials()
    lines = ["logged out" + (f": session {creds.session} revoked" if revoked and creds else "")]
    answer: dict[str, Any] = {"logged_out": removed, "revoked": revoked}
    if not removed:
        lines = ["not logged in"]
    if os.environ.get("AMSYOS_TOKEN"):
        lines.append("AMSYOS_TOKEN is still set in this environment: unset it too")
    if removed or revoked:  # the owner, 2026-11-20: the client tells the team itself
        answer["next"] = logout_line()
        lines.append(logout_line())
    out.emit(answer, *lines)
    return 0


async def cmd_whoami(args: argparse.Namespace, out: Out) -> int:
    """Where the caller stands: who, the workspace, the budget, the space, the project here
    and services to try now (amsyos_cli/overview.py)."""
    async with Api(credentials(), out) as api:
        answer, lines = await overview(api, Path.cwd())
    told = out.extra.get("update")  # a newer amsyos is out: said here, on stdout (main)
    if isinstance(told, dict):
        lines += updates.update_lines(cast("dict[str, Any]", told))
    out.emit(answer, *lines)
    return 0


# ---------------------------------------------------------------- services and jobs
_LIMIT = re.compile(r"(\d{1,7})(s|m|h)?")


def limit_of(text: str) -> int:
    """`--limit`: seconds, or a number with s, m or h (90s, 10m, 2h); at least 10 s."""
    found = _LIMIT.fullmatch(text.strip().lower())
    if found is None:
        raise argparse.ArgumentTypeError(f"{text!r}: like 90s, 10m or 2h")
    seconds = int(found.group(1)) * {"s": 1, "m": 60, "h": 3600}[found.group(2) or "s"]
    if not 10 <= seconds <= 31 * 86400:
        raise argparse.ArgumentTypeError("between 10s and 744h")
    return seconds


def _params(items: list[str]) -> tuple[dict[str, Any], dict[str, Path]]:
    """`params.json`, `-` (JSON on stdin) or `k=v …`; `k=@path` is a local file: uploaded
    for a file input, its text for any other (`_as_declared`)."""
    if len(items) == 1 and items[0] == "-":
        raw: Any = json.load(sys.stdin)
    elif len(items) == 1 and "=" not in items[0]:
        path = Path(items[0])
        if not path.is_file():
            raise CliError("INVALID_INPUT", f"'{items[0]}' is neither k=v nor a JSON file")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise CliError("INVALID_INPUT", f"{items[0]} is not JSON") from None
    else:
        raw = {}
        bad = [i for i in items if "=" not in i or not i.split("=", 1)[0]]
        if bad:
            raise CliError("INVALID_INPUT", f"params are k=v: {', '.join(bad[:5])}")
        for item in items:
            key, value = item.split("=", 1)
            raw[key] = value if value.startswith("@") else parse_value(value)
    if not isinstance(raw, dict):
        raise CliError("INVALID_INPUT", "params are a JSON object")
    params: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    local = {k: Path(v[1:]) for k, v in params.items() if isinstance(v, str) and v.startswith("@")}
    missing = [f"{k}={p}" for k, p in local.items() if not p.is_file()]
    if missing:
        raise CliError("INVALID_INPUT", f"no such file: {', '.join(missing)}")
    return params, local


def _read_as_json(items: list[str], params: dict[str, Any]) -> dict[str, str]:
    """The `k=v` whose text `parse_value` read as a number, true/false/null, an object or a
    list: their text, in case the service takes text there."""
    if len(items) == 1 and "=" not in items[0]:
        return {}
    texts = dict(i.split("=", 1) for i in items if "=" in i)
    return {k: t for k, t in texts.items() if k in params and not isinstance(params[k], str)}


async def _as_declared(
    api: Api, service: str, params: dict[str, Any], *, texts: dict[str, str],
    local: dict[str, Path],
) -> None:  # fmt: skip
    """A customer, 2026-11-23: `end=5.5` went as a number to an input that takes text, and was
    refused. A `k=v` read as JSON goes as its text where `describe` says the input takes text;
    a `k=@path` of an input that takes no file goes as the file's text, and leaves `local`
    (one describe, only when such a `k=v` or a `k=@path` is given); whatever describe cannot
    say leaves the value as it was read, a `k=@path` as a file to upload."""
    if not texts and not local:
        return
    try:
        inputs = obj((await api.describe(service)).get("inputs"))
    except CliError:
        return
    for key, text in texts.items():
        if obj(inputs.get(key)).get("type") in ("str", "enum"):
            params[key] = text
    for key in [k for k in local if k in inputs]:
        kind = str(obj(inputs[key]).get("type") or "asset")
        if kind != "asset":
            path = local.pop(key)
            params[key] = text_of(key, path, kind)
            api.out.note(f"{key}: the text of {path} ({key} takes {kind}, not a file)")


async def _send_files(api: Api, out: Out, params: dict[str, Any], local: dict[str, Path]) -> None:
    """`k=@path`: each file as an asset, sent only when the workspace lacks its content."""
    if not local:
        return
    sent = await upload(api, list(local.values()))
    for name, row in zip(local, sent, strict=True):
        params[name] = row["asset"]
        how = "already in the workspace" if row.get("reused") else "uploaded"
        out.note(f"{name}: {row['asset']} ({how}, {human(int(row['size']))})")


async def cmd_self_update(args: argparse.Namespace, out: Out) -> int:
    """`amsyos self-update` (amsyos_cli/update.py)."""
    return updates.self_update(
        out, plan=args.plan, rollback=args.rollback, if_needed=args.if_needed,
        check=args.check, force=args.force,
    )  # fmt: skip


async def cmd_install(args: argparse.Namespace, out: Out) -> int:
    """`amsyos install` (amsyos_cli/update.py), run once with `uvx --from <wheel> amsyos
    install`."""
    return updates.install(out, path=not args.no_path, keep_old=args.keep_old)


async def cmd_machine(args: argparse.Namespace, out: Out) -> int:
    """`amsyos machine`: what this computer has, and whether the card fits it."""
    root = Path(args.dir or ".").resolve()
    here = machines.probe(root)
    judged = machines.fits(cards.load(root), here) if (root / cards.CARD).is_file() else None
    out.emit({"machine": here, **(judged or {})}, *machines.lines(here, judged))
    return 0


def _run_local(args: argparse.Namespace, out: Out) -> int:
    """`run --local` (amsyos_cli/local.py): this repository's service run here, as the
    space would run it; the service's name may be left out, params k=v change the example."""
    items = [*([args.service] if args.service else []), *args.params]
    named = items.pop(0) if items and "=" not in items[0] and items[0] != "-" else None
    params, files = _params(items) if items else ({}, {})
    root = Path(args.dir or ".")
    keep = root / args.out if args.keep else None  # <repository>/out/<job>/, as run's outputs
    return local_run.run_here(
        root, out, named=named, given=params, files=files, keep=keep, force=args.force_here,
    )  # fmt: skip


async def cmd_run(args: argparse.Namespace, out: Out) -> int:
    if args.local:
        return _run_local(args, out)
    if args.keep or args.dir is not None:
        raise CliError("INVALID_INPUT", "--dir and --keep go with --local")
    if not args.service:
        raise CliError(
            "INVALID_INPUT",
            "name the service: amsyos run <service> k=v …; or amsyos run --local in a "
            "repository with amsyos.yaml",
        )
    params, local = _params(args.params)
    texts = _read_as_json(args.params, params)
    creds = credentials()
    async with Api(creds, out) as api:
        await _as_declared(api, args.service, params, texts=texts, local=local)
        # The key from the command, its params and the session: a file by its content.
        keyed = {
            k: (f"sha256:{sha256_of(local[k])}" if k in local else v) for k, v in params.items()
        }
        key = idempotency_key(args.service, keyed, creds, fresh=args.new)
        await _send_files(api, out, params, local)
        started = await api.run(
            args.service, params, key=key, wait_s=RUN_WAIT_S if args.wait else 0,
            workspace=args.workspace, limit_s=args.limit,
        )  # fmt: skip
        job_id = str(started.get("job_id"))
        for line in fixed_lines(started):  # what AMSYOS changed of the params, first
            out.note(line)
        if _answered(started):
            return _instant(out, started)
        eta, cost = with_basis(eta_text(started), started) or "eta ?", started.get("est_cost")
        said = f"{job_id}: started, {eta}, est. cost {amount(cost)}"
        left_out = obj(started.get("data")).get("eta_leaves_out")
        asked = [*([f"its eta leaves out {left_out}"] if left_out else []), *request_lines(started)]
        if asked:
            out.note("\n".join(asked))
        if not args.wait:
            out.emit(started, said, f"next: amsyos wait {job_id}")
            return 0
        if obj(started.get("status")).get("state") not in TERMINAL:
            out.note(said)  # the estimate before the wait, a machine's start apart from the run
        try:
            status = await follow(api, out, job_id, obj(started.get("status")) or None)
        except (KeyboardInterrupt, asyncio.CancelledError):
            out.note(f"stopped waiting; the job goes on: amsyos wait {job_id}")
            return 130
        code, _ = await finish(
            api, out, job_id, status, None if args.no_download else Path(args.out)
        )
        return code


async def _me(api: Api) -> set[str]:
    """Who a task's assignee names when it names this caller: the session's agent, or the
    principal of a token without a session."""
    try:
        shown = await session_show(api)
    except CliError:
        return set()
    named = (shown.get("agent"), shown.get("actor"), obj(shown.get("you")).get("id"))
    return {v for v in named if isinstance(v, str) and v}


async def cmd_wait(args: argparse.Namespace, out: Out) -> int:
    """Until the job ends, as long as it takes: through a wait for a person or another agent
    (an approval, `grant ask`'s task, a hand-over's approval), which is decided by them, not
    by asking again (v3 T5: it exited at once with 3). It stops at a wait for this caller
    itself, which only its own action ends, and at `--timeout` (exit 3)."""
    told: set[str] = set()
    async with Api(credentials(), out) as api:
        me: set[str] | None = None

        async def stop(status: dict[str, Any]) -> bool:
            nonlocal me
            if status.get("state") in TERMINAL or held_up(status):
                return True
            if not done(status):  # running, or an order that runs to its end by itself
                return False
            if me is None:
                me = await _me(api)
            entries = [obj(e) for e in seq(status.get("waiting_on"))]
            if any(e.get("assignee") in me or e.get("claimed_by") in me for e in entries):
                return True
            for entry in entries:
                said = f"{entry.get('task')}:{entry.get('assignee')}"
                if said not in told:
                    told.add(said)
                    out.note(
                        f"waiting on {entry.get('assignee') or 'somebody'} "
                        f"({entry.get('kind') or 'task'} {entry.get('task') or ''}); amsyos wait "
                        "goes on until it is decided (--timeout s stops sooner)"
                    )
            return False

        try:
            waited = follow(api, out, args.job, stop=stop)
            status = await (asyncio.wait_for(waited, args.timeout) if args.timeout else waited)
        except TimeoutError:
            now = with_id(await api.status(args.job), args.job)
            out.emit(
                {"job_id": args.job, "state": now.get("state"), "timed_out": True, "status": now},
                *status_lines(now),
                f"not ended within {args.timeout:g} s; the job goes on: amsyos wait {args.job}",
            )
            return WAITING
        except (KeyboardInterrupt, asyncio.CancelledError):
            out.note(f"stopped waiting; the job goes on: amsyos wait {args.job}")
            return 130
        mine = [obj(e) for e in seq(status.get("waiting_on"))]
        task = next((str(e.get("task")) for e in mine if e.get("task")), args.job)
        code, _ = await finish(
            api, out, args.job, status, None if args.no_download else Path(args.out),
            waiting=f"it waits for you: {task}; act on it as `waiting on` says (task.decide "
            f"{{task: '{task}', decision: …}}), then amsyos wait {args.job}",
            check=await check_of(api, args.job, status) if status.get("state") in TERMINAL
            else None,
        )  # fmt: skip
    return code


def _answered(started: dict[str, Any]) -> bool:
    """A service the kernel answered in `run` itself (its `outputs`, its `data`), not a job
    started whose `data` says only how its ETA was reached (`ETA_KEYS`)."""
    data = started.get("data")
    if started.get("outputs"):
        return True
    if isinstance(data, dict) and data:
        return bool(set(obj(data)) - ETA_KEYS)
    return data is not None


SAID_KEYS = frozenset({"hint", "next", "how"})  # an answer's words of what to do next


def _said_in_cli(value: Any, depth: int = 0) -> Any:
    """An answer for a line of text: what it says to do next (`hint`, `next`, `how`) with
    the calls it names as `amsyos` commands (Claude Code, 2026-11-20: a gap's `next` said
    `capability.request {gap: …}`). `--json` keeps them as they came."""
    if depth > 16:
        return value
    if isinstance(value, list):
        return [_said_in_cli(v, depth + 1) for v in seq(value)]
    if not isinstance(value, dict):
        return value
    return {
        k: cli_hint(v) if k in SAID_KEYS and isinstance(v, str) else _said_in_cli(v, depth + 1)
        for k, v in obj(value).items()
    }


def _instant(out: Out, started: dict[str, Any]) -> int:
    """A service the kernel answered at once: its data, and its outputs (a file's link
    hidden, anything else as it is: a task, a worker, an id)."""
    outputs = obj(started.get("outputs"))
    lines = [f"{started.get('job_id')}: answered"]
    if started.get("data") is not None:
        # Its numbers in fixed decimals; its hints and next steps as `amsyos` commands.
        lines.append(plain_json(_said_in_cli(started["data"]), indent=2))
    for name, value in outputs.items():
        lines.append(f"{name}: {out.value(value)}")
    lines += request_lines(started)  # a plan's: whether it meets what was asked
    shown = dict(started)
    if outputs:
        shown["outputs"] = out.outputs(outputs)
    out.emit(shown, *lines)
    return 0


async def cmd_status(args: argparse.Namespace, out: Out) -> int:
    if args.clear or not around.is_job(args.job):  # `status "<doing>"`: what you are doing
        return await around.cmd_doing(args, out)
    async with Api(credentials(), out) as api:
        if args.wait:
            status = await follow(api, out, args.job)
        else:
            status = await api.status(args.job)
        status = {"job_id": args.job, **status}
        ended = status.get("state") in TERMINAL
        check = await check_of(api, args.job, status) if ended else None
    shown = hide_left(out, status)
    result = obj(shown.get("result"))
    if result and result.get("outputs"):
        shown["result"] = {**result, "outputs": out.outputs(obj(result["outputs"]))}
    if space_closed(status):  # a space's job after its close: its ordinary end (v3 T2)
        shown["space_closed"] = True
    if check:
        shown["check"] = check
    out.emit(shown, *status_lines(status), *version_check_lines(check))
    state = status.get("state")
    return (
        0
        if ended_well(status) or state in {"queued", "provisioning", "running"}
        else (WAITING if state == "waiting" else 1)
    )


async def cmd_result(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        try:
            result = await api.result(args.job)
        except CliError as exc:
            if exc.code not in {"JOB_FAILED", "JOB_CANCELLED"}:
                raise
            return failed_result(out, args.job, exc)
        if not args.apply:
            shown = await show_result(
                out, args.job, result, Path(args.out),
                download_files=not (args.list or args.no_download),
                check=await check_of(api, args.job, result),
            )  # fmt: skip
            return shown.code
        shown = await show_result(out, args.job, result, Path(args.out), emit=False)
        found = patches.patches(shown.saved, {})
        if not found:
            found = patches.patches(shown.saved, await patches.means_of(api, result))
    code, applied, lines = patches.apply(out, found)
    out.emit({**shown.answer, **applied}, *shown.lines, *lines)
    return code


async def cmd_cancel(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        told = await api.cancel(args.job)
    done = "cancelled" if told.get("cancelled") else "not cancelled"
    line = f"{args.job}: {done}, {told.get('state')}"
    out.emit(told, line, *([cli_hint(str(told["note"]))] if told.get("note") else []))
    return 0


async def cmd_logs(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        if args.full:
            return await show_full_log(api, out, args.job, Path(args.out))
        return await show_logs(api, out, args.job, follow_it=args.follow)


def _need(path: str) -> dict[str, Any]:
    """A typed need (spec v3 §6) as the kernel's resolver takes it (kernel/need.py
    `parse_need`): from a JSON file, `-` (stdin) or the JSON itself; the need, `{"need": …}`,
    or the need as JSON text. Its problems are the kernel's to say, every one at once."""
    inline = path.lstrip().startswith("{")
    try:
        if path == "-":
            raw: Any = json.load(sys.stdin)
        else:
            raw = json.loads(path if inline else Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and set(obj(raw)) == {"need"}:
            raw = obj(raw)["need"]
        if isinstance(raw, str):
            raw = json.loads(raw)
    except OSError:
        raise CliError("INVALID_INPUT", f"no file {path}") from None
    except ValueError:
        what = "the need given" if inline else path
        raise CliError("INVALID_INPUT", f"{what} is not JSON") from None
    need = obj(raw)
    if not need:
        raise CliError(
            "INVALID_INPUT",
            'a need is {"inputs": {...}, "outputs": {...}, "constraints": {...}} (spec v3 §6)',
        )
    return need


# Said where an answer gives neither a time nor a cost (a customer's feedback, 2026-11-21: a
# hit with no figures printed nothing at all).
NO_ESTIMATE = "no estimate yet"


def _fill_text(entry: dict[str, Any]) -> str:
    """One thing a plan leaves for the caller to give (the planner's `fill`): a step's
    param (`2.text`) or one of the plan's inputs, with what it wants."""
    if entry.get("step") and entry.get("param"):
        name = f"{entry['step']}.{entry['param']}"
    else:
        name = str(entry.get("input") or entry.get("param") or "?")
    said = entry.get("give") or entry.get("how") or entry.get("must") or entry.get("write")
    return f"{name} ({short(str(said), 80)})" if said else name


def _plan_lines(plans: list[Any]) -> list[str]:
    """Each plan: its quote ("no estimate yet" where it has none), why, its steps, and what
    the caller still gives (`fill`) or the plan does not meet (`unmet`): a customer's
    feedback (2026-11-21) took a plan whose step needs a text nobody had given."""
    lines: list[str] = []
    for n, given in enumerate(plans, 1):
        plan = obj(given)
        quote = obj(plan.get("quote"))
        bits = [f"plan {n}:"]
        if quote.get("cost") is not None:
            bits.append(f"cost {amount(quote['cost'])}")
        eta = with_basis(eta_text(quote), quote, counts_runs=True)
        if eta:
            bits.append(eta)
        if quote.get("cost") is None and not eta:
            bits.append(NO_ESTIMATE)
        if quote.get("n"):
            bits.append(f"(from {quote['n']} runs)")
        lines.append(" ".join(bits) + (f"  {short(plan['why'], 160)}" if plan.get("why") else ""))
        for i, step in enumerate(seq(plan.get("steps")), 1):
            entry = obj(step)
            params = obj(entry.get("params"))
            lines.append(
                f"  {i}. {entry.get('service')}" + (f"  {short(params, 200)}" if params else "")
            )
        fills = [_fill_text(obj(f)) for f in seq(plan.get("fill")) if obj(f)]
        if fills:
            lines.append(f"  you give: {'; '.join(fills[:8])}")
        unmet = [
            short(u if isinstance(u, str) else plain_json(u), 120) for u in seq(plan.get("unmet"))
        ]
        if unmet:
            lines.append(f"  unmet: {'; '.join(unmet[:8])}")
    return lines


def _hit_lines(hit: dict[str, Any]) -> list[str]:
    """A service found: its id; whether it would run now (`ready: false` with `to_run`,
    its first call's time with the machine's start in it, `start_s`); its estimate (said a
    default where no run is behind it, "no estimate yet" where it has none), cost and
    effect; that a person reviews where one acts in it; its summary below. A customer's
    feedback (2026-11-21): the line hid that its own service waited for its space."""
    cost = hit.get("est_cost")
    eta = with_basis(eta_text(hit), hit, counts_runs=True)
    bits: list[str] = []
    if hit.get("ready") is False:
        need = cli_hint(str(hit.get("to_run") or "describe says what it needs"))
        bits.append(f"not ready: {short(need, 200)}" + (f"; first call {eta}" if eta else ""))
    elif eta:
        bits.append(eta)
    if cost is not None:
        bits.append(f"cost {amount(cost)}")
    if not eta and cost is None:
        bits.append(NO_ESTIMATE)
    bits.append(str(hit.get("effect") or ""))
    if hit.get("person_step"):
        bits.append("a person reviews")
    return [
        f"{hit.get('id')}  {'  '.join(b for b in bits if b)}",
        f"    {short(str(hit.get('summary', '')), 160)}",
    ]


def _group_of(hit: dict[str, Any]) -> str:
    """A hit's group in the starting list: its first tag where the answer gives tags, else
    its family (`asset.upload`: asset)."""
    tags = [str(t) for t in seq(hit.get("tags")) if t]
    return tags[0] if tags else str(hit.get("id") or "?").split(".", 1)[0]


# A query that finds a service in every workspace: AMSYOS ships only its platform's own
# services; those of a field are organisations' own or offered to them. Its words are all
# in asset.fetch's summary, a read that changes nothing.
EXAMPLE_QUERY = "bring a file from a URL"
# Meaning search works across languages (kernel/search.py); the kernel says the same.
LANGUAGES = "any language; English works best"
SEARCH_LINE = (
    f"amsyos discover \"<words>\" searches AMSYOS's platform services, your organisation's own "
    f'and those other organisations offer it ({LANGUAGES}), e.g. "{EXAMPLE_QUERY}"'
)
# The starting list's note says how many of its first hits this workspace used most (the
# kernel's `_starting`): those, then the starting points, are listed under two headings.
_USED_MOST = re.compile(r"used most here: (\d+)")


def _starting_lines(hits: list[dict[str, Any]], note: str, skip: int) -> list[str]:
    """`discover` without words: what the workspace used most, then the starting points,
    in the kernel's order, under those two headings where its note says how many are which;
    else grouped by tag or family as before."""
    lines: list[str] = []
    found = _USED_MOST.search(note) if not skip else None
    if found is not None:
        used = int(found[1])
        for name, members in (("used most here", hits[:used]), ("starting points", hits[used:])):
            if members:
                lines.append(f"{name}:")
                lines += [f"  {line}" for hit in members for line in _hit_lines(hit)]
        return lines
    lines.append("where to start: what this workspace used most, then the starting points")
    groups: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        groups.setdefault(_group_of(hit), []).append(hit)
    for name, members in groups.items():
        lines.append(f"{name}:")
        lines += [f"  {line}" for hit in members for line in _hit_lines(hit)]
    return lines


async def cmd_discover(args: argparse.Namespace, out: Out) -> int:
    asked: dict[str, Any] = {}
    # Sent as given, 0 too: the kernel takes, cuts or refuses a page's size and says so
    # (a customer's feedback, 2026-11-21: `--limit 0` was dropped here and read as 5).
    if args.limit is not None:
        asked["limit"] = args.limit
    if args.automatic:
        asked["automatic"] = True
    if args.skip is not None:
        asked["skip"] = args.skip
    if args.need:
        asked["need"] = _need(args.need)
    async with Api(credentials(), out) as api:
        try:
            found = await api.discover(" ".join(args.words), **asked)
        except CliError as exc:
            # Only a Gateway that does not take `need` at all is one before stage B; a
            # problem of the need itself is the kernel's own answer (v3 T5).
            if "need" in asked and refused_field(exc, "need"):
                raise CliError(
                    "NEED_NOT_HERE",
                    "this Gateway takes no typed need yet (v3 stage B): amsyos discover <words>",
                ) from None
            raise
    starting = not " ".join(args.words).strip() and not args.need
    lines = _plan_lines(seq(found.get("plans")))
    # Every hit the kernel returned, never cut here: it sets the page's size.
    hits = [obj(h) for h in seq(found.get("results"))]
    note = str(found.get("note") or "")
    if starting:
        # Codex, 2026-11-20: no words were refused here although the kernel answers them
        # with where to start (`Kernel._starting`).
        lines += _starting_lines(hits, note, int(args.skip or 0))
    else:
        lines += [line for hit in hits for line in _hit_lines(hit)]
    if not lines:
        lines.append("nothing found" + (" that meets the need" if args.need else ""))
    if found.get("more"):
        lines.append(f"{found['more']} more: --skip {int(args.skip or 0) + len(hits)}")
    if note:  # what the kernel says of the page: its counts, a size it cut, how to go on
        lines.append(f"note: {cli_hint(note)}")
    if starting:
        lines.append(SEARCH_LINE)
    out.emit(found, *lines)
    return 0


def _as_arg(key: str, value: Any) -> str:
    """One `k=v` of `amsyos run` that reads back as this value, quoted as PowerShell and a
    POSIX shell both read it (amsyos_cli/shell.py `quoted`)."""
    if isinstance(value, str):
        text = value
        try:
            json.loads(value)
            text = json.dumps(value)  # "12", "true": kept as text
        except ValueError:
            pass
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return quoted(f"{key}={text}")


FILLER_MOST = 60  # an input's own example longer than this is no part of a run line


def _filler(spec: dict[str, Any]) -> Any:
    """What a run line gives an input it must name: its default, its first value, its own
    short example, a local file to upload (`@<file>`), or `<type>` to fill in."""
    if spec.get("default") is not None:
        return spec["default"]
    values = seq(spec.get("values"))
    if values:
        return values[0]
    example = spec.get("example")
    if isinstance(example, int | float | bool) or (
        isinstance(example, str) and example and len(example) <= FILLER_MOST and "\n" not in example
    ):
        return example
    if spec.get("type") == "asset":
        return "@<file>"
    return f"<{spec.get('type') or 'value'}>"


def run_line(told: dict[str, Any]) -> str:
    """One ready `amsyos run <id> k=v …` (Claude Code, 2026-11-20: `describe` of a builtin
    printed none): the service's example where it has one; else its required inputs, or,
    with none required, the first text input without a default (`audio.tts`: its text),
    each filled as `_filler` says."""
    service = str(told.get("id") or "?")
    example = obj(told.get("example"))
    if example:
        service = str(example.get("service_id") or service)
        args = [_as_arg(str(k), v) for k, v in obj(example.get("params")).items()]
    else:
        inputs = {str(k): obj(v) or {"type": v} for k, v in obj(told.get("inputs")).items()}
        named = [k for k, spec in inputs.items() if spec.get("required")]
        if not named:  # a text first (mission.plan's goal, not its aim), else any
            bare = [k for k, spec in inputs.items() if spec.get("default") is None]
            named = ([k for k in bare if inputs[k].get("type") == "str"] or bare)[:1]
        args = [_as_arg(k, _filler(inputs[k])) for k in named]
    return " ".join(["amsyos run", service, *args])


_NUMBERED = re.compile(r"^(.*\D)(\d+)$")
IN_A_ROW = 3  # numbered outputs of one kind said as one entry from this many on


def _output_entries(told: dict[str, Any]) -> list[str]:
    """`describe`'s outputs, numbered ones of one type in a row as one entry (Claude Code,
    2026-11-20: `voice_01` … `voice_11` listed one by one): `voice_01 … voice_11: wav
    (audio.speech; as input lines says)`, naming the input whose description names them."""
    outputs = obj(told.get("outputs"))
    kinds = obj(told.get("means"))
    rows: dict[tuple[str, ...], list[str]] = {}
    for name, fmt in outputs.items():
        found = _NUMBERED.match(str(name))
        key = (found[1], str(fmt), str(kinds.get(name, ""))) if found else (str(name),)
        rows.setdefault(key, []).append(str(name))
    entries: list[str] = []
    for key, names in rows.items():
        if len(key) == 1 or len(names) < IN_A_ROW:
            entries += [
                f"{n}: {outputs[n]}" + (f" ({kinds[n]})" if n in kinds else "") for n in names
            ]
            continue
        prefix, fmt, kind = key
        said = [kind] if kind else []
        named_in = [
            str(i) for i, spec in obj(told.get("inputs")).items()
            if names[0] in str(obj(spec).get("description") or "")
            or f"{prefix}…" in str(obj(spec).get("description") or "")
        ]  # fmt: skip
        if named_in:
            said.append(f"as input {named_in[0]} says")
        entries.append(
            f"{names[0]} … {names[-1]}: {fmt}" + (f" ({'; '.join(said)})" if said else "")
        )
    return entries


def _export(out: Out, doc: dict[str, Any], kind: str, path: str | None) -> int:
    """An export in an open standard: the document itself on stdout (in JSON mode too:
    nothing beside it), or written to `path`."""
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    what = "an MCP tool definition" if kind == "mcp" else "an A2A Agent Card (A2A 1.0)"
    if not path:
        sys.stdout.write(text)
        sys.stdout.flush()
        return 0
    target = Path(path)
    try:
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise CliError("INVALID_INPUT", f"cannot write {target}: {exc.strerror}") from None
    out.emit({"written": str(target), "export": kind}, f"wrote {target}: {what}")
    return 0


def _exported(told: dict[str, Any], kind: str, url: str) -> dict[str, Any]:
    """describe's answer as an MCP tool or an A2A Agent Card of one skill; the kernel's own
    `input_schema` and `annotations` where it said them."""
    if kind == "mcp":
        return standards.mcp_tool(told)
    return standards.a2a_card(
        str(told.get("id")), url, [standards.a2a_skill(told)],
        description=str(told.get("summary") or "") or None,
        version=str(told.get("version") or "1.0.0"),
    )  # fmt: skip


async def cmd_describe(args: argparse.Namespace, out: Out) -> int:
    creds = credentials()
    async with Api(creds, out) as api:
        told = await api.describe(args.service)
    if args.export:
        return _export(out, _exported(told, args.export, creds.mcp_url), args.export, args.out)
    effect = told.get("effect") or "pure"
    lines = [f"{told.get('id')}@{told.get('version')}  {told.get('kind')}  effect {effect}"]
    lines.append(f"  {told.get('summary')}")
    if told.get("cannot_run"):
        lines.append(f"  cannot run: {told['cannot_run']}")
    inputs = obj(told.get("inputs"))
    if inputs:
        lines.append("inputs:")
    for name, given in inputs.items():
        spec = obj(given) or {"type": given}
        bits = [str(spec.get("type"))]
        means: Any = spec.get("means")
        if means:
            bits.append(f"({', '.join(map(str, seq(means))) or means})")
        if spec.get("required"):
            bits.append("required")
        if spec.get("default") is not None:
            bits.append(f"default {json.dumps(spec['default'], ensure_ascii=False)}")
        if spec.get("values"):
            bits.append(f"one of {', '.join(map(str, spec['values']))}")
        if spec.get("min") is not None or spec.get("max") is not None:
            low, high = ("" if spec.get(k) is None else amount(spec[k]) for k in ("min", "max"))
            bits.append(f"{low}..{high}")
        lines.append(f"  {name}: {' '.join(bits)}")
        if spec.get("description"):
            lines.append(f"      {short(str(spec['description']), 300)}")
        if spec.get("example") is not None:
            example = spec["example"]
            text = example if isinstance(example, str) else json.dumps(example, ensure_ascii=False)
            lines.append("      example:")
            lines += [f"        {row}" for row in text.rstrip("\n").splitlines()]
    listed = _output_entries(told)
    if listed:
        lines.append("outputs: " + ", ".join(listed))
    for key, given in obj(told.get("estimate")).items():
        entry = obj(given)
        # `start_s` where the kernel says it, on the entry or on the whole answer (K2).
        if "start_s" not in entry and "start_s" in told:
            entry = {**entry, "start_s": told["start_s"]}  # its reason: one line below
        eta = eta_text(entry) or f"eta {entry.get('eta_s')}s"
        if entry.get("n"):
            eta += f" (from {entry['n']} runs)"
        # No `history`: none of the organisation's runs, nor the network's, is behind it.
        # A composed service's figures are its steps' added up until a run of it is measured.
        basis = {**entry, "recipe": bool(told.get("steps"))}
        eta = with_basis(eta, basis, counts_runs=not told.get("history")) or eta
        lines.append(f"estimate {key}: {eta}, cost {amount(entry.get('est_cost'))}")
    if isinstance(told.get("start"), str) and told["start"]:
        lines.append(f"machine: {short(told['start'], 200)}")
    check = obj(told.get("check"))
    if check:
        job = f" ({check['job']})" if check.get("job") else ""
        lines.append(f"check: {check.get('state') or short(check, 300)}{job}")
        lines += [f"  problem: {short(p, 300)}" for p in seq(check.get("problems"))[:5]]
        lines += check_lines(check)
    elif told.get("check") is not None:
        lines.append(f"check: {short(told['check'], 2000)}")
    for key in ("hosted", "versions", "standing", "warnings", "untrusted"):
        if told.get(key) is not None:
            lines.append(f"{key}: {short(told[key], 2000)}")
    # Last, always: one ready line (`run_cli` in JSON), its <placeholders> to fill in.
    line = run_line(told)
    lines.append("run it (fill in each <…>):" if "<" in line else "run it:")
    lines.append(f"  {line}")
    out.emit({**told, "run_cli": line}, *lines)
    return 0


# ---------------------------------------------------------------- files
async def cmd_files(args: argparse.Namespace, out: Out) -> int:
    creds = credentials()
    async with Api(creds, out) as api:
        if args.files_cmd == "put":
            sent = await upload(api, [Path(p) for p in args.paths])
            out.emit(
                sent,
                *[
                    f"{s['asset']}  {s['file']}  {human(int(s['size']))}"
                    + ("  (already in the workspace)" if s.get("reused") else "")
                    for s in sent
                ],
            )
            return 0
        if args.files_cmd == "get":
            linked = await api.instant("asset.url", {"asset": args.asset})
            urls = obj(linked.get("outputs"))
            folder = Path(args.out)
            saved = await download({"file": urls.get("url")}, folder)
            out.emit(
                {"asset": args.asset, "files": [s.row(Path.cwd()) for s in saved]},
                *describe_saved(saved, Path.cwd()),
            )
            return 0
        asked: dict[str, Any] = {}
        for key in ("name", "origin"):
            if getattr(args, key):
                asked[key] = getattr(args, key)
        if args.mine:
            asked["mine"] = True
        if args.skip:
            asked["skip"] = args.skip
        listed = await api.instant("asset.list", asked)
    rows = [obj(r) for r in seq(listed.get("assets"))]
    noted = face_note(rows, out)  # what `face` means, once, under the list (files.py)
    lines = [
        f"{r.get('asset') or r.get('ref') or r.get('id')}  {r.get('name')}  "
        f"{human(int(r.get('size') or 0))}  {r.get('from') or r.get('origin') or ''}"
        + (f"  job {r['job']}" if r.get("job") else "")
        + (f"  kept {kept_for(r['kept_s'])}" if r.get("kept_s") is not None else "")
        + out.marks(r.get("marks"))
        for r in rows
    ]
    skip = int(args.skip or 0)
    total = listed.get("total", skip + len(rows))
    shown = f"{skip + 1}-{skip + len(rows)}" if rows else "none"
    lines.append(f"{shown} of {total}")
    more = listed.get("more")
    if isinstance(more, int) and more > 0 and rows:
        # Claude Code, 2026-11-20: "30 of 54" said no way to the rest.
        same = [f"--{k} {quoted(str(getattr(args, k)))}" for k in ("name", "origin")
                if getattr(args, k)] + (["--mine"] if args.mine else [])  # fmt: skip
        page = " ".join(["amsyos files ls", *same, f"--skip {skip + len(rows)}"])
        lines.append(f"{more} more: {page}")
    out.emit(listed, *lines, *noted)
    return 0


# ---------------------------------------------------------------- grants
async def cmd_grant(args: argparse.Namespace, out: Out) -> int:
    if not args.may and args.budget is None and not args.services:
        raise CliError(
            "INVALID_INPUT",
            "ask for something: --may <power>, --services <pattern> (each again for more) "
            "or --budget <credit>",
        )
    asked: dict[str, Any] = {}
    if args.may:
        asked["may"] = ",".join(args.may)
    if args.services:
        asked["services"] = ",".join(args.services)
    if args.budget is not None:
        asked["budget"] = args.budget
    if args.note:
        asked["note"] = args.note
    async with Api(credentials(), out) as api:
        told = await api.run("grant.ask", asked)
    data = obj(told.get("data"))
    task = data.get("task") or obj(told.get("outputs")).get("task") or data.get("job_id")
    task = task or told.get("job_id")
    out.emit(
        told,
        f"asked your person: {task} waits for their approval",
        f"they approve it on their task list; amsyos wait {task} waits for their answer",
    )
    return 0


# ---------------------------------------------------------------- this repository
async def cmd_init(args: argparse.Namespace, out: Out) -> int:
    if args.everywhere:
        return _init_global(args.host or args.agent, out)
    root = Path(args.dir).resolve()
    if not root.is_dir():
        raise CliError("INVALID_INPUT", f"no folder {root}")
    if args.type != "service":
        return await _init_desk(args, out, root)
    desk = hosts.desk_root(root.parent)  # a service's folder inside a desk
    bare = desk is None and not any(not p.name.startswith(".") for p in root.iterdir())
    path = root / cards.CARD
    answer: dict[str, Any] = {"card": str(path)}
    lines: list[str] = []
    tool = _mcp_tool(args.from_mcp) if args.from_mcp else None
    state_made = not (root / ".amsyos").exists()
    if path.is_file():
        card = cards.load(root)
        lines.append(f"{cards.CARD} is there already: kept as it is")
        if tool is not None:
            lines.append(
                f"--from-mcp not applied: move {cards.CARD} away to start one from the tool"
            )
    else:
        card, notes = cards.propose(root, args.name)
        if tool is not None:
            notes += cards.from_mcp(card, tool, root, named=bool(args.name))
        path.write_text(cards.render(card), encoding="utf-8")
        how = f"{card.get('runtime')}, {card.get('command') or card.get('serve')}"
        lines.append(f"wrote {cards.CARD}: {card['service']} ({how})")
        lines += [f"  {n}" for n in notes]
        answer["proposed"] = card
        answer["notes"] = notes
    left = cards.left_to_fill(card)
    try:
        creds: Credentials | None = credentials()
    except CliError as exc:
        creds = None
        lines.append(f"not checked, project not bound ({exc.code}): amsyos login, then init again")
    if left:
        answer["left_to_fill"] = left
        lines.append(f"left to fill in {cards.CARD} (the code did not say):")
        lines += [f"  {item}" for item in left]
    if creds is not None:
        async with Api(creds, out) as api:
            if not left and not args.no_check:
                lines += await _check_card(api, card, answer, root)
            lines += await _bind(api, desk or root, answer)
    if desk is not None:
        # The desk's rules and adapters serve its services' folders; none are written here.
        answer["desk"] = str(desk)
        lines.append(f"inside the desk {desk}: its DESK.md, adapters and project serve here")
    else:
        agents = _agents(args.host or args.agent)
        gateway = creds.gateway if creds is not None else gateway_default()
        written = hosts.write(root, agents, gateway)
        answer["written"] = written.files
        answer["adapter_notes"] = written.notes
        lines.append(f"adapters for {', '.join(agents)}: " + ", ".join(written.files))
        lines += written.notes
    if bare:
        lines.append(
            "no code here: a desk an organisation builds many services from is amsyos init "
            "--type studio; one it only uses services from, --type work"
        )
    if state_made and (root / ".amsyos").exists():
        lines.append(repo.STATE_NOTE)
    rel = os.path.relpath(root, Path.cwd().resolve())
    at = "" if rel == "." else f" --dir {quoted(rel)}"  # inside a desk: from its root
    lines.append(
        f"next: fill what is left, then amsyos card --check{at}, amsyos run --local{at} (the "
        f"example here, nothing rented) and amsyos deploy --dry-run{at}"
        if left
        else f"next: amsyos run --local{at} (the example here, nothing rented), amsyos deploy "
        f"--dry-run{at} (what would be sent), then amsyos deploy{at}"
    )
    out.emit(answer, *lines)
    return 0


def _mcp_tool(path: str) -> dict[str, Any]:
    """`init --from-mcp`: the MCP tool definition in a JSON file (a tool, or a tools/list
    answer of one tool)."""
    try:
        given: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliError("INVALID_INPUT", f"cannot read {path}: {exc.strerror}") from None
    except ValueError as exc:
        raise CliError(
            "INVALID_INPUT", f"{path} is not JSON ({exc}): give the tool's definition as JSON"
        ) from None
    return cards.tool_of(given, path)


async def _check_card(
    api: Api, card: dict[str, Any], answer: dict[str, Any], root: Path
) -> list[str]:
    """`service.check` of the card (deploy.check_card, which deploy runs too before it
    uploads or builds); nothing is defined."""
    told, pending = await check_card(api, card, root)
    problems = [str(p) for p in seq(told.get("problems"))]
    answer["check"] = told
    checked = f" ({told['service']})" if told.get("service") else ""
    lines = [f"service.check (AMSYOS): {'ok' if told.get('ok') else 'problems'}{checked}"]
    shown = cards.server_problems(problems, cards.left_to_fill(card))  # once, in card words
    lines += [f"  problem: {short(p, 300)}" for p in shown]
    if problems and not shown:
        lines.append("  problem: the same as found here, above")
    lines += [f"  advice: {short(a, 300)}" for a in seq(told.get("advice"))]
    lines += [
        f"  note (not a problem): example.{name}: {path} is not in the workspace yet: deploy "
        f"uploads it, then checks it (amsyos files put {shlex.quote(path)} has AMSYOS check "
        "it now)"
        for name, path in pending.items()
    ]
    return lines


async def _bind(api: Api, root: Path, answer: dict[str, Any]) -> list[str]:
    """`project.bind` (spec v3 §8.1): the repository to the workspace, by the hash of its
    remote's URL (of the folder's path without one). Quiet where the Gateway lacks it."""
    found = repo.find(str(root)) or repo.Repo(root, False)
    name = found.root.name[:128] or "project"
    asked = {
        "name": name,
        "repo_hash": found.repo_hash,
    }
    if found.default_branch:  # a folder without git has none: left out, not sent empty
        asked["default_branch"] = found.default_branch[:128]  # fmt: skip
    told = await api.maybe("project.bind", asked)
    if told is None:
        return []
    project = told.get("project") or told.get("id") or told.get("project_id")
    if not isinstance(project, str):
        api.out.debug("project.bind named no project")
        return []
    entry = {"project": project, "name": name, "repo_hash": found.repo_hash}
    repo.write_project(found.root, api.creds.gateway, entry)
    answer["project"] = entry
    how = "bound to" if told.get("new", True) else "already bound to"
    return [f"project {project} ({name}) {how} this workspace: calls from here carry it"]


def _init_global(asked: str | None, out: Out) -> int:
    """`init --global`: AMSYOS's brief where the host reads it in every project, the host as
    `login` finds it (`--host` over it); no call, no token, no code (amsyos_cli/adapters.py)."""
    if asked == "all":
        chosen = list(hosts.HOSTS)
    else:
        found = asked or hosts.HOST_NAMES.get(host_of()[0])
        if found is None:
            raise CliError(
                "HOST_UNKNOWN",
                "which AI client is this? amsyos init --global --host claude|codex|cursor|"
                "antigravity (the one whose rules to write)",
            )
        chosen = [found]
    gateway = gateway_default()
    answer: dict[str, Any] = {"global": True, "hosts": chosen, "written": [], "notes": []}
    for host in chosen:
        done = hosts.write_global(host, gateway)
        answer["written"] += done.files
        answer["notes"] += done.notes
    out.emit(answer, *answer["notes"])
    return 0


DESK_NEXT = {
    "studio": "next: fill the <fill: ...> lines of DESK.md; then a service: its code in "
    "services/<name>/, amsyos init --dir services/<name>, amsyos card --check --dir "
    "services/<name>, amsyos deploy --dir services/<name>",
    "work": "next: fill the <fill: ...> lines of DESK.md; then amsyos whoami (the credit) and "
    'amsyos discover "<what the work needs>"',
}


async def _init_desk(args: argparse.Namespace, out: Out, root: Path) -> int:
    """`init --type studio|work`: a desk, the folder an organisation's agent works from
    (adapters.py, "a desk"): DESK.md and log.md written once, each host's pointer to
    DESK.md, the host's adapters, and the folder bound to the workspace as one project."""
    kind = str(args.type)
    answer: dict[str, Any] = {"type": kind, "desk": str(root)}
    lines: list[str] = []
    unused = [f"--{n.replace('_', '-')}" for n in ("name", "from_mcp", "no_check")
              if getattr(args, n)]  # fmt: skip
    if unused:
        lines.append(f"{', '.join(unused)}: for --type service only; not applied")
    state_made = not (root / ".amsyos").exists()
    values = {
        "{{ORG}}": "<see amsyos whoami>",
        "{{WORKSPACE}}": "<see amsyos whoami>",
        "{{PERSON}}": "the user of this session",
        "{{DATE}}": time.strftime("%Y-%m-%d", time.gmtime()),
    }
    try:
        creds: Credentials | None = credentials()
    except CliError as exc:
        creds = None
        lines.append(
            f"not bound ({exc.code}): amsyos login, then amsyos init --type {kind} again "
            "(DESK.md is kept; fill its names by hand or delete it first)"
        )
    if creds is not None:
        async with Api(creds, out) as api:
            shown, org = await asyncio.gather(
                session_show(api), tenant_of(api), return_exceptions=True
            )
            if isinstance(shown, dict):
                for key, field in (("{{WORKSPACE}}", "workspace"), ("{{PERSON}}", "person")):
                    if isinstance(shown.get(field), str):
                        values[key] = f"`{shown[field]}`"
            if isinstance(org, str):
                values["{{ORG}}"] = f"`{org}`"
            lines += await _bind(api, root, answer)
    agents = _agents(args.host or args.agent)
    gateway = creds.gateway if creds is not None else gateway_default()
    rules = hosts.desk(root, kind, agents, gateway, values)
    written = hosts.write(root, agents, gateway)
    answer["written"] = rules.files + written.files
    answer["adapter_notes"] = rules.notes + written.notes
    made = [f for f in rules.files if f in (hosts.DESK, hosts.LOG)]
    if made:
        lines.append(f"wrote {', '.join(made)}: the desk's rules and log, yours to edit")
    lines += rules.notes
    pointers = [f for f in rules.files if f not in (hosts.DESK, hosts.LOG)]
    lines.append(f"DESK.md is read through: {', '.join(pointers)}")
    lines.append(f"adapters for {', '.join(agents)}: " + ", ".join(written.files))
    lines += written.notes
    if state_made and (root / ".amsyos").exists():
        lines.append(repo.STATE_NOTE)
    lines.append(DESK_NEXT[kind])
    out.emit(answer, *lines)
    return 0


def _agents(asked: str | None) -> list[str]:
    if asked == "all":
        return list(hosts.HOSTS)
    if asked:
        return [asked]
    env = os.environ
    if env.get("CLAUDECODE"):
        return ["claude"]
    if any(k.startswith("CODEX_") for k in env):
        return ["codex"]
    if env.get("CURSOR_TRACE_ID"):
        return ["cursor"]
    return list(hosts.HOSTS)


async def cmd_deploy(args: argparse.Namespace, out: Out) -> int:
    root = Path(args.dir).resolve()
    only = [p for p in str(args.only).split(",") if p.strip()] if args.only else None
    if args.dry_run:
        try:
            creds = credentials()
            async with Api(creds, out) as api:
                return await dry_run(
                    api, out, root, args.channel, args.hours, node=args.node, only=only,
                    image=args.image, force=args.force,
                )  # fmt: skip
        except CliError as exc:
            if exc.code.startswith("INVALID") or exc.code in {"NO_CARD", "CREDENTIALS_FILE"}:
                raise
            out.debug(f"dry run without AMSYOS ({exc.code})")
        return await dry_run(
            None, out, root, args.channel, node=args.node, only=only, image=args.image,
            force=args.force,
        )  # fmt: skip
    creds = credentials()
    async with Api(creds, out) as api:
        return await deploy(
            api, out, creds, root,
            hours=args.hours, idle_minutes=args.idle_minutes or None, close=args.close,
            gpu=args.gpu, force=args.force, channel=args.channel, node=args.node, only=only,
            image=args.image,
        )  # fmt: skip


async def cmd_card(args: argparse.Namespace, out: Out) -> int:
    if not args.check and not args.spec and not args.export:
        out.emit({"help": cards.CARD_HELP}, cards.CARD_HELP.rstrip("\n"))
        return 0
    root = Path(args.dir).resolve()
    card = cards.load(root)
    name = card.get("service")
    named = isinstance(name, str) and cards.NAME_RE.fullmatch(name) is not None
    if args.export:
        found = [p for p in cards.problems(card) if p.startswith(("service:", "outputs"))]
        if found:
            raise CliError(
                "INVALID_CARD", f"{cards.CARD}: " + "; ".join(found), {"problems": found}
            )
        left = cards.left_to_fill(card)
        if left:
            out.note(
                f"{len(left)} values of {cards.CARD} are still <fill: ...>: exported as they are"
            )
        url = gateway_default().rstrip("/") + "/mcp"
        return _export(out, cards.export(card, args.export, url), args.export, args.out)
    if args.spec:
        if not named:
            raise CliError(
                "INVALID_CARD",
                f"service: 2 to 4 snake_case words with dots, like csv.chart, in {cards.CARD}",
                {"fields": ["service"]},
            )
        service = cards.for_check(card)
        out.emit(
            service,
            f"# the spec {cards.CARD} becomes, as service.check reads it (a build's service: "
            f"name, version, spec); an example's local file stands as {cards.EXAMPLE_FILE} "
            "until deploy uploads it",
            json.dumps(service, ensure_ascii=False, indent=2),
        )
        return 0
    # v3 T2: checked here only, `ok` went to a card service.check refused. Logged in, AMSYOS
    # checks the spec the card becomes (free, nothing is defined) and the tests' services.
    left = cards.left_to_fill(card)
    left += [
        f"example.{name}: no file {path} (a path from the folder of {cards.CARD})"
        for name, path in cards.local_files(card.get("example")).items()
        if not (root / path).is_file()
    ]
    answer: dict[str, Any] = {"card": str(root / cards.CARD), "problems": left}
    lines = (
        [f"{cards.CARD}: checked here: ok"]
        if not left
        else [f"{cards.CARD}: fix these, then check again:", *[f"  {p}" for p in left]]
    )
    # v3 T2: "The x" passed as x's description; a caller learns nothing from it.
    warned = cards.description_warnings(card)
    if warned:
        answer["warnings"] = warned
        lines += [f"  warning: {w}" for w in warned]
    try:
        creds: Credentials | None = credentials()
    except CliError as exc:
        creds = None
        answer["remote"] = exc.code
        lines.append(f"not checked by AMSYOS ({exc.code}): amsyos login, then card --check again")
    remote_ok = True
    if creds is not None and not named:
        remote_ok = False
        lines.append("not sent to AMSYOS's service.check until the card names its service")
    elif creds is not None:
        # Sent with the problems found here too: AMSYOS's answer lists every one of its own.
        async with Api(creds, out) as api:
            lines += await _check_card(api, card, answer, root)
            remote_ok = bool(obj(answer.get("check")).get("ok"))
            wrong = await _tests_remote(api, card)
        tests = seq(card.get("tests"))
        if wrong:
            answer["test_problems"] = wrong
            lines.append("tests, against the services you may call: problems")
            lines += [f"  problem: {p}" for p in wrong]
        elif tests:
            lines.append(
                f"tests: {len(tests)} checked against the services you may call: ok "
                "(AMSYOS runs them after the example in the version's check)"
            )
        remote_ok = remote_ok and not wrong
    ok = not left and remote_ok
    answer["ok"] = ok
    lines.append(
        "next: amsyos deploy --dry-run" if ok else "next: fix the card, then amsyos card --check"
    )
    out.emit(answer, *lines)
    return 0 if ok else 1


async def _tests_remote(api: Api, card: dict[str, Any]) -> list[str]:
    """Each test's `check` as the caller may call it (`describe`, free): it exists, takes a
    file the output can be, and makes a JSON report for `pass` to read (v3 T2)."""
    outputs = obj(card.get("outputs"))
    found: list[str] = []
    for i, given in enumerate(seq(card.get("tests"))):
        test = obj(given)
        check, out_name = test.get("check"), test.get("output")
        if not isinstance(check, str) or not cards.SERVICE_RE.fullmatch(check):
            continue
        where = f"tests[{i}].check"
        try:
            told = await api.describe(check)
        except CliError as exc:
            if exc.code in {"SERVICE_NOT_FOUND", "FORBIDDEN", "INVALID_INPUT", "NOT_FOUND"}:
                found.append(
                    f"{where}: {check} is no service you may call ({exc.code}): a service whose "
                    "JSON report the test reads, like t_x.video.check (amsyos discover check)"
                )
                continue
            raise
        files = {
            n: obj(s) for n, s in obj(told.get("inputs")).items() if obj(s).get("type") == "asset"
        }
        reports = [n for n, fmt in obj(told.get("outputs")).items() if fmt == "json"]
        if not files:
            found.append(f"{where}: {check} takes no file, so it cannot read outputs.{out_name}")
        if not reports:
            found.append(f"{where}: {check} makes no JSON report for pass to read")
        if files and isinstance(out_name, str) and out_name in outputs:
            means = cards.means_of_output(outputs[out_name])
            takes = [str(m) for spec in files.values() for m in seq(spec.get("means"))]
            if takes and not any(means == m or means.startswith(f"{m}.") for m in takes):
                found.append(
                    f"{where}: {check} reads {', '.join(takes)}; outputs.{out_name} is {means}"
                )
    return found


# ---------------------------------------------------------------- memory (stage C)
LATER = "this Gateway has no {service} yet (v3 stage C): {what}"


async def _later(api: Api, service: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    """A stage-C service's answer; a plain refusal on a Gateway that lacks it."""
    try:
        return await api.instant(service, params)
    except CliError as exc:
        if exc.code == "SERVICE_NOT_FOUND" and service in exc.hint:
            raise CliError(
                "NOT_ON_THIS_GATEWAY", LATER.format(service=service, what=what)
            ) from None
        raise


def _project_here(root: Path, gateway: str, asked: str | None) -> str | None:
    """`--project`, else the project `init` bound this repository to (none, in a folder
    `init` wrote into: the kernel takes the workspace's only project, or lists the choices;
    `sync` says the overview where there is none to take)."""
    if asked:
        return asked
    found = repo.find(str(root))
    bound = repo.read_project(found.root if found else root, gateway)
    return str(bound["project"]) if bound else None


def _choices(told: dict[str, Any], command: str) -> CliError | None:
    """`project.brief`/`project.why` with no project, in a workspace of several."""
    listed = [obj(p) for p in seq(told.get("projects") or told.get("choices"))]
    if told.get("lines") or told.get("entries") or not listed:
        return None
    names = ", ".join(f"{p.get('project') or p.get('id')} ({p.get('name')})" for p in listed[:10])
    return CliError(
        "INVALID_INPUT",
        f"this workspace has several projects: {command} --project <id>; one of {names}",
        {"projects": listed},
    )


BIND = "bind this repository with `amsyos init` to keep its memory"


def _no_project(exc: CliError) -> bool:
    """`project.brief` without a project, refused: the workspace has none bound, or several
    to choose from (kernel/brief.py `project_of`)."""
    return exc.code in {"INVALID_PARAMS", "INVALID_INPUT"} and (
        "no project bound" in exc.hint or "several projects" in exc.hint
    )


async def _unbound(
    api: Api, out: Out, start: Path, projects: list[Any], said: str | None = None
) -> int:
    """`amsyos sync` where no project is bound (Codex, 2026-11-20: it failed): the
    workspace's overview as `whoami` gives it, and how to bind this repository; exit 0."""
    answer, lines = await overview(api, start, project=False)
    lines.append(BIND)
    listed = [obj(p) for p in projects]
    if listed:
        names = ", ".join(
            f"{p.get('project') or p.get('id')} ({p.get('name')})" for p in listed[:10]
        )
        lines.append(f"this workspace's projects: {names}; amsyos sync --project <id> shows one")
    elif said and "several projects" in said:
        lines.append(f"AMSYOS: {short(said, 300)} (amsyos sync --project <id>)")
    out.emit({"bound": False, "project": None, "next": BIND, "projects": listed, **answer}, *lines)
    return 0


async def cmd_sync(args: argparse.Namespace, out: Out) -> int:
    """`project.brief` into the host's files (spec v3 §9.3, amsyos_cli/brief.py)."""
    start = Path(args.dir).resolve()
    if args.quiet and not args.background:
        return brief.quiet(start)  # as entry.py: the cached brief, refreshed in the background
    inited = brief.root_of(start)
    root = inited or start
    creds = credentials()
    project = _project_here(root, creds.gateway, args.project)
    async with Api(creds, out) as api:
        if project is None and inited is None:
            # Nothing here was ever bound (no .amsyos/, no amsyos.yaml): where the caller
            # stands, not another repository's brief, and nothing written into this folder.
            return 0 if args.background else await _unbound(api, out, start, [])
        asked = {"project": project} if project else {}
        try:
            told = await _later(api, "project.brief", asked, "nothing was written")
        except CliError as exc:
            if project is not None or not _no_project(exc):
                raise
            return 0 if args.background else await _unbound(api, out, start, [], exc.hint)
        several = _choices(told, "amsyos sync")
        if several is not None:
            if args.background:
                return 0
            return await _unbound(api, out, start, seq(several.extra.get("projects")))
    said = brief.clean(seq(told.get("lines")))
    entry: dict[str, Any] = {
        "project": told.get("project") or project,
        "lines": said,
        "at": told.get("at"),
        "fetched_at": round(time.time()),
    }
    brief.save_cache(root, creds.gateway, entry)
    written = brief.write_all(root, entry)
    answer = {**entry, "written": written}
    lines = [f"brief of {entry['project']} ({entry['at']}):", *said]
    if written:
        lines.append("written into: " + ", ".join(written))
    elif brief.targets(root):
        lines.append("the host's files already hold this brief")
    else:
        lines.append(
            "no host file to write it into: amsyos init writes one (or put "
            f"{brief.BEGIN} {brief.END} in CLAUDE.md)"
        )
    out.emit(answer, *lines)
    return 0


def _entry_line(given: Any) -> str:
    """One fact of `project.why`: its text, then its job ids and time."""
    if not isinstance(given, dict):
        return f"- {short(given, 300)}"
    entry = obj(given)
    keys = ("says", "line", "text", "fact", "what", "summary")
    text = next((entry[k] for k in keys if entry.get(k)), None)
    jobs = [str(j) for j in seq(entry.get("jobs"))]
    for key in ("job", "job_id"):
        if entry.get(key):
            jobs.insert(0, str(entry[key]))
    bits = [x for x in (entry.get("kind"), entry.get("at")) if x]
    shown = short(text, 300) if text is not None else short(entry, 300)
    tail = ", ".join([*map(str, bits), *jobs])
    return f"- {shown}" + (f"  ({tail})" if tail else "")


async def cmd_why(args: argparse.Namespace, out: Out) -> int:
    """`project.why {project, topic}`: the facts behind a line of the brief, or a word."""
    topic = " ".join(args.topic).strip()
    if not topic:
        raise CliError(
            "INVALID_INPUT",
            "amsyos why <topic>: a line of the brief, a service, a package or an error code",
        )
    root = Path(args.dir).resolve()
    creds = credentials()
    project = _project_here(brief.root_of(root) or root, creds.gateway, args.project)
    asked: dict[str, Any] = {"topic": topic[:300]}
    if project:
        asked["project"] = project
    async with Api(creds, out) as api:
        told = await _later(api, "project.why", asked, "nothing to explain")
    several = _choices(told, "amsyos why")
    if several is not None:
        raise several
    entries = seq(told.get("entries") or told.get("facts"))
    lines = [f"why {topic} ({told.get('project') or project or 'this workspace'}):"]
    lines += [_entry_line(e) for e in entries]
    if not entries:
        lines.append("  nothing recorded on this")
    if told.get("more"):
        lines.append(f"  {told['more']} more: ask about a narrower topic")
    out.emit(told, *lines)
    return 0


async def cmd_contract(args: argparse.Namespace, out: Out) -> int:
    """`capability.contract {gap | service | need}` (spec v3 §11): what a build must meet,
    the input a worker agent takes; nothing in it the caller did not have."""
    what = args.what
    is_need = (
        what == "-" or what.lstrip().startswith("{") or what.endswith(".json")
        or Path(what).is_file()
    )  # fmt: skip
    creds = credentials()
    async with Api(creds, out) as api:
        if is_need:
            need = _need(what)
            try:  # the need as JSON text, as a service's input; else as the object itself
                told = await _later(
                    api, "capability.contract", {"need": json.dumps(need)}, "no contract made"
                )
            except CliError as exc:
                if exc.code not in {"INVALID_INPUT", "INVALID_PARAMS"} or "need" not in exc.hint:
                    raise
                try:
                    told = await _later(
                        api, "capability.contract", {"need": need}, "no contract made"
                    )
                except CliError:
                    raise exc from None  # the need's own problems, as first said
            kind = "need"
        elif what.startswith("gp_"):
            told = await _later(api, "capability.contract", {"gap": what}, "no contract made")
            kind = "gap"
        else:
            service = what
            if not service.startswith("t_") and service.count(".") >= 1:
                own = f"{await tenant_of(api)}.{service}"
                service = own if await _defined(api, own) else service
            told = await _later(
                api, "capability.contract", {"service": service}, "no contract made"
            )
            kind = "service"
    contract = obj(told.get("contract")) or told
    lines = [f"build contract for the {kind} {what}:"]
    for key in ("inputs", "outputs"):
        named = obj(contract.get(key))
        if named:
            lines.append(f"  {key}: " + ", ".join(f"{k} {short(v, 60)}" for k, v in named.items()))
    tests = seq(contract.get("tests"))
    lines.append(f"  tests: {len(tests)}")
    lines += [f"    - {short(t, 200)}" for t in tests[:10]]
    for key in ("budget", "where", "source", "deadline"):
        if contract.get(key) is not None:
            lines.append(f"  {key}: {short(contract[key], 200)}")
    lines.append("the whole contract: amsyos contract " + shlex.quote(what) + " --json")
    out.emit(told, *lines)
    return 0


async def _defined(api: Api, service_id: str) -> bool:
    try:
        await api.describe(service_id)
    except CliError:
        return False
    return True


# ---------------------------------------------------------------- the parser
class UsageError(Exception):
    """A command line argparse refuses, said in the kernel's shape with a hint."""

    def __init__(self, parser: argparse.ArgumentParser, message: str, extra: list[str]) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message
        self.extra = extra


class Parser(argparse.ArgumentParser):
    """argparse, whose refusals name the flags the command takes (v3 T4: an unknown flag got
    a bare usage line)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commands: dict[str, argparse.ArgumentParser] = {}

    def error(self, message: str) -> NoReturn:
        raise UsageError(self, message, [])


def _flags(p: argparse.ArgumentParser) -> list[str]:
    actions = p._actions  # pyright: ignore[reportPrivateUsage]
    return sorted({o for a in actions for o in a.option_strings if o.startswith("--")})


def _usage(err: UsageError, as_json: bool) -> int:
    """`error: INVALID_INPUT: <what>; <the command's flags, a near one>` (exit 2)."""
    p = err.parser
    flags = _flags(p)
    unknown = [x for x in err.extra if x.startswith("-")]
    words = [x for x in err.extra if not x.startswith("-")]
    if p.prog.split()[-1] == "setup":  # what was typed may hold the code: never repeated
        hint = (
            "amsyos setup takes one setup file, or --code <code> [--gateway <url>]; what else "
            "was given is not repeated here"
        )
    elif p.prog.split()[-1] == "feedback" and words:  # a text may hold a secret: never repeated
        hint = (
            'amsyos feedback takes one text, in quotes, before its flags ("<what happened; what '
            'you expected>"), or --file <path>; what else was given is not repeated here'
        )
    elif unknown:
        near = [
            g for x in unknown
            for g in difflib.get_close_matches(x.split("=", 1)[0], flags, 1, 0.6)
        ]  # fmt: skip
        hint = f"{p.prog} takes no {' '.join(unknown)}"
        if near:
            hint += f" (meant {' '.join(near)}?)"
        hint += f"; it takes {' '.join(flags)}"
    elif words:
        hint = f"{p.prog} takes no {' '.join(words)} here; {p.prog} --help says what it takes"
    else:
        hint = f"{err.message}; {p.prog} --help says what it takes"
        chosen = re.search(r"invalid choice: '([^']*)' \(choose from (.*)\)", err.message)
        if chosen:
            choices = re.findall(r"'([^']+)'", chosen[2])
            near = difflib.get_close_matches(chosen[1], choices, 1, 0.5)
            hint = f"{p.prog}: no {chosen[1]!r}" + (f" (meant {near[0]}?)" if near else "")
            hint += f"; one of {', '.join(choices)}"
    if as_json:
        sys.stdout.write(json.dumps({"error": "INVALID_INPUT", "hint": hint}) + "\n")
    else:
        sys.stderr.write(f"error: INVALID_INPUT: {hint}\n")
    return 2


def _command_of(top: Parser, args: argparse.Namespace) -> argparse.ArgumentParser:
    """The (sub)command's parser that unknown words were given to."""
    name = getattr(args, "command", None)
    if not isinstance(name, str):
        return top
    inner = getattr(args, f"{name}_cmd", None)
    return top.commands.get(f"{name} {inner}") or top.commands.get(name) or top


def parser() -> Parser:
    common = Parser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="the whole answer as JSON"
    )
    common.add_argument(
        "--show-urls", action="store_true", default=argparse.SUPPRESS, help="show signed URLs"
    )
    common.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="say what fell back (no event stream, no asset.has on this Gateway, …)",
    )  # fmt: skip
    top = Parser(
        prog="amsyos",
        description="AMSYOS from a terminal or an AI coding agent: compute, durable work, your "
        "organisation's own services and those offered to it, memory. Errors say the next "
        "action in their hint.",
    )
    top.add_argument("--version", action="version", version=f"amsyos {__version__}")
    top.add_argument("--json", action="store_true", default=False, help="the whole answer as JSON")
    top.add_argument("--show-urls", action="store_true", default=False, help="show signed URLs")
    top.add_argument("--verbose", action="store_true", default=False, help="say what fell back")
    sub = top.add_subparsers(dest="command", metavar="<command>")

    def add(
        name: str, func: Command, help_: str, long: str | None = None, **more: Any
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_, description=long or help_, **more)
        p.set_defaults(func=func)
        top.commands[name] = p
        return p

    p = add("login", cmd_login, "log in by device code: a person approves it on the page shown")
    p.add_argument(
        "--gateway", help="the Gateway's URL (default AMSYOS_GATEWAY or https://103.77.173.205)"
    )
    p.add_argument("--workspace", help="the workspace to offer first on the page")
    p.add_argument("--budget", type=float, help="the credit to offer first (default 1)")
    p.add_argument("--hours", type=float, help="the hours to offer first (default and at most 12)")
    p.add_argument("--host", help="the host's name shown to the person (found by itself)")
    p = add(
        "setup", cmd_setup,
        "log in with the setup file (config-amsyos.md) a person downloaded at <gateway>/start",
        "log in with the setup file (config-amsyos.md) a person downloaded at <gateway>/start: "
        "its one-time code is redeemed once, the session's token kept as amsyos login keeps "
        "it, then the overview as whoami gives it. The code is never printed; one file for "
        "each AI client. Next: amsyos init --global",
    )  # fmt: skip
    p.add_argument("file", nargs="?", help="the setup file, e.g. config-amsyos.md")
    p.add_argument("--code", help="instead of the file: the code of its block")
    p.add_argument(
        "--gateway",
        help="with --code: the Gateway's URL (default AMSYOS_GATEWAY, the one logged in at, "
        "else https://103.77.173.205)",
    )
    add("logout", cmd_logout, "end the session and forget its token")
    add_feedback(add)
    add(
        "whoami", cmd_whoami,
        "where you stand: who, the workspace, the budget, your rented node, the project here, "
        "and services to try now",
    )  # fmt: skip

    p = add(
        "init", cmd_init,
        "bind this repository to the workspace, propose amsyos.yaml from the code, check it, "
        "write the host's adapter; --type studio|work: a desk instead (DESK.md, log.md)",
    )  # fmt: skip
    p.add_argument(
        "--agent",
        choices=[*hosts.HOSTS, "all"],
        help="the host to write for (found by itself, else all)",
    )
    p.add_argument(
        "--global", dest="everywhere", action="store_true",
        help="only the host's rules for every project, in your home: Claude Code's personal "
        "skill, Codex's AGENTS.md ($CODEX_HOME), Antigravity's ~/.gemini/GEMINI.md (Cursor "
        "has none); nothing else is written or asked",
    )  # fmt: skip
    p.add_argument(
        "--host", choices=hosts.HOSTS, help="the AI client to write for (found by itself)"
    )
    p.add_argument(
        "--type", choices=hosts.KINDS, default="service",
        help="what this folder is: service (default), one service's code: its amsyos.yaml; "
        "studio, a desk an organisation builds many services from, one folder each under "
        "services/; work, a desk it only uses services from. A desk gets DESK.md (its rules: "
        "budget, licences, material from the web, what to ask first; yours to edit, never "
        "overwritten), log.md and each host's pointer to DESK.md",
    )  # fmt: skip
    p.add_argument("--name", help="the service's name, like csv.chart")
    p.add_argument("--dir", default=".", help="the repository (default here)")
    p.add_argument("--no-check", action="store_true", help="do not ask service.check")
    p.add_argument(
        "--from-mcp", metavar="TOOL_JSON",
        help="start the card from an MCP tool's definition (its description, inputSchema, "
        "annotations → effect), with what init reads from the code",
    )  # fmt: skip

    p = add(
        "card", cmd_card, "amsyos.yaml, the card of this repository's service",
        epilog=cards.CARD_HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    p.add_argument(
        "--check", action="store_true",
        help="check ./amsyos.yaml here, then by AMSYOS's service.check and its tests' services "
        "when logged in (free; nothing is defined)",
    )  # fmt: skip
    p.add_argument(
        "--spec", action="store_true", help="print the spec the card becomes (service.check's)"
    )
    p.add_argument(
        "--export", choices=cards.EXPORTS,
        help="print the card as an MCP tool definition or an A2A Agent Card (made here)",
    )  # fmt: skip
    p.add_argument("--out", help="with --export: write it to this file")
    p.add_argument("--dir", default=".", help="the repository (default here)")

    p = add(
        "deploy", cmd_deploy,
        "build this repository on the organisation's rented node, or its own node (the card's "
        "where: node), and define it",
        epilog=cards.PACK_RULES + DEPLOY_NODE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    p.add_argument(
        "--hours", type=float, default=1.0,
        help="hours to rent a node for when none is up (default 1)",
    )  # fmt: skip
    p.add_argument(
        "--idle-minutes",
        type=int,
        default=15,
        help="a node rented here closes after this many idle minutes (0: never)",
    )
    p.add_argument("--gpu", help="any, rtx, or a GPU name node.show lists")
    p.add_argument(
        "--node", metavar="n_…",
        help="where: node: the own node to build on, its id or name (amsyos node list), when "
        "the organisation has several",
    )  # fmt: skip
    p.add_argument(
        "--only", metavar="DIR,…",
        help="where: node: deploy only these of the cards that share the image, by their folder "
        "from the top card's ('.' is the top card); default every one. Under a top card of "
        "where: space, its own-node cards, built on the own node from its folder",
    )  # fmt: skip
    p.add_argument(
        "--image", metavar="NAME",
        help="no card here, cards side by side below it on several images: the one to deploy",
    )  # fmt: skip
    p.add_argument("--close", action="store_true", help="give the rented node back after")
    p.add_argument(
        "--force", action="store_true",
        help="build even when nothing changed (on an own node: also when only cards did)",
    )  # fmt: skip
    p.add_argument(
        "--channel", choices=["dev", "stable"], default="dev",
        help="the channel the new version moves (default dev; stable needs service.promote)",
    )  # fmt: skip
    p.add_argument(
        "--dry-run", action="store_true", help="list the files and the version; send nothing"
    )
    p.add_argument("--dir", default=".", help="the repository (default here)")

    p = add("promote", cmd_promote, "move a channel of a service of yours to a version")
    p.add_argument("service", help="t_x.name, or name (your organisation's)")
    p.add_argument("version", help="e.g. 1.0.3")
    p.add_argument("--channel", choices=["dev", "stable"], default="stable")

    p = add(
        "pool", cmd_pool,
        "your organisation's own services by family: where each runs, version, check, and over "
        "30 days runs, failures, median time, credit, last run, disk",
        "your organisation's own services by family (service.pool): where each runs, the "
        "version a bare id runs and its check, and over the last 30 days its runs, failure "
        "share, median time, credit spent and last run, and the disk it holds on own nodes. "
        "A manager sees the organisation's figures, anyone else the workspace's. Judge what "
        "is efficient, then amsyos uninstall what is not",
    )  # fmt: skip
    p.add_argument("family", nargs="?", help="only this family: t_x.image, image or image.*")

    p = add(
        "compare", cmd_compare,
        "measure two versions of a service of yours, or two alike services, on the same "
        "inputs: passed checks, time, credit, verdict",
        "measure two versions of one service of yours, or two services with the same inputs "
        "and outputs, on the same inputs (service.compare): each version's example, or "
        "--inputs. The runs are quoted first (refused above --max-credit), run as ordinary "
        "jobs of yours, waited for, and said as one table: per input whether each output "
        "passed the service's checks, its time and credit, why it failed; totals; a verdict "
        "and the next step (amsyos promote, amsyos uninstall --version)",
    )  # fmt: skip
    p.add_argument("service", nargs="?", help="t_x.name, or name (yours); name@1.2.0 pins a")
    p.add_argument(
        "--versions",
        help="two versions, comma separated: 1.2.0,1.3.0 or stable,dev (default: stable and "
        "dev when they differ, else the two newest kept)",
    )  # fmt: skip
    p.add_argument("--other", help="instead of --versions: a second service with the same inputs")
    p.add_argument(
        "--inputs",
        help='JSON or @file: up to 3 params objects, [{"file": "asset://..."}] (default: the '
        "examples)",
    )  # fmt: skip
    p.add_argument("--max-credit", type=float, help="refuse above this quote (default 1)")
    p.add_argument(
        "--external", action="store_true",
        help="also compare a service that acts outside AMSYOS (each run acts there)",
    )  # fmt: skip
    p.add_argument("--dry-run", action="store_true", help="only the runs and their quote")
    p.add_argument("--wait", type=int, default=600, help="seconds to wait for the runs (600)")
    p.add_argument("--read", metavar="CMP", help="read an earlier compare (cmp_...)")
    p.add_argument("--cancel", metavar="CMP", help="cancel the runs of a compare not yet ended")

    p = add(
        "uninstall", cmd_uninstall,
        "take a service of yours, or a family (name.*), out completely: a dry run without --yes",
        "take a service of your organisation, or a whole family (t_x.image.* or image.*), out "
        "of its pool completely (service.remove): its versions and channels, its code, "
        "environment and warm servers on own nodes, its skills. Without --yes a dry run says "
        "everything that would go and the disk it frees; nothing is removed while anything "
        "outside it still uses it (each user is listed with its fix). Undo for 7 days: "
        "amsyos restore. --version removes one version only (final)",
    )  # fmt: skip
    p.add_argument("service", help="t_x.name, name (yours), or a family: name.*")
    p.add_argument("--version", help="only this version (e.g. a beta beside stable); final")
    p.add_argument(
        "--with-image", action="store_true",
        help="a service of the rented node: forget its kept image too",
    )  # fmt: skip
    p.add_argument(
        "--ignore-code-calls", action="store_true",
        help="remove it although another service's code called it lately (it may be dead code)",
    )  # fmt: skip
    p.add_argument("--yes", action="store_true", help="remove it now (without: a dry run)")

    p = add(
        "restore", cmd_restore,
        "bring back a service or family removed in the last 7 days",
        "bring back a service or family removed in the last 7 days (service.restore): its "
        "definitions, versions and channels; a service of an own node is deployed again "
        "(amsyos deploy)",
    )  # fmt: skip
    p.add_argument("service", help="t_x.name, name (yours), or a family: name.*")

    add_git(add, common)  # spec v3 §18: amsyos repo …, amsyos git-credential

    p = add(
        "self-update", cmd_self_update,
        "install the newest amsyos beside this one and switch to it, now, in this command "
        "(a failed step changes nothing)",
        "install the newest amsyos beside this one and switch to it, now, in this command "
        "(a failed step changes nothing). An agent's host may refuse an agent this, as "
        "replacing its own tools: the person then runs it in a terminal. Until then only fixes "
        "in the amsyos command itself wait; AMSYOS's server-side fixes need no update.",
    )  # fmt: skip
    p.add_argument("--check", action="store_true", help="only say whether a newer one exists")
    p.add_argument("--plan", action="store_true", help="the steps it would take; nothing done")
    p.add_argument("--rollback", action="store_true", help="back to the version before")
    p.add_argument(
        "--if-needed", action="store_true",
        help="only when a newer one is out or this one is too old (the daily check); silent "
        "otherwise: the SessionStart hook's way",
    )  # fmt: skip
    p.add_argument("--force", action="store_true", help="install it even when it is not newer")

    p = add(
        "install", cmd_install,
        "set amsyos up in AMSYOS_HOME (bin/ with its shims, versions/, current) and put bin on "
        "PATH; run once as: uvx --from <wheel URL> amsyos install",
    )  # fmt: skip
    p.add_argument("--no-path", action="store_true", help="leave PATH as it is")
    p.add_argument(
        "--keep-old", action="store_true",
        help="leave the uv tool install of 0.3.12 and older (removed by default)",
    )  # fmt: skip

    p = add(
        "machine", cmd_machine,
        "this computer's cores, memory, disk and GPU; in a repository, whether it can run the "
        "card's service here (run --local) or needs a rented node (deploy). Free, sent nowhere",
    )  # fmt: skip
    p.add_argument("--dir", help="the repository (default here)")

    p = add(
        "run", cmd_run,
        "run a service: params as k=v (k=@file uploads it for a file input; another input "
        "takes its text, at most 1 MB), a JSON file or -",
    )  # fmt: skip
    p.add_argument("service", nargs="?", help="the service; with --local, left out")
    p.add_argument("params", nargs="*")
    p.add_argument("--local", action="store_true", help=local_run.LONG_HELP)
    p.add_argument("--dir", help="with --local: the repository (default here)")
    p.add_argument(
        "--keep", action="store_true",
        help="with --local: keep the job's folder in the repository's <out>/<job>/ (default "
        "out/, which deploy never packs; --out DIR elsewhere) and say where",
    )  # fmt: skip
    p.add_argument(
        "--force", dest="force_here", action="store_true",
        help="with --local: run here even when this machine lacks what the card needs",
    )  # fmt: skip
    p.add_argument("--wait", action="store_true", help="wait for the end, as long as it takes")
    p.add_argument(
        "--out", default="out",
        help="where outputs land: <out>/<job>/ (default ./out); with --local --keep, the "
        "job's folder, from the repository",
    )  # fmt: skip
    p.add_argument("--no-download", action="store_true", help="do not fetch the outputs")
    p.add_argument("--workspace", help="run in this workspace of yours")
    p.add_argument("--new", action="store_true", help="a new job even for the same call")
    p.add_argument(
        "--limit", type=limit_of, metavar="TIME",
        help="end the job if it runs longer (90s, 10m, 2h): a job of your code on your "
        "rented node may otherwise run as long as the node is rented",
    )  # fmt: skip

    p = add(
        "wait", cmd_wait, "wait for a job's end, then fetch its outputs into <out>/<job>/",
        "wait for a job's end, as long as it takes, then fetch its outputs into <out>/<job>/: "
        "through a wait for a person or another agent (an approval, grant ask's task), until "
        "it is decided; it stops at a wait for you yourself. Exit 0 succeeded, 1 failed, 3 "
        "not ended within --timeout or waiting for you",
    )  # fmt: skip
    p.add_argument("job")
    p.add_argument("--out", default="out", help="default ./out")
    p.add_argument("--no-download", action="store_true", help="do not fetch the outputs")
    p.add_argument(
        "--timeout",
        type=float,
        help="seconds to wait at most (exit 3), default: as long as it takes",
    )
    p = add(
        "status", cmd_status,
        'a job\'s state and a parent job\'s steps (status j_…); or "<what you are doing>"',
        "status j_…: a job's state, a parent job's steps. status \"<what you are doing>\" (at "
        "most 120 characters): the one line others in this workspace see of you in amsyos "
        "around, for 2 hours after your last call (session.status); --clear ends it",
    )  # fmt: skip
    p.add_argument("job", nargs="?", metavar="j_…|doing", help="a job, or what you are doing")
    p.add_argument("--wait", action="store_true", help="wait for its end, as long as it takes")
    p.add_argument("--clear", action="store_true", help="end your status: nothing shown")
    p = add(
        "around", around.cmd_around, "who else works in this workspace now, and on what",
        "who else works in this workspace now (status, leases held, jobs running), what others "
        "did in the last 2 hours, and with --about who holds, changed or runs those services "
        "or leases (session.around); others only, this workspace only. Every command's answer "
        "may end with around: lines, what others did since you last saw (stderr; `around` in "
        "--json); deploy, uninstall and promote first say who works on what they change",
    )  # fmt: skip
    p.add_argument(
        "--about", metavar="X,Y", help="service ids or lease names, comma separated (at most 20)"
    )
    p.add_argument(
        "--hook", action="store_true",
        help="Claude Code's prompt hook: only what this project was not told yet, at most 5 "
        "lines, at most once a minute; nothing on any error",
    )  # fmt: skip
    p = add(
        "lease", around.cmd_lease, "say what you work on: take, release or list leases",
        "lease take <what> [--minutes 60] [--note why]: hold <what> (free text like film/part-3, "
        "service:<id>, space, work:<job>) so the others in the workspace see it in their around "
        "and a second taker is refused; take it again to renew. lease release <what>; lease ls "
        "(spec v3 §10)",
    )  # fmt: skip
    p.add_argument("action", choices=["take", "release", "ls"])
    p.add_argument("what", nargs="?", help="what you hold, e.g. film/part-3 or service:<id>")
    p.add_argument("--minutes", type=int, default=60, help="1 to 720 (default 60)")
    p.add_argument("--note", help="why, for the others (at most 300 characters)")
    p = add(
        "note", around.cmd_note, "post or list the workspace's notes, decisions and asks",
        'note post <topic> "<text>" [--kind info|decision|ask]: a note the others see (their '
        "around says it); note ls [topic] [--since n_…|time] [--limit 20] (spec v3 §10)",
    )  # fmt: skip
    p.add_argument("action", choices=["post", "ls"])
    p.add_argument("topic", nargs="?", help="what it is about, e.g. film/part-3")
    p.add_argument("text", nargs="?", help="the note (post)")
    p.add_argument("--kind", choices=["info", "decision", "ask"], default="info")
    p.add_argument("--since", help="ls: after this note id or time")
    p.add_argument("--limit", type=int, default=20, help="ls: at most this many (1-100)")
    p = add("result", cmd_result, "a finished job's outputs, fetched into <out>/<job>/")
    p.add_argument("job")
    p.add_argument("--out", default="out", help="default ./out")
    p.add_argument("--list", action="store_true", help="name the outputs, fetch nothing")
    p.add_argument("--no-download", action="store_true", help="as --list: fetch nothing")
    p.add_argument(
        "--apply", action="store_true",
        help="apply its patch output here with git apply --3way (the files are named first)",
    )  # fmt: skip
    p = add("cancel", cmd_cancel, "cancel a job")
    p.add_argument("job")
    p = add("logs", cmd_logs, "a job's log, or what it printed last")
    p.add_argument("job")
    p.add_argument("-f", "--follow", action="store_true", help="follow it until the job ends")
    p.add_argument(
        "--full", action="store_true",
        help="all it printed on an own node (node 2.1.6, its last 50 jobs): saved to <out>/<job>/",
    )  # fmt: skip
    p.add_argument("--out", default="out", help="with --full: where it lands (default ./out)")

    p = add(
        "discover", cmd_discover,
        "find services by words, or plans by a typed need; no words: where to start",
        "find services by words (any language; English works best), or plans by a typed "
        "need; no words: where to start. It searches AMSYOS's platform services, your "
        "organisation's own services and those other organisations offer it: services of "
        "a field (media, documents, 3D) are organisations' own, or offered to them",
    )  # fmt: skip
    p.add_argument("words", nargs="*")
    p.add_argument(
        "--need",
        help="a typed need: a JSON file, - (stdin) or the JSON itself, of inputs, outputs, "
        'constraints; its shape: {"inputs": {"clip": "video/mp4"}, "outputs": {"report": '
        '"json:report"}} (a plan exists only where services make those types)',
    )
    p.add_argument("--limit", type=int, help="a page's size, as the answer's note says")
    p.add_argument("--skip", type=int, help="hits to skip: the next page")
    p.add_argument("--automatic", action="store_true", help="only services no person acts in")
    p = add("describe", cmd_describe, "a service's inputs, outputs, examples, cost and state")
    p.add_argument("service")
    p.add_argument(
        "--export", choices=cards.EXPORTS,
        help="the answer as an MCP tool definition or an A2A Agent Card",
    )  # fmt: skip
    p.add_argument("--out", help="with --export: write it to this file")

    p = add("files", cmd_files, "files of the workspace: put, get, ls")
    fsub = p.add_subparsers(dest="files_cmd", metavar="put|get|ls", required=True)
    fp = fsub.add_parser("put", parents=[common], help="upload local files the workspace lacks")
    fp.add_argument("paths", nargs="+")
    fg = fsub.add_parser("get", parents=[common], help="fetch a file: asset://…")
    fg.add_argument("asset")
    fg.add_argument("--out", default="out", help="default ./out")
    fl = fsub.add_parser("ls", parents=[common], help="list files, newest first")
    top.commands.update({"files put": fp, "files get": fg, "files ls": fl})
    fl.add_argument("--name", help="names starting like this")
    fl.add_argument("--origin", choices=["upload", "job", "fetch"])
    fl.add_argument("--mine", action="store_true")
    fl.add_argument("--skip", type=int)

    p = add("work", cmd_work, "durable work: a parent job's steps, resume, hand over, checkpoint")
    p.add_argument("what", choices=["show", "resume", "hand", "checkpoint"])
    p.add_argument("job", nargs="?")
    p.add_argument("--to", help="hand: the principal (u_…, a_…) or session taking it over")
    p.add_argument("--wait", action="store_true", help="resume: wait for the new job's end")
    p.add_argument("--out", default="out", help="resume --wait: where outputs land")
    p.add_argument("--no-download", action="store_true", help="resume --wait: fetch nothing")
    p.add_argument("--new", action="store_true", help="resume: a new job even if asked before")
    p.add_argument(
        "--param", action="append", metavar="STEP.NAME=VALUE",
        help="resume: a step's param changed before it runs again, e.g. voice.voice=Adam "
        "(again for more; a value is JSON when it reads as JSON, else text)",
    )  # fmt: skip
    p.add_argument("--quiet", action="store_true", help="checkpoint: the hook's quiet no-op")

    p = add("grant", cmd_grant, "ask the session's person for more")
    gsub = p.add_subparsers(dest="grant_cmd", metavar="ask", required=True)
    ga = gsub.add_parser("ask", parents=[common], help="ask for a power, services or budget")
    ga.add_argument("--may", action="append", help="a power, e.g. node.rent (again for more)")
    ga.add_argument(
        "--services", action="append", help="a service pattern, e.g. blender.* (again for more)"
    )
    ga.add_argument("--budget", type=float, help="the session's budget asked for, in credit")
    ga.add_argument("--note", help="why, for the person")
    top.commands["grant ask"] = ga

    def rented(
        parent: argparse.ArgumentParser, dest: str, rent: str, more: str = ""
    ) -> dict[str, argparse.ArgumentParser]:
        """The verbs of the rented node, `amsyos node`'s and its old name `amsyos space`'s."""
        verbs = parent.add_subparsers(
            dest=dest, metavar=f"{rent}|show|close|extend|clean{more}", required=True
        )
        so = verbs.add_parser(
            rent, parents=[common],
            help="rent a machine for some hours, for your organisation alone (credit: "
            "price_per_hour each)",
        )  # fmt: skip
        so.add_argument(
            "--hours",
            type=float,
            default=1.0,
            help="hours to buy (default 1, at most 720: 30 days; held from your credit)",
        )
        so.add_argument(
            "--idle-minutes", type=int, help="close it after this many idle minutes (2 to 1440)"
        )
        so.add_argument("--gpu", help="any (default), rtx, or a GPU name node show --json lists")
        so.add_argument(
            "--no-wait", action="store_true",
            help="answer at once; by default it waits until the node is up (about a minute)",
        )  # fmt: skip
        ss = verbs.add_parser(
            "show", parents=[common],
            help="a node in full: the rented one (default: up or not, time left, charged) or an "
            "own node",
        )  # fmt: skip
        ss.add_argument(
            "node", nargs="?", help="rented (default), or an own node's id n_… (node list)"
        )
        sc = verbs.add_parser("close", parents=[common], help="give the rented node back now")
        sc.add_argument(
            "--even-if-busy", action="store_true",
            help="close even while jobs run there: they are stopped and what they did is lost",
        )  # fmt: skip
        se = verbs.add_parser("extend", parents=[common], help="add hours to the rented node")
        se.add_argument(
            "--hours", type=float, required=True, help="hours to add (720 in all at most)"
        )
        sk = verbs.add_parser(
            "clean", parents=[common],
            help="free the rented node's disk, the node kept; nothing asked: what is on it and "
            "its size",
        )  # fmt: skip
        sk.add_argument(
            "--images", help="name@version,... or all: deleted; a kept one is made again"
        )
        sk.add_argument("--models", help="owner/name,... or all: deleted from the model cache")
        sk.add_argument(
            "--all", action="store_true",
            help="the machine as just rented: every image, model, cache and file of yours",
        )  # fmt: skip
        sk.add_argument(
            "--even-if-not-kept", action="store_true",
            help="delete an image even when nothing is kept to make it again",
        )  # fmt: skip
        found = {rent: so, "show": ss, "close": sc, "extend": se, "clean": sk}
        if more:
            found["list"] = verbs.add_parser(
                "list", parents=[common], help="every node: id, type (rented, own), state"
            )
        return found

    p = add(
        "node", cmd_node, "your nodes: rent one, show, close, extend, clean, list",
        "the machines where your organisation's own code runs, for it alone (spec v3 §17): a "
        "rented node, AMSYOS rents by the hour from your credit (GPU, or none for less; images "
        "are built on it; one at a time), and own nodes, computers of yours running AMSYOS "
        "Node. Rent one for some hours, show one, give it back, add hours, free its disk, "
        "list them all; one line each",
    )  # fmt: skip
    top.commands.update({f"node {k}": v for k, v in rented(p, "node_cmd", "rent", "|list").items()})
    # `amsyos space …`, the old name (open is rent): still taken, listed nowhere.
    p = sub.add_parser(
        "space", parents=[common], description="the old name of amsyos node (open is rent)"
    )
    p.set_defaults(func=cmd_node)
    top.commands["space"] = p
    top.commands.update({f"space {k}": v for k, v in rented(p, "space_cmd", "open").items()})

    p = add(
        "sync", cmd_sync, "the project's brief, into the host's files",
        "the project's brief from AMSYOS (project.brief, at most 30 lines) written between "
        f"{brief.BEGIN} markers into the host's files init wrote (the Claude Code plugin's "
        "brief.md, AGENTS.md, .cursor/rules/amsyos-brief.mdc; CLAUDE.md when it holds them). "
        "Where no project is bound: the workspace's overview, as whoami, and how to bind one",
    )  # fmt: skip
    p.add_argument(
        "--quiet", action="store_true",
        help="the hook's way: the brief of the last sync written at once, refreshed in the "
        "background; prints nothing",
    )  # fmt: skip
    p.add_argument("--project", help="the project (default: the one init bound here)")
    p.add_argument("--dir", default=".", help="the repository (default here)")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)

    p = add(
        "why", cmd_why, "the facts behind a line of the brief, or a word",
        "the facts behind a line of the brief, or a word (a service, a package, an error "
        "code), with their job ids (project.why)",
    )  # fmt: skip
    p.add_argument("topic", nargs="*")
    p.add_argument("--project", help="the project (default: the one init bound here)")
    p.add_argument("--dir", default=".", help="the repository (default here)")

    p = add(
        "contract", cmd_contract, "what a build must meet: of a gap, a failing service or a need",
        "the build contract of a capability gap (gp_…), a failing service (its failures as "
        "tests to pass) or a typed need (a JSON file or -): need, inputs, outputs, tests, "
        "budget, where, source, deadline (capability.contract)",
    )  # fmt: skip
    p.add_argument("what", help="gp_… | a service (t_x.name, or name: yours) | need.json | -")

    p = add(
        "watch", cmd_watch, "wait for a note, a lease to end, or events: no polling",
        "wait on AMSYOS's event stream instead of asking again and again: the next note under "
        "a topic (or a topic below it), a lease that ends, a hand-over's end, a job's end, or "
        "any events of some kinds; exit code 3 when nothing came within --timeout",
        epilog="examples:\n  amsyos watch --note film --after n_...\n"
        "  amsyos watch --free film/part-2 --timeout 900\n"
        "  amsyos watch --handover ho_... --timeout 1800\n"
        "  amsyos watch --job j_...\n"
        "  amsyos watch --kinds job.state,task.waiting --count 5 --since 1234",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    p.add_argument("--note", nargs="?", const="", help="a topic: its next note (any topic without)")
    p.add_argument(
        "--after", help="with --note: a note id; a newer note already there answers at once"
    )
    p.add_argument("--free", metavar="WHAT", help="until nobody holds this lease")
    p.add_argument(
        "--handover", metavar="HO", help="until this hand-over is done, failed or rejected"
    )
    p.add_argument("--job", metavar="JOB", help="until this job ends or waits for somebody to act")
    p.add_argument(
        "--since", help="an event id (each answer says its last): take up from there, missing none"
    )
    p.add_argument("--kinds", help="event kinds, comma separated: job.state, note.posted, …")
    p.add_argument("--count", type=int, default=1, help="end after this many (default 1)")
    p.add_argument(
        "--timeout", type=float, default=600, help="seconds to wait at most (default 600)"
    )
    return top


def main(argv: list[str] | None = None) -> int:
    given = sys.argv[1:] if argv is None else argv
    top = parser()
    try:
        args, extra = top.parse_known_args(given)
        if extra:
            raise UsageError(_command_of(top, args), "unrecognized arguments", extra)
    except UsageError as err:
        return _usage(err, "--json" in given)
    out = Out(json=bool(args.json), show_urls=bool(args.show_urls), verbose=bool(args.verbose))
    func: Command | None = getattr(args, "func", None)
    if func is None:
        top.print_help()
        return 2
    began = time.monotonic()
    # At most once a day: a newer amsyos out? One line on stderr, `update` in JSON.
    told = updates.notice(getattr(args, "command", None))
    if told is not None:
        out.extra["update"] = told
    ending = session_ending()  # its last 2 hours: the agent tells the person in time
    if ending is not None:
        out.extra["session_ending"] = ending
    try:
        code = asyncio.run(func(args, out))
    except CliError as err:
        code = out.fail(err)
    except KeyboardInterrupt:
        sys.stderr.write(f"stopped after {time.monotonic() - began:.0f}s\n")
        return 130
    around.said(out)  # what others in the workspace did, after the command's own output
    said = getattr(args, "command", None) == "whoami" and code == 0  # whoami says it itself
    if told is not None and not out.json and not said:
        sys.stderr.write("".join(f"{line}\n" for line in updates.update_lines(told)))
    if ending is not None and not out.json:
        sys.stderr.write(ending + "\n")
    return code
