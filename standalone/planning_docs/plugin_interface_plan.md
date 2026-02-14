# Custom Plugin Interface — Implementation Plan

## Situation

The existing `Node` base class in `graph.h` already does most of the DSP-side work: it declares ports, processes audio blocks, and receives MIDI events. What it lacks is **metadata richness** — there's no way for a node to describe to the frontend *what kind of UI widget* a parameter should get, whether a port is a "send" or a "monitor", what the human-readable documentation is, or what the valid categorical choices are. The `PortDecl` struct has `name`, `type`, `min/max/default` and that's it.

Meanwhile, the `NodeDesc` factory struct and `make_node()` are a flat switch statement with per-type fields (`sf2_path`, `lv2_uri`, `pitch_lo`, etc.) — every new node type needs edits in multiple places.

The goal is a **self-describing plugin API** where:
1. A single header (`plugin_api.h`) fully defines the contract — you can build a plugin without seeing any other engine code.
2. Plugins register themselves (static self-registration pattern), so adding a plugin doesn't require editing the factory.
3. The metadata is rich enough that the frontend can auto-generate appropriate UI.

## Design Decisions

### Stream / Port Taxonomy

Keep the existing `PortType` enum but extend it:

```
enum class PortType {
    AudioMono,    // float[block_size], one channel
    AudioStereo,  // convenience: engine auto-expands to L+R mono buffers
    Event,        // MIDI-style event stream (note on/off, CC, etc.)
    Control,      // single float per block, control-rate
};
```

`AudioStereo` is a **declaration convenience** — the engine still allocates two mono buffers internally, but the plugin declares one stereo port and gets `float* left, float* right` in its process context. This eliminates the dual-mono hack from the LV2 adapter.

### Control Sub-types (ControlHint)

Control ports carry a `ControlHint` that tells the frontend how to present them:

```cpp
enum class ControlHint {
    Continuous,     // 0..1 knob/slider (default)
    Toggle,         // bool-like: 0 or 1 — checkbox/switch
    Integer,        // integer in [min, max] — stepped slider or spinbox  
    Categorical,    // one-of-N choices — dropdown/combobox
    Radio,          // one-of-N — radio buttons (few choices, mutually exclusive)
    Meter,          // read-only output — VU meter, level display
    GraphEditor,    // complex: EQ curve, envelope, etc. — described separately
};
```

### Port Role

Each port gets a `PortRole`:

```cpp
enum class PortRole {
    Input,          // user-driven: the user connects or sets this
    Output,         // plugin-driven: the user watches this (audio out, meter, etc.)
    Sidechain,      // secondary input (e.g. sidechain compressor key)
    Monitor,        // read-only output not part of the signal chain (level meter)
};
```

This replaces the current `is_output` bool and gives the frontend enough to distinguish "this is a stereo output to route" from "this is a level meter to display."

### Port Descriptor (replaces PortDecl)

```cpp
struct PortDescriptor {
    std::string   id;            // machine-readable, stable across versions
    std::string   display_name;  // human-readable
    std::string   doc;           // tooltip / help text
    PortType      type;
    PortRole      role;
    
    // Control-specific metadata (ignored for Audio/Event ports):
    ControlHint   hint          = ControlHint::Continuous;
    float         default_value = 0.0f;
    float         min_value     = 0.0f;
    float         max_value     = 1.0f;
    float         step          = 0.0f;   // 0 = continuous
    
    // For Categorical / Radio:
    std::vector<std::string> choices;  // index maps to integer value
    
    // For GraphEditor: an identifier for the graph type
    // ("eq_curve", "adsr_envelope", "breakpoint", etc.)
    std::string   graph_type;
};
```

### Plugin Descriptor (replaces NodeDesc)

```cpp
struct PluginDescriptor {
    std::string   id;             // unique plugin ID, e.g. "builtin.sine", "builtin.fluidsynth"
    std::string   display_name;
    std::string   category;       // "Synth", "Effect", "Filter", "Mixer", "EventGen", "Output"
    std::string   doc;            // description paragraph
    std::string   author;
    int           version = 1;
    
    std::vector<PortDescriptor> ports;
    
    // Non-port parameters presented via GUI (file paths, etc.)
    // These don't flow through the signal graph — they're config.
    std::vector<ConfigParam> config_params;
};

struct ConfigParam {
    std::string   id;
    std::string   display_name;
    std::string   doc;
    ConfigType    type;           // String, FilePath, Integer, Float, Bool, Categorical
    std::string   default_value;  // always string-encoded
    std::string   file_filter;    // for FilePath: "SF2 Files (*.sf2);;All Files (*)"
    std::vector<std::string> choices;  // for Categorical
};
```

