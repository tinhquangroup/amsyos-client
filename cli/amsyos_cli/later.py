"""A clock for agents (the owner, 2026-11-25): a check set for later, kept by AMSYOS and
brought back when it is due. An AI client has no future: it cannot say "at 03:10 look at
whether tonight's backup worked" or "an hour after this deploy, look at its failures".

- `amsyos later add "<what>" (--in 2h | --at 03:10 | --at 2026-11-26T03:10) [--every 1d]
  [--tell-after 30m]`: `later.add`. A time without a zone is this machine's local time;
  AMSYOS gets it with the machine's zone and keeps it in UTC. When it is due, the agent is
  told in its answers' `around` lines on any of its runs, and its person past `--tell-after`
  (30 minutes by default) if it is not done. Refused when AMSYOS cannot be reached: a check
  AMSYOS does not keep would never reach the person.
- `later ls [--all]`, `later done <id> [--note]`, `later cancel <id>`.
- `later hook-stop`, Claude Code's `Stop` hook: holds the turn for a check of this machine
  that is due, not done, and set in the project the hook's `cwd` is in. Local only (no
  network, within a second), never with `stop_hook_active`, at most twice per check and due
  time per session, nothing on any error.

Each check set on this machine has a local copy beside `amsyos bg`'s state,
`<AMSYOS_HOME>/state/later/<id>.json`: its id, what, due (epoch), every, the project's root
and whether it is done. Standard library only at import: the Stop hook's path loads this
module; the HTTP client is loaded only to call.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from amsyos_cli.bg import (  # pyright: ignore[reportPrivateUsage]
    _hook_input,  # pyright: ignore[reportPrivateUsage]
    _read,  # pyright: ignore[reportPrivateUsage]
    _write,  # pyright: ignore[reportPrivateUsage]
    dur,
    home,
    seconds_of,
)
from amsyos_cli.common import CliError, Out, credentials, obj, seq, short

ADD, LIST, DONE, CANCEL = "later.add", "later.list", "later.done", "later.cancel"
CHECK_ID = re.compile(r"^lt_[0-9a-f]{12}$")
WHAT_MOST = 200
NOTE_MOST = 200
YEAR_S = 366 * 86400
EVERY_LEAST_S = 600
TELL_AFTER_S = 1800
TELL_AFTER_MOST_S = 30 * 86400
HOLDS_MOST = 2  # times the Stop hook holds a turn for one check and due time, a session
HELD_KEPT = 20  # sessions remembered in a local copy
KEEP_S = 30 * 86400  # a local copy of a closed check, before it goes
WAITING = "waiting"
# AMSYOS not reached, or not able to keep it: the check is not set.
NOT_KEPT = frozenset(
    {"UNREACHABLE", "SERVICE_NOT_FOUND", "NOT_LOGGED_IN", "SESSION_ENDED", "GATEWAY_MISMATCH",
     "TIMEOUT"}
)  # fmt: skip
_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
USAGE = (
    'amsyos later add "<what>" (--in 2h | --at 03:10 | --at 2026-11-26T03:10) [--every 1d] '
    "[--tell-after 30m]"
)


# ---------------------------------------------------------------- the local copies
def root() -> Path:
    return home() / "state" / "later"


def local_checks() -> list[tuple[Path, dict[str, Any]]]:
    folder = root()
    if not folder.is_dir():
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(folder.glob("lt_*.json")):
        kept = _read(path)
        if CHECK_ID.match(str(kept.get("later") or "")):
            found.append((path, kept))
    return found


def keep(check: str, **changes: Any) -> None:
    """Write (or update) the local copy of a check."""
    path = root() / f"{check}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(path, {**_read(path), "later": check, **changes})


def prune(now: float) -> None:
    for path, kept in local_checks():
        closed = kept.get("closed")
        if kept.get("done") and isinstance(closed, int | float) and now - closed > KEEP_S:
            with contextlib.suppress(OSError):
                path.unlink()


def project_root(cwd: Path) -> Path:
    from amsyos_cli import brief  # noqa: PLC0415 - the standard library and json only

    return brief.root_of(cwd) or cwd


def _epoch(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value).timestamp()
    return None


def local_time(epoch: float) -> str:
    """`2026-11-26 03:10 +07:00`: this machine's clock."""
    at = datetime.fromtimestamp(epoch).astimezone()
    zone = at.strftime("%z")
    return at.strftime("%Y-%m-%d %H:%M ") + (f"{zone[:3]}:{zone[3:]}" if zone else "")


