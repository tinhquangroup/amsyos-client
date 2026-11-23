"""`amsyos deploy` (spec v3 §7.2 on the tools of today and of stage B, spec v2 §21):

1. read `amsyos.yaml` and check it here: its form and its example (`INVALID_CARD`,
   `INVALID_PARAMS`, every field at once);
2. pack the folder: git's files when it is a repository, never a dot-folder, `out/`,
   `node_modules`, `__pycache__` or what `.gitignore` lists, never an agent's instruction
   file (CLAUDE.md, AGENTS.md, a top-level *.md the card does not name: v3 T2 packed
   them), and never a credentials file (refused); the files are printed, names and
   sizes, with what was left out, before anything is sent (`--dry-run` stops there;
   `card.PACK_RULES` says it to the reader);
3. upload what the workspace lacks (`asset.has`): the archive and the example's files;
4. open the organisation's space when none is open and `space.auto` would not open it;
5. `space.build` with the card as its `service`, `source` and `channel` where the
   Gateway takes them; wait for the build, then for the check of its example;
6. print `service@version`, the build and check jobs, the check, and the cost; `--close`
   gives the space back after; then how to run it: the bare id when it runs this version,
   else `service@version`, the version the bare id still runs (stable) and the promote
   command (a customer's feedback).

Exit 0: defined, its check passed or still running (said with its job); 1: a real failure,
with its hint. The version is the card's major.minor with the patch counted (one more than
the newest defined), or the card's major.minor.patch when that is higher. The same files
and card deployed again make nothing new (`--force` builds anyway).
"""

from __future__ import annotations

import asyncio
import fnmatch
import gzip
import hashlib
import io
import json
import re
import tarfile
import time
from pathlib import Path
from typing import Any

from amsyos_cli import card as cards
from amsyos_cli import repo
from amsyos_cli.api import Api, idempotency_key
from amsyos_cli.common import CliError, Credentials, Out, amount, obj, seq, short
from amsyos_cli.files import human, upload
from amsyos_cli.jobs import check_lines, follow, status_lines
from amsyos_cli.shell import cli_hint

MOST_ARCHIVE_BYTES = 200 * 1024 * 1024  # space.build: at most 200 MB
MOST_ARCHIVE_FILES = 5000
STATE = Path(".amsyos") / "state" / "deploy.json"
NEVER_DIRS = frozenset({"out", "node_modules", "__pycache__"}) | cards.SKIP_DIRS
# Files that hold credentials by their name: refused, never packed (spec v3 §12).
SECRET_NAMES = frozenset({
    "credentials.json", ".env", ".netrc", ".git-credentials", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", ".pgpass",
})  # fmt: skip
SECRET_GLOBS = ("*credentials*.json", "*.p12", "*.pfx", "*.keystore", "*.jks", ".env.*")
SECRET_OK = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})
# ... and by what they hold (small files only).
SECRET_TEXT = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    rb'|"private_key"\s*:\s*"-----BEGIN|aws_secret_access_key\s*=|_authToken\s*='
)
SECRET_SCAN_BYTES = 1024 * 1024
# An agent's instruction files, at any depth (compared lower-case): never packed, nor any
# top-level *.md but README.md and the files the card names (v3 T2: CLAUDE.md, prompt.md).
AGENT_FILES = frozenset({
    "claude.md", "claude.local.md", "agents.md", "agent.md", "gemini.md", ".cursorrules",
    ".windsurfrules", ".clinerules", "copilot-instructions.md",
})  # fmt: skip
MARKDOWN = frozenset({".md", ".markdown", ".mdx"})
CHECK_START_S = 120.0  # a check not given a job by then is left running (the sweep starts it)
SHOWN_FILES = 60
CHECKS = "checks.json"  # check job → service@version of the deploys made here, the last 20
# A candidate's vote note names its version (kernel/capability.py `note_for`).
_VOTED = re.compile(
    r'"service": "(t_[a-z0-9_]+(?:\.[a-z][a-z0-9_]*){1,4})", "version": "(\d+\.\d+\.\d+)"'
)


