#pragma once
// plugin_api.h
// ==========================================================================
// Arranger Audio Server — Plugin API
// ==========================================================================
//
// This is the ONLY header a plugin needs to include. It defines the complete
// contract between a plugin and the audio engine.
//
// To create a plugin:
//   1. #include "plugin_api.h"
//   2. Subclass Plugin, implement descriptor() and process().
//   3. Call REGISTER_PLUGIN(MyPlugin) in your .cpp file.
//
// The engine discovers registered plugins at startup and makes them available
// in the signal graph.
//
// Threading model:
//   - descriptor(), configure(), read_monitor(), get/set_graph_data()
//     are called on the MAIN thread.
//   - activate() and deactivate() are called on the MAIN thread (never while
//     process() is running).
//   - process() and all event methods (note_on, etc.) are called on the
//     AUDIO thread. They must not allocate, lock, or do I/O.
//
// ==========================================================================

#include <string>
#include <vector>
#include <memory>
#include <functional>
#include <cstdint>

// ==========================================================================
// Port and control types
// ==========================================================================

/// What kind of signal flows through a port.
enum class PluginPortType {
    AudioMono,      ///< float[block_size] — single audio channel
    AudioStereo,    ///< Convenience: engine allocates L+R mono buffers.
                    ///< Plugin sees left/right pointers in PluginBuffers.
    Event,          ///< MIDI-style event stream (note on/off, CC, pitch bend, etc.)
    Control,        ///< Single float per block (control rate).
};

/// How the frontend should present a Control port.
enum class ControlHint {
    Continuous,     ///< 0..1 knob or slider (default)
    Toggle,         ///< Bool-like: 0 or 1 — checkbox / switch
    Integer,        ///< Integer in [min, max] — stepped slider or spinbox
    Categorical,    ///< One-of-N — dropdown / combobox
    Radio,          ///< One-of-N — radio buttons (few mutually exclusive choices)
    Meter,          ///< Read-only output — VU meter, level indicator
    GraphEditor,    ///< Complex editor — EQ curve, envelope, breakpoint function
};

/// The role of a port within the signal graph.
enum class PortRole {
    Input,          ///< User-driven: the user connects or adjusts this.
    Output,         ///< Plugin-driven: audio/event/control output to route.
    Sidechain,      ///< Secondary input (e.g. compressor key signal).
    Monitor,        ///< Read-only output for display only (level meter, etc.).
                    ///< Not routable in the signal graph.
};

// ==========================================================================
// Port descriptor
// ==========================================================================

/// Fully describes one port of a plugin.
struct PortDescriptor {
    std::string   id;             ///< Machine-readable, stable across versions.
    std::string   display_name;   ///< Human-readable label.
    std::string   doc;            ///< Tooltip / help text (may be empty).
    PluginPortType type;
    PortRole      role;

    // --- Control-specific metadata (ignored for Audio/Event ports) ---

    ControlHint   hint          = ControlHint::Continuous;
    float         default_value = 0.0f;
    float         min_value     = 0.0f;
    float         max_value     = 1.0f;
    float         step          = 0.0f;   ///< 0 = continuous; >0 = stepped

    /// For Categorical / Radio hints: display label for each integer value.
    /// Index i corresponds to control value i.
    std::vector<std::string> choices;

    /// For GraphEditor hint: identifies the editor type.
    /// Convention strings: "eq_curve", "adsr_envelope", "breakpoint", etc.
    std::string   graph_type;

    /// Whether this port should show as a connectable port in the graph editor
    /// by default.  Set to false to start hidden; the user can always reveal via
    /// right-click.  The UI also auto-hides Categorical/Radio/Toggle ports regardless
    /// of this flag — this provides explicit opt-out for other hint types.
    bool          show_port_default = true;
};

// ==========================================================================
// Non-port configuration parameters
// ==========================================================================

/// Types for configuration parameters that don't flow through the signal graph.
/// These are presented via GUI elements (file pickers, text fields, etc.).
enum class ConfigType {
    String,         ///< Free-form text
    FilePath,       ///< File picker dialog
    Integer,        ///< Integer spinner
    Float,          ///< Float field
    Bool,           ///< Checkbox
    Categorical,    ///< Dropdown from choices list
};

/// A configuration parameter (not a signal-graph port).
struct ConfigParam {
    std::string   id;
    std::string   display_name;
    std::string   doc;
    ConfigType    type;
    std::string   default_value;   ///< Always string-encoded.
    std::string   file_filter;     ///< For FilePath: e.g. "SF2 Files (*.sf2);;All (*)"
    std::vector<std::string> choices;  ///< For Categorical
};

