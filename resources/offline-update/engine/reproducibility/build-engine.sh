#!/usr/bin/env bash
# Build only on a development Linux machine. This script never installs system
# packages, starts a service, or talks to printer hardware.
set -euo pipefail

PINNED_COMMIT=602edf8e57f940791034096c9e24206097b4b7a6
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE=""
OUTPUT=""
ALLOW_HOST_BUILD=false
JOBS=2

usage() {
    printf 'Usage: bash build-engine.sh --source SWUPDATE_GIT --output NEW_DIRECTORY [--jobs N] [--allow-host-build]\n'
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --source) SOURCE="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --jobs) JOBS="$2"; shift 2 ;;
        --allow-host-build) ALLOW_HOST_BUILD=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[ -n "$SOURCE" ] && [ -n "$OUTPUT" ] || { usage >&2; exit 2; }
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid job count' >&2; exit 2; }
[ "$(uname -s)" = Linux ] || { echo 'A Linux development environment is required' >&2; exit 2; }
ARCH="$(uname -m)"
if [ "$ALLOW_HOST_BUILD" != true ]; then
    [ "$ARCH" = aarch64 ] || { echo 'Production build requires native aarch64; host tests use --allow-host-build' >&2; exit 2; }
    # Source this fixed OS metadata only; never source a package-supplied file.
    . /etc/os-release
    [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 22.04 ] || {
        echo 'Production baseline is Ubuntu 22.04 ARM64' >&2; exit 2;
    }
fi
for tool in git make cc python3 pkg-config tar sha256sum readelf; do
    command -v "$tool" >/dev/null || { echo "Missing development tool: $tool" >&2; exit 2; }
done
SOURCE="$(realpath "$SOURCE")"
OUTPUT="$(realpath -m "$OUTPUT")"
case "$SOURCE $OUTPUT" in *$'\n'*|*$'\t'*) echo 'Unsupported build path' >&2; exit 2 ;; esac
# Upstream make uses unquoted O=/srctree substitutions.
case "$SOURCE" in *' '*) echo 'Source path must not contain spaces' >&2; exit 2 ;; esac
case "$OUTPUT" in *' '*) echo 'Output path must not contain spaces' >&2; exit 2 ;; esac
[ ! -e "$OUTPUT" ] || { echo 'Output must be a new directory; existing builds are never overwritten' >&2; exit 2; }
[ "$(git -C "$SOURCE" rev-parse HEAD)" = "$PINNED_COMMIT" ] || {
    echo 'Source checkout is not the pinned SWUpdate 2026.05 commit' >&2; exit 2;
}

# Only a new, explicitly requested output directory is written. A git archive
# also avoids CRLF working-tree conversion from Windows checkouts.
mkdir -p "$OUTPUT/source" "$OUTPUT/build" "$OUTPUT/release"
git -C "$SOURCE" archive --format=tar.gz HEAD > "$OUTPUT/release/swupdate-source-2026.05.tar.gz"
tar -xzf "$OUTPUT/release/swupdate-source-2026.05.tar.gz" -C "$OUTPUT/source"
export GIT_CEILING_DIRECTORIES="$OUTPUT/source"
cp "$SCRIPT_DIR/swupdate.config" "$OUTPUT/build/.config"

# Native dependency check. The public libubootenv API probe catches old Ubuntu
# packages lacking namespaces even when their pkg-config file is present.
cat > "$OUTPUT/build/dependency-probe.c" <<'EOF'
#include <openssl/ssl.h>
#include <json-c/json.h>
#include <libconfig.h>
#include <libuboot.h>
#include <zlib.h>
int main(void) {
    void *a = (void *)&libuboot_read_config_ext;
    void *b = (void *)&libuboot_get_namespace;
    return a == b;
}
EOF
# CFLAGS/LDFLAGS/LDLIBS may point to a development-only dependency prefix. They
# are deliberately word-split as compiler arguments, never evaluated as shell.
# shellcheck disable=SC2086
cc ${CFLAGS:-} "$OUTPUT/build/dependency-probe.c" ${LDFLAGS:-} \
    -lssl -lcrypto -ljson-c -lconfig -lubootenv -lz ${LDLIBS:-} \
    -o "$OUTPUT/build/dependency-probe" || {
    echo 'Build dependencies missing or libubootenv API too old; see docs/SWUpdate-engine-build.md' >&2
    exit 2
}

make -C "$OUTPUT/source" O="$OUTPUT/build" olddefconfig
python3 "$SCRIPT_DIR/engine-receipt.py" --check-config "$OUTPUT/build/.config"
make -C "$OUTPUT/source" O="$OUTPUT/build" -j"$JOBS" swupdate
install -m 0755 "$OUTPUT/build/swupdate" "$OUTPUT/release/swupdate"
install -m 0644 "$OUTPUT/build/.config" "$OUTPUT/release/swupdate.config"
cp "$OUTPUT/source/COPYING" "$OUTPUT/release/COPYING.swupdate"
cp -a "$OUTPUT/source/LICENSES" "$OUTPUT/release/LICENSES"
cp "$SCRIPT_DIR/build-engine.sh" "$SCRIPT_DIR/engine-receipt.py" "$OUTPUT/release/"
"$OUTPUT/release/swupdate" --version > "$OUTPUT/release/version.txt"
ldd "$OUTPUT/release/swupdate" > "$OUTPUT/release/runtime-libraries.txt"
if grep -q 'not found' "$OUTPUT/release/runtime-libraries.txt"; then
    echo 'Runtime library unresolved; this is not a deployable engine' >&2
    exit 2
fi
python3 "$SCRIPT_DIR/engine-receipt.py" --binary "$OUTPUT/release/swupdate" \
    --config "$OUTPUT/release/swupdate.config" --output "$OUTPUT/release/build-info.json" \
    --arch "$ARCH" --development-build "$ALLOW_HOST_BUILD"
printf 'Built engine and source/license receipt in %s/release\n' "$OUTPUT"
