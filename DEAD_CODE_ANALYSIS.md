# Dead Code Analysis: Audio Server

This document catalogues dead, unreachable, or superseded code found in the
`audio_server/` C++ layer and the related Python graph editor code, along with
a phased plan for removing it.

Three root causes account for all of the dead code:

1. **LV2 plugin support** was removed (`AS_ENABLE_LV2` is never defined in
   `CMakeLists.txt`), leaving a large conditional block that can never compile.
2. **Built-in node classes** (`SineNode`, `MixerNode`, `ControlSourceNode`,
   `FluidSynthNode`, `NoteGateNode`) have been reimplemented as plugins.
   Because `make_node()` tries the plugin registry first, the legacy fallback
   branches are now unreachable.
3. **The standalone binary** (IPC server + `main.cpp`) is still built but the
   production app uses in-process Python bindings (`binding_engine.py`)
   exclusively.

---

## 1. LV2 Support — Entirely Dead

`AS_ENABLE_LV2` is never defined anywhere in `CMakeLists.txt`, so every
`#ifdef AS_ENABLE_LV2` block is permanently dead.  Additionally,
`lv2_world_acquire()` / `lv2_world_release()` / `list_lv2_plugins()` — which
`LV2Node` calls — now live only in the leftover `lv2_host.bak` backup file and
are not compiled into any target.

### C++ (`audio_server/`)

| File | Dead region |
|------|-------------|
| `src/synth_node.cpp` | Lines 300–891: global URID map state and functions (`urid_map_func`, `urid_unmap_func`, statics); `lilv_node_as_number()` helper; `AtomBuffer` struct and methods; `LV2Node::Impl` struct; all `LV2Node` method bodies |
| `include/synth_node.h` | `LV2Node` forward declaration and class declaration; `NodeDesc::lv2_uri` field |
| `src/server_handler.cpp` | `#ifdef AS_ENABLE_LV2` include of `synth_node.h` (for `list_lv2_plugins`); `"lv2"` feature entry in ping response; `CMD_LIST_PLUGINS` handler block |
| `include/protocol.h` | `CMD_LIST_PLUGINS` constant; `"lv2"` type entry in NodeDesc doc-comment; `"lv2_uri": str` field entry; `"or LV2 port symbol"` qualifier in Connection doc-comment |
| `src/lv2_host.bak` | **Entire file** — backup of the shared LV2 world management code (`lv2_world_acquire`, `lv2_world_release`, `list_lv2_plugins`). Not compiled by any CMake target. |

### Python graph editor (`standalone/graph_editor/`)

| File | Dead regions |
|------|-------------|
| `graph_editor_window.py` | `_lv2_plugins_ready = Signal(object)` (line 65); LV2 entry in the add-node type menu (line 302); `"lv2"` listed as a synth type in the default-target check (line 403) |
| `graph_model.py` | `AUDIO_MONO` doc mentioning LV2 (line 9); `lv2` in the node-type doc (line 38); `_lv2_stereo_key()` function; `_lv2_build_ports()` function; all `node_type == "lv2"` branches in `GraphNode.build_ports()`, `GraphNode.to_server_dict()`, `GraphModel.to_server_dict()` (dual-mono expansion, stereo-map lookup, etc.) |
| `node_canvas.py` | `"lv2": QColor(...)` entry in the colour palette (line 68); LV2 node panel block in `build_node_panel()` (lines 1194–1218) |

### Test files (`audio_server/test/`)

| File | Dead regions |
|------|-------------|
| `test_client.py` | `build_lv2_graph()` function; all LV2 test sections and the `--lv2` CLI argument |
| `test_audio_server.py` | `fetch_lv2_safe_defaults()`, `lv2_passthrough_graph()`, `test_lv2_plugin_list()`, `test_lv2_graph_loads()`, `test_lv2_passthrough_audio()`, `test_lv2_port_introspection()`, `test_lv2_graph_incremental()` functions; `--lv2-reverb-uri`, `--lv2-audio-in`, `--lv2-audio-out`, `--lv2-params` CLI arguments |
| `list_plugins.py` | **Entire file** — LV2 port-layout diagnostic tool |

---

## 2. Built-in Node Classes Transferred to Plugins

`make_node()` in `synth_node.cpp` queries the plugin registry **before** the
legacy fallback chain.  The three node types that are **statically registered**
in `builtin_plugins.cpp` (`sine`, `mixer`, `control_source`) make their
legacy node classes permanently unreachable.  The two that are **dynamically
registered** at startup (`fluidsynth`, `note_gate`) do the same once their
`.so` files are loaded, which happens unconditionally in
`binding_engine._load_plugins_dir()`.

