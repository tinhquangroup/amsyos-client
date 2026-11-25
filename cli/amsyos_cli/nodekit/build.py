#!/usr/bin/env python3
# ruff: noqa: S603, S607, T201 - it runs the node's own tools, and what it prints is its log
"""A service's environment on an organisation's own node (Linux), made by `amsyos deploy`
(`where: node`) through node.exec, as the node's own account:

    python3 -m zipfile -e in/file1.zip src && python3 src/_node/build.py in/file1.zip src

`src/_node/build.json` says what goes in, from the card: the conda-forge packages (the
runtime and `system`), the pip packages, PyTorch when the code imports it, the card's
`models` (public, pinned) and its `setup`. Under AMSYOS_BUILD_HOME (by default
~/amsyos-build, the node's account's):

  envs/<key>                 one environment per hash of what is in it: a new version with
                             the same packages takes it as it is
  images/<image>@<version>   the code and what `setup` made there; its jobs run in it
  hf/                        Hugging Face's cache of the models (jobs read it offline)
  fetched/                   what amsyos-fetch downloaded, once a machine
  tools/micromamba           when the node has none on its PATH: pinned, checked by sha256

It ends with out/build.json, which deploy reads: {"ok": true, "image_dir", ...} or
{"ok": false, "error", "hint"}; its log is what it printed. It puts no token or key here.
Python 3.8 and later, standard library only.

The organisation's build cache (kernel/build_cache.py): when the job's params (job.json)
hold `cache`, the kernel's presigned links, the environment and the image are first taken
from a copy another of the organisation's machines of the same kind (`fit`: system,
architecture, C library, build folder) built, each checked against the size and MD5 the
VPS keeps before it is unpacked, as this account, under this build folder only. What this
build makes itself, when it took CACHE_LEAST_S or more, is packed and sent for the next
machine; out/build.json says it under `cache`. Nothing of the cache fails a build.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import http.client
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import BinaryIO

# micromamba, pinned to a release and its sha256, as AMSYOS's rented nodes fetch it.
MICROMAMBA = {
    "x86_64": (
        "https://github.com/mamba-org/micromamba-releases/releases/download/2.9.0-0/"
        "micromamba-linux-64",
        "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3",
    ),
}
MOST_MICROMAMBA = 64 << 20
JOB = Path(os.environ.get("AMSYOS_JOB") or ".")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
CACHE_LEAST_S = 30  # a part built faster than this is not worth keeping
CACHE_COMPRESS = 1  # gzip: fast, the build waits for it
CACHE_TIMEOUT_S = 120  # one read or write of a copy over the network
CHUNK = 1 << 20
MOST_MEMBERS = 500_000


class Refused(Exception):  # noqa: N818 - how a step says what stopped the build
    def __init__(self, error: str, hint: str) -> None:
        super().__init__(hint)
        self.error, self.hint = error, hint


def log(message: str) -> None:
    print(f"[build {time.strftime('%H:%M:%S')}] {message}", flush=True)


def progress(percent: int) -> None:
    with contextlib.suppress(OSError):
        (JOB / "progress").write_text(str(percent))


def run(command: list[str], step: str, hint: str, **more: object) -> None:
    log("$ " + " ".join(shlex.quote(c) for c in command)[:400])
    done = subprocess.run(command, stdin=subprocess.DEVNULL, check=False, **more)
    if done.returncode != 0:
        raise Refused("BUILD_FAILED", f"{step}: {hint} (exit code {done.returncode})")


# ---------------------------------------------------------------- the node
def _asked_version(exe: str, argv0: str) -> str | None:
    try:
        said = subprocess.run(
            [argv0, "version"], executable=exe, capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"amsyos-node (\d+\.\d+\.\d+)", said.stdout)
    return found.group(1) if found else None


def node_version() -> str | None:
    """The version of the AMSYOS Node that started this command: the program up its line
    of parents, else the copy of it that every job finds as `amsyos-job`."""
    pid = os.getppid()
    for _ in range(8):
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            break
        if os.path.basename(exe).startswith("amsyos-node"):
            found = _asked_version(exe, exe)
            if found:
                return found
        try:
            with open(f"/proc/{pid}/stat") as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
    job = shutil.which("amsyos-job")
    return _asked_version(job, "amsyos-node") if job else None


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version)[:3])


def closed_on_the_way(path: Path) -> str | None:
    """The first folder on the way to `path` that other accounts (the job account
    amsyos-job) may not enter, or None."""
    at = Path("/")
    for part in path.parts[1:]:
        at = at / part
        try:
            if not at.stat().st_mode & stat.S_IXOTH:
                return str(at)
        except OSError:
            return str(at)
    return None


def readable(path: Path) -> None:
    """Every file readable and every folder enterable by every account; written by none
    but the node's own."""
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            target = Path(root) / name
            if target.is_symlink():
                continue
            mode = target.stat().st_mode
            add = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            if target.is_dir() or mode & stat.S_IXUSR:
                add |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            target.chmod((mode | add) & ~(stat.S_IWGRP | stat.S_IWOTH))
    path.chmod(0o755)


