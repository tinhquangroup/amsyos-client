"""`amsyos setup`: log in with the setup file a person downloaded at `{gateway}/start` (the
owner, 2026-11-20: one file to give an AI client instead of five steps).

The page approved a device login before it made the file (a pre-approved device code,
gateway/start.py), and the file carries that device code in one machine block at its end:

    <!-- amsyos-setup v1
    gateway: https://103.77.173.205
    client_id: amsyos-cli
    code: <device_code>
    expires: <ISO 8601 UTC, when the code can no longer be redeemed>
    -->

`setup` reads the last such block (or `--code`), redeems the code once on the device grant
exactly as `login`'s poll does (`login.redeem`), and keeps the token as `login` does. A used,
expired or unknown code is said with the next action: a new file from `{gateway}/start`.

The credentials file also keeps the SHA-256 of the code it was made with
(`setup_code_sha256`), never the code: the same file given again on the machine it set up,
whose session is still live, is "already set up" (exit 0, the overview), not "used" (T2,
2026-11-20: an agent that ran `setup` twice was told to download a new file). A login kept
without that hash (made by amsyos 0.3.5 or older), or by another file, is "already logged in"
when its live session is the file's own person's (`u_…` in the file's first lines) or ends
when the file's code did, within 120 s (a customer, 2026-11-20).

TODO(review): OAuth — the code is a secret of the file and of this process: never printed,
in text or JSON, nor in an error; it goes only to the file's own Gateway, over HTTPS (HTTP
only to this machine's loopback); everything that could still refuse here is checked before
it is redeemed, so that a refusal of this command never spends it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from amsyos_cli import __version__
from amsyos_cli.api import Api
from amsyos_cli.common import (
    DEFAULT_GATEWAY,
    CliError,
    Credentials,
    Out,
    config_dir,
    credentials_path,
    gateway_default,
    gateway_of,
    obj,
    read_credentials_file,
    short,
)
from amsyos_cli.login import CLIENT_ID, HEADERS, TOKEN_ROUTES, credentials_of, redeem
from amsyos_cli.overview import overview, session_show

VERSION = "v1"
KEYS = ("gateway", "client_id", "code", "expires")
OPENS = re.compile(r"<!--[ \t]*amsyos-setup\b")
MOST_BYTES = 1 << 20  # a setup file is a page of text; anything bigger is not one
CODE_SHAPE = re.compile(r"[\w.~+/=-]{8,256}", re.A)
CLIENT_SHAPE = re.compile(r"[\w.-]{1,64}", re.A)
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
FILE_NAME = "config-amsyos.md"
# The person the file was made for, as gateway/start.py writes it ("for An (u_3f2a...), of
# the organisation ..."): read from the prose above the block, never trusted for anything
# but "is the login this machine keeps the same person's".
PERSON = re.compile(r"\((u_[A-Za-z0-9]{1,64})\),?\s+of\s+the\s+organisation\b")
SAME_END_S = 120.0  # the file's `expires` and its session's end, made in one request


@dataclass(frozen=True)
class Setup:
    gateway: str
    client_id: str
    code: str = field(repr=False)  # the secret: never in a repr, a line or an answer
    expires: str | None = None
    person: str | None = None  # the `u_…` the file names; None for --code

    @property
    def new_file(self) -> str:
        return f"download a new one at {self.gateway}/start"


def _start(gateway: str | None = None) -> str:
    return f"{gateway or DEFAULT_GATEWAY}/start"


# ---------------------------------------------------------------- the file
def read_file(path: Path) -> Setup:
    try:
        with path.open("rb") as fh:
            raw = fh.read(MOST_BYTES + 1)
    except FileNotFoundError:
        raise CliError(
            "INVALID_INPUT",
            f"no file {path}: give the path of the setup file ({FILE_NAME}) the person "
            f"downloaded at {_start(gateway_default())}",
        ) from None
    except OSError as exc:
        raise CliError(
            "INVALID_INPUT", f"{path} cannot be read ({type(exc).__name__}): give a file's path"
        ) from None
    if len(raw) > MOST_BYTES:
        raise CliError(
            "SETUP_FILE_INVALID",
            f"{path} is over 1 MB: not a setup file; give {FILE_NAME} as downloaded at "
            f"{_start(gateway_default())}",
        )
    return parse(raw.decode("utf-8", "replace"), str(path))


def parse(text: str, where: str) -> Setup:
    """The last `<!-- amsyos-setup v1 ... -->` block of a file: every problem in one answer.
    From its last opening, so that the file's prose may name the block before it."""
    opened = list(OPENS.finditer(text))
    rest = text[opened[-1].end() :] if opened else ""
    if not opened or "-->" not in rest:
        raise CliError(
            "SETUP_FILE_INVALID",
            f"{where} holds no <!-- amsyos-setup {VERSION} ... --> block: it is not a setup "
            f"file, or it was cut; download it again at {_start(gateway_default())}",
        )
    head, _, body = rest[: rest.index("-->")].partition("\n")
    version = head.strip()
    fields: dict[str, str] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if sep and key and key not in fields:
            fields[key] = value.strip()
    try:
        gateway = gateway_of(fields["gateway"]) if fields.get("gateway") else None
    except CliError:
        gateway = None
    if version != VERSION:
        raise CliError(
            "SETUP_FILE_INVALID",
            f"{where} is a setup file of version {short(version, 20) or '(none)'}; amsyos "
            f"{__version__} reads {VERSION}: amsyos self-update (an install of 0.3.12 or older: "
            f"uvx --from <the wheel {gateway or DEFAULT_GATEWAY}/cli names> amsyos install), "
            "then amsyos setup again",
        )
    missing = [k for k in KEYS if not fields.get(k)]
    problems = [f"lacks {', '.join(missing)}"] if missing else []
    if fields.get("gateway") and gateway is None:
        problems.append("its gateway is no https:// URL")
    if fields.get("client_id") and not CLIENT_SHAPE.fullmatch(fields["client_id"]):
        problems.append("its client_id is not one AMSYOS gives")
    if fields.get("code") and not CODE_SHAPE.fullmatch(fields["code"]):
        problems.append("its code is not one AMSYOS gives")
    if problems:
        raise CliError(
            "SETUP_FILE_INVALID",
            f"the setup block of {where} {'; '.join(problems)}: the file was cut or edited; "
            f"download a new one at {_start(gateway)}",
            {"missing": missing} if missing else None,
        )
    assert gateway is not None  # noqa: S101 - checked above: a missing or bad one refused
    person = PERSON.search(text[: opened[-1].start()])
    return Setup(
        gateway, fields["client_id"], fields["code"], fields["expires"],
        person.group(1) if person else None,
    )  # fmt: skip


