"""Durable work and channels from the terminal (spec v3 §5, §7.3):

- `amsyos work show <job>`: a parent job's steps (recipe, composed service, mission), each
  with its service@version, state, attempts, cost and timing;
- `amsyos work resume <job>`: a new parent job that reuses the steps that succeeded on the
  same inputs (`work.resume`); `--param step.name=value` changes a step's param first,
  checked against its service before anything runs; `--wait` follows it;
- `amsyos work hand <job> --to <principal|session>`: its responsibility to another
  (`work.hand`);
- `amsyos work checkpoint`: the session's open jobs, noted (`work.checkpoint`); with
  `--quiet` (the PreCompact hook) a no-op that answers in the entry point, until stage C;
- `amsyos promote <service> <version> [--channel stable]` (`service.promote`).

On a Gateway without these services each says so and changes nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from amsyos_cli.api import Api, idempotency_key
from amsyos_cli.common import CliError, Out, credentials, obj, seq, short
from amsyos_cli.deploy import tenant_of
from amsyos_cli.jobs import finish, follow, status_lines, step_lines, steps_of, with_id

LATER = "this Gateway has no {service} yet (v3 stage B): nothing was done"
# a customer's feedback: a failure the caller's params or files caused; resumed unchanged, the
# work fails the same way (kernel/missions.py CALLER_FAULT).
CALLER_FAULT = frozenset({"INVALID_PARAMS", "INVALID_INPUT", "ASSET_NOT_FOUND", "MODEL_NOT_FOUND"})


async def _service(api: Api, service: str, params: dict[str, Any]) -> dict[str, Any]:
    """A work service's answer, or a plain refusal when the Gateway lacks it."""
    try:
        return await api.run(service, params)
    except CliError as exc:
        if exc.code == "SERVICE_NOT_FOUND" and service in exc.hint:
            raise CliError("NOT_ON_THIS_GATEWAY", LATER.format(service=service)) from None
        raise


async def cmd_work(args: argparse.Namespace, out: Out) -> int:
    what = args.what
    if what == "checkpoint" and args.quiet:
        return 0  # the PreCompact hook: quiet and free until stage C (entry.py answers first)
    if what != "checkpoint" and not args.job:
        raise CliError("INVALID_INPUT", f"amsyos work {what} <job>: name the parent job")
    creds = credentials()
    async with Api(creds, out) as api:
        if what == "show":
            return await _show(api, out, args.job)
        if what == "resume":
            return await _resume(api, out, args)
        if what == "hand":
            if not args.to:
                raise CliError(
                    "INVALID_INPUT", "amsyos work hand <job> --to <principal or session>"
                )
            told = await _service(api, "work.hand", {"job": args.job, "to": args.to})
            data = obj(told.get("data"))
            moved = seq(data.get("moved"))
            lines = [f"{args.job}: handed to {data.get('to') or args.to}"]
            if moved:
                lines.append(
                    f"  replanning tasks moved to their inbox: {', '.join(map(str, moved))}"
                )
            if data.get("note"):
                lines.append(f"  {short(data['note'], 300)}")
            out.emit(told, *lines)
            return 0
        told = await _service(api, "work.checkpoint", {})
    data = obj(told.get("data"))
    jobs = [obj(j) for j in seq(data.get("open") or data.get("jobs"))]
    lines = [f"checkpoint: {len(jobs)} open jobs of this session noted"]
    for job in jobs[:20]:
        bits = [str(job.get("job_id")), str(job.get("service") or ""), str(job.get("state") or "")]
        if job.get("progress") is not None:
            bits.append(f"{job['progress']}%")
        if job.get("work"):
            bits.append("(work show lists its steps)")
        lines.append("  " + "  ".join(b for b in bits if b))
    out.emit(told, *lines)
    return 0


async def _show(api: Api, out: Out, job_id: str) -> int:
    status = with_id(await api.status(job_id), job_id)
    lines = status_lines(status)
    steps = steps_of(status)
    if steps is None:
        lines.append("  no steps: not a recipe, composed service or mission")
    elif not step_lines(steps):
        lines.append("  no step has started yet")
    if status.get("state") in {"failed", "cancelled"} and steps:
        lines += _next(job_id, status, steps)
    out.emit(status, *lines)
    return 0