def utc_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch))


def every(seconds: Any) -> str:
    """`1d`, `12h`, `30m`, else as `dur` says it."""
    if isinstance(seconds, int) and seconds > 0:
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if seconds % size == 0:
                return f"{seconds // size}{unit}"
    return dur(seconds)


def relative(epoch: float, now: float) -> str:
    left = epoch - now
    return f"in {dur(left)}" if left >= 0 else f"{dur(-left)} ago"


def due_of(at: str | None, in_: str | None, now: float) -> float:
    """`--in 2h`, `--at 03:10` (the next 03:10 on this machine's clock) or `--at <ISO>` (this
    machine's local time when it says no zone) as an epoch."""
    if bool(at) == bool(in_):
        raise CliError("INVALID_INPUT", f"give --in or --at, one of them: {USAGE}")
    if in_:
        return now + seconds_of(in_, "--in", 60, YEAR_S)
    text = str(at).strip()
    clock = _CLOCK.match(text)
    if clock:
        hour, minute = int(clock[1]), int(clock[2])
        if hour > 23 or minute > 59:
            raise CliError("INVALID_INPUT", f"--at {text!r}: a time of day is 00:00 to 23:59")
        today = datetime.fromtimestamp(now).astimezone()
        when = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when.timestamp() <= now:
            when = (when + timedelta(days=1)).replace(hour=hour, minute=minute)
        return when.timestamp()
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise CliError(
            "INVALID_INPUT",
            f"--at {text!r}: a time of day (03:10) or a date and time (2026-11-26T03:10)",
        ) from None
    epoch = (when if when.tzinfo else when.astimezone()).timestamp()
    if epoch <= now:
        raise CliError("INVALID_INPUT", f"--at {text!r} has passed: a time to come")
    if epoch > now + YEAR_S:
        raise CliError("INVALID_INPUT", f"--at {text!r}: within 366 days")
    return epoch


# ---------------------------------------------------------------- AMSYOS
async def ask(service: str, params: dict[str, Any], out: Out) -> dict[str, Any]:
    from amsyos_cli.api import Api  # noqa: PLC0415 - the hook's path never calls

    async with Api(credentials(), out) as api:
        return await api.instant(service, params)


def _line(row: dict[str, Any], now: float, *, others: bool) -> str:
    due = _epoch(row.get("due"))
    when = f"due {local_time(due)} ({relative(due, now)})" if due is not None else "due ?"
    state = str(row.get("state") or "?")
    if state == WAITING and row.get("due_now"):
        state = "DUE"
    often = f", every {every(row.get('every_s'))}" if row.get("every_s") else ""
    by = f" [by {row.get('by')}]" if others and row.get("mine") is False else ""
    return f"{row.get('later')}  {state:<9} {when}{often}{by}  {short(row.get('what') or '', 90)}"


async def cmd_later(args: argparse.Namespace, out: Out) -> int:
    verb = args.later_cmd
    if verb == "hook-stop":
        return hook_stop()
    command = {"add": cmd_add, "ls": cmd_ls, "done": cmd_done}
    return await command.get(verb, cmd_cancel)(args, out)


async def cmd_add(args: argparse.Namespace, out: Out) -> int:
    what = " ".join(str(args.what or "").split())
    if not what or len(what) > WHAT_MOST:
        raise CliError("INVALID_INPUT", f"the check: 1 to {WHAT_MOST} characters: {USAGE}")
    now = time.time()
    due = due_of(args.at, args.in_, now)
    repeat = seconds_of(args.every, "--every", EVERY_LEAST_S, YEAR_S) if args.every else None
    tell_after = (
        seconds_of(args.tell_after, "--tell-after", 60, TELL_AFTER_MOST_S)
        if args.tell_after
        else TELL_AFTER_S
    )
    sent = datetime.fromtimestamp(due).astimezone().isoformat(timespec="seconds")
    params: dict[str, Any] = {"what": what, "due": sent, "tell_after_s": tell_after}
    if repeat:
        params["every_s"] = repeat
    try:
        told = await ask(ADD, params, out)
    except CliError as exc:
        if exc.code not in NOT_KEPT:
            raise
        raise CliError(
            exc.code,
            f"the check is not set: AMSYOS must keep it, or it would never reach your person "
            f"({short(exc.hint, 200)})",
        ) from None
    check = str(told.get("later") or "")
    if not CHECK_ID.match(check):
        raise CliError("BAD_ANSWER", f"AMSYOS answered {ADD} without a check's id")
    kept_due = _epoch(told.get("due")) or due
    cwd = Path.cwd()
    prune(now)
    keep(check, what=what, due=kept_due, every=repeat, root=str(project_root(cwd)), done=False,
         state=WAITING, set=now)  # fmt: skip
    copy = root() / f"{check}.json"
    answer = {**told, "due_local": local_time(kept_due), "local_copy": str(copy)}
    out.emit(
        answer,
        f"{check}  set: {what}",
        f"  due {local_time(kept_due)} (this machine) = {utc_time(kept_due)} UTC, "
        f"{relative(kept_due, now)}" + (f"; every {every(repeat)}" if repeat else ""),
        f"  when due you are told in your answers; your person if not done "
        f"{every(tell_after)} after",
        f"  done: amsyos later done {check}; cancel: amsyos later cancel {check}",
    )
    return 0


