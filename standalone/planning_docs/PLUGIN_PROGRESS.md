# Plugin System Implementation — Progress Log

## Status: Phase 2 IN PROGRESS (4/5 built-ins ported)

## Phase 1: COMPLETE — API header + registry + adapter

See previous log for details. All infrastructure in place.

## Phase 2: Port existing built-ins

### Completed plugins

All plugins in `plugins/builtin/`, each a standalone `.cpp` file with only `#include "plugin_api.h"`.

1. **`sine_plugin.cpp`** — `builtin.sine`
   - Polyphonic sine synth with release envelope
   - AudioStereo output + Continuous gain control input
   - The gain control port replaces the old `set_param("gain", v)` — it now flows through the graph as a connectable control port
   - Equivalent behavior to legacy SineNode

2. **`note_gate_plugin.cpp`** — `builtin.note_gate`
   - MIDI event → control signal converter (Gate/Velocity/Pitch/NoteCount modes)
   - Event input + Control output + 3 control inputs (mode, pitch_lo, pitch_hi)
   - Mode/pitch_lo/pitch_hi are now graph-connectable control ports with appropriate ControlHints (Categorical for mode, Integer for pitch bounds) — the old `set_param()` approach had no type metadata
   - The mode/band params can now be modulated from other control sources in the graph

3. **`control_source_plugin.cpp`** — `builtin.control_source`
   - Passes automation values from Dispatcher → control output
   - Has a control_in input (receives push_control values via adapter's pending value mechanism) and a control_out output
   - Design note: the original used a ring buffer for push_control(); the plugin version uses the adapter's atomic pending_value on the input control port, which serves the same purpose with less code

4. **`mixer_plugin.cpp`** — `builtin.mixer`
   - N stereo inputs → 1 stereo output with per-channel + master gain
   - Dynamic descriptor based on channel_count_ (set via configure() before adapter construction)
   - Each channel gets an AudioStereo input + Continuous gain control
   - ConfigParam for channel_count so frontend can resize

5. **`fluidsynth_plugin.cpp`** — `builtin.fluidsynth` *(AS_ENABLE_SF2 only)*
   - SF2 soundfont MIDI synth
   - AudioStereo output + ConfigParam for sf2_path (FilePath type)
   - No longer throws on missing sf2_path — silently produces no audio until a valid soundfont is loaded via configure()
   - Supports sf2 hot-reload: calling configure("sf2_path", ...) while activated unloads the old soundfont and loads the new one

### Changes to existing files

- **`CMakeLists.txt`** — Added all 4 unconditional plugin sources + fluidsynth_plugin.cpp conditionally under ENABLE_SF2
- **`src/synth_node.cpp` `make_node()`** — Updated plugin registry path to pass NodeDesc-specific fields (sf2_path, channel_count, pitch_lo, pitch_hi, gate_mode) through configure() before adapter construction

### Design notes

**Type naming**: Plugin IDs are `"builtin.sine"`, `"builtin.mixer"`, etc. The old type names (`"sine"`, `"mixer"`, etc.) still work via the legacy fallthrough in `make_node()`. Both paths coexist. The frontend will switch to the new IDs in Phase 3.

**ControlSourcePlugin input port**: The original ControlSourceNode had only an output port and received values via `push_control()`. The plugin version adds a `control_in` input port that the adapter's `push_control()` writes to (via the atomic pending_value mechanism). This is functionally equivalent but makes the data flow explicit in the graph — and theoretically allows connecting other control sources to it directly.

**NoteGatePlugin control ports**: The original used `set_param()` for mode/pitch_lo/pitch_hi. The plugin version exposes these as typed control ports with hints (Categorical, Integer). This means they can now be modulated from the graph rather than only set via IPC.

**MixerPlugin dynamic descriptor**: The descriptor is generated dynamically based on `channel_count_`. The `REGISTER_PLUGIN` macro sees the default (2-channel) descriptor for the ID extraction. When a specific channel count is needed, `configure("channel_count", "N")` is called before the adapter constructor, which re-queries `descriptor()` and gets the correct port count.

**FluidSynthPlugin error handling**: Changed from throw-on-missing-sf2 to graceful degradation. The plugin produces silence when no soundfont is loaded, and supports hot-reloading sf2 files at runtime.

### Not ported (by design)

- **TrackSourceNode** — Remains an engine primitive. Has intimate relationship with Dispatcher (preview injection, scheduled event fan-out). As discussed in the plan.
- **LV2Node** — External plugin host, not a built-in to port. Stays as-is.

## What's next

### Remaining Phase 2 work

- Verify compilation and test the new plugins through the existing test suite
- Consider adding `push_control()` virtual to Plugin base class for cleaner ControlSource pattern (optional refinement)

### Phase 3: Frontend integration

1. Update `graph_model.py` to use `list_registered_plugins` response for port definitions
2. Update `node_canvas.py` to render widgets based on ControlHint
3. Update `graph_editor_window.py` to populate "Add Node" menu from registry
4. Map old type names → new builtin.* IDs (or support both in the frontend)

### Phase 4: New plugins

- EQ, compressor, delay, reverb
- Arpeggiator (Event in → Event out)
- Metronome
- MIDI output sink, file writer