### Unreachable class implementations

| Class | File / Lines | Superseding plugin |
|-------|--------------|--------------------|
| `SineNode` | `synth_node.cpp` 29–103; `synth_node.h` | `plugins/builtin/sine_plugin.cpp` (static) |
| `MixerNode` | `synth_node.cpp` 109–166; `synth_node.h` | `plugins/builtin/mixer_plugin.cpp` (static) |
| `ControlSourceNode` | `synth_node.cpp` 172–196; `synth_node.h` | `plugins/builtin/control_source_plugin.cpp` (static) |
| `FluidSynthNode` | `synth_node.cpp` 202–294; `synth_node.h` | `plugins/builtin/fluidsynth_plugin.cpp` (dynamic `.so`) |
| `NoteGateNode` | `synth_node.cpp` 958–1043; `synth_node.h` | `plugins/builtin/note_gate_plugin.cpp` (dynamic `.so`) |

### Unreachable factory fallback branches in `make_node()`

```cpp
// synth_node.cpp lines 1075–1098  — all unreachable
if (desc.type == "sine")           return std::make_unique<SineNode>(...);
if (desc.type == "mixer")          return std::make_unique<MixerNode>(...);
if (desc.type == "control_source") return std::make_unique<ControlSourceNode>(...);
if (desc.type == "track_source")   return std::make_unique<TrackSourceNode>(...); // see note
if (desc.type == "note_gate")      return std::make_unique<NoteGateNode>(...);
#ifdef AS_ENABLE_SF2
if (desc.type == "fluidsynth")     return std::make_unique<FluidSynthNode>(...);
#endif
#ifdef AS_ENABLE_LV2
if (desc.type == "lv2")            return std::make_unique<LV2Node>(...);
#endif
```

> **Note on `TrackSourceNode`**: there is no `track_source_plugin.cpp`, so
> `TrackSourceNode` is _not_ superseded by a plugin and its factory branch and
> class remain needed.

### Unused `NodeDesc` fields

