"""`amsyos node rent|show|close|extend|clean|list`: the organisation's nodes (spec v3 §17),
its rented one (spec v2 §21) without `amsyos run node.*` (v3 T2: closing was `amsyos run
node.close`, and its answer an `output job: (url hidden)` line). Each answers in plain
lines; `--json` gives the whole answer. `amsyos space …` is its old name, kept hidden
(`open` is `rent`).

TODO(review): billing — `open` and `extend` buy hours of a machine on the organisation's
credit; the kernel holds and charges them (`node.rent`, `node.extend`), the CLI only asks.
"""

from __future__ import annotations

import argparse
from typing import Any

from amsyos_cli.api import Api, idempotency_key
from amsyos_cli.common import CliError, Out, amount, credentials, obj, seq, short


def _clock(stamp: Any) -> str:
    """`2026-11-20T08:01:35` → `08:01` (UTC, as AMSYOS says it)."""
    text = str(stamp or "")
    return text[11:16] if len(text) >= 16 else text


def show_line(told: dict[str, Any]) -> str:
    space = obj(told.get("space"))
    if not space:
        if told.get("enabled") is False:
            return "no rented node: rented nodes are not switched on for your organisation"
        last = obj(told.get("last"))
        was = (
            f" (the last one: {last.get('seconds')} s, charged {amount(last.get('charged'))}"
            + (f", {last['ended']}" if last.get("ended") else "")
            + ")"
            if last
            else ""
        )
        auto = obj(told.get("auto"))
        how = "your services' calls rent one by themselves" if auto.get("on") else (
            f"amsyos node rent --hours 1 rents one at {amount(told.get('price_per_hour'))} an hour"
            + (
                f" ({amount(told.get('cpu_price_per_hour'))} with --gpu none: no GPU)"
                if told.get("cpu_price_per_hour") is not None
                else ""
            )
        )  # fmt: skip
        return f"no rented node up{was}; {how}"
    bits = [f"rented node {space.get('state', '?')}: {space.get('job')}"]
    if space.get("note"):
        return f"{bits[0]} ({short(space['note'], 200)})"
    if space.get("class") == "CPU":
        bits.append("CPU class")
    elif space.get("gpu"):
        bits.append(str(obj(space["gpu"]).get("name") or space["gpu"]))
    if space.get("minutes_left") is not None:
        bits.append(f"{space['minutes_left']} min left (until {_clock(space.get('ends'))} UTC)")
    if space.get("charged_so_far") is not None:
        bits.append(f"charged {amount(space['charged_so_far'])}")
    if space.get("closes_idle_at"):
        bits.append(f"closes idle at {_clock(space['closes_idle_at'])}")
    elif space.get("closes_after_idle_minutes"):
        bits.append(f"closes after {space['closes_after_idle_minutes']} idle min")
    ready = [str(obj(s).get("service")) for s in seq(told.get("services")) if obj(s).get("ready")]
    if ready:
        bits.append("ready: " + " ".join(ready[:6]) + (" …" if len(ready) > 6 else ""))
    elif told.get("images"):
        bits.append(f"{len(seq(told['images']))} images")
    if space.get("cut_at_end"):
        bits.append(f"{len(seq(space['cut_at_end']))} jobs would be cut at its end")
    return ", ".join(bits)


def overview_line(told: dict[str, Any]) -> str:
    """The rented node in `whoami`'s overview: its line when one is up, else what rents one."""
    if obj(told.get("space")):
        return show_line(told)
    if told.get("enabled") is False:
        return "rented node: none (rented nodes are not switched on for your organisation)"
    if obj(told.get("auto")).get("on"):
        return "rented node: none up; your services' calls rent one by themselves"
    return "rented node: none: `amsyos node rent` when the work needs your own code"


def _close_line(told: dict[str, Any]) -> str:
    job = told.get("job")
    if told.get("state") == "closing":
        return f"rented node closing: {job}; amsyos node show says what it cost"
    bits = [f"rented node closed: {job}"]
    if told.get("seconds") is not None:
        bits.append(f"{told['seconds']} s")
    if told.get("charged") is not None:
        bits.append(f"charged {amount(told['charged'])}")
    if told.get("balance") is not None:
        bits.append(f"balance {amount(told['balance'])}")
    stopped = [
        str(obj(j).get("job") or obj(j).get("service")) for j in seq(told.get("jobs_stopped"))
    ]
    if stopped:
        bits.append(f"stopped {' '.join(stopped[:6])}")
    return ", ".join(bits)


def _clean_line(told: dict[str, Any]) -> str:
    on = obj(told.get("on_machine"))
    bits: list[str] = []
    if told.get("cleared"):
        bits.append("rented node cleared: as just rented, still up")
    elif told.get("removed") or told.get("models_removed"):
        gone = [*map(str, seq(told.get("removed"))), *map(str, seq(told.get("models_removed")))]
        bits.append(f"rented node cleaned: {', '.join(gone[:8])}")
    else:
        bits.append("on the rented node")
    if told.get("freed_gb") is not None:
        bits.append(f"{told['freed_gb']} GB freed")
    images = [
        f"{obj(i).get('image')} {obj(i).get('gb')} GB"
        # What deleting it alone frees: the rest is shared with other images or kept once.
        + (f" (frees ~{obj(i)['alone_gb']} GB alone)" if obj(i).get("alone_gb") is not None else "")
        + (" (unfinished build)" if obj(i).get("unfinished") else "")
        for i in seq(on.get("images"))
    ]
    models = [f"{obj(m).get('model')} {obj(m).get('gb')} GB" for m in seq(on.get("models"))]
    if not told.get("cleared"):
        for label, items in (("images", images), ("models", models)):
            more = f" (+{len(items) - 8} more: --json)" if len(items) > 8 else ""
            bits.append(f"{label}: {', '.join(items[:8]) or 'none'}{more}")
    disk = obj(on.get("disk"))
    if disk:
        bits.append(f"disk {disk.get('free_gb')} GB free of {disk.get('total_gb')}")
    if seq(told.get("left")):
        bits.append(f"left: {short('; '.join(map(str, seq(told.get('left')))), 160)}")
    return ", ".join(bits)


