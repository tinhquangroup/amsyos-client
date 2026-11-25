"""Requests between AI clients of different machines and organisations (the owner,
2026-11-25): ask another workspace, of your organisation or another, for something, and get
the answer back through AMSYOS, durably. An AI client whose login cannot do a step (it
belongs to another organisation) asks those who can, instead of a person relaying by hand.

- `amsyos ask <t_…|w_…> "<text>" [--for <principal>]`: `ask.send`. Any workspace of your own
  organisation takes it; another organisation's once its managers allowed requests from
  yours (`amsyos contact allow`). Until then it waits (`asked_contact`) and they are asked.
  `-` as the text reads it from stdin (multi-line, at most 2000 characters).
- `amsyos ask ls [--out] [--all]`, `amsyos ask show <rq_…>`, `amsyos ask close <rq_…>`.
- `amsyos reply <rq_…> "<text>" [--close]`: `ask.reply`, either side.
- `amsyos contact allow <t_…> [--until <date>]`, `amsyos contact block <t_…>`, `amsyos contact
  ls`: which organisations this workspace takes requests from (a manager allows and blocks).

A new request, or a reply to yours, is told in the `around` lines of your answers.
The other side's words are printed without control characters.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from typing import Any

from amsyos_cli.common import CliError, Out, credentials, obj, seq

SEND, LIST, SHOW, REPLY, CLOSE = "ask.send", "ask.list", "ask.show", "ask.reply", "ask.close"
ALLOW, BLOCK, CONTACTS = "contact.allow", "contact.block", "contact.list"
REQUEST_ID = re.compile(r"^rq_[0-9a-f]{12}$")
TARGET = re.compile(r"^(t_[A-Za-z0-9_]{1,60}|w_[0-9A-Za-z_]{1,62})$")
ORG = re.compile(r"^t_[A-Za-z0-9_]{1,60}$")
PRINCIPAL = re.compile(r"^[ua]_[0-9a-f]{10}$")
TEXT_MOST = 2000
VERBS = ("ls", "show", "close")
OPEN = ("asked_contact", "delivered")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # newline and tab stay
USAGE = (
    'amsyos ask <t_…|w_…> "<text>" [--for <principal>] | ask ls [--out] [--all] | '
    "ask show <rq_…> | ask close <rq_…>"
)


# ---------------------------------------------------------------- small helpers
def clean(value: Any, most: int | None = None, *, lines: bool = False) -> str:
    """Another side's words, safe to print: no control character (and on one line unless
    `lines`), at most `most` characters."""
    text = _CONTROL.sub(" ", value if isinstance(value, str) else "")
    if not lines:
        text = " ".join(text.split())
    if most is not None and len(text) > most:
        text = text[: most - 1] + "…"
    return text


def ago(iso: Any, now: float) -> str:
    if not isinstance(iso, str) or not iso:
        return "?"
    try:
        at = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return "?"
    s = max(0, int(now - at))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago" if s < 2 * 86400 else f"{s // 86400}d ago"


def _who(org: Any, by: Any, name: Any) -> str:
    org, by, name = clean(org, 64), clean(by, 64), clean(name, 40)
    if not by or by == org:
        return org or "?"
    return f"{org} ({by}, {name})" if name else f"{org} ({by})"


def _text(words: list[str]) -> str:
    """The text the command was given: its words, or stdin for `-`."""
    given = sys.stdin.read() if words == ["-"] else " ".join(words)
    text = given.replace("\r\n", "\n").strip()
    if not text:
        raise CliError(
            "INVALID_INPUT", f'the text, in quotes after the id: {USAGE}; "-" reads stdin'
        )
    if len(text) > TEXT_MOST:
        raise CliError(
            "INVALID_INPUT",
            f"the text has {len(text)} characters, at most {TEXT_MOST}: shorten it, or put the "
            "details in a file of the workspace (amsyos files put) and name it",
        )
    return text


def _request(words: list[str], how: str) -> str:
    if len(words) != 1 or not REQUEST_ID.match(words[0]):
        raise CliError(
            "INVALID_INPUT", f"{how} takes one request's id, rq_ and 12 hex (amsyos ask ls)"
        )
    return words[0]


async def call(service: str, params: dict[str, Any], out: Out) -> dict[str, Any]:
    from amsyos_cli.api import Api  # noqa: PLC0415 - loaded only to call

    async with Api(credentials(), out) as api:
        return await api.instant(service, params)


# ---------------------------------------------------------------- amsyos ask
async def cmd_ask(args: argparse.Namespace, out: Out) -> int:
    first = str(args.first or "")
    rest = [str(w) for w in seq(args.rest)]
    if first in VERBS:
        if args.for_:
            raise CliError("INVALID_INPUT", f"--for goes with a new request only: {USAGE}")
        if first == "ls":
            if rest:
                raise CliError("INVALID_INPUT", "amsyos ask ls takes no words; --out, --all")
            return await _ls(args, out)
        if args.out or args.all:
            raise CliError("INVALID_INPUT", f"--out and --all go with ask ls only: {USAGE}")
        if first == "show":
            return await _show(_request(rest, "amsyos ask show"), out)
        return await _close(_request(rest, "amsyos ask close"), out)
    if not TARGET.match(first):
        raise CliError(
            "INVALID_INPUT",
            f"amsyos ask's first word is the target (an organisation's id t_… or a workspace's "
            f"w_…) or ls, show, close: {USAGE}",
        )
    if args.out or args.all:
        raise CliError("INVALID_INPUT", f"--out and --all go with ask ls only: {USAGE}")
    params: dict[str, Any] = {"to": first, "text": _text(rest)}
    if args.for_:
        if not PRINCIPAL.match(args.for_):
            raise CliError("INVALID_INPUT", "--for: a principal's id, u_… or a_… and 10 hex")
        params["for"] = args.for_
    told = await call(SEND, params, out)
    request, state = str(told.get("request") or "?"), str(told.get("state") or "?")
    if state == "delivered":
        line = (
            f"{request}  delivered to {first}; the answer comes in your around lines: amsyos ask "
            f"show {request}"
        )
    else:
        line = (
            f"{request}  waits ({state}): {first} has not allowed requests from your organisation "
            "yet; its managers are asked, and it is delivered once they allow it (amsyos ask ls "
            "--out)"
        )
    out.emit(told, line)
    return 0


async def _ls(args: argparse.Namespace, out: Out) -> int:
    box = "out" if args.out else "in"
    told = await call(LIST, {"box": box, "all": bool(args.all)}, out)
    now = time.time()
    lines: list[str] = []
    for row in (obj(r) for r in seq(told.get("requests"))):
        request, state = clean(row.get("request"), 16), clean(row.get("state"), 16)
        if box == "in":
            whom = f"from {_who(row.get('from'), row.get('by'), row.get('name'))}"
            if row.get("for"):
                whom += f" for {clean(row.get('for'), 16)}"
        else:
            whom = f"to {clean(row.get('to'), 64)}"
        lines.append(
            f"{request}  {state:<13} {whom}  {ago(row.get('sent'), now)}  "
            f"{clean(row.get('text'), 90)}"
        )
        reply = obj(row.get("last_reply"))
        if reply:
            by = "you" if reply.get("side") == "yours" else clean(reply.get("org"), 64)
            said = clean(reply.get("text"), 110)
            lines.append(f"    last reply ({by}, {ago(reply.get('at'), now)}): {said}")
    if not lines:
        which = "received" if box == "in" else "sent"
        lines = [f"no open request {which}" + ("" if args.all else " (--all: closed ones too)")]
    out.emit(told, *lines)
    return 0


async def _show(request: str, out: Out) -> int:
    told = await call(SHOW, {"request": request}, out)
    now = time.time()
    if told.get("side") == "to":
        whom = f"from {_who(told.get('from'), told.get('by'), told.get('name'))}"
        if told.get("for"):
            whom += f" for {clean(told.get('for'), 16)}"
    else:
        whom = f"to {clean(told.get('to'), 64)}"
    lines = [
        f"{request}  {clean(told.get('state'), 16)}  {whom}  sent {ago(told.get('sent'), now)}",
        *(f"  {line}" for line in clean(told.get("text"), lines=True).split("\n")),
    ]
    for reply in (obj(r) for r in seq(told.get("replies"))):
        by = (
            "you"
            if reply.get("side") == "yours"
            else _who(reply.get("org"), reply.get("by"), reply.get("name"))
        )
        lines.append(f"reply by {by}, {ago(reply.get('at'), now)}:")
        lines += [f"  {line}" for line in clean(reply.get("text"), lines=True).split("\n")]
    if told.get("next"):
        lines.append(clean(told.get("next"), 300))
    out.emit(told, *lines)
    return 0


async def _close(request: str, out: Out) -> int:
    told = await call(CLOSE, {"request": request}, out)
    state = clean(told.get("state"), 16)
    out.emit(told, f"{request} closed" if told.get("changed") else f"{request} was {state} already")
    return 0


# ---------------------------------------------------------------- amsyos reply
async def cmd_reply(args: argparse.Namespace, out: Out) -> int:
    request = _request([str(args.request or "")], "amsyos reply")
    params: dict[str, Any] = {"request": request, "text": _text([str(w) for w in seq(args.text)])}
    if args.close:
        params["close"] = True
    told = await call(REPLY, params, out)
    state = clean(told.get("state"), 16)
    tail = "; closed" if state == "closed" else f"; close: amsyos ask close {request}"
    out.emit(told, f"{request}  reply {told.get('reply') or '?'} sent{tail}")
    return 0


# ---------------------------------------------------------------- amsyos contact
def _until(text: str | None) -> str | None:
    """`--until`: a date as it is (to its end, UTC), a time without a zone in this machine's."""
    if not text:
        return None
    try:
        at = datetime.fromisoformat(text.strip())
    except ValueError:
        raise CliError(
            "INVALID_INPUT",
            f"--until {text[:40]!r}: a date (2026-12-31) or a time (2026-12-31T18:00)",
        ) from None
    if len(text.strip()) <= 10:
        return text.strip()
    return (at if at.tzinfo else at.astimezone()).isoformat(timespec="seconds")


