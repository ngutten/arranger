# Arranger: Python Bindings Implementation Plan

Replace the IPC-over-socket architecture with in-process pybind11 bindings,
while keeping the standalone server binary as a dev/headless tool.

The key insight driving the design: rather than binding `AudioEngine` methods
individually (which would require parallel maintenance across IPC dispatch and
bindings as the API grows), we expose a single `ServerHandler::handle(json) ->
json` method. This means the JSON command format becomes the stable internal
contract, new commands are added in exactly one place (`dispatch()` in
`ServerHandler`), and the binding layer never needs to change.

---

## Phase 1: Extract `ServerHandler` into the library

**Goal:** Make `ServerHandler` a first-class library type so both `main.cpp`
and the future binding can use it.

Currently `ServerHandler` lives entirely inside `main.cpp` as a local class.
Move it out so it can be linked into the Python extension module.

### 1.1 — Create `server_handler.h`

New file: `audio_server/include/server_handler.h`

```cpp
#pragma once
#include "audio_engine.h"
#include <string>

class ServerHandler {
public:
    explicit ServerHandler(const AudioEngineConfig& cfg = {});

    // Handle a JSON command string; return a JSON response string.
    // Thread-safe: may be called from any thread.
    std::string handle(const std::string& request_json);

    // Direct access for callers that need it (e.g. main.cpp shutdown logic).
    AudioEngine& engine() { return engine_; }

private:
    AudioEngine engine_;
    bool        stream_open_ = false;

    nlohmann::json dispatch(const std::string& cmd, const nlohmann::json& req);
};
```

The `AudioEngineConfig` constructor means the binding can configure sample
rate / block size at construction time from Python settings.

### 1.2 — Create `server_handler.cpp`

New file: `audio_server/src/server_handler.cpp`

Move the `ServerHandler` class body and `dispatch()` method verbatim from
`main.cpp` into this file. Include `base64_encode` here too (it's only used
by `dispatch`).

No logic changes — this is a pure extraction.

### 1.3 — Simplify `main.cpp`

After the extraction, `main.cpp` becomes:

```cpp
#include "server_handler.h"
#include "ipc.h"
#include "protocol.h"

void register_builtin_plugins();

int main(int argc, char** argv) {
    // ... parse args (unchanged) ...

    register_builtin_plugins();

    std::signal(SIGINT,  handle_signal);
    std::signal(SIGTERM, handle_signal);

    AudioEngineConfig cfg;
    cfg.sample_rate = sample_rate;
    cfg.block_size  = block_size;

    ServerHandler handler(cfg);

    IpcServer server(address);
    std::string err = server.start([&](const std::string& req) {
        return handler.handle(req);
    });
    // ... rest unchanged ...
}
```

### 1.4 — Update `CMakeLists.txt`

Add `src/server_handler.cpp` to `audio_server_lib` sources. Remove it from
the `audio_server` executable target (it now comes through the lib).

### Verification

Build `audio_server` binary and confirm existing behaviour is unchanged.
Run `test_audio_server.py` against the rebuilt binary.

---

## Phase 2: Add pybind11 and write `bindings.cpp`

**Goal:** Build a Python extension module `arranger_engine` that exposes
`ServerHandler::handle()` to Python.

### 2.1 — Add pybind11 to `CMakeLists.txt`

```cmake
option(ENABLE_PYTHON_BINDINGS "Build Python extension module" OFF)

if(ENABLE_PYTHON_BINDINGS)
    find_package(Python3 REQUIRED COMPONENTS Interpreter Development)

    include(FetchContent)
    FetchContent_Declare(pybind11
        GIT_REPOSITORY https://github.com/pybind/pybind11
        GIT_TAG        v2.13.6
    )
    FetchContent_MakeAvailable(pybind11)

    pybind11_add_module(arranger_engine bindings/bindings.cpp)
    target_link_libraries(arranger_engine PRIVATE audio_server_lib)

    # Output into standalone/ so `from .arranger_engine import ...` works
    set_target_properties(arranger_engine PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY
        "${CMAKE_SOURCE_DIR}/standalone")
endif()
```

