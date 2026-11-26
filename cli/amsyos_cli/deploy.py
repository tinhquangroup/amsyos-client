"""`amsyos deploy` (spec v3 §7.2 on the tools of today and of stage B, spec v2 §21):

1. read `amsyos.yaml` and check it here: its form and its example (`INVALID_CARD`,
   `INVALID_PARAMS`, every field at once);
2. pack the folder: git's files when it is a repository, never a dot-folder, `out/`,
   `node_modules`, `__pycache__` or what `.gitignore` lists, never an agent's instruction
   file (CLAUDE.md, AGENTS.md, a top-level *.md the card does not name: v3 T2 packed
   them), and never a credentials file (refused); the files are printed, names and
   sizes, with what was left out, before anything is sent (`--dry-run` stops there, and
   says the disk the build needs against what the space's machine has free: the models,
   as Hugging Face's public API sizes their files, and PyTorch for a Blackwell GPU when
   the code imports torch; `card.PACK_RULES` says it to the reader);
3. upload what the workspace lacks (`asset.has`): the archive and the example's files;
4. open the organisation's space when none is open and `node.auto` would not open it;
5. `node.build` with the card as its `service`, `source` and `channel` where the
   Gateway takes them; wait for the build, then for the check of its example;
6. print `service@version`, the build and check jobs, the check, and the cost; `--close`
   gives the space back after; then how to run it: the bare id when it runs this version,
   else `service@version`, the version the bare id still runs (stable) and the promote
   command (a customer's feedback); and the organisation's chains that still pin an older
   version of it (`behind`), each with the command that renews it (a customer, 2026-11-26).

With `where: node` (an own node, spec v3 §17; `--node n_…` when the organisation has
several), steps 4-5 are instead: build the environment and the image on that node by
`node.exec` (`nodekit/build.py`; the node at `full`, an admin), `node.deploy` the package with
the command `sh _node/run.sh <image> '<command>'`, then `service.define` with `compute: node`,
whose check runs the example there. `--dry-run` plans those steps; an own node costs nothing.
On a rented node too, the cards below the top one that name its image with `where: space` go
with it: one build, then for each its example's files, `service.define` on the image and the
check, a card at a time; `--only` some of them defines them on the image built before (no
build). With `where: node`, the cards below the top one that name its image go with it: one build,
then `node.deploy`, `service.define` and the check for each, a card at a time (the next once
the previous card's check has ended; `--only` picks some). The top card may be `where:
space` (it deploys on the rented node; `--only` its own-node cards), or missing: cards side
by side below a folder with none share its code (`--image` picks one image); from one of
their folders, deploy goes up to the highest folder with no card whose cards all name its
image (`side_top`). When only cards changed since the node's image was built, there is no
build: a card deployed there with the same command is only defined again, any other gets
`node.deploy` from that image (`.amsyos/state/node.json` keeps what is on the node).

Before anything is uploaded or built, AMSYOS's `service.check` (free) checks each card's
spec, every problem of every card in one answer.

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
import shlex
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from amsyos_cli import around, repo
from amsyos_cli import card as cards
from amsyos_cli.api import Api, idempotency_key, refused_field
from amsyos_cli.common import CliError, Credentials, Out, amount, obj, seq, short
from amsyos_cli.files import fetch_text, held, human, sha256_of, upload
from amsyos_cli.jobs import check_lines, follow, status_lines
from amsyos_cli.shell import cli_hint

MOST_ARCHIVE_BYTES = 200 * 1024 * 1024  # node.build: at most 200 MB
MOST_ARCHIVE_FILES = 5000
STATE = Path(".amsyos") / "state" / "deploy.json"
NODE_STATE = "node.json"  # an own node's: the code its image was built from, the cards there
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
# What a build takes of the machine's disk (`--dry-run`): a model, `owner/name@<40 hex>`
# maybe with the files it fetches (`:<pattern>,...`), sized as the kernel sizes it (Hugging
# Face's public API, no token); PyTorch for a Blackwell GPU (compute capability 10.0 and
# up), installed once a machine when the code imports torch (space_build.py).
HF_MODELS = "https://huggingface.co/api/models"
HF_TIMEOUT_S = 10.0
_MODEL = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95})@([0-9a-f]{40})"
    r"(?::([A-Za-z0-9_.*?/\[\],-]{1,900}))?"
)
TORCH_GB = 7.0
_USES_TORCH = re.compile(rb"^\s*(?:import|from)\s+torch\b", re.MULTILINE)
_MOST_SCANNED = 400  # Python files looked at for `import torch`, as the build does


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


def plan(root: Path, shared: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything deploy can know here, before any call: the card checked, the files, the
    archive and its fingerprint. `shared`: the card that stands for a folder with none of
    its own, whose cards below share its code (`shared_top`)."""
    card = shared if shared is not None else cards.load(root)
    wrong: list[str] = []
    left: list[str] = []
    if shared is None:  # the cards below are checked one by one (node_cards)
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
            "files in `setup` instead (`amsyos describe node.build`, input setup)",
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


