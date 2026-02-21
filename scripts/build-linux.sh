#!/bin/bash
# scripts/build-linux.sh
#
# Local Linux build script — mirrors the GitHub Actions Linux CI job.
# Produces dist/arranger/ (PyInstaller bundle) and optionally an AppImage.
#
# Usage:
#   ./scripts/build-linux.sh            # build bundle only
#   ./scripts/build-linux.sh --appimage # also create AppImage
#
# Prerequisites (Ubuntu/Debian):
#   sudo apt-get install \
#     build-essential cmake pkg-config python3-dev \
#     libportaudio2 libportaudio-dev \
#     libfluidsynth-dev libsndfile1-dev \
#     ffmpeg librsvg2-bin wget
#   pip install -r requirements.txt pyinstaller

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

BUILD_APPIMAGE=0
for arg in "$@"; do
  [ "$arg" = "--appimage" ] && BUILD_APPIMAGE=1
done

echo "=== Arranger Linux build ==="
echo "    Project root: $ROOT"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Build C++ engine and plugins
# ---------------------------------------------------------------------------
echo "--- Building C++ engine (arranger_engine.so) and plugins ---"
cd "$ROOT/audio_server"
cmake -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_PYTHON_BINDINGS=ON \
    -DENABLE_LV2=OFF \
    -DENABLE_TESTS=OFF
cmake --build build -j"$(nproc)"
cd "$ROOT"

echo ""
echo "Build outputs:"
ls standalone/arranger_engine*.so 2>/dev/null && echo "  ✓ arranger_engine.so" || echo "  ✗ arranger_engine.so MISSING"
plugin_count=$(ls plugins/arranger_plugin_*.so 2>/dev/null | wc -l)
echo "  ✓ $plugin_count plugins in plugins/"

# ---------------------------------------------------------------------------
# Step 2: Download bundled ffmpeg (optional but recommended)
# ---------------------------------------------------------------------------
if [ ! -f "$ROOT/packaging/ffmpeg/ffmpeg" ]; then
    echo ""
    echo "--- Downloading static ffmpeg ---"
    mkdir -p "$ROOT/packaging/ffmpeg"
    wget -q "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" \
        -O /tmp/ffmpeg-static.tar.xz
    tar -xf /tmp/ffmpeg-static.tar.xz --strip-components=1 \
        --wildcards --no-anchored 'ffmpeg' \
        -C "$ROOT/packaging/ffmpeg/"
    chmod +x "$ROOT/packaging/ffmpeg/ffmpeg"
    echo "  ffmpeg downloaded: $($ROOT/packaging/ffmpeg/ffmpeg -version 2>&1 | head -1)"
else
    echo "  ffmpeg already present, skipping download"
fi

# ---------------------------------------------------------------------------
# Step 3: PyInstaller bundle
# ---------------------------------------------------------------------------
echo ""
echo "--- Running PyInstaller ---"
cd "$ROOT"
pyinstaller packaging/arranger.spec

echo ""
echo "Bundle created: dist/arranger/"
echo "  Size: $(du -sh dist/arranger/ | cut -f1)"

# ---------------------------------------------------------------------------
# Step 4: AppImage (optional)
# ---------------------------------------------------------------------------
if [ "$BUILD_APPIMAGE" -eq 1 ]; then
    echo ""
    echo "--- Creating AppImage ---"

    # Download appimagetool if not already present
    if [ ! -f /tmp/appimagetool ]; then
        wget -q "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage" \
            -O /tmp/appimagetool
        chmod +x /tmp/appimagetool
    fi

    VERSION="${ARRANGER_VERSION:-dev}"
    APPIMAGE_PATH="$ROOT/arranger-${VERSION}-linux-x86_64.AppImage"

    rm -rf "$ROOT/AppDir"
    mkdir -p "$ROOT/AppDir"
    cp -r "$ROOT/dist/arranger/." "$ROOT/AppDir/"

    # Convert icon
    if [ -f "$ROOT/packaging/arranger.png" ]; then
        cp "$ROOT/packaging/arranger.png" "$ROOT/AppDir/arranger.png"
    elif [ -f "$ROOT/packaging/arranger.svg" ]; then
        rsvg-convert -w 256 -h 256 "$ROOT/packaging/arranger.svg" \
            -o "$ROOT/AppDir/arranger.png"
    fi

    cp "$ROOT/packaging/arranger.desktop" "$ROOT/AppDir/"
    cp "$ROOT/packaging/AppRun" "$ROOT/AppDir/AppRun"
    chmod +x "$ROOT/AppDir/AppRun"
    ln -sf arranger.png "$ROOT/AppDir/.DirIcon" 2>/dev/null || true

    ARCH=x86_64 /tmp/appimagetool "$ROOT/AppDir" "$APPIMAGE_PATH"

    echo ""
    echo "AppImage created: $APPIMAGE_PATH"
    echo "  Size: $(du -sh "$APPIMAGE_PATH" | cut -f1)"
    echo ""
    echo "Test with:"
    echo "  chmod +x $APPIMAGE_PATH && $APPIMAGE_PATH"
else
    echo ""
    echo "To run directly from the bundle:"
    echo "  ./dist/arranger/arranger"
    echo ""
    echo "To also create an AppImage:"
    echo "  $0 --appimage"
fi