### Plugin Base Class

```cpp
class Plugin {
public:
    virtual ~Plugin() = default;
    
    // --- Metadata (called once, before anything else) ---
    virtual PluginDescriptor descriptor() const = 0;
    
    // --- Lifecycle ---
    virtual void activate(float sample_rate, int max_block_size) {}
    virtual void deactivate() {}
    
    // --- Configuration (main thread, not realtime) ---
    // Called when the user changes a ConfigParam (file path, etc.)
    virtual void configure(const std::string& key, const std::string& value) {}
    
    // --- Realtime processing ---
    virtual void process(const ProcessContext& ctx, PluginBuffers& buffers) = 0;
    
    // --- Events (called on audio thread before process()) ---
    virtual void note_on(int channel, int pitch, int velocity) {}
    virtual void note_off(int channel, int pitch) {}
    virtual void all_notes_off(int channel = -1) {}
    virtual void pitch_bend(int channel, int value) {}
    virtual void program_change(int channel, int bank, int program) {}
    virtual void control_change(int channel, int cc, int value) {}
    
    // --- Monitor readback (main thread, for UI meters) ---
    // Called by the engine to read back monitor port values after process().
    virtual float read_monitor(const std::string& port_id) { return 0.0f; }
    
    // --- Graph editor data (for GraphEditor hint) ---
    // Return current curve/envelope data as JSON for the frontend to display.
    virtual std::string get_graph_data(const std::string& port_id) { return "{}"; }
    // Set curve/envelope data from the frontend editor.
    virtual void set_graph_data(const std::string& port_id, const std::string& json) {}
};
```

### PluginBuffers (replaces the raw vector<PortBuffer>)

```cpp
struct PluginBuffers {
    // Indexed by port ID for clarity (the engine pre-resolves these to pointers)
    struct AudioBuf { float* left; float* right; int frames; };
    struct ControlBuf { float value; };
    
    std::unordered_map<std::string, AudioBuf>    audio;
    std::unordered_map<std::string, ControlBuf>  control;
    // Event inputs are delivered via the note_on/note_off/etc. virtual methods
    // (same as current design — no change needed)
};
```

For hot-path performance, the engine will pre-resolve these maps to flat arrays internally and the `PluginBuffers` the plugin sees will actually be backed by pre-allocated storage with the map lookups done once at activate time. The plugin author sees a clean named-port API; the engine doesn't pay for hash lookups per block.

> **Open question:** Should we skip the map-based API entirely and just give plugins indexed arrays + a helper? The map is nicer for plugin authors but the indirection is ugly. One option: the engine fills a `PluginBuffers` struct with direct pointers each block (it knows the mapping from activate), so no hash lookups happen at process time. The "map" is really just a named struct the engine populates. This is probably the right call — the map exists in `PluginDescriptor` and `activate()`, not in the hot path.

### Self-Registration

Static registration via a macro:

```cpp
// In plugin_api.h:
using PluginFactory = std::unique_ptr<Plugin>(*)();

struct PluginRegistration {
    const char* id;
    PluginFactory factory;
};

// Plugins call this in a .cpp file:
#define REGISTER_PLUGIN(cls) \
    static PluginRegistration _reg_##cls { \
        cls().descriptor().id.c_str(), \
        []() -> std::unique_ptr<Plugin> { return std::make_unique<cls>(); } \
    }; \
    static bool _reg_init_##cls = (PluginRegistry::add(&_reg_##cls), true);

// Registry singleton:
class PluginRegistry {
public:
    static void add(PluginRegistration* reg);
    static const std::vector<PluginRegistration*>& all();
    static std::unique_ptr<Plugin> create(const std::string& id);
    static const PluginDescriptor* descriptor(const std::string& id);
};
```

The `make_node()` factory becomes:

