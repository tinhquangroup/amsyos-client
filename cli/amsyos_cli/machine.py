"""What this computer has, against what a card's service needs (the owner, 2026-11-20):
before `amsyos run --local` runs a service here, the AI client's machine is checked, and
one that is not strong enough sends the work to a rented machine (the space) instead.

`probe()` reads the machine with the standard library and `nvidia-smi` when there is one:
cores, memory, free disk, CUDA GPUs and their memory. `fits()` compares it with the card's
`resources` (`gpu: none|optional|required`, `min_vram_gb`, `cpu`, `ram_gb`, `disk_gb`).
Nothing is sent anywhere.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from amsyos_cli.common import obj, seq

DISK_MARGIN_GB = 1.0  # beside what the job declares: its folder, the program's own files
GPU_PRICE_NOTE = "a GPU space"
CPU_PRICE_NOTE = "a space with no GPU (--gpu none)"


def _ram_gb() -> tuple[float | None, float | None]:
    """Total and available memory in GB, or None where it cannot be read."""
    system = platform.system()
    try:
        if system == "Linux":
            seen: dict[str, float] = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    seen[key] = float(rest.split()[0]) / 1024 / 1024
            return seen.get("MemTotal"), seen.get("MemAvailable")
        if system == "Darwin":
            told = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5, check=False,
            )  # fmt: skip
            return (float(told.stdout.strip()) / 1024**3, None) if told.returncode == 0 else (
                None, None)  # fmt: skip
        if system == "Windows":

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]  # fmt: skip

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return status.ullTotalPhys / 1024**3, status.ullAvailPhys / 1024**3
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        pass
    return None, None


def _gpus() -> list[dict[str, Any]]:
    """CUDA GPUs `nvidia-smi` lists: name and memory (GB). None found: an empty list."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return []
    try:
        told = subprocess.run(  # noqa: S603 - nvidia-smi found on PATH, fixed arguments
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[dict[str, Any]] = []
    for line in told.stdout.splitlines() if told.returncode == 0 else []:
        name, _, mib = line.rpartition(",")
        try:
            found.append({"name": name.strip(), "vram_gb": round(float(mib) / 1024, 1)})
        except ValueError:
            continue
    return found


def probe(where: Path | None = None) -> dict[str, Any]:
    """This computer: system, cores, memory, free disk where the job folder goes, GPUs."""
    total, free = _ram_gb()
    try:
        disk = shutil.disk_usage(where or Path.cwd()).free / 1024**3
    except OSError:
        disk = None
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    return {
        "system": f"{platform.system()} {platform.release()}".strip(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "cpus": os.cpu_count(),
        "ram_gb": round(total, 1) if total is not None else None,
        "ram_free_gb": round(free, 1) if free is not None else None,
        "disk_free_gb": round(disk, 1) if disk is not None else None,
        "gpus": _gpus(),
        **({"note": "Apple GPU: CUDA code does not run on it"} if apple else {}),
    }


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def fits(card: dict[str, Any], machine: dict[str, Any]) -> dict[str, Any]:
    """Whether this machine can run the card's service as its `resources` say: `fits`,
    what it `lacks` (each with the figure), `notes`, and the space to use instead."""
    need = obj(card.get("resources"))
    gpu = str(need.get("gpu") or "none")
    vram = _number(need.get("min_vram_gb")) or _number(need.get("vram_gb"))
    lacks: list[str] = []
    notes: list[str] = []
    gpus = [obj(g) for g in seq(machine.get("gpus"))]
    best = max((float(g.get("vram_gb") or 0.0) for g in gpus), default=0.0)
    if gpu == "required":
        if not gpus:
            lacks.append("a CUDA GPU (none found: nvidia-smi)")
        elif vram is not None and best < vram:
            lacks.append(f"a GPU with {vram:g} GB (the largest here has {best:g} GB)")
    elif gpu == "optional" and not gpus:
        notes.append("no CUDA GPU here: it runs on the CPU, slower than on a GPU space")
    for key, have, unit in (
        ("cpu", machine.get("cpus"), " cores"),
        ("ram_gb", machine.get("ram_gb"), " GB of memory"),
    ):
        wanted = _number(need.get(key))
        if wanted is not None and have is not None and have < wanted:
            lacks.append(f"{wanted:g}{unit} (this machine has {have:g})")
    free = machine.get("ram_free_gb")
    wanted_ram = _number(need.get("ram_gb"))
    if wanted_ram is not None and free is not None and free < wanted_ram and not lacks:
        notes.append(f"only {free:g} GB of memory is free now (it needs {wanted_ram:g})")
    disk = _number(need.get("disk_gb"))
    room = machine.get("disk_free_gb")
    if disk is not None and room is not None and room < disk + DISK_MARGIN_GB:
        lacks.append(f"{disk + DISK_MARGIN_GB:g} GB of free disk (this machine has {room:g})")
    space = CPU_PRICE_NOTE if gpu == "none" else GPU_PRICE_NOTE
    return {"fits": not lacks, "lacks": lacks, "notes": notes, "space": space, "gpu": gpu}


def too_small_hint(service: str, judged: dict[str, Any]) -> str:
    """What to do when this machine cannot run the service: its space, said with the cost
    `deploy --dry-run` shows first."""
    return (
        f"this machine is not strong enough for {service or 'this service'}: it lacks "
        f"{'; '.join(judged['lacks'])}. Run it on a rented machine instead: `amsyos deploy "
        f"--dry-run` says the space ({judged['space']}) and the credit its hours hold, then "
        "`amsyos deploy`. Or `amsyos run --local --force` to try here anyway."
    )


def lines(machine: dict[str, Any], judged: dict[str, Any] | None) -> list[str]:
    """`amsyos machine` in text."""
    gpus = ", ".join(
        f"{g.get('name')} {g.get('vram_gb')} GB" for g in (obj(x) for x in seq(machine.get("gpus")))
    )
    said = [
        f"this machine: {machine.get('system')}, {machine.get('cpus') or '?'} cores, "
        f"{machine.get('ram_gb') or '?'} GB memory ({machine.get('ram_free_gb') or '?'} free), "
        f"{machine.get('disk_free_gb') or '?'} GB free disk, GPU: {gpus or 'none (CUDA)'}",
    ]
    if machine.get("note"):
        said.append(f"  {machine['note']}")
    if judged is None:
        said.append("no amsyos.yaml here: `amsyos machine` in a repository says whether it fits")
        return said
    if judged["fits"]:
        said.append("fits the card's resources: `amsyos run --local` runs it here, free")
    else:
        said.append("does not fit the card's resources: " + "; ".join(judged["lacks"]))
        said.append(
            f"use a rented machine: `amsyos deploy --dry-run` ({judged['space']}), then "
            "`amsyos deploy`"
        )
    said.extend(f"  note: {n}" for n in judged["notes"])
    return said
