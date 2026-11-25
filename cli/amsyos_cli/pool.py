"""An organisation's pool of services from the terminal: try an approach freely, keep what
works, take out what does not as a whole.

- `amsyos pool [family]`: each own service, grouped by family, with where it runs, the
  version a bare id runs and its check, and over the last 30 days its runs, failure share,
  median time, credit spent, its last run and the disk it holds on own nodes
  (`service.pool`);
- `amsyos uninstall <service | family.*> [--version V] [--yes]`: without `--yes` a dry run
  (`service.remove` with `dry_run`) saying everything that would go and the disk it frees;
  with `--yes` the removal, undoable for 7 days (a version's is final). A refusal lists every
  user of it outside the removal, each with its fix;
- `amsyos restore <service | family.*>`: a removal of the last 7 days undone
  (`service.restore`); a service of an own node is then deployed again (`amsyos deploy`).

On a Gateway without these services each says so and changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from amsyos_cli import around
from amsyos_cli.api import Api, refused_field
from amsyos_cli.common import CliError, Out, amount, credentials, obj, seq, short
from amsyos_cli.deploy import tenant_of
from amsyos_cli.shell import cli_hint

LATER = "this Gateway has no {service} yet: nothing was done"
MOST_USERS = 40  # users of a refused removal printed (the JSON answer has every one)
MOST_ROWS = 200  # services printed by `amsyos pool`
# What to do about one user of a service a removal would leave behind, by its kind.
FIX = {
    "service": "remove it too (uninstall a family holding both) or change its steps",
    "rule": "amsyos run rule.remove rule={id}",
    "hook": "amsyos run hook.remove hook={id}",
    "mission": "let it end, or cancel it",
    "offer": "amsyos run offer.withdraw offer={id}",
    "binding": "another organisation's, through your offer: withdraw the offer, then wait",
    "job": "amsyos wait {id}, or amsyos cancel {id}",
}


async def _service(api: Api, service: str, params: dict[str, Any]) -> dict[str, Any]:
    """The answer of one of these services, or a plain refusal when the Gateway lacks it."""
    try:
        return await api.instant(service, params)
    except CliError as exc:
        if exc.code == "SERVICE_NOT_FOUND" and service in exc.hint:
            raise CliError("NOT_ON_THIS_GATEWAY", LATER.format(service=service)) from None
        raise


async def _own(api: Api, given: str) -> str:
    """A service or family of the caller's organisation, by its full id: `image.thumb` and
    `image.*` are its own (`t_x.image.thumb`, `t_x.image.*`)."""
    name = given.strip()
    if not name or name in {"*", ".*"}:
        raise CliError("INVALID_INPUT", "name a service (t_x.name, or name) or a family (name.*)")
    return name if name.startswith("t_") else f"{await tenant_of(api)}.{name}"


# ---------------------------------------------------------------- amsyos pool
def _gb(value: Any) -> str:
    return f"{value:g} GB" if isinstance(value, int | float) and value else "-"


def _row(one: dict[str, Any], family: str) -> list[str]:
    name = str(one.get("service") or "?")
    short_name = name.removeprefix(family.removesuffix("*")) or name
    where = str(one.get("kind") or "")
    if one.get("where"):
        where += f" {one['where']}"
    version = str(one.get("version") or "?")
    dev = obj(one.get("channels")).get("dev")
    if dev and dev != version:
        version += f" (dev {dev})"
    share = one.get("failed_share")
    median = one.get("median_s")
    return [
        short_name,
        short(where, 40),
        version,
        str(one.get("check") or "-"),
        str(one.get("runs") or 0),
        f"{share:.0%}" if isinstance(share, int | float) else "-",
        f"{median:g}s" if isinstance(median, int | float) else "-",
        amount(one["spent"]) if one.get("spent") else "-",
        str(one.get("last_run") or "-")[:10],
        _gb(one.get("disk_gb")),
    ]


def _totals(totals: dict[str, Any]) -> str:
    services, runs = totals.get("services", 0), totals.get("runs", 0)
    bits = [
        f"{services} service{'' if services == 1 else 's'}",
        f"{runs} run{'' if runs == 1 else 's'}",
    ]
    if totals.get("failed"):
        bits.append(f"{totals['failed']} failed")
    if totals.get("spent"):
        bits.append(f"{amount(totals['spent'])} credit")
    if totals.get("disk_gb"):
        bits.append(_gb(totals["disk_gb"]))
    return ", ".join(bits)


def pool_lines(data: dict[str, Any]) -> list[str]:
    """`service.pool`'s answer as a short table: a line a family, a line a service."""
    families = [obj(f) for f in seq(data.get("families"))]
    lines = [
        f"{data.get('organisation', '?')}'s services ({data.get('scope', '?')}, since "
        f"{data.get('since', '?')}): {_totals(obj(data.get('totals')))}"
    ]
    head = ["service", "runs on", "version", "check", "runs", "failed", "median", "spent",
            "last run", "disk"]  # fmt: skip
    table: list[str | list[str]] = []  # a family's line, or a service's cells
    printed = 0
    for family in families:
        name = str(family.get("family") or "?")
        table.append(f"{name}  {_totals(obj(family.get('totals')))}")
        for one in (obj(s) for s in seq(family.get("services"))):
            if printed < MOST_ROWS:
                table.append(_row(one, name))
            printed += 1
    rows = [item for item in table if isinstance(item, list)]
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]

    def cells(row: list[str]) -> str:
        return "  " + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True))

    if rows:
        lines.append(cells(head))
    lines += [item if isinstance(item, str) else cells(item) for item in table]
    if printed > MOST_ROWS:
        lines.append(f"  … {printed - MOST_ROWS} more: amsyos pool <family>, or --json")
    for node in (obj(n) for n in seq(data.get("unused_builds"))):
        lines.append(
            f"node {node.get('name') or node.get('node')}: {_gb(node.get('builds_gb'))} of builds "
            "no deployment uses"
        )
    if not families:
        lines.append(str(data.get("how") or "no service of yours yet"))
    elif rows:
        lines.append("next: amsyos uninstall <service | family.*> says what would go (a dry run)")
    else:
        lines.append("next: amsyos pool <family> lists its services")
    return [line.rstrip() for line in lines]


