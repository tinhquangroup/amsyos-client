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
   command (a customer's feedback).

With `where: node` (an own node, spec v3 §17; `--node n_…` when the organisation has
several), steps 4-5 are instead: build the environment and the image on that node by
`node.exec` (`nodekit/build.py`; the node at `full`, an admin), `node.deploy` the package with
the command `sh _node/run.sh <image> '<command>'`, then `service.define` with `compute: node`,
whose check runs the example there. `--dry-run` plans those steps; an own node costs nothing.
The cards below the top one that name its image (`where: node` too) go with it: one build,
then `node.deploy`, `service.define` and the check for each (`--only` picks some).

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

from amsyos_cli import card as cards
from amsyos_cli import repo
from amsyos_cli.api import Api, idempotency_key, refused_field
from amsyos_cli.common import CliError, Credentials, Out, amount, obj, seq, short
from amsyos_cli.files import fetch_text, human, upload
from amsyos_cli.jobs import check_lines, follow, status_lines
from amsyos_cli.shell import cli_hint

MOST_ARCHIVE_BYTES = 200 * 1024 * 1024  # node.build: at most 200 MB
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
) -> int:
    """What deploy would send and make, with nothing sent (read-only calls when logged in)."""
    root, only = from_top(out, root, only)
    made = plan(root)
    card = made["card"]
    if card.get("where", "space") == "node":
        return await dry_run_node(api, out, root, made, channel, node, only)
    if only:
        raise CliError("INVALID_PARAMS", ONLY_SPACE)
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
) -> int:
    root, only = from_top(out, root, only)
    made = plan(root)
    card: dict[str, Any] = made["card"]
    if card.get("where", "space") == "node":
        return await deploy_node(api, out, creds, root, made, node=node, force=force,
                                 channel=channel, only=only)  # fmt: skip
    if only:
        raise CliError("INVALID_PARAMS", ONLY_SPACE)
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
        and last.get("node") is None
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
then steps 2-3 for each card, with its own name, version, inputs, outputs and tests; its
command runs in the image's folder too (job.service tells them apart).
--only services/a,. deploys only those ('.' is the top card); deploy in a card's own
folder deploys that card from the top one.
"""
ONLY_SPACE = (
    "--only picks cards that share one image on an own node (where: node); a card of a "
    "rented node deploys from its own folder: leave --only out"
)
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
    node: dict[str, Any], you: dict[str, Any] | None, card: dict[str, Any] | None
) -> list[str]:
    """What keeps deploy from building on this node, each with what fixes it: online, a
    Linux machine at `full` (node.exec), an admin calling, a GPU when the card needs one."""
    name = f"node {node.get('node')} '{node.get('name') or '?'}'"
    machine = obj(node.get("machine"))
    found: list[str] = []
    if node.get("state") != "online":
        seen = f" (last seen {node['last_seen']})" if node.get("last_seen") else ""
        found.append(f"{name} is offline{seen}: start it at that machine (amsyos-node run)")
    if str(machine.get("os") or "") == "windows":
        found.append(
            f"{name} runs Windows: deploy builds on a Linux node only; on Windows, zip the "
            "service yourself and node.deploy it (amsyos describe node.deploy)"
        )
    if node.get("access") != "full":
        found.append(
            f"{name} is at the level '{node.get('access') or '?'}': deploy builds there with "
            "node.exec, which needs 'full'. Only the person at that machine raises it, there: "
            "sudo amsyos-node access full"
        )
    if you is not None and you.get("kind") == "person" and not you.get("admin"):
        found.append(
            f"node.exec, which builds there, is for an admin of your organisation (or an agent "
            f"of one); you ({you.get('id')}) are no admin: an admin runs amsyos deploy"
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


def _named_image(card: dict[str, Any]) -> str | None:
    try:
        return cards.image_of(card)
    except KeyError:  # names neither an image nor a service
        return None


def _on_node(card: dict[str, Any]) -> bool:
    return card.get("where", "space") == "node"


def image_top(root: Path) -> Path | None:
    """The folder of the card above this one whose image it shares (both `where: node`):
    the code is there, and this folder may hold only its card. None when there is none."""
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
        if _on_node(up) and _named_image(up) == image:
            return folder
    return None


def from_top(out: Out, root: Path, only: list[str] | None) -> tuple[Path, list[str] | None]:
    """A card below the card whose image it shares is deployed from that card's folder,
    which holds the code: this card alone, unless `--only` names others."""
    top = image_top(root)
    if top is None:
        return root, only
    sub = root.relative_to(top).as_posix()
    out.note(
        f"{sub}/{cards.CARD} shares its image with the {cards.CARD} of {top}, whose folder holds "
        "the code: deploying from there, "
        + (f"--only {','.join(only)}" if only else f"this card alone (--only {sub})")
    )
    return top, only or [sub]


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
    """The cards one build on an own node serves: the top card (folder '.') and each card
    packed below it that names the same image with `where: node`; those `--only` names; the
    notes (cards of the image left out, keys of the image a card below sets); and every
    problem of the cards chosen, by card, as `plan` finds them for the top one."""
    top: dict[str, Any] = made["card"]
    image = cards.image_of(top)
    group: list[dict[str, Any]] = [{"dir": ".", "root": root, "card": top}]
    notes: list[str] = []
    for row in made["files"]:
        rel = str(row["file"])
        if not rel.endswith(f"/{cards.CARD}"):
            continue
        folder = rel.removesuffix(f"/{cards.CARD}")
        try:
            card = cards.load(root / folder)
        except CliError as exc:
            notes.append(f"{rel} not read ({exc.code}): left out")
            continue
        if _named_image(card) != image:
            continue
        if not _on_node(card):
            notes.append(
                f"{folder}: names the image {image} but where: {card.get('where', 'space')}: "
                "left out (where: node takes it)"
            )
            continue
        group.append({"dir": folder, "root": root / folder, "card": card})
    problems: list[str] = []
    chosen = group
    if only:
        asked = [_folder_of(p) for given in only for p in given.split(",") if p.strip()]
        known = [str(e["dir"]) for e in group]
        unknown = [a for a in asked if a not in known]
        if unknown:
            problems.append(
                f"--only: no card of the image {image} at {', '.join(unknown)}; its cards: "
                f"{', '.join(known)} ('.' is the top one)"
            )
        chosen = [e for e in group if e["dir"] in asked]
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
            continue  # the card's problem, said already
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


def image_version(top: dict[str, Any], chosen: list[dict[str, Any]], fingerprint: str) -> str:
    """The version the image's folder on the node is named by: the top card's new one; when
    `--only` leaves it out, that with the files' fingerprint, so that no folder a service
    of another version runs from is replaced."""
    version = str(top.get("version") or cards.full_version(str(top["card"]["version"])))
    return version if top in chosen else f"{version}-{fingerprint[:8]}"


def _same_as_last(
    last: dict[str, Any], made: dict[str, Any], node_id: str | None, chosen: list[dict[str, Any]]
) -> bool:
    """Whether the last deploy here defined every chosen card's newest version from these
    very files on this node."""
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


async def dry_run_node(  # noqa: PLR0917 - the dry run's state, passed on
    api: Api | None,
    out: Out,
    root: Path,
    made: dict[str, Any],
    channel: str,
    node: str | None,
    only: list[str] | None = None,
) -> int:
    """`--dry-run` of `where: node`: the files, the node, the build and the calls for every
    card of the image, with nothing sent (read-only calls when logged in). An own node
    costs nothing."""
    card: dict[str, Any] = made["card"]
    group, chosen, notes, problems = node_cards(root, made, only)
    top = group[0]
    multi = len(chosen) > 1 or top not in chosen
    examples = sorted(
        (e["examples"] / p).relative_to(root).as_posix()
        for e in chosen
        for p in cards.local_files(e["card"].get("example")).values()
    )
    answer: dict[str, Any] = {
        "dry_run": True, "where": "node", "service": card["service"], "files": made["files"],
        "left_out": made["left_out"], "fingerprint": made["fingerprint"], "channel": channel,
        "example_files": examples, "cost": 0,
    }  # fmt: skip
    chosen_node: dict[str, Any] | None = None
    ready: list[str] = []
    tenant: str | None = None
    if api is not None:
        tenant, you = await who_calls(api)
        chosen_node = await own_node(api, node)
        ready = node_problems(chosen_node, you, _gpu_card(top, chosen))
    problems += await node_versions(api, tenant, top, chosen)
    version = image_version(top, chosen, made["fingerprint"])
    gpu = bool(seq(obj((chosen_node or {}).get("machine")).get("gpus")))
    build = node_build(root, made, version, gpu)
    package = node_package(root, made, build)
    long_dir = f"/home/{'x' * 24}/amsyos-build/images/{build['image']}@{version}"
    for entry in chosen:
        try:
            node_commands(entry["card"], long_dir)
        except CliError as exc:
            problems.append(_at(entry) + exc.hint)
    if problems:
        raise CliError(_problem_code(problems), "; ".join(problems), {"problems": problems})
    for entry in chosen:  # the image's folder is the node's to say
        entry["commands"] = {
            k: v.replace("IMAGE_DIR", f"<the folder of {build['image']}@{version}>")
            for k, v in node_commands(entry["card"], "IMAGE_DIR").items()
        }
    node_id = str(chosen_node.get("node")) if chosen_node is not None else None
    same = api is not None and _same_as_last(_read_state(root), made, node_id, chosen)
    first = chosen[0]
    answer["service"] = first.get("service", answer["service"])
    if chosen_node is not None:
        answer |= {"node": node_id, "node_ready": not ready}
        if ready:
            answer["problems"] = ready
    answer["would_make"] = None if same else first.get("version")
    answer["image"] = f"{build['image']}@{version}"
    answer["cards"] = [
        {"dir": e["dir"], "service": e.get("service"), "newest": e.get("current"),
         "would_make": None if same else e.get("version"), "commands": e["commands"]}
        for e in chosen
    ]  # fmt: skip
    rows = await model_sizes(build["models"]) if build["models"] else []
    known = [r["bytes"] for r in rows if r["bytes"] is not None]
    models_gb = sum(known) / 1e9 if rows and len(known) == len(rows) else None
    answer |= {"build": build, "package_bytes": len(package), "commands": first["commands"]}
    if notes:
        answer["notes"] = notes
    lines = file_lines({**made, "archive": package}, every=True)
    if examples:
        lines.append("example files uploaded too: " + ", ".join(examples))
    lines += notes
    if chosen_node is not None:
        lines.append(f"own node: {node_line(chosen_node)}")
        lines += [f"  not ready: {p}" for p in ready]
    if same:
        lines += [f"{e['service']}@{e['current']}: nothing changed, nothing would be built"
                  for e in chosen]  # fmt: skip
    elif chosen_node is None:
        lines.append(
            f"would deploy {', '.join(str(e['service']) for e in chosen)} on your own node (not "
            "logged in: the node and the versions are not known):"
        )
    elif not multi:
        newest = f"newest now: {first['current']}" if first.get("current") else "none defined yet"
        lines.append(
            f"would deploy {first['service']}@{first['version']} on it ({newest}), channel "
            f"{channel}:"
        )
    else:
        lines.append(
            f"would deploy {len(chosen)} of the {len(group)} cards of the image "
            f"{build['image']} on it, one build ({build['image']}@{version}), channel {channel}:"
        )
    if multi and not same:
        for e in chosen:
            newest = f"newest now: {e['current']}" if e.get("current") else "none defined yet"
            lines.append(
                f"  {e['dir']}: {e['service']}@{e['version']}"
                + (f" ({newest})" if chosen_node is not None else "")
                + f", command: {e['commands'].get('command') or '(none)'}"
                + (f"; serve: {e['commands']['serve']}" if e["commands"].get("serve") else "")
            )
    steps = [
        f"  1. node.exec (an admin; the node at full), with the package as file1: {NODE_EXEC}; "
        f"up to {NODE_EXEC_S // 60} min",
        *node_plan_lines(build, models_gb),
    ]
    if multi:
        steps += [
            "  2. for each card: node.deploy the package with its command (above)",
            "  3. for each card: service.define its version with compute: node; its check runs "
            "its example on the node",
        ]
    else:
        steps += [
            f"  2. node.deploy the package, command: {first['commands'].get('command') or '(none)'}"
            + (f"; serve: {first['commands']['serve']}" if first["commands"].get("serve") else ""),
            f"  3. service.define {card['service']}@{first.get('version')} with compute: node; "
            "its check runs the example on the node",
        ]
    if not same:
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
    each check runs there. One card's failure after the build is said with the others'."""
    group, chosen, notes, problems = node_cards(root, made, only)
    top = group[0]
    multi = len(chosen) > 1 or top not in chosen
    tenant, you = await who_calls(api)
    chosen_node = await own_node(api, node)
    node_id = str(chosen_node.get("node"))
    problems += await node_versions(api, tenant, top, chosen)
    last = _read_state(root)
    if not force and not problems and _same_as_last(last, made, node_id, chosen):
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
    ready = node_problems(chosen_node, you, _gpu_card(top, chosen))
    version = image_version(top, chosen, made["fingerprint"])
    gpu = bool(seq(obj(chosen_node.get("machine")).get("gpus")))
    build = node_build(root, made, version, gpu)
    package = node_package(root, made, build)
    # A command too long for node.deploy is said before the build: the image's folder as a
    # long home folder would put it.
    long_dir = f"/home/{'x' * 24}/amsyos-build/images/{build['image']}@{version}"
    for entry in chosen:
        try:
            node_commands(entry["card"], long_dir)
        except CliError as exc:
            problems.append(_at(entry) + exc.hint)
    if problems or ready:  # every one at once, before anything is uploaded
        raise CliError(
            _problem_code(problems), "; ".join(problems + ready),
            {"node": node_id, "problems": problems + ready},
        )  # fmt: skip
    for line in file_lines({**made, "archive": package}, every=False):
        out.note(line)
    for line in notes:
        out.note(line)
    if multi:
        out.note(
            f"image {build['image']}@{version}: {len(chosen)} of its {len(group)} cards: "
            + ", ".join(str(e["dir"]) for e in chosen)
        )
    state_made = not (root / ".amsyos").exists()
    for e in chosen:
        await upload_example(api, out, e["examples"], e["card"], f"{e['dir']}: " if multi else "")
    folder, _ = repo.state_dir(root)
    if state_made:
        out.note(repo.STATE_NOTE)
    tmp = folder / f"{build['image']}-{version}.zip"
    tmp.write_bytes(package)
    try:
        row = (await upload(api, [tmp]))[0]
    finally:
        tmp.unlink(missing_ok=True)
    asset = str(row["asset"])
    how = "already in the workspace" if row.get("reused") else "uploaded"
    out.note(f"package: {asset} ({how})")
    first = chosen[0]
    answer: dict[str, Any] = {
        "service": first["service"], "version": first["version"], "node": node_id,
        "channel": channel, "image": f"{build['image']}@{version}", "files": made["files"],
        "left_out": made["left_out"], "package": asset, "cost": 0,
    }  # fmt: skip
    if notes:
        answer["notes"] = notes
    if state_made:
        answer["made"] = ".amsyos/"
    began = time.monotonic()
    built, lines = await _node_exec(api, out, creds, node_id, asset, answer)
    if built is None:
        out.emit(answer, *lines)
        return 1
    closed = built.get("closed_at")
    if closed:
        out.note(
            f"its jobs run as the node's own account: {closed} is closed to the job account "
            "amsyos-job (a job of an order there would fail); open it (chmod o+x) or set "
            "AMSYOS_BUILD_HOME for the node, then deploy again, to have them run as amsyos-job"
        )
    image_dir = str(built.get("image_dir"))
    source = api.source()
    for e in chosen:
        try:
            commands = node_commands(e["card"], image_dir)
            e["deployed"] = await _node_deploy(api, out, node_id, e["service"], asset, commands,
                                               built)  # fmt: skip
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
            if not multi:
                raise
            e["error"] = exc.answer()
            out.note(f"{e['service']}@{e['version']}: not deployed: {exc.code}: {exc.hint}")
            continue
        out.note(f"defined {e['service']}@{e['version']} (compute: node)")
    for e in chosen:  # their checks run on the node meanwhile
        if "error" in e:
            continue
        e["check"] = await _check(api, out, e["service"], e["version"])
        check_job = obj(e["check"]).get("job")
        if isinstance(check_job, str):
            remember_check(root, check_job, f"{e['service']}@{e['version']}")
    took = round(time.monotonic() - began)
    on = f"on node {node_id} '{chosen_node.get('name') or '?'}'"
    if multi:
        lines = [
            f"image {build['image']}@{version} {on}: build {answer['build_job']}, {took}s in all, "
            f"cost none; {len(chosen)} cards:"
        ]
    else:
        lines = [
            f"{first['service']}@{first['version']}  {on} (build {answer['build_job']}, {took}s "
            "in all, cost none)"
        ]
    too_old = False
    for e in chosen:
        if multi:
            lines.append(f"{e['service']}@{e['version']} ({e['dir']})")
        e["ok"], said, old = _card_lines(e, multi=multi)
        lines += said
        too_old = too_old or old
    if too_old:
        lines.append(f"  {NODE_UPDATE}")
    answer["cards"] = [
        {k: e[k] for k in ("dir", "service", "version", "check", "run", "error") if k in e}
        for e in chosen
    ]
    if not multi:
        answer |= {k: first[k] for k in ("deployed", "defined", "check", "run") if k in first}
    if any(e["ok"] for e in chosen):
        _remember_cards(root, last, made, node_id, chosen)
    out.emit(answer, *lines)
    return 0 if all(e["ok"] for e in chosen) else 1


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
    run, said = run_lines(entry["service"], entry["version"], stable_of(obj(entry.get("defined"))))
    entry["run"] = run
    return True, lines + [pad + s for s in said], too_old


