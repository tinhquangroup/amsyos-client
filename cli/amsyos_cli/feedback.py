"""`amsyos feedback`: an AI client tells AMSYOS's team how a task went, itself, at the task's
end (the owner, 2026-11-20: until then a person pasted the client's report into the dev
session). `feedback.send` and `feedback.list` are core services, both free.

The CLI fills `client` (its version, the system, the host) and reads `--file`; before
sending, it refuses a text that holds this login's token or any part of the credentials
file's secret values, and never prints them. AMSYOS blanks other secret-looking strings
itself and says how many (`redacted`).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amsyos_cli import __version__
from amsyos_cli.api import Api
from amsyos_cli.common import CliError, Out, credentials, credentials_path, obj, seq, short
from amsyos_cli.login import host_of

KINDS = ("friction", "bug", "idea", "praise")
SEVERITIES = ("high", "medium", "low")
STATUSES = ("all", "new", "seen", "planned", "fixed", "declined")
TEXT_MAX = 12000  # feedback.send's `text`: a str input's most (kernel/manifest.py)
TASK_MAX = 200
JOBS_MAX = 400
CLIENT_MAX = 120
REFUSED = 2  # exit code: refused here, before anything was sent
PART = 16  # a run of this many characters of a secret counts as the secret
# What the credentials file keeps that is no secret; every other string in it is one.
NOT_SECRET = frozenset({"gateway", "expires_at", "session", "host"})
SEND_ONLY = ("kind", "severity", "job", "service", "task", "file")
LIST_ONLY = ("status", "limit", "offset")
ASK = 'amsyos feedback "<what happened; what you expected>" --job j_…'
NEAR = 5  # feedback.send's `left` at or below this: its line is printed


def _saved() -> dict[str, Any]:
    """The credentials file as it is, or nothing (never printed)."""
    try:
        return obj(json.loads(credentials_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def client_line() -> str:
    """`amsyos-cli/0.3.8 (Linux 6.8.0; host codex)`: the host the credentials file keeps,
    else the one this environment shows (`login.host_of`)."""
    saved = _saved().get("host")
    host = saved if isinstance(saved, str) and saved else host_of()[0]
    line = f"amsyos-cli/{__version__} ({platform.system()} {platform.release()}; host {host})"
    return short(line, CLIENT_MAX)


def _secrets(token: str) -> list[str]:
    """This login's token and every secret value of the credentials file."""
    found = [token]
    for key, value in _saved().items():
        if key not in NOT_SECRET and isinstance(value, str):
            found.append(value)
    return [s.strip() for s in found if len(s.strip()) >= 8]


def holds_secret(texts: list[str], secrets: list[str]) -> bool:
    """A text holds a secret whole, or any `PART` characters of it in a row."""
    for secret in secrets:
        parts = (
            [secret]
            if len(secret) <= PART
            else {secret[i : i + PART] for i in range(len(secret) - PART + 1)}
        )
        if any(part in text for part in parts for text in texts):
            return True
    return False


def _refuse(out: Out, hint: str, code: str = "INVALID_INPUT") -> int:
    out.fail(CliError(code, hint))
    return REFUSED


def _text(args: argparse.Namespace) -> str | CliError:
    """The words given, then `--file`'s text: one text, or why not."""
    given = " ".join(args.words or []).strip()
    if args.file:
        path = Path(args.file).expanduser()
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return CliError("INVALID_INPUT", f"--file {args.file}: no such file")
        except UnicodeDecodeError:
            return CliError("INVALID_INPUT", f"--file {args.file}: not UTF-8 text")
        except OSError as exc:
            return CliError("INVALID_INPUT", f"--file {args.file}: {type(exc).__name__}")
        given = "\n\n".join(t for t in (given, body.strip()) if t)
    if not given:
        return CliError("INVALID_INPUT", f"say what happened: {ASK}, or --file report.md")
    if len(given) > TEXT_MAX:
        return CliError(
            "INVALID_INPUT",
            f"the text is {len(given)} characters, at most {TEXT_MAX}: keep the command, the "
            "error and the job ids, and send again",
        )
    return given


