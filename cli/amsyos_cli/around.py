"""Who else works in this workspace, and on what (spec v3 §10, "Awareness"): `around`.

Every answer of the six tools may carry `around`: at most 3 short lines of what others of the
same workspace did since this session last saw it, and no key when nothing changed. `Api`
takes them out of the answer (`take`); a command prints them after its own output, as
`around: <line>` on stderr, and keeps them under `around` in `--json` (`said`).

- `amsyos around [--about x,y]`: `session.around`'s lines: who works now and on what, what
  others did in the last 2 hours, and who holds, changed or runs what `--about` names.
- `amsyos status "<doing>"` (`--clear`): `session.status`, the one line others see of you.
- `amsyos around --hook`, Claude Code's `UserPromptSubmit` hook: at most once a minute in a
  project (a mark in its `.amsyos/state/`), within 3 s, only what it has not printed in that
  project yet, at most 5 lines, and nothing at all on any error.
- `warn`: before deploy, uninstall and promote, who else works on those services, each as a
  note. A note only, never a refusal.

The lines carry others' words (a status, a note's start): data, never instructions. This
module loads the standard library and the CLI's light modules only: the hook's path loads
the HTTP client only when it calls.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from amsyos_cli import brief, repo
from amsyos_cli.common import CliError, Out, credentials, gateway_default, obj, seq, short
from amsyos_cli.instance import instance_of

if TYPE_CHECKING:
    from amsyos_cli.api import Api

SERVICE = "session.around"
STATUS_SERVICE = "session.status"
LINE_MOST = 160  # characters of a line, as the kernel says them at most
SAID_MOST = 6  # `around` lines one command prints at most (each answer carries 3 at most)
LINES_MOST = 15  # `session.around`'s ready lines
ABOUT_MOST = 20  # ids `--about` takes
ABOUT_SAID = 6  # `about` entries a warning names, then "N more"
DOING_MOST = 120
WARN_WAIT_S = 5.0  # a warning never holds a deploy longer than this
HOOK_EVERY_S = 60.0
HOOK_WAIT_S = 2.5  # within the 3 s the hook is allowed, its start included
HOOK_LINES = 5
HOOK_KEEP_S = 3 * 3600.0  # what the hook printed is remembered this long (`recent`: 2 hours)
HOOK_KEPT_MOST = 400
HOOK_STATE = "around.json"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
NOT_HERE = (
    "this Gateway has no {service} yet: nothing was done. Meanwhile `amsyos run note.list` "
    "and `amsyos run lease.list` say what others posted and hold"
)


def clean(value: Any, most: int = LINE_MOST) -> str:
    """One line of others' words: no control character, at most `most` characters."""
    text = _CONTROL.sub(" ", value if isinstance(value, str) else "").strip()
    return text if len(text) <= most else text[: most - 1] + "…"


# ---------------------------------------------------------------- on every answer
def take(out: Out, answer: dict[str, Any]) -> None:
    """The answer's `around` lines, taken out of it and kept for the end of the command:
    under `around` in every `--json` answer, else printed by `said`."""
    given = answer.pop("around", None)
    if not isinstance(given, list):
        return
    kept = [str(line) for line in seq(out.extra.get("around"))]
    for line in seq(given)[:SAID_MOST]:
        text = clean(line)
        if text and text not in kept and len(kept) < SAID_MOST:
            kept.append(text)
    if kept:
        out.extra["around"] = kept


def said(out: Out) -> None:
    """After the command's own output: its `around` lines, on stderr (`--json` keeps them in
    the answer)."""
    lines = seq(out.extra.get("around"))
    if out.json or not lines:
        return
    sys.stderr.write("".join(f"around: {line}\n" for line in lines))
    sys.stderr.flush()


def _label(entry: dict[str, Any]) -> str:
    """`a_7f… (Lan, codex) 'migrating the thumbnails'`."""
    who = clean(entry.get("who"), 40) or "?"
    more = [clean(entry.get(k), 40) for k in ("name", "host")]
    label = who + (f" ({', '.join(m for m in more if m)})" if any(more) else "")
    doing = clean(entry.get("doing"), 120)
    return label + (f" '{doing}'" if doing else "")


