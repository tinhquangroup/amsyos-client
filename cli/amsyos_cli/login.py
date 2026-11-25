"""Device-code login (spec v3 §4.2, the shape of RFC 8628).

1. `POST /oauth/device` gives a device code, a user code and the page to open.
2. A person opens the page, types the code and their key, picks the workspace, the budget
   and the hours, and approves.
3. The CLI polls the device grant on `POST /oauth/token`, or on `POST /oauth/device/token`
   where the Gateway serves it there, and keeps the token in the credentials file.

TODO(review): OAuth — the device code is a secret of this process (never printed); only
the user code and the page are shown, and the token goes only to the file, mode 600.
"""

from __future__ import annotations

import sys
import time
from typing import Any

import httpx

from amsyos_cli import __version__
from amsyos_cli.common import CliError, Credentials, save_credentials
from amsyos_cli.instance import host_of, instance_of  # noqa: F401 - their home before 0.5.14

CLIENT_ID = "amsyos-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TOKEN_ROUTES = ("/oauth/token", "/oauth/device/token")
# What /oauth/token says when it does not serve the device grant: try the other route.
NOT_HERE = frozenset({"unsupported_grant_type", "invalid_request", "invalid_client"})
HEADERS = {"User-Agent": f"amsyos-cli/{__version__}", "Accept": "application/json"}


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        told: Any = response.json()
    except ValueError:
        return {}
    return told if isinstance(told, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def login(
    gateway: str,
    *,
    workspace: str | None = None,
    budget: float | None = None,
    hours: float | None = None,
    host: str | None = None,
) -> Credentials:
    found_host, found_version = host_of()
    form: dict[str, str] = {
        "client_id": CLIENT_ID,
        "host": host or found_host,
        "host_version": found_version,
    }
    # What the person is offered first on the page; they decide there.
    if workspace:
        form["workspace"] = workspace
    if budget is not None:
        form["budget"] = str(budget)
    if hours is not None:
        form["hours"] = str(hours)
    with httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=False) as http:
        try:
            response = http.post(gateway + "/oauth/device", data=form)
        except httpx.HTTPError as exc:
            raise CliError(
                "UNREACHABLE", f"cannot reach {gateway} ({type(exc).__name__}); try again"
            ) from None
        if response.status_code in {404, 405}:
            raise CliError(
                "DEVICE_LOGIN_OFF",
                f"{gateway} has no device login yet; ask the operator for a token and set "
                "AMSYOS_TOKEN",
            )
        told = _json(response)
        if response.status_code >= 400 or "device_code" not in told:
            raise CliError(
                str(told.get("error") or "DEVICE_LOGIN_FAILED"),
                str(told.get("error_description") or told.get("hint") or "try amsyos login again"),
            )
        device_code = str(told["device_code"])
        interval = float(told.get("interval") or 5)
        deadline = time.monotonic() + float(told.get("expires_in") or 600)
        page = str(told.get("verification_uri") or gateway + "/device")
        full = str(told.get("verification_uri_complete") or page)
        # On stderr even with --json: the person must be sent to the page either way.
        sys.stderr.write(
            f"Open {full}\nand approve the code {told.get('user_code')} (it lasts 10 minutes).\n"
        )
        sys.stderr.flush()
        token = _poll(http, gateway, device_code, interval, deadline)
    creds = credentials_of(gateway, token)
    save_credentials(creds)
    return creds


def credentials_of(gateway: str, token: dict[str, Any]) -> Credentials:
    """The credentials a device grant's answer gives (`login`'s poll, `setup`)."""
    expires_in = token.get("expires_in")
    session = token.get("session")
    return Credentials(
        gateway=gateway,
        token=str(token["access_token"]),
        expires_at=time.time() + float(expires_in) if isinstance(expires_in, int | float) else None,
        session=session if isinstance(session, str) else None,
    )


def redeem(
    http: httpx.Client,
    gateway: str,
    device_code: str,
    routes: list[str],
    client_id: str = CLIENT_ID,
) -> tuple[int, dict[str, Any]]:
    """One ask of the device grant: the HTTP status and the answer's JSON. On the first of
    `routes`, or on the next where a Gateway does not serve the grant there (`routes` keeps
    the one that answered, for the next ask). `login`'s poll and `setup` both ask this way;
    a network error is the caller's (httpx.HTTPError).
    TODO(review): OAuth — the device code goes only in this form's body, to the Gateway."""
    form = {"grant_type": DEVICE_GRANT, "device_code": device_code, "client_id": client_id}
    while True:
        response = http.post(gateway + routes[0], data=form)
        told = _json(response)
        error = str(told.get("error") or "")
        if len(routes) > 1 and (response.status_code in {404, 405} or error in NOT_HERE):
            routes.pop(0)  # this Gateway answers the device grant on its own route
            continue
        return response.status_code, told


def _poll(
    http: httpx.Client, gateway: str, device_code: str, interval: float, deadline: float
) -> dict[str, Any]:
    routes: list[str] = list(TOKEN_ROUTES)
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            status, told = redeem(http, gateway, device_code, routes)
        except httpx.HTTPError:
            continue  # the network blinked: ask again at the next interval
        error = str(told.get("error") or "")
        if status == 200 and "access_token" in told:
            return told
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise CliError("LOGIN_EXPIRED", "the code was not approved in time; amsyos login again")
        if error == "access_denied":
            raise CliError("LOGIN_DENIED", "the person denied this login")
        raise CliError(
            error or "LOGIN_FAILED",
            str(told.get("error_description") or f"HTTP {status}; amsyos login again"),
        )
    raise CliError("LOGIN_EXPIRED", "the code was not approved in time; amsyos login again")
