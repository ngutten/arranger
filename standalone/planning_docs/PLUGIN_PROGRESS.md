# Plugin System Implementation — Progress Log

## Status: Phase 4 IN PROGRESS (2/N new plugins, event routing operational)

## Phase 1: COMPLETE — API header + registry + adapter

See previous log for details. All infrastructure in place.

## Phase 2: COMPLETE — All built-ins ported

All 5 built-in plugins ported (sine, note_gate, control_source, mixer, fluidsynth).
See previous log for details.

## Phase 3: COMPLETE — Frontend integration

### Completed

1. **`graph_model.py`** — Plugin descriptor cache + dynamic port derivation
2. **`node_canvas.py`** — Auto-generated settings widgets from ControlHint
3. **`graph_editor_window.py`** — Plugin-aware Add Node menu + synth detection
4. **`server_engine.py`** — Descriptor fetch at connect time
5. **`__init__.py`** — Exported new public API functions

### Low-latency parameter path (NEW)

**Problem**: Every settings widget change triggered a full `set_graph` push (graph rebuild), adding ~120ms latency to knob tweaks.

**Solution**: Added a fast path that sends `set_param` directly to the server for numeric control values, bypassing the graph rebuild. The debounced full push still happens for consistency.

Changes:
- **`node_canvas.py`**: Added `param_changed` signal `(node_id, param_id, value)`. `_on_node_param_changed` emits it for all `int`/`float` values alongside the existing `graph_changed` signal.
- **`graph_editor_window.py`**: Connects `param_changed` → `_on_param_changed_fast()`, which calls `server_engine.set_param()`. Resolves `_server_id()` for output node remapping.
- **`server_engine.py`**: Added `set_param(node_id, param_id, value)` — sends `CMD_SET_PARAM` directly. The audio engine queues it and applies at the start of the next block.

**Result**: Knob/slider changes hit the audio thread within one block (~1-5ms), while structural changes (channel_count, file paths) still go through the full graph rebuild.

### Graph save/load round-trip

Works correctly: `to_dict()` preserves `node_type` as the plugin ID, `from_dict()` restores it, `ports()` falls through to the descriptor cache. Only edge case: loading a graph before descriptors are fetched (no server connection) results in empty ports — same as LV2 behavior.

## Phase 4: New plugins — IN PROGRESS

### Completed plugins

6. **`reverb_plugin.cpp`** — `builtin.reverb`
   - Schroeder/Freeverb-style stereo reverb effect
   - AudioStereo in + AudioStereo out
   - Controls: room_size (feedback), damping (LP in comb feedback), wet, dry, width
   - Implementation: 4 parallel comb filters per channel (8 total, slightly detuned L vs R for stereo width) → 2 series allpass filters per channel
   - Sample-rate-scaled delay line lengths (Freeverb-derived primes)
   - Category: "Effect" (appears in Add Node → Plugins → Effect)

7. **`arpeggiator_plugin.cpp`** — `builtin.arpeggiator`
   - Event in → Event out tempo-synced arpeggiator
   - First plugin to exercise the Event output routing path
   - Patterns: Up, Down, Up-Down, Random, As Played (all categorical controls)
   - Rate: 1/4, 1/8, 1/8T, 1/16, 1/16T, 1/32 (categorical)
   - Controls: gate length (0.05–1.0), octave range (1–4), velocity override (0=passthrough)
   - Uses beat_position from ProcessContext for tempo sync
   - Note tracking via note_on/off convenience methods; event emission via EventPortBuffer
   - Audio-thread-safe: xorshift PRNG for Random mode, no allocations in process()

### Event output routing infrastructure (NEW)

**Problem**: The graph engine had no mechanism to route events from a plugin's Event output port to downstream nodes. TrackSourceNode uses a bespoke `set_downstream()` mechanism, but there was no generic Event connection routing.

**Solution**: Added event output forwarding in `Graph::process()`. After each node processes, if it's a `PluginAdapterNode` with non-empty event outputs, the engine scans `connections_` for matching event connections and delivers events to downstream nodes via their `note_on/off/pitch_bend/program_change` methods.

Changes:
- **`graph.cpp`**: Added `#include "plugin_adapter.h"`. Extended `Graph::process()` with post-node event output routing via `dynamic_cast<PluginAdapterNode*>` + `event_outputs()`.
- Topo-sort already considers all connections (including event connections), so ordering is correct: event-producing nodes process before event-consuming nodes.
- Event delivery happens between the producer's `process()` and the consumer's `process()`, so the consumer sees the events on its next process call.

**Routing flow for arpeggiator**:
```
TrackSourceNode::process()
  → calls arpeggiator.note_on() (via set_downstream)
ArpeggiatorPlugin::process()
  → reads held_notes_, emits arpeggiated events to events_out EventPortBuffer
Graph event routing
  → reads arpeggiator's event_outputs()
  → calls synth.note_on/off() for each event
SynthPlugin::process()
  → renders audio with arpeggiated notes active
```

### Build changes

- **`CMakeLists.txt`**: Added `reverb_plugin.cpp` and `arpeggiator_plugin.cpp` to `audio_server_lib` sources (unconditional — no external dependencies).

### Design notes

**Convenience method vs EventPortBuffer duplication**: The PluginAdapterNode calls both `plugin_->note_on()` AND accumulates events into the EventPortBuffer. For plugins that use both paths (like the arpeggiator), this could cause double-counting. The arpeggiator avoids this by only using the convenience methods for note tracking, ignoring the EventPortBuffer input. Future consideration: add a flag or convention for plugins that prefer one path over the other.

**Event routing performance**: The current event routing scans all connections for each event-producing node. This is O(connections × event_producers) per block. Fine for typical graphs (10-50 connections, 0-2 event producers). If this becomes a bottleneck, pre-compute event routing tables in activate().

## What's next

### Phase 4 continued

- EQ (parametric — exercises GraphEditor hint for EQ curve)
- Compressor (exercises Sidechain port role)
- Delay (tempo-synced, stereo)
- Metronome (generates click track — Event output + Audio output)
- MIDI output sink, file writer

### Future considerations

- Pre-compute event routing tables in Graph::activate() for better perf
- Add convention for plugins to opt out of convenience method forwarding
- Consider CC (control change) forwarding in event routing
- Arpeggiator: add swing parameter, latch mode