```cpp
// Try plugin registry first
auto plugin = PluginRegistry::create(desc.type);
if (plugin) {
    return std::make_unique<PluginAdapterNode>(desc.id, std::move(plugin));
}
// Fall through to legacy built-in types...
```

`PluginAdapterNode` wraps a `Plugin` into the existing `Node` interface so the graph engine doesn't need to change at all.

## Plugin Categories & What Changes

| Category | Examples | Event input? | Audio in? | Audio out? | Notes |
|----------|----------|:---:|:---:|:---:|-------|
| EventGen | arpeggiator, step sequencer, MIDI file player | no (or clock) | no | no | Event output port; generates note events |
| Synth | sine, fluidsynth, sampler | yes | no | yes | MIDI→Audio |
| Filter | EQ, lowpass, highpass | no | yes | yes | Audio→Audio, with control ports for cutoff etc. |
| Mixer | mixer, panner | no | yes (N) | yes | N→1 mixing |
| EventEffect | velocity curve, transpose, channel filter | yes | no | no | Event→Event processing |
| Output | MIDI out, file writer | depends | yes | no | Terminal sinks |

The `category` field in `PluginDescriptor` is advisory (for the "Add Node" menu). The engine determines actual capabilities from the declared ports.

## Stereo Handling

A plugin declares `AudioStereo` ports for the common case. The engine:
1. Allocates two mono buffers per stereo port.
2. Populates `PluginBuffers::AudioBuf` with both `left` and `right` pointers.

If a plugin declares only mono ports (`AudioMono`), and the user connects a stereo wire, the engine auto-duplicates:
- Stereo→Mono input: the engine sums L+R (or lets the user choose L/R/sum via a connection dialog).
- Mono→Stereo output: the engine duplicates to both channels.

This replaces the current `_dual_mono` hack in `graph_model.py` and the complex `split_stereo`/`merge_stereo` elision logic.

## File Organization

```
audio_server/
  include/
    plugin_api.h          ← THE standalone header. Everything a plugin needs.
    plugin_registry.h     ← Registry singleton (included by plugin_api.h)
    plugin_adapter.h      ← PluginAdapterNode : public Node (engine internal)
    graph.h               ← unchanged (or minor: PluginAdapterNode friend)
    audio_engine.h        ← unchanged
    ...
  src/
    plugin_registry.cpp
    plugin_adapter.cpp
  plugins/
    builtin/
      sine_plugin.cpp
      fluidsynth_plugin.cpp
      mixer_plugin.cpp
      track_source_plugin.cpp
      control_source_plugin.cpp
      note_gate_plugin.cpp
    # Future:
    # effects/
    #   eq_plugin.cpp
    #   compressor_plugin.cpp
```

Each plugin `.cpp` file includes only `plugin_api.h`, implements a class, and calls `REGISTER_PLUGIN(MyPlugin)`. That's it. No other engine headers needed.

## Frontend Contract

The frontend queries the engine for available plugins via a new IPC command:

```json
{"cmd": "list_plugins"}
→ {
    "status": "ok",
    "plugins": [
        {
            "id": "builtin.sine",
            "display_name": "Sine Synth",
            "category": "Synth",
            "doc": "Simple sine wave synthesizer for testing.",
            "ports": [
                {
                    "id": "audio_out",
                    "display_name": "Audio Out",
                    "type": "audio_stereo",
                    "role": "output",
                    "doc": "Stereo audio output"
                },
                {
                    "id": "gain",
                    "display_name": "Gain",
                    "type": "control",
                    "role": "input",
                    "hint": "continuous",
                    "min": 0.0, "max": 1.0, "default": 0.15,
                    "doc": "Output volume"
                }
            ],
            "config_params": []
        },
        {
            "id": "builtin.fluidsynth",
            "display_name": "FluidSynth",
            "category": "Synth",
            "doc": "SF2 soundfont-based MIDI synthesizer.",
            "ports": [ ... ],
            "config_params": [
                {
                    "id": "sf2_path",
                    "display_name": "Soundfont",
                    "type": "filepath",
                    "file_filter": "SF2 Files (*.sf2);;All Files (*)",
                    "doc": "Path to .sf2 soundfont file"
                }
            ]
        }
    ]
}
```

