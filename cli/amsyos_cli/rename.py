"""`amsyos rename <old> <new>`: move one of your organisation's services, every version of
it, to a new id (`service.rename`), and, run in the card's folder, write the new name into
amsyos.yaml's `service:`.

Either form works for both: the whole id (`t_x.media.thumb t_x.image.thumb`) or the card's
words (`media.thumb image.thumb`), which take your organisation. Nothing that named the old id
breaks: calls, describe, steps of recipes, missions, rules' plans and services of steps that
name it run the new one; another organisation's calls through your offer too; an own node
that has the old id on keeps taking jobs under it until the service is deployed there under
the new one (`amsyos deploy`) or its person switches the new one on. Renamed back, the alias
goes. On a Gateway without service.rename it says so and changes nothing.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from amsyos_cli import card as cards
from amsyos_cli.api import Api
from amsyos_cli.common import CliError, Out, credentials, obj, seq, short

RENAME = "service.rename"
TENANT = re.compile(r"^t_[a-z0-9_]+\.")


def _words(service_id: str) -> str:
    """An id as a card says it: without its organisation."""
    return TENANT.sub("", service_id.strip(), count=1)


def rewrite_card(root: Path, had: list[str], now: str) -> str | None:
    """amsyos.yaml's `service:` line set to `now` when it names one of the ids the service
    had (`had`, whole ids or words), its other lines and comments as they are. What it says:
    None when there is no card here."""
    path = root / cards.CARD
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    line = re.compile(r"(?m)^(service:[ \t]*)([\"']?)([A-Za-z0-9_.]+)\2([ \t]*(?:#.*)?)$")
    found = line.search(text)
    if found is None:
        return f"{cards.CARD} here names no service: left as it is"
    said = _words(found.group(3))
    if said == now:
        return f"{cards.CARD} says service: {now} already"
    if said not in {_words(h) for h in had}:
        return f"{cards.CARD} here is of '{said}', not this service: left as it is"
    new_text = (
        text[: found.start()] + f"{found.group(1)}{now}{found.group(4)}" + text[found.end() :]
    )
    path.write_text(new_text, encoding="utf-8")
    return f"{cards.CARD}: service: {now} (was {said})"


async def cmd_rename(args: argparse.Namespace, out: Out) -> int:
    asked = {"service": str(args.old).strip(), "to": str(args.new).strip()}
    async with Api(credentials(), out) as api:
        try:
            data = await api.instant(RENAME, asked)
        except CliError as exc:
            if exc.code == "SERVICE_NOT_FOUND" and RENAME in exc.hint:
                raise CliError(
                    "NOT_ON_THIS_GATEWAY", f"this Gateway has no {RENAME} yet: nothing was done"
                ) from None
            raise
    new = str(data.get("service") or asked["to"])
    old = str(data.get("renamed_from") or asked["service"])
    moved = obj(data.get("moved"))
    versions = ", ".join(str(v) for v in seq(moved.get("versions"))) or "-"
    channels = ", ".join(f"{c} {v}" for c, v in obj(moved.get("channels")).items())
    lines = [f"renamed: {old} -> {new} (versions {versions}{'; ' + channels if channels else ''})"]
    aliases = [str(a) for a in seq(data.get("aliases"))] or [old]
    lines.append(f"  still runs {new}: {short(', '.join(aliases), 300)} (calls, steps, plans)")
    for key in ("offers", "bindings", "orders", "workers", "handovers"):
        count = moved.get(key)
        n = len(seq(count)) if isinstance(count, list) else count
        if isinstance(n, int) and n:
            lines.append(f"  moved: {n} {key}")
    for node in seq(data.get("nodes")):
        one = obj(node)
        lines.append(
            f"  node {one.get('name')} ({one.get('access')}): {short(one.get('takes_jobs'), 120)}"
        )
        lines.append(f"    to move it there: {short(one.get('to_move'), 300)}")
    lines += [f"  note: {short(n, 300)}" for n in seq(data.get("notes"))]
    card = None if args.no_card else rewrite_card(Path(args.dir).resolve(), aliases, _words(new))
    if card is not None:
        data["card"] = card
        lines.append(card)
    else:
        lines.append(f"  next: {short(data.get('next'), 300)}")
    out.emit(data, *lines)
    return 0


def add_parser(add: Any) -> None:
    """`amsyos rename` in the parser (`main.parser`'s `add`)."""
    p = add(
        "rename", cmd_rename,
        "move a service of yours, every version, to a new id; the old id keeps running it",
        "Move one of your organisation's services, every version, its channels, checks, open "
        "offers and workers, to a new id (service.rename). Give whole ids (t_x.media.thumb "
        "t_x.image.thumb) or the card's words (media.thumb image.thumb). The old id stays an "
        "alias: calls, describe, steps of recipes, missions, rules and services of steps that "
        "name it run the new one, and an own node that has the old id keeps taking its jobs. "
        "Run in the card's folder, it writes the new name into amsyos.yaml's service:. "
        "Renaming back to an old id takes that alias away.",
    )  # fmt: skip
    p.add_argument("old", help="the service now: t_x.name or its words (name)")
    p.add_argument("new", help="its new id: t_x.area.action or area.action (area.group.action)")
    p.add_argument("--dir", default=".", help="the card's folder (default here)")
    p.add_argument("--no-card", action="store_true", help="leave amsyos.yaml as it is")