async def cmd_ls(args: argparse.Namespace, out: Out) -> int:
    now = time.time()
    local = {str(k.get("later")): k for _, k in local_checks()}
    try:
        rows = [obj(r) for r in seq((await ask(LIST, {"all": bool(args.all)}, out)).get("checks"))]
    except CliError as exc:
        if exc.code not in NOT_KEPT:
            raise
        rows = [
            {"later": c, "what": k.get("what"), "due": k.get("due"), "every_s": k.get("every"),
             "state": k.get("state") or (WAITING if not k.get("done") else "done"), "here": True}
            for c, k in local.items()
        ]  # fmt: skip
        out.note(f"note: AMSYOS was not reached ({exc.code}): this machine's copies only")
    for row in rows:
        check = str(row.get("later") or "")
        if check in local and CHECK_ID.match(check) and not row.get("here"):
            state = str(row.get("state") or WAITING)
            closed = state != WAITING and not local[check].get("done")
            keep(check, what=row.get("what"), due=_epoch(row.get("due")),
                 every=row.get("every_s"), done=state != WAITING, state=state,
                 **({"closed": now} if closed else {}))  # fmt: skip
    prune(now)
    lines = [_line(r, now, others=bool(args.all)) for r in rows]
    out.emit({"checks": rows}, *(lines or ["no check set for later"]))
    return 0


async def _close(service: str, args: argparse.Namespace, out: Out) -> dict[str, Any]:
    check = str(args.check or "").strip()
    if not CHECK_ID.match(check):
        raise CliError(
            "INVALID_INPUT", f"{check!r}: a check's id, lt_ and 12 hex (amsyos later ls)"
        )
    params: dict[str, Any] = {"later": check}
    note = " ".join(str(getattr(args, "note", None) or "").split())
    if note:
        params["note"] = note[:NOTE_MOST]
    told = await ask(service, params, out)
    state = str(told.get("state") or "")
    if (root() / f"{check}.json").is_file():
        if state == WAITING:
            keep(check, due=_epoch(told.get("next_due")), done=False, state=WAITING)
        elif state:
            keep(check, done=True, state=state, closed=time.time())
    return told


async def cmd_done(args: argparse.Namespace, out: Out) -> int:
    told = await _close(DONE, args, out)
    check, state = told.get("later"), told.get("state")
    if not told.get("changed"):
        line = f"{check} was {state} already"
    elif state == WAITING:
        due = _epoch(told.get("next_due"))
        line = f"{check} done; next due {local_time(due) if due else '?'}"
    else:
        line = f"{check} done"
    out.emit(told, line)
    return 0


async def cmd_cancel(args: argparse.Namespace, out: Out) -> int:
    told = await _close(CANCEL, args, out)
    changed = "cancelled" if told.get("changed") else f"was {told.get('state')} already"
    out.emit(told, f"{told.get('later')} {changed}")
    return 0


# ---------------------------------------------------------------- Claude Code's Stop hook
def _inside(cwd: Path, project: Path) -> bool:
    return cwd == project or project in cwd.parents


