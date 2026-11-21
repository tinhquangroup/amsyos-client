"""`amsyos watch` (spec v3 §5.1, §10, §11): wait on the Gateway's event stream, not by asking
again and again (bench T4: two agents polled `note.list` and `lease.list` every 30 s).

- `--note [TOPIC]`: the next note under TOPIC or a topic below it (`film` sees
  `film/part-3`); with `--after n_…`, a note already posted after that one answers at once.
- `--free WHAT`: until nobody holds the lease WHAT (at once when nobody does).
- `--handover ho_…`: until the hand-over is `done`, `failed` or `rejected` (its caller events
  `work.handover` carry its state; `work.handover {handover}` gives the whole record).
- `--job j_…`: until the job ends or waits for somebody to act (as `amsyos wait` follows it).
- `--kinds a,b`: any event of these kinds (job.state, task.waiting, note.posted,
  lease.changed, work.hand, work.handover…), as one JSON line each.

Nothing that comes while the CLI reads is lost (bench T5: about 3 minutes were): the stream is
opened first and what is already there is read after it (a newer note, a free lease, a
hand-over's state), events wait in a queue while an answer is fetched, a stream that is cut is
taken up again from its last event (`since`), and what came before any event is read again.
`--since <id>` takes up from an earlier watch's last event; each answer says its own.

It ends after `--count` matches (default 1), or after `--timeout` seconds with exit code 3.
Events carry ids and states only; a note's text is read with `note.list`.

TODO(review): OAuth — the session's bearer token goes only to the Gateway it came from
(`creds.gateway`), never into a URL or a log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from amsyos_cli import __version__
from amsyos_cli.api import Api
from amsyos_cli.common import CliError, Out, credentials, obj, seq, short
from amsyos_cli.events import Event, _events  # pyright: ignore[reportPrivateUsage]
from amsyos_cli.jobs import follow, space_closed, status_lines, with_id

TIMED_OUT = 3
LOOK_EVERY_S = 60.0  # a lease may end by its time, with no event: look again this often
SILENCE_S = 45.0
POLL_S = 5.0  # a hand-over on a Gateway with no event stream
HANDOVER_ENDS = frozenset({"done", "failed", "rejected"})


def _under(topic: str, wanted: str) -> bool:
    wanted = wanted.rstrip("/")
    return not wanted or topic == wanted or topic.startswith(wanted + "/")


def _note_line(note: dict[str, Any]) -> str:
    files = seq(note.get("files"))
    return (
        f"note {note.get('note')} [{note.get('topic')}, {note.get('kind')}] {note.get('at')} "
        f"by {note.get('session') or note.get('by')}: {short(str(note.get('text') or ''), 600)}"
        + (f"  files: {' '.join(str(f) for f in files)}" if files else "")
    )


async def _notes(api: Api, topic: str, after: str | None) -> list[dict[str, Any]]:
    asked: dict[str, Any] = {"limit": 20}
    if topic:
        asked["topic"] = topic
    if after:
        asked["since"] = after
    told = await api.instant("note.list", asked)
    return [obj(n) for n in seq(told.get("notes"))]


async def _holder(api: Api, what: str) -> dict[str, Any] | None:
    told = await api.instant("lease.list", {})
    return next((obj(x) for x in seq(told.get("leases")) if obj(x).get("what") == what), None)


async def _handover(api: Api, handover: str) -> dict[str, Any]:
    told = await api.instant("work.handover", {"handover": handover})
    return {k: v for k, v in told.items() if k not in {"job_id", "outputs"}} or told


def _clock(stamp: Any) -> str:
    text = str(stamp or "")
    return text[11:19] if len(text) >= 19 else text


def handover_lines(told: dict[str, Any]) -> list[str]:
    """A hand-over's record in a few lines: its state and what it made, each step with its
    time, why it failed, what differs from the contract, what is next."""
    head = f"{told.get('handover')}: {told.get('state')}"
    if told.get("made"):
        head += f", made {told['made']}"
    jobs = [f"{k} {told[k]}" for k in ("work_job", "build_job") if told.get(k)]
    if jobs:
        head += f" ({', '.join(jobs)})"
    lines = [head]
    steps = [obj(s) for s in seq(told.get("steps"))]
    if steps:
        lines.append(
            "  steps: "
            + ", ".join(f"{s.get('state')} {_clock(s.get('at'))}".strip() for s in steps[-10:])
        )
    for step in steps:
        if step.get("differs"):
            lines.append(f"  differs from the contract: {short(step['differs'], 400)}")
        if step.get("check") or step.get("check_job"):
            lines.append(f"  check: {short(step.get('check') or step.get('check_job'), 200)}")
    if told.get("why"):
        lines.append(f"  why: {short(told['why'], 600)}")
    if told.get("next"):
        lines.append(f"  next: {short(told['next'], 400)}")
    return lines


@dataclass
class _Watch:
    """What one `amsyos watch` waits for, and what it found."""

    topic: str | None
    free: str | None
    handover: str | None
    kinds: set[str]
    wanted: int
    after: str | None  # notes after this one answer (the newest seen)
    found: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    seen_notes: set[str] = field(default_factory=set[str])
    state: str | None = None  # the hand-over's, as last said
    record: dict[str, Any] | None = None  # the hand-over's whole record, at its end

    @property
    def finished(self) -> bool:
        if self.free is not None or self.handover is not None:
            return bool(self.found)
        return len(self.found) >= self.wanted


async def _catch_up(api: Api, out: Out, w: _Watch, first: bool) -> None:
    """What is there already, read once the stream is open (so nothing after is missed):
    notes after the newest seen, whether the lease is free, the hand-over's state."""
    if w.topic is not None:
        if first and w.after is None:  # "the next note": the newest now is where it starts
            newest = await _notes(api, w.topic, None)
            w.seen_notes.update(str(n.get("note")) for n in newest)
            w.after = str(newest[-1].get("note")) if newest else None
        else:
            for note in await _notes(api, w.topic, w.after):
                _take_note(out, w, note)
                if w.finished:
                    return
    if w.free is not None and await _holder(api, w.free) is None:
        w.found.append({"free": w.free})
        out.say(f"lease {w.free}: free")
    if w.handover is not None:
        await _look_handover(api, out, w, None)


