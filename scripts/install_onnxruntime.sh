#!/bin/bash
# install_onnxruntime.sh
# Downloads and installs ONNX Runtime C++ API for Linux (x86_64 or aarch64).
#
# Usage:
#   ./install_onnxruntime.sh              # CPU-only, install to /usr/local
#   ./install_onnxruntime.sh --gpu        # CUDA GPU build
#   ./install_onnxruntime.sh --prefix ~/ort  # custom install prefix
#
# After running, use in CMake with:
#   cmake -DONNXRUNTIME_ROOT=/usr/local ..   (or your --prefix)

set -euo pipefail

VERSION="1.24.2"
GPU=false
PREFIX="/usr/local"

while [[ $# -gt 0 ]]; do
    case $1 in
        --gpu)      GPU=true; shift;;
        --prefix)   PREFIX="$2"; shift 2;;
        --version)  VERSION="$2"; shift 2;;
        *)          echo "Unknown option: $1"; exit 1;;
    esac
done

ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  ARCH_TAG="x64";;
    aarch64) ARCH_TAG="aarch64";;
    *)       echo "Unsupported architecture: $ARCH"; exit 1;;
esac

if $GPU; then
    # GPU builds are named differently and include CUDA
    FILENAME="onnxruntime-linux-${ARCH_TAG}-gpu-${VERSION}.tgz"
else
    FILENAME="onnxruntime-linux-${ARCH_TAG}-${VERSION}.tgz"
fi

URL="https://github.com/microsoft/onnxruntime/releases/download/v${VERSION}/${FILENAME}"

echo "=== ONNX Runtime ${VERSION} installer ==="
echo "  Architecture: ${ARCH} (${ARCH_TAG})"
echo "  GPU:          ${GPU}"
echo "  Install to:   ${PREFIX}"
echo "  URL:          ${URL}"
echo

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "Downloading..."
wget -q --show-progress -O "${TMPDIR}/${FILENAME}" "${URL}"

echo "Extracting..."
tar xzf "${TMPDIR}/${FILENAME}" -C "${TMPDIR}"

# The tarball extracts to a directory like onnxruntime-linux-x64-1.24.2/
EXTRACTED=$(find "${TMPDIR}" -maxdepth 1 -type d -name 'onnxruntime-*' | head -1)
echo "Extracted to: ${EXTRACTED}"

# Install headers
echo "Installing headers to ${PREFIX}/include/onnxruntime..."
mkdir -p "${PREFIX}/include/onnxruntime"
cp -r "${EXTRACTED}/include/"* "${PREFIX}/include/onnxruntime/"

# Also copy headers to a flat location so #include <onnxruntime_cxx_api.h> works
cp "${EXTRACTED}/include/"*.h "${PREFIX}/include/" 2>/dev/null || true

# Install libraries (recursive to pick up cmake/ and pkgconfig/ subdirs)
echo "Installing libraries to ${PREFIX}/lib..."
mkdir -p "${PREFIX}/lib"
cp -a "${EXTRACTED}/lib/"* "${PREFIX}/lib/"

# Create pkg-config file if the tarball didn't ship one
if [[ ! -f "${PREFIX}/lib/pkgconfig/libonnxruntime.pc" ]]; then
    mkdir -p "${PREFIX}/lib/pkgconfig"
    cat > "${PREFIX}/lib/pkgconfig/libonnxruntime.pc" << EOF
prefix=${PREFIX}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: ONNX Runtime
Description: ONNX Runtime C/C++ inference engine
Version: ${VERSION}
Libs: -L\${libdir} -lonnxruntime
Cflags: -I\${includedir}
EOF
fi

# Create CMake config if the tarball didn't ship one
if [[ ! -f "${PREFIX}/lib/cmake/onnxruntime/onnxruntimeConfig.cmake" ]]; then
    mkdir -p "${PREFIX}/lib/cmake/onnxruntime"
    cat > "${PREFIX}/lib/cmake/onnxruntime/onnxruntimeConfig.cmake" << 'CMEOF'
# onnxruntimeConfig.cmake
# Provides: onnxruntime::onnxruntime imported target
#
# Usage in CMakeLists.txt:
#   find_package(onnxruntime REQUIRED)
#   target_link_libraries(my_target onnxruntime::onnxruntime)

get_filename_component(_ort_prefix "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)

if(NOT TARGET onnxruntime::onnxruntime)
    add_library(onnxruntime::onnxruntime SHARED IMPORTED)
    set_target_properties(onnxruntime::onnxruntime PROPERTIES
        IMPORTED_LOCATION "${_ort_prefix}/lib/libonnxruntime.so"
        INTERFACE_INCLUDE_DIRECTORIES "${_ort_prefix}/include"
    )
endif()

set(onnxruntime_FOUND TRUE)
set(ONNXRUNTIME_LIBRARIES onnxruntime::onnxruntime)
set(ONNXRUNTIME_INCLUDE_DIRS "${_ort_prefix}/include")
CMEOF

cat > "${PREFIX}/lib/cmake/onnxruntime/onnxruntimeConfigVersion.cmake" << CMEOF
set(PACKAGE_VERSION "${VERSION}")
if("\${PACKAGE_FIND_VERSION}" VERSION_GREATER "${VERSION}")
    set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
    set(PACKAGE_VERSION_COMPATIBLE TRUE)
    if("\${PACKAGE_FIND_VERSION}" VERSION_EQUAL "${VERSION}")
        set(PACKAGE_VERSION_EXACT TRUE)
    endif()
endif()
CMEOF
fi

# Update ldconfig if installing to /usr/local (needs root)
if [[ "${PREFIX}" == "/usr/local" ]] && command -v ldconfig &>/dev/null; then
    echo "Running ldconfig..."
    sudo ldconfig 2>/dev/null || ldconfig 2>/dev/null || true
fi

echo
echo "=== Done ==="
echo
echo "Verify with:"
echo "  pkg-config --libs --cflags libonnxruntime"
echo "  ls ${PREFIX}/include/onnxruntime_cxx_api.h"
echo
echo "In CMake, either:"
echo "  find_package(onnxruntime REQUIRED)     # uses the Config we just installed"
echo "  target_link_libraries(... onnxruntime::onnxruntime)"
echo
echo "Or set ONNXRUNTIME_ROOT=${PREFIX} and use the manual find in cmake_diffsinger.cmake"
