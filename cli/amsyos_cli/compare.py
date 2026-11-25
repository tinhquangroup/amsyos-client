"""`amsyos compare`: two versions of one service, or two services with the same inputs and
outputs, measured on the same inputs (`service.compare`), then the next step: keep the
efficient one (`amsyos promote`), take out the other (`amsyos uninstall … --version`).

- `amsyos compare <service> [--versions A,B | --other S] [--inputs JSON|@file]`: the runs
  are quoted first (refused above `--max-credit`), let in as ordinary jobs of yours, waited
  for (at most `--wait` seconds), and said as one short table: per input whether each side's
  output passed the service's checks, its time and credit, why it failed; totals; a verdict;
- `--dry-run`: only the runs and their quote;
- `amsyos compare --read cmp_…` reads one again; `--cancel cmp_…` cancels its runs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from amsyos_cli.api import Api
from amsyos_cli.common import CliError, Out, amount, credentials, obj, seq, short
from amsyos_cli.deploy import tenant_of

SERVICE = "service.compare"
LATER = "this Gateway has no service.compare yet: nothing was done"


async def _ask(api: Api, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return await api.instant(SERVICE, params)
    except CliError as exc:
        if exc.code == "SERVICE_NOT_FOUND" and SERVICE in exc.hint:
            raise CliError("NOT_ON_THIS_GATEWAY", LATER) from None
        raise


async def _own(api: Api, given: str) -> str:
    """A service of the caller's organisation by its full id: `image.thumb` is its own
    (`t_x.image.thumb`); a pinned version stays on it (`image.thumb@1.2.0`)."""
    name = given.strip()
    if not name:
        raise CliError("INVALID_INPUT", "name a service: t_x.name, or name (yours)")
    return name if name.startswith("t_") else f"{await tenant_of(api)}.{name}"


def _inputs(given: str) -> str:
    """`--inputs`: JSON text, or @file holding it; checked here so that a typo costs no call."""
    text = Path(given[1:]).read_text(encoding="utf-8") if given.startswith("@") else given
    try:
        json.loads(text)
    except ValueError as exc:
        raise CliError("INVALID_INPUT", f"--inputs: not JSON ({exc})") from None
    return text


def _cell(one: dict[str, Any]) -> str:
    if not one:
        return "-"
    state = "passed" if one.get("ok") else str(one.get("state") or "failed")
    bits = [state]
    if isinstance(one.get("s"), int | float):
        bits.append(f"{one['s']:g}s")
    if one.get("credit") is not None:
        bits.append(amount(one["credit"]))
    if one.get("cached"):
        bits.append("(cached)")
    return " ".join(bits)


def _total(one: dict[str, Any]) -> str:
    bits = [f"{one.get('passed', 0)}/{one.get('of', 0)} passed"]
    if isinstance(one.get("median_s"), int | float):
        bits.append(f"median {one['median_s']:g}s")
    bits.append(f"{amount(one.get('credit', 0))} credit")
    return ", ".join(bits)


def table_lines(data: dict[str, Any]) -> list[str]:
    """`service.compare`'s table as lines: a row per input, totals, the verdict, the next step."""
    a, b = str(data.get("a") or "?"), str(data.get("b") or "?")
    lines = [f"compare {data.get('compare', '?')}: a = {a}, b = {b} ({data.get('state', '?')})"]
    rows = [obj(r) for r in seq(data.get("rows"))]
    cells = [
        [str(r.get("input", "?")), _cell(obj(r.get("a"))), _cell(obj(r.get("b")))] for r in rows
    ]
    head = ["input", "a", "b"]
    widths = [max(len(c[i]) for c in [head, *cells]) for i in range(3)]
    lines.append("  " + "  ".join(h.ljust(w) for h, w in zip(head, widths, strict=True)))
    for row, shown in zip(rows, cells, strict=True):
        lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(shown, widths, strict=True)))
        for side in ("a", "b"):
            why = obj(row.get(side)).get("why")
            if why:
                lines.append(f"    {side}: {short(why, 240)}")
    totals = obj(data.get("totals"))
    for side in ("a", "b"):
        lines.append(f"  total {side}: {_total(obj(totals.get(side)))}")
    if data.get("spent") is not None:
        lines.append(f"  spent by this compare: {amount(data['spent'])} credit")
    if data.get("verdict"):
        lines.append(f"verdict: {data['verdict']}")
    if data.get("then"):
        lines.append(f"next: {data['then']}")
    return [line.rstrip() for line in lines]


def started_lines(data: dict[str, Any]) -> list[str]:
    lines = [
        f"{data.get('runs', '?')} runs ({data.get('inputs', '?')} inputs x a = {data.get('a')}, "
        f"b = {data.get('b')}), quoted {amount(data.get('budget'))} credit"
    ]
    if data.get("unquoted"):
        lines.append(f"  not quoted here: {', '.join(str(u) for u in seq(data['unquoted']))}")
    if data.get("compare"):
        lines.append(f"  compare {data['compare']}: {data.get('state', 'starting')}")
    else:
        lines.append(f"  {data.get('how') or 'nothing ran'}")
    return lines


async def _until_done(api: Api, cid: str, most_s: int, out: Out) -> dict[str, Any]:
    """The compare read until every run has ended or `most_s` has passed."""
    ends = time.monotonic() + most_s
    while True:
        data = await _ask(api, {"compare": cid})
        left = int(ends - time.monotonic())
        if data.get("state") == "done" or left <= 0:
            return data
        waiting = data.get("waiting_on")
        if not out.json:
            sys.stderr.write(f"  {data.get('left', '?')} runs left…\n")
        if isinstance(waiting, str) and waiting:
            with contextlib.suppress(CliError):  # read again: the table says what happened
                await api.status(waiting, wait_s=min(50, left))
        else:
            await asyncio.sleep(min(3, left))  # its runs are being let in


async def cmd_compare(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        if args.read or args.cancel:
            cid = args.read or args.cancel
            data = await _ask(api, {"compare": cid, **({"cancel": True} if args.cancel else {})})
            if args.read and args.wait and data.get("state") != "done":
                data = await _until_done(api, cid, args.wait, out)
            out.emit(data, *table_lines(data))
            return 0
        if not args.service:
            raise CliError("INVALID_INPUT", "name the service to compare, or --read cmp_…")
        asked: dict[str, Any] = {"service": await _own(api, args.service)}
        if args.versions:
            asked["versions"] = args.versions
        if args.other:
            asked["other"] = await _own(api, args.other)
        if args.inputs:
            asked["inputs"] = _inputs(args.inputs)
        if args.max_credit is not None:
            asked["max_credit"] = args.max_credit
        if args.external:
            asked["external"] = True
        if args.dry_run:
            asked["dry_run"] = True
        started = await _ask(api, asked)
        cid = started.get("compare")
        if args.dry_run or not isinstance(cid, str):
            out.emit(started, *started_lines(started))
            return 0
        if not out.json:
            sys.stderr.write("".join(f"{line}\n" for line in started_lines(started)))
        try:
            data = await _until_done(api, cid, args.wait, out)
        except KeyboardInterrupt:
            sys.stderr.write(f"stopped waiting; the runs go on. amsyos compare --cancel {cid}\n")
            return 130
    lines = table_lines(data)
    if data.get("state") != "done":
        lines.append(f"not done yet: amsyos compare --read {cid} --wait 600")
    out.emit(data, *lines)
    return 0