def own_line(told: dict[str, Any]) -> str:
    """An own node (`node.show` with its n_… id): a computer of the organisation's own."""
    machine = obj(told.get("machine"))
    bits = [
        f"own node {told.get('node')} ({told.get('name') or '?'}): {told.get('state', '?')}",
        f"level {told.get('access') or '?'}",
    ]
    if told.get("kind"):
        bits.append(str(told["kind"]))
    if machine.get("os"):
        bits.append(str(machine["os"]))
    gpus = [str(g) for g in seq(machine.get("gpus"))]
    if gpus:
        bits.append("GPU " + ", ".join(gpus[:4]))
    if told.get("running") is not None:
        bits.append(f"{told['running']} of {told.get('slots', '?')} slots busy")
    services = [str(s) for s in seq(told.get("services"))]
    if services:
        bits.append("services: " + " ".join(services[:6]) + (" …" if len(services) > 6 else ""))
    elif told.get("last_seen") and told.get("state") != "online":
        bits.append(f"last seen {told['last_seen']}")
    return ", ".join(bits)


def list_lines(told: dict[str, Any]) -> list[str]:
    """`node.list`: one line a node, its id, type and state; an own one's name and level."""
    nodes = [obj(n) for n in seq(told.get("nodes"))]
    if not nodes:
        return [
            "no nodes: `amsyos node rent` rents one; a computer of your own joins with "
            "`amsyos run node.join_code`"
        ]
    lines: list[str] = []
    for node in nodes:
        own = str(node.get("node", "")).startswith("n_")
        kind = str(node.get("type") or ("own" if own else "?"))
        bits = [str(node.get("node") or "?"), kind, str(node.get("state") or "?")]
        if kind == "own":
            bits += [str(node.get("name") or "?"), f"level {node.get('access') or '?'}"]
        elif node.get("state") == "closed":
            bits.append("(amsyos node rent rents one)")
        elif node.get("class") or node.get("machine"):
            bits.append(str(node.get("machine") or node.get("class")))
        lines.append("  ".join(bits))
    return lines


async def _clean(api: Api, args: argparse.Namespace, out: Out) -> int:
    params: dict[str, Any] = {}
    for key in ("images", "models"):
        if getattr(args, key):
            params[key] = getattr(args, key)
    for key in ("all", "even_if_not_kept"):
        if getattr(args, key):
            params[key] = True
    started = await api.run("node.clean", params, wait_s=50)
    job = str(started.get("job_id"))
    status = obj(started.get("status"))
    state = status.get("state")
    if state in ("failed", "cancelled"):
        failure = obj(status.get("failure"))
        raise CliError(str(failure.get("error") or "JOB_FAILED"), str(failure.get("hint") or state))
    if state != "succeeded":
        out.emit(
            started, f"rented node cleaning: {job} ({state or 'queued'}); next: amsyos wait {job}"
        )
        return 0
    result = obj(status.get("result")) or await api.result(job)
    told = dict(obj(result.get("data")))
    out.emit(told, _clean_line(told))
    return 0


async def cmd_node(args: argparse.Namespace, out: Out) -> int:
    """`amsyos node rent|show|close|extend|clean|list`, and `amsyos space …`, its old name
    (`open` is `rent`)."""
    what = getattr(args, "node_cmd", None) or getattr(args, "space_cmd", None)
    what = "rent" if what == "open" else what
    creds = credentials()
    async with Api(creds, out) as api:
        if what == "list":
            told = await api.instant("node.list")
            out.emit(told, *list_lines(told))
            return 0
        if what == "show":
            node = str(getattr(args, "node", None) or "").strip()
            told = await api.instant("node.show", {"node": node} if node else {})
            out.emit(told, own_line(told) if told.get("type") == "own" else show_line(told))
            return 0
        if what == "rent":
            params: dict[str, Any] = {"hours": args.hours}
            if args.idle_minutes:
                params["idle_minutes"] = args.idle_minutes
            if args.gpu:
                params["gpu"] = args.gpu
            key = idempotency_key("node.rent", params, creds)
            started = await api.run("node.rent", params, key=key)
            idle = f", closes after {args.idle_minutes} idle min" if args.idle_minutes else ""
            out.emit(
                started,
                f"rented node starting: {started.get('job_id')} for {args.hours:g} h{idle}, est. "
                f"cost {amount(started.get('est_cost'))}; amsyos node show says when it is up "
                "(about a minute), a build waits for it",
            )
            return 0
        if what == "clean":
            return await _clean(api, args, out)
        if what == "close":
            try:
                told = await api.instant(
                    "node.close", {"even_if_busy": True} if args.even_if_busy else {}
                )
            except CliError as exc:
                if exc.code == "SPACE_BUSY":
                    hint = exc.hint.replace(
                        "the same call with even_if_busy: true", "amsyos node close --even-if-busy"
                    )
                    raise CliError(exc.code, hint, exc.extra) from None
                raise
            out.emit(told, _close_line(told))
            return 0
        told = await api.instant("node.extend", {"hours": args.hours})
    out.emit(
        told,
        f"rented node extended by {told.get('added', args.hours)} h: {told.get('hours')} h in "
        f"all, {told.get('minutes_left')} min left (until {_clock(told.get('ends'))} UTC), held "
        f"{amount(told.get('held'))}",
    )
    return 0
