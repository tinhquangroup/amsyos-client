"""Waiting on the Gateway's stream of events (spec v3 §5.1, §5.2): `GET /events?job=<id>`,
Server-Sent Events with the session's bearer token. A `job.state` event says when the job
moved; the CLI then asks `amsyos.status` once for the whole answer. Where the stream is
missing (a Gateway before stage B) or cut, `follow_events` answers None and the caller
falls back to `amsyos.status` with `wait_s`. Events carry ids and states only.

TODO(review): OAuth — the session's bearer token goes only to the Gateway it came from
(`creds.gateway`, the same check as the MCP calls), never into a URL or a log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from amsyos_cli import __version__
from amsyos_cli.api import TERMINAL, WAIT_MAX_S, Api, Stop, until_done
from amsyos_cli.common import CliError, obj

ENDED = TERMINAL | {"waiting"}  # states after which the whole status is asked for
SILENCE_S = 45.0  # the Gateway sends a heartbeat every 15 s: this long without one is a cut
LOOK_EVERY_S = 60.0  # no event this long: one `amsyos.status` for the phase and progress
RECONNECTS = 3
LASTED_S = 120.0  # a stream open this long ended normally: it does not count as a failure


@dataclass
class Event:
    id: str | None = None
    kind: str | None = None
    data: list[str] = field(default_factory=list[str])

    def payload(self) -> dict[str, Any]:
        try:
            told: Any = json.loads("\n".join(self.data)) if self.data else {}
        except ValueError:
            return {}
        return obj(told)


async def _events(response: httpx.Response) -> AsyncIterator[Event]:
    """The stream's events; comments (the heartbeat) are skipped."""
    event = Event()
    async for line in response.aiter_lines():
        if not line:
            if event.data or event.kind:
                yield event
            event = Event()
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "id":
            event.id = value
        elif name == "event":
            event.kind = value
        elif name == "data":
            event.data.append(value)


def state_of(event: Event, job_id: str) -> str | None:
    """The new state of this job, when the event is its `job.state`."""
    told = event.payload()
    kind = event.kind or told.get("kind")
    if kind != "job.state":
        return None
    inner = obj(told.get("data")) or told
    job = inner.get("job_id") or told.get("job_id")
    if job is not None and job != job_id:
        return None
    state = inner.get("state") or told.get("state")
    return state if isinstance(state, str) else None


async def follow_events(
    api: Api, job_id: str, show: Callable[[dict[str, Any]], None], stop: Stop = until_done
) -> dict[str, Any] | None:
    """The job's status once `stop` says so (by default: it has ended or waits for
    somebody), read from the stream; None when there is no stream to read (the caller
    polls instead)."""
    out = api.out
    url = api.creds.gateway + "/events"
    headers = {
        "Authorization": f"Bearer {api.creds.token}",
        "Accept": "text/event-stream",
        "User-Agent": f"amsyos-cli/{__version__}",
    }
    since: str | None = None
    timeout = httpx.Timeout(15.0, read=SILENCE_S)
    failed = 0  # streams that ended soon after they opened; one that lasted (the Gateway
    # ends each after an hour) is taken up again with `since`, as long as the job takes
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as http:
        while failed < RECONNECTS:
            query = {"job": job_id, "kinds": "job.state"}
            if since:
                query["since"] = since
            queue: asyncio.Queue[Event | BaseException | None] = asyncio.Queue()
            opened = time.monotonic()
            failed += 1
            try:
                async with http.stream("GET", url, params=query) as response:
                    kind = response.headers.get("content-type", "")
                    if response.status_code != 200 or not kind.startswith("text/event-stream"):
                        await response.aread()
                        out.debug(
                            f"no event stream here (GET /events: HTTP {response.status_code}): "
                            "waiting with amsyos.status"
                        )
                        return None
                    # It may have ended before the stream was open.
                    status = await api.status(job_id)
                    if await stop(status):
                        return status
                    show(status)
                    reader = asyncio.create_task(_read(response, queue))
                    try:
                        seen = {str(status.get("state"))}
                        ended = await _until_ended(api, job_id, queue, show, seen, stop=stop)
                    finally:
                        reader.cancel()
                        with contextlib.suppress(BaseException):
                            await reader
                    if isinstance(ended, dict):
                        return ended
                    since = ended or since
            except httpx.HTTPError as exc:
                out.debug(f"the event stream was cut ({type(exc).__name__}); reading on")
            if time.monotonic() - opened > LASTED_S:
                failed = 0
    out.debug("the event stream kept closing: waiting with amsyos.status")
    return None


async def _read(response: httpx.Response, queue: asyncio.Queue[Event | BaseException | None]):
    try:
        async for event in _events(response):
            await queue.put(event)
    except BaseException as exc:
        await queue.put(exc)
        return
    await queue.put(None)


async def _until_ended(
    api: Api,
    job_id: str,
    queue: asyncio.Queue[Event | BaseException | None],
    show: Callable[[dict[str, Any]], None],
    seen: set[str],
    *,
    stop: Stop = until_done,
) -> dict[str, Any] | str | None:
    """The final status, or the last event id when the stream ended first. A stream that
    follows one job replays its earlier events once: a state already seen asks nothing."""
    last: str | None = None
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), LOOK_EVERY_S)
        except TimeoutError:
            status = await api.status(job_id)  # quiet for a while: the phase and progress
            if await stop(status):
                return status
            show(status)
            continue
        if item is None:
            return last
        if isinstance(item, httpx.HTTPError):
            api.out.debug(f"the event stream was cut ({type(item).__name__})")
            return last
        if isinstance(item, CliError | asyncio.CancelledError):
            raise item
        if isinstance(item, BaseException):
            return last
        last = item.id or last
        state = state_of(item, job_id)
        if state is None:
            continue
        if state in ENDED:
            # `wait_s` gives a succeeded job's result with its status (no second call),
            # and waits through an order that runs to its end by itself.
            status = await api.status(job_id, wait_s=1 if state in TERMINAL else WAIT_MAX_S)
            if await stop(status):
                return status
            show(status)
            seen.add(str(status.get("state")))
            continue
        if state in seen:
            continue
        status = await api.status(job_id)
        show(status)
        seen.update({state, str(status.get("state"))})
