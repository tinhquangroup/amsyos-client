"""Hints in the syntax of the command line (ChatGPT, 2026-11-20: `amsyos describe
data.tranform` answered with the hint `amsyos.discover('data tranform')`, an MCP call; a
shell needs `amsyos discover "data tranform"`).

The kernel's hints name its six tools as an MCP client calls them. Text mode shows them as
the `amsyos` command a shell runs; `--json` keeps the hint as it came and adds `hint_cli`.
A call this cannot say plainly (a param that is an object, a value that reads as a number
but is text) is left as it came.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, cast

TOOLS = ("discover", "describe", "run", "status", "result", "cancel")
_NAMED = re.compile(r"(?<![\w.])amsyos\.(" + "|".join(TOOLS) + r")(?![\w-]|\.\w)")
_PLAIN = re.compile(r"^[\w.@:/+,=-]+$")  # one word both shells take as it is
_EXPANDS = frozenset("$`")  # expanded inside double quotes, by POSIX shells and PowerShell
CALL_MOST = 2000  # characters of a call's arguments read; a longer call is left as it is
SCALARS = (str, int, float, bool)


def quoted(text: str) -> str:
    """One argument as PowerShell and a POSIX shell both read it: a plain word as it is;
    else in double quotes. Text with a double quote, `$` or a backtick and no single quote
    goes in single quotes, literal in both shells; with both kinds of quote, the double
    quotes are escaped (`\\"`, as a POSIX shell reads them)."""
    if text and _PLAIN.match(text):
        return text
    if '"' not in text and not _EXPANDS & set(text):
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    return '"' + text.replace('"', '\\"') + '"'


def _end(text: str, start: int) -> int | None:
    """Where the bracket at `start` closes (the index after it), quotes minded."""
    depth = 0
    quote: str | None = None
    i = start
    while i < len(text) and i - start <= CALL_MOST:
        c = text[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _values(inside: str) -> list[Any] | None:
    """A call's arguments, as JSON or as Python writes them (`'data tranform'`)."""
    try:
        return cast("list[Any]", json.loads(f"[{inside}]"))
    except ValueError:
        pass
    try:
        found: Any = ast.literal_eval(f"[{inside}]")
    except Exception:  # SyntaxError, ValueError, a nesting too deep: not a call to say
        return None
    return cast("list[Any]", found) if isinstance(found, list) else None


def _one(values: list[Any], *keys: str) -> str | None:
    """The one id or text a call names: `('x')` or `({"service_id": "x"})`."""
    if len(values) != 1:
        return None
    given = values[0]
    if isinstance(given, dict):
        named = cast("dict[str, Any]", given)
        found = [named[k] for k in keys if k in named]
        if len(found) != 1 or set(named) - {*keys, "wait_s", "workspace"}:
            return None
        given = found[0]
    return given if isinstance(given, str) and given.strip() else None


def _arg(key: str, value: Any) -> str | None:
    """`k=v` as `amsyos run` reads it back (amsyos_cli/jobs.py `parse_value`); None when
    the value is no plain one."""
    if not isinstance(value, SCALARS) and value is not None:
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
            return None  # "12", "true": text that `k=v` would read as a number
        except ValueError:
            text = value
    else:
        text = json.dumps(value)
    return quoted(f"{key}={text}")


def _run(values: list[Any]) -> str | None:
    service: Any = None
    params: Any = {}
    if values and isinstance(values[0], dict):
        if len(values) != 1:
            return None
        call = cast("dict[str, Any]", values[0])
        service = call.get("service_id") or call.get("id") or call.get("service")
        params = call.get("params", {})
    elif 1 <= len(values) <= 2:
        service = values[0]
        params = values[1] if len(values) == 2 else {}
    if not isinstance(service, str) or not _PLAIN.match(service) or not isinstance(params, dict):
        return None
    args = [_arg(str(k), v) for k, v in cast("dict[str, Any]", params).items()]
    if any(a is None for a in args):
        return None
    return " ".join(["amsyos run", service, *cast("list[str]", args)])


