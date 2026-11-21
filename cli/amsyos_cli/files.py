"""Files in and out: PUT to the upload URL `asset.upload` gives, GET the signed URLs a
result gives into `./out/<job>/`. What a file is (name, size, type, hash) is printed, never
what is in it (spec v3 §12)."""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from amsyos_cli import __version__
from amsyos_cli.api import Api
from amsyos_cli.common import FACE_NOTE, CliError, Out, obj, seq

MOST_FILES = 20  # asset.upload takes at most 20 names a call
HAS_MOST = 180  # asset.has takes at most 180 digests a call
LOG_MOST_BYTES = 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
_HEADERS = {"User-Agent": f"amsyos-cli/{__version__}"}


@dataclass
class Saved:
    output: str
    path: Path
    size: int
    type: str
    sha256: str

    def row(self, root: Path | None = None) -> dict[str, Any]:
        shown = self.path
        if root is not None:
            try:
                shown = self.path.relative_to(root)
            except ValueError:
                shown = self.path
        return {
            "output": self.output,
            "file": str(shown),
            "size": self.size,
            "type": self.type,
            "sha256": self.sha256,
        }


def human(size: int) -> str:
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{size} B"


def kept_for(seconds: Any) -> str:
    """How long AMSYOS keeps a file still (`kept_s`): `23 h`, `40 min`, `3 days` (Claude
    Code, 2026-11-20: "kept 86153s")."""
    if not isinstance(seconds, int | float) or isinstance(seconds, bool):
        return str(seconds)
    left = max(0, int(seconds))
    if left < 3600:
        return f"{max(1, round(left / 60))} min" if left else "0 min"
    if left < 48 * 3600:
        return f"{round(left / 3600)} h"
    return f"{round(left / 86400)} days"


def face_note(rows: list[dict[str, Any]], out: Out) -> list[str]:
    """`files ls`: what the mark `face` means, once, as the list's last line on stdout, and
    not on stderr as `Out.marks` says it elsewhere (a customer, 2026-11-20: a host that shows
    stderr above the answer and again below it printed the note twice)."""
    out.face_told = True  # the rows' `marks face` stay; the stderr note is not said
    faced = any("face" in [str(m) for m in seq(r.get("marks"))] for r in rows)
    return [FACE_NOTE] if faced else []


def safe_name(name: str, fallback: str) -> str:
    """A file name of this folder only: no path, no leading dot, no odd characters."""
    name = urllib.parse.unquote(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE.sub("_", name).strip(" .")[:120]
    return name or fallback


async def upload(api: Api, paths: list[Path]) -> list[dict[str, Any]]:
    """Each local file as an asset of the workspace: `[{file, asset, size, sha256,
    reused}]`. Contents the workspace already holds are not sent again (`asset.has`, spec
    v3 §8.2); a Gateway without it gets every file, as before."""
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise CliError("INVALID_INPUT", f"not a file: {', '.join(missing[:5])}")
    digests = [sha256_of(p) for p in paths]
    have = await held(api, sorted(set(digests)))
    done: dict[int, dict[str, Any]] = {}
    for i, (path, digest) in enumerate(zip(paths, digests, strict=True)):
        if digest in have:
            size = path.stat().st_size
            done[i] = {"file": str(path), "asset": have[digest], "size": size, "reused": True}
    rest = [i for i in range(len(paths)) if i not in done]
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0), headers=_HEADERS) as http:
        for start in range(0, len(rest), MOST_FILES):
            batch = rest[start : start + MOST_FILES]
            names = [safe_name(paths[i].name, f"file{n}") for n, i in enumerate(batch)]
            if len(set(names)) < len(names):  # two files of one name: each named apart
                names = [f"{n}_{name}" for n, name in enumerate(names)]
            if len(batch) == 1:
                answer = await api.instant("asset.upload", {"name": names[0]})
                links = {names[0]: _url(answer.get("outputs"), "upload_url")}
            else:
                answer = await api.instant("asset.upload", {"names": "\n".join(names)})
                outputs: Any = answer.get("outputs") or {}
                links = {n: _url(outputs, n) for n in names}
            for i, name in zip(batch, names, strict=True):
                done[i] = {**await _put(http, paths[i], links[name]), "reused": False}
    return [{**done[i], "sha256": digests[i]} for i in range(len(paths))]


