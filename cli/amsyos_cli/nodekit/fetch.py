#!/usr/bin/env python3
# ruff: noqa: T201 - what it did is its output
"""amsyos-fetch on an own node, as on a rented one: a big download a card's `setup` makes,
once a machine, checked by sha256.

    amsyos-fetch URL --sha256 HEX --to PATH [--unpack] [--strip N]

The file is kept once under $AMSYOS_FETCH_STORE (amsyos deploy's build sets it), an archive
also unpacked once beside it; PATH gets hard links to it (a copy where links cannot go).
Python 3.8 and later, standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

STORE = Path(os.environ.get("AMSYOS_FETCH_STORE") or Path.home() / "amsyos-build" / "fetched")


def say(message: str) -> None:
    print(f"[amsyos-fetch] {message}", file=sys.stderr, flush=True)


def fetched(url: str, want: str) -> Path:
    kept = STORE / want
    if kept.is_file():
        say(f"on this machine already: {want[:12]}")
        return kept
    STORE.mkdir(parents=True, exist_ok=True)
    handle, part = tempfile.mkstemp(dir=str(STORE), prefix=".part-")
    os.close(handle)
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "amsyos-fetch/1"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=120) as got, open(part, "wb") as fh:  # noqa: S310
        while True:
            block = got.read(1 << 20)
            if not block:
                break
            digest.update(block)
            fh.write(block)
    if digest.hexdigest() != want:
        os.unlink(part)
        raise SystemExit(f"amsyos-fetch: its sha256 is {digest.hexdigest()}, not {want}")
    os.chmod(part, 0o444)
    os.replace(part, kept)
    say(f"fetched {kept.stat().st_size / 1e6:.1f} MB")
    return kept


def unpacked(archive: Path, want: str) -> Path:
    done = STORE / f"{want}.d"
    if done.is_dir():
        return done
    into = Path(tempfile.mkdtemp(dir=str(STORE), prefix=".unpack-"))
    top = str(into.resolve())
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            for info in zipped.infolist():
                target = (into / info.filename).resolve()
                if not str(target).startswith(top + os.sep):
                    raise SystemExit(f"amsyos-fetch: {info.filename} leaves the archive's folder")
                zipped.extract(info, str(into))
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    os.chmod(target, mode)
    else:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                target = (into / member.name).resolve()
                if not str(target).startswith(top + os.sep) or member.isdev():
                    raise SystemExit(f"amsyos-fetch: {member.name} is no plain file of its own")
                if member.issym() or member.islnk():
                    link = (target.parent / member.linkname).resolve()
                    if not str(link).startswith(top + os.sep):
                        raise SystemExit(f"amsyos-fetch: {member.name} links outside the archive")
            tar.extractall(str(into))  # noqa: S202 - every member checked above
    for root, _dirs, files in os.walk(into):
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, (os.stat(path).st_mode | 0o444) & ~0o222)
    os.replace(into, done)
    return done


def linked(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def main() -> None:
    parser = argparse.ArgumentParser(prog="amsyos-fetch")
    parser.add_argument("url")
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--to", required=True)
    parser.add_argument("--unpack", action="store_true")
    parser.add_argument("--strip", type=int, default=0)
    args = parser.parse_args()
    want, to = args.sha256.lower(), Path(args.to)
    if not to.parent.is_dir():
        raise SystemExit(f"amsyos-fetch: no folder {to.parent}")
    kept = fetched(args.url, want)
    if not args.unpack:
        linked(kept, to)
        say(f"linked to {to}")
        return
    tree = unpacked(kept, want)
    for root, dirs, files in os.walk(tree):
        rel = Path(root).relative_to(tree).parts
        for name in dirs + files:
            parts = (*rel, name)
            if len(parts) <= args.strip:
                continue
            source, target = Path(root) / name, to.joinpath(*parts[args.strip :])
            if source.is_symlink():
                if not target.exists() and not target.is_symlink():
                    os.symlink(os.readlink(source), target)
            elif source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                linked(source, target)
    say(f"unpacked into {to}")


if __name__ == "__main__":
    main()
