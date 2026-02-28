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
    Pattern,        ///< A complete pattern snapshot delivered each block.
                    ///< Contains beat-relative note events with channel/program info.
                    ///< Populated once at graph activation from a PatternSourceNode;
                    ///< contents are stable (no per-block reallocation on audio thread).
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

    // --- Control-specific metadata (ignored for Audio/Event/Pattern ports) ---

    ControlHint   hint          = ControlHint::Continuous;
    float         default_value = 0.0f;
    float         min_value     = 0.0f;
    float         max_value     = 1.0f;
    float         step          = 0.0f;   ///< 0 = continuous; >0 = stepped

    /// For Categorical / Radio hints: display label for each integer value.
    std::vector<std::string> choices;

    /// For GraphEditor hint: identifies the editor type.
    std::string   graph_type;

    /// Whether this port should show as a connectable port in the graph editor
    /// by default.
    bool          show_port_default = true;
};

// ==========================================================================
// Non-port configuration parameters
// ==========================================================================

enum class ConfigType {
    String,
    FilePath,
    Integer,
    Float,
    Bool,
    Categorical,
};

struct ConfigParam {
    std::string   id;
    std::string   display_name;
    std::string   doc;
    ConfigType    type;
    std::string   default_value;
    std::string   file_filter;
    bool          save_mode = false;
    std::vector<std::string> choices;
};

// ==========================================================================
// Plugin descriptor
// ==========================================================================

struct PluginDescriptor {
    std::string   id;
    std::string   display_name;
    std::string   category;
    std::string   doc;
    std::string   author;
    int           version = 1;

    std::vector<PortDescriptor> ports;
    std::vector<ConfigParam>    config_params;
};

// ==========================================================================
// Process-time data structures
// ==========================================================================

struct PluginProcessContext {
    int    block_size;
    float  sample_rate;
    float  bpm;
    double beat_position;
    double beats_per_sample;

    bool   is_playing         = false;
    bool   transport_started  = false;
    bool   transport_stopped  = false;
};

struct MidiEvent {
    int      frame;
    uint8_t  status;
    uint8_t  data1;
    uint8_t  data2;
    uint8_t  channel;
};

// ==========================================================================
// Pattern data structures
// ==========================================================================

/// One note event within a pattern, with full instrument identity.
///
/// beat and duration are relative to the pattern's own time base (beat 0 =
/// pattern start).  channel and program carry the original instrument
/// assignment so plugins can route to the correct synth channel downstream.
/// program == -1 means unspecified (use whatever the downstream synth has).
struct PatternNote {
    double  beat;        ///< Onset in pattern-local beats.
    double  duration;    ///< Duration in pattern-local beats.
    uint8_t channel;     ///< MIDI channel (instrument identity).
    uint8_t pitch;       ///< MIDI note number.
    uint8_t velocity;    ///< MIDI velocity 1-127.
    int     program;     ///< MIDI program number, or -1 if unspecified.
    int     bank;        ///< MIDI bank, or -1 if unspecified.
    std::string lyric;   ///< Optional lyric syllable (for singing synthesis).
};

/// A complete pattern snapshot delivered via a Pattern port.
///
/// This is stable, heap-allocated data that lives for the lifetime of the
/// graph.  The plugin receives a const pointer to it each process() call;
/// it must NOT be mutated on the audio thread.
struct PatternData {
    std::vector<PatternNote> notes;
    double length_beats  = 0.0;   ///< Total pattern length (for looping/phase).
    int    subdivision   = 0;     ///< Steps per beat (beat patterns); 0 = melodic.
    bool   is_beat_pattern = false;
};

/// Pattern port buffer — wraps a pointer to the immutable pattern data.
/// The pointer is null until a PatternSourceNode is connected.
struct PatternPortBuffer {
    const PatternData* pattern = nullptr;  ///< null if no pattern connected.
};

// ==========================================================================
// DLL export/import macros for Windows
// ==========================================================================

#ifdef AS_PLATFORM_WINDOWS
    #ifdef AS_PLUGIN_DYNAMIC
        // Plugin DLLs import these symbols from the host executable
        #define AS_API __declspec(dllimport)
    #else
        // Host executable exports these symbols for plugin DLLs
        #define AS_API __declspec(dllexport)
    #endif
#else
    // Linux/macOS: no special annotations needed
    #define AS_API
#endif

// ==========================================================================
// PluginBuffers
// ==========================================================================

struct AudioPortBuffer {
    float* left   = nullptr;
    float* right  = nullptr;
    int    frames  = 0;
};

struct ControlPortBuffer {
    float  value   = 0.0f;
};

struct EventPortBuffer {
    const std::vector<MidiEvent>* events = nullptr;
    std::vector<MidiEvent>*       output_events = nullptr;
};

/// All port buffers for a plugin, keyed by port ID.
struct PluginBuffers {
    struct AudioMap {
        AS_API AudioPortBuffer* get(const std::string& id);
        AS_API const AudioPortBuffer* get(const std::string& id) const;
        std::vector<std::pair<std::string, AudioPortBuffer>> entries;
    } audio;

    struct ControlMap {
        AS_API ControlPortBuffer* get(const std::string& id);
        AS_API const ControlPortBuffer* get(const std::string& id) const;
        std::vector<std::pair<std::string, ControlPortBuffer>> entries;
    } control;