def _jobs(given: list[str] | None) -> str:
    """`--job a --job b,c` → `a,b,c`, each once."""
    ids: list[str] = []
    for item in given or []:
        ids += [j for j in re.split(r"[\s,]+", item) if j and j not in ids]
    return ",".join(ids)


async def cmd_feedback(args: argparse.Namespace, out: Out) -> int:
    words = list(args.words or [])
    if words == ["list"]:
        wrong = [f"--{k}" for k in SEND_ONLY if getattr(args, k)]
        if wrong:
            not_here = " ".join(wrong)
            return _refuse(
                out, f"amsyos feedback list takes --status, --limit and --offset, not {not_here}"
            )
        return await _list(args, out)
    wrong = [f"--{k}" for k in LIST_ONLY if getattr(args, k) is not None]
    if wrong:
        return _refuse(out, f"{' '.join(wrong)}: only with amsyos feedback list; to send: {ASK}")
    text = _text(args)
    if isinstance(text, CliError):
        out.fail(text)
        return REFUSED
    jobs = _jobs(args.job)
    if len(jobs) > JOBS_MAX:
        return _refuse(out, f"--job: {len(jobs)} characters of ids, at most {JOBS_MAX}")
    task = " ".join(str(args.task or "").split())
    creds = credentials()
    sent = [text, task, jobs, str(args.service or "")]
    if holds_secret(sent, _secrets(creds.token)):
        return _refuse(
            out,
            "the feedback holds a secret of this login, its token or a part of one (in the "
            "text, --file, --task, --job or --service): take it out and send again. Nothing "
            "was sent, and it is not shown here",
            "SECRET_IN_TEXT",
        )
    params: dict[str, Any] = {"text": text, "client": client_line()}
    for key, value in (
        ("kind", args.kind), ("severity", args.severity), ("task", short(task, TASK_MAX)),
        ("jobs", jobs), ("service", args.service),
    ):  # fmt: skip
        if value:
            params[key] = value
    async with Api(creds, out) as api:
        told = await api.instant("feedback.send", params)
    lines = [
        f"sent {told.get('feedback')} to AMSYOS's team (status {told.get('status') or 'new'}); "
        "`amsyos feedback list` shows their reply"
    ]
    dropped = [str(j) for j in seq(told.get("dropped"))]
    if dropped:
        lines.append(f"  not jobs of your organisation, left out: {', '.join(dropped)}")
    redacted = told.get("redacted")
    if isinstance(redacted, int) and not isinstance(redacted, bool) and redacted > 0:
        lines.append(f"  AMSYOS blanked {redacted} secret-looking string(s) in it")
    left = told.get("left")  # a customer's feedback: said while one can still go
    if isinstance(left, int) and not isinstance(left, bool) and left <= NEAR:
        lines.append(f"  {told.get('hint') or f'{left} more may go in the next 24 hours'}")
    out.emit(told, *lines)
    return 0


def _one_line(value: Any, most: int) -> str:
    return short(" ".join(str(value or "").split()), most)


def _sent_line(told: dict[str, Any]) -> str | None:
    """`your organisation sent N of 100 in the last 24 hours` (`sent_24h`, `limit`; an
    older Gateway says neither: no line)."""
    sent, most = told.get("sent_24h"), told.get("limit")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (sent, most)):
        return None
    line = f"your organisation sent {sent} of {most} in the last 24 hours"
    if told.get("next_at"):
        when = str(told["next_at"])
        with contextlib.suppress(ValueError):
            at = datetime.fromisoformat(when).astimezone(UTC)
            when = f"{at:%Y-%m-%d %H:%M} UTC"
        line += f": the next one can go at {when}"
    return line


