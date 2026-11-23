"""AMSYOS through its six MCP tools, and nothing else (spec v3 §3.2, principle 1).

MCP 2026-07-28 over Streamable HTTP, stateless: one `tools/call` POST a call, with the
session's bearer token, the protocol's headers and `_meta` as the MCP Python SDK's client
(`mcp==2.3.0`) sends them, and no handshake, no `tools/list`: the CLI talks to AMSYOS only,
and importing the SDK cost every command about 1.5 s (bench T4). Each tool answers one JSON
text block; an answer with `error` raises a `CliError` with the kernel's code and hint.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import httpx

from amsyos_cli import __version__, repo
from amsyos_cli.common import CliError, Credentials, Out, short

# The SDK logs what it falls back from; the reader wants answers, not that.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.ERROR)

PROTOCOL = "2026-07-28"
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAIT_MAX_S = 50  # `wait_s` of run and status: at most 50 s (the owner, 2026-11-17)


def done(status: dict[str, Any]) -> bool:
    """A job a client stops waiting for: it has ended, or it waits for a person or an agent
    to act (as the kernel's `_somebody_acts`: an order that runs to its end by itself is
    still work being done)."""
    state = status.get("state")
    if state in TERMINAL:
        return True
    if state != "waiting":
        return False
    waiting: Any = status.get("waiting_on")
    entries = [e for e in waiting if isinstance(e, dict)] if isinstance(waiting, list) else []  # pyright: ignore[reportUnknownVariableType]
    return any("order" not in e or "assignee" in e for e in entries)  # pyright: ignore[reportUnknownVariableType]


# When a waiter stops: `done` by default; `amsyos wait` goes on through a person's decision.
Stop = Callable[[dict[str, Any]], Awaitable[bool]]


async def until_done(status: dict[str, Any]) -> bool:
    return done(status)


class Api:
    """One connection for one command: `async with Api(creds) as api: await api.call(...)`."""

    def __init__(self, creds: Credentials, out: Out | None = None) -> None:
        self.creds = creds
        self.out = out or Out()
        self._stack: AsyncExitStack | None = None
        self._client: httpx.AsyncClient | None = None
        self._ids = 0
        self._status: int | None = None  # the last HTTP status a request was answered
        # Spec v3 §8.1: where the call comes from, read from git once a command; None
        # outside a repository. False once the Gateway said it takes no `source` yet.
        self._source: dict[str, Any] | bool | None = True
        self.missing: set[str] = set()  # services this Gateway does not have yet

    async def __aenter__(self) -> Api:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            http = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.creds.token}",
                    "User-Agent": f"amsyos-cli/{__version__}",
                },
                # A call may hold for `wait_s` (50 s) before it answers.
                timeout=httpx.Timeout(30.0, read=WAIT_MAX_S + 40.0),
                event_hooks={"response": [self._seen]},
            )
            client = await stack.enter_async_context(http)
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await stack.__aexit__(None, None, None)
            raise self._unreachable(exc) from None
        self._stack, self._client = stack, client
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            # Closing a finished connection: nothing in it is worth telling.
            with contextlib.suppress(Exception):
                await self._stack.__aexit__(None, None, None)
        self._stack = self._client = None

    async def _seen(self, response: httpx.Response) -> None:
        self._status = response.status_code

    def _unreachable(self, exc: BaseException) -> CliError:
        if isinstance(exc, CliError):
            return exc
        if isinstance(exc, KeyboardInterrupt):
            raise exc
        if self._status in {401, 403}:
            how = (
                "AMSYOS_TOKEN is refused: set a valid one, or unset it and amsyos login"
                if self.creds.from_env
                else "the token is refused (revoked or ended): amsyos login again"
            )
            return CliError("NOT_LOGGED_IN", how)
        reason = _first_cause(exc)
        return CliError(
            "UNREACHABLE",
            f"cannot reach AMSYOS at {self.creds.gateway} ({short(reason, 160)}); "
            "check the network or AMSYOS_GATEWAY, then try again",
        )

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One of the six tools; the answer's JSON, or a `CliError` with its code and hint."""
        if self._client is None:
            raise RuntimeError("Api is used outside `async with`")
        self._ids += 1
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientInfo": {"name": "amsyos-cli", "version": __version__},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        body = {
            "jsonrpc": "2.0", "id": self._ids, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_meta": meta},
        }  # fmt: skip
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/call", "Mcp-Name": tool,
        }  # fmt: skip
        try:
            response = await self._client.post(self.creds.mcp_url, json=body, headers=headers)
            if response.status_code in {401, 403}:
                raise RuntimeError(f"HTTP {response.status_code}")
            message = _message(response, self._ids)
        except CliError:
            raise
        except Exception as exc:
            raise self._unreachable(exc) from None
        if "error" in message:
            said: Any = message["error"]
            what = short(str(said.get("message") if isinstance(said, dict) else said), 300)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            raise CliError("PROTOCOL", f"AMSYOS refused the call ({what}); amsyos is up to date?")
        result: Any = message.get("result") or {}
        blocks: Any = result.get("content") if isinstance(result, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        text = "".join(
            str(b.get("text", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            for b in (blocks if isinstance(blocks, list) else [])  # pyright: ignore[reportUnknownVariableType]
            if isinstance(b, dict) and b.get("type") == "text"  # pyright: ignore[reportUnknownMemberType]
        )
        try:
            answer: Any = json.loads(text) if text else {}
        except ValueError:
            raise CliError("BAD_ANSWER", f"AMSYOS answered {tool} with no JSON") from None
        if not isinstance(answer, dict):
            raise CliError("BAD_ANSWER", f"AMSYOS answered {tool} with no JSON object")
        told: dict[str, Any] = answer  # pyright: ignore[reportUnknownVariableType]
        if "error" in told:
            extra = {k: v for k, v in told.items() if k not in {"error", "hint"}}
            raise CliError(str(told["error"]), str(told.get("hint") or ""), extra)
        return told

    # ------------------------------------------------------------ the six tools
    async def discover(self, query: str, **more: Any) -> dict[str, Any]:
        return await self.call("amsyos.discover", {"query": query, **more})

    async def describe(self, service_id: str) -> dict[str, Any]:
        return await self.call("amsyos.describe", {"service_id": service_id})

    def source(self) -> dict[str, Any] | None:
        """`source: {project, branch, commit, dirty}` of this command (spec v3 §8.1)."""
        if self._source is True:
            self._source = repo.source(self.creds.gateway)
        return self._source if isinstance(self._source, dict) else None

    async def run(
        self,
        service_id: str,
        params: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        wait_s: int = 0,
        workspace: str | None = None,
        limit_s: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"service_id": service_id, "params": params or {}}
        if limit_s:
            args["max_runtime_s"] = limit_s
        if key:
            args["idempotency_key"] = key
        if wait_s:
            args["wait_s"] = min(wait_s, WAIT_MAX_S)
        if workspace:
            args["workspace"] = workspace
        source = self.source()
        if source:
            args["source"] = source
        try:
            return await self.call("amsyos.run", args)
        except CliError as exc:
            if "source" not in args or not refused_field(exc, "source"):
                raise
        # A Gateway before stage B takes no `source`: the call goes without it (C5).
        self._source = False
        self.out.debug("this Gateway takes no `source` on amsyos.run yet: sent without it")
        del args["source"]
        return await self.call("amsyos.run", args)

    async def maybe(
        self, service_id: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """`instant`, or None when this Gateway does not have the service yet (a stage-B
        service on an older Gateway) or the session's grant leaves it out: the caller
        falls back and says so with --verbose."""
        if service_id in self.missing:
            return None
        try:
            return await self.instant(service_id, params)
        except CliError as exc:
            if exc.code not in {"SERVICE_NOT_FOUND", "FORBIDDEN"}:
                raise
            why = "has no" if exc.code == "SERVICE_NOT_FOUND" else "refuses this session"
            self.out.debug(f"this Gateway {why} {service_id} ({short(exc.hint, 160)})")
        self.missing.add(service_id)
        return None

    async def status(self, job_id: str, wait_s: int = 0) -> dict[str, Any]:
        args: dict[str, Any] = {"job_id": job_id}
        if wait_s:
            args["wait_s"] = min(wait_s, WAIT_MAX_S)
        return await self.call("amsyos.status", args)

    async def result(self, job_id: str) -> dict[str, Any]:
        return await self.call("amsyos.result", {"job_id": job_id})

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return await self.call("amsyos.cancel", {"job_id": job_id})

    async def instant(
        self, service_id: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """A service the kernel answers at once (workspace.list, asset.upload, service.check,
        session.show…): its `data` with its `outputs` beside them."""
        answer = await self.run(service_id, params)
        data: dict[str, Any] = dict(answer.get("data") or {})
        if answer.get("outputs"):
            data.setdefault("outputs", answer["outputs"])
        if "status" in answer or (not data and answer.get("job_id")):
            data.setdefault("job_id", answer.get("job_id"))
        return data


def _message(response: httpx.Response, wanted: int) -> dict[str, Any]:
    """The JSON-RPC answer to request `wanted`: the body (application/json), or the `data:`
    of the event that carries it (text/event-stream)."""
    if response.status_code >= 400 and not response.content:
        raise RuntimeError(f"HTTP {response.status_code}")
    kind = response.headers.get("content-type", "")
    if "text/event-stream" in kind:
        for event in response.text.split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
            )
            if not data:
                continue
            with contextlib.suppress(ValueError):
                said: Any = json.loads(data)
                if isinstance(said, dict) and said.get("id") == wanted:  # pyright: ignore[reportUnknownMemberType]
                    return said  # pyright: ignore[reportUnknownVariableType]
        raise RuntimeError("no answer in the event stream")
    try:
        told: Any = response.json()
    except ValueError:
        raise RuntimeError(f"HTTP {response.status_code}, not JSON") from None
    if not isinstance(told, dict):
        raise RuntimeError("an answer that is not a JSON object")
    return told  # pyright: ignore[reportUnknownVariableType]


def refused_field(exc: CliError, field: str) -> bool:
    """The Gateway does not take this field at all (yet): `source: Extra inputs are not
    permitted`, or a service's `unknown params [source]`. A problem inside a field it takes
    (`need: constraints.what: unknown`) is no such refusal: it is said as it came (v3 T5:
    `discover --need` called a need's own problems "no typed need on this Gateway")."""
    if exc.code not in {"INVALID_INPUT", "INVALID_PARAMS"}:
        return False
    name = re.escape(field)
    if re.search(rf"(?<![\w.]){name}: Extra inputs are not permitted", exc.hint):
        return True
    for listed in re.findall(r"unknown params? \[([^\]]*)\]", exc.hint):
        if field in {part.strip() for part in listed.split(",")}:
            return True
    return False


def _first_cause(exc: BaseException) -> str:
    """The innermost reason of an error the SDK wrapped (task groups wrap theirs)."""
    inner = exc
    for _ in range(10):
        wrapped: Any = getattr(inner, "exceptions", None)
        first: object = wrapped[0] if isinstance(wrapped, tuple | list) and wrapped else None  # pyright: ignore[reportUnknownVariableType]
        deeper = first if isinstance(first, BaseException) else inner.__cause__
        if deeper is None:
            break
        inner = deeper
    return f"{type(inner).__name__}: {inner}" if str(inner) else type(inner).__name__


# One invocation of the command line: a call it sends again after a lost answer keeps its key
# and runs once; the same command typed again later is a new call (v3 T2: `space.show` run
# twice answered the first, stale job).
_INVOCATION = time.time_ns()


def idempotency_key(service: str, params: Any, creds: Credentials, *, fresh: bool = False) -> str:
    """`amsyos.run`'s key from the command, its params, the session and this invocation
    (spec v3 §5, point 5): a call repeated after a lost answer runs once. `fresh` makes a new
    job on purpose."""
    holder = creds.session or "t:" + hashlib.sha256(creds.token.encode()).hexdigest()[:16]
    material = json.dumps(
        [service, params, holder, time.time_ns() if fresh else _INVOCATION],
        sort_keys=True, default=str,
    )  # fmt: skip
    return "cli:" + hashlib.sha256(material.encode()).hexdigest()[:40]
