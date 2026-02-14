# Plugin System Implementation — Progress Log

## Status: Phase 1 COMPLETE (structural)

## What was done

### Phase 1: API header + registry + adapter

All files created and integrated into the build:

**New files:**

1. **`include/plugin_api.h`** — The standalone plugin API header. Contains:
   - `PluginPortType` enum: AudioMono, AudioStereo, Event, Control
   - `ControlHint` enum: Continuous, Toggle, Integer, Categorical, Radio, Meter, GraphEditor
   - `PortRole` enum: Input, Output, Sidechain, Monitor
   - `PortDescriptor` — full port metadata (id, display_name, doc, type, role, hint, min/max/default, choices, graph_type)
   - `ConfigParam` / `ConfigType` — non-port configuration (file paths, etc.)
   - `PluginDescriptor` — complete self-description (id, display_name, category, doc, ports, config_params)
   - `PluginProcessContext` — block timing info
   - `MidiEvent` — MIDI event with sample-accurate frame offset
   - `AudioPortBuffer`, `ControlPortBuffer`, `EventPortBuffer` — per-port buffer types
   - `PluginBuffers` — named-map buffer container (audio, control, events maps with linear-scan get())
   - `Plugin` — base class with descriptor(), activate(), deactivate(), configure(), process(), MIDI event methods, read_monitor(), get/set_graph_data()
   - `PluginRegistry` — static self-registration singleton
   - `REGISTER_PLUGIN(ClassName)` macro

2. **`src/plugin_registry.cpp`** — Registry singleton + PluginBuffers map accessor implementations

3. **`include/plugin_adapter.h`** — `PluginAdapterNode : public Node` declaration with port mapping structures

4. **`src/plugin_adapter.cpp`** — Full adapter implementation:
   - `build_port_mapping()` — translates PluginDescriptor ports into engine PortDecl + mapping tables
   - `declare_ports()` — exposes AudioStereo as two AudioMono PortDecls, event ports are invisible to engine
   - `process()` — walks descriptor in declaration order to correctly wire flat input[]/output[] vectors to named PluginBuffers
   - MIDI event methods accumulate into EventPortBuffer and forward to plugin convenience virtuals
   - `set_param()` → atomic pending value on matching control port
   - `push_control()` → first non-output control port

**Modified files:**

5. **`src/synth_node.cpp`** — `make_node()` now checks `PluginRegistry::create(desc.type)` first; falls through to legacy types if not found. Added includes for plugin_api.h and plugin_adapter.h.

6. **`include/protocol.h`** — Added `CMD_LIST_REGISTERED_PLUGINS` command.

7. **`src/main.cpp`** — Added handler for `list_registered_plugins` IPC command that serializes all PluginDescriptor data to JSON.

8. **`CMakeLists.txt`** — Added `plugin_registry.cpp` and `plugin_adapter.cpp` to audio_server_lib.

### Design decisions made

- **TrackSourceNode and the output mixer remain engine primitives**, not plugins. They have intimate relationships with the scheduler/dispatcher.
- **Event ports don't create PortDecls** in the graph engine. Input events arrive via Node's note_on/off virtual methods (from TrackSourceNode fan-out). Output events are stored in `event_output_storage_` and can be read by the engine after process().
- **PluginBuffers uses linear-scan maps** (vector of pairs). For typical 1-4 entries per type, this is cache-friendly and avoids hash overhead. The engine pre-populates the structs each block; no allocation in the hot path.
- **Stereo expansion**: AudioStereo ports become two AudioMono PortDecls (`id_L`, `id_R`). The plugin sees a single `AudioPortBuffer{left, right}`.
- **Backward compatibility**: `make_node()` checks the registry first, then falls through to legacy built-ins. Old JSON graph descriptions continue to work.
- **REGISTER_PLUGIN macro**: constructs a temporary instance to extract the descriptor ID, then stores the factory. Registration happens at static init time.

### Key architectural detail: buffer wiring in process()

The graph engine builds `inputs[]` and `outputs[]` by walking `declare_ports()` in order and splitting by `is_output`. The adapter's `process()` must walk `desc_.ports` in the same order to correctly map flat indices to named buffers. This is validated by the fact that `declare_ports()` and the process() wiring loop both iterate `desc_.ports` identically.

## What's next

### Phase 2: Port existing built-ins

Order of conversion (each is a standalone .cpp in `plugins/builtin/`):
1. `SinePlugin` — simplest synth, first test of the full pipeline
2. `NoteGatePlugin` — event→control conversion
3. `ControlSourcePlugin` — control output
4. `MixerPlugin` — variable port count (needs constructor arg)
5. `FluidSynthPlugin` — tests ConfigParam (sf2_path)
6. (TrackSourceNode stays as engine primitive)

### Phase 3: Frontend integration

1. Update `graph_model.py` to use `list_registered_plugins` response for port definitions
2. Update `node_canvas.py` to render widgets based on ControlHint
3. Update `graph_editor_window.py` to populate "Add Node" menu from registry

### Phase 4: New plugins

- EQ, compressor, delay, reverb (AudioStereo in → AudioStereo out + control ports)
- Arpeggiator (Event in → Event out)
- Metronome (no input → Event out, with BPM-synced timing)
- MIDI output sink, file writer
