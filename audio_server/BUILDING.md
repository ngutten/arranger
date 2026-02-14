# Building audio_server

## Overview

The server has two build targets:
- **Linux** (native): standard CMake + system packages
- **Windows** (cross-compiled from Linux): MinGW-w64 toolchain + cross-compiled deps

Both targets produce a single `audio_server` binary with no runtime dependencies
beyond the OS audio stack (ALSA/PulseAudio/PipeWire on Linux, WASAPI on Windows).

---

## Linux Build

### Prerequisites

```bash
# Ubuntu/Debian
sudo apt install \
    build-essential cmake pkg-config \
    libportaudio2 libportaudio-dev \
    nlohmann-json3-dev \
    libfluidsynth-dev \   # for SF2/soundfont support
    lilv-utils lv2-dev    # for LV2 plugin hosting
```

On Fedora/RHEL:
```bash
sudo dnf install cmake portaudio-devel fluidsynth-devel lilv-devel lv2-devel
# nlohmann-json is fetched by CMake if not available
```

### Build

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

This produces:
- `build/audio_server`     — the server binary
- `build/test_ipc`         — IPC layer unit test
- `build/test_graph`       — graph/scheduler unit test
- `build/test_render`      — offline render integration test

### Run Tests

```bash
./build/test_ipc     # no server needed
./build/test_graph   # no server needed
./build/test_render  # no server needed, but requires portaudio (just for link)
```

For the Python integration test:
```bash
# Terminal 1: start server
./build/audio_server

# Terminal 2: run test
python3 test/test_client.py
# With SF2: python3 test/test_client.py --sf2 /path/to/font.sf2
```

### Build Without Optional Features

```bash
# Sine-only (no FluidSynth, no LV2):
cmake -B build -DENABLE_SF2=OFF -DENABLE_LV2=OFF
```

---

## Windows Cross-Compile from Linux

Cross-compiling with MinGW-w64 lets you build and roughly test the Windows
binary entirely from Linux. You cannot run it natively, but you can:
- Run it under Wine (see Testing section below)
- Verify it links correctly
- Test the protocol via the Python client under Wine

### Step 1: Install MinGW-w64 toolchain

```bash
sudo apt install \
    gcc-mingw-w64-x86-64 \
    g++-mingw-w64-x86-64 \
    binutils-mingw-w64-x86-64
```

Verify:
```bash
x86_64-w64-mingw32-gcc --version
```

### Step 2: Build cross-compiled dependencies

The helper script `deps/build_mingw_deps.sh` downloads, builds, and installs
PortAudio, FluidSynth, and lilv into `/usr/local/mingw-sysroot`.

```bash
chmod +x deps/build_mingw_deps.sh
sudo deps/build_mingw_deps.sh
```

This takes ~10 minutes. Sources are downloaded from official repositories.
See the script for exact versions and configuration options.

**What the script builds:**
- PortAudio v19.7.0 (WASAPI backend for Windows)
- FluidSynth 2.3.x (with no GUI, no dbus)
- libsndfile (FluidSynth dependency)
- lilv 0.24.x + sord, sratom, lv2 headers

**Note on SF2 under Windows**: FluidSynth on Windows requires that SF2 paths
use backslashes or raw forward slashes; the server handles this internally.

### Step 3: Cross-compile the server

```bash
cmake -B build-win \
    -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-w64-x86_64.cmake \
    -DMINGW_SYSROOT=/usr/local/mingw-sysroot \
    -DCMAKE_BUILD_TYPE=Release
cmake --build build-win -j$(nproc)
```

Output: `build-win/audio_server.exe`

The binary is statically linked (no MinGW DLL runtime required).
It still requires the Windows system DLLs (kernel32, ws2_32, etc.) which
are always present on any Windows installation.

---

## Testing the Windows Build from Linux

### Option A: Wine (recommended for protocol testing)

Wine can run many Windows console applications:

```bash
sudo apt install wine64

# Run the server under wine
wine ./build-win/audio_server.exe &

# The named pipe address \\.\pipe\AudioServer works in Wine.
# However Wine maps it to a Unix socket internally, so you can
# test from the Python client targeting the Wine pipe address:
python3 test/test_client.py --address '\\.\pipe\AudioServer'
```

**Caveats with Wine:**
- Audio output may or may not work depending on Wine's audio configuration.
  For the purposes of protocol testing and offline render, audio output
  doesn't matter.
- The offline render (`test_client.py`) will work fully — it doesn't need
  audio hardware.
- Real-time playback under Wine is for smoke-testing only; production testing
  requires a real Windows machine.

### Option B: Quick binary validation (no Wine)

```bash
# Verify the binary links correctly and is a valid Windows PE
file build-win/audio_server.exe
# → PE32+ executable (console) x86-64, for MS Windows

# Check it doesn't depend on unexpected DLLs
x86_64-w64-mingw32-objdump -p build-win/audio_server.exe | grep "DLL Name"
# Should only show: KERNEL32.dll, WINMM.dll, ws2_32.dll, and optionally
# fluidsynth/portaudio if dynamic linking was chosen.
```

### Option C: CI/CD — GitHub Actions

If using GitHub Actions, add a `windows-latest` runner job:

```yaml
# .github/workflows/build.yml (partial)
jobs:
  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install deps
        run: choco install cmake portaudio  # or vcpkg
      - name: Build
        run: |
          cmake -B build -G "Visual Studio 17 2022"
          cmake --build build --config Release
      - name: Test
        run: |
          Start-Process build\Release\audio_server.exe
          Start-Sleep 1
          python test\test_client.py --skip-transport
```

