"""The job contract of an organisation's own service, as a few lines of Python.

A service that runs on an organisation's space (spec v2 §21) is started once a job with
`$AMSYOS_JOB` naming the job's folder. This module reads and writes that folder, so a
service need not know its layout:

    from amsyos import job

    text = job.params["text"]                      # the request's params
    picture = job.input("image")                   # an input file, or None
    job.input_name("image")                        # the name it was uploaded with, or None
    job.progress(40)                               # 0 to 100, optional
    job.output("result", ".txt").write_text(text)  # one file an output
    job.fail("the picture is too small")           # ends the job with a hint

Or a server (an image built with `serve`): started once, it loads what it needs and then
does the jobs one after the other, each in its own folder; `job` is the job now:

    from amsyos import job, serve

    model = load()                                 # once, not once a job

    def handle(job):
        job.output("result", ".txt").write_text(model(job.params["text"]))

    serve(handle)                                  # returns when the machine ends it

Each job is a line on stdin and its end a line on stdout (what `serve` reads and
writes): what the handler prints goes to stderr, the job's log. `job.fail` ends only the
job; an exception in the handler fails the job with its last line, and the server goes
on.

Only the standard library; it is on the PYTHONPATH of every job. The same surface for
Node is `require("amsyos")` (sdk/node), and for any other language the command
`amsyos-job` (sdk/bin), both there on every job too.
"""

import contextlib
import inspect
import json
import os
import re
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

LINE_MAX = 4096  # a line of the server's protocol, each way
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class _Failed(Exception):  # noqa: N818 - how `fail` ends a job, not the server
    pass


class Job:
    def __init__(self, folder: str | None = None) -> None:
        self.folder = Path(folder or os.environ.get("AMSYOS_JOB", "."))
        self._told: dict[str, Any] | None = None
        self._serving = False

    @property
    def told(self) -> dict[str, Any]:
        told = self._told
        if told is None:
            told = self._told = json.loads((self.folder / "job.json").read_text())
        return told

    @property
    def params(self) -> dict[str, Any]:
        """The request's params, as the manifest's inputs name them."""
        return self.told.get("params", {})

    def input(self, name: str) -> Path | None:
        """The file given for the input `name`, or None when none was."""
        rel = self.told.get("inputs", {}).get(name)
        return self.folder / rel if rel else None

    def input_name(self, name: str) -> str | None:
        """The name the file of the input `name` was uploaded with ("chair v3.glb"; the
        file itself is `in/<name>.<ext>`), or None when it is not known. A plain file
        name, at most 200 characters: no folder in it."""
        names: dict[str, str] = self.told.get("input_names") or {}
        return names.get(name)

    def output(self, name: str, suffix: str = "") -> Path:
        """Where to write the output `name`: one file, with the extension of its kind."""
        out = self.folder / "out"
        out.mkdir(exist_ok=True)
        if suffix and not suffix.startswith("."):
            suffix = f".{suffix}"  # "txt" is meant as ".txt"
        return out / f"{name}{suffix}"

    def progress(self, percent: float) -> None:
        (self.folder / "progress").write_text(f"{max(0.0, min(100.0, percent)):.0f}")

    def fail(self, hint: str, code: str = "INVALID_PARAMS") -> NoReturn:
        """End the job as failed; `hint` is what the caller reads to fix its request.
        In a server, the server goes on with the next job."""
        (self.folder / "error.json").write_text(json.dumps({"error": code, "hint": hint}))
        if self._serving:
            raise _Failed
        sys.exit(1)

    def serve(self, handler: Callable[..., object]) -> None:
        """Do the jobs the machine hands this process, one after the other, with
        `handler(job)` (or `handler()`), until the machine ends it."""
        protocol = os.fdopen(os.dup(1), "w", buffering=1)
        sys.stdout.flush()
        os.dup2(2, 1)  # what the handler prints is its output, not the protocol
        takes_job = _takes_one(handler)
        self._serving = True
        while line := sys.stdin.buffer.readline(LINE_MAX + 1):
            told = _job_line(line)
            if told is None:
                continue
            ident, folder = told
            self.folder, self._told = Path(folder), None
            _own_tmp(self.folder / "tmp")
            try:
                handler(self) if takes_job else handler()
            except _Failed:
                pass
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    self._failed(f"The service ended the job with exit code {exc.code}.")
            except Exception as exc:
                traceback.print_exc()
                self._failed(f"{type(exc).__name__}: {exc}"[:500])
            finally:
                with contextlib.suppress(Exception):
                    sys.stdout.flush()
                    sys.stderr.flush()
            protocol.write(json.dumps({"done": ident}) + "\n")

    def _failed(self, hint: str) -> None:
        error = self.folder / "error.json"
        if not error.exists():
            error.write_text(json.dumps({"error": "SERVICE_ERROR", "hint": hint}))


def _own_tmp(tmp: Path) -> None:
    """A served job's temporary files in its own folder (removed with it), as a job
    started as a command has them: `tempfile` and what the handler starts."""
    if tmp.is_dir():
        for name in ("TMPDIR", "TEMP", "TMP"):
            os.environ[name] = str(tmp)
        tempfile.tempdir = None  # found again from TMPDIR


def _takes_one(handler: Callable[..., object]) -> bool:
    try:
        return len(inspect.signature(handler).parameters) >= 1
    except (TypeError, ValueError):
        return True


def _job_line(line: bytes) -> tuple[str, str] | None:
    """(job id, folder) of the machine's line, or None for anything else."""
    if len(line) > LINE_MAX:
        return None
    try:
        told: object = json.loads(line)
    except ValueError:
        return None
    if not isinstance(told, dict):
        return None
    ident, folder = told.get("job"), told.get("folder")  # type: ignore[union-attr]
    if not (isinstance(ident, str) and _JOB_ID.fullmatch(ident) and isinstance(folder, str)):
        return None
    return ident, folder


job = Job()


def serve(handler: Callable[..., object]) -> None:
    """`job.serve(handler)`: a server's loop (an image built with `serve`)."""
    job.serve(handler)


__all__ = ["Job", "job", "serve"]