The Python `GraphModel` then uses this to auto-generate port definitions, and `NodeGraphCanvas` uses the hints to render appropriate inline widgets (sliders, dropdowns, radio buttons, etc.).

## Implementation Phases

### Phase 1: The API header + registry + adapter

1. Write `plugin_api.h` with all the types above.
2. Write `plugin_registry.h/.cpp` — static registration.
3. Write `plugin_adapter.h/.cpp` — wraps `Plugin` into `Node`.
4. Update `make_node()` to check the registry first.
5. Update CMake to compile the new files and a `plugins/` subdirectory.

Outcome: the old built-in nodes still work unchanged, and the plugin pathway is functional but has no plugins on it yet.

### Phase 2: Port existing built-ins to the plugin API

Convert one-by-one, starting simple:
1. `SinePlugin` — simplest synth, good test case
2. `NoteGatePlugin` — event→control, tests event routing
3. `MixerPlugin` — variable port count, tests dynamic descriptors
4. `ControlSourcePlugin` — tests control output
5. `FluidSynthPlugin` — tests `ConfigParam` (sf2_path)
6. `TrackSourcePlugin` — tests event fan-out (this one may stay as a special engine node rather than a plugin, since it has special relationships with the scheduler)

Each conversion: write the plugin `.cpp`, add `REGISTER_PLUGIN`, remove the old class from `synth_node.h`, verify tests pass.

### Phase 3: Frontend integration

1. New IPC command `list_plugins` → returns all `PluginDescriptor`s as JSON.
2. Update `graph_model.py`: replace hardcoded port tables with descriptor-driven port generation.
3. Update `node_canvas.py`: render widgets based on `ControlHint` (dropdown for Categorical, radio for Radio, etc.).
4. Update `graph_editor_window.py`: populate "Add Node" menu from `list_plugins` response, categorized.
5. Handle `ConfigParam` UI: file dialogs for `FilePath`, text fields for `String`, etc.

### Phase 4: New plugin types

Now that the API exists, we can write new plugins:
- EQ (with `GraphEditor` hint for the curve)
- Compressor / limiter
- Delay / reverb
- Arpeggiator (EventGen)
- MIDI output sink
- File writer sink

## Things to Think About

**`TrackSourceNode` as a plugin vs. engine primitive.** It has a special relationship with the `Dispatcher` (it receives scheduled events and preview injections from IPC). Making it a plugin means the plugin API needs a way for the engine to push events into it, which is already covered by the `note_on`/`note_off` virtuals. But the preview injection pathway (`preview_note_on` with its own mutex and pending queue) is more intimate. Options: (a) keep it as an engine primitive that coexists with the plugin system, (b) add a "preview injection" virtual to the Plugin API, (c) handle preview injection in the adapter layer. I'd lean toward (a) for now — TrackSourceNode and the output/mixer nodes are genuinely "engine infrastructure" rather than "plugins."

**Variable port counts.** The mixer needs N inputs where N is user-configurable. The descriptor is generated at construction time, so this works: you construct a `MixerPlugin(n_channels)` and its `descriptor()` returns the right ports. But the frontend needs to know this is resizable. We could add an `is_resizable` flag or a `port_template` concept. For Phase 2 this can be deferred — the frontend already handles mixer channel count changes via `set_node_config`.

**Graph editor data exchange.** For EQ curves and envelopes, the `get_graph_data`/`set_graph_data` methods exchange JSON. This is inherently non-realtime. The plugin applies the curve data to its internal DSP on the next `process()` call (possibly via a lock-free swap). The JSON schema per graph type should be documented as part of the plugin's descriptor or as a convention (e.g., `"eq_curve"` always means `{bands: [{freq, gain_db, q}, ...]})`).

**Monitor readback threading.** `read_monitor` is called from the main thread but the value is written by the audio thread. Plugins should use `std::atomic<float>` internally. The adapter can also handle this — cache the last output value of monitor ports atomically.

**Backward compatibility.** During migration, both the old `Node`-based built-ins and new `Plugin`-based ones coexist. The adapter makes plugins look like Nodes to the graph. Old JSON graph descriptions with `"type": "sine"` should continue to work — the registry maps `"builtin.sine"` and `make_node` still falls through to the old code until that type is fully ported.
