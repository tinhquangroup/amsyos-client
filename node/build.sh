#!/usr/bin/env bash
# Builds the AMSYOS node program for Windows x86_64 and Linux x86_64 (static, musl) in Docker:
#   node/dist/amsyos-node.exe  and  node/dist/amsyos-node
# Nothing is installed on the host. The cargo registry is kept in the volume amsyos-node-cargo.
#   node/build.sh            both targets
#   node/build.sh linux      one of them (linux | windows)
#   node/build.sh shell      a shell in the build image, the crate at /src
set -euo pipefail

RUST_IMAGE="rust:1.99.0-bookworm"           # rustc 1.99.0 (2026-09-28)
BUILD_IMAGE="amsyos-node-build:1.99.0"
HERE="$(cd "$(dirname "$0")" && pwd)"
WHAT="${1:-all}"

if ! docker image inspect "$BUILD_IMAGE" >/dev/null 2>&1; then
  docker build -t "$BUILD_IMAGE" - <<EOF
FROM $RUST_IMAGE
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc-mingw-w64-x86-64 musl-tools \
 && rm -rf /var/lib/apt/lists/* \
 && rustup target add x86_64-pc-windows-gnu x86_64-unknown-linux-musl
EOF
fi

run() {
  docker run --rm -v "$HERE":/src -w /src \
    -v amsyos-node-cargo:/usr/local/cargo/registry \
    -e CARGO_TERM_COLOR=never "$BUILD_IMAGE" "$@"
}

if [ "$WHAT" = "shell" ]; then
  docker run --rm -it -v "$HERE":/src -w /src -v amsyos-node-cargo:/usr/local/cargo/registry \
    "$BUILD_IMAGE" bash
  exit 0
fi

mkdir -p "$HERE/dist"
targets=()
case "$WHAT" in
  all) targets=(x86_64-unknown-linux-musl x86_64-pc-windows-gnu) ;;
  linux) targets=(x86_64-unknown-linux-musl) ;;
  windows) targets=(x86_64-pc-windows-gnu) ;;
  *) echo "usage: $0 [all|linux|windows|shell]" >&2; exit 2 ;;
esac

for t in "${targets[@]}"; do
  run cargo build --release --locked --target "$t"
done

# Copy out as the host user, so dist/ is not owned by root.
for t in "${targets[@]}"; do
  case "$t" in
    *windows*) src="target/$t/release/amsyos-node.exe"; dst="dist/amsyos-node.exe" ;;
    *) src="target/$t/release/amsyos-node"; dst="dist/amsyos-node" ;;
  esac
  cp "$HERE/$src" "$HERE/$dst"
  echo "$dst: $(stat -c %s "$HERE/$dst") bytes"
done