async def held(api: Api, digests: list[str]) -> dict[str, str]:
    """`asset.has`: which of these contents the workspace holds, as sha256 → `asset://…`.
    Empty when the Gateway has no `asset.has` yet (said with --verbose)."""
    found: dict[str, str] = {}
    # Its `sha256` is text: digests a line each, at most 180 a call.
    for start in range(0, len(digests), HAS_MOST):
        batch = digests[start : start + HAS_MOST]
        if "asset.has" in api.missing:
            break
        try:
            told = await api.maybe("asset.has", {"sha256": "\n".join(batch)})
        except CliError as exc:
            api.out.debug(f"asset.has refused ({exc.code}: {exc.hint}): every file is sent")
            return found
        if told is None:
            api.out.debug("every file is sent (no asset.has here)")
            return found
        found.update(held_of(told, set(batch)))
    if digests:
        api.out.debug(f"asset.has: {len(found)} of {len(digests)} already in the workspace")
    return found


def held_of(told: Any, want: set[str]) -> dict[str, str]:
    """sha256 → `asset://…` wherever `asset.has` says it: `{sha: ref}`, `{sha: {asset}}`,
    `[{sha256, asset}]`, under any key. A content held without a reference is sent again."""
    found: dict[str, str] = {}

    def ref_of(value: Any) -> str | None:
        if isinstance(value, list) and value:
            value = value[0]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, dict):
            entry = obj(value)
            value = entry.get("asset") or entry.get("ref")
        return value if isinstance(value, str) and value.startswith("asset://") else None

    def visit(node: Any, depth: int) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            entry = obj(node)
            sha = entry.get("sha256")
            if isinstance(sha, str) and sha in want and (ref := ref_of(entry)):
                found[sha] = ref
            for key, value in entry.items():
                if key in want:
                    if ref := ref_of(value):
                        found[key] = ref
                else:
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for item in node:  # pyright: ignore[reportUnknownVariableType]
                visit(item, depth + 1)

    visit(told, 0)
    return found


def _url(outputs: Any, key: str) -> str:
    url = obj(outputs).get(key)
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise CliError("BAD_ANSWER", f"asset.upload gave no URL for '{key}'")
    return url


async def _put(http: httpx.AsyncClient, path: Path, url: str) -> dict[str, Any]:
    size = path.stat().st_size

    async def body() -> AsyncIterator[bytes]:
        with path.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                yield chunk

    response = await http.put(
        url,
        content=body(),
        headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
    )
    if response.status_code >= 400:
        raise CliError(*_refusal(response, f"uploading {path.name}"))
    asset = obj(response.json()).get("asset")
    if not isinstance(asset, str):
        raise CliError("BAD_ANSWER", f"the upload of {path.name} named no asset")
    return {"file": str(path), "asset": asset, "size": size}


def _refusal(response: httpx.Response, doing: str) -> tuple[str, str]:
    try:
        told: Any = response.json()
    except ValueError:
        told = None
    said = obj(told)
    if "error" in said:
        return str(said["error"]), f"{doing}: {said.get('hint') or ''}"
    return "TRANSFER_FAILED", f"{doing}: HTTP {response.status_code}; try again"


async def download(outputs: dict[str, Any], folder: Path) -> list[Saved]:
    """Each output's signed URL into `folder`, named as the URL names the file."""
    folder.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    saved: list[Saved] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0), headers=_HEADERS) as http:
        for output, url in outputs.items():
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            name = safe_name(urllib.parse.urlsplit(url).path, output)
            if name in used:
                name = f"{output}_{name}"
            used.add(name)
            saved.append(await _get(http, output, url, folder / name))
    return saved


async def _get(http: httpx.AsyncClient, output: str, url: str, target: Path) -> Saved:
    digest = hashlib.sha256()
    size = 0
    tmp = target.with_name(target.name + ".part")
    async with http.stream("GET", url) as response:
        if response.status_code >= 400:
            await response.aread()
            raise CliError(*_refusal(response, f"fetching output '{output}'"))
        kind = response.headers.get("content-type", "application/octet-stream").split(";")[0]
        with tmp.open("wb") as fh:
            async for chunk in response.aiter_bytes(1024 * 1024):
                fh.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    tmp.replace(target)
    return Saved(output, target, size, kind, digest.hexdigest())


async def fetch_text(url: str) -> str:
    """A job's log, at most 1 MB: its end when longer."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), headers=_HEADERS) as http:
        response = await http.get(url)
        if response.status_code >= 400:
            raise CliError(*_refusal(response, "fetching the log"))
        body = response.content[-LOG_MOST_BYTES:]
    return body.decode("utf-8", errors="replace")


def describe_saved(saved: list[Saved], root: Path) -> list[str]:
    lines: list[str] = []
    for s in saved:
        try:
            shown = s.path.relative_to(root)
        except ValueError:
            shown = s.path
        lines.append(f"  {s.output}: {shown}  {human(s.size)}  {s.type}  sha256 {s.sha256[:12]}")
    return lines


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