On Linux this produces `standalone/arranger_engine.cpython-3xx-x86_64-linux-gnu.so`.
On Windows it produces `standalone/arranger_engine.cpython-3xx-win_amd64.pyd`.
Both are importable as `arranger_engine` from within the `standalone` package.

### 2.2 — Create `bindings/bindings.cpp`

New file: `audio_server/bindings/bindings.cpp`

```cpp
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "server_handler.h"
#include "audio_engine.h"
#include "plugin_api.h"

namespace py = pybind11;
void register_builtin_plugins();

// Helper: list all registered plugin descriptors as a Python list of dicts.
// Replicates the list_registered_plugins command so Python can call it at
// import time without a round-trip through handle().
static py::list _list_plugins() {
    py::list result;
    for (auto* reg : PluginRegistry::all()) {
        auto desc = PluginRegistry::find_descriptor(reg->id);
        if (!desc) continue;
        py::dict p;
        p["id"]           = desc->id;
        p["display_name"] = desc->display_name;
        p["category"]     = desc->category;
        p["doc"]          = desc->doc;
        p["author"]       = desc->author;
        p["version"]      = desc->version;
        // ports and config_params omitted here for brevity;
        // full descriptor is available via handle("list_registered_plugins")
        result.append(p);
    }
    return result;
}

PYBIND11_MODULE(arranger_engine, m) {
    m.doc() = "Arranger audio engine — in-process Python bindings";

    // Register built-in plugins once at module import time.
    // Safe to call multiple times (guarded internally).
    register_builtin_plugins();

    py::class_<AudioEngineConfig>(m, "AudioEngineConfig")
        .def(py::init<>())
        .def_readwrite("sample_rate",   &AudioEngineConfig::sample_rate)
        .def_readwrite("block_size",    &AudioEngineConfig::block_size)
        .def_readwrite("output_device", &AudioEngineConfig::output_device);

    py::class_<ServerHandler>(m, "AudioServer")
        .def(py::init<const AudioEngineConfig&>(),
             py::arg("cfg") = AudioEngineConfig{})
        // Release the GIL on handle() — it may block briefly on graph swap
        // or offline render, and we don't want to freeze Qt's event loop.
        .def("handle", &ServerHandler::handle,
             py::call_guard<py::gil_scoped_release>());

    m.def("list_plugins", &_list_plugins,
          "Return descriptors for all registered plugins.");
}
```

Two things worth noting:

- `py::call_guard<py::gil_scoped_release>()` on `handle()` means the audio
  callback thread can never accidentally acquire the GIL — it's already
  released before we enter C++.
- `render_offline_wav` goes through `handle()` like everything else, so its
  `std::vector<uint8_t>` return comes back as a JSON base64 string (same as
  the IPC path). This is fine — the decode is already in `ServerEngine`.

### 2.3 — Build and smoke-test

```bash
cmake -DENABLE_PYTHON_BINDINGS=ON ..
make arranger_engine
python3 -c "
from standalone.arranger_engine import AudioServer, AudioEngineConfig
s = AudioServer()
print(s.handle('{\"cmd\": \"ping\"}'))
"
```

Expected: `{"status":"ok","version":"0.1.0","features":[...]}"`

---

## Phase 3: Write `binding_engine.py`

**Goal:** A drop-in replacement for `ServerEngine` that uses the binding
instead of a socket connection.

New file: `standalone/core/binding_engine.py`