| Field | Reason unused |
|-------|---------------|
| `lv2_uri` | Only consumed by the dead `LV2Node` branch. |
| `sample_path` | Mentioned in `protocol.h` comments but not forwarded to the sampler plugin via `configure()` in `make_node()` (the sampler receives its path through the graph editor's generic params map instead). |

### Redundant CMake source

In `CMakeLists.txt` (inside `if(ENABLE_SF2)`):

```cmake
target_sources(audio_server_lib PRIVATE plugins/builtin/fluidsynth_plugin.cpp)
```

This adds `fluidsynth_plugin.cpp` to the static library, but as the comment in
`builtin_plugins.cpp` explains, `REGISTER_PLUGIN` static initialisers in a
static library TU are dead-stripped by the linker unless explicitly referenced
from `register_builtin_plugins()`.  The FluidSynth plugin is only activated via
the separate `arranger_plugin_fluidsynth.so` dynamic build.  The static source
inclusion is therefore a no-op and should be removed.

---

## 3. Standalone Binary and IPC Layer

The production Python application drives the engine via in-process pybind11
bindings (`binding_engine.py` → `arranger_engine.so`).  The standalone binary
— which listens on a Unix socket / named pipe and speaks the length-prefixed
JSON protocol — is no longer part of the production path.

### Files to remove

| File | Purpose |
|------|---------|
| `audio_server/src/main.cpp` | Standalone server binary entry point |
| `audio_server/src/ipc.cpp` | Socket / named-pipe IPC implementation |
| `audio_server/include/ipc.h` | IPC header |

### CMakeLists.txt regions to remove

- `add_executable(audio_server src/main.cpp)`
- `target_link_libraries(audio_server PRIVATE audio_server_lib)`
- `set_target_properties(audio_server PROPERTIES ENABLE_EXPORTS ON)` (and the Windows `--export-all-symbols` companion)
- `add_dependencies(audio_server ...)` block listing all dynamic plugin targets
- `install(TARGETS audio_server DESTINATION bin)` and the plugins install rule

### `protocol.h` constants that become dead

Once the IPC layer is removed, the following constants in `protocol.h` lose
their only consumer:

- `DEFAULT_ADDRESS` — socket path / named pipe name
- `MAX_MESSAGE_BYTES` — framing limit for the IPC layer

Three additional command constants are already dead (defined but have no
handler in `server_handler.cpp`):

- `CMD_LOAD_SF2` — "load_sf2"
- `CMD_UNLOAD_NODE` — "unload_node"
- `CMD_GET_GRAPH` — "get_graph"

### Socket-based test files

All test scripts in `audio_server/test/` that connect via a socket should be
removed along with the standalone binary.  The file `test_render.cpp` (which
links directly against `audio_server_lib`) is an exception and should be kept.

| File | Action |
|------|--------|
| `test/test_client.py` | Delete — socket-based client and full test suite |
| `test/test_audio_server.py` | Delete — socket-based integration tests |
| `test/debug_note_gate.py` | Delete — socket-based debug tool |
| `test/test_control_lfo.py` | Delete — socket-based test |
| `test/list_plugins.py` | Delete — LV2 diagnostic (already covered above) |
| `test/test_render.cpp` | **Keep** — links against `audio_server_lib` directly |

---

## Removal Plan

### Phase 1 — Remove LV2 code

1. `src/synth_node.cpp`: Delete lines 300–891 (entire `#ifdef AS_ENABLE_LV2` block, including URID map, `AtomBuffer`, `LV2Node::Impl`, and all `LV2Node` methods).
2. `include/synth_node.h`: Remove `LV2Node` forward declaration and class declaration; remove `NodeDesc::lv2_uri` field.
3. `src/server_handler.cpp`: Remove the `#ifdef AS_ENABLE_LV2` include; remove the `"lv2"` entry in the ping-response features array; remove the `CMD_LIST_PLUGINS` handler block.
4. `include/protocol.h`: Remove `CMD_LIST_PLUGINS` constant; update the NodeDesc doc-comment to remove `"lv2"` type, `"lv2_uri"` field, and the "or LV2 port symbol" qualifier.
5. Delete `src/lv2_host.bak`.
6. `standalone/graph_editor/graph_editor_window.py`: Remove `_lv2_plugins_ready` signal, LV2 menu entry, and LV2 synth-type check.
7. `standalone/graph_editor/graph_model.py`: Remove `_lv2_stereo_key()`, `_lv2_build_ports()`, and all `node_type == "lv2"` branches.
8. `standalone/graph_editor/node_canvas.py`: Remove the LV2 colour palette entry and the LV2 node panel block.
9. `audio_server/test/test_client.py`: Remove `build_lv2_graph()`, all LV2 test sections, and the `--lv2` argument.
10. `audio_server/test/test_audio_server.py`: Remove all `test_lv2_*` functions and `--lv2-*` CLI arguments.
11. Delete `audio_server/test/list_plugins.py`.

### Phase 2 — Remove superseded node classes

1. `src/synth_node.cpp`: Delete `SineNode` implementation.
2. `src/synth_node.cpp`: Delete `MixerNode` implementation.
3. `src/synth_node.cpp`: Delete `ControlSourceNode` implementation.
4. `src/synth_node.cpp`: Delete `FluidSynthNode` implementation (and the `#ifdef AS_ENABLE_SF2` wrapper).
5. `src/synth_node.cpp`: Delete `NoteGateNode` implementation.
6. `src/synth_node.cpp`: Remove the five legacy fallback branches in `make_node()`.
7. `include/synth_node.h`: Remove class declarations for all five nodes above.
8. `include/synth_node.h`: Remove `NodeDesc::sample_path` field.
9. `CMakeLists.txt`: Remove the `target_sources(audio_server_lib PRIVATE fluidsynth_plugin.cpp)` line inside `if(ENABLE_SF2)`.
10. Audit `#include` directives in `src/synth_node.cpp` — `<cstdio>`, `<cmath>`, `<cstring>`, `<algorithm>` should be reviewed; some may only have been needed by the removed code.

### Phase 3 — Remove standalone binary and IPC layer

1. Delete `src/main.cpp`.
2. Delete `src/ipc.cpp` and `include/ipc.h`.
3. `CMakeLists.txt`: Remove the `add_executable(audio_server ...)` target, its `target_link_libraries`, `ENABLE_EXPORTS` property, Windows `--export-all-symbols` option, `add_dependencies(audio_server ...)` block, and `install(TARGETS audio_server ...)`.
4. `include/protocol.h`: Remove `DEFAULT_ADDRESS`, `MAX_MESSAGE_BYTES`, `CMD_LOAD_SF2`, `CMD_UNLOAD_NODE`, and `CMD_GET_GRAPH`.
5. Delete the socket-based test scripts listed in §3 above.

### Phase 4 — Build verification

1. Configure and build with `cmake -DENABLE_PYTHON_BINDINGS=ON -DENABLE_SF2=ON -DENABLE_SNDFILE=ON .. && make`.
2. Confirm no linker errors or unexpected "undefined reference" diagnostics.
3. Run `test_render` to verify the audio pipeline is intact.
4. Smoke-test the Python application with a real soundfont to confirm the binding engine still produces audio.