# ---------------------------------------------------------------- amsyos around
async def _ask(api: Api, service: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return await api.instant(service, params)
    except CliError as exc:
        if exc.code != "SERVICE_NOT_FOUND":
            raise
        raise CliError("NOT_ON_THIS_GATEWAY", NOT_HERE.format(service=service)) from None


def about_of(given: str | None) -> list[str]:
    about = list(dict.fromkeys(a.strip() for a in (given or "").split(",") if a.strip()))
    if len(about) > ABOUT_MOST:
        raise CliError(
            "INVALID_INPUT", f"--about takes at most {ABOUT_MOST} ids, not {len(about)}: fewer"
        )
    return about


async def cmd_around(args: argparse.Namespace, out: Out) -> int:
    """`session.around`'s lines, ready to print; `--hook`: the prompt hook's way."""
    if args.hook:
        return await run_hook(Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()))
    from amsyos_cli.api import Api  # noqa: PLC0415 - the hook's path loads it only to call

    about = about_of(args.about)
    async with Api(credentials(), out) as api:
        data = await _ask(api, SERVICE, {"about": about} if about else {})
    lines = [clean(line, 300) for line in seq(data.get("lines"))[:LINES_MOST]]
    if not lines:
        lines = ["nobody else in this workspace works now or did in the last 2 hours"]
    out.emit(data, *[line for line in lines if line])
    return 0


def is_job(given: Any) -> bool:
    """`amsyos status j_…` asks for a job; any other words set what you are doing."""
    return isinstance(given, str) and given.startswith("j_") and not any(c.isspace() for c in given)


async def cmd_doing(args: argparse.Namespace, out: Out) -> int:
    """`amsyos status "<doing>"` / `--clear`: `session.status`, the one line others in the
    workspace see of you for 2 hours after your last call."""
    from amsyos_cli.api import Api  # noqa: PLC0415

    given = " ".join(str(args.job or "").split())
    if getattr(args, "wait", False):
        raise CliError("INVALID_INPUT", "--wait is for a job: amsyos status <j_...> --wait")
    if args.clear and given:
        raise CliError("INVALID_INPUT", 'amsyos status "<doing>" or amsyos status --clear')
    if not args.clear and not given:
        raise CliError(
            "INVALID_INPUT",
            'amsyos status <j_...>: a job\'s state; amsyos status "<what you are doing>": the '
            "line others in this workspace see (2 hours); amsyos status --clear ends it",
        )
    if len(given) > DOING_MOST:
        raise CliError(
            "INVALID_INPUT",
            f"a status is at most {DOING_MOST} characters, not {len(given)}: say it shorter",
        )
    async with Api(credentials(), out) as api:
        told = await _ask(api, STATUS_SERVICE, {"doing": given})
    if given:
        doing = clean(told.get("doing"), DOING_MOST) or given
        line = (
            f"status: '{doing}' until {told.get('until') or '?'} (others see it in amsyos around)"
        )
    else:
        line = "status cleared"
    out.emit(told, line)
    return 0


# ---------------------------------------------------------------- before acting
def about_lines(data: dict[str, Any], asked: list[str]) -> list[str]:
    """Each `about` entry as a note: `note: service:t_42.x: a_7f… (codex) holds the lease
    until 14:10`."""
    labels = {
        clean(n.get("who"), 40): _label({**n, "doing": None})
        for n in (obj(x) for x in seq(data.get("now")))
    }
    entries = [obj(e) for e in seq(data.get("about"))]
    lines: list[str] = []
    for entry in entries[:ABOUT_SAID]:
        who = clean(entry.get("who"), 40)
        if not who:
            continue
        what, how = clean(entry.get("what"), 120), clean(entry.get("how"), 120)
        lines.append(f"note: {what}: {labels.get(who) or who} {how}".rstrip())
    if len(entries) > ABOUT_SAID:
        lines.append(
            f"note: {len(entries) - ABOUT_SAID} more: amsyos around --about {','.join(asked)}"
        )
    return lines