async def cmd_contact(args: argparse.Namespace, out: Out) -> int:
    verb = args.contact_cmd
    if verb == "ls":
        told = await call(CONTACTS, {}, out)
        lines: list[str] = []
        for row in (obj(r) for r in seq(told.get("contacts"))):
            until = f" until {clean(row.get('until'), 30)}" if row.get("until") else ""
            by = f" by {clean(row.get('by'), 64)}" if row.get("by") else ""
            waits = int(row.get("waiting") or 0)
            lines.append(
                f"{clean(row.get('from'), 64)}  {clean(row.get('state'), 10):<8}{until}{by}"
                + (f"; {waits} request{'s' if waits != 1 else ''} wait" if waits else "")
            )
        out.emit(told, *(lines or ["no organisation allowed, blocked or asking"]))
        return 0
    org = str(args.org or "")
    if not ORG.match(org):
        raise CliError("INVALID_INPUT", f"amsyos contact {verb} takes an organisation's id, t_…")
    if verb == "allow":
        params: dict[str, Any] = {"from": org}
        until = _until(args.until)
        if until:
            params["until"] = until
        told = await call(ALLOW, params, out)
        when = f" until {told.get('until')}" if told.get("until") else ""
        out.emit(
            told,
            f"{org} may send requests to this workspace{when}; {told.get('delivered') or 0} "
            "waiting delivered (amsyos contact block ends it)",
        )
        return 0
    if args.until:
        raise CliError("INVALID_INPUT", "--until goes with contact allow only")
    told = await call(BLOCK, {"from": org}, out)
    out.emit(
        told,
        f"{org} blocked: it sends no requests to this workspace; "
        f"{told.get('not_delivered') or 0} waiting not delivered",
    )
    return 0