async def cmd_pool(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        asked: dict[str, Any] = {}
        if args.family:
            asked["family"] = await _own(api, args.family)
        data = await _service(api, "service.pool", asked)
    out.emit(data, *pool_lines(data))
    return 0


# ---------------------------------------------------------------- amsyos uninstall
def used_by(exc: CliError) -> list[dict[str, Any]]:
    """The users a refused removal names (its error's `used_by`, beside the hint or in its
    `data`)."""
    found = exc.extra.get("used_by")
    if found is None:
        found = obj(exc.extra.get("data")).get("used_by")
    return [obj(u) for u in seq(found)]


def refused_lines(exc: CliError) -> list[str]:
    users = used_by(exc)
    lines = [f"error: {exc.code}: {cli_hint(exc.hint)}"]
    if users:
        lines.append(f"  {len(users)} outside the removal use it:")
    for user in users[:MOST_USERS]:
        kind, ident = str(user.get("kind") or "?"), str(user.get("id") or "?")
        fix = FIX.get(kind, "").format(id=ident)
        lines.append(
            f"  {kind} {ident} uses {user.get('uses') or '?'}" + (f": {fix}" if fix else "")
        )
    if len(users) > MOST_USERS:
        lines.append(f"  … {len(users) - MOST_USERS} more (--json lists every one)")
    return lines


def removal_lines(data: dict[str, Any], service: str, *, done: bool) -> list[str]:
    """What `service.remove` answered (or, in a dry run, would do), and the next step."""
    removed = [str(s) for s in seq(data.get("removed"))]
    if not removed:  # a Gateway before the pool: the removed id among the outputs
        removed = [str(obj(data.get("outputs")).get("service") or service)]
    version = data.get("version")
    what = f"version {version} of {removed[0]}" if version else ", ".join(removed)
    lines = [f"{'removed' if done else 'would remove'}: {short(what, 600)}"]
    for node in (obj(n) for n in seq(data.get("nodes"))):
        freed = node.get("freed_gb")
        said = f"frees {freed:g} GB" if isinstance(freed, int | float) and freed else ""
        lines.append(
            f"  node {node.get('name') or node.get('node')}: {node.get('service')}"
            + (f" ({said})" if said else "")
        )
    images = [str(i) for i in seq(data.get("images"))]
    if images:
        lines.append(f"  kept images forgotten: {', '.join(images)}")
    skills = [str(s) for s in seq(data.get("skills"))]
    if skills:
        lines.append(f"  skills no longer offered: {', '.join(skills[:12])}")
    lines += [f"  note: {short(n, 300)}" for n in seq(data.get("notes"))]
    until = data.get("restore_until")
    if not done:
        undo = (
            "final: a version is not restored"
            if version
            else f"undo for 7 days: amsyos restore {service}"
        )
        again = f" --version {version}" if version else ""
        lines.append(f"next: amsyos uninstall {service}{again} --yes ({undo})")
    elif until:
        lines.append(f"undo until {until} with amsyos restore {service}")
    elif version:
        lines.append("final: a version removed is not restored")
    return lines


async def cmd_uninstall(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        service = await _own(api, args.service)
        await around.warn(api, out, [service])  # who else works on it: a note, never a refusal
        asked: dict[str, Any] = {"service": service}
        if args.version:
            asked["version"] = args.version
        if args.with_image:
            asked["with_image"] = True
        if not args.yes:
            asked["dry_run"] = True
        try:
            data = await api.instant("service.remove", asked)
        except CliError as exc:
            if not args.yes and refused_field(exc, "dry_run"):
                raise CliError(
                    "NOT_ON_THIS_GATEWAY",
                    "this Gateway has no dry run of service.remove yet: nothing was done. "
                    f"amsyos uninstall {service} --yes removes it, with no undo there",
                ) from None
            if out.json or not used_by(exc):
                raise
            sys.stderr.write("".join(f"{line}\n" for line in refused_lines(exc)))
            return 1
    out.emit(data, *removal_lines(data, service, done=bool(args.yes)))
    return 0


# ---------------------------------------------------------------- amsyos restore
async def cmd_restore(args: argparse.Namespace, out: Out) -> int:
    async with Api(credentials(), out) as api:
        service = await _own(api, args.service)
        data = await _service(api, "service.restore", {"service": service})
    restored = [str(s) for s in seq(data.get("restored"))] or [service]
    lines = [f"restored: {short(', '.join(restored), 600)}"]
    lines += [f"  note: {short(n, 300)}" for n in seq(data.get("notes"))]
    out.emit(data, *lines)
    return 0