def _command(tool: str, values: list[Any]) -> str | None:
    """The shell command of one MCP call; None when it cannot be said plainly."""
    if tool == "run":
        return _run(values)
    if tool == "discover":
        words = _one(values, "query")
        return None if words is None else f"amsyos discover {quoted(words)}"
    if tool == "describe":
        ref = _one(values, "service_id", "id", "service")
    else:
        ref = _one(values, "job_id")
    return None if ref is None or not _PLAIN.match(ref) else f"amsyos {tool} {ref}"


# Claude Code, 2026-11-20: hints still said "call amsyos.run again" and named services as
# calls (`capability.request {gap: 'gp_…'}`); a shell runs neither.
_AGAIN = re.compile(r"\b([Cc])all amsyos\.run again\b")
AGAIN = "un the same `amsyos run` again"  # after its R or r
_SERVICE = r"(?!amsyos\.)[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}"
_SERVICE_CALL = re.compile(r"(?<![\w.'\"/:@$-])(" + _SERVICE + r") \{")
_CALL_SERVICE = re.compile(r"\b([Cc])all (`?)(" + _SERVICE + r")\2(?!\.?\w| ?\{)")
_KEY = re.compile(r"^[A-Za-z_]\w*$")
# A file's name is no service (`main.py {…}`).
FILE_ENDINGS = frozenset({
    "py", "md", "yaml", "yml", "json", "txt", "csv", "toml", "sh", "js", "ts", "html", "png",
    "jpg", "wav", "mp4", "zip", "gz", "tar", "xlsx",
})  # fmt: skip
_AN_ID = re.compile(r"^[a-z]{1,4}_[0-9a-f]{6,}$")  # gp_…, j_…, o_…: an id said bare
_UNSAID = frozenset({"…", "...", ""})


def _items(inside: str) -> list[str] | None:
    """The comma-separated items of `{…}`, brackets and quotes minded; None if unclosed."""
    items: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for i, c in enumerate(inside):
        if quote is not None:
            if c == quote and inside[i - 1] != "\\":
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            items.append(inside[start:i])
            start = i + 1
    if quote is not None or depth:
        return None
    return [*items, inside[start:]]


def _scalar(text: str) -> tuple[bool, Any]:
    """A value a hint gives in `{key: value}`: JSON, as Python writes it, or an id said
    bare; (False, None) when it is words, not a value."""
    value: Any = _read(text)
    if value is _NOT_READ:
        return (True, text) if _AN_ID.match(text) else (False, None)
    return (True, value) if isinstance(value, SCALARS) else (False, None)


_NOT_READ = object()