```python
"""In-process audio engine via pybind11 bindings.

Drop-in replacement for ServerEngine. Identical external API so app.py
needs no changes beyond backend selection.

The _send() method accepts the same command dicts that ServerEngine sends
over IPC and routes them through ServerHandler::handle() in-process.
JSON serialization still occurs (memory-to-memory), which is negligible
for the payload sizes involved and keeps a single dispatch table in C++.

The position poll thread is gone: current_beat and is_playing are direct
reads of C++ atomics via handle("get_position"), called from the existing
QTimer path in app.py.
"""

from __future__ import annotations

import json
from typing import Optional

from ..arranger_engine import AudioServer, AudioEngineConfig, list_plugins
from .server_engine import _build_graph, _build_server_schedule  # reuse builders
from .settings import Settings


class BindingEngine:

    def __init__(self, state, settings: Optional[Settings] = None):
        self.state    = state
        self.settings = settings or Settings()

        cfg = AudioEngineConfig()
        cfg.sample_rate = self.settings.sample_rate
        cfg.block_size  = self.settings.block_size

        self._server = AudioServer(cfg)
        self._sf2_path: Optional[str] = None
        self._graph_loaded             = False
        self._graph_track_ids          = frozenset()

        # Cache playing state so is_playing property doesn't need a round-trip
        self._is_playing = False
        self._current_beat = 0.0

        # Populate graph editor plugin descriptors
        from ..graph_editor.graph_model import set_plugin_descriptors
        resp = self._send({"cmd": "list_registered_plugins"})
        if resp and resp.get("status") == "ok":
            set_plugin_descriptors(resp.get("plugins", []))

    # ------------------------------------------------------------------
    # Core IPC-compatible dispatch
    # ------------------------------------------------------------------

    def _send(self, request: dict) -> Optional[dict]:
        try:
            return json.loads(self._server.handle(json.dumps(request)))
        except Exception as e:
            print(f"[BindingEngine] handle() error: {e}")
            return None

    # ------------------------------------------------------------------
    # Graph / soundfont  (mirrors ServerEngine exactly)
    # ------------------------------------------------------------------

    def _current_track_ids(self) -> frozenset:
        return frozenset(
            [t.id for t in self.state.tracks] +
            [bt.id for bt in self.state.beat_tracks]
        )

    def _graph_payload(self) -> dict:
        if self.state.signal_graph is not None:
            return self.state.signal_graph.to_server_dict(bpm=self.state.bpm)
        return _build_graph(self.state, self._sf2_path)

    def load_sf2(self, sf2_path: str) -> bool:
        self._sf2_path = sf2_path
        if self.state.signal_graph is not None:
            for node in self.state.signal_graph.nodes:
                if node.node_type == "fluidsynth" and node.is_default_synth:
                    node.params["sf2_path"] = sf2_path
        resp = self._send(self._graph_payload())
        ok = resp is not None and resp.get("status") == "ok"
        if ok:
            self._graph_loaded    = True
            self._graph_track_ids = self._current_track_ids()
        return ok

    def _ensure_graph(self):
        current = self._current_track_ids()
        if not self._graph_loaded or current != self._graph_track_ids:
            if self.state.signal_graph is not None:
                self.state.signal_graph.sync_track_sources(self.state, self._sf2_path)
            resp = self._send(self._graph_payload())
            if resp and resp.get("status") == "ok":
                self._graph_loaded    = True
                self._graph_track_ids = current

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def mark_dirty(self):
        if self.state.signal_graph is not None:
            self.state.signal_graph.sync_track_sources(self.state, self._sf2_path)
        self._send(self._graph_payload())
        self._graph_loaded    = True
        self._graph_track_ids = self._current_track_ids()
        self._send({"cmd": "set_bpm", "bpm": self.state.bpm})
        self._send({"cmd": "set_schedule",
                    "events": _build_server_schedule(self.state)})

    def play(self):
        self.mark_dirty()
        self._send({"cmd": "play"})
        self._is_playing = True

    def stop(self):
        self._send({"cmd": "stop"})
        self._is_playing = False

    def seek(self, beat: float):
        self._send({"cmd": "seek", "beat": beat})
        self._current_beat = beat

    def set_loop(self, start: Optional[float], end: Optional[float]):
        if start is not None and end is not None:
            self._send({"cmd": "set_loop", "start": start, "end": end,
                        "enabled": True})
        else:
            self._send({"cmd": "set_loop", "enabled": False})

    @property
    def current_beat(self) -> float:
        # Poll on demand rather than in a background thread.
        # Called from app.py's QTimer (~30fps) so the cost is negligible.
        resp = self._send({"cmd": "get_position"})
        if resp and resp.get("status") == "ok":
            self._current_beat = resp.get("beat", self._current_beat)
            self._is_playing   = resp.get("playing", self._is_playing)
        return self._current_beat

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    # ------------------------------------------------------------------
    # Note preview
    # ------------------------------------------------------------------

    def play_single_note(self, pitch: int, velocity: int = 100,
                         channel: int = 0, duration: float = 0.5,
                         track_id=None):
        self._ensure_graph()
        node_id = self._source_node_for(track_id, channel)
        self._send({"cmd": "note_on", "node_id": node_id,
                    "channel": channel, "pitch": pitch, "velocity": velocity})
        import threading, time
        def _off():
            time.sleep(duration)
            self._send({"cmd": "note_off", "node_id": node_id,
                        "channel": channel, "pitch": pitch})
        threading.Thread(target=_off, daemon=True).start()

    def all_notes_off(self, track_id=None):
        if track_id is not None:
            self._send({"cmd": "all_notes_off", "node_id": f"track_{track_id}"})
        else:
            self._send({"cmd": "all_notes_off"})

    def set_channel_program(self, channel: int, bank: int, program: int):
        pass  # handled by schedule setup events, same as ServerEngine

    def _source_node_for(self, track_id, channel: int) -> str:
        if track_id is not None:
            return f"track_{track_id}"
        for t in self.state.tracks:
            if (t.channel & 0x0F) == channel:
                return f"track_{t.id}"
        for bt in self.state.beat_tracks:
            return f"track_{bt.id}"
        return "track_default"

    # ------------------------------------------------------------------
    # Parameters and node data  (graph editor paths)
    # ------------------------------------------------------------------

    def set_param(self, node_id: str, param_id: str, value: float):
        self._send({"cmd": "set_param", "node_id": node_id,
                    "param_id": param_id, "value": value})

    def get_node_data(self, node_id: str, port_id: str = "history") -> list:
        resp = self._send({"cmd": "get_node_data", "node_id": node_id,
                           "port_id": port_id})
        if not resp or resp.get("status") != "ok":
            return []
        try:
            return json.loads(resp.get("data", "[]"))
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Offline render
    # ------------------------------------------------------------------

    def render_offline_wav(self) -> Optional[bytes]:
        import base64
        self.mark_dirty()
        resp = self._send({"cmd": "render", "format": "wav"})
        if resp is None or resp.get("status") != "ok":
            return None
        try:
            return base64.b64decode(resp["data"])
        except Exception as e:
            print(f"[BindingEngine] render decode error: {e}")
            return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        self.all_notes_off()
        # AudioServer destructor closes PortAudio stream.
        # Explicit del here ensures ordering if app.py holds other refs.
        self._server = None

    def ensure_instrument(self):
        self._ensure_graph()

    @property
    def is_connected(self) -> bool:
        return self._server is not None
```