def _take_note(out: Out, w: _Watch, note: dict[str, Any]) -> None:
    note_id = str(note.get("note") or "")
    if note_id and note_id in w.seen_notes:
        return
    w.seen_notes.add(note_id)
    w.after = note_id or w.after
    w.found.append(note)
    out.say(_note_line(note))


async def _look_handover(api: Api, out: Out, w: _Watch, said: str | None) -> None:
    """The hand-over's state (from an event, or asked); its whole record at its end."""
    handover = str(w.handover)
    state = said
    told: dict[str, Any] | None = None
    if state is None or state in HANDOVER_ENDS:
        told = await _handover(api, handover)
        state = str(told.get("state") or state)
    if state in HANDOVER_ENDS:
        w.record = told or await _handover(api, handover)
        w.found.append(w.record)
        return
    if state != w.state:
        w.state = state
        out.note(f"{handover}: {state}")


async def cmd_watch(args: argparse.Namespace, out: Out) -> int:
    topic: str | None = args.note
    free: str | None = args.free
    handover: str | None = args.handover
    kinds = {k.strip() for k in (args.kinds or "").split(",") if k.strip()}
    if args.job:
        return await _watch_job(args, out)
    if topic is None and free is None and handover is None and not kinds:
        raise CliError(
            "INVALID_INPUT",
            "amsyos watch --note [TOPIC] [--after n_…] | --free WHAT | --handover ho_… | --job "
            "j_… | --kinds a,b: what to wait for",
        )
    if handover is not None and not handover.startswith("ho_"):
        raise CliError("INVALID_INPUT", f"--handover takes a hand-over's id (ho_…), not {handover}")
    if topic is not None:
        kinds.add("note.posted")
    if free is not None:
        kinds.add("lease.changed")
    if handover is not None:
        kinds.add("work.handover")
    w = _Watch(topic, free, handover, kinds, max(1, int(args.count)), args.after)
    deadline = time.monotonic() + max(1.0, float(args.timeout))
    creds = credentials()
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Accept": "text/event-stream",
        "User-Agent": f"amsyos-cli/{__version__}",
    }
    url = creds.gateway + "/events"
    since: str | None = args.since
    if since is not None and not since.strip().isdigit():
        raise CliError(
            "INVALID_INPUT", "--since takes an event's id (a whole number), as last_event says"
        )
    first = True
    async with (
        Api(creds, out) as api,
        httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(15.0, read=SILENCE_S)) as http,
    ):
        while time.monotonic() < deadline and not w.finished:
            query = {"kinds": ",".join(sorted(kinds))}
            if since:
                query["since"] = since
            opened = time.monotonic()
            try:
                async with http.stream("GET", url, params=query) as response:
                    if response.status_code != 200:
                        await response.aread()
                        if response.status_code == 404 and handover and topic is None and not free:
                            await _poll_handover(api, out, w, deadline)
                            break
                        raise _refused(response)
                    # The stream is open: what is already there answers now, and nothing
                    # posted from here on is missed.
                    await _catch_up(api, out, w, first)
                    first = False
                    if w.finished:
                        break
                    since = await _read(api, response, w, deadline, out) or since
            except httpx.HTTPError as exc:
                out.debug(f"the event stream was cut ({type(exc).__name__}); reading on")
            if time.monotonic() - opened < 2.0 and not w.finished:
                await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    return _answer(args, out, w, since)


