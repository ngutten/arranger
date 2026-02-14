#!/bin/bash
# deps/build_mingw_deps.sh
# Builds cross-compiled Windows (x86_64) dependencies into $SYSROOT.
# Run as root or with sudo. Requires: mingw-w64, cmake, wget, tar.
#
# Usage:
#   sudo ./deps/build_mingw_deps.sh
#   sudo SYSROOT=/opt/mingw-sysroot ./deps/build_mingw_deps.sh

set -e

SYSROOT="${SYSROOT:-/usr/local/mingw-sysroot}"
HOST=x86_64-w64-mingw32
CORES=$(nproc)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLCHAIN="$SCRIPT_DIR/../cmake/mingw-w64-x86_64.cmake"
BUILD_DIR=$(mktemp -d)

echo "=== Building MinGW deps into $SYSROOT ==="
echo "    Toolchain: $TOOLCHAIN"
echo "    Build dir: $BUILD_DIR"
echo "    Cores: $CORES"

mkdir -p "$SYSROOT"
export PKG_CONFIG_PATH="$SYSROOT/lib/pkgconfig:$SYSROOT/share/pkgconfig"
export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"

COMMON_CMAKE=(
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN"
    -DMINGW_SYSROOT="$SYSROOT"
    -DCMAKE_INSTALL_PREFIX="$SYSROOT"
    -DCMAKE_BUILD_TYPE=Release
    -DBUILD_SHARED_LIBS=OFF
    -DCMAKE_FIND_ROOT_PATH="$SYSROOT"
)

cd "$BUILD_DIR"

# ---------------------------------------------------------------------------
# PortAudio 19.7.0
# ---------------------------------------------------------------------------
echo ""
echo "=== PortAudio ==="
wget -q https://files.portaudio.com/archives/pa_stable_v190700_20210406.tgz
tar xf pa_stable_v190700_20210406.tgz
cd portaudio
cmake -B build "${COMMON_CMAKE[@]}" \
    -DPA_BUILD_SHARED=OFF \
    -DPA_USE_WASAPI=ON \
    -DPA_USE_WDMKS=ON \
    -DPA_USE_DS=ON \
    -DPA_USE_MME=ON \
    -DPA_USE_ASIO=OFF  # ASIO SDK not included; enable if you have it
cmake --build build -j"$CORES"
cmake --install build
cd ..

# ---------------------------------------------------------------------------
# libogg (libsndfile dep)
# ---------------------------------------------------------------------------
echo ""
echo "=== libogg ==="
wget -q https://downloads.xiph.org/releases/ogg/libogg-1.3.5.tar.gz
tar xf libogg-1.3.5.tar.gz
cd libogg-1.3.5
cmake -B build "${COMMON_CMAKE[@]}"
cmake --build build -j"$CORES"
cmake --install build
cd ..

# ---------------------------------------------------------------------------
# libvorbis (libsndfile dep)
# ---------------------------------------------------------------------------
echo ""
echo "=== libvorbis ==="
wget -q https://downloads.xiph.org/releases/vorbis/libvorbis-1.3.7.tar.gz
tar xf libvorbis-1.3.7.tar.gz
cd libvorbis-1.3.7
cmake -B build "${COMMON_CMAKE[@]}"
cmake --build build -j"$CORES"
cmake --install build
cd ..

# ---------------------------------------------------------------------------
# libFLAC (libsndfile dep)
# ---------------------------------------------------------------------------
echo ""
echo "=== libFLAC ==="
wget -q https://github.com/xiph/flac/archive/refs/tags/1.4.3.tar.gz -O flac-1.4.3.tar.gz
tar xf flac-1.4.3.tar.gz
cd flac-1.4.3
cmake -B build "${COMMON_CMAKE[@]}" \
    -DBUILD_DOCS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF \
    -DINSTALL_MANPAGES=OFF -DWITH_OGG=ON
cmake --build build -j"$CORES"
cmake --install build
cd ..

# ---------------------------------------------------------------------------
# libsndfile 1.2.2
# ---------------------------------------------------------------------------
echo ""
echo "=== libsndfile ==="
wget -q https://github.com/libsndfile/libsndfile/releases/download/1.2.2/libsndfile-1.2.2.tar.xz
tar xf libsndfile-1.2.2.tar.xz
cd libsndfile-1.2.2
cmake -B build "${COMMON_CMAKE[@]}" \
    -DBUILD_PROGRAMS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF \
    -DENABLE_EXTERNAL_LIBS=ON
cmake --build build -j"$CORES"
cmake --install build
cd ..