Note that `_build_graph` and `_build_server_schedule` are imported directly
from `server_engine.py` — no duplication. If those helpers grow, consider
moving them to a shared `_schedule_builder.py`, but that's not necessary now.

---

## Phase 4: Wire `BindingEngine` into `app.py`

**Goal:** Make `binding` the default backend; keep `server` and `fluidsynth`
as fallbacks.

### 4.1 — Add import

```python
try:
    from .core.binding_engine import BindingEngine
    _HAS_BINDING_ENGINE = True
except ImportError:
    _HAS_BINDING_ENGINE = False
```

### 4.2 — Update `_init_engine()`

```python
def _init_engine(self):
    backend = self.settings.audio_backend  # 'binding', 'server', or 'fluidsynth'

    if backend == 'binding' and _HAS_BINDING_ENGINE:
        try:
            self.engine = BindingEngine(self.state, self.settings)
            return
        except Exception as e:
            print(f"[App] BindingEngine init failed: {e}; falling back")

    if backend in ('binding', 'server') and _HAS_SERVER_ENGINE:
        # 'binding' falls through here if the .so wasn't built
        try:
            from .core.server_engine import DEFAULT_ADDRESS
            addr = self.settings.server_address or DEFAULT_ADDRESS
            self.engine = ServerEngine(self.state, self.settings, address=addr)
            return
        except Exception as e:
            print(f"[App] ServerEngine init failed: {e}; falling back")

    if _HAS_ENGINE:
        try:
            self.engine = AudioEngine(self.state, self.settings)
            return
        except Exception as e:
            print(f"[App] AudioEngine init failed: {e}")

    self.engine = None
```