# ---------------------------------------------------------------- what is packed
def _ignored_by(root: Path) -> list[tuple[str, bool, bool]]:
    """`.gitignore`'s patterns of a folder that is no repository: (pattern, anchored,
    folder only). Negations are not followed: what they bring back stays out."""
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[tuple[str, bool, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        folder = line.endswith("/")
        line = line.strip("/")
        if line:
            found.append((line, raw.strip().startswith("/") or "/" in line, folder))
    return found


def _matches(rel: str, patterns: list[tuple[str, bool, bool]]) -> bool:
    parts = rel.split("/")
    for pattern, anchored, folder in patterns:
        if anchored:
            # The path or one of its folders, from the top.
            prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
            if any(
                fnmatch.fnmatchcase(p, pattern) for p in (prefixes[:-1] if folder else prefixes)
            ):
                return True
        else:
            names = parts[:-1] if folder else parts
            if any(fnmatch.fnmatchcase(n, pattern) for n in names):
                return True
    return False


def files_of(root: Path) -> list[str]:
    """What goes into the archive: the repository's files (tracked, or not ignored), or
    the folder's files less what its `.gitignore` lists; never a dot-folder or a folder of
    tools or outputs."""
    listed: list[str] | None = None
    found = repo.find(str(root))
    if found is not None and found.git:
        told = repo.git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        if told is not None:
            listed = [p for p in told.split("\0") if p]
    patterns: list[tuple[str, bool, bool]] = []
    if listed is None:
        patterns = _ignored_by(root)
        listed = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    kept: list[str] = []
    for rel in sorted(set(listed)):
        parts = Path(rel).parts
        if any(part.startswith(".") or part in NEVER_DIRS for part in parts[:-1]):
            continue
        if parts[-1].endswith((".pyc", ".part")) or (patterns and _matches(rel, patterns)):
            continue
        path = root / rel
        if path.is_file() and not path.is_symlink():
            kept.append(rel)
    return kept


def agent_files(files: list[str], card: dict[str, Any]) -> list[str]:
    """The files an agent reads for its instructions, which a service never needs: those
    names anywhere, Cursor's *.mdc, and a top-level *.md other than README.md that the
    card does not name."""
    named = cards.named_files(card)
    found: list[str] = []
    for rel in files:
        path = Path(rel)
        name = path.name.lower()
        top_md = (
            len(path.parts) == 1
            and path.suffix.lower() in MARKDOWN
            and name != "readme.md"
            and rel not in named
        )
        if name in AGENT_FILES or path.suffix.lower() == ".mdc" or top_md:
            found.append(rel)
    return found


CODE_SUFFIXES = frozenset(
    {".py", ".sh", ".js", ".mjs", ".ts", ".rb", ".go", ".rs", ".toml", ".cfg", ".json", ".txt"}
)


def example_files(root: Path, files: list[str], card: dict[str, Any]) -> list[str]:
    """A customer, 2026-11-23: the example's sample files went into the archive (879 KB for a
    video cut) though deploy uploads them as the example's inputs too. Left out: each file
    the example gives as `@path` that no code file of the archive names; one the code names
    (a fixture it reads) stays."""
    named = {
        Path(p).as_posix().removeprefix("./")
        for p in cards.local_files(card.get("example")).values()
    }
    samples = [rel for rel in files if rel in named]
    if not samples:
        return []
    code = ""
    for rel in files:
        path = root / rel
        if rel not in samples and path.suffix.lower() in CODE_SUFFIXES:
            try:
                if path.stat().st_size <= SECRET_SCAN_BYTES * 16:
                    code += path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return [rel for rel in samples if Path(rel).name not in code]


def secrets_in(root: Path, files: list[str]) -> list[str]:
    """The files that look like credentials, by name or by what a small one holds."""
    found: list[str] = []
    for rel in files:
        name = Path(rel).name
        if name in SECRET_OK:
            continue
        if name in SECRET_NAMES or any(fnmatch.fnmatchcase(name, g) for g in SECRET_GLOBS):
            found.append(rel)
            continue
        path = root / rel
        try:
            if path.stat().st_size <= SECRET_SCAN_BYTES and SECRET_TEXT.search(path.read_bytes()):
                found.append(rel)
        except OSError:
            continue
    return found


def pack(root: Path, files: list[str]) -> bytes:
    """A .tar.gz of these files at its top, the same bytes for the same files."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in files:
            path = root / rel
            info = tarfile.TarInfo(rel)
            stat = path.stat()
            info.size = stat.st_size
            info.mode = 0o755 if stat.st_mode & 0o111 else 0o644
            info.mtime = 0
            with path.open("rb") as fh:
                tar.addfile(info, fh)
    return gzip.compress(raw.getvalue(), mtime=0)


def plan(root: Path) -> dict[str, Any]:
    """Everything deploy can know here, before any call: the card checked, the files, the
    archive and its fingerprint."""
    card = cards.load(root)
    wrong = cards.problems(card)
    left = cards.example_problems(card)
    wrong += [p for p in cards.left_to_fill(card) if p not in wrong and p not in left]
    if wrong or left:
        raise CliError(
            "INVALID_CARD" if wrong else "INVALID_PARAMS",
            f"fix {cards.CARD}, then deploy again: " + "; ".join(wrong + left),
            {"fields": [p.split(":", 1)[0] for p in wrong + left]},
        )
    files = files_of(root)
    left_out = agent_files(files, card)
    files = [f for f in files if f not in left_out]
    samples = example_files(root, files, card)
    files = [f for f in files if f not in samples]
    if not files:
        raise CliError("INVALID_INPUT", f"no files to deploy in {root}")
    if len(files) > MOST_ARCHIVE_FILES:
        raise CliError(
            "INVALID_INPUT",
            f"{len(files)} files: a build takes at most {MOST_ARCHIVE_FILES}; list the rest "
            "in .gitignore",
        )
    secret = secrets_in(root, files)
    if secret:
        raise CliError(
            "CREDENTIALS_FILE",
            f"{', '.join(secret[:8])} look like credentials, which deploy never sends: list "
            "them in .gitignore or move them out of the folder, then deploy again",
            {"files": secret},
        )
    archive = pack(root, files)
    if len(archive) > MOST_ARCHIVE_BYTES:
        raise CliError(
            "INVALID_INPUT",
            f"the archive is {human(len(archive))}: a build takes at most 200 MB; fetch large "
            "files in `setup` instead (`amsyos describe space.build`, input setup)",
        )
    build = cards.build_params(card)
    fingerprint = hashlib.sha256(
        archive + json.dumps([cards.spec_of(card), build], sort_keys=True, default=str).encode()
    ).hexdigest()
    sizes = [{"file": rel, "size": (root / rel).stat().st_size} for rel in files]
    return {
        "card": card, "files": sizes, "archive": archive, "build": build,
        "fingerprint": fingerprint, "left_out": left_out, "samples": samples,
    }  # fmt: skip


def file_lines(made: dict[str, Any], *, every: bool) -> list[str]:
    sizes: list[dict[str, Any]] = made["files"]
    archive: bytes = made["archive"]
    shown = sizes if every else sizes[:SHOWN_FILES]
    lines = [
        f"packing {len(sizes)} files into a {human(len(archive))} archive "
        f"(sha256 {hashlib.sha256(archive).hexdigest()[:12]}):"
    ]
    lines += [f"  {s['file']}  {human(int(s['size']))}" for s in shown]
    if len(shown) < len(sizes):
        lines.append(f"  … {len(sizes) - len(shown)} more (amsyos deploy --dry-run lists all)")
    left_out: list[str] = made["left_out"]
    if left_out:
        lines.append(
            f"left out, an agent's instruction files: {', '.join(left_out[:20])}"
            + (f" and {len(left_out) - 20} more" if len(left_out) > 20 else "")
            + " (a top-level .md the service needs: name it in the card's include:)"
        )
    samples: list[str] = made.get("samples") or []
    if samples:
        lines.append(
            f"left out, the example's files (uploaded as its inputs, no code names them): "
            f"{', '.join(samples[:20])}"
        )
    return lines


# ---------------------------------------------------------------- versions
def next_version(asked: str, current: str | None) -> str:
    """The card's major.minor with its patch one more than the newest defined of the
    same; a card's major.minor.patch when it is above the newest."""
    nums = tuple(int(x) for x in asked.split("."))
    major, minor = nums[0], nums[1]
    floor = nums[2] if len(nums) == 3 else 0
    if current is None:
        return f"{major}.{minor}.{floor}"
    have = tuple(int(x) for x in current.split(".")[:3])
    if (major, minor) == have[:2]:
        return f"{major}.{minor}.{max(have[2] + 1, floor)}"
    if (major, minor) > have[:2]:
        return f"{major}.{minor}.{floor}"
    raise CliError(
        "INVALID_CARD",
        f"version: {asked} is below {current}, the newest defined; give {have[0]}.{have[1]} "
        "or higher in amsyos.yaml",
    )


def _read_state(root: Path) -> dict[str, Any]:
    try:
        told: Any = json.loads((root / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj(told)


def _write_state(root: Path, state: dict[str, Any]) -> None:
    folder, _ = repo.state_dir(root)
    (folder / STATE.name).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- a version's check
def remember_check(root: Path, job: str, ref: str) -> None:
    """The check job of a version deployed here, so that `status` and `result` of that job
    show its tests (the job's own answer does not say it is a check)."""
    folder, _ = repo.state_dir(root)
    path = folder / CHECKS
    try:
        told = obj(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        told = {}
    told.pop(job, None)
    told[job] = ref
    path.write_text(json.dumps(dict(list(told.items())[-20:]), indent=2) + "\n", encoding="utf-8")


def _checked_here(job: str) -> str | None:
    found = repo.find(".")
    root = found.root if found is not None else Path.cwd()
    try:
        told = obj(json.loads((root / repo.STATE_DIR / CHECKS).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    ref = told.get(job)
    return ref if isinstance(ref, str) else None


async def check_of(api: Api, job_id: str, answer: dict[str, Any]) -> dict[str, Any] | None:
    """The check of a version that this job ran, with its tests: the answer's own, else the
    check (`describe`) of the version a deploy made here or a candidate's vote note names,
    when that check's job is this one. None for any other job, with no call."""
    own = obj(answer.get("check"))
    if own:
        return own
    if seq(answer.get("tests")):
        return {"tests": answer["tests"]}
    refs: list[str] = []
    here = _checked_here(job_id)
    if here:
        refs.append(here)
    vote = answer.get("vote")
    found = _VOTED.search(vote) if isinstance(vote, str) else None
    if found and f"{found[1]}@{found[2]}" not in refs:
        refs.append(f"{found[1]}@{found[2]}")
    for ref in refs:
        try:
            described = await api.describe(ref)
        except CliError:
            continue
        check = obj(described.get("check"))
        if check.get("job") == job_id:
            return check
    return None


async def tenant_of(api: Api) -> str:
    who = await api.instant("workspace.list")
    you = obj(who.get("you"))
    tenant = you.get("organisation") or (
        you.get("id") if you.get("kind") == "organisation" else None
    )
    if not isinstance(tenant, str):
        raise CliError("BAD_ANSWER", "workspace.list did not say your organisation")
    return tenant


async def _defined(api: Api, service_id: str) -> tuple[str | None, str | None]:
    """(the newest version defined, the version the bare id runs). `describe` of a bare
    id answers the version it runs (`stable` when set), and `versions.newest` when the
    newest is another: a new version is numbered after the newest, not after `stable`
    (it was numbered after `stable`, which named a version dev already had)."""
    try:
        described = await api.describe(service_id)
    except CliError as exc:
        if exc.code == "SERVICE_NOT_FOUND":
            return None, None
        raise
    version = described.get("version")
    runs = version if isinstance(version, str) else None
    newest = obj(described.get("versions")).get("newest")
    return (newest if isinstance(newest, str) else runs), runs


def run_lines(service_id: str, version: str, runs: str | None) -> tuple[str, list[str]]:
    """How to run the version just deployed, and the lines that say it (a customer's feedback:
    1.0.2 went to dev, the run line named the bare id, which still ran stable 1.0.1, and
    the client tested 1.0.1 without knowing): `id@version` when the bare id runs another
    (`runs`), with the version it runs and the command that moves stable."""
    if runs is None or runs == version:
        return service_id, [f"run it: amsyos run {service_id} …"]
    ref = f"{service_id}@{version}"
    return ref, [
        f"run it: amsyos run {ref} …",
        f"  the bare id {service_id} still runs {runs} (its stable channel); "
        f"amsyos promote {service_id} {version} makes it run {version}",
    ]


def stable_of(data: dict[str, Any]) -> str | None:
    """The version `stable` points at, from what the define answered (kernel/offers.py
    `_move`: `channels` only once a stable is set; none, the bare id runs the newest)."""
    stable = obj(data.get("channels")).get("stable")
    return stable if isinstance(stable, str) else None


# ---------------------------------------------------------------- deploy
def gpu_for(card: dict[str, Any], gpu: str | None) -> str | None:
    """The `gpu` a space is opened with: as asked, else 'none' for a card that needs no
    GPU (a cheaper space, the owner's 2026-11-20 price), else the space's default."""
    if gpu:
        return gpu
    return "none" if str(obj(card.get("resources")).get("gpu") or "none") == "none" else None


async def space_ready(api: Api, hours: float, gpu: str | None = None) -> tuple[bool, str]:
    """Whether a build can have its space, said in one line before anything is uploaded
    (Claude Code for a customer, 2026-11-20: deploy uploaded the source and the example, then
    space.open was refused). Free: `space.show`."""
    shown = await api.instant("space.show")
    if shown.get("enabled") is False:
        why = str(shown.get("next") or "spaces are not switched on for your organisation")
        return False, f"space: {cli_hint(why)}"
    space = obj(shown.get("space"))
    if space.get("job"):
        return True, f"space: {space.get('state')} ({space.get('job')})"
    if obj(shown.get("auto")).get("on"):
        return True, "space: the build opens it by itself (space.auto is on)"
    cpu = gpu == "none" and isinstance(shown.get("cpu_price_per_hour"), int | float)
    price = shown.get("cpu_price_per_hour" if cpu else "price_per_hour")
    held = (
        f", {amount(price * hours)} credit held from your organisation's credit"
        if isinstance(price, int | float) and not isinstance(price, bool)
        else ""
    )
    kind = " with no GPU" if cpu else ""
    return True, f"space: none open; deploy opens one{kind} for {hours:g} h{held} (--hours)"


async def dry_run(api: Api | None, out: Out, root: Path, channel: str, hours: float = 1.0) -> int:
    """What deploy would send and make, with nothing sent (read-only calls when logged in)."""
    made = plan(root)
    card = made["card"]
    answer: dict[str, Any] = {
        "dry_run": True, "service": card["service"], "files": made["files"],
        "left_out": made["left_out"],
        "archive_bytes": len(made["archive"]), "fingerprint": made["fingerprint"],
        "build": made["build"], "channel": channel,
        "example_files": sorted(cards.local_files(card.get("example")).values()),
    }  # fmt: skip
    lines = file_lines(made, every=True)
    if answer["example_files"]:
        lines.append("example files uploaded too: " + ", ".join(answer["example_files"]))
    if api is not None:
        ok, said = await space_ready(api, hours, gpu_for(card, None))
        answer["space_ready"] = ok
        lines.append(said)
        service_id = f"{await tenant_of(api)}.{card['service']}"
        current, _ = await _defined(api, service_id)
        last = _read_state(root)
        same = last.get("fingerprint") == made["fingerprint"] and last.get("version") == current
        answer["service"] = service_id
        answer["would_make"] = None if same else next_version(str(card["version"]), current)
        lines.append(
            f"{service_id}@{current}: nothing changed, nothing would be built"
            if same
            else f"would build {service_id}@{answer['would_make']} (newest now: {current}), "
            f"channel {channel}"
        )
    else:
        lines.append(f"would build {card['service']} (not logged in: the version is not known)")
    lines.append("nothing was sent (--dry-run)")
    out.emit(answer, *lines)
    return 0


async def deploy(
    api: Api,
    out: Out,
    creds: Credentials,
    root: Path,
    *,
    hours: float,
    idle_minutes: int | None,
    close: bool,
    gpu: str | None,
    force: bool,
    channel: str = "dev",
) -> int:
    made = plan(root)
    card: dict[str, Any] = made["card"]
    if card.get("where", "space") == "node":
        raise CliError(
            "INVALID_CARD", "where: node is deployed with node.deploy (later); use where: space"
        )
    # Always said before anything is sent, not only with --dry-run (v3 T2).
    for line in file_lines(made, every=False):
        out.note(line)
    state_made = not (root / ".amsyos").exists()

    service_id = f"{await tenant_of(api)}.{card['service']}"
    current, runs = await _defined(api, service_id)
    last = _read_state(root)
    if (
        not force
        and current is not None
        and last.get("fingerprint") == made["fingerprint"]
        and last.get("service") == service_id
        and last.get("version") == current
    ):
        ref, said = run_lines(service_id, current, runs)
        answer = {"service": service_id, "version": current, "changed": False, "run": ref}
        if ref != service_id:
            answer["bare_runs"] = runs
        out.emit(answer, f"{service_id}@{current}: nothing changed (--force builds again)", *said)
        return 0
    version = next_version(str(card["version"]), current)
    ok, said = await space_ready(api, hours, gpu_for(card, gpu))
    if not ok:  # before anything is uploaded
        raise CliError("FORBIDDEN", said.removeprefix("space: "))

    # The example's local files, then the source: only what the workspace lacks.
    example: Any = card.get("example")
    local = cards.local_files(example)
    if local:
        missing = [p for p in local.values() if not (root / p).is_file()]
        if missing:
            raise CliError(
                "INVALID_PARAMS",
                f"example: no file {', '.join(missing)}; give a sample file's path (from the "
                f"folder of {cards.CARD})",
            )
        sent = await upload(api, [root / p for p in local.values()])
        for name, row in zip(local, sent, strict=True):
            how = "already in the workspace" if row.get("reused") else "uploaded"
            out.note(f"example {name}: {row['asset']} ({how})")
        card["example"] = {
            **obj(example),
            **{k: s["asset"] for k, s in zip(local, sent, strict=True)},
        }
    folder, _ = repo.state_dir(root)
    if state_made:  # said once, by the command that makes it (v3 T2)
        out.note(repo.STATE_NOTE)
    tmp = folder / f"{cards.image_of(card)}-{version}.tar.gz"
    tmp.write_bytes(made["archive"])
    try:
        row = (await upload(api, [tmp]))[0]
    finally:
        tmp.unlink(missing_ok=True)
    source_asset = row["asset"]
    how = "already in the workspace" if row.get("reused") else "uploaded"
    out.note(f"source: {source_asset} ({how})")

    space = await _ensure_space(api, out, card, hours=hours, idle_minutes=idle_minutes, gpu=gpu)
    space_job = space.get("job")
    # Spec v3 §7.2: the version keeps where it came from and moves its channel; the kernel
    # takes both inside the build's `service` (kernel/offers.py), not as params of their own.
    service = cards.service_of(card, version)
    source = api.source()
    if source:
        service["source"] = source
    service["channel"] = channel
    params: dict[str, Any] = {
        "name": cards.image_of(card),
        "version": version,
        "files": source_asset,
        **made["build"],
        "service": json.dumps(service, separators=(",", ":")),
    }
    takes = {"channel": "str"}  # the channel is in `service`: no promote after the check
    answer: dict[str, Any] = {
        "service": service_id, "version": version, "space_job": space_job, "space": space,
        "channel": channel, "files": made["files"], "left_out": made["left_out"],
    }  # fmt: skip
    if state_made:
        answer["made"] = ".amsyos/"
    try:
        code, lines = await _build(api, out, creds, params, answer, root)
        if code == 0 and channel == "stable" and "channel" not in takes:
            lines += await _promote(api, answer)
    finally:
        if close:  # given back whatever the build did
            await _close(api, out, answer)
    if not close or "close_error" in answer:  # still open: said, whatever the build did
        lines.append(space_line(space))
    if code == 0:
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state = {"service": service_id, "version": answer["version"], "at": at}
        _write_state(root, {"fingerprint": made["fingerprint"], **state})
    out.emit(answer, *lines)
    return code


async def _build(  # noqa: PLR0917 - the deploy's state, passed on
    api: Api,
    out: Out,
    creds: Credentials,
    params: dict[str, Any],
    answer: dict[str, Any],
    root: Path,
) -> tuple[int, list[str]]:
    """`space.build` with the card as its service, waited for; then the version's check.
    Fills `answer`; returns the exit code and the lines to print."""
    key = idempotency_key("space.build", {k: v for k, v in params.items() if k != "source"}, creds)
    began = time.monotonic()
    started = await api.run("space.build", params, key=key, wait_s=50)
    job_id = str(started["job_id"])
    answer["build_job"] = job_id
    out.note(f"building {params['name']}@{params['version']}: {job_id}")
    status = await follow(api, out, job_id, obj(started.get("status")) or None)
    took = round(time.monotonic() - began)
    if status.get("state") != "succeeded":
        answer["status"] = status
        failure = obj(status.get("failure"))
        return 1, [
            *status_lines(status),
            f"the build failed after {took}s: amsyos logs {job_id}"
            + (f"; {cli_hint(str(failure['hint']))}" if failure.get("hint") else ""),
        ]
    result = obj(status.get("result")) or await api.result(job_id)
    data = obj(result.get("data"))
    answer["built"] = data
    if data.get("service_error"):
        return 1, [
            f"built ({job_id}, {took}s), but the service was not defined: {data['service_error']}",
            f"fix {cards.CARD}, then amsyos deploy again (the image is kept)",
        ]
    made = str(data.get("service", answer["service"]))
    answer["version"] = str(data.get("version", answer["version"]))
    out.note(f"built in {took}s ({job_id}; its log: amsyos logs {job_id})")
    check = await _check(api, out, made, answer["version"])
    answer["check"] = check
    state = check.get("state") if check else None
    check_job = check.get("job") if check else None
    if isinstance(check_job, str):
        remember_check(root, check_job, f"{made}@{answer['version']}")
    lines = [
        f"{made}@{answer['version']}  build {job_id} "
        f"({took}s, cost {amount(result.get('cost', 0))})",
        f"  check: {state or 'none'}" + (f" ({check_job})" if check_job else ""),
    ]
    lines += [f"  problem: {short(p, 300)}" for p in seq(obj(check).get("problems"))]
    lines += check_lines(check, "    ")
    if state == "failed":
        lines.append(
            f"  the version is defined but its example failed: fix the code or the card, then "
            f"amsyos deploy; the check's log: amsyos logs {check_job}"
        )
        return 1, lines
    # The version just made, by a ref that names it: the bare id may run stable's.
    this = f"{made}@{answer['version']}"
    if state == "running":
        lines.append(
            f"  check running: {check_job or 'starting'}; amsyos wait {check_job} or amsyos "
            f"describe {this} says when it ends"
            if check_job
            else f"  check running (not started yet): amsyos describe {this} says when it ends"
        )
    stable = stable_of(data)
    ref, said = run_lines(made, answer["version"], stable)
    answer["run"] = ref
    if ref != made:
        answer["bare_runs"] = stable
    lines += said
    return 0, lines


async def _check(api: Api, out: Out, service_id: str, version: str) -> dict[str, Any] | None:
    """The check of the version just defined: its example run once (contract 1, §5).
    Waited for to its end; `describe` settles it once its job has ended."""
    ref = f"{service_id}@{version}"
    began = time.monotonic()
    told: dict[str, Any] = {}
    followed: set[str] = set()
    for _ in range(60):
        try:
            described = await api.describe(ref)
        except CliError:
            described = await api.describe(service_id)
        told = obj(described.get("check"))
        state = told.get("state")
        if not told or state in {"passed", "failed", "ok"}:
            return told or None
        job = told.get("job")
        if isinstance(job, str) and job not in followed:
            out.note(f"check of the example: {state} ({job})")
            await follow(api, out, job)
            followed.add(job)
            continue
        if isinstance(job, str):  # its job has ended; describe settles it in a moment
            await asyncio.sleep(2)
            continue
        if time.monotonic() - began > CHECK_START_S:
            return told
        await asyncio.sleep(3)  # its job is let in within 30 s of the build's end
    return told


async def _promote(api: Api, answer: dict[str, Any]) -> list[str]:
    """`--channel stable` on a Gateway whose space.build takes no channel."""
    asked = {"service": answer["service"], "version": answer["version"], "channel": "stable"}
    try:
        told = await api.maybe("service.promote", asked)
    except CliError as exc:
        answer["promote_error"] = exc.answer()
        return [f"  not moved to stable: {exc.code}: {cli_hint(exc.hint)}"]
    if told is None:
        return ["  no channels on this Gateway yet: the bare id runs the newest version"]
    answer["promoted"] = told
    return [f"  channel stable → {answer['version']}"]


def space_line(space: dict[str, Any]) -> str:
    """The one line a real deploy ends with about the space it used, which bills by the
    hour (a customer's feedback: the dry run said it would open one, the real deploy said
    nothing of the space it had opened). From `_ensure_space`'s answer."""
    idle = space.get("closes_after_idle_minutes")
    if isinstance(idle, int | float) and not isinstance(idle, bool) and idle > 0:
        ends = f"closes after {idle:g} idle min"
    else:
        ends = "open all its hours: no idle close"
    price = space.get("price_per_hour")
    costs = f"{amount(price)} credit an hour, " if isinstance(price, int | float) else ""
    kind = {"CPU": "a CPU space", "GPU": "a GPU space"}.get(str(space.get("class")), "a space")
    hours = space.get("hours")
    for_h = f" for {hours:g} h" if isinstance(hours, int | float) else ""
    stop = "amsyos space close stops it now"
    if space.get("opened") == "auto":
        return f"the build opened {kind} by itself (space.auto; {costs}{ends}): {stop}"
    if space.get("opened"):
        return f"opened {kind}{for_h} ({costs}{ends}): {stop}"
    return f"{kind} was open already and stays open ({costs}{ends}): {stop}"


def _price(shown: dict[str, Any], cpu: bool) -> float | None:
    """The space's price an hour as `space.show` says it: the CPU class's for a CPU one."""
    price = shown.get("cpu_price_per_hour" if cpu else "price_per_hour")
    if isinstance(price, int | float) and not isinstance(price, bool):
        return float(price)
    return None


async def _ensure_space(
    api: Api,
    out: Out,
    card: dict[str, Any],
    *,
    hours: float,
    idle_minutes: int | None,
    gpu: str | None,
) -> dict[str, Any]:
    """The space the build runs on: the open one (`opened` false), one opened now (true),
    or none yet because the build opens it by itself (`space.auto` on: `opened` "auto",
    no `job`). With its `class`, `hours`, `price_per_hour` and
    `closes_after_idle_minutes`, from the one `space.show` and what was asked."""
    shown = await api.instant("space.show")
    space = obj(shown.get("space"))
    if space.get("job"):
        out.note(f"space: {space.get('state')} ({space.get('job')})")
        cls = space.get("class")
        return {
            "job": str(space["job"]), "opened": False, "class": cls, "hours": space.get("hours"),
            "price_per_hour": _price(shown, cls == "CPU"),
            "closes_after_idle_minutes": space.get("closes_after_idle_minutes"),
        }  # fmt: skip
    auto = obj(shown.get("auto"))
    if auto.get("on"):
        out.note("space: the build opens it by itself (space.auto is on)")
        return {"job": None, "opened": "auto",
                "closes_after_idle_minutes": auto.get("idle_minutes")}  # fmt: skip
    params: dict[str, Any] = {"hours": hours}
    if idle_minutes:
        params["idle_minutes"] = idle_minutes
    gpu = gpu_for(card, gpu)
    if gpu:
        params["gpu"] = gpu
    resources = obj(card.get("resources"))
    vram = resources.get("min_vram_gb") or resources.get("vram_gb")
    if isinstance(vram, int | float) and vram >= 8:
        params["min_vram_gb"] = int(vram)
    opened = await api.run("space.open", params)
    out.note(
        f"space: opening for {hours} h"
        + (f", closing after {idle_minutes} idle minutes" if idle_minutes else "")
        + f", est. cost {amount(opened.get('est_cost'))}: {opened.get('job_id')}"
    )
    cpu = gpu == "none" and _price(shown, True) is not None  # as space_ready says it
    return {
        "job": str(opened.get("job_id")), "opened": True, "class": "CPU" if cpu else "GPU",
        "hours": hours, "price_per_hour": _price(shown, cpu),
        "closes_after_idle_minutes": idle_minutes,
    }  # fmt: skip


async def _close(api: Api, out: Out, answer: dict[str, Any]) -> None:
    try:
        closed = await api.instant("space.close")
    except CliError as exc:
        answer["close_error"] = exc.answer()
        out.note(f"space not closed: {exc.code}: {cli_hint(exc.hint)}")
        return
    answer["closed"] = closed
    told = {k: closed[k] if k == "seconds" else amount(closed[k])
            for k in ("seconds", "charged", "balance") if k in closed}  # fmt: skip
    out.note("space closed: " + (", ".join(f"{k} {v}" for k, v in told.items()) or "done"))