def given(path: str | None, code: str | None, gateway: str | None) -> Setup:
    """What the command line names: a setup file, or a code and its Gateway."""
    if path and code:
        raise CliError(
            "INVALID_INPUT", "amsyos setup takes a setup file or --code <code>, not both"
        )
    if not path and not code:
        raise CliError(
            "INVALID_INPUT",
            f"amsyos setup <file>: the setup file ({FILE_NAME}) the person downloads at "
            f"{_start(gateway_default())}; or amsyos setup --code <code> [--gateway <url>]",
        )
    if path:
        setup = read_file(Path(path).expanduser())
        if gateway and gateway_of(gateway) != setup.gateway:
            raise CliError(
                "INVALID_INPUT",
                f"the file names its own Gateway, {setup.gateway}; --gateway goes with --code "
                "only: leave it out",
            )
    else:
        code = (code or "").strip()
        if not CODE_SHAPE.fullmatch(code):
            raise CliError(
                "INVALID_INPUT",
                "--code is not a code AMSYOS gives: copy it whole from the setup file's block "
                f"(code: ...), or give the file itself: amsyos setup {FILE_NAME}",
            )
        where = gateway_of(gateway) if gateway else gateway_default()
        setup = Setup(where, CLIENT_ID, code)
    _check_transport(setup.gateway)
    return setup