def has_nvidia() -> bool:
    try:
        return (
            subprocess.run(
                ["nvidia-smi", "-L"], capture_output=True, timeout=30, check=False,
                stdin=subprocess.DEVNULL,
            ).returncode == 0
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- the build cache
def cache_links() -> dict[str, object]:
    """The kernel's links for this build (job.json's params `cache`), or {}."""
    try:
        told = json.loads((JOB / "job.json").read_text())
    except (OSError, ValueError):
        return {}
    cache = (told.get("params") or {}).get("cache") if isinstance(told, dict) else None
    return cache if isinstance(cache, dict) else {}


def fit_of(base: Path) -> str:
    """The kind of machine what this build makes runs on: the same system, architecture,
    C library and build folder (an environment names the folder it was made in)."""
    libc = "-".join(p for p in platform.libc_ver() if p) or "libc"
    return f"own|{platform.system().lower()}-{platform.machine()}|{libc}|{base}"


def _url(value: object) -> str | None:
    text = str(value or "")
    return text if urllib.parse.urlsplit(text).scheme in ("https", "http") else None


def _reason(exc: BaseException) -> str:
    """Why a request failed, never with its URL (it carries the signature)."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror[:80]
    return type(exc).__name__


def fetch_copy(entry: dict[str, object], into: Path) -> int:
    """A copy, into `into`, checked against the size and MD5 the VPS keeps: anything else
    and nothing is kept."""
    url, want, md5 = _url(entry.get("url")), entry.get("bytes"), str(entry.get("md5") or "")
    shaped = isinstance(want, int) and want > 0 and re.fullmatch(r"[0-9a-f]{32}", md5)
    if url is None or not shaped or not isinstance(want, int):
        raise Refused("CACHE", "the copy came without its address, size or checksum")
    if want + (2 << 30) > shutil.disk_usage(str(into.parent)).free:
        raise Refused("CACHE", "too little room on this disk for the copy")
    digest, size = hashlib.md5(), 0  # noqa: S324 - R2's own checksum, compared, not trusted for more
    try:
        with urllib.request.urlopen(url, timeout=CACHE_TIMEOUT_S) as got, into.open("wb") as out:  # noqa: S310 - the kernel's https URL
            while True:
                block = got.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > want:
                    raise Refused("CACHE", "the copy is larger than the VPS says it is")
                digest.update(block)
                out.write(block)
    except (OSError, http.client.HTTPException) as exc:
        into.unlink(missing_ok=True)
        raise Refused("CACHE", f"the copy could not be fetched ({_reason(exc)})") from None
    if size != want or digest.hexdigest() != md5:
        into.unlink()
        raise Refused("CACHE", "the copy did not match the size and checksum the VPS keeps")
    return size


def _safe_member(member: tarfile.TarInfo, top: str) -> bool:
    """A member under `top` only: no absolute path, no `..`, a link that stays inside,
    folders, files and links only (no device, no set-user bit kept)."""
    name = member.name.rstrip("/")
    parts = name.split("/")
    if name.startswith("/") or parts[0] != top or any(p in ("", ".", "..") for p in parts):
        return False
    if member.issym() or member.islnk():
        link = member.linkname
        if link.startswith("/"):
            return False
        here = os.path.dirname(name) if member.issym() else ""
        resolved = os.path.normpath(os.path.join(here, link))
        if resolved != top and not resolved.startswith(top + "/"):
            return False
    elif not (member.isdir() or member.isfile()):
        return False
    member.mode &= 0o777
    return True


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def unpack_copy(copy: Path, staging: Path, top: str) -> Path:
    """The copy (a gzip or zstd tar, as its first bytes say) unpacked into `staging` (a
    new folder of this account's), member by member as it streams: returns
    staging/<top>. A member outside `top` refuses it all (what came before is in staging,
    which goes)."""
    staging.mkdir(mode=0o700)
    with copy.open("rb") as head:
        zstd = head.read(4) == ZSTD_MAGIC
    unzstd: subprocess.Popen[bytes] | None = None
    if zstd:
        tool = shutil.which("zstd")
        if tool is None:
            raise Refused("CACHE", "the copy is zstd and this machine has no zstd")
        unzstd = subprocess.Popen(
            [tool, "-d", "-q", "-c", str(copy)], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL
        )
        assert unzstd.stdout is not None  # noqa: S101 - asked for above
        opened = tarfile.open(fileobj=unzstd.stdout, mode="r|")  # noqa: SIM115 - closed below
    else:
        opened = tarfile.open(str(copy), "r|gz")  # noqa: SIM115 - closed below
    try:
        with opened as tar:
            for n, member in enumerate(tar):
                if n >= MOST_MEMBERS:
                    raise Refused("CACHE", "the copy has more files than a build keeps")
                if not _safe_member(member, top):
                    raise Refused(
                        "CACHE", f"the copy has a path that leaves it: {member.name[:80]}"
                    )
                if hasattr(tarfile, "data_filter"):  # Python 3.12, and 3.8.17 and later
                    tar.extract(member, str(staging), filter="data")
                else:
                    tar.extract(member, str(staging))
    finally:
        if unzstd is not None:
            unzstd.kill()
            unzstd.wait()
    if unzstd is not None and unzstd.returncode not in (0, -9):
        raise Refused("CACHE", "zstd could not read the copy")
    return staging / top


def from_cache(links: object, top: str, folder: Path, fit: str, label: str) -> dict[str, object]:
    """`folder` (envs/<key> or images/<image>@<version>) put in place from a copy of this
    fit, when the links hold one; what happened, for out/build.json."""
    told: dict[str, object] = {"restored": False}
    entries = links.get("get") if isinstance(links, dict) else None
    mine = [e for e in entries or [] if isinstance(e, dict) and e.get("fit") == fit]
    if not mine:
        if entries:
            told["why"] = "the copies kept are of other machines"
        return told
    began = time.monotonic()
    copy = JOB / f"cache-{label}.tar.gz"
    staging = folder.parent / f".incoming-{top}-{os.getpid()}"
    try:
        size = fetch_copy(mine[0], copy)
        made = unpack_copy(copy, staging, top)
        old = folder.with_name(folder.name + ".old")
        if old.exists():
            shutil.rmtree(str(old))
        if folder.exists():
            folder.rename(old)
        made.rename(folder)
        if old.exists():
            shutil.rmtree(str(old), ignore_errors=True)
        told.update(restored=True, bytes=size, s=round(time.monotonic() - began))
        log(f"{label} {top}: taken from your build cache ({size / 1e6:.0f} MB in {told['s']} s)")
    except Refused as exc:
        told["why"] = exc.hint
        log(f"{label} {top}: not taken from your build cache: {exc.hint}")
    except (OSError, tarfile.TarError, EOFError, ValueError) as exc:
        told["why"] = f"it could not be unpacked ({_reason(exc)})"
        log(f"{label} {top}: not taken from your build cache: {told['why']}")
    finally:
        copy.unlink(missing_ok=True)
        shutil.rmtree(str(staging), ignore_errors=True)
    return told


def _has_fit(links: object, fit: str) -> bool:
    """Whether the cache keeps a copy for this kind of machine already."""
    entries = links.get("get") if isinstance(links, dict) else None
    return any(isinstance(e, dict) and e.get("fit") == fit for e in entries or [])


class _Counted:
    """Where the packed copy goes: counted and hashed as it is written."""

    def __init__(self, out: BinaryIO) -> None:
        self.out, self.size, self.md5 = out, 0, hashlib.md5()  # noqa: S324 - compared with R2's

    def write(self, data: bytes) -> int:
        self.size += len(data)
        self.md5.update(data)
        return self.out.write(data)

    def flush(self) -> None:
        self.out.flush()


def _put(url: str, path: Path, size: int) -> None:
    parts = urllib.parse.urlsplit(url)
    made = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = made(parts.hostname or "", parts.port, timeout=CACHE_TIMEOUT_S, blocksize=CHUNK)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        with path.open("rb") as body:
            conn.request("PUT", target, body=body, headers={"Content-Length": str(size)})
            answer = conn.getresponse()
            answer.read(4096)
    except (OSError, http.client.HTTPException) as exc:
        raise Refused("CACHE", f"sending the copy failed ({_reason(exc)})") from None
    finally:
        conn.close()
    if answer.status not in (200, 201, 204):
        raise Refused("CACHE", f"the store refused the copy (HTTP {answer.status})")


def to_cache(links: object, folder: Path, fit: str, label: str, took: float) -> dict[str, object]:
    """`folder` packed and sent with the kernel's PUT, when it took CACHE_LEAST_S or more
    to make; what happened, for out/build.json (the kernel asks R2 what arrived)."""
    url = _url(links.get("put")) if isinstance(links, dict) else None
    if url is None:
        return {"kept": False}
    if took < CACHE_LEAST_S:
        return {"kept": False, "why": f"made in {took:.0f} s: not worth keeping"}
    most = links.get("max_bytes") if isinstance(links, dict) else None
    began = time.monotonic()
    packed = JOB / f"cache-{label}-out.tar"
    try:
        size, md5 = _pack(folder, packed)
        if isinstance(most, int) and size > most:
            return {"kept": False, "why": f"{size / 1e9:.1f} GB packed, over what a copy may be"}
        _put(url, packed, size)
    except Refused as exc:
        log(f"{label} not kept in your build cache: {exc.hint}")
        return {"kept": False, "why": exc.hint}
    except (OSError, tarfile.TarError) as exc:
        log(f"{label} not kept in your build cache: {_reason(exc)}")
        return {"kept": False, "why": _reason(exc)}
    finally:
        with contextlib.suppress(OSError):
            packed.unlink()
    secs = round(time.monotonic() - began)
    log(f"{label} kept in your build cache ({size / 1e6:.0f} MB packed and sent in {secs} s)")
    return {"kept": True, "fit": fit, "bytes": size, "md5": md5, "s": secs}


def _pack(folder: Path, into: Path) -> tuple[int, str]:
    """`folder` as a tar: zstd on every core where the machine has GNU tar and zstd (a
    gzip on one core took longer than the upload), else gzip; its size and MD5."""
    tar, zstd = shutil.which("tar"), shutil.which("zstd")
    if tar and zstd:
        with into.open("wb") as out:
            packing = subprocess.Popen(
                [tar, "-C", str(folder.parent), "-cf", "-", folder.name],
                stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
            )  # fmt: skip
            squeezed = subprocess.run(
                [zstd, "-q", "-T0", "-3", "-c"], stdin=packing.stdout, stdout=out, check=False
            )
            if packing.stdout is not None:
                packing.stdout.close()
            if packing.wait() != 0 or squeezed.returncode != 0:
                raise Refused("CACHE", "tar and zstd could not pack it")
        digest = hashlib.md5()  # noqa: S324 - compared with R2's
        with into.open("rb") as done:
            while block := done.read(CHUNK):
                digest.update(block)
        return into.stat().st_size, digest.hexdigest()
    with into.open("wb") as raw:
        counted = _Counted(raw)
        gz = gzip.GzipFile(
            filename="", mode="wb", compresslevel=CACHE_COMPRESS, fileobj=counted, mtime=0
        )  # type: ignore[arg-type]
        with gz, tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar_file:
            tar_file.add(str(folder), arcname=folder.name)
    return counted.size, counted.md5.hexdigest()


# ---------------------------------------------------------------- the environment
def micromamba(base: Path) -> str:
    found = shutil.which("micromamba")
    if found:
        return found
    tool = base / "tools" / "micromamba"
    if tool.is_file():
        return str(tool)  # checked when it was fetched
    pin = MICROMAMBA.get(platform.machine())
    if pin is None:
        raise Refused(
            "UNSUPPORTED",
            f"this node is {platform.machine()}: deploy fetches micromamba for x86_64 only; put "
            "micromamba on the node's PATH (mamba.readthedocs.io), then deploy again",
        )
    url, want = pin
    tool.parent.mkdir(parents=True, exist_ok=True)
    part = tool.with_name("micromamba.part")
    log("micromamba: fetching 2.9.0, pinned")
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=120) as got, part.open("wb") as fh:  # noqa: S310 - a pinned https URL
        while True:
            block = got.read(1 << 20)
            if not block:
                break
            size += len(block)
            if size > MOST_MICROMAMBA:
                raise Refused("BUILD_FAILED", "micromamba's download is larger than it should be")
            digest.update(block)
            fh.write(block)
    if digest.hexdigest() != want:
        part.unlink()
        raise Refused("BUILD_FAILED", "micromamba's download did not match its pinned sha256")
    part.chmod(0o755)
    part.replace(tool)
    return str(tool)


def conda_owned(python: Path, packages: list[str]) -> list[str]:
    """The pinned pip packages that conda installed already with no RECORD (opencv from
    conda-forge): pip cannot uninstall those, so their wheels go over them first."""
    code = (
        "import re, sys\nfrom importlib import metadata\n"
        "for line in sys.argv[1:]:\n"
        "    name = re.split(r'[=<>\\[ @;!~]', line, maxsplit=1)[0]\n"
        "    try:\n        d = metadata.distribution(name)\n"
        "    except metadata.PackageNotFoundError:\n        continue\n"
        "    if d.read_text('RECORD') is None:\n        print(line)\n"
    )
    said = subprocess.run(
        [str(python), "-c", code, *packages], capture_output=True, text=True, check=False,
        stdin=subprocess.DEVNULL,
    )  # fmt: skip
    return [line for line in said.stdout.splitlines() if line.strip()]


def make_env(
    asked: dict[str, object], src: Path, base: Path, cached: dict[str, object]
) -> tuple[Path, bool]:
    """The environment: made before here, taken from the build cache (`cached` says it),
    or made now."""
    env = base / "envs" / str(asked["env"])
    if (env / ".ready").is_file():
        log(f"environment {env.name}: made before, taken as it is")
        return env, True
    links = CACHE.get("env")
    if isinstance(links, dict) and links.get("key") == env.name:
        cached.update(from_cache(links, env.name, env, fit_of(base), "environment"))
        if cached.get("restored") and (env / ".ready").is_file():
            return env, False
        if cached.get("restored"):  # whole, but not made ready: made again
            cached.update(restored=False, why="the copy was not a finished environment")
    if env.exists():
        shutil.rmtree(env)
    mamba = micromamba(base)
    progress(10)
    conda = [str(c) for c in asked.get("conda") or []]
    env_vars = dict(os.environ, MAMBA_ROOT_PREFIX=str(base / "mamba"))
    run(
        [mamba, "create", "--yes", "--quiet", "--prefix", str(env), "-c", "conda-forge",
         "--override-channels", *conda],
        "runtime/system",
        f"micromamba could not install {' '.join(conda)}: is each a conda-forge package, and "
        "a version it has? Its words above say which",
        env=env_vars,
    )  # fmt: skip
    progress(40)
    python = env / "bin" / "python"
    pip = [str(python), "-m", "pip", "install", "--quiet", "--no-cache-dir",
           "--disable-pip-version-check"]  # fmt: skip
    torch = asked.get("torch")
    if isinstance(torch, dict):
        run(
            [*pip, "--index-url", str(torch["index"]), *[str(p) for p in torch["packages"]]],
            "PyTorch", "pip could not install PyTorch from its index",
        )  # fmt: skip
    packages = [str(p) for p in asked.get("packages") or []]
    if packages:
        over = conda_owned(python, packages)
        if over:
            log(f"pip over conda's own: {' '.join(over)}")
            run([*pip, "--no-deps", "--ignore-installed", *over], "packages", "pip failed")
        listed = env / "requirements.txt"
        listed.write_text("\n".join(packages) + "\n")
        why = "pip could not install them; its words above say which (a version that does "
        run([*pip, "-r", str(listed)], "packages", why + "not exist, or one that needs a compiler)")
    requirements = asked.get("requirements")
    if requirements:
        run([*pip, "-r", str(src / str(requirements))], "packages",
            f"pip could not install {requirements}; its words above say which")  # fmt: skip
    if asked.get("models"):
        run([*pip, "huggingface_hub>=0.30"], "models", "pip could not install huggingface_hub")
    readable(env)
    (env / ".ready").write_text(json.dumps({"made": time.time()}))
    return env, False


def fetch_models(asked: dict[str, object], env: Path, base: Path) -> None:
    code = (
        "import sys\nfrom huggingface_hub import snapshot_download\n"
        "repo, _, rest = sys.argv[1].partition('@')\n"
        "rev, _, only = rest.partition(':')\n"
        "path = snapshot_download(repo, revision=rev or None, "
        "allow_patterns=only.split(',') if only else None)\n"
        "print('model', repo, rev[:8], 'at', path, flush=True)\n"
    )
    for model in asked.get("models") or []:
        run(
            [str(env / "bin" / "python"), "-c", code, str(model)], "models",
            f"{model} could not be fetched from Hugging Face (public, at its pinned commit)",
            env=dict(os.environ, HF_HOME=str(base / "hf")),
        )  # fmt: skip
    if (base / "hf").is_dir():
        readable(base / "hf")


def setup(asked: dict[str, object], src: Path, env: Path, base: Path) -> None:
    """The card's `setup`, in the code's folder, with the environment as a job has it (run.sh):
    its bin/ first on PATH, its lib/ not on LD_LIBRARY_PATH ($CONDA_PREFIX/lib)."""
    command = str(asked.get("setup") or "").strip()
    if not command:
        return
    path = f"{src / '_node' / 'bin'}:{env / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    env_vars = dict(
        os.environ, PATH=path, CONDA_PREFIX=str(env), PREFIX=str(env), HF_HOME=str(base / "hf"),
        AMSYOS_FETCH_STORE=str(base / "fetched"), PYTHONDONTWRITEBYTECODE="1",
    )  # fmt: skip
    run(["/bin/sh", "-c", command], "setup", "the card's setup failed; its words above say why",
        cwd=str(src), env=env_vars)  # fmt: skip


# ---------------------------------------------------------------- the image
def unpacked_modes(package: Path, src: Path) -> None:
    """`python -m zipfile -e` keeps no file mode: the executable ones made so again."""
    with zipfile.ZipFile(package) as zipped:
        for info in zipped.infolist():
            mode = (info.external_attr >> 16) & 0o777
            target = src / info.filename
            if mode & 0o111 and target.is_file() and not target.is_symlink():
                target.chmod(target.stat().st_mode | 0o755)
    for tool in (src / "_node" / "bin").glob("*"):
        tool.chmod(0o755)


def put_image(asked: dict[str, object], src: Path, env: Path, base: Path) -> Path:
    image = base / "images" / f"{asked['image']}@{asked['version']}"
    where = {"ENV_DIR": env, "HF_DIR": base / "hf", "FETCHED": base / "fetched"}
    (src / "_node" / "where.env").write_text(
        "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in where.items())
    )
    readable(src)
    image.parent.mkdir(parents=True, exist_ok=True)
    old = image.with_name(image.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if image.exists():
        image.rename(old)
    shutil.move(str(src), str(image))
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    return image


CACHE: dict[str, object] = {}  # the kernel's links for this build (`cache_links`)


def build(package: Path, src: Path) -> dict[str, object]:
    began = time.monotonic()
    asked = json.loads((src / "_node" / "build.json").read_text())
    for key in ("image", "version", "env"):
        if not _NAME.fullmatch(str(asked.get(key) or "")):
            raise Refused("INVALID_PARAMS", f"build.json: {key} is no plain name")
    base = Path(os.environ.get("AMSYOS_BUILD_HOME") or Path.home() / "amsyos-build")
    for folder in ("envs", "images", "hf", "fetched"):
        (base / folder).mkdir(parents=True, exist_ok=True)
    base.chmod(base.stat().st_mode | 0o755)
    version = node_version()
    least = str(asked.get("node_min") or "0")
    told: dict[str, object] = {"node_version": version, "base": str(base)}
    if asked.get("needs_module") and version and as_tuple(version) < as_tuple(least):
        raise Refused(
            "NODE_TOO_OLD",
            f"this node runs AMSYOS Node {version}; the code imports amsyos, which AMSYOS "
            f"Node {least} and later gives every job",
        )
    log(f"image {asked['image']}@{asked['version']}, environment {asked['env']}, under {base}"
        + (f"; AMSYOS Node {version}" if version else ""))  # fmt: skip
    unpacked_modes(package, src)
    progress(5)
    CACHE.update(cache_links())
    fit = fit_of(base)
    env_cache: dict[str, object] = {}
    env_began = time.monotonic()
    env, reused = make_env(asked, src, base, env_cache)
    env_took = time.monotonic() - env_began
    told.update({"env": env.name, "env_reused": reused})
    progress(60)
    # The environment is kept for the next machine while models and setup run.
    env_links = CACHE.get("env")
    env_keep: list[dict[str, object]] = []
    keeping: threading.Thread | None = None
    if CACHE and not env_cache.get("restored") and (not reused or not _has_fit(env_links, fit)):
        took = CACHE_LEAST_S if reused else env_took  # made here before the cache had it

        def keep_env() -> None:
            env_keep.append(to_cache(env_links, env, fit, "environment", took))

        keeping = threading.Thread(target=keep_env, daemon=True)
        keeping.start()
    if asked.get("needs_module") and not version:
        probe = subprocess.run(
            [str(env / "bin" / "python"), "-c", "import amsyos"], cwd=str(src),
            capture_output=True, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
        if probe.returncode != 0:
            raise Refused(
                "NODE_TOO_OLD",
                f"a job here finds no amsyos module, which AMSYOS Node {least} and later "
                "gives every job",
            )
    fetch_models(asked, env, base)
    progress(75)
    image_name = f"{asked['image']}@{asked['version']}"
    image_links = CACHE.get("image")
    image_cache = (
        from_cache(image_links, image_name, base / "images" / image_name, fit, "image")
        if isinstance(image_links, dict)
        else {}
    )
    if image_cache.get("restored"):
        image = base / "images" / image_name
        image_took = 0.0
    else:
        image_began = time.monotonic()
        setup(asked, src, env, base)
        progress(90)
        image = put_image(asked, src, env, base)
        image_took = time.monotonic() - image_began
    if CACHE:
        if not image_cache.get("restored"):
            image_cache.update(to_cache(image_links, image, fit, "image", image_took))
        if keeping is not None:
            keeping.join()
            env_cache.update(env_keep[0] if env_keep else {"kept": False})
        told["cache"] = {"env": env_cache, "image": image_cache}
    free = shutil.disk_usage(str(base)).free
    told.update({
        "ok": True, "image_dir": str(image), "closed_at": closed_on_the_way(image),
        "seconds": round(time.monotonic() - began), "disk_free_gb": round(free / 1e9, 1),
    })  # fmt: skip
    log(f"image ready in {told['seconds']} s: {image}; {told['disk_free_gb']} GB free")
    return told


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: build.py <package.zip> <its unpacked folder>", file=sys.stderr)
        return 2
    try:
        told = build(Path(sys.argv[1]), Path(sys.argv[2]).resolve())
    except Refused as exc:
        told = {"ok": False, "error": exc.error, "hint": exc.hint}
    except Exception as exc:  # said to deploy, whatever it was
        traceback.print_exc(file=sys.stdout)
        told = {"ok": False, "error": "BUILD_FAILED", "hint": f"{type(exc).__name__}: {exc}"}
    if not told.get("ok"):
        log(f"FAILED: {told.get('hint')}")
    out = JOB / "out"
    out.mkdir(exist_ok=True)
    (out / "build.json").write_text(json.dumps(told))
    return 0


if __name__ == "__main__":
    sys.exit(main())