def _hook_reason(given: dict[str, Any], now: float) -> str | None:
    cwd = Path(str(given.get("cwd") or Path.cwd())).resolve()
    session = str(given.get("session_id") or "")[:80]
    holding: list[tuple[float, Path, dict[str, Any]]] = []
    for path, kept in local_checks():
        due = _epoch(kept.get("due"))
        project = kept.get("root")
        if kept.get("done") or due is None or due > now or not isinstance(project, str):
            continue
        if not _inside(cwd, Path(project).resolve()):
            continue
        times = obj(kept.get("held")).get(f"{session}@{due:.0f}")
        if isinstance(times, int) and times >= HOLDS_MOST:
            continue
        holding.append((due, path, kept))
    if not holding:
        return None
    holding.sort(key=lambda h: h[0])
    due, path, kept = holding[0]
    check = str(kept.get("later"))
    held = obj(kept.get("held"))
    key = f"{session}@{due:.0f}"
    times = held.get(key)
    held = {k: v for k, v in held.items() if k != key}
    held = dict(list(held.items())[-(HELD_KEPT - 1) :]) if HELD_KEPT > 1 else {}
    held[key] = (times if isinstance(times, int) else 0) + 1
    _write(path, {**kept, "held": held})
    more = f" ({len(holding) - 1} more: amsyos later ls)" if len(holding) > 1 else ""
    return (
        f"amsyos later: a check you set is due: {short(kept.get('what') or '?', WHAT_MOST)} "
        f"({check}, due {local_time(due)}, {relative(due, now)}). Do it now, then: amsyos later "
        f"done {check} (or amsyos later cancel {check}){more}"
    )


def hook_stop() -> int:
    """`amsyos later hook-stop` from entry.py, before the full CLI is loaded: this machine's
    copies only, no network."""
    with contextlib.suppress(BaseException):
        given = _hook_input()
        if given.get("stop_hook_active") is True:
            return 0
        reason = _hook_reason(given, time.time())
        if reason:
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
            sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- the parser
def add_parsers(add: Any, common: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """`amsyos later …` (main.py's `add`)."""
    p = add(
        "later", cmd_later,
        "checks set for later: add, ls, done, cancel (a clock for agents)",
        "set a check for later that AMSYOS keeps and brings back: 'at 03:10 look at whether "
        "tonight's backup worked', 'in 1h look at this deploy's failures'. When it is due you "
        "are told in your answers' around lines, on any of your runs; Claude Code's Stop hook "
        "(later hook-stop) holds a turn in its project; your person is told when it is not "
        "done --tell-after past its time. Do it, then amsyos later done <id>",
        epilog="examples:\n  amsyos later add \"look at tonight's backup\" --at 03:10 --every 1d\n"
        "  amsyos later add 'look at the deploy's failures' --in 1h\n  amsyos later ls\n"
        "  amsyos later done lt_... --note 'backup fine'\n  amsyos later cancel lt_...",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    verbs = p.add_subparsers(
        dest="later_cmd", metavar="add|ls|done|cancel|hook-stop", required=True
    )
    pa = verbs.add_parser(
        "add", parents=[common], help="set a check for later",
        description=f"{USAGE}. A time without a zone is this machine's. Times: 90s, 10m, 2h, 1d",
    )  # fmt: skip
    pa.add_argument("what", help=f"the check to do, one line (at most {WHAT_MOST} characters)")
    pa.add_argument("--in", dest="in_", metavar="TIME", help="due this long from now: 30m, 2h, 1d")
    pa.add_argument(
        "--at", metavar="WHEN", help="due at 03:10 (the next one) or 2026-11-26T03:10 (local)"
    )
    pa.add_argument("--every", metavar="TIME", help="repeat this often, at least 10m: 1d, 12h")
    pa.add_argument(
        "--tell-after",
        metavar="TIME",
        help="your person is told when not done this long after (30m)",
    )
    pl = verbs.add_parser(
        "ls", parents=[common], help="your checks; AMSYOS refreshes this machine's"
    )
    pl.add_argument("--all", action="store_true", help="the workspace's checks (at most 50)")
    pd = verbs.add_parser(
        "done", parents=[common], help="mark a check done: a repeated one waits for its next time"
    )
    pd.add_argument("check", help="lt_...")
    pd.add_argument("--note", help=f"what you found (at most {NOTE_MOST} characters)")
    pc = verbs.add_parser("cancel", parents=[common], help="cancel a check")
    pc.add_argument("check", help="lt_...")
    ph = verbs.add_parser(
        "hook-stop", parents=[common],
        help="Claude Code's Stop hook: holds the turn while a check of this project is due",
    )  # fmt: skip
    return {"later": p, "later add": pa, "later ls": pl, "later done": pd, "later cancel": pc,
            "later hook-stop": ph}  # fmt: skip