def _check_transport(gateway: str) -> None:
    """The code goes over HTTPS only; over HTTP to this machine alone (a Gateway run here)."""
    parts = urlsplit(gateway)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK):
        return
    raise CliError(
        "INVALID_INPUT",
        f"{gateway} is not HTTPS: a setup code goes only over HTTPS (HTTP only to this "
        f"machine); use the https:// address of the Gateway, as {_start()} gives it",
    )


# ---------------------------------------------------------------- before the code is spent
def ready(setup: Setup) -> Path:
    """What would make the login useless once redeemed, refused first: the folder the token
    is kept in (it must be writable) and an AMSYOS_GATEWAY that names another Gateway."""
    env = os.environ.get("AMSYOS_GATEWAY")
    if env and gateway_of(env) != setup.gateway:
        raise CliError(
            "GATEWAY_MISMATCH",
            f"AMSYOS_GATEWAY is {gateway_of(env)}, but the setup is for {setup.gateway}: unset "
            "AMSYOS_GATEWAY (or set it to that one), then amsyos setup again; the code was not "
            "used",
        )
    folder = config_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError as exc:
        raise CliError(
            "CONFIG_NOT_WRITABLE",
            f"{folder} cannot be written ({type(exc).__name__}): set AMSYOS_CONFIG_DIR to a "
            "folder of yours, then amsyos setup again; the code was not used",
        ) from None
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    return folder


# ---------------------------------------------------------------- the one redemption
def redeem_once(setup: Setup) -> Credentials:
    """The code, redeemed once on the device grant (`login.redeem`, both `TOKEN_ROUTES`).
    TODO(review): OAuth — the token comes back only to the caller, which keeps it in the
    credentials file (mode 600); nothing here prints it or the code."""
    with httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=False) as http:
        try:
            status, told = redeem(
                http, setup.gateway, setup.code, list(TOKEN_ROUTES), setup.client_id
            )
        except httpx.HTTPError as exc:
            raise CliError(
                "UNREACHABLE",
                f"cannot reach {setup.gateway} ({type(exc).__name__}): check the network, then "
                "amsyos setup again (a code not yet handed out still works)",
            ) from None
    if status == 200 and isinstance(told.get("access_token"), str):
        return credentials_of(setup.gateway, told)
    raise refusal(setup, status, told)


# ---------------------------------------------------------------- the same file again
HASH_KEY = "setup_code_sha256"