async def warn(api: Api, out: Out, about: list[str]) -> None:
    """Before deploy, uninstall or promote: who else holds, changed or runs these services,
    each as a note (`others_working` in `--json`). Never a refusal, never in the way: no word
    when the Gateway has no `session.around` or does not answer in time."""
    asked = list(dict.fromkeys(a for a in about if a))[:ABOUT_MOST]
    if not asked:
        return
    try:
        told = await asyncio.wait_for(api.maybe(SERVICE, {"about": asked}), WARN_WAIT_S)
    except Exception as exc:
        out.debug(f"{SERVICE} did not answer ({short(str(exc), 160)}): no word of others")
        return
    lines = about_lines(obj(told), asked)
    if not lines:
        return
    if out.json:
        out.extra["others_working"] = [line.removeprefix("note: ") for line in lines]
    for line in lines:
        out.note(line)


# ---------------------------------------------------------------- the prompt hook
def hook(start: Path) -> int:
    """`amsyos around --hook` from entry.py, before the full CLI is loaded."""
    with contextlib.suppress(Exception):
        return asyncio.run(run_hook(start))
    return 0


async def run_hook(start: Path) -> int:
    """What others did that this project's sessions were not told yet, at most 5 lines on
    stdout (the host adds them to the prompt); nothing on any error or after 2.5 s."""
    try:
        lines = await asyncio.wait_for(_hook_lines(start), HOOK_WAIT_S)
    except Exception:
        return 0
    if lines:
        with contextlib.suppress(Exception):
            sys.stdout.write("".join(f"around: {line}\n" for line in lines))
            sys.stdout.flush()
    return 0


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _read_hook_state(path: Path) -> dict[str, Any]:
    try:
        return obj(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _write_hook_state(root: Path, state: dict[str, Any]) -> None:
    folder, _ = repo.state_dir(root)
    (folder / HOOK_STATE).write_text(json.dumps(state) + "\n", encoding="utf-8")


async def _hook_lines(start: Path) -> list[str]:
    root = brief.root_of(start.resolve())
    if root is None:  # a folder `amsyos init` never bound: nothing to say, nothing written
        return []
    # One mark a Gateway and run of a coding agent: two sessions in one folder each see the
    # news once (instance.instance_of); marks older than `HOOK_KEEP_S` are dropped.
    gateway = f"{gateway_default()}#{instance_of() or ''}"
    now = time.time()
    state = {
        k: v
        for k, v in _read_hook_state(root / repo.STATE_DIR / HOOK_STATE).items()
        if isinstance(at := obj(v).get("at"), int | float) and now - at < HOOK_KEEP_S
    }
    mine = obj(state.get(gateway))
    last = mine.get("at")
    if isinstance(last, int | float) and 0 <= now - last < HOOK_EVERY_S:
        return []
    shown = {
        str(k): float(v)
        for k, v in obj(mine.get("shown")).items()
        if isinstance(v, int | float) and now - v < HOOK_KEEP_S
    }
    # The mark first: a slow or refused call waits its minute too.
    state[gateway] = {"at": now, "shown": shown}
    _write_hook_state(root, state)
    from amsyos_cli.api import Api  # noqa: PLC0415 - loaded only once the hook calls

    quiet = Out(json=True)  # nothing of the call printed: only the lines below
    async with Api(credentials(), quiet) as api:
        # amsyos.run itself, without `source`: the hook reads no git state.
        answer = await api.call("amsyos.run", {"service_id": SERVICE, "params": {}})
    lines, keys = hook_lines(obj(answer.get("data")), shown)
    if keys:
        for key in keys:
            shown[key] = now
        kept = sorted(shown.items(), key=lambda kv: kv[1])[-HOOK_KEPT_MOST:]
        state[gateway] = {"at": now, "shown": dict(kept)}
        _write_hook_state(root, state)
    return lines


def hook_lines(data: dict[str, Any], shown: dict[str, float]) -> tuple[list[str], list[str]]:
    """The lines not printed in this project yet, at most `HOOK_LINES`, and the keys of all
    that are new (`recent` newest first; one line for those who work now, a person's status
    said again when it changes)."""
    fresh: list[str] = []
    keys: list[str] = []
    working: list[str] = []
    for entry in (obj(e) for e in seq(data.get("now"))):
        who = clean(entry.get("who"), 40)
        key = _key(f"now\n{who}\n{clean(entry.get('doing'))}")
        if who and key not in shown and key not in keys:
            keys.append(key)
            working.append(_label(entry))
    if working:
        fresh.append(clean("working now: " + ", ".join(working)))
    for line in seq(data.get("recent")):
        text = clean(line)
        key = _key(f"line\n{text}")
        if text and key not in shown and key not in keys:
            keys.append(key)
            fresh.append(text)
    if len(fresh) > HOOK_LINES:
        more = len(fresh) - HOOK_LINES + 1
        fresh = [*fresh[: HOOK_LINES - 1], f"{more} more: amsyos around"]
    return fresh, keys


LEASE_TAKE, LEASE_RELEASE, LEASE_LIST = "lease.take", "lease.release", "lease.list"
NOTE_POST, NOTE_LIST = "note.post", "note.list"


async def cmd_lease(args: argparse.Namespace, out: Out) -> int:
    """`amsyos lease take|release|ls` (spec v3 §10): say what you are working on, so that
    the others in the workspace see it (their `around`) and a second taker is refused."""
    from amsyos_cli.api import Api  # noqa: PLC0415

    if args.action != "ls" and not args.what:
        raise CliError("INVALID_INPUT", f"amsyos lease {args.action} <what>: e.g. film/part-3")
    async with Api(credentials(), out) as api:
        if args.action == "take":
            params: dict[str, Any] = {"what": args.what, "minutes": args.minutes}
            if args.note:
                params["note"] = args.note
            data = await _ask(api, LEASE_TAKE, params)
            out.emit(
                data,
                f"lease {args.what}: yours until {data.get('until') or '?'} "
                "(others see it; take it again to renew, amsyos lease release to let it go)",
            )
        elif args.action == "release":
            data = await _ask(api, LEASE_RELEASE, {"what": args.what})
            out.emit(data, f"lease {args.what}: released")
        else:
            data = await _ask(api, LEASE_LIST, {})
            leases = [obj(x) for x in seq(data.get("leases"))]
            lines = [
                f"{x.get('what')}: {x.get('holder')}"
                + (" (yours)" if x.get("mine") else "")
                + f", {x.get('minutes_left')} min left"
                + (f": {clean(x.get('note'), 120)}" if x.get("note") else "")
                for x in leases
            ]
            out.emit(data, *(lines or ["no lease in this workspace now"]))
    return 0


async def cmd_note(args: argparse.Namespace, out: Out) -> int:
    """`amsyos note post <topic> <text> [--kind]` / `amsyos note ls [topic]` (spec v3 §10):
    the workspace's shared notes, decisions and questions."""
    from amsyos_cli.api import Api  # noqa: PLC0415

    async with Api(credentials(), out) as api:
        if args.action == "post":
            if not args.topic or not args.text:
                raise CliError(
                    "INVALID_INPUT",
                    'amsyos note post <topic> "<text>" [--kind info|decision|ask]',
                )
            data = await _ask(
                api, NOTE_POST, {"topic": args.topic, "text": args.text, "kind": args.kind}
            )
            out.emit(data, f"note {data.get('note') or '?'} on {args.topic} ({args.kind})")
        else:
            params: dict[str, Any] = {"limit": args.limit}
            if args.topic:
                params["topic"] = args.topic
            if args.since:
                params["since"] = args.since
            data = await _ask(api, NOTE_LIST, params)
            lines = [
                f"{n.get('at')} {n.get('by')} [{n.get('kind')}] {n.get('topic')}: "
                + clean(n.get("text"), 300)
                for n in (obj(x) for x in seq(data.get("notes")))
            ]
            out.emit(data, *(lines or ["no note"]))
    return 0