# ---------------------------------------------------------------- the parsers
def add_parsers(add: Any, common: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """`amsyos ask …`, `amsyos reply …`, `amsyos contact …` (main.py's `add`)."""
    p = add(
        "ask", cmd_ask,
        "ask another workspace or organisation for something; ls, show, close your requests",
        "ask another workspace or organisation for something it can do and you cannot (a step "
        "in its own organisation, on its node): its AI clients are told in their around lines, "
        "its managers get a notice, and the answer comes back in yours. Another organisation "
        "takes requests once its managers allowed yours (amsyos contact allow); until then it "
        "waits. The text goes first, in quotes; - reads it from stdin",
        epilog="examples:\n  amsyos ask t_... \"Please define the service x on your node; spec in "
        "asset://...\"\n  amsyos ask w_..._abc123 - --for a_... < request.txt\n  amsyos ask ls\n"
        "  amsyos ask ls --out --all\n  amsyos ask show rq_...\n  amsyos reply rq_... \"done: "
        "x@1.0.0\" --close\n  amsyos ask close rq_...",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    p.add_argument(
        "first", metavar="t_…|w_…|ls|show|close",
        help="the target (an organisation's id, for its default workspace, or a workspace's), "
        "or what to do with your requests",
    )  # fmt: skip
    p.add_argument(
        "rest", nargs="*", metavar="TEXT|rq_…",
        help='the text, in quotes (at most 2000 characters; "-" reads stdin), or a request\'s id',
    )  # fmt: skip
    p.add_argument(
        "--for", dest="for_", metavar="PRINCIPAL",
        help="a new request: only this principal of the target (u_… or a_…) is told",
    )  # fmt: skip
    p.add_argument("--out", action="store_true", help="ask ls: the requests this workspace sent")
    p.add_argument("--all", action="store_true", help="ask ls: closed ones too (at most 50)")
    pr = add(
        "reply", cmd_reply,
        "answer a request, received or sent; the other side is told",
        "answer a request (amsyos ask ls lists them): the other side's agent that sent or replied "
        "last is told in its around lines, its person when nobody read it within 30 minutes. "
        "--close when it is done. The text goes first, in quotes; - reads it from stdin",
    )  # fmt: skip
    pr.add_argument("request", metavar="rq_…", help="the request's id")
    pr.add_argument(
        "text", nargs="+", metavar="TEXT", help="the reply, in quotes (2000 characters)"
    )
    pr.add_argument("--close", action="store_true", help="close the request with this reply")
    pc = add(
        "contact", cmd_contact,
        "which organisations this workspace takes requests from: allow, block, ls",
        "which organisations this workspace takes requests from (amsyos ask): a manager of the "
        "workspace, or an admin of the organisation, allows or blocks one; what waited for it is "
        "delivered, or not. Your own organisation's workspaces always take yours",
        epilog="examples:\n  amsyos contact allow t_...\n  amsyos contact allow t_... --until "
        "2026-12-31\n  amsyos contact block t_...\n  amsyos contact ls",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    verbs = pc.add_subparsers(dest="contact_cmd", metavar="allow|block|ls", required=True)
    pa = verbs.add_parser("allow", parents=[common], help="take its requests (a manager)")
    pa.add_argument("org", metavar="t_…", help="the organisation's id")
    pa.add_argument(
        "--until", metavar="WHEN", help="a date (to its end, UTC) or a time (this machine's)"
    )
    pb = verbs.add_parser(
        "block", parents=[common], help="take no more of its requests (a manager)"
    )
    pb.add_argument("org", metavar="t_…", help="the organisation's id")
    pb.set_defaults(until=None)
    pl = verbs.add_parser("ls", parents=[common], help="allowed, blocked, and who asks")
    pl.set_defaults(org=None, until=None)
    return {"ask": p, "reply": pr, "contact": pc, "contact allow": pa, "contact block": pb,
            "contact ls": pl}  # fmt: skip
