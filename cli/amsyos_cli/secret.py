"""`amsyos secret`: keys an agent's scripts use, kept out of the agent's context (2026-11-25).

AI clients read `.env` files, print tokens into logs and chats, and everything in their
context goes to the model's provider. With this, a workspace keeps its secrets in AMSYOS,
sealed, and a command gets them in its environment without the agent ever seeing them:

- `amsyos secret set NAME [--about "..."]` asks AMSYOS for a link (`secret.ask`) and prints
  it for the person: they open it, type the value and the AMSYOS key of a manager of the
  workspace or an admin of the organisation. The agent never asks for the value itself.
  With `--prompt`, a person at a terminal types both here (`getpass`, only when stdin is a
  terminal, so an agent's shell cannot), and they are sent through the page's own POST.
- `amsyos secret ls`: names, who set each and when, the last use and the number of uses.
- `amsyos secret rm NAME`: a manager of the workspace or an admin.
- `amsyos secret run --env NAME [--env NAME=VAR …] -- <command>`: the values fetched from
  the Gateway's own route (`POST /secrets/values`, never an MCP answer), added to the
  command's environment (as NAME, or as VAR), its stdout and stderr passed through a masker
  that replaces each value of 6 characters or more with `[secret NAME]`, also across chunks;
  it ends with the command's exit code.
- `amsyos bg start --env NAME -- <command>` does the same for a watched task (bg.py): its
  supervisor fetches the values at start and writes the output into its log masked.

The limit, plainly: this protects against accidents (a printed log, a pasted output, an
environment dump), not against an agent that means to print a value (it could encode it,
or write it to a file). On this machine it is the same exposure as a key in a `.env`, minus
the file. Standard library only at import (bg's supervisor loads it); the HTTP client is
loaded only to call.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from amsyos_cli.common import CliError, Out, credentials, obj, seq, short

ASK, LIST, REMOVE = "secret.ask", "secret.list", "secret.remove"
VALUES_PATH = "/secrets/values"
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MASK_MIN = 6  # shorter values are not masked: too many innocent words would be
MOST_NAMES = 20
CHUNK = 65536
JOIN_S = 5.0
LIMIT = (
    "This protects against accidents (a printed log, a pasted output, an environment dump), "
    "not against an agent that means to print a value; on this machine it is the same "
    "exposure as a key in a .env, minus the file. Values shorter than 6 characters are not "
    "masked."
)
RUN_USAGE = "amsyos secret run --env NAME [--env NAME=VAR ...] -- <command...>"


# ---------------------------------------------------------------- names
def name_of(given: str) -> str:
    if not NAME.match(given or ""):
        raise CliError(
            "INVALID_INPUT",
            "a secret's name is an environment variable's: A-Z first, then A-Z, 0-9 or _, 2 to "
            f"64 characters (e.g. STRIPE_KEY), not {short(given, 40)!r}",
        )
    return given


def env_specs(given: list[str]) -> list[tuple[str, str]]:
    """`--env NAME` and `--env NAME=VAR`: (the secret's name, the variable it is given as)."""
    pairs: list[tuple[str, str]] = []
    for item in given:
        name, _, var = item.partition("=")
        name_of(name)
        var = var or name
        if not VAR.match(var):
            raise CliError(
                "INVALID_INPUT",
                f"--env {short(item, 40)}: VAR is an environment variable's name (letters, "
                "digits and _, a letter or _ first)",
            )
        if any(v == var for _, v in pairs):
            raise CliError("INVALID_INPUT", f"--env gives {var} twice")
        pairs.append((name, var))
    if len({n for n, _ in pairs}) > MOST_NAMES:
        raise CliError("INVALID_INPUT", f"at most {MOST_NAMES} secrets a command")
    return pairs


# ---------------------------------------------------------------- the masker
class Masker:
    """Each value (of `MASK_MIN` characters or more) replaced with `[secret NAME]` in a
    stream of bytes, also when a value is split across chunks: the end of a chunk that could
    begin a value is held, unmasked, until the next one shows whether it does (a value that
    begins another, longer one included); the rest goes on at once."""

    def __init__(self, values: dict[str, str]) -> None:
        by: dict[bytes, bytes] = {}
        for name, value in values.items():
            raw = value.encode()
            if len(value) >= MASK_MIN and raw not in by:
                by[raw] = f"[secret {name}]".encode()
        self._by = by
        ordered = sorted(by, key=len, reverse=True)  # the longest first, where two overlap
        self._re = re.compile(b"|".join(re.escape(v) for v in ordered)) if ordered else None
        self._longest = max((len(v) for v in ordered), default=0)
        self._firsts = {v[0] for v in ordered}
        self._held = b""

    def _masked(self, raw: bytes) -> bytes:
        return self._re.sub(lambda m: self._by[m.group(0)], raw) if self._re else raw

    def _tail(self, buf: bytes) -> int:
        """Where the longest end of `buf` that could begin a longer value starts (len: none)."""
        for i in range(max(0, len(buf) - self._longest + 1), len(buf)):
            rest = buf[i:]
            if buf[i] in self._firsts and any(
                len(v) > len(rest) and v.startswith(rest) for v in self._by
            ):
                return i
        return len(buf)

    def feed(self, data: bytes) -> bytes:
        if self._re is None:
            return data
        buf = self._held + data
        cut = self._tail(buf)
        for m in self._re.finditer(buf):
            if m.start() >= cut:
                break
            if m.end() > cut:  # a whole value runs across the cut: held whole, masked later
                cut = m.start()
                break
        self._held = buf[cut:]
        return self._masked(buf[:cut])

    def end(self) -> bytes:
        """What is held at the end of the stream, masked."""
        rest, self._held = self._held, b""
        return self._masked(rest)


def pump(
    source: Any, write: Callable[[bytes], Any], masker: Masker, flush: Callable[[], Any]
) -> None:
    """A pipe through the masker into `write`, until its end. A destination that fails is
    no longer written to, and the pipe is still read (the command would block on it)."""
    broken = False

    def put(data: bytes) -> None:
        nonlocal broken
        if data and not broken:
            try:
                write(data)
                flush()
            except (OSError, ValueError):
                broken = True

    try:
        while True:
            chunk = source.read1(CHUNK)
            if not chunk:
                break
            put(masker.feed(chunk))
    except (OSError, ValueError):
        pass
    finally:
        put(masker.end())


def masked_pipe(
    source: Any, write: Callable[[bytes], Any], values: dict[str, str], flush: Callable[[], Any]
) -> threading.Thread:
    """A thread that pumps `source` through a masker of `values` (bg.py's supervisor too)."""
    thread = threading.Thread(target=pump, args=(source, write, Masker(values), flush), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- AMSYOS
async def fetch(names: list[str]) -> dict[str, str]:
    """The values of `names` in the login's workspace, from the Gateway's own route (never
    through MCP). Never printed, never logged. TODO(review): credentials."""
    import httpx  # noqa: PLC0415 - loaded only to call

    from amsyos_cli import __version__  # noqa: PLC0415

    creds = credentials()
    url = creds.gateway + VALUES_PATH
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            response = await http.post(
                url,
                json={"names": names},
                headers={
                    "Authorization": f"Bearer {creds.token}",
                    "User-Agent": f"amsyos-cli/{__version__}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise CliError(
            "UNREACHABLE",
            f"cannot reach AMSYOS at {creds.gateway} ({type(exc).__name__}); check the network "
            "or AMSYOS_GATEWAY, then try again",
        ) from None
    try:
        told: Any = response.json()
    except ValueError:
        told = None
    told = obj(told)
    if response.status_code == 401:
        raise CliError(
            "NOT_LOGGED_IN",
            "the token is refused: amsyos login (a person approves it), or set a valid "
            "AMSYOS_TOKEN",
        )
    if response.status_code == 404 and "error" not in told:
        raise CliError(
            "SERVICE_NOT_FOUND", "this Gateway keeps no secrets yet: keep the key where it is"
        )
    if "error" in told:
        extra = {k: v for k, v in told.items() if k not in {"error", "hint"}}
        raise CliError(str(told["error"]), str(told.get("hint") or ""), extra)
    values = obj(told.get("values"))
    if response.status_code != 200 or any(not isinstance(values.get(n), str) for n in names):
        raise CliError(
            "BAD_ANSWER", f"AMSYOS answered {VALUES_PATH} with HTTP {response.status_code}"
        )
    return {n: str(values[n]) for n in names}


# ---------------------------------------------------------------- running a command
def run_masked(argv: list[str], env: dict[str, str], values: dict[str, str]) -> int:
    """The command with `env` added, its stdout and stderr masked; its exit code."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - the agent's own command, on its own machine
            argv, env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        sys.stderr.write(f"amsyos secret run: could not start {argv[0]}: {exc.strerror or exc}\n")
        return 127
    threads = [
        masked_pipe(proc.stdout, sys.stdout.buffer.write, values, sys.stdout.buffer.flush),
        masked_pipe(proc.stderr, sys.stderr.buffer.write, values, sys.stderr.buffer.flush),
    ]
    interrupted = 0
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:  # the command got the same Ctrl-C; a second one kills it
            interrupted += 1
            if interrupted > 1:
                with contextlib.suppress(OSError):
                    proc.kill()
    for thread in threads:
        thread.join(JOIN_S)
    return code if code >= 0 else 128 - code


# ---------------------------------------------------------------- the commands
async def cmd_secret(args: argparse.Namespace, out: Out) -> int:
    verb = args.secret_cmd
    command = {"set": cmd_set, "ls": cmd_ls, "rm": cmd_rm}
    return await command.get(verb, cmd_run)(args, out)


async def _ask(service: str, params: dict[str, Any], out: Out) -> dict[str, Any]:
    from amsyos_cli.api import Api  # noqa: PLC0415 - loaded only to call

    async with Api(credentials(), out) as api:
        return await api.instant(service, params)


def _terminal() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


async def _typed(link: str, name: str, out: Out) -> dict[str, Any]:
    """A person at this terminal types the value and their key; both go to the page's own
    POST, never anywhere else. TODO(review): credentials."""
    import httpx  # noqa: PLC0415 - loaded only to call

    creds = credentials()
    if not link.startswith(creds.gateway + "/secret/"):
        raise CliError("BAD_ANSWER", "the link is not this Gateway's: nothing was sent")
    value = getpass.getpass(f"Value of {name} (not shown): ").strip()
    key = getpass.getpass(
        "Your AMSYOS key (a manager's of the workspace or an admin's; not shown): "
    ).strip()
    if not value or not key:
        raise CliError("INVALID_INPUT", "nothing typed: nothing was stored")
    out.note("storing...")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            response = await http.post(
                link, data={"key": key, "value": value}, headers={"Accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        raise CliError("UNREACHABLE", f"cannot reach AMSYOS ({type(exc).__name__})") from None
    try:
        told = obj(response.json())
    except ValueError:
        told = {}
    if response.status_code != 200 or told.get("ok") is not True:
        hint = str(told.get("hint") or f"HTTP {response.status_code}")
        raise CliError(
            str(told.get("error") or "NOT_STORED"),
            f"{hint} Nothing was stored; to try again: amsyos secret set {name} --prompt",
        )
    return told


async def cmd_set(args: argparse.Namespace, out: Out) -> int:
    name = name_of(args.name)
    params: dict[str, Any] = {"name": name}
    if args.about:
        params["about"] = " ".join(str(args.about).split())
    told = await _ask(ASK, params, out)
    link = str(told.get("enter_url") or "")
    if not link:  # said once: an answer repeated after a lost one has none
        raise CliError("NO_LINK", f"AMSYOS gave no link: run amsyos secret set {name} again")
    workspace = str(told.get("workspace") or "the workspace")
    if args.prompt and _terminal():
        await _typed(link, name, out)
        out.emit(
            {"secret": name, "workspace": workspace, "stored": True},
            f"{name} is stored sealed in {workspace}; never shown again.",
            f"  use: amsyos secret run --env {name} -- <command>",
        )
        return 0
    answer = {k: told.get(k) for k in ("secret", "workspace", "expires", "replaces", "tell")}
    answer["enter_url"] = link
    replaces = "; it replaces the value set before" if told.get("replaces") else ""
    out.emit(
        answer,
        f"Give the person this link for {name}{replaces}. They type the value and their AMSYOS "
        "key (a manager's of the workspace or an admin's) there, within 24 hours; the link "
        "works once. Never ask for the value in the chat.",
        f"  {link}",
        f"then: amsyos secret run --env {name} -- <command>  (secret ls: is it set?)",
    )
    if args.prompt:
        raise CliError(
            "NO_TERMINAL",
            "--prompt asks a person at a terminal, and stdin is not one (an agent's shell "
            "cannot type a secret): the person opens the link above instead",
        )
    return 0


def _when(value: Any) -> str:
    return str(value or "?").replace("T", " ").removesuffix("Z")[:16]


async def cmd_ls(_args: argparse.Namespace, out: Out) -> int:
    told = await _ask(LIST, {}, out)
    rows = [obj(r) for r in seq(told.get("secrets"))]
    lines: list[str] = []
    for r in rows:
        if r.get("set"):
            state = f"set by {r.get('set_by')} {_when(r.get('set_at'))}"
        else:
            state = "not set: link open" if r.get("link_open") else "not set (amsyos secret set)"
        used = f"used {r.get('uses') or 0}x"
        if r.get("used_at"):
            used += f", last by {r.get('used_by')} {_when(r.get('used_at'))}"
        about = f"  ({short(r.get('about'), 60)})" if r.get("about") else ""
        lines.append(f"{r.get('name')}  {state}; {used}{about}")
    workspace = told.get("workspace") or "this workspace"
    out.emit(told, *(lines or [f"no secrets in {workspace}: amsyos secret set NAME"]))
    return 0


async def cmd_rm(args: argparse.Namespace, out: Out) -> int:
    name = name_of(args.name)
    told = await _ask(REMOVE, {"name": name}, out)
    out.emit(told, f"removed {name}")
    return 0


async def cmd_run(args: argparse.Namespace, _out: Out) -> int:
    argv = [str(a) for a in seq(args.argv)]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    pairs = env_specs(list(args.env or []))
    if not argv or not pairs:
        raise CliError("INVALID_INPUT", RUN_USAGE)
    names = list(dict.fromkeys(n for n, _ in pairs))
    values = await fetch(names)
    env = {var: values[name] for name, var in pairs}
    return run_masked(argv, env, values)


def add_parsers(add: Any, common: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """`amsyos secret …` (main.py's `add`)."""
    p = add(
        "secret", cmd_secret,
        "a workspace's secrets, kept out of the agent's context: set, ls, rm, run",
        "keys your scripts use, kept by AMSYOS sealed and never in your context: secret set "
        "gives a link where a person types the value (never ask for it in the chat); secret run "
        "gives the values to a command's environment and masks them in its output. " + LIMIT,
        epilog="examples:\n  amsyos secret set STRIPE_KEY --about 'Stripe test key'\n"
        "  amsyos secret ls\n  amsyos secret run --env STRIPE_KEY -- python charge.py\n"
        "  amsyos secret run --env PROD_DB=DATABASE_URL -- ./migrate.sh\n"
        "  amsyos bg start --env STRIPE_KEY -- python nightly.py\n  amsyos secret rm STRIPE_KEY",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    verbs = p.add_subparsers(dest="secret_cmd", metavar="set|ls|rm|run", required=True)
    ps = verbs.add_parser(
        "set", parents=[common], help="a link where a person types the value",
        description="asks AMSYOS for a link (24 hours, once) where a person types the value and "
        "their AMSYOS key (a manager's of the workspace or an admin's); prints it for them. "
        "A name that exists is replaced once the new value is typed",
    )  # fmt: skip
    ps.add_argument("name", metavar="NAME", help="e.g. STRIPE_KEY: A-Z, 0-9 and _")
    ps.add_argument("--about", help="what it is for, shown on the page (200 characters)")
    ps.add_argument(
        "--prompt", action="store_true",
        help="a person at this terminal types the value and their key here (not shown); "
        "refused without a terminal",
    )  # fmt: skip
    verbs.add_parser("ls", parents=[common], help="names, who set each, uses; never a value")
    pr = verbs.add_parser(
        "rm", parents=[common], help="remove one (a manager of the workspace or an admin)"
    )
    pr.add_argument("name", metavar="NAME")
    pn = verbs.add_parser(
        "run", parents=[common], help="run a command with secrets in its environment, masked",
        description=f"{RUN_USAGE}. The values are fetched from AMSYOS, added to the command's "
        "environment (as NAME, or as VAR), and each is replaced with [secret NAME] in its "
        f"output; it ends with the command's exit code. {LIMIT}",
    )  # fmt: skip
    pn.add_argument(
        "--env", action="append", metavar="NAME[=VAR]",
        help="a secret, given as NAME or as VAR; repeat for more",
    )  # fmt: skip
    pn.add_argument(  # not `command`: the top parser names the subcommand so (main.py)
        "argv", metavar="command", nargs=argparse.REMAINDER, help="-- then the command"
    )
    return {"secret": p, "secret set": ps, "secret ls": verbs.choices["ls"],
            "secret rm": pr, "secret run": pn}  # fmt: skip