// ==========================================================================
// Plugin descriptor
// ==========================================================================

/// Complete self-description of a plugin.
struct PluginDescriptor {
    std::string   id;             ///< Unique ID, e.g. "builtin.sine"
    std::string   display_name;   ///< Shown in menus, e.g. "Sine Synth"
    std::string   category;       ///< "Synth", "Effect", "Filter", "Mixer",
                                  ///< "EventGen", "EventEffect", "Output", "Utility"
    std::string   doc;            ///< Description paragraph.
    std::string   author;
    int           version = 1;

    std::vector<PortDescriptor> ports;
    std::vector<ConfigParam>    config_params;
};

// ==========================================================================
// Process-time data structures
// ==========================================================================

/// Timing and transport context for one process block.
struct PluginProcessContext {
    int    block_size;
    float  sample_rate;
    float  bpm;
    double beat_position;     ///< Beat at start of this block.
    double beats_per_sample;
};

/// A single MIDI-style event with a sample offset within the block.
struct MidiEvent {
    int      frame;       ///< Sample offset within the block [0, block_size).
    uint8_t  status;      ///< MIDI status byte (0x80 = note off, 0x90 = note on, etc.)
    uint8_t  data1;       ///< First data byte (pitch, CC number, etc.)
    uint8_t  data2;       ///< Second data byte (velocity, CC value, etc.)
    uint8_t  channel;     ///< MIDI channel 0-15 (extracted for convenience).
};

/// Audio buffer pair for a single port (mono or stereo).
struct AudioPortBuffer {
    float* left   = nullptr;  ///< Always valid. For mono ports, this is the buffer.
    float* right  = nullptr;  ///< Non-null for stereo ports; null for mono.
    int    frames  = 0;       ///< Number of samples (== block_size).
};

/// Control port value.
struct ControlPortBuffer {
    float  value   = 0.0f;    ///< Current value for this block.
};

/// Event port buffer — a sequence of MIDI events for this block.
struct EventPortBuffer {
    /// Events received this block (for input ports), sorted by frame.
    const std::vector<MidiEvent>* events = nullptr;

    /// Events to emit this block (for output ports).
    /// Plugin appends events here during process().
    std::vector<MidiEvent>* output_events = nullptr;
};

/// All port buffers for a plugin, keyed by port ID.
///
/// The engine pre-populates these maps before each process() call.
/// Map lookups are NOT in the hot path — the engine resolves port IDs
/// to internal buffer indices at activate() time and fills these structs
/// with direct pointers each block.
///
/// Plugins that want to cache pointers can do so in activate() or on
/// first process() call, but the map API is the primary interface.
struct PluginBuffers {
    /// Audio port buffers, keyed by PortDescriptor::id.
    struct AudioMap {
        AudioPortBuffer* get(const std::string& id);
        const AudioPortBuffer* get(const std::string& id) const;
        // Internal storage — plugins shouldn't touch these directly.
        std::vector<std::pair<std::string, AudioPortBuffer>> entries;
    } audio;

    /// Control port buffers, keyed by PortDescriptor::id.
    struct ControlMap {
        ControlPortBuffer* get(const std::string& id);
        const ControlPortBuffer* get(const std::string& id) const;
        // Internal storage.
        std::vector<std::pair<std::string, ControlPortBuffer>> entries;
    } control;

    /// Event port buffers, keyed by PortDescriptor::id.
    struct EventMap {
        EventPortBuffer* get(const std::string& id);
        const EventPortBuffer* get(const std::string& id) const;
        // Internal storage.
        std::vector<std::pair<std::string, EventPortBuffer>> entries;
    } events;
};

// ==========================================================================
// Plugin base class
// ==========================================================================

/// Base class for all plugins. Subclass this and implement at least
/// descriptor() and process().
class Plugin {
public:
    virtual ~Plugin() = default;

    // --- Metadata (called once, before anything else) ---

    /// Return the complete self-description of this plugin.
    /// Called on main thread. The returned descriptor must be stable
    /// (same result every time for a given instance).
    virtual PluginDescriptor descriptor() const = 0;

    // --- Lifecycle (main thread) ---

    /// Called once when the plugin is placed in an active graph.
    /// Allocate any internal buffers here.
    virtual void activate(float sample_rate, int max_block_size) {}