def _read_state(root: Path, name: str = STATE.name) -> dict[str, Any]:
    try:
        told: Any = json.loads((root / repo.STATE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj(told)


def _write_state(root: Path, state: dict[str, Any], name: str = STATE.name) -> None:
    folder, _ = repo.state_dir(root)
    (folder / name).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _read_node_state(root: Path) -> dict[str, Any]:
    """The own node's deploys here (`node.json`); CLI 0.5.4 kept them in deploy.json."""
    told = _read_state(root, NODE_STATE)
    if told:
        return told
    before = _read_state(root)
    return before if before.get("node") else {}


# ---------------------------------------------------------------- AMSYOS's check of a card
async def check_card(
    api: Api, card: dict[str, Any], root: Path, *, compute: str | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """`service.check` of the spec the card becomes; nothing is defined (free). An example's
    local file (from `root`) goes as the workspace's own asset when it holds that content
    (`asset.has`, read-only), else as a placeholder whose problem is left out: deploy uploads
    the file first. With `compute` ('node'), the spec is checked as service.define gets it,
    else as a build's `service`. (answer, the example's files left to deploy)"""
    service = cards.for_check(card)
    local = cards.local_files(card.get("example"))
    pending: dict[str, str] = {}
    if local:
        digests = {k: sha256_of(root / p) for k, p in local.items() if (root / p).is_file()}
        have = await held(api, sorted(set(digests.values())))
        example = obj(service["spec"].get("example"))
        for name, path in local.items():
            ref = have.get(digests.get(name, ""))
            if ref:
                example[name] = ref
            else:
                pending[name] = path
    if compute:
        params = {
            "spec": json.dumps({**service["spec"], "compute": compute}),
            "name": str(service["name"]),
            "version": str(service.get("version") or ""),
        }
        params = {k: v for k, v in params.items() if v}
    else:
        params = {"spec": json.dumps(service)}
    told = await api.instant("service.check", params)
    problems = [str(p) for p in seq(told.get("problems"))]
    if pending:
        kept = [p for p in problems if cards.EXAMPLE_FILE not in p]
        if len(kept) < len(problems):
            told = {**told, "problems": kept, "ok": not kept, "left_to_deploy": pending}
    return told, pending


async def checked_problems(
    api: Api, entries: list[dict[str, Any]], compute: str | None
) -> list[str]:
    """Each card's problems that AMSYOS's `service.check` finds and were not found here,
    by card, before anything is uploaded or built (a customer, 2026-11-25: an output format
    no check reads and a description over 200 characters were found at service.define,
    after a build)."""
    found: list[str] = []
    for entry in entries:
        card: dict[str, Any] = entry["card"]
        if not cards.NAME_RE.fullmatch(str(card.get("service"))):
            continue  # its own problem, said already
        told, _ = await check_card(api, card, entry["examples"], compute=compute)
        here = cards.problems(card) + cards.example_problems(card) + cards.left_to_fill(card)
        said = cards.server_problems([str(p) for p in seq(told.get("problems"))], here)
        found += [f"{_at(entry)}{p}" for p in said]
    return found


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


MOST_BEHIND = 20  # chains behind a service printed after its define (--json has each)


def behind_lines(data: dict[str, Any], pad: str = "  ") -> list[str]:
    """A customer, 2026-11-26: the organisation's chains that still pin an older version of
    the service just defined (`behind` in what the define answered), each with the command
    that renews it: its next patch version, its steps at the versions they run now."""
    behind = [obj(b) for b in seq(data.get("behind"))]
    if not behind:
        return []
    lines = [
        f"{pad}{len(behind)} chain{'' if len(behind) == 1 else 's'} of yours still "
        f"run{'s' if len(behind) == 1 else ''} an older version of it; renew each (its next "
        "patch version, checked as any define):"
    ]
    for b in behind[:MOST_BEHIND]:
        chain = str(b.get("service") or "?")
        lines.append(
            f"{pad}  {chain}@{b.get('version')} pins {b.get('pins')}: amsyos run "
            f"service.define name={chain.split('.', 1)[-1]} renew=true"
        )
    if len(behind) > MOST_BEHIND:
        lines.append(f"{pad}  … {len(behind) - MOST_BEHIND} more (--json lists every one)")
    return lines


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


async def space_ready(
    api: Api, hours: float, gpu: str | None = None, shown: dict[str, Any] | None = None
) -> tuple[bool, str]:
    """Whether a build can have its space, said in one line before anything is uploaded
    (Claude Code for a customer, 2026-11-20: deploy uploaded the source and the example, then
    node.rent was refused). Free: `node.show` (`shown`: its answer, already asked)."""
    shown = await api.instant("node.show") if shown is None else shown
    if shown.get("enabled") is False:
        why = str(shown.get("next") or "rented nodes are not switched on for your organisation")
        return False, f"rented node: {cli_hint(why)}"
    space = obj(shown.get("space"))
    if space.get("job"):
        return True, f"rented node: {space.get('state')} ({space.get('job')})"
    if obj(shown.get("auto")).get("on"):
        return True, "rented node: the build rents one by itself (node.auto is on)"
    cpu = gpu == "none" and isinstance(shown.get("cpu_price_per_hour"), int | float)
    price = shown.get("cpu_price_per_hour" if cpu else "price_per_hour")
    held = (
        f", {amount(price * hours)} credit held from your organisation's credit"
        if isinstance(price, int | float) and not isinstance(price, bool)
        else ""
    )
    kind = " with no GPU" if cpu else ""
    return True, f"rented node: none up; deploy rents one{kind} for {hours:g} h{held} (--hours)"


def wanted(path: str, patterns: list[str]) -> bool:
    """Whether a model's file is fetched: all without patterns, else one a pattern matches
    as huggingface_hub's `allow_patterns` (fnmatchcase, '*' across '/', 'dir/' a folder)."""
    return not patterns or any(
        fnmatch.fnmatchcase(path, f"{p}*" if p.endswith("/") else p) for p in patterns
    )


async def model_sizes(models: list[str]) -> list[dict[str, Any]]:
    """Each model with the bytes of the files it fetches (`bytes` None: not known, `why`),
    from Hugging Face's public API as the kernel asks it, with no token."""
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=HF_TIMEOUT_S, follow_redirects=True) as http:
        for entry in models[:8]:
            found = _MODEL.fullmatch(entry)
            if found is None:
                rows.append({"model": entry, "bytes": None, "why": "not owner/name@<40 hex>"})
                continue
            repo, commit, patterns = (
                found[1],
                found[2],
                [p for p in (found[3] or "").split(",") if p],
            )
            row: dict[str, Any] = {"model": repo, "bytes": None}
            if patterns:
                row["only"] = patterns
            try:
                got = await http.get(
                    f"{HF_MODELS}/{repo}/revision/{commit}", params={"blobs": "true"}
                )
                siblings = seq(obj(got.json()).get("siblings")) if got.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                siblings = []
            if not siblings:
                row["why"] = "Hugging Face did not size it"
                rows.append(row)
                continue
            files = [obj(s) for s in siblings if wanted(str(obj(s).get("rfilename", "")), patterns)]
            row["bytes"] = sum(int(f.get("size") or 0) for f in files)
            if patterns and not files:
                row["why"] = "no file matches its patterns: the build is refused"
            rows.append(row)
    return rows


def uses_torch(root: Path, made: dict[str, Any]) -> bool:
    """Whether the build installs PyTorch for the GPU: the code imports torch and its
    packages name no torch of their own (space_build.py `_uses_torch`)."""
    named = str(obj(made["build"]).get("packages") or "").lower().split()
    if any(re.match(r"torch\b", p) for p in named):
        return False
    scanned = [str(f["file"]) for f in made["files"] if str(f["file"]).endswith(".py")]
    for rel in scanned[:_MOST_SCANNED]:
        try:
            if _USES_TORCH.search((root / rel).read_bytes()[:400_000]):
                return True
        except OSError:
            continue
    return False


def _torch_need(space: dict[str, Any]) -> tuple[float | None, str]:
    """PyTorch for the GPU on the space's machine: (GB, why); GB None when the GPU is not
    known yet (no space open: a Blackwell one takes it)."""
    if not space.get("job"):
        return None, f"about {TORCH_GB:g} GB if the rented GPU is a Blackwell, once a machine"
    if str(space.get("machine") or "").startswith("CPU"):
        return 0.0, "a CPU node: the machine's own"
    compute = obj(space.get("gpu")).get("compute")
    try:
        cap = float(str(compute)) if compute is not None else None
    except ValueError:
        cap = None
    if cap is None:
        return None, f"about {TORCH_GB:g} GB if the machine's GPU is a Blackwell, once a machine"
    if cap < 10.0:
        return 0.0, f"the machine's own fits its GPU, compute {compute}"
    if seq(obj(space.get("machine_disk")).get("shared_pytorch")):
        return 0.0, "on the machine already, shared by its images"
    return TORCH_GB, "installed once on this machine, then shared by its images"


async def disk_need(
    root: Path, made: dict[str, Any], shown: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """The disk a build needs (its models, PyTorch for the GPU) against what the space's
    machine has free (`node.show`), and the line that says it."""
    models = str(obj(made["build"]).get("models") or "").split()
    rows = await model_sizes(models) if models else []
    models_gb = sum(r["bytes"] or 0 for r in rows) / 1e9
    space = obj(shown.get("space"))
    torch_gb, torch_why = _torch_need(space) if uses_torch(root, made) else (0.0, "")
    need = models_gb + (torch_gb or 0.0)
    disk = obj(space.get("machine_disk"))
    free = disk.get("free_gb") if isinstance(disk.get("free_gb"), int | float) else None
    told: dict[str, Any] = {
        "need_gb": round(need, 1), "models_gb": round(models_gb, 1),
        "models": [{**r, "gb": None if r["bytes"] is None else round(r["bytes"] / 1e9, 2)}
                   for r in rows],
    }  # fmt: skip
    if torch_why:
        told["pytorch_gb"], told["pytorch"] = torch_gb, torch_why
    bits = [
        f"{r['model']} "
        + ("size not known" if r["bytes"] is None else f"{r['bytes'] / 1e9:.1f} GB")
        + (f", only {','.join(r['only'])}" if r.get("only") else "")
        + (f" ({r['why']})" if r.get("why") else "")
        for r in rows
    ]
    if torch_why:  # not counted when the GPU is not known: its line says what it takes
        bits.append(
            f"PyTorch for the GPU: {torch_why}"
            if torch_gb is None
            else f"PyTorch for the GPU {torch_gb:g} GB ({torch_why})"
        )
    line = f"disk: the build needs about {need:.1f} GB" + (f" ({'; '.join(bits)})" if bits else "")
    if free is None:
        told["fits"] = None
        line += "; the machine's free disk is not known until the rented node is up"
    else:
        told["free_gb"], told["total_gb"] = free, disk.get("total_gb")
        told["fits"] = need <= free
        line += f"; the machine has {free} GB free of {disk.get('total_gb')}"
        if need > free:
            line += (
                ": not enough. Free room with amsyos node clean (alone it lists what is on the "
                "machine), or fetch only the files the code loads (a model's ':<pattern>,...')"
            )
    if models:
        line += "; a model already on the machine takes no more"
    return told, line


async def dry_run(
    api: Api | None,
    out: Out,
    root: Path,
    channel: str,
    hours: float = 1.0,
    *,
    node: str | None = None,
    only: list[str] | None = None,
    image: str | None = None,
    force: bool = False,
) -> int:
    """What deploy would send and make, with nothing sent (read-only calls when logged in)."""
    root, only = from_top(out, root, only)
    made = plan_at(root, image, only)
    card = made["card"]
    if _on_node(card) or only:
        return await dry_run_node(api, out, root, made, channel, node, only, force=force)
    on_node = _node_line(root, made)
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
    if on_node:
        answer["node_cards"] = on_node[0]
        lines.append(on_node[1])
    if api is not None:
        wrong = await checked_problems(api, [{"dir": ".", "card": card, "examples": root}], None)
        if wrong:
            raise CliError("INVALID_CARD", "; ".join(wrong), {"problems": wrong})
        shown = await api.instant("node.show")
        ok, said = await space_ready(api, hours, gpu_for(card, None), shown=shown)
        answer["space_ready"] = ok
        lines.append(said)
        answer["disk"], said = await disk_need(root, made, shown)
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
        answer["disk"], said = await disk_need(root, made, {})
        lines.append(said)
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
    node: str | None = None,
    only: list[str] | None = None,
    image: str | None = None,
) -> int:
    root, only = from_top(out, root, only)
    made = plan_at(root, image, only)
    card: dict[str, Any] = made["card"]
    rented = not _on_node(card) and not made.get("shared")
    group, group_problems = space_cards(root, made) if rented else ([], [])
    if only and rented:
        asked = [_folder_of(p) for given in only for p in given.split(",") if p.strip()]
        on_space = [a for a in asked if a in {e["dir"] for e in group}]
        if on_space and len(on_space) < len(asked):
            raise CliError(
                "INVALID_PARAMS",
                f"--only: {', '.join(on_space)} are on the rented node (where: space), "
                f"{', '.join(a for a in asked if a not in on_space)} not: deploy them apart",
            )
        if on_space:
            chosen = [e for e in group if e["dir"] in on_space]
            return await deploy_space_cards(api, out, root, made, chosen, group_problems, channel)
    if _on_node(card) or only:
        return await deploy_node(api, out, creds, root, made, node=node, force=force,
                                 channel=channel, only=only)  # fmt: skip
    # Always said before anything is sent, not only with --dry-run (v3 T2).
    for line in file_lines(made, every=False):
        out.note(line)
    on_node = _node_line(root, made)
    if on_node:
        out.note(on_node[1])
    if group:
        out.note(
            f"{', '.join(e['dir'] for e in group)}: name the image {cards.image_of(card)} with "
            "where: space: each is defined on it after the build, its check a card at a time"
        )
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
        and last.get("node") is None
    ):
        ref, said = run_lines(service_id, current, runs)
        answer = {"service": service_id, "version": current, "changed": False, "run": ref}
        if ref != service_id:
            answer["bare_runs"] = runs
        out.emit(answer, f"{service_id}@{current}: nothing changed (--force builds again)", *said)
        return 0
    await around.warn(api, out, [service_id])  # who else works on it: a note, never a refusal
    version = next_version(str(card["version"]), current)
    entries = [{"dir": ".", "card": card, "examples": root}]
    entries += [{"dir": e["dir"], "card": e["card"], "examples": e["examples"]} for e in group]
    wrong = group_problems or await checked_problems(api, entries, None)
    if wrong:  # before anything is uploaded or rented
        raise CliError("INVALID_CARD", "; ".join(wrong), {"problems": wrong})
    ok, said = await space_ready(api, hours, gpu_for(card, gpu))
    if not ok:  # before anything is uploaded
        raise CliError("FORBIDDEN", said.removeprefix("rented node: "))

    # The example's local files, then the source: only what the workspace lacks.
    await upload_example(api, out, root, card)
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
    image_ref, built = f"{params['name']}@{version}", False
    try:
        code, lines = await _build(api, out, creds, params, answer, root)
        built = code == 0
        if code == 0 and channel == "stable" and "channel" not in takes:
            lines += await _promote(api, answer)
        if built and group:  # while the rented node is still open: their checks run there
            ok, answer["cards"], more = await define_on_image(
                api, out, root, group, image_ref, channel
            )
            lines += [f"{len(group)} more cards on the image {image_ref}:", *more]
            code = 0 if ok else 1
    finally:
        if close:  # given back whatever the build did
            await _close(api, out, answer)
    if not close or "close_error" in answer:  # still open: said, whatever the build did
        lines.append(space_line(space))
    if built:  # the image is kept: `--only` defines a card on it again without a build
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state = {"service": service_id, "version": answer["version"], "at": at}
        _write_state(root, {"fingerprint": made["fingerprint"], **state, "image": image_ref})
    out.emit(answer, *lines)
    return code


async def upload_example(
    api: Api, out: Out, root: Path, card: dict[str, Any], label: str = ""
) -> None:
    """The example's local files (`@path`, from `root`) uploaded when the workspace lacks
    them, and the card's example made to name their assets (`label`: the card's, in notes)."""
    example: Any = card.get("example")
    local = cards.local_files(example)
    if not local:
        return
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
        out.note(f"{label}example {name}: {row['asset']} ({how})")
    card["example"] = {
        **obj(example),
        **{k: s["asset"] for k, s in zip(local, sent, strict=True)},
    }


async def _build(  # noqa: PLR0917 - the deploy's state, passed on
    api: Api,
    out: Out,
    creds: Credentials,
    params: dict[str, Any],
    answer: dict[str, Any],
    root: Path,
) -> tuple[int, list[str]]:
    """`node.build` with the card as its service, waited for; then the version's check.
    Fills `answer`; returns the exit code and the lines to print."""
    key = idempotency_key("node.build", {k: v for k, v in params.items() if k != "source"}, creds)
    began = time.monotonic()
    started = await api.run("node.build", params, key=key, wait_s=50)
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
    lines += behind_lines(data)
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
    """`--channel stable` on a Gateway whose node.build takes no channel."""
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
        ends = "up all its hours: no idle close"
    price = space.get("price_per_hour")
    costs = f"{amount(price)} credit an hour, " if isinstance(price, int | float) else ""
    kind = {"CPU": "a CPU node", "GPU": "a GPU node"}.get(str(space.get("class")), "a node")
    hours = space.get("hours")
    for_h = f" for {hours:g} h" if isinstance(hours, int | float) else ""
    stop = "amsyos node close gives it back now"
    if space.get("opened") == "auto":
        return f"the build rented {kind} by itself (node.auto; {costs}{ends}): {stop}"
    if space.get("opened"):
        return f"rented {kind}{for_h} ({costs}{ends}): {stop}"
    return f"{kind} was rented already and stays up ({costs}{ends}): {stop}"


def _price(shown: dict[str, Any], cpu: bool) -> float | None:
    """The space's price an hour as `node.show` says it: the CPU class's for a CPU one."""
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
    or none yet because the build opens it by itself (`node.auto` on: `opened` "auto",
    no `job`). With its `class`, `hours`, `price_per_hour` and
    `closes_after_idle_minutes`, from the one `node.show` and what was asked."""
    shown = await api.instant("node.show")
    space = obj(shown.get("space"))
    if space.get("job"):
        out.note(f"rented node: {space.get('state')} ({space.get('job')})")
        cls = space.get("class")
        return {
            "job": str(space["job"]), "opened": False, "class": cls, "hours": space.get("hours"),
            "price_per_hour": _price(shown, cls == "CPU"),
            "closes_after_idle_minutes": space.get("closes_after_idle_minutes"),
        }  # fmt: skip
    auto = obj(shown.get("auto"))
    if auto.get("on"):
        out.note("rented node: the build rents one by itself (node.auto is on)")
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
    opened = await api.run("node.rent", params)
    out.note(
        f"rented node: starting for {hours} h"
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
        closed = await api.instant("node.close")
    except CliError as exc:
        answer["close_error"] = exc.answer()
        out.note(f"rented node not given back: {exc.code}: {cli_hint(exc.hint)}")
        return
    answer["closed"] = closed
    told = {k: closed[k] if k == "seconds" else amount(closed[k])
            for k in ("seconds", "charged", "balance") if k in closed}  # fmt: skip
    out.note(
        "rented node given back: " + (", ".join(f"{k} {v}" for k, v in told.items()) or "done")
    )


# ---------------------------------------------------------------- an own node (where: node)
NODE_KIT = Path(__file__).resolve().parent / "nodekit"  # what runs on the node (package data)
NODE_MIN = "2.1.2"  # the AMSYOS Node that gives every job the `amsyos` Python module
NODE_EXEC_S = 3600  # node.exec's longest: the environment, the models and setup
NODE_COMMAND_MOST = 900  # node.deploy's command
# What node.deploy says (node 2.1.5) when the build a command names was freed from the node.
BUILD_GONE = "is not on this machine (removed"
NODE_DIR = "_node"  # where the package carries the build and the run of an own node
NODE_EXEC = (
    f"python3 -m zipfile -e in/file1.zip src && python3 src/{NODE_DIR}/build.py in/file1.zip src"
)
NODE_UPDATE = (
    f"update AMSYOS Node on that machine to {NODE_MIN} or later: stop it, run the new program's "
    "setup again (Linux: sudo amsyos-node setup, with the same --jobs and --services as "
    "before), start it again; then amsyos deploy again"
)
DEPLOY_NODE_HELP = f"""
On an own node (the card's where: node; --node n_… when your organisation has several):
  1. node.exec builds there as the node's own account (the node at full, you an admin): a
     micromamba environment of the runtime, `system` and `packages` (PyTorch when the code
     imports torch), the card's `models`, then `setup`; under ~/amsyos-build of that
     account, one environment for every version with the same packages;
  2. node.deploy puts the package there with the card's command (and serve);
  3. service.define with compute: node; its check runs the example on the node.
A Linux node; free. `from amsyos import job` needs AMSYOS Node {NODE_MIN} or later there.
Cards that share one image: deploy in the top card's folder also takes every card below it
(services/*/amsyos.yaml, any depth) that names the same image with where: node. One build,
then steps 2-3 for each card in turn (the next once the previous card's check has ended),
with its own name, version, inputs, outputs and tests; its command runs in the image's
folder too (job.service tells them apart).
--only services/a,. deploys only those ('.' is the top card); deploy in a card's own
folder deploys that card from the top one.
A top card of where: space deploys on the rented node; --only <its own-node cards> builds
those on the own node from its folder and build keys. No card at the top, cards side by
side below it on one image (services/*/amsyos.yaml): the top folder is the image's, the
cards agree on its runtime, system, packages, models and setup; --image <name> picks one
image when they name several. From one of those cards' folders, deploy goes up to the
highest folder with no card whose cards all name its image with where: node.
No build when only cards changed since the node's image was built from these files: a
card deployed there with the same command is defined again ("define only"), another gets
node.deploy from that image; --force builds anyway (say, the image's folder was removed).
AMSYOS's service.check (free) checks every card before anything is uploaded or built.
"""
# The conda-forge packages of each runtime, pinned as a rented node pins them.
NODE_RUNTIMES: dict[str, tuple[str, ...]] = {
    "python": ("python=3.11", "pip"),
    "node": ("nodejs=24.22.3",),
    "java": ("openjdk=21.0.10", "maven=3.9.16"),
    "go": ("go=1.27.4",),
    "r": ("r-base=4.5.3", "r-jsonlite=2.0.0"),
}
# What a rented node's Python has that code imports without listing it (card.PREINSTALLED):
# put in an own node's environment when the code imports it and its packages do not name it.
NODE_BASE = {"numpy": "numpy==2.1.3", "PIL": "pillow==11.3.0", "requests": "requests==2.32.3"}
NODE_TORCH = {
    "torch": "torch==2.8.0", "torchvision": "torchvision==0.23.0",
    "torchaudio": "torchaudio==2.8.0",
}  # fmt: skip
TORCH_INDEX = {
    False: "https://download.pytorch.org/whl/cpu",
    True: "https://download.pytorch.org/whl/cu128",
}
_IMPORT = re.compile(rb"^[ \t]*(?:import|from)[ \t]+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_PIP_NAME = re.compile(r"[=<>\[ @;!~]")


def _listed(value: Any) -> list[str]:
    """A card's list (`[a, b]`) or text (`a b`)."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return str(value).split() if isinstance(value, str) else []


def _pip_name(requirement: str) -> str:
    return _PIP_NAME.split(requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _imports(root: Path, files: list[str]) -> set[str]:
    """The top-level modules the code's Python files import."""
    found: set[str] = set()
    for rel in [f for f in files if f.endswith(".py")][:_MOST_SCANNED]:
        try:
            text = (root / rel).read_bytes()[:400_000]
        except OSError:
            continue
        found |= {m.decode() for m in _IMPORT.findall(text)}
    return found


def node_build(root: Path, made: dict[str, Any], version: str, gpu: bool) -> dict[str, Any]:
    """`_node/build.json`: what nodekit/build.py makes on the node, from the card. The
    environment is named by a hash of what is in it, so a version with the same packages
    takes the one made before."""
    card: dict[str, Any] = made["card"]
    files = [str(f["file"]) for f in made["files"]]
    runtime = str(card.get("runtime", "python"))
    imports = _imports(root, files) if runtime == "python" else set[str]()
    packages = _listed(card.get("packages"))
    named = {_pip_name(p) for p in packages}
    requirements: str | None = None
    if not packages and "requirements.txt" in files:  # as on a rented node
        requirements = "requirements.txt"
        lines = (root / requirements).read_text(encoding="utf-8", errors="replace").splitlines()
        named |= {_pip_name(x) for x in lines if x.strip() and x.lstrip()[0] not in "#-"}
    if runtime == "python":
        packages += [p for m, p in NODE_BASE.items() if m in imports and _pip_name(p) not in named]
    torch: dict[str, Any] | None = None
    if imports & set(NODE_TORCH) and not any(n.startswith("torch") for n in named):
        torch = {
            "packages": [p for m, p in NODE_TORCH.items() if m == "torch" or m in imports],
            "index": TORCH_INDEX[gpu],
        }
    models = _listed(card.get("models"))
    conda = list(NODE_RUNTIMES.get(runtime, NODE_RUNTIMES["python"]))
    if runtime != "python" and (packages or requirements or models):
        conda += NODE_RUNTIMES["python"]  # pip and huggingface_hub need a Python
    conda += _listed(card.get("system"))
    held = hashlib.sha256((root / requirements).read_bytes()).hexdigest() if requirements else None
    what = {"conda": conda, "packages": packages, "requirements": held, "torch": torch}
    key = hashlib.sha256(json.dumps(what, sort_keys=True).encode()).hexdigest()[:12]
    own_module = "amsyos.py" in files or "amsyos/__init__.py" in files
    return {
        "image": cards.image_of(card), "version": version, "env": f"env-{key}", "conda": conda,
        "packages": packages, "requirements": requirements, "torch": torch, "models": models,
        "setup": str(card.get("setup") or ""),
        "needs_module": runtime == "python" and "amsyos" in imports and not own_module,
        "node_min": NODE_MIN,
    }  # fmt: skip


def node_package(root: Path, made: dict[str, Any], build: dict[str, Any]) -> bytes:
    """The package of an own node: the files deploy packs, and `_node/` (build.py, run.sh,
    amsyos-fetch, build.json); a .zip, as node.deploy takes it, the same bytes for the same
    files. node.exec builds from it, node.deploy unpacks it as the service's folder."""
    clash = [str(f["file"]) for f in made["files"] if str(f["file"]).split("/")[0] == NODE_DIR]
    if clash:
        raise CliError(
            "INVALID_INPUT",
            f"{', '.join(clash[:5])}: {NODE_DIR}/ is where deploy puts an own node's build; "
            "rename that folder",
        )
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zipped:

        def put(name: str, data: bytes, mode: int) -> None:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, data)

        for row in made["files"]:
            path = root / str(row["file"])
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            put(str(row["file"]), path.read_bytes(), mode)
        put(f"{NODE_DIR}/build.py", (NODE_KIT / "build.py").read_bytes(), 0o755)
        put(f"{NODE_DIR}/run.sh", (NODE_KIT / "run.sh").read_bytes(), 0o755)
        put(f"{NODE_DIR}/bin/amsyos-fetch", (NODE_KIT / "fetch.py").read_bytes(), 0o755)
        put(f"{NODE_DIR}/build.json", json.dumps(build, sort_keys=True, indent=1).encode(), 0o644)
    package = raw.getvalue()
    if len(package) > MOST_ARCHIVE_BYTES:
        raise CliError(
            "INVALID_INPUT",
            f"the package is {human(len(package))}: node.deploy takes at most 200 MB; fetch "
            "large files in `setup` instead (amsyos-fetch)",
        )
    return package


def node_commands(card: dict[str, Any], image_dir: str) -> dict[str, str]:
    """node.deploy's `command` (and `serve`): the card's, run by `_node/run.sh` in the
    image's folder on the node, with its environment."""
    told: dict[str, str] = {}
    for key in ("command", "serve"):
        given = str(card.get(key) or "").strip()
        if not given:
            continue
        told[key] = f"sh {NODE_DIR}/run.sh {shlex.quote(image_dir)} {shlex.quote(given)}"
        if "\n" in given or len(told[key]) > NODE_COMMAND_MOST:
            raise CliError(
                "INVALID_CARD",
                f"{key}: one line; with the image's folder it is {len(told[key])} characters, "
                f"node.deploy takes {NODE_COMMAND_MOST}: shorten it, or start a script of the "
                "folder",
            )
    return told


def node_line(node: dict[str, Any]) -> str:
    machine = obj(node.get("machine"))
    gpus = [str(g) for g in seq(machine.get("gpus"))]
    bits = [
        f"{node.get('node')} '{node.get('name') or '?'}'", str(node.get("state") or "?"),
        f"level {node.get('access') or '?'}", str(machine.get("os") or "os not known"),
    ]  # fmt: skip
    if machine.get("cpus"):
        bits.append(f"{machine['cpus']} CPUs")
    bits.append(f"GPU {', '.join(gpus[:2])}" if gpus else "no GPU")
    return ", ".join(bits)


def node_problems(
    node: dict[str, Any],
    you: dict[str, Any] | None,
    card: dict[str, Any] | None,
    need: str = "build",
) -> list[str]:
    """What keeps deploy from building on this node, each with what fixes it: online, a
    Linux machine at `full` (node.exec), an admin calling, a GPU when the card needs one.
    `need`: build; as_node (node.deploy as the node's own account: full, an admin); deploy
    (node.deploy: managed); define (service.define only)."""
    name = f"node {node.get('node')} '{node.get('name') or '?'}'"
    machine = obj(node.get("machine"))
    found: list[str] = []
    if node.get("state") != "online":
        seen = f" (last seen {node['last_seen']})" if node.get("last_seen") else ""
        found.append(f"{name} is offline{seen}: start it at that machine (amsyos-node run)")
    if need == "build" and str(machine.get("os") or "") == "windows":
        found.append(
            f"{name} runs Windows: deploy builds on a Linux node only; on Windows, zip the "
            "service yourself and node.deploy it (amsyos describe node.deploy)"
        )
    level = node.get("access")
    what = (
        "builds there with node.exec"
        if need == "build"
        else "deploys there as the node's own account (its image's folder is closed to amsyos-job)"
    )
    if need in {"build", "as_node"} and level != "full":
        found.append(
            f"{name} is at the level '{level or '?'}': deploy {what}, which needs 'full'. Only "
            "the person at that machine raises it, there: sudo amsyos-node access full"
        )
    elif need == "deploy" and level not in {"managed", "full"}:
        found.append(
            f"{name} is at the level '{level or '?'}': deploy puts each service there with "
            "node.deploy, which needs 'managed' or 'full'. Only the person at that machine "
            "raises it, there: sudo amsyos-node access managed"
        )
    admin = you is None or you.get("kind") != "person" or you.get("admin")
    if need in {"build", "as_node"} and not admin:
        found.append(
            (
                "node.exec, which builds there,"
                if need == "build"
                else "node.deploy as the node's own account"
            )
            + f" is for an admin of your organisation (or an agent of one); you "
            f"({obj(you).get('id')}) are no admin: an admin runs amsyos deploy"
        )
    needs = str(obj((card or {}).get("resources")).get("gpu") or "none")
    if needs == "required" and not seq(machine.get("gpus")):
        found.append(f"resources.gpu: required, but {name} has no GPU: --node one with a GPU")
    return found


async def own_node(api: Api, asked: str | None) -> dict[str, Any]:
    """The own node deploy builds on: `--node` (its id or name), the only one, or the only
    one ready; `node.list` says them."""
    told = await api.instant("node.list")
    own = [obj(n) for n in seq(told.get("nodes")) if str(obj(n).get("node", "")).startswith("n_")]
    listed = "; ".join(node_line(n) for n in own[:8])
    ids = {"nodes": [n.get("node") for n in own]}
    if asked:
        found = [n for n in own if asked in {n.get("node"), n.get("name")}]
        if found:
            return found[0]
        raise CliError(
            "INVALID_PARAMS",
            f"--node: '{asked}' is no own node of your organisation; "
            + (f"yours: {listed}" if own else "it has none (amsyos run node.join_code)"),
            ids,
        )
    if not own:
        raise CliError(
            "INVALID_CARD",
            "where: node, but your organisation has no own node: a computer of yours joins "
            "with `amsyos run node.join_code` (the person at it runs the command it gives); or "
            "where: space in amsyos.yaml builds on a rented node",
        )
    ready = [n for n in own if not node_problems(n, None, None)]
    if len(own) == 1 or len(ready) == 1:
        return own[0] if len(own) == 1 else ready[0]
    raise CliError(
        "INVALID_PARAMS",
        f"your organisation has {len(own)} own nodes: name one with --node <id>: {listed}",
        ids,
    )


async def who_calls(api: Api) -> tuple[str, dict[str, Any]]:
    """(the organisation, `workspace.list`'s `you`)."""
    you = obj((await api.instant("workspace.list")).get("you"))
    tenant = you.get("organisation") or (
        you.get("id") if you.get("kind") == "organisation" else None
    )
    if not isinstance(tenant, str):
        raise CliError("BAD_ANSWER", "workspace.list did not say your organisation")
    return tenant, you


def node_plan_lines(build: dict[str, Any], models_gb: float | None) -> list[str]:
    """What the build on the node puts in, in words."""
    bits = [f"conda-forge {' '.join(build['conda'])}"]
    packages: list[str] = build["packages"]
    if packages:
        bits.append(f"pip {' '.join(packages[:8])}" + (" …" if len(packages) > 8 else ""))
    if build.get("requirements"):
        bits.append(f"pip -r {build['requirements']}")
    torch = obj(build.get("torch"))
    if torch:
        kind = "CUDA" if str(torch.get("index", "")).endswith("cu128") else "the CPU"
        bits.append(f"PyTorch for {kind} ({' '.join(map(str, seq(torch.get('packages'))))})")
    lines = [f"     environment {build['env']}: {'; '.join(bits)}"]
    models: list[str] = build["models"]
    if models:
        size = "" if models_gb is None else f", about {models_gb:.1f} GB"
        lines.append(f"     models: {len(models)}{size}, into the node's Hugging Face cache")
    if build.get("setup"):
        lines.append(f"     setup: {short(str(build['setup']), 200)}")
    lines.append(
        "     under ~/amsyos-build of the node's account (AMSYOS_BUILD_HOME there moves it); an "
        "environment made before for the same packages is taken as it is"
    )
    if build.get("needs_module"):
        lines.append(f"     the code imports amsyos: AMSYOS Node {NODE_MIN} or later gives it")
    return lines


# ---------------------------------------------------------------- cards that share one image
# What makes the image, which every card on it shares: the top card's (v3 §17; a customer,
# 2026-11-24: five cards of one image, their code in the top card's folder).
BUILD_KEYS = ("runtime", "system", "packages", "models", "setup")
MOST_UP = 8  # folders above a card looked at for the card whose image it shares
# What deploy does for a card on an own node (`plan_cards`), as its answer and --dry-run say.
BUILD, DEPLOY, DEFINE, UNCHANGED = "build", "deploy", "define only", "unchanged"


def _named_image(card: dict[str, Any]) -> str | None:
    try:
        return cards.image_of(card)
    except KeyError:  # names neither an image nor a service
        return None


def _on_node(card: dict[str, Any]) -> bool:
    return card.get("where", "space") == "node"


def _unnamed(card: dict[str, Any]) -> bool:
    """A card with no `image:` line: its image is its service's name, its own (a customer,
    2026-11-25: sub-cards a former tool gave the shared image by a flag named none)."""
    named = card.get("image")
    return not (isinstance(named, str) and cards.IMAGE_RE.fullmatch(named))


def _share_hint(dirs: list[str], image: str) -> str:
    return (
        f"{', '.join(dirs[:6])}: no image: line, so each card's image is its service's name; "
        f"give {'it' if len(dirs) == 1 else 'each'} `image: {image}` to share that image"
    )


def _cards_below(
    root: Path, files: list[str]
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Each card among these files below `root`, (its folder, the card), and a note for each
    one that cannot be read."""
    found: list[tuple[str, dict[str, Any]]] = []
    notes: list[str] = []
    for rel in files:
        if not rel.endswith(f"/{cards.CARD}"):
            continue
        folder = rel.removesuffix(f"/{cards.CARD}")
        try:
            found.append((folder, cards.load(root / folder)))
        except CliError as exc:
            notes.append(f"{rel} not read ({exc.code}): left out")
    return found, notes


def image_top(root: Path) -> Path | None:
    """The folder whose code a card of an own node (`where: node`) shares: the folder above
    with a card that names the same image, whatever its `where`; else (cards side by side)
    `side_top`. None when there is none."""
    try:
        card = cards.load(root)
    except CliError:
        return None
    image = _named_image(card)
    if image is None or not _on_node(card):
        return None
    found = repo.find(str(root))
    top = found.root.resolve() if found is not None and found.git else None
    for folder in list(root.parents)[: MOST_UP if top is not None else 4]:
        if top is not None and folder != top and top not in folder.parents:
            break  # above the repository
        if not (folder / cards.CARD).is_file():
            continue
        try:
            up = cards.load(folder)
        except CliError:
            continue
        if _named_image(up) == image:
            return folder
    return side_top(root.resolve(), top, image) if top is not None else None


def side_top(here: Path, top: Path, image: str) -> Path | None:
    """Cards side by side, none above naming the image (a customer, 2026-11-25: many
    projects in one repository with no card at its top): the highest folder above `here`,
    at or below the repository's `top`, with no card, every card below which names this
    image with `where: node`, when two cards or more are below it. Never from a top card's
    folder (a card below it names its image: it climbed to the repository's top and packed
    every project). A card that cannot be read counts as another image's."""
    above: list[Path] = []
    for folder in list(here.parents)[:MOST_UP]:
        if folder != top and top not in folder.parents:
            break  # above the repository
        if (folder / cards.CARD).is_file():
            break  # another image's card (one of this image was found above)
        above.append(folder)
    if not above:
        return None
    reach = above[-1]
    listed = [
        rel.removesuffix(f"/{cards.CARD}")
        for rel in files_of(reach)
        if rel.endswith(f"/{cards.CARD}")
    ]
    found, _ = _cards_below(reach, [f"{f}/{cards.CARD}" for f in listed])
    at = here.relative_to(reach).as_posix() + "/"
    if any(f.startswith(at) and _named_image(c) == image for f, c in found):
        return None  # a top card's folder
    mine = {f for f, c in found if _on_node(c) and _named_image(c) == image}
    others = [f for f in listed if f not in mine]
    best: Path | None = None
    for folder in above:
        below = folder.relative_to(reach).as_posix()
        below = "" if below == "." else below + "/"
        if any(f.startswith(below) for f in others):
            break  # the folder holds other cards: those of another project
        best = folder
    if best is None:
        return None
    below = best.relative_to(reach).as_posix()
    below = "" if below == "." else below + "/"
    return best if sum(1 for f in mine if f.startswith(below)) >= 2 else None


def from_top(out: Out, root: Path, only: list[str] | None) -> tuple[Path, list[str] | None]:
    """A card below the folder whose code it shares is deployed from that folder: this card
    alone, unless `--only` names others."""
    top = image_top(root)
    if top is None:
        return root, only
    sub = root.relative_to(top).as_posix()
    whose = (
        f"the {cards.CARD} of {top}, whose folder holds the code"
        if (top / cards.CARD).is_file()
        else f"the cards beside it below {top}, which holds the code (no {cards.CARD} there)"
    )
    out.note(
        f"{sub}/{cards.CARD} shares its image with {whose}: deploying from there, "
        + (f"--only {','.join(only)}" if only else f"this card alone (--only {sub})")
    )
    return top, only or [sub]


def plan_at(root: Path, image: str | None, only: list[str] | None) -> dict[str, Any]:
    """`plan` of the card here; with none here, of the cards side by side below it that
    share one image on an own node (`shared_top`)."""
    if (root / cards.CARD).is_file():
        made = plan(root)
        named = _named_image(made["card"])
        if image and image != named:
            raise CliError(
                "INVALID_PARAMS",
                f"--image: the {cards.CARD} here names the image {named}; --image chooses among "
                f"cards side by side below a folder with no {cards.CARD}: leave it out",
            )
        return made
    shared, notes = shared_top(root, image, only)
    made = plan(root, shared)
    made |= {"shared": True, "notes": notes}
    return made


def shared_top(
    root: Path, image: str | None, only: list[str] | None
) -> tuple[dict[str, Any], list[str]]:
    """Cards side by side, none in `root` (a customer, 2026-11-25: services/*/amsyos.yaml on
    one image with where: node, the code at the top): `root` is the image's folder, and the
    card that stands for it holds what makes the image, on which the cards agree. The image:
    `--image`, the one `--only`'s cards name, or the only one. The notes: cards left out."""
    found, notes = _cards_below(root, files_of(root))
    if not found:
        raise CliError("NO_CARD", f"no {cards.CARD} in {root} nor below it: amsyos init writes one")
    images: dict[str, list[str]] = {}
    for folder, card in found:
        named = _named_image(card)
        if named is not None and _on_node(card):
            images.setdefault(named, []).append(folder)
    listed = "; ".join(f"{n}: {', '.join(d[:6])}" for n, d in sorted(images.items()))
    asked = {_folder_of(p) for given in only or [] for p in given.split(",") if p.strip()}
    of_asked = sorted(n for n, d in images.items() if asked & set(d))
    if image and image not in images:
        raise CliError(
            "INVALID_PARAMS",
            f"--image: no card below {root} names the image {image} with where: node; "
            + (f"theirs: {listed}" if images else "none says where: node"),
            {"images": images},
        )
    if not image and len(of_asked) > 1:
        raise CliError(
            "INVALID_PARAMS",
            f"--only: its cards name {len(of_asked)} images ({', '.join(of_asked)}); one deploy "
            f"builds one: name the cards of one image ({listed})",
            {"images": images},
        )
    chosen = image or (of_asked[0] if of_asked else None)
    if chosen is None and len(images) == 1:
        chosen = next(iter(images))
    if chosen is None:
        if not images:
            raise CliError(
                "INVALID_CARD",
                f"no {cards.CARD} in {root}, and no card below it says where: node: a rented "
                "node's card deploys from its own folder (amsyos deploy --dir <its folder>)",
            )
        alone = [f for f, c in found if _on_node(c) and _unnamed(c) and _named_image(c)]
        raise CliError(
            "INVALID_PARAMS",
            f"no {cards.CARD} in {root}, and the cards below it name {len(images)} images on an "
            f"own node: {listed}; choose one with --image <name> (or --only <folders>)"
            + (f"; {_share_hint(alone, '<one name>')}" if alone else ""),
            {"images": images},
        )
    for named, dirs in sorted(images.items()):
        if named != chosen:
            notes.append(f"{', '.join(dirs[:6])}: the image {named}: left out (--image {named})")
    for folder, card in found:
        named = _named_image(card)
        if named is None:
            notes.append(f"{folder}: names no service: left out")
        elif named != chosen and not _on_node(card):
            notes.append(
                f"{folder}: where: {card.get('where', 'space')}: left out (a rented node's card "
                "deploys from its own folder)"
            )
    mine = [(f, c) for f, c in found if f in images[chosen]]
    agreed: dict[str, Any] = {}
    differ: list[str] = []
    for key in BUILD_KEYS:
        said = {f: _build_value(key, c.get(key)) for f, c in mine if key in c or key == "runtime"}
        if len({json.dumps(v, sort_keys=True) for v in said.values()}) > 1:
            differ.append(
                f"{key}: " + ", ".join(f"{f} {short(json.dumps(v), 80)}" for f, v in said.items())
            )
        elif said:
            agreed[key] = next(iter(said.values()))
    if differ:
        raise CliError(
            "INVALID_CARD",
            f"the cards of the image {chosen} differ on what makes it, built once for them all: "
            + "; ".join(differ)
            + ": give each the same, or say it in one card only",
            {"fields": [d.split(":", 1)[0] for d in differ]},
        )
    named_files = set[str]().union(*(cards.named_files(c) for _, c in mine))
    return {"image": chosen, "where": "node", **agreed, "include": sorted(named_files)}, notes


def _node_line(root: Path, made: dict[str, Any]) -> tuple[list[str], str] | None:
    """A rented node's top card: the cards packed below it that name its image with
    `where: node`, and the line that says how they are deployed (apart, with `--only`)."""
    image = _named_image(made["card"])
    found, _ = _cards_below(root, [str(r["file"]) for r in made["files"]])
    dirs = [f for f, c in found if _on_node(c) and _named_image(c) == image]
    if not dirs:
        return None
    return dirs, (
        f"not deployed now: {', '.join(dirs[:8])} name the image {image} with where: node; "
        f"amsyos deploy --only {','.join(dirs)} builds them on your own node from this folder"
    )


def space_cards(root: Path, made: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """A rented node's top card: the cards packed below it that name its image with `where:
    space` too (a customer, 2026-11-25: only the top card was deployed, `--only` refused them), and
    every problem of theirs found here, by card. Each is defined on the image the top card's
    build makes; the image runs the top card's command for all of them."""
    top: dict[str, Any] = made["card"]
    image = cards.image_of(top)
    found, _ = _cards_below(root, [str(r["file"]) for r in made["files"]])
    group: list[dict[str, Any]] = [
        {"dir": folder, "root": root / folder, "card": card}
        for folder, card in found
        if not _on_node(card) and _named_image(card) == image
    ]
    problems: list[str] = []
    for entry in group:
        card = entry["card"]
        wrong = cards.problems(card)
        left = cards.example_problems(card)
        wrong += [p for p in cards.left_to_fill(card) if p not in wrong and p not in left]
        problems += [_at(entry) + p for p in wrong + left]
        differ = [
            k for k in (*BUILD_KEYS, "command", "serve")
            if k in card and _build_value(k, card[k]) != _build_value(k, top.get(k))
        ]  # fmt: skip
        if differ:
            problems.append(
                f"{_at(entry)}{', '.join(differ)}: the image is built once, from the top "
                f"card's, and runs its command for every card on it (dispatch on the job's "
                "service): leave them out here, or say the same"
            )
        entry["examples"], missing = _example_root(entry, root)
        if missing:
            problems.append(f"{_at(entry)}example: no file {', '.join(missing)}")
    return group, problems


async def define_on_image(  # noqa: PLR0917 - the deploy's state, passed on
    api: Api,
    out: Out,
    root: Path,
    group: list[dict[str, Any]],
    image_ref: str,
    channel: str,
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Each card of `group` defined on the image `name@version` the rented node built: its
    example's files uploaded, `service.define` with the image, its check waited for before
    the next card's (one check at a time on the node). (whether none failed, what each came
    to, the lines to print)"""
    tenant = await tenant_of(api)
    source = api.source()
    ok, told, lines = True, list[dict[str, Any]](), list[str]()
    for e in group:
        card: dict[str, Any] = e["card"]
        service_id = f"{tenant}.{card['service']}"
        entry: dict[str, Any] = {"dir": e["dir"], "service": service_id}
        told.append(entry)
        try:
            current, _ = await _defined(api, service_id)
            version = next_version(str(card["version"]), current)
            entry["version"] = version
            await upload_example(api, out, e["examples"], card, label=f"{e['dir']}: ")
            spec = cards.spec_of(card)
            spec["image"] = image_ref
            params: dict[str, Any] = {
                "name": card["service"], "version": version, "channel": channel,
                "spec": json.dumps(spec, separators=(",", ":")),
            }  # fmt: skip
            if source:
                params["source"] = json.dumps(source, separators=(",", ":"))
            defined = await api.instant("service.define", params)
        except CliError as exc:
            ok = False
            entry["error"] = exc.answer()
            hint = cli_hint(exc.hint)
            lines.append(f"{service_id} ({e['dir']}): not defined: {exc.code}: {hint}")
            continue
        out.note(f"defined {service_id}@{version} on the image {image_ref}")
        check = await _check(api, out, service_id, version)
        entry["check"] = check
        state = obj(check).get("state")
        job = obj(check).get("job")
        if isinstance(job, str):
            remember_check(root, job, f"{service_id}@{version}")
        lines.append(
            f"{service_id}@{version} ({e['dir']})  check: {state or 'none'}"
            + (f" ({job})" if isinstance(job, str) else "")
        )
        lines += [f"  problem: {short(p, 300)}" for p in seq(obj(check).get("problems"))]
        lines += check_lines(check, "    ")
        if state == "failed":
            ok = False
            lines.append(
                f"  its example failed: fix the code or the card, then amsyos deploy (or amsyos "
                f"deploy --only {e['dir']}, no build, when only its card changed); the check's "
                f"log: amsyos logs {job}"
            )
            continue
        ref, said = run_lines(service_id, version, stable_of(defined))
        entry["run"] = ref
        lines += [f"  {s}" for s in said]
        if defined.get("behind"):
            entry["behind"] = defined["behind"]
        lines += behind_lines(defined)
    return ok, told, lines


async def deploy_space_cards(  # noqa: PLR0917 - the deploy's state, passed on
    api: Api,
    out: Out,
    root: Path,
    made: dict[str, Any],
    chosen: list[dict[str, Any]],
    problems: list[str],
    channel: str,
) -> int:
    """`--only` cards of a rented node's image (`where: space`): no build, each defined on
    the image the last deploy from this folder built."""
    image = cards.image_of(made["card"])
    last = _read_state(root)
    image_ref = last.get("image")
    if not isinstance(image_ref, str) and isinstance(last.get("version"), str):
        image_ref = f"{image}@{last['version']}"  # a state written before it kept the image
    if not isinstance(image_ref, str) or not image_ref.startswith(f"{image}@"):
        raise CliError(
            "INVALID_PARAMS",
            f"--only {','.join(e['dir'] for e in chosen)}: no image {image} was built from this "
            "folder yet: amsyos deploy (without --only) builds it and defines every card on it",
        )
    mine = [p for p in problems if any(p.startswith(_at(e)) for e in chosen)]
    entries = [{"dir": e["dir"], "card": e["card"], "examples": e["examples"]} for e in chosen]
    mine += await checked_problems(api, entries, None) if not mine else []
    if mine:
        raise CliError("INVALID_CARD", "; ".join(mine), {"problems": mine})
    out.note(f"no build: defined on the image {image_ref}, built before from this folder")
    ok, told, lines = await define_on_image(api, out, root, chosen, image_ref, channel)
    answer = {"image": image_ref, "cards": told}
    out.emit(answer, f"image {image_ref} (no build): {len(chosen)} cards:", *lines)
    return 0 if ok else 1


def _folder_of(given: str) -> str:
    """A card's folder as `--only` names it, from the top card's: '.' is the top card."""
    text = given.strip().replace("\\", "/").strip("/")
    while text.startswith("./"):
        text = text[2:].lstrip("/")
    return text or "."


def _build_value(key: str, value: Any) -> Any:
    if key in {"system", "packages", "models"}:
        return _listed(value)
    return str(value or "python") if key == "runtime" else str(value or "").strip()


def _example_root(entry: dict[str, Any], top: Path) -> tuple[Path, list[str]]:
    """Where a card's example files are read from: its own folder, else the top card's when
    all are there; and those found in neither."""
    local = list(cards.local_files(entry["card"].get("example")).values())
    for folder in (entry["root"], top):
        if all((folder / p).is_file() for p in local):
            return folder, []
    return entry["root"], [p for p in local if not (entry["root"] / p).is_file()]


def _at(entry: dict[str, Any]) -> str:
    """How a problem names its card: nothing for the top one, else its card's path."""
    return "" if entry["dir"] == "." else f"{entry['dir']}/{cards.CARD}: "


def node_cards(
    root: Path, made: dict[str, Any], only: list[str] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    """The cards one build on an own node serves: the top card (folder '.') when it says
    `where: node`, and each card packed below it that names the same image with `where:
    node`; those `--only` names; the notes (cards of the image left out, keys of the image a
    card below sets); and every problem of the cards chosen, by card, as `plan` finds them
    for the top one. The group's first entry is the top card's, whatever its `where` (a
    rented node's, or one that stands for cards side by side): `on_node` says whether it is
    one of them."""
    top: dict[str, Any] = made["card"]
    image = cards.image_of(top)
    shared = bool(made.get("shared"))
    group: list[dict[str, Any]] = [
        {"dir": ".", "root": root, "card": top, "on_node": _on_node(top) and not shared}
    ]
    notes: list[str] = [str(n) for n in seq(made.get("notes"))]
    found, unread = _cards_below(root, [str(r["file"]) for r in made["files"]])
    notes += unread
    unnamed: list[str] = []  # cards on an own node that name no image: each its own
    for folder, card in found:
        if _named_image(card) != image:
            if _on_node(card) and _unnamed(card) and _named_image(card) is not None:
                unnamed.append(folder)
            continue
        if not _on_node(card):
            notes.append(
                f"{folder}: names the image {image} but where: {card.get('where', 'space')}: "
                "left out (where: node takes it)"
            )
            continue
        group.append({"dir": folder, "root": root / folder, "card": card, "on_node": True})
    if unnamed:
        notes.append(_share_hint(unnamed, image) + " (left out)")
    problems: list[str] = []
    chosen = [e for e in group if e["on_node"]]
    if only:
        asked = [_folder_of(p) for given in only for p in given.split(",") if p.strip()]
        if "." in asked and not group[0]["on_node"]:
            problems.append(
                f"--only: '.' has no {cards.CARD}"
                if shared
                else f"--only: '.' is the top card, where: {top.get('where', 'space')}: amsyos "
                "deploy without --only deploys it on the rented node"
            )
            asked = [a for a in asked if a != "."]
        known = [str(e["dir"]) for e in chosen]
        unknown = [a for a in asked if a not in known]
        if unknown:
            theirs = (
                f"its cards: {', '.join(known)}" + (" ('.' is the top one)" if "." in known else "")
                if known
                else "no card below names it"
            )
            problems.append(
                f"--only: no card of the image {image} with where: node at "
                f"{', '.join(unknown)}; {theirs}"
            )
            own = [u for u in unknown if u in unnamed]
            if own:
                problems.append(_share_hint(own, image))
        chosen = [e for e in chosen if e["dir"] in asked]
    if not chosen and not problems:
        problems.append(f"no card of the image {image} says where: node")
    names: dict[str, str] = {}
    for entry in chosen:
        card = entry["card"]
        if entry["dir"] != ".":  # the top card: `plan` checked it
            wrong = cards.problems(card)
            left = cards.example_problems(card)
            wrong += [p for p in cards.left_to_fill(card) if p not in wrong and p not in left]
            problems += [_at(entry) + p for p in wrong + left]
            differ = [
                k for k in BUILD_KEYS
                if k in card and _build_value(k, card[k]) != _build_value(k, top.get(k))
            ]  # fmt: skip
            if differ:
                notes.append(
                    f"{entry['dir']}: its {', '.join(differ)} not used: the image is built once, "
                    f"from the top {cards.CARD}; put what this card needs there"
                )
        name = str(card.get("service"))
        if name in names:
            problems.append(f"{_at(entry)}service: {name} is the service of {names[name]} too")
        names.setdefault(name, f"{entry['dir']}/{cards.CARD}")
        entry["examples"], missing = _example_root(entry, root)
        if missing:
            problems.append(
                f"{_at(entry)}example: no file {', '.join(missing)}; give a sample file's path "
                f"(from the folder of {cards.CARD})"
            )
    return group, chosen, notes, problems


async def node_versions(
    api: Api | None, tenant: str | None, top: dict[str, Any], chosen: list[dict[str, Any]]
) -> list[str]:
    """Each chosen card's `service` id, the newest version defined (`current`), the one
    the bare id runs (`runs`) and the one deploy makes (`version`), as deploy finds them for
    one card; the top card's too, which names the image's folder. Their problems."""
    problems: list[str] = []
    for entry in [top, *[e for e in chosen if e is not top]]:
        card = entry["card"]
        asked = str(card.get("version", ""))
        if not cards.VERSION_RE.fullmatch(asked) or not cards.NAME_RE.fullmatch(
            str(card.get("service"))
        ):
            continue  # the card's problem, said already (none at all: cards side by side)
        entry["version"] = cards.full_version(asked)
        if api is None or tenant is None:  # not logged in: the version is not known
            entry["service"] = card["service"]
            continue
        entry["service"] = f"{tenant}.{card['service']}"
        entry["current"], entry["runs"] = await _defined(api, entry["service"])
        try:
            entry["version"] = next_version(asked, entry["current"])
        except CliError as exc:
            if entry is not top or top in chosen:
                problems.append(_at(entry) + exc.hint)
    return problems


def image_version(top: dict[str, Any], chosen: list[dict[str, Any]], made: dict[str, Any]) -> str:
    """The version the image's folder on the node is named by: the top card's new one; when
    `--only` leaves it out, that with the files' fingerprint, so that no folder a service
    of another version runs from is replaced; with no top card (cards side by side), the
    print of the code it holds (`code_print`)."""
    if made.get("shared"):
        return str(made["code"])[:12]
    version = str(top.get("version") or cards.full_version(str(top["card"]["version"])))
    return version if top in chosen else f"{version}-{str(made['fingerprint'])[:8]}"


def _image_version_of(image_dir: str) -> str:
    """The version part of an image's folder on the node (images/<image>@<version>)."""
    return Path(image_dir).name.partition("@")[2] or "0"


def code_print(root: Path, made: dict[str, Any]) -> str:
    """What an own node's image is built from: every file packed but the cards (a card that
    changed alone needs no build), the kit deploy adds (`_node/`), the image's name and what
    makes it (the top card's BUILD_KEYS)."""
    digest = hashlib.sha256()
    for row in made["files"]:
        rel = str(row["file"])
        if Path(rel).name == cards.CARD:
            continue
        path = root / rel
        runs = "x" if path.stat().st_mode & 0o111 else "-"
        digest.update(f"{rel}\0{runs}\0".encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    for name in ("build.py", "run.sh", "fetch.py"):
        digest.update((NODE_KIT / name).read_bytes())
    top: dict[str, Any] = made["card"]
    keys = {k: _build_value(k, top.get(k)) for k in BUILD_KEYS}
    digest.update(json.dumps([cards.image_of(top), keys], sort_keys=True).encode())
    return digest.hexdigest()


def card_print(entry: dict[str, Any]) -> str:
    """What a card says, with its example's files: another print, another version."""
    card: dict[str, Any] = entry["card"]
    files: dict[str, str | None] = {}
    for name, path in cards.local_files(card.get("example")).items():
        local = Path(entry.get("examples") or entry["root"]) / path
        files[name] = sha256_of(local) if local.is_file() else None
    return hashlib.sha256(
        json.dumps([card, files], sort_keys=True, default=str).encode()
    ).hexdigest()


def plan_cards(
    last: dict[str, Any],
    made: dict[str, Any],
    node_id: str | None,
    chosen: list[dict[str, Any]],
    *,
    force: bool,
) -> bool:
    """Each chosen card's `plan` on this node, from what the last deploys here put there
    (node.json; a customer, 2026-11-25: two services defined again several times a night
    with new defaults in their cards, each a build to them). When the node's image was built
    from these very files (`code_print`): `unchanged` (deployed and defined there from this
    card, its newest version), `define only` (deployed there with this command and serve:
    service.define alone), `deploy` (node.deploy from that image, then define); else every
    card `build`. Returns whether a build is needed."""
    image_dir = last.get("image_dir")
    same_code = (
        not force
        and node_id is not None
        and last.get("node") == node_id
        and last.get("code") == made["code"]
        and isinstance(image_dir, str)
        and bool(image_dir)
    )
    before = obj(last.get("cards")) if last.get("node") == node_id else {}
    for entry in chosen:
        entry["print"] = card_print(entry)
        if not same_code:
            entry["plan"] = BUILD
            continue
        was = obj(before.get(entry["dir"]))
        try:
            commands: dict[str, str] | None = node_commands(entry["card"], str(image_dir))
        except CliError:
            commands = None  # too long: said by the commands' own check
        deployed = (
            commands is not None
            and was.get("service") == entry.get("service")
            and obj(was.get("commands")) == commands
        )
        if not deployed:
            entry["plan"] = DEPLOY
        elif (
            was.get("card") == entry["print"]
            and entry.get("current") is not None
            and was.get("version") == entry["current"]
        ):
            entry["plan"] = UNCHANGED
        else:
            entry["plan"] = DEFINE
    return not same_code


def _same_as_last(
    last: dict[str, Any], made: dict[str, Any], node_id: str | None, chosen: list[dict[str, Any]]
) -> bool:
    """CLI 0.5.4's state (no `code`): whether the last deploy here defined every chosen
    card's newest version from these very files on this node."""
    if last.get("fingerprint") != made["fingerprint"] or last.get("node") != node_id:
        return False
    before = obj(last.get("cards")) or (
        {".": {"service": last.get("service"), "version": last.get("version")}}
        if last.get("service")
        else {}
    )
    return all(
        e.get("current") is not None
        and obj(before.get(e["dir"])) == {"service": e.get("service"), "version": e["current"]}
        for e in chosen
    )


def _problem_code(problems: list[str]) -> str:
    """The code of an answer that says them all: a card's, else `--only`'s, else the node's."""
    if any(not p.startswith("--only") for p in problems):
        return "INVALID_CARD"
    return "INVALID_PARAMS" if problems else "NODE_NOT_READY"


def _gpu_card(top: dict[str, Any], chosen: list[dict[str, Any]]) -> dict[str, Any]:
    """The card node_problems weighs a GPU by: one that requires it, else the top card."""
    for entry in chosen:
        if str(obj(entry["card"].get("resources")).get("gpu") or "none") == "required":
            return entry["card"]
    return top["card"]


def _need(build_now: bool, todo: list[dict[str, Any]], last: dict[str, Any]) -> str:
    """What node_problems weighs: a build; a node.deploy from the image already there (as the
    node's own account when its folder is closed to amsyos-job); or service.define alone."""
    if build_now:
        return "build"
    if any(e["plan"] == DEPLOY for e in todo):
        return "as_node" if last.get("closed_at") else "deploy"
    return "define"


def _commands_said(commands: dict[str, str]) -> str:
    return f"command: {commands.get('command') or '(none)'}" + (
        f"; serve: {commands['serve']}" if commands.get("serve") else ""
    )


def _plan_line(entry: dict[str, Any], known: bool) -> str:
    """A card's line in --dry-run: its version and what deploy does for it."""
    if entry["plan"] == UNCHANGED:
        return f"  {entry['dir']}: {entry['service']}@{entry.get('current')}: nothing changed"
    newest = f"newest now: {entry['current']}" if entry.get("current") else "none defined yet"
    head = f"  {entry['dir']}: {entry['service']}@{entry['version']}" + (
        f" ({newest})" if known else ""
    )
    if entry["plan"] == DEFINE:
        return f"{head}: define only (no build, no node.deploy: it runs there with this command)"
    if entry["plan"] == DEPLOY:
        return (
            f"{head}: node.deploy from the image, then define; {_commands_said(entry['commands'])}"
        )
    return f"{head}, {_commands_said(entry['commands'])}"


async def dry_run_node(  # noqa: PLR0917 - the dry run's state, passed on
    api: Api | None,
    out: Out,
    root: Path,
    made: dict[str, Any],
    channel: str,
    node: str | None,
    only: list[str] | None = None,
    *,
    force: bool = False,
) -> int:
    """`--dry-run` of `where: node`: the files, the node, the build and the calls for every
    card of the image, with nothing sent (read-only calls when logged in, and AMSYOS's
    service.check of each card, which defines nothing). An own node costs nothing."""
    card: dict[str, Any] = made["card"]
    group, chosen, notes, problems = node_cards(root, made, only)
    top = group[0]
    multi = len(chosen) > 1 or top not in chosen
    made["code"] = code_print(root, made)
    examples = sorted(
        (e["examples"] / p).relative_to(root).as_posix()
        for e in chosen
        for p in cards.local_files(e["card"].get("example")).values()
    )
    answer: dict[str, Any] = {
        "dry_run": True, "where": "node", "service": card.get("service"), "files": made["files"],
        "left_out": made["left_out"], "fingerprint": made["fingerprint"], "channel": channel,
        "example_files": examples, "cost": 0,
    }  # fmt: skip
    chosen_node: dict[str, Any] | None = None
    you: dict[str, Any] | None = None
    tenant: str | None = None
    if api is not None:
        tenant, you = await who_calls(api)
        chosen_node = await own_node(api, node)
    problems += await node_versions(api, tenant, top, chosen)
    node_id = str(chosen_node.get("node")) if chosen_node is not None else None
    last = _read_node_state(root) if api is not None else {}
    build_now = plan_cards(last, made, node_id, chosen, force=force)
    if (
        api is not None
        and not force
        and "code" not in last
        and _same_as_last(last, made, node_id, chosen)
    ):
        for e in chosen:
            e["plan"] = UNCHANGED
    todo = [e for e in chosen if e["plan"] != UNCHANGED]
    ready: list[str] = []
    if chosen_node is not None and todo:
        need = _need(build_now, todo, last)
        ready = node_problems(chosen_node, you, _gpu_card(top, todo), need)
    image_dir = "" if build_now else str(last.get("image_dir") or "")
    version = image_version(top, chosen, made) if build_now else _image_version_of(image_dir)
    gpu = bool(seq(obj((chosen_node or {}).get("machine")).get("gpus")))
    build = node_build(root, made, version, gpu)
    package = node_package(root, made, build)
    long_dir = f"/home/{'x' * 24}/amsyos-build/images/{build['image']}@{version}"
    for entry in todo:
        if entry["plan"] in {BUILD, DEPLOY}:
            try:
                node_commands(entry["card"], long_dir if build_now else image_dir)
            except CliError as exc:
                problems.append(_at(entry) + exc.hint)
    if api is not None and todo:
        problems += await checked_problems(api, todo, "node")
    if problems:
        raise CliError(_problem_code(problems), "; ".join(problems), {"problems": problems})
    there = f"<the folder of {build['image']}@{version}>" if build_now else image_dir
    for entry in chosen:  # the image's folder is the node's to say
        entry["commands"] = {
            k: v.replace("IMAGE_DIR", there)
            for k, v in node_commands(entry["card"], "IMAGE_DIR").items()
        }
    first = todo[0] if todo else chosen[0]
    answer["service"] = first.get("service", answer["service"])
    if chosen_node is not None:
        answer |= {"node": node_id, "node_ready": not ready}
        if ready:
            answer["problems"] = ready
    answer["would_make"] = first.get("version") if todo else None
    answer["image"] = f"{build['image']}@{version}"
    if not build_now and image_dir:
        answer["image_dir"] = image_dir
    answer["cards"] = [
        {"dir": e["dir"], "service": e.get("service"), "newest": e.get("current"),
         "would_make": None if e["plan"] == UNCHANGED else e.get("version"), "plan": e["plan"],
         "commands": e["commands"]}
        for e in chosen
    ]  # fmt: skip
    rows = await model_sizes(build["models"]) if build["models"] and build_now else []
    known = [r["bytes"] for r in rows if r["bytes"] is not None]
    models_gb = sum(known) / 1e9 if rows and len(known) == len(rows) else None
    answer |= {
        "build": build if build_now else None, "package_bytes": len(package),
        "commands": first["commands"],
    }  # fmt: skip
    if notes:
        answer["notes"] = notes
    lines = file_lines({**made, "archive": package}, every=True)
    if examples:
        lines.append("example files uploaded too: " + ", ".join(examples))
    lines += notes
    if chosen_node is not None:
        lines.append(f"own node: {node_line(chosen_node)}")
        lines += [f"  not ready: {p}" for p in ready]
    n_cards = sum(1 for e in group if e["on_node"])
    if not todo:
        lines += [f"{e['service']}@{e['current']}: nothing changed, nothing would be built"
                  for e in chosen]  # fmt: skip
    elif chosen_node is None:
        lines.append(
            f"would deploy {', '.join(str(e['service']) for e in chosen)} on your own node (not "
            "logged in: the node and the versions are not known):"
        )
    elif not build_now:
        lines.append(
            f"would deploy {len(todo)} of the {n_cards} cards of the image {build['image']} on "
            f"it with no build: the code is as built in {image_dir} (--force builds), channel "
            f"{channel}:"
        )
    elif not multi:
        newest = f"newest now: {first['current']}" if first.get("current") else "none defined yet"
        lines.append(
            f"would deploy {first['service']}@{first['version']} on it ({newest}), channel "
            f"{channel}:"
        )
    else:
        lines.append(
            f"would deploy {len(todo)} of the {n_cards} cards of the image {build['image']} on "
            f"it, one build ({build['image']}@{version}), channel {channel}:"
        )
    if todo and (multi or not build_now):
        lines += [_plan_line(e, chosen_node is not None) for e in chosen]
    if build_now:
        steps = [
            f"  1. node.exec (an admin; the node at full), with the package as file1: {NODE_EXEC}; "
            f"up to {NODE_EXEC_S // 60} min",
            *node_plan_lines(build, models_gb),
        ]
        if multi:
            steps += [
                "  2. for each card in turn: node.deploy the package with its command (above)",
                "  3. then service.define its version with compute: node; its check runs its "
                "example on the node, and the next card goes once that check has ended",
            ]
        else:
            steps += [
                f"  2. node.deploy the package, {_commands_said(first['commands'])}",
                f"  3. service.define {first['card']['service']}@{first.get('version')} with "
                "compute: node; its check runs the example on the node",
            ]
    else:
        deploying = [str(e["dir"]) for e in todo if e["plan"] == DEPLOY]
        steps = (
            [f"  1. node.deploy the package for {', '.join(deploying)} (above)"]
            if deploying
            else []
        )
        steps.append(
            f"  {len(steps) + 1}. service.define {'each in turn' if len(todo) > 1 else 'it'} "
            "with compute: node; its check runs its example on the node"
            + (", and the next card goes once that check has ended" if len(todo) > 1 else "")
        )
    if todo:
        lines += steps
    lines += ["cost: none (an own node is free)", "nothing was sent (--dry-run)"]
    out.emit(answer, *lines)
    return 0


async def deploy_node(
    api: Api,
    out: Out,
    creds: Credentials,
    root: Path,
    made: dict[str, Any],
    *,
    node: str | None,
    force: bool,
    channel: str,
    only: list[str] | None = None,
) -> int:
    """`where: node`: build on the organisation's own node by node.exec, then for each card
    of the image node.deploy the package there and service.define it with `compute: node`;
    each check runs there. When the node's image was built from these files already, no
    build: a card changed is defined again, or deployed from that image first (`plan_cards`).
    One card's failure after the build is said with the others'."""
    group, chosen, notes, problems = node_cards(root, made, only)
    top = group[0]
    multi = len(chosen) > 1 or top not in chosen
    made["code"] = code_print(root, made)
    tenant, you = await who_calls(api)
    chosen_node = await own_node(api, node)
    node_id = str(chosen_node.get("node"))
    problems += await node_versions(api, tenant, top, chosen)
    last = _read_node_state(root)
    build_now = plan_cards(last, made, node_id, chosen, force=force)
    if not force and "code" not in last and _same_as_last(last, made, node_id, chosen):
        for e in chosen:
            e["plan"] = UNCHANGED
    todo = [e for e in chosen if e["plan"] != UNCHANGED]
    if not todo and not problems:
        said_cards: list[dict[str, Any]] = []
        lines: list[str] = []
        for e in chosen:
            ref, said = run_lines(e["service"], e["current"], e["runs"])
            said_cards.append(
                {"dir": e["dir"], "service": e["service"], "version": e["current"], "run": ref}
            )
            lines += [f"{e['service']}@{e['current']}: nothing changed (--force builds again)",
                      *said]  # fmt: skip
        answer = {**said_cards[0], "changed": False}
        if multi:
            answer["cards"] = said_cards
        out.emit(answer, *lines)
        return 0
    ready = node_problems(chosen_node, you, _gpu_card(top, todo), _need(build_now, todo, last))
    image_dir = "" if build_now else str(last["image_dir"])
    version = image_version(top, chosen, made) if build_now else _image_version_of(image_dir)
    gpu = bool(seq(obj(chosen_node.get("machine")).get("gpus")))
    build = node_build(root, made, version, gpu)
    deploying = [e for e in todo if e["plan"] in {BUILD, DEPLOY}]
    package = node_package(root, made, build) if deploying else b""
    # A command too long for node.deploy is said before the build: the image's folder as a
    # long home folder would put it.
    long_dir = f"/home/{'x' * 24}/amsyos-build/images/{build['image']}@{version}"
    for entry in deploying:
        try:
            node_commands(entry["card"], long_dir if build_now else image_dir)
        except CliError as exc:
            problems.append(_at(entry) + exc.hint)
    # AMSYOS's check of every card, before anything is uploaded or built (free).
    problems += await checked_problems(api, todo, "node")
    if problems or ready:  # every one at once, before anything is uploaded
        raise CliError(
            _problem_code(problems), "; ".join(problems + ready),
            {"node": node_id, "problems": problems + ready},
        )  # fmt: skip
    await around.warn(api, out, [str(e["service"]) for e in todo])  # a note, never a refusal
    if deploying:
        for line in file_lines({**made, "archive": package}, every=False):
            out.note(line)
    for line in notes:
        out.note(line)
    image = f"{build['image']}@{version}"
    n_cards = sum(1 for e in group if e["on_node"])
    if not build_now:
        out.note(
            f"no build: the code is as built on node {node_id} in {image_dir} (--force builds); "
            + ", ".join(f"{e['dir']}: {e['plan']}" for e in todo)
        )
    elif multi:
        out.note(
            f"image {image}: {len(todo)} of its {n_cards} cards: "
            + ", ".join(str(e["dir"]) for e in todo)
        )
    state_made = not (root / ".amsyos").exists()
    for e in todo:
        await upload_example(api, out, e["examples"], e["card"], f"{e['dir']}: " if multi else "")
    folder, _ = repo.state_dir(root)
    if state_made:
        out.note(repo.STATE_NOTE)
    asset = ""
    if deploying:
        tmp = folder / f"{build['image']}-{version}.zip"
        tmp.write_bytes(package)
        try:
            row = (await upload(api, [tmp]))[0]
        finally:
            tmp.unlink(missing_ok=True)
        asset = str(row["asset"])
        how = "already in the workspace" if row.get("reused") else "uploaded"
        out.note(f"package: {asset} ({how})")
    first = todo[0]
    answer: dict[str, Any] = {
        "service": first["service"], "version": first["version"], "node": node_id,
        "channel": channel, "image": image, "files": made["files"],
        "left_out": made["left_out"], "cost": 0,
    }  # fmt: skip
    if asset:
        answer["package"] = asset
    if not build_now:
        answer |= {"build": None, "image_dir": image_dir}
    if notes:
        answer["notes"] = notes
    if state_made:
        answer["made"] = ".amsyos/"
    began = time.monotonic()
    if build_now:
        made_on_node, lines = await _node_exec(
            api, out, creds, node_id, asset, answer, str(build["env"])
        )
        if made_on_node is None:
            out.emit(answer, *lines)
            return 1
        built: dict[str, Any] = made_on_node
        closed = built.get("closed_at")
        if closed:
            out.note(
                f"its jobs run as the node's own account: {closed} is closed to the job account "
                "amsyos-job (a job of an order there would fail); open it (chmod o+x) or set "
                "AMSYOS_BUILD_HOME for the node, then deploy again, to have them run as amsyos-job"
            )
    else:
        built = {"image_dir": image_dir, "closed_at": last.get("closed_at")}
    image_dir = str(built.get("image_dir"))
    source = api.source()
    for e in todo:
        try:
            if e["plan"] in {BUILD, DEPLOY}:
                commands = node_commands(e["card"], image_dir)
                e["deployed"] = await _node_deploy(api, out, node_id, e["service"], asset,
                                                   commands, built)  # fmt: skip
                e["commands"] = commands
            spec = cards.spec_of(e["card"])
            spec["compute"] = "node"
            params: dict[str, Any] = {
                "name": e["card"]["service"], "version": e["version"], "channel": channel,
                "spec": json.dumps(spec, separators=(",", ":")),
            }  # fmt: skip
            if source:
                params["source"] = json.dumps(source, separators=(",", ":"))
            e["defined"] = await api.instant("service.define", params)
        except CliError as exc:
            if not build_now and e["plan"] == DEPLOY and BUILD_GONE in exc.hint:
                # Node 2.1.5 frees a build no deployment uses (an hour after it was made):
                # nothing was deployed from it yet in this run, so build it again.
                out.note(
                    f"{image_dir} is no longer on node {node_id} (no deployment used it): "
                    "building again"
                )
                return await deploy_node(
                    api, out, creds, root, made, node=node, force=True, channel=channel, only=only
                )
            e["error"] = exc.answer()
            if not multi:
                e["ok"] = False
                _remember_cards(root, last, made, node_id, chosen, built if build_now else None)
                raise
            out.note(f"{e['service']}@{e['version']}: not deployed: {exc.code}: {exc.hint}")
            continue
        out.note(
            f"defined {e['service']}@{e['version']} (compute: node"
            + ("; define only)" if e["plan"] == DEFINE else ")")
        )
        # service.define starts the check on the node: the next card is defined once this
        # one's has ended (a customer, 2026-11-25: three checks at once each loaded one
        # 1.2 GB model and the node ran out of memory).
        e["check"] = await _check(api, out, e["service"], e["version"])
        check_job = obj(e["check"]).get("job")
        if isinstance(check_job, str):
            remember_check(root, check_job, f"{e['service']}@{e['version']}")
    took = round(time.monotonic() - began)
    on = f"on node {node_id} '{chosen_node.get('name') or '?'}'"
    if build_now:
        how = f"build {answer['build_job']}"
    elif not multi and first["plan"] == DEFINE:
        how = "define only: no build, no node.deploy"
    else:
        how = f"no build: the image built before, {image_dir}"
    if multi:
        lines = [f"image {image} {on}: {how}, {took}s in all, cost none; {len(chosen)} cards:"]
    else:
        lines = [f"{first['service']}@{first['version']}  {on} ({how}, {took}s in all, cost none)"]
    too_old = False
    for e in chosen:
        if e["plan"] == UNCHANGED:
            lines.append(f"{e['service']}@{e['current']} ({e['dir']}): nothing changed")
            continue
        if multi:
            lines.append(
                f"{e['service']}@{e['version']} ({e['dir']})"
                + (f": {e['plan']}" if e["plan"] in {DEFINE, DEPLOY} else "")
            )
        e["ok"], said, old = _card_lines(e, multi=multi)
        lines += said
        too_old = too_old or old
    if too_old:
        lines.append(f"  {NODE_UPDATE}")
    answer["cards"] = [
        {
            k: e[k]
            for k in ("dir", "service", "version", "plan", "check", "run", "behind", "error")
            if k in e
        }
        | ({"version": e.get("current")} if e["plan"] == UNCHANGED else {})
        for e in chosen
    ]
    if not multi:
        answer |= {k: first[k] for k in ("deployed", "defined", "check", "run") if k in first}
    _remember_cards(root, last, made, node_id, chosen, built if build_now else None)
    out.emit(answer, *lines)
    return 0 if all(e["ok"] for e in todo) else 1


def _card_lines(entry: dict[str, Any], *, multi: bool) -> tuple[bool, list[str], bool]:
    """What a card's deploy came to after the build: (whether it is defined with no failed
    check, its lines, whether its check found no `amsyos` module)."""
    pad = "  " if multi else ""
    if "error" in entry:
        err = obj(entry["error"])
        said = f"  not deployed: {err.get('error')}: {short(str(err.get('hint')), 400)}"
        return False, [said], False
    check = obj(entry.get("check"))
    state, job = check.get("state"), check.get("job")
    lines = [f"  check: {state or 'none'}" + (f" ({job})" if job else "")]
    lines += [f"  problem: {short(p, 300)}" for p in seq(check.get("problems"))]
    lines += check_lines(check, "    ")
    too_old = "No module named 'amsyos'" in str(check.get("problems") or "") + str(
        check.get("hint") or ""
    )
    ref = f"{entry['service']}@{entry['version']}"
    if state == "failed":
        lines.append(
            "  the version is defined but its example failed: fix the code or the card, then "
            f"amsyos deploy; the check's log: amsyos logs {job}"
        )
        return False, lines, too_old
    if state == "running":
        lines.append(f"  check running: amsyos describe {ref} says when it ends")
    defined = obj(entry.get("defined"))
    run, said = run_lines(entry["service"], entry["version"], stable_of(defined))
    entry["run"] = run
    if defined.get("behind"):
        entry["behind"] = defined["behind"]
    return True, lines + [pad + s for s in said] + behind_lines(defined, pad + "  "), too_old


def _remember_cards(  # noqa: PLR0917 - the deploy's state, passed on
    root: Path, last: dict[str, Any], made: dict[str, Any], node_id: str,
    chosen: list[dict[str, Any]], built: dict[str, Any] | None,
) -> None:  # fmt: skip
    """What `plan_cards` reads next time (node.json): the code the node's image was built
    from (`built`: by this deploy) and its folder, and by card folder the service deployed
    there with its commands, the version defined and the card's print. A card node.deploy
    put there whose version did not pass keeps no print: it is defined again next time; one
    whose node.deploy did not happen in a build is forgotten (deployed again)."""
    same = last.get("node") == node_id and "code" in last
    kept = {str(k): obj(v) for k, v in obj(last.get("cards")).items()} if same else {}
    for e in chosen:
        if e["plan"] == UNCHANGED:
            continue
        was = kept.get(str(e["dir"]), {})
        if e.get("deployed"):
            was = {"service": e["service"], "commands": e["commands"]}
        elif e["plan"] != DEFINE:
            was = {}  # what the node runs for it is not known
        if was and e.get("ok"):
            was = {**was, "version": e["version"], "card": e["print"]}
        if was:
            kept[str(e["dir"])] = was
        else:
            kept.pop(str(e["dir"]), None)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state: dict[str, Any] = {"node": node_id, "fingerprint": made["fingerprint"], "at": at}
    if built is not None:
        state |= {
            "code": made["code"], "image_dir": built.get("image_dir"),
            "closed_at": built.get("closed_at"),
        }  # fmt: skip
    elif same:
        state |= {k: last.get(k) for k in ("code", "image_dir", "closed_at")}
    _write_state(root, {**state, "cards": kept}, NODE_STATE)


async def _node_exec(  # noqa: PLR0917 - the deploy's state, passed on
    api: Api,
    out: Out,
    creds: Credentials,
    node_id: str,
    asset: str,
    answer: dict[str, Any],
    env: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The build on the node (node.exec of `_node/build.py`), followed to its end: what it
    made (`out/build.json`), or None and the lines that say why not. `cache_env` names the
    environment it builds: the kernel hands the build its organisation's cached copies
    (the environment, and the image by the package) and where to keep what it makes."""
    params: dict[str, Any] = {
        "node": node_id, "command": NODE_EXEC, "timeout_s": NODE_EXEC_S, "file1": asset,
        "outputs": "build", "cache_env": env,
    }  # fmt: skip
    began = time.monotonic()
    started = await api.run(
        "node.exec", params, key=idempotency_key("node.exec", params, creds), wait_s=50
    )
    job_id = str(started["job_id"])
    answer["build_job"] = job_id
    out.note(f"building on node {node_id}: {job_id} (environment, models, setup)")
    status = await follow(api, out, job_id, obj(started.get("status")) or None)
    took = round(time.monotonic() - began)
    if status.get("state") != "succeeded":
        answer["status"] = status
        return None, [
            *status_lines(status),
            f"the build on node {node_id} failed after {took}s: amsyos logs {job_id}",
        ]
    result = obj(status.get("result")) or await api.result(job_id)
    url = obj(result.get("outputs")).get("build")
    try:
        built = obj(json.loads(await fetch_text(url))) if isinstance(url, str) else {}
    except (CliError, ValueError):
        built = {}
    answer["built"] = built
    if not built.get("ok"):
        error = str(built.get("error") or "BUILD_FAILED")
        hint = str(built.get("hint") or "it wrote no out/build.json")
        lines = [f"the build on node {node_id} failed after {took}s: {error}: {short(hint, 600)}"]
        if error == "NODE_TOO_OLD":
            lines.append(NODE_UPDATE)
        lines.append(f"its log: amsyos logs {job_id}")
        return None, lines
    cached = obj(built.get("cache"))
    env_said, image_said = obj(cached.get("env")), obj(cached.get("image"))
    how = (
        "taken from your build cache" if env_said.get("restored")
        else "taken as it was" if built.get("env_reused") else "made"
    )  # fmt: skip
    bits = [f"environment {built.get('env')} {how}"]
    if image_said.get("restored"):
        bits.append("image taken from your build cache (setup not run again)")
    kept = [n for n, said in (("environment", env_said), ("image", image_said)) if said.get("kept")]
    if kept:
        bits.append(f"{' and '.join(kept)} kept in your build cache for your next machine")
    out.note(f"built in {took}s: {'; '.join(bits)} (log: amsyos logs {job_id})")
    return built, []


async def _node_deploy(  # noqa: PLR0917 - the deploy's state, passed on
    api: Api,
    out: Out,
    node_id: str,
    service_id: str,
    asset: str,
    commands: dict[str, str],
    built: dict[str, Any],
) -> dict[str, Any]:
    """node.deploy of the package with the card's command (and serve), waited for until the
    node has unpacked it. Its jobs run as the node's own account (`as_node`, the node at
    full and an admin, as node.exec was) only when the build's folder is closed to the job
    account amsyos-job (deploy_node says it once)."""
    params: dict[str, Any] = {"node": node_id, "service": service_id, "package": asset, **commands}
    if built.get("closed_at"):
        params["as_node"] = True
    try:
        told = await api.instant("node.deploy", params)
    except CliError as exc:
        if "serve" not in params or not (
            refused_field(exc, "serve") or ("command" in exc.hint and "required" in exc.hint)
        ):
            raise
        if "command" not in params:
            raise CliError(
                "INVALID_CARD",
                "serve: this Gateway's node.deploy does not take serve yet; give the card a "
                "command too (each job then starts its own process)",
            ) from None
        del params["serve"]
        out.note("this Gateway's node.deploy takes no serve yet: deployed with the command")
        told = await api.instant("node.deploy", params)
    action = str(told.get("action") or "")
    for _ in range(36):  # the node had not answered in the call: up to 3 more minutes
        if told.get("state") != "sent":
            break
        await asyncio.sleep(5)
        listed = await api.instant("node.list")
        for n in seq(listed.get("nodes")):
            for done in seq(obj(n).get("actions")) if obj(n).get("node") == node_id else []:
                if obj(done).get("action") == action and obj(done).get("state") != "sent":
                    told = {**told, **obj(done)}
    if told.get("state") != "done":
        raise CliError(
            "NODE_NOT_READY",
            f"node.deploy on node {node_id}: {told.get('state')}"
            + (f" ({short(str(told['hint']), 300)})" if told.get("hint") else "")
            + "; when amsyos node list shows the node online, amsyos deploy again (the "
            "environment made on it is kept)",
            {"action": action},
        )
    out.note(f"deployed on node {node_id} (action {action})")
    return told