### 4.3 — Clean up `_push_graph_to_engine()`

The current implementation pattern-matches on `ServerEngine` via
`hasattr(self.engine, '_send')`. Since `BindingEngine` also has `_send`,
this works as-is, but it's worth making the intent explicit:

```python
def _push_graph_to_engine(self) -> None:
    if self.engine and self.state.signal_graph:
        payload = self.state.signal_graph.to_server_dict(bpm=self.state.bpm)
        self.engine._send(payload)
```

Both `BindingEngine` and `ServerEngine` implement `_send(dict) -> dict`, so
this is clean duck typing rather than an accident.

### 4.4 — Update default setting

In `settings.py`, change `audio_backend` default from `'server'` to
`'binding'`. The fallback chain in `_init_engine` means users without the
compiled `.so` automatically get `ServerEngine` or `AudioEngine`.

---

## Phase 5: Cleanup and verification

**Goal:** Remove assumptions about a running external server; confirm all
paths work.

### 5.1 — Remove the server-running requirement from documentation/README

Update any setup instructions that say "run `audio_server` before launching
the UI".

### 5.2 — Update `switch_backend()` in `app.py`

This method currently tears down and reinitialises the engine. Add `'binding'`
as a valid option alongside `'server'` and `'fluidsynth'`.

### 5.3 — Update `ConfigDialog`

If the settings dialog exposes backend selection, add `binding` as the
default/preferred option with a label like "Built-in (recommended)".

### 5.4 — Rewrite or supplement `test_audio_server.py`

The Python test scripts in `audio_server/test/` currently require a running
server process. Add a parallel test path that instantiates `BindingEngine`
directly, exercising the same commands. The IPC-based tests can stay for
testing the standalone binary path.

### 5.5 — CI build matrix (if applicable)

Ensure `ENABLE_PYTHON_BINDINGS=ON` is covered in CI. The `audio_server`
binary target should remain a separate build that is always tested
independently, confirming the IPC path hasn't regressed.

---

## What stays, what changes, what is kept for reference

| Component | Status | Notes |
|---|---|---|
| `audio_engine.h/.cpp` | Unchanged | Core engine, untouched |
| `ipc.h/.cpp` | Unchanged | Still compiled into lib and binary |
| `main.cpp` | Simplified | Delegates to `ServerHandler`; ~40 lines |
| `server_handler.h/.cpp` | New | Extracted from `main.cpp` |
| `bindings/bindings.cpp` | New | One `handle()` binding + `list_plugins()` |
| `binding_engine.py` | New | Primary Python-side engine |
| `server_engine.py` | Kept | Secondary path; also source of shared helpers |
| `engine.py` (pure Python) | Kept | Final fallback |
| `app.py` | Minor edits | New import + backend priority order |
| `builtin_plugins.cpp` | Unchanged | Called from bindings init |
| `audio_server` binary | Kept | Dev/headless/test tool |

---

## Notes on dynamic plugin loading (future)

Once the binding layer is in place, dynamic plugin loading becomes
straightforward. Add to `bindings.cpp`:

```cpp
m.def("load_plugin_library", [](const std::string& path) -> bool {
    // dlopen / LoadLibrary
    // dlsym("register_plugin")(PluginRegistry*)
    // return success
});
```

Each plugin `.so` exports:

```cpp
extern "C" void register_plugin(PluginRegistry* registry) {
    // static PluginRegistration reg = ...;
    // registry->add(&reg);
}
```

After `load_plugin_library()` returns, `list_plugins()` and
`handle("list_registered_plugins")` immediately reflect the new plugin —
no restart needed. The `builtin_plugins.cpp` approach (static registration
via `register_builtin_plugins()`) remains valid for built-ins; dynamic
loading only applies to externally distributed plugins.