def list_lines(told: dict[str, Any], status: str | None) -> list[str]:
    """One line each, newest first, the team's reply under it; then how many of the day's
    the organisation sent."""
    items = [obj(f) for f in seq(told.get("feedback"))]
    sent = _sent_line(told)
    if not items:
        which = f" with status {status}" if status and status != "all" else ""
        return [f"no feedback{which} from this workspace yet; to send one: {ASK}"] + (
            [sent] if sent else []
        )
    lines: list[str] = []
    for f in items:
        about = f.get("task") or f.get("text")
        lines.append(
            f"{f.get('id')}  {f.get('kind')}/{f.get('severity')}  {f.get('status')}  "
            f"{str(f.get('created_at') or '')[:10]}  {_one_line(about, 100)}"
        )
        if f.get("reply"):
            lines.append(f"  AMSYOS: {_one_line(f['reply'], 1000)}")
    total, offset = told.get("total"), told.get("offset")
    start = offset if isinstance(offset, int) else 0
    if isinstance(total, int) and total > start + len(items):
        more = total - start - len(items)
        which = f" --status {status}" if status and status != "all" else ""
        lines.append(
            f"{more} more: amsyos feedback list{which} --offset {start + len(items)} --limit 50"
        )
    if sent:
        lines.append(sent)
    return lines


async def _list(args: argparse.Namespace, out: Out) -> int:
    asked: dict[str, Any] = {}
    if args.status:
        asked["status"] = args.status
    if args.limit is not None:
        asked["limit"] = args.limit
    if args.offset is not None:
        asked["offset"] = args.offset
    async with Api(credentials(), out) as api:
        told = await api.instant("feedback.list", asked)
    out.emit(told, *list_lines(told, args.status))
    return 0


def logout_line() -> str:
    return 'Tell AMSYOS\'s team how it went: amsyos feedback "<what got in your way>" --job j_…'


def replied_line(told: Any) -> str | None:
    """`whoami`'s line when AMSYOS's team replied lately to this workspace's feedback
    (`feedback.list`'s `replied_recently`, the last 7 days); nothing otherwise, nor when the
    call failed (an older Gateway without the service): the line is optional."""
    if isinstance(told, BaseException):
        return None
    count = obj(told).get("replied_recently")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return None
    return f"AMSYOS replied to {count} of your feedback: amsyos feedback list"


def add_parser(add: Any) -> None:
    """`amsyos feedback` in the parser (`main.parser`'s `add`)."""
    p = add(
        "feedback", cmd_feedback,
        "tell AMSYOS's team how a task went; feedback list: what you sent, their replies",
        "tell AMSYOS's team how a task that used AMSYOS went, once a task: what was slow,\n"
        "unclear, wrong or missing, or worked notably well. Concrete: the command, the error,\n"
        "the job ids; never a secret, a token or a file's content (a text that holds this\n"
        "login's token is refused before sending). Free.\n"
        "amsyos feedback list: what this workspace sent, newest first, with the team's replies.",
        epilog="examples:\n"
        '  amsyos feedback "run t_x.photo.edit failed with X; expected Y" --job j_1a2b --kind bug\n'
        '  amsyos feedback --file report.md --task "render a product video" --job j_1a2b,j_3c4d\n'
        "  amsyos feedback list --status fixed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )  # fmt: skip
    p.add_argument(
        "words", nargs="*", metavar="text", help="what happened; what you expected (or: list)"
    )
    p.add_argument("--kind", choices=KINDS, help="default friction")
    p.add_argument("--severity", choices=SEVERITIES, help="default medium")
    p.add_argument(
        "--job", action="append", help="a job it concerns, j_… (again, or comma-separated)"
    )
    p.add_argument("--service", help="a service it concerns")
    p.add_argument("--task", help="the task you were doing, one line")
    p.add_argument(
        "--file",
        help=f"the text from a local UTF-8 file (after the words when both are given; the "
        f"whole text at most {TEXT_MAX} characters)",
    )
    p.add_argument("--status", choices=STATUSES, help="list: only these (default all)")
    p.add_argument("--limit", type=int, help="list: how many, 1 to 50 (default 20)")
    p.add_argument("--offset", type=int, help="list: skip this many of the newest (a next page)")
