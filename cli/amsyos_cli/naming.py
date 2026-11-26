"""The naming standard of an organisation's services (sdk/CONTRACT.md, "Naming a service"),
said as warnings: never a refusal.

A service id is `t_<org>.<area>.<action>`, at most one group between
(`t_<org>.<area>.<group>.<action>`), so a card's `service` is `area.action` or
`area.group.action`: the area is the tool or the domain a client looks under, never the
organisation's own name; the group a product line inside the area; the action what it does.
Versions, models, dates and rounds are not in a name: `version` and the channels (dev, stable)
carry those.

The kernel holds the same checks (`service.check`, `service.define`); the two say the same of
an id. AMSYOS's answer, which knows the organisation, is the one printed when there is one;
these are said only without it (offline, not logged in, a dry run), and check that the first
word is the organisation's name only when the organisation is known here.

Checks, on the card's words (the id after `t_<org>.`):
1. `org_repeat`: the first word is the organisation's name (the tenant id without `t_`, or
   the tenant id itself), `org` or `tenant`;
2. `repeat`: a word is the word before it;
3. `too_deep`: more than 3 words;
4. `versioned`: a word that is `v<digits>`, ends `_v<digits>`, ends in two or more digits
   that begin with 0, holds a year, or is a model with digits (`qwen4b`, `sdxl`);
5. `scratch`: a word that is test, tmp, demo, … (experiments go on the dev channel).
A suggested name applies 1, 4 and 2 together, in that order; none when it would leave fewer
than 2 words or is the name itself.
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any, Final

# A card's `service`: 2 to 4 snake_case words with dots (the standard says 2 or 3; 4 warned).
NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,47}(\.[a-z][a-z0-9_]{0,47}){1,3}")
ORG_WORDS: Final = frozenset({"org", "tenant"})
SCRATCH_WORDS: Final = frozenset(
    {"test", "tmp", "temp", "demo", "stub", "scratch", "wip", "old", "new", "final", "copy"}
)
MOST_WORDS: Final = 3  # area.group.action
FEWEST_WORDS: Final = 2  # area.action: a suggestion leaves no fewer

_V_WORD: Final = re.compile(r"v\d+")
_V_END: Final = re.compile(r"_v\d+$")
_ZERO_END: Final = re.compile(r"(?<!\d)0\d+$")  # review02: a round, not mp4 or h264
_YEAR: Final = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# A model with digits, at the start of a word or after `_`: qwen4b, llama3, gpt4o, sdxl.
_MODEL: Final = re.compile(
    r"(?:^|(?<=_))(?:(?:qwen|llama|gpt|claude|gemini|mistral|phi|flux)\d|sdxl)[a-z0-9]*"
)
_WORD: Final = re.compile(r"[a-z][a-z0-9_]{0,47}")
CARD: Final = "amsyos.yaml"


def _unversioned(word: str) -> str:
    """The word without its version, model, date or round ('' when nothing is left; the
    word itself when it has none)."""
    if _V_WORD.fullmatch(word):
        return ""
    left = _V_END.sub("", word)
    left = _ZERO_END.sub("", left)
    left = _YEAR.sub("", left)
    left = _MODEL.sub("", left)
    if left == word:
        return word
    parts = [p for p in left.split("_") if p]
    while parts and parts[0][0].isdigit():  # a word begins with a letter
        parts.pop(0)
    left = "_".join(parts)
    return left if _WORD.fullmatch(left) else ""


def _quoted(words: list[str]) -> str:
    return ", ".join(f"'{w}'" for w in dict.fromkeys(words))


def name_warnings(name: str, tenant: str | None = None) -> tuple[list[str], str | None]:
    """What the naming standard says of a card's `service` (the words after `t_<org>.`): a
    warning a problem (each once, with its code, in the kernel's words), and the name
    suggested in its place when one can be made, in the card's form. With `tenant` (the
    caller's organisation, when known here) the first word is checked against its name and
    the closing line names whole ids."""
    words = name.split("@", 1)[0].split(".")
    if not name or not all(words):
        return [], None
    org = tenant.removeprefix("t_") if tenant else None
    whole = f"{tenant}." if tenant else "t_<organisation>."
    lines: list[str] = []
    first = words[0]
    own = tenant is not None and first in (org, tenant)
    org_repeat = own or first in ORG_WORDS
    if org_repeat:
        what = "your organisation's name" if own else "no area"
        lines.append(
            f"name (org_repeat): '{first}' is {what}: the first word is the area a client "
            "looks under, the tool or the domain (blender, video, invoice)"
        )
    repeated = [w for before, w in pairwise(words) if w == before]
    if repeated:
        lines.append(f"name (repeat): {_quoted(repeated)} repeats the word before it")
    if len(words) > MOST_WORDS:
        lines.append(
            f"name (too_deep): {len(words)} words after {whole}: at most area.group.action, "
            "the group only for a product line inside the area"
        )
    versioned = [w for w in words if _unversioned(w) != w]
    if versioned:
        lines.append(
            f"name (versioned): {_quoted(versioned)} carries a version, model, date or round: "
            "`version` and the channels (dev, stable) carry those"
        )
    scratch = [w for w in words if w in SCRATCH_WORDS]
    if scratch:
        lines.append(
            f"name (scratch): {_quoted(scratch)}: experiments go on the dev channel; a stable "
            "service's name says what it does"
        )
    if not lines:
        return [], None
    kept = words[1:] if org_repeat else list(words)
    kept = [w for w in map(_unversioned, kept) if w]
    kept = [w for i, w in enumerate(kept) if i == 0 or w != kept[i - 1]]
    suggested = ".".join(kept) if len(kept) >= FEWEST_WORDS else None
    if suggested == ".".join(words):
        suggested = None
    standard = f"the standard is {whole}<area>.<action>, at most one group between"
    if suggested is not None:
        old, new = (f"{tenant}.{name}", f"{tenant}.{suggested}") if tenant else (name, suggested)
        lines.append(
            f"name: suggested service: {suggested} ({standard}). Not deployed yet: write it in "
            f"{CARD}; deployed: service.rename moves it (amsyos rename {old} {new}) and what "
            "names the old id runs the new one"
        )
    else:
        lines.append(
            f"name: {standard}. Not deployed yet: write such a name in {CARD}; deployed: "
            "service.rename moves it (amsyos rename <old> <new>) and what names the old id runs "
            "the new one"
        )
    return lines, suggested


def card_warnings(card: dict[str, Any], tenant: str | None = None) -> list[str]:
    """`name_warnings` of a card's `service`; none when it is no name at all (that is a
    problem of the card's form, said already)."""
    name = card.get("service")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return []
    return name_warnings(name, tenant)[0]


# ---------------------------------------------------------------- the name init proposes
# Words of a folder's name or a README's title that name no area and no action.
GENERIC_WORDS: Final = frozenset({
    "amsyos", "my", "our", "the", "a", "an", "service", "services", "app", "project", "repo",
    "code", "tool", "tools", "script", "scripts", "main", "src", "job", "worker",
})  # fmt: skip
# Outputs named for no thing (`out`, `result`): their action is `make`.
GENERIC_OUTPUTS: Final = frozenset({"out", "output", "outputs", "result", "results", "file"})
# A tool a client looks under, from a program the code runs or a module it imports.
TOOL_AREAS: Final = {"blender": "blender", "bpy": "blender"}
# The domain a client looks under, from what the first output is (its `means`).
DOMAIN_OF_MEANS: Final = {
    "image": "image", "video": "video", "audio": "sound", "mesh": "mesh", "sheet": "sheet",
    "table": "table", "document": "document", "subtitles": "subtitles", "text": "text",
    "web": "web", "geo": "geo", "motion": "motion", "archive": "archive",
}  # fmt: skip


def _clean(words: list[str]) -> list[str]:
    """The words of a name without what the standard keeps out: versions, models, dates,
    scratch and generic words, a word that begins with a digit, a repeat."""
    kept: list[str] = []
    for word in map(_unversioned, words):
        if not _WORD.fullmatch(word) or word in SCRATCH_WORDS | GENERIC_WORDS | ORG_WORDS:
            continue
        if not kept or kept[-1] != word:
            kept.append(word)
    return kept


def propose(
    words: list[str], tools: list[str], outputs: dict[str, str], tenant: str | None = None
) -> str:
    """A card's `service` by the standard, `area.action`: the area the main tool the code
    uses (blender), else the first word of its title or folder (`words`, lowercase), else the
    domain of what it makes; the action the other words, else what it makes (`make_chart`,
    or `make`); never the organisation's name, a version or a scratch word. `outputs`: each
    output's name to what it is (`means`), in order."""
    org = tenant.removeprefix("t_") if tenant else None
    named = [w for w in _clean(words) if w not in (org, tenant)]
    area = next((TOOL_AREAS[t] for t in tools if t in TOOL_AREAS), None)
    first_out, means = next(iter(outputs.items()), (None, None))
    domain = DOMAIN_OF_MEANS.get(str(means or "").split(".")[0])
    if area is not None:
        rest = [w for w in named if w != area]
    elif named:
        area, rest = named[0][:40], named[1:]
    else:
        area, rest = domain or "files", []
    action = "_".join(rest[:3])[:40].strip("_")
    if not action:
        made = _unversioned(first_out or "") if first_out and _WORD.fullmatch(first_out) else ""
        action = "make" if not made or made in GENERIC_OUTPUTS | {area} else f"make_{made}"
    return f"{area}.{action[:40]}"