def _next(job_id: str, status: dict[str, Any], steps: Any) -> list[str]:
    """What to do with a parent job that failed or was cancelled: resume it; where it
    failed on its params (a customer's feedback), which param of which step to change (the
    Gateway's `change` on a step), and the resume that changes it."""
    changes = [
        (str(step.get("id")), str(step.get("service") or ""), obj(found))
        for step in (obj(s) for s in (seq(steps) if isinstance(steps, list) else []))
        for found in seq(step.get("change"))
    ]
    said = [c for c in changes if c[2].get("param")]
    if said:
        lines = [
            f"  change: step {sid} ({service}) {found['param']}"
            + (f"={found['value']}" if found.get("value") else "")
            + f": {short(found.get('problem'), 240)}"
            for sid, service, found in said[:8]
        ]
        sid, _, found = said[0]
        return [
            *lines,
            f"next: amsyos work resume {job_id} --param {sid}.{found['param']}=<value> (the "
            "steps that did not succeed run again with it; resumed unchanged it fails the "
            "same way)",
        ]
    if obj(status.get("failure")).get("error") in CALLER_FAULT:
        return [
            f"next: change what the error names: amsyos work resume {job_id} --param "
            "<step>.<param>=<value> (resumed unchanged it fails the same way), or run it "
            "again with other params"
        ]
    return [
        f"next: amsyos work resume {job_id} (reruns only what did not succeed; a mission: "
        "mission.revise)"
    ]


def _changes(given: list[str] | None) -> dict[str, dict[str, Any]]:
    """`--param step.name=value`, each, as work.resume's `params` (step id → {name:
    value}); a step id may hold dots (a sub-recipe's step): the name is after the last."""
    out: dict[str, dict[str, Any]] = {}
    bad: list[str] = []
    for item in given or []:
        name, eq, raw = item.partition("=")
        step, dot, param = name.strip().rpartition(".")
        if not eq or not dot or not step or not param:
            bad.append(item)
            continue
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        out.setdefault(step, {})[param] = value
    if bad:
        raise CliError(
            "INVALID_INPUT",
            f"--param takes <step>.<param>=<value>, e.g. voice.voice=Adam: {', '.join(bad[:5])}",
        )
    return out


async def _resume(api: Api, out: Out, args: argparse.Namespace) -> int:
    changes = _changes(getattr(args, "param", None))
    asked: dict[str, Any] = {"job": args.job}
    if changes:
        asked["params"] = json.dumps(changes, ensure_ascii=False)
    key = idempotency_key("work.resume", asked, api.creds, fresh=args.new)
    try:
        told = await api.run("work.resume", asked, key=key)
    except CliError as exc:
        if exc.code == "SERVICE_NOT_FOUND" and "work.resume" in exc.hint:
            raise CliError("NOT_ON_THIS_GATEWAY", LATER.format(service="work.resume")) from None
        raise
    data = obj(told.get("data"))
    new = data.get("job") or data.get("job_id") or data.get("resumed_as") or told.get("job_id")
    reused = seq(data.get("reused"))
    changed = ", ".join(f"{s}.{p}" for s, ps in changes.items() for p in ps)
    lines = [
        f"{args.job} resumed as {new}"
        + (f" with {changed} changed" if changed else "")
        + (f": {len(reused)} steps reused" if reused else "")
    ]
    if not args.wait:
        out.emit(told, *lines, f"next: amsyos wait {new}")
        return 0
    out.note(lines[0])
    status = await follow(api, out, str(new))
    code, _ = await finish(api, out, str(new), status, None if args.no_download else Path(args.out))
    return code


async def cmd_promote(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        service = args.service
        if not service.startswith("t_"):
            service = f"{await tenant_of(api)}.{service}"
        asked = {"service": service, "version": args.version, "channel": args.channel}
        told = await _service(api, "service.promote", asked)
    out.emit(told, f"{service}: channel {args.channel} → {args.version}")
    return 0