def _remember_cards(
    root: Path, last: dict[str, Any], made: dict[str, Any], node_id: str,
    chosen: list[dict[str, Any]],
) -> None:  # fmt: skip
    """What `_same_as_last` reads: the cards defined from these files on this node, kept
    with those an earlier deploy of the same files defined there (`--only`)."""
    done = {
        str(e["dir"]): {"service": e["service"], "version": e["version"]}
        for e in chosen
        if e.get("ok")
    }
    same = last.get("fingerprint") == made["fingerprint"] and last.get("node") == node_id
    kept = {**(obj(last.get("cards")) if same else {}), **done}
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state: dict[str, Any] = {"fingerprint": made["fingerprint"], "at": at, "node": node_id}
    if "." in kept:
        state |= obj(kept["."])
    _write_state(root, {**state, "cards": kept})


async def _node_exec(  # noqa: PLR0917 - the deploy's state, passed on
    api: Api, out: Out, creds: Credentials, node_id: str, asset: str, answer: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """The build on the node (node.exec of `_node/build.py`), followed to its end: what it
    made (`out/build.json`), or None and the lines that say why not."""
    params = {
        "node": node_id, "command": NODE_EXEC, "timeout_s": NODE_EXEC_S, "file1": asset,
        "outputs": "build",
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
    how = "taken as it was" if built.get("env_reused") else "made"
    out.note(f"built in {took}s: environment {built.get('env')} {how} (log: amsyos logs {job_id})")
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
