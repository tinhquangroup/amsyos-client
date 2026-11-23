"""What every command shares: errors in the kernel's shape, output, the credentials file."""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from amsyos_cli.shell import cli_hint, with_cli_hints

DEFAULT_GATEWAY = "https://103.77.173.205"
MCP_PATH = "/mcp"


class CliError(Exception):
    """An error the caller can fix in one turn: `{"error": CODE, "hint": "next action"}`,
    the kernel's own shape (CLAUDE.md, "Errors are fixable in one turn")."""

    def __init__(self, code: str, hint: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {hint}")
        self.code = code
        self.hint = hint
        self.extra = extra or {}

    def answer(self) -> dict[str, Any]:
        return {"error": self.code, "hint": self.hint, **self.extra}


# Spec v2 §23 (kernel/runner.py `_marks`, storage/assets.py `_marked`): said once a command,
# the first time a `face` mark shows; the mark itself is never hidden.
FACE_NOTE = (
    "note: the mark 'face' means the file may show a real person's face. AMSYOS puts it on "
    "every picture-like file uploaded or made by an organisation's own code until a manager "
    "says otherwise; it matters only when the file goes to a provider whose terms forbid "
    "faces, or is shared or sold. A manager who knows it shows none, or holds the person's "
    "consent, lifts it: amsyos run asset.attest asset=<asset://...> kind=no_real_face "
    "note='<what you know>'"
)


@dataclass
class Out:
    """Short text by default; the whole answer as JSON with `--json`."""

    json: bool = False
    show_urls: bool = False
    verbose: bool = False
    face_told: bool = False  # the meaning of the mark `face` was said in this command
    # Fields every JSON answer of this command carries (`update`: a newer amsyos is out).
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    def debug(self, line: str) -> None:
        """What the CLI fell back to or left out, on stderr, only with `--verbose`."""
        if self.verbose:
            sys.stderr.write(f"[verbose] {line}\n")
            sys.stderr.flush()

    def marks(self, marks: Any) -> str:
        """A file's marks for a line of text; what `face` means is said the first time it
        shows in a command."""
        listed = [str(m) for m in seq(marks)]
        if not listed:
            return ""
        if "face" in listed and not self.face_told:
            self.face_told = True
            self.note(FACE_NOTE)
        return f"  marks {','.join(listed)}"

    def say(self, *lines: str) -> None:
        """Text for the reader; nothing in JSON mode (the JSON answer says it)."""
        if not self.json:
            for line in lines:
                sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def note(self, line: str) -> None:
        """Progress on stderr, so that stdout stays the answer."""
        if not self.json:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()

    def emit(self, answer: Any, *lines: str) -> None:
        """The answer: as JSON with `--json` (a hint that names an MCP call with its
        `hint_cli` beside it), else the text lines."""
        if self.json:
            shown: Any = with_cli_hints(answer)
            if isinstance(shown, dict) and self.extra:
                shown = {**self.extra, **cast("dict[str, Any]", shown)}
            sys.stdout.write(json.dumps(shown, ensure_ascii=False, indent=2) + "\n")
            sys.stdout.flush()
        else:
            self.say(*lines)

    def fail(self, err: CliError) -> int:
        """The error: its hint as the kernel said it in JSON, with `hint_cli` (the MCP
        calls it names as `amsyos` commands); in text, as those commands."""
        if self.json:
            answer = {**self.extra, **with_cli_hints(err.answer())}
            for key in ("partial", "log"):  # signed URLs: only with --show-urls, in JSON too
                if answer.get(key) and not self.show_urls:
                    answer[key] = "(hidden; --show-urls)"
            sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
        else:
            sys.stderr.write(f"error: {err.code}: {cli_hint(err.hint)}\n")
            for key, value in err.extra.items():
                if key in {"partial", "log"} and not self.show_urls:
                    continue  # signed URLs: only with --show-urls
                sys.stderr.write(f"  {key}: {short(value)}\n")
        return 1

    def url(self, value: str) -> str:
        """A signed URL is shown only with `--show-urls` (spec v3 §12)."""
        return value if self.show_urls else "(url hidden; --show-urls shows it)"

    def value(self, value: Any) -> str:
        """An output as a line says it: a file's link hidden (`url`); anything else, a task,
        a worker, an id, as it is (v3 T3, T5: `job: (url hidden)` said nothing)."""
        return self.url(value) if is_link(value) else short(value, 300)

    def outputs(self, outputs: dict[str, Any]) -> dict[str, Any]:
        """The outputs for a JSON answer: links only with `--show-urls`."""
        if self.show_urls:
            return outputs
        return {k: "(hidden; --show-urls)" if is_link(v) else v for k, v in outputs.items()}


def is_link(value: Any) -> bool:
    """A URL of a file (signed, or the Gateway's asset route): hidden unless asked."""
    return isinstance(value, str) and value.startswith(("https://", "http://"))


def obj(value: Any) -> dict[str, Any]:
    """A JSON object of an answer, or an empty one: answers are read, never trusted."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def seq(value: Any) -> list[Any]:
    """A JSON list of an answer, or an empty one."""
    return cast("list[Any]", value) if isinstance(value, list) else []


AMOUNT_PLACES = 8  # a cost or credit is said to this many decimals at most, trimmed


def amount(value: Any) -> str:
    """A cost, price, balance or budget as a line says it: fixed decimals, never an exponent,
    trimmed (`0.000065`, `0.0025`, `2.5`, `0`); not a number: as it is (`?` for none). A
    figure too small for `AMOUNT_PLACES` keeps its own digits (Claude Code, 2026-11-20:
    `discover` and `describe` printed `6.5e-05`)."""
    if value is None:
        return "?"
    if not isinstance(value, int | float) or isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return str(value)
    rounded = round(float(value), AMOUNT_PLACES)
    exact = Decimal(repr(rounded if rounded or not value else float(value)))
    text = f"{exact:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _plain(value: Any, found: dict[str, str], depth: int = 0) -> Any:
    """`value` with each float that JSON would write with an exponent swapped for a token
    `plain_json` writes back as fixed decimals."""
    if depth > 32:
        return value
    if isinstance(value, float) and math.isfinite(value) and "e" in repr(value):
        token = f"@@amount{len(found)}@@"
        found[token] = amount(value)
        return token
    if isinstance(value, dict):
        return {k: _plain(v, found, depth + 1) for k, v in cast("dict[Any, Any]", value).items()}
    if isinstance(value, list):
        return [_plain(v, found, depth + 1) for v in cast("list[Any]", value)]
    return value


def plain_json(value: Any, indent: int | None = None) -> str:
    """JSON for a line of text, its numbers in fixed decimals (`0.000065`, not `6.5e-05`):
    still JSON. `--json` answers keep the numbers as they came."""
    found: dict[str, str] = {}
    text = json.dumps(_plain(value, found), ensure_ascii=False, indent=indent)
    for token, said in found.items():
        text = text.replace(f'"{token}"', said)
    return text


def short(value: Any, most: int = 300) -> str:
    text = value if isinstance(value, str) else plain_json(value)
    return text if len(text) <= most else text[: most - 1] + "…"


def gateway_of(url: str) -> str:
    """`https://host`, `https://host/`, `https://host/mcp` are the same Gateway."""
    url = url.strip().rstrip("/")
    if url.endswith(MCP_PATH):
        url = url[: -len(MCP_PATH)]
    if not url.startswith(("https://", "http://")):
        raise CliError(
            "INVALID_INPUT", f"the Gateway is a URL like {DEFAULT_GATEWAY}, not '{short(url, 60)}'"
        )
    return url


# ---------------------------------------------------------------- the credentials file
def config_dir() -> Path:
    """`AMSYOS_CONFIG_DIR` (a login kept apart without moving every program's config, git's
    among them), else `$XDG_CONFIG_HOME/amsyos`, else `~/.config/amsyos`.
    TODO(review): OAuth — where the token file is read from and written to (mode 600)."""
    own = os.environ.get("AMSYOS_CONFIG_DIR")
    if own:
        return Path(own)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "amsyos"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


@dataclass
class Credentials:
    """`{gateway, token, expires_at, session}` (mode 600). The token is never printed."""

    gateway: str
    token: str
    expires_at: float | None = None
    session: str | None = None
    from_env: bool = False

    @property
    def mcp_url(self) -> str:
        return self.gateway + MCP_PATH


def save_credentials(creds: Credentials) -> Path:
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    path = credentials_path()
    body = json.dumps(
        {
            "gateway": creds.gateway,
            "token": creds.token,
            "expires_at": creds.expires_at,
            "session": creds.session,
        },
        indent=2,
    )
    # Written mode 600 from the first byte: never readable by another user, not for a moment.
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def forget_credentials() -> bool:
    try:
        credentials_path().unlink()
    except FileNotFoundError:
        return False
    return True


def read_credentials_file() -> Credentials | None:
    path = credentials_path()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise CliError(
            "NOT_LOGGED_IN", f"{path} cannot be read; amsyos logout, then amsyos login"
        ) from None
    data = obj(raw)
    if not isinstance(data.get("token"), str):
        raise CliError("NOT_LOGGED_IN", f"{path} holds no token; amsyos login")
    expires = data.get("expires_at")
    session = data.get("session")
    return Credentials(
        gateway=gateway_of(str(data.get("gateway") or DEFAULT_GATEWAY)),
        token=str(data["token"]),
        expires_at=float(expires) if isinstance(expires, int | float) else None,
        session=session if isinstance(session, str) else None,
    )


def credentials() -> Credentials:
    """The token to call with: `AMSYOS_TOKEN` when set (a host that holds its own), else
    the file `amsyos login` wrote. A file's token goes only to the Gateway it came from."""
    env_gateway = os.environ.get("AMSYOS_GATEWAY")
    token = os.environ.get("AMSYOS_TOKEN")
    if token:
        return Credentials(gateway_of(env_gateway or DEFAULT_GATEWAY), token.strip(), from_env=True)
    creds = read_credentials_file()
    if creds is None:
        start = gateway_of(env_gateway or DEFAULT_GATEWAY) + "/start"
        raise CliError(
            "NOT_LOGGED_IN",
            f"amsyos setup <file>, with the setup file a person downloads at {start}; or "
            "amsyos login (a person approves the code it shows)",
        )
    if env_gateway and gateway_of(env_gateway) != creds.gateway:
        raise CliError(
            "GATEWAY_MISMATCH",
            f"logged in at {creds.gateway}, but AMSYOS_GATEWAY is {gateway_of(env_gateway)}: "
            "unset AMSYOS_GATEWAY, or amsyos login again there",
        )
    if creds.expires_at is not None and creds.expires_at < time.time():
        raise CliError("SESSION_ENDED", _renew(creds, "the session ended at"))
    return creds


ENDING_S = 2 * 3600.0  # a session's last hours: each command says when it ends


def _renew(creds: Credentials, said: str) -> str:
    """When the session ends and what only the person can do about it."""
    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(creds.expires_at or 0))
    return (
        f"{said} {when} UTC; only the person can start a new one: a setup file from "
        f"{creds.gateway}/start (it lasts up to 30 days), then amsyos setup <file>; or amsyos "
        "login (the person approves its code; 12 hours at most). Tell the person; do not retry"
    )


def session_ending() -> str | None:
    """A line when the logged-in session ends within `ENDING_S` (the CLI's own file only)."""
    if os.environ.get("AMSYOS_TOKEN"):
        return None
    try:
        creds = read_credentials_file()
    except CliError:
        return None
    if creds is None or creds.expires_at is None:
        return None
    left = creds.expires_at - time.time()
    if not 0 < left <= ENDING_S:
        return None
    return "note: " + _renew(creds, f"this session ends in {left / 60:.0f} min, at")


def gateway_default() -> str:
    """Where `amsyos login` goes when not told."""
    env = os.environ.get("AMSYOS_GATEWAY")
    if env:
        return gateway_of(env)
    try:
        creds = read_credentials_file()
    except CliError:
        creds = None
    return creds.gateway if creds is not None else DEFAULT_GATEWAY