def code_sha256(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def remember(setup: Setup, creds: Credentials) -> None:
    """Keep the SHA-256 of the file's code in the credentials file, next to the token this
    code gave (`creds`, saved just before): never the code itself. Quiet when that file
    holds another token or cannot be written; the login stands either way.
    TODO(review): OAuth — the hash goes only into the mode-600 credentials file."""
    path = credentials_path()
    try:
        data = obj(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return
    if data.get("token") != creds.token:
        return
    data[HASH_KEY] = code_sha256(setup.code)
    tmp = path.with_suffix(".tmp")
    with contextlib.suppress(OSError):
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.replace(path)


def _same_file(setup: Setup) -> bool:
    """The credentials file keeps this very code's hash: this file set this machine up."""
    try:
        data = obj(json.loads(credentials_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False
    return data.get(HASH_KEY) == code_sha256(setup.code)


def kept_for(setup: Setup) -> Credentials | None:
    """The login this machine keeps for the file's Gateway, when it is not past its end:
    one this very file made (its code's hash), or one `again` may still find to be the
    file's person's (a login of amsyos 0.3.5 or older keeps no hash). None otherwise
    (whether the session is still live, and whose, is asked of the Gateway: `again`)."""
    try:
        creds = read_credentials_file()
    except (CliError, OSError, ValueError):
        return None
    if creds is None or creds.from_env or creds.gateway != setup.gateway:
        return None
    if creds.expires_at is not None and creds.expires_at < time.time():
        return None
    return creds


def _when(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def same_login(setup: Setup, shown: dict[str, Any]) -> bool:
    """The live session `session.show` answers is the one this file was made for: its
    person is the `u_…` the file names, or it ends when the file's code did (within
    `SAME_END_S`: the page made both in one request, gateway/start.py).
    TODO(review): OAuth — this only decides between "already logged in" and
    SETUP_FILE_USED; nothing is redeemed, kept or widened by it."""
    if not isinstance(shown.get("session"), str):
        return False
    if setup.person and shown.get("person") == setup.person:
        return True
    ends, expires = _when(shown.get("expires_at")), _when(setup.expires)
    return (
        ends is not None
        and expires is not None
        and (abs((ends - expires).total_seconds()) <= SAME_END_S)
    )


async def again(setup: Setup, kept: Credentials, used: CliError, out: Out) -> int:
    """The file was redeemed already, and this machine keeps a login whose session still
    answers `session.show`: made by this very file (its code's hash), or the file's own
    person's (`same_login`, for a login kept without the hash). "Already set up" or
    "already logged in", then the overview as `whoami` gives it (exit 0). A session ended,
    refused or of somebody else: the Gateway's SETUP_FILE_USED as it was.
    TODO(review): OAuth — the kept token goes only to its own Gateway, as any command's."""
    same_file = _same_file(setup)
    try:
        async with Api(kept, out) as api:
            shown = await session_show(api)
            if kept.session and shown.get("session") not in {None, kept.session}:
                raise used
            if not same_file and not same_login(setup, shown):
                raise used
            told, lines = await overview(api, Path.cwd(), shown=shown)
    except CliError:
        raise used from None
    answer: dict[str, Any] = {
        "logged_in": True, "gateway": kept.gateway, "kept_in": str(credentials_path()),
        **({"already_set_up": True} if same_file else {"already_logged_in": True}), **told,
    }  # fmt: skip
    if same_file:
        said = [
            f"already set up with this file on this machine: logged in at {kept.gateway} with "
            f"its session (the token is in {credentials_path()}); nothing to do"
        ]
    else:
        until = _when(shown.get("expires_at"))
        said = [
            f"already logged in on this machine (session {shown.get('session')}, until "
            f"{until.strftime('%Y-%m-%d %H:%M UTC') if until else 'its end'}) at {kept.gateway}:"
            " this file's code was used already, and this login is its person's; nothing to do"
        ]
    if os.environ.get("AMSYOS_TOKEN"):
        said.append("AMSYOS_TOKEN is set in this environment and wins over this login: unset it")
    out.emit(answer, *said, *lines)
    return 0


def _expired(setup: Setup) -> bool:
    """The file's own `expires` has passed (a Gateway may forget an old code entirely)."""
    if not setup.expires:
        return False
    try:
        when = datetime.fromisoformat(setup.expires)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when <= datetime.now(UTC)


def refusal(setup: Setup, status: int, told: dict[str, Any]) -> CliError:
    """The Gateway's refusal as the next action (the interface of the setup file)."""
    error = str(told.get("error") or "")
    said = str(told.get("error_description") or told.get("hint") or "")
    said = short(said.replace(setup.code, "(the code)"), 300) if said else ""
    extra = {"oauth_error": error} if error else None
    if error == "invalid_grant" and "handed out" in said:
        return CliError(
            "SETUP_FILE_USED",
            f"this file was used already: {setup.new_file} (one file for each AI client)",
            extra,
        )
    if error == "expired_token" or (error == "invalid_grant" and _expired(setup)):
        return CliError("SETUP_FILE_EXPIRED", f"this file expired: {setup.new_file}", extra)
    if error == "access_denied":
        return CliError("SETUP_DENIED", said or "the Gateway refused this login", extra)
    if error == "invalid_grant":
        return CliError(
            "SETUP_CODE_UNKNOWN",
            f"{setup.gateway} does not know this setup code (a file of another Gateway, or an "
            f"edited code): {setup.new_file}",
            extra,
        )
    if error in {"authorization_pending", "slow_down"}:
        return CliError(
            "SETUP_NOT_APPROVED",
            "this code still waits for a person's approval: it is the code of an amsyos login, "
            f"not of a setup file; {setup.new_file}, or amsyos login",
            extra,
        )
    if status in {404, 405}:
        return CliError(
            "DEVICE_LOGIN_OFF",
            f"{setup.gateway} has no device login yet; ask the operator for a token and set "
            "AMSYOS_TOKEN",
        )
    return CliError(error or "SETUP_FAILED", said or f"HTTP {status}; amsyos setup again", extra)
