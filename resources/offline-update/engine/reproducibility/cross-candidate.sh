#!/bin/bash
set -euo pipefail
BASE=/mnt/e/MINGDA-3DPrinting-Cloud/tmp/swupdate-arm64-emulated-20261023
SYSROOT="$BASE/rootfs"
OUTPUT="$BASE/cross-candidate-r2"
chmod 0755 "$BASE/cc-cross"
mkdir -p "$OUTPUT/source" "$OUTPUT/build" "$OUTPUT/release/lib"
"$BASE/cc-cross" --sysroot="$SYSROOT" -L"$SYSROOT/usr/lib/aarch64-linux-gnu" -L"$SYSROOT/usr/lib/gcc/aarch64-linux-gnu/11" -shared -fPIC -O2 -std=gnu99 '-DVERSION="0.3.5"' -Wl,-soname,libubootenv.so.0 -I"$BASE/libubootenv-source/src" "$BASE/libubootenv-source/src/uboot_env.c" -lz -lyaml -o "$OUTPUT/release/lib/libubootenv.so.0"
cp "$BASE/libubootenv-source/src/libuboot.h" "$OUTPUT/release/"
git -C /mnt/e/MINGDA-3DPrinting-Cloud/tmp/swupdate-2026.05-source archive --format=tar.gz HEAD > "$OUTPUT/release/swupdate-source-2026.05.tar.gz"
tar -xzf "$OUTPUT/release/swupdate-source-2026.05.tar.gz" -C "$OUTPUT/source"
cp "$BASE/build-tools/swupdate.config" "$OUTPUT/build/.config"
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"
export PKG_CONFIG_LIBDIR="$SYSROOT/usr/lib/aarch64-linux-gnu/pkgconfig:$SYSROOT/usr/share/pkgconfig"
export EXTRA_CFLAGS="--sysroot=$SYSROOT -I$OUTPUT/release"
export EXTRA_LDFLAGS="--sysroot=$SYSROOT -L$OUTPUT/release/lib -L$SYSROOT/usr/lib/aarch64-linux-gnu -L$SYSROOT/usr/lib/gcc/aarch64-linux-gnu/11"
ln -sf libubootenv.so.0 "$OUTPUT/release/lib/libubootenv.so"
make -C "$OUTPUT/source" O="$OUTPUT/build" CROSS_COMPILE=aarch64-linux-gnu- CC="$BASE/cc-cross" LD="$BASE/cc-cross" olddefconfig
python3 "$BASE/build-tools/engine-receipt.py" --check-config "$OUTPUT/build/.config"
make -C "$OUTPUT/source" O="$OUTPUT/build" CROSS_COMPILE=aarch64-linux-gnu- CC="$BASE/cc-cross" LD="$BASE/cc-cross" -j4 swupdate
cp "$OUTPUT/build/swupdate" "$OUTPUT/release/swupdate"
cp "$OUTPUT/build/.config" "$OUTPUT/release/swupdate.config"
readelf -h "$OUTPUT/release/swupdate" | head -20
readelf -V "$OUTPUT/release/swupdate" | grep GLIBC_ | sort -u
"$BASE/hosttools/usr/bin/qemu-aarch64-static" -L "$SYSROOT" -E LD_LIBRARY_PATH="$OUTPUT/release/lib:$SYSROOT/usr/lib/aarch64-linux-gnu" "$OUTPUT/release/swupdate" --version