def _refused(response: httpx.Response) -> CliError:
    """The stream's refusal in the kernel's shape: its own `{error, hint}`, else no stream."""
    with contextlib.suppress(ValueError):
        said = obj(response.json())
        if said.get("error"):
            return CliError(str(said["error"]), str(said.get("hint") or ""))
    return CliError(
        "NO_EVENTS",
        f"this AMSYOS gives no event stream (GET /events: HTTP {response.status_code}); ask "
        "with note.list, lease.list or work.handover instead",
    )


def _answer(args: argparse.Namespace, out: Out, w: _Watch, since: str | None) -> int:
    if not w.found:
        tail = f"; --since {since} takes up from here" if since else ""
        state = f" (the hand-over is {w.state})" if w.state else ""
        out.emit(
            {"found": [], "timed_out": True, "last_event": since},
            f"nothing within {int(args.timeout)} s{state} (amsyos watch --timeout to wait longer"
            f"{tail})",
        )
        return TIMED_OUT
    if w.record is not None:
        out.emit({"found": w.found, "handover": w.record}, *handover_lines(w.record))
        return 0 if w.record.get("state") == "done" else 1
    if out.json:
        out.emit({"found": w.found, "last_event": since})
    elif since and args.kinds:
        out.say(f"last event {since}: amsyos watch … --since {since} takes up from here")
    return 0


async def _watch_job(args: argparse.Namespace, out: Out) -> int:
    """`--job`: as `amsyos wait` follows it (the stream, else `amsyos.status` with `wait_s`),
    until it ends or waits for somebody, within `--timeout`."""
    job = str(args.job)
    async with Api(credentials(), out) as api:
        try:
            status = await asyncio.wait_for(follow(api, out, job), max(1.0, float(args.timeout)))
        except TimeoutError:
            now = with_id(await api.status(job), job)
            out.emit(
                {"found": [], "timed_out": True, "status": now},
                *status_lines(now),
                f"{job} has not ended within {int(args.timeout)} s (amsyos watch --timeout to "
                "wait longer)",
            )
            return TIMED_OUT
    status = with_id(status, job)
    out.emit({"found": [status], "status": status}, *status_lines(status))
    ok = status.get("state") in {"succeeded", "waiting"} or space_closed(status)
    return 0 if ok else 1


async def _poll_handover(api: Api, out: Out, w: _Watch, deadline: float) -> None:
    """A Gateway with no event stream: the hand-over asked every few seconds."""
    out.debug("no event stream here: asking work.handover every few seconds")
    while time.monotonic() < deadline:
        await _look_handover(api, out, w, None)
        if w.finished:
            return
        await asyncio.sleep(min(POLL_S, max(0.0, deadline - time.monotonic())))


async def _read(
    api: Api, response: httpx.Response, w: _Watch, deadline: float, out: Out
) -> str | None:
    """Events until enough matched, the deadline, or the stream's end: the last event id.
    Events that come while an answer is fetched wait in the queue."""
    queue: asyncio.Queue[Event | BaseException | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in _events(response):
                await queue.put(event)
        except BaseException as exc:
            await queue.put(exc)
            return
        await queue.put(None)

    reader = asyncio.create_task(pump())
    last: str | None = None
    try:
        while not w.finished:
            left = deadline - time.monotonic()
            if left <= 0:
                return last
            try:
                item = await asyncio.wait_for(queue.get(), min(left, LOOK_EVERY_S))
            except TimeoutError:
                if w.free is not None and await _holder(api, w.free) is None:
                    w.found.append({"free": w.free})
                    out.say(f"lease {w.free}: free (its time ended)")
                    return last
                if w.handover is not None:  # quiet for a while: asked once
                    await _look_handover(api, out, w, None)
                continue
            if item is None or isinstance(item, BaseException):
                return last
            last = item.id or last
            await _event(api, out, w, item)
    finally:
        reader.cancel()
        with contextlib.suppress(BaseException):
            await reader
    return last


async def _event(api: Api, out: Out, w: _Watch, item: Event) -> None:
    told = item.payload()
    kind = item.kind or str(told.get("kind") or "")
    data = obj(told.get("data")) or told
    if kind == "note.posted" and w.topic is not None:  # a note under the topic
        if not _under(str(data.get("topic") or ""), w.topic):
            return
        if str(data.get("note_id") or "") in w.seen_notes:
            return
        notes = await _notes(api, str(data.get("topic") or ""), None)
        note = next((n for n in notes if n.get("note") == data.get("note_id")), None)
        _take_note(out, w, note or {"note": data.get("note_id"), "topic": data.get("topic")})
    elif kind == "lease.changed" and w.free is not None:
        if data.get("what") == w.free and not data.get("holder"):
            w.found.append({"free": w.free})
            out.say(f"lease {w.free}: free")
    elif kind == "work.handover" and w.handover is not None:
        if data.get("handover") == w.handover:
            await _look_handover(api, out, w, str(data.get("state") or "") or None)
    elif kind in w.kinds:
        w.found.append(told)
        out.say(json.dumps(told, ensure_ascii=False, separators=(",", ":"))[:600])
