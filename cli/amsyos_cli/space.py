"""`amsyos space open|show|close|extend`: the organisation's own rented machine (spec v2
§21) without `amsyos run space.*` (v3 T2: closing was `amsyos run space.close`, and its
answer an `output job: (url hidden)` line). Each answers in one plain line; `--json` gives
the whole answer.

TODO(review): billing — `open` and `extend` buy hours of a machine on the organisation's
credit; the kernel holds and charges them (`space.open`, `space.extend`), the CLI only asks.
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
            return "no space: spaces are not switched on for your organisation"
        last = obj(told.get("last"))
        was = (
            f" (the last one: {last.get('seconds')} s, charged {amount(last.get('charged'))}"
            + (f", {last['ended']}" if last.get("ended") else "")
            + ")"
            if last
            else ""
        )
        auto = obj(told.get("auto"))
        how = "your services' calls open it by itself" if auto.get("on") else (
            f"amsyos space open --hours 1 opens one at {amount(told.get('price_per_hour'))} an hour"
            + (
                f" ({amount(told.get('cpu_price_per_hour'))} with --gpu none: no GPU)"
                if told.get("cpu_price_per_hour") is not None
                else ""
            )
        )  # fmt: skip
        return f"no space open{was}; {how}"
    bits = [f"space {space.get('state', '?')}: {space.get('job')}"]
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
    """The space in `whoami`'s overview: its line when one is there, else what opens one."""
    if obj(told.get("space")):
        return show_line(told)
    if told.get("enabled") is False:
        return "space: none (spaces are not switched on for your organisation)"
    if obj(told.get("auto")).get("on"):
        return "space: none open; your services' calls open it by themselves"
    return "space: none: `amsyos space open` when the work needs your own code"


def _close_line(told: dict[str, Any]) -> str:
    job = told.get("job")
    if told.get("state") == "closing":
        return f"space closing: {job}; amsyos space show says what it cost"
    bits = [f"space closed: {job}"]
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


async def cmd_space(args: argparse.Namespace, out: Out) -> int:
    what = args.space_cmd
    creds = credentials()
    async with Api(creds, out) as api:
        if what == "show":
            told = await api.instant("space.show")
            out.emit(told, show_line(told))
            return 0
        if what == "open":
            params: dict[str, Any] = {"hours": args.hours}
            if args.idle_minutes:
                params["idle_minutes"] = args.idle_minutes
            if args.gpu:
                params["gpu"] = args.gpu
            key = idempotency_key("space.open", params, creds)
            started = await api.run("space.open", params, key=key)
            idle = f", closes after {args.idle_minutes} idle min" if args.idle_minutes else ""
            out.emit(
                started,
                f"space opening: {started.get('job_id')} for {args.hours:g} h{idle}, est. cost "
                f"{amount(started.get('est_cost'))}; amsyos space show says when it is up (about a "
                "minute), a build waits for it",
            )
            return 0
        if what == "close":
            try:
                told = await api.instant(
                    "space.close", {"even_if_busy": True} if args.even_if_busy else {}
                )
            except CliError as exc:
                if exc.code == "SPACE_BUSY":
                    hint = exc.hint.replace(
                        "the same call with even_if_busy: true", "amsyos space close --even-if-busy"
                    )
                    raise CliError(exc.code, hint, exc.extra) from None
                raise
            out.emit(told, _close_line(told))
            return 0
        told = await api.instant("space.extend", {"hours": args.hours})
    out.emit(
        told,
        f"space extended by {told.get('added', args.hours)} h: {told.get('hours')} h in all, "
        f"{told.get('minutes_left')} min left (until {_clock(told.get('ends'))} UTC), held "
        f"{amount(told.get('held'))}",
    )
    return 0