Note: the CMake build currently targets MinGW (GCC). For MSVC, the
`CMakeLists.txt` would need minor adjustments (mainly removing the
`-static-libgcc` linker flags).

---

## Dependency Installation Script

The following is the content of `deps/build_mingw_deps.sh`:

```bash
#!/bin/bash
# Builds cross-compiled Windows dependencies into /usr/local/mingw-sysroot.
# Run as root (or with sudo) on a Linux machine with mingw-w64 installed.

set -e
SYSROOT=/usr/local/mingw-sysroot
HOST=x86_64-w64-mingw32
BUILD_DIR=$(mktemp -d)
CORES=$(nproc)

mkdir -p "$SYSROOT"
export PKG_CONFIG_PATH="$SYSROOT/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"

cd "$BUILD_DIR"

# --- PortAudio ---
wget -q https://files.portaudio.com/archives/pa_stable_v190700_20210406.tgz
tar xf pa_stable_v190700_20210406.tgz
cd portaudio
cmake -B build \
    -DCMAKE_TOOLCHAIN_FILE=... \  # point to your MinGW toolchain file
    -DCMAKE_INSTALL_PREFIX="$SYSROOT" \
    -DPA_BUILD_SHARED=OFF \
    -DPA_USE_WASAPI=ON \
    -DPA_USE_DS=ON
cmake --build build -j$CORES
cmake --install build
cd ..

# --- libsndfile (FluidSynth dep) ---
wget -q https://github.com/libsndfile/libsndfile/releases/download/1.2.2/libsndfile-1.2.2.tar.xz
tar xf libsndfile-1.2.2.tar.xz
cd libsndfile-1.2.2
cmake -B build \
    -DCMAKE_TOOLCHAIN_FILE=... \
    -DCMAKE_INSTALL_PREFIX="$SYSROOT" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_PROGRAMS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF
cmake --build build -j$CORES
cmake --install build
cd ..

# --- FluidSynth ---
wget -q https://github.com/FluidSynth/fluidsynth/archive/refs/tags/v2.3.5.tar.gz
tar xf v2.3.5.tar.gz
cd fluidsynth-2.3.5
cmake -B build \
    -DCMAKE_TOOLCHAIN_FILE=... \
    -DCMAKE_INSTALL_PREFIX="$SYSROOT" \
    -DBUILD_SHARED_LIBS=OFF \
    -Denable-jack=OFF -Denable-pulse=OFF -Denable-alsa=OFF \
    -Denable-dbus=OFF -Denable-sdl2=OFF
cmake --build build -j$CORES
cmake --install build
cd ..

# --- LV2 headers ---
wget -q https://gitlab.com/lv2/lv2/-/archive/v1.18.10/lv2-v1.18.10.tar.gz
tar xf lv2-v1.18.10.tar.gz
cp -r lv2-v1.18.10/include/lv2 "$SYSROOT/include/"

# --- sord, sratom, lilv ---
# (abridged — full script in deps/build_mingw_deps.sh)

echo "MinGW sysroot built in $SYSROOT"
```

The full script is in `deps/build_mingw_deps.sh`.

---

## Project Layout

```
audio_server/
├── CMakeLists.txt          Build system
├── cmake/
│   └── mingw-w64-x86_64.cmake   Cross-compile toolchain
├── deps/
│   └── build_mingw_deps.sh  Fetch + build Windows deps
├── include/
│   ├── protocol.h          Wire protocol (shared with Python client)
│   ├── graph.h             Signal graph
│   ├── scheduler.h         Beat-timed event dispatcher
│   ├── synth_node.h        Node types (sine, fluidsynth, lv2, mixer, ...)
│   ├── audio_engine.h      PortAudio engine + offline render
│   ├── ipc.h               Unix socket / named pipe server+client
│   └── nlohmann/json.hpp   Bundled (or fetched by CMake)
├── src/
│   ├── main.cpp            Server entry point + command dispatcher
│   ├── graph.cpp
│   ├── scheduler.cpp
│   ├── synth_node.cpp
│   ├── audio_engine.cpp
│   ├── ipc.cpp
│   └── lv2_host.cpp        (optional, compiled only with AS_ENABLE_LV2)
└── test/
    ├── test_ipc.cpp         C++ IPC unit test
    ├── test_graph.cpp       C++ graph/scheduler unit test
    ├── test_render.cpp      C++ offline render integration test
    └── test_client.py       Python IPC integration test (frontend prototype)
```

---

## Integration with the Python Sequencer

The Python client in `test/test_client.py` is designed to show exactly how
the sequencer frontend will eventually talk to the server. The key translation
points are:

| Python sequencer concept | Audio server equivalent |
|---|---|
| `build_schedule(state)` → `SchedEvent` list | `build_schedule_from_pattern()` → `set_schedule` JSON |
| `engine.mark_dirty()` | `client.send(set_schedule_json)` |
| `engine.play()` / `stop()` | `client.send({"cmd":"play"})` etc. |
| `track.synth_type` / SF2 path | `set_graph` node type + sf2_path |
| Beat pattern as control signal | `build_control_schedule()` → `set_schedule` |
| `engine.render_offline_wav()` | `client.send({"cmd":"render"})` → base64 WAV |

The `AudioServerClient` class in `test_client.py` will move to
`core/audio_server_client.py` when integrating with the main app.
`engine.py` will be refactored so `AudioEngine` optionally delegates to
the server rather than driving FluidSynth directly.