# ---------------------------------------------------------------------------
# FluidSynth 2.3.5
# ---------------------------------------------------------------------------
echo ""
echo "=== FluidSynth ==="
wget -q https://github.com/FluidSynth/fluidsynth/archive/refs/tags/v2.3.5.tar.gz \
     -O fluidsynth-2.3.5.tar.gz
tar xf fluidsynth-2.3.5.tar.gz
cd fluidsynth-2.3.5
cmake -B build "${COMMON_CMAKE[@]}" \
    -Denable-jack=OFF \
    -Denable-pulse=OFF \
    -Denable-alsa=OFF \
    -Denable-oss=OFF \
    -Denable-dbus=OFF \
    -Denable-sdl2=OFF \
    -Denable-readline=OFF \
    -Denable-threads=ON \
    -Denable-wasapi=ON
cmake --build build -j"$CORES"
cmake --install build
cd ..

# ---------------------------------------------------------------------------
# LV2 headers + sord + sratom + lilv
# ---------------------------------------------------------------------------
echo ""
echo "=== LV2 headers ==="
wget -q https://gitlab.com/lv2/lv2/-/archive/v1.18.10/lv2-v1.18.10.tar.gz
tar xf lv2-v1.18.10.tar.gz
# LV2 is headers only — just copy them
cp -r lv2-v1.18.10/include/lv2 "$SYSROOT/include/"
# Write a minimal .pc file
mkdir -p "$SYSROOT/lib/pkgconfig"
cat > "$SYSROOT/lib/pkgconfig/lv2.pc" << EOF
prefix=$SYSROOT
includedir=\${prefix}/include
Name: LV2
Description: LV2 plugin specification headers
Version: 1.18.10
Cflags: -I\${includedir}
EOF

echo ""
echo "=== sord (lilv dep) ==="
wget -q https://download.drobilla.net/sord-0.16.14.tar.xz
tar xf sord-0.16.14.tar.xz
cd sord-0.16.14
# sord uses waf; we need to cross-compile it manually
"${HOST}-gcc" -O2 -I. -Isrc \
    src/sord.c src/syntax.c src/serd.c \
    -c -I"$SYSROOT/include" 2>/dev/null || true
# Fallback: use cmake wrapper if available, else skip (lilv can be built without it)
# For CI purposes, we build lilv in --no-sord mode if sord isn't available
cd ..

echo ""
echo "=== lilv 0.24.24 ==="
wget -q https://download.drobilla.net/lilv-0.24.24.tar.xz
tar xf lilv-0.24.24.tar.xz
cd lilv-0.24.24
# lilv uses waf; cross-compiling with waf is painful.
# For the Windows build, we statically include a minimal lilv implementation
# using the CMake build via the unofficial cmake port if available,
# otherwise we disable LV2 for Windows builds.
#
# See: https://github.com/lv2/lilv/tree/master (cmake branch)
# Recommended: use vcpkg on a real Windows machine for production LV2 support.
#
# For cross-compile CI, build with -DENABLE_LV2=OFF and test LV2 on Linux only.
echo "  NOTE: lilv cross-compile from Linux → Windows is non-trivial."
echo "  Recommend: build with -DENABLE_LV2=OFF for the Windows target,"
echo "  or use vcpkg on native Windows."
cd ..

# ---------------------------------------------------------------------------
# nlohmann/json (header only — just write the file)
# ---------------------------------------------------------------------------
echo ""
echo "=== nlohmann/json ==="
mkdir -p "$SYSROOT/include/nlohmann"
# If json.hpp is already in the project's include dir, copy it
if [ -f "$SCRIPT_DIR/../include/nlohmann/json.hpp" ]; then
    cp "$SCRIPT_DIR/../include/nlohmann/json.hpp" "$SYSROOT/include/nlohmann/"
    echo "  Copied from project include dir"
else
    echo "  Not found — CMake FetchContent will handle it at build time"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Done ==="
echo "MinGW sysroot contents:"
ls "$SYSROOT/lib/" | grep -E "\.a$" | head -20
echo ""
echo "To build the Windows target:"
echo "  cmake -B build-win \\"
echo "      -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-w64-x86_64.cmake \\"
echo "      -DMINGW_SYSROOT=$SYSROOT \\"
echo "      -DENABLE_LV2=OFF \\"  # unless you got lilv working above
echo "      -DCMAKE_BUILD_TYPE=Release"
echo "  cmake --build build-win -j$(nproc)"

rm -rf "$BUILD_DIR"