def _read(text: str) -> Any:
    """`text` as JSON, else as Python writes a literal (`'gp_1'`); `_NOT_READ` if neither."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return ast.literal_eval(text)
    except Exception:  # SyntaxError, ValueError, a nesting too deep: not a value
        return _NOT_READ


def _service_args(inside: str) -> list[str] | None:
    """`{gap: 'gp_1', hours}` as `k=v` words of `amsyos run`: a value given as it is, a
    name alone (or `…`) as `<name>` to fill in; None when the hint's words are no plain
    values (`{spec: the spec in that card}`, `{gap | service}`)."""
    items = _items(inside)
    if items is None:
        return None
    args: list[str] = []
    for item in (i.strip() for i in items):
        if not item:
            continue
        key, _, rest = item.partition(":")
        key, rest = key.strip(), rest.strip()
        if not _KEY.match(key):
            return None
        if rest in _UNSAID or (rest.startswith("<") and rest.endswith(">")):
            args.append(quoted(f"{key}=<{key}>"))
            continue
        ok, value = _scalar(rest)
        said = _arg(key, value) if ok else None
        if said is None:
            return None
        args.append(said)
    return args


def _services(text: str) -> str:
    """A service named as a call (`capability.request {gap: 'gp_1'}`) as `amsyos run
    capability.request gap=gp_1`; one whose params cannot be said plainly is left as it
    came, with `(amsyos describe <id>)` after it."""
    out: list[str] = []
    at = 0
    for found in _SERVICE_CALL.finditer(text):
        service = found.group(1)
        if found.start() < at or service.rsplit(".", 1)[-1] in FILE_ENDINGS:
            continue
        brace = found.end() - 1
        # A quote the hint's words leave open (`option's`): to the first `}`.
        close = _end(text, brace) or (text.find("}", brace) + 1) or None
        if close is None:
            continue
        args = _service_args(text[brace + 1 : close - 1])
        out.append(text[at : found.start()])
        if args is None:
            out.append(f"{text[found.start() : close]} (amsyos describe {service})")
        else:
            out.append(" ".join(["amsyos run", service, *args]))
        at = close
    out.append(text[at:])
    return "".join(out)


def _again(text: str) -> str:
    """`call amsyos.run again` → ``run the same `amsyos run` again``; `call x.y` (a service
    named as a call, no params) → ``use `amsyos run x.y` ``."""
    text = _AGAIN.sub(lambda m: f"{'R' if m[1] == 'C' else 'r'}{AGAIN}", text)
    return _CALL_SERVICE.sub(
        lambda m: (
            m[0]
            if m[3].rsplit(".", 1)[-1] in FILE_ENDINGS
            else f"{'U' if m[1] == 'C' else 'u'}se `amsyos run {m[3]}`"
        ),
        text,
    )


def cli_hint(text: str) -> str:
    """A hint with every MCP call it names as the `amsyos` command a shell runs:
    `amsyos.discover('x y')` → `amsyos discover "x y"`, `amsyos.describe({"service_id":
    "x"})` → `amsyos describe x`, `amsyos.run('s', {"k": 1})` → `amsyos run s k=1`, and a
    tool named alone (`amsyos.status shows its steps`) → `amsyos status`. Also `call
    amsyos.run again` → ``run the same `amsyos run` again``, and a service named as a call
    (`capability.request {gap: 'gp_1'}`, `call service.check`) → `amsyos run …`."""
    text = _services(_again(text))
    if "amsyos." not in text:
        return text
    out: list[str] = []
    at = 0
    for found in _NAMED.finditer(text):
        if found.start() < at:
            continue  # inside a call already said
        tool = found.group(1)
        out.append(text[at : found.start()])
        at = found.end()
        rest = text[at:]
        gap = len(rest) - len(rest.lstrip(" "))
        opening = rest[gap : gap + 1]
        if (opening == "(" and not gap) or opening == "{":
            begin = at + gap
            close = _end(text, begin)
            if close is not None:
                inside = text[begin + 1 : close - 1] if opening == "(" else text[begin:close]
                values = _values(inside)
                said = _command(tool, values) if values is not None else None
                if said is not None:
                    out.append(said)
                    at = close
                    continue
                if opening == "(":  # as it came: a call not said plainly
                    out.append(text[found.start() : close])
                    at = close
                    continue
        out.append(f"amsyos {tool}")  # the tool alone; what follows it stays
    out.append(text[at:])
    return "".join(out)


def with_cli_hints(value: Any, depth: int = 0) -> Any:
    """An answer for `--json`: every `hint` that names an MCP call keeps its words, with
    `hint_cli` beside it."""
    if depth > 8:
        return value
    if isinstance(value, list):
        return [with_cli_hints(v, depth + 1) for v in cast("list[Any]", value)]
    if not isinstance(value, dict):
        return value
    shown = {k: with_cli_hints(v, depth + 1) for k, v in cast("dict[str, Any]", value).items()}
    hint = shown.get("hint")
    if isinstance(hint, str) and "hint_cli" not in shown:
        said = cli_hint(hint)
        if said != hint:
            shown["hint_cli"] = said
    return shown
