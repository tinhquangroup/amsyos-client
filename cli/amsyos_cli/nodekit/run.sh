#!/bin/sh
# A job of a service `amsyos deploy` built on this node (where: node). node.deploy's command
# (and serve) is `sh _node/run.sh <its image folder> '<the card's command>'`: the command runs
# in the image folder that build.py made, with its environment first on PATH. Hugging Face's
# cache is read offline; caches and temporary files stay in the job's own folder.
# $ENV_DIR/lib comes first on LD_LIBRARY_PATH, before what was there (a customer, 2026-11-26:
# a program a build's setup ran found no libQt5Core.so.5 of the card's `system`, which only
# the environment's own programs find by their RPATH; build.py's setup has the same). A system program the job calls then loads
# the environment's copy of a library both have: git, bash, tar and python3 run as before,
# curl warns "no version information available" and runs. Start a program that must keep the
# system's libraries with LD_LIBRARY_PATH unset, for that process alone.
set -e
IMG=$1
. "$IMG/_node/where.env"
J="${AMSYOS_JOB:-$PWD}"
mkdir -p "$J/tmp/cache" "$J/tmp/home"
export PATH="$ENV_DIR/bin:$IMG/_node/bin:$PATH" CONDA_PREFIX="$ENV_DIR" \
  LD_LIBRARY_PATH="$ENV_DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  HF_HOME="$HF_DIR" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 AMSYOS_FETCH_STORE="$FETCHED" \
  TMPDIR="$J/tmp" XDG_CACHE_HOME="$J/tmp/cache" MPLCONFIGDIR="$J/tmp/cache/mpl" \
  NUMBA_CACHE_DIR="$J/tmp/cache/numba" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
[ -w "${HOME:-/nonexistent}" ] || export HOME="$J/tmp/home"
cd "$IMG"
exec /bin/sh -c "$2"