    /// Called when the plugin is removed from the graph.
    /// Free internal resources. May be called without a prior activate()
    /// (e.g. if graph construction fails).
    virtual void deactivate() {}

    // --- Configuration (main thread, not realtime) ---

    /// Called when the user changes a ConfigParam.
    /// key is ConfigParam::id; value is the new string-encoded value.
    virtual void configure(const std::string& key, const std::string& value) {}

    // --- Realtime processing (audio thread) ---

    /// Process one block. Called on the audio thread — must not allocate,
    /// lock, or perform I/O.
    ///
    /// Input buffers are pre-filled by the engine. Output buffers are
    /// zeroed before this call; the plugin writes its output into them.
    ///
    /// Event input ports contain events for this block (sorted by frame).
    /// Event output ports should be populated by the plugin.
    virtual void process(const PluginProcessContext& ctx, PluginBuffers& buffers) = 0;

    // --- MIDI event convenience interface (audio thread) ---
    //
    // These are called by the engine to deliver events from the legacy
    // TrackSourceNode fan-out path (scheduled events and preview notes).
    // Default implementations build MidiEvents and append them to the
    // first Event input port buffer. Override if you need custom handling.
    //
    // Plugins that declare Event input ports can also receive events
    // directly in the EventPortBuffer — both paths coexist.

    virtual void note_on (int channel, int pitch, int velocity) { (void)channel; (void)pitch; (void)velocity; }
    virtual void note_off(int channel, int pitch) { (void)channel; (void)pitch; }
    virtual void all_notes_off(int channel = -1) { (void)channel; }
    virtual void pitch_bend(int channel, int value) { (void)channel; (void)value; }
    virtual void program_change(int channel, int bank, int program) { (void)channel; (void)bank; (void)program; }
    virtual void control_change(int channel, int cc, int value) { (void)channel; (void)cc; (void)value; }
    virtual void channel_volume(int channel, int volume) { (void)channel; (void)volume; }

    // --- Monitor readback (main thread) ---

    /// Read the current value of a Monitor port. Called from the main thread.
    /// Plugins should use std::atomic<float> internally for thread safety.
    virtual float read_monitor(const std::string& port_id) { (void)port_id; return 0.0f; }

    // --- Graph editor data (main thread, non-realtime) ---

    /// Return current curve/envelope data as JSON for GraphEditor ports.
    virtual std::string get_graph_data(const std::string& port_id) { (void)port_id; return "{}"; }

    /// Set curve/envelope data from the frontend.
    virtual void set_graph_data(const std::string& port_id, const std::string& json) { (void)port_id; (void)json; }
};

// ==========================================================================
// Plugin registration
// ==========================================================================

/// Factory function type — returns a new default-constructed plugin instance.
using PluginFactory = std::unique_ptr<Plugin>(*)();

/// Registration entry — one per plugin type.
struct PluginRegistration {
    std::string   id;
    PluginFactory factory;
};

/// Global plugin registry.
///
/// Plugins register themselves via the REGISTER_PLUGIN macro (below).
/// The engine queries the registry at startup to discover available plugins.
class PluginRegistry {
public:
    /// Add a registration (called at static init time).
    static void add(PluginRegistration* reg);

    /// All registered plugins.
    static const std::vector<PluginRegistration*>& all();

    /// Create a plugin instance by ID. Returns nullptr if not found.
    static std::unique_ptr<Plugin> create(const std::string& id);

    /// Look up a descriptor by ID. Returns nullptr if not found.
    /// The returned pointer is valid for the lifetime of the program.
    static const PluginDescriptor* find_descriptor(const std::string& id);
};

/// Register a plugin class. Place this in the plugin's .cpp file.
///
/// Example:
///   class MySynth : public Plugin { ... };
///   REGISTER_PLUGIN(MySynth)
///
/// The macro creates a static PluginRegistration and registers it before
/// main() runs.
#define REGISTER_PLUGIN(PluginClass)                                       \
    static ::PluginRegistration _plugin_reg_##PluginClass = [] {           \
        auto tmp = std::make_unique<PluginClass>();                        \
        ::PluginRegistration reg;                                          \
        reg.id = tmp->descriptor().id;                                     \
        reg.factory = []() -> std::unique_ptr<Plugin> {                    \
            return std::make_unique<PluginClass>();                         \
        };                                                                 \
        return reg;                                                        \
    }();                                                                   \
    static bool _plugin_init_##PluginClass =                               \
        (::PluginRegistry::add(&_plugin_reg_##PluginClass), true)