    struct EventMap {
        AS_API EventPortBuffer* get(const std::string& id);
        AS_API const EventPortBuffer* get(const std::string& id) const;
        std::vector<std::pair<std::string, EventPortBuffer>> entries;
    } events;

    /// Pattern port buffers, keyed by PortDescriptor::id.
    struct PatternMap {
        AS_API PatternPortBuffer* get(const std::string& id);
        AS_API const PatternPortBuffer* get(const std::string& id) const;
        std::vector<std::pair<std::string, PatternPortBuffer>> entries;
    } patterns;
};

// ==========================================================================
// Plugin base class
// ==========================================================================

class Plugin {
public:
    virtual ~Plugin() = default;

    virtual PluginDescriptor descriptor() const = 0;

    virtual void activate(float sample_rate, int max_block_size) {}
    virtual void deactivate() {}
    virtual void configure(const std::string& key, const std::string& value) {}

    virtual void process(const PluginProcessContext& ctx, PluginBuffers& buffers) = 0;

    virtual void note_on (int channel, int pitch, int velocity) { (void)channel; (void)pitch; (void)velocity; }
    virtual void note_off(int channel, int pitch) { (void)channel; (void)pitch; }
    virtual void all_notes_off(int channel = -1) { (void)channel; }
    virtual void pitch_bend(int channel, int value) { (void)channel; (void)value; }
    virtual void program_change(int channel, int bank, int program) { (void)channel; (void)bank; (void)program; }
    virtual void control_change(int channel, int cc, int value) { (void)channel; (void)cc; (void)value; }
    virtual void channel_volume(int channel, int volume) { (void)channel; (void)volume; }
    virtual void note_tune(int channel, int note, float semitones) { (void)channel; (void)note; (void)semitones; }

    virtual void on_transport_stop() {}

    /// Called during graph setup whenever a pattern_source is connected to
    /// this plugin.  The full PatternData (including per-note lyrics) is
    /// available here, before activate() runs, so the plugin can pre-render
    /// anything it needs.  Default implementation is a no-op.
    virtual void on_pattern_connected(const PatternData& /*pd*/) {}

    /// Called from the main thread (in AudioEngine::set_schedule()) once for
    /// each NoteOn event that carries a non-empty lyric, in beat order.
    /// beat:           absolute arrangement beat of the note.
    /// lyric:          lyric syllable text.
    /// pitch:          MIDI note number (0-127), or -1 if unavailable.
    /// duration_beats: duration in beats (from paired NoteOff), or 0.0 if
    ///                 unavailable or no matching NoteOff was found.
    ///
    /// The plugin should accumulate note data for rendering.
    /// Called AFTER activate() — espeak (or other synth) is ready.
    virtual void push_lyric(double /*beat*/, const std::string& /*lyric*/,
                            int /*pitch*/ = -1,
                            double /*duration_beats*/ = 0.0) {}

    /// Called from the main thread after all push_lyric() calls for a given
    /// schedule have been delivered.  The plugin should finalize and publish
    /// its pre-rendered phoneme sequence so the audio thread can read it.
    virtual void on_schedule_loaded() {}

    /// Called from the audio thread when the transport seeks to a new beat
    /// position.  The plugin should reposition its phoneme cursor so that
    /// the next note_on plays the phoneme for the note at or after beat.
    virtual void on_seek(double /*beat*/) {}

    virtual float read_monitor(const std::string& port_id) { (void)port_id; return 0.0f; }
    virtual std::string get_graph_data(const std::string& port_id) { (void)port_id; return "{}"; }
    virtual void set_graph_data(const std::string& port_id, const std::string& json) { (void)port_id; (void)json; }
};

// ==========================================================================
// Plugin registration
// ==========================================================================

using PluginFactory = std::unique_ptr<Plugin>(*)();

struct PluginRegistration {
    std::string   id;
    PluginFactory factory;
};

class PluginRegistry {
public:
    AS_API static void add(PluginRegistration* reg);
    AS_API static const std::vector<PluginRegistration*>& all();
    AS_API static std::unique_ptr<Plugin> create(const std::string& id);
    AS_API static const PluginDescriptor* find_descriptor(const std::string& id);
};

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

#ifdef AS_PLUGIN_DYNAMIC
#  undef REGISTER_PLUGIN
#  define REGISTER_PLUGIN(PluginClass)  /* suppressed: register_plugin() handles it */
#  define REGISTER_PLUGIN_DYNAMIC(PluginClass)                             \
    extern "C" void register_plugin(::PluginRegistry* /*registry*/) {     \
        static ::PluginRegistration _dyn_reg = [] {                        \
            auto tmp = std::make_unique<PluginClass>();                    \
            ::PluginRegistration reg;                                      \
            reg.id = tmp->descriptor().id;                                 \
            reg.factory = []() -> std::unique_ptr<Plugin> {                \
                return std::make_unique<PluginClass>();                     \
            };                                                             \
            return reg;                                                    \
        }();                                                               \
        ::PluginRegistry::add(&_dyn_reg);                                  \
    }
#else
#  define REGISTER_PLUGIN_DYNAMIC(PluginClass)  /* no-op in static build */
#endif
