// shepard_tone_plugin.cpp
//
// Shepard-tone arpeggiator with multiple interleaved layers and
// configurable step density (octaves, scales, chords).
//
// Instead of jumping by a fixed octave each step, the plugin builds a voice
// list by tiling a chosen interval pattern upward across `octaves` octaves
// of range.  Examples:
//
//   Octaves   : [0, 12, 24, 36, 48]          (one note per octave)
//   Major     : [0,2,4,5,7,9,11,12,14, ...]  (scale degrees tiled up)
//   Major Triad: [0,4,7,12,16,19,24, ...]    (chord tones tiled up)
//
// Each layer steps through this list sequentially.  Velocity is shaped by a
// Gaussian bell over semitone position so top/bottom voices fade out and the
// octave wrap is inaudible.
//
// Multiple layers are evenly staggered in both time and position within the
// voice list, so there is always a layer near the loud centre of the bell.
//
// Parameters
// ----------
//   velocity    - Peak MIDI velocity             [1, 127]    default 100
//   channel     - MIDI channel                   [0, 15]     default 0
//   direction   - 0 = ascending, 1 = descending              default 0
//   rate        - Step length in beats           [0.0625, 4] default 0.25
//   gate        - Note length / step length      [0.05, 0.95]default 0.8
//   octaves     - Range to tile the pattern over [1, 6]      default 4
//   base_octave - Lowest octave (4 = middle C)   [0, 7]      default 2
//   spread      - Bell sigma in semitones         [1, 24]     default 10
//   num_layers  - Interleaved streams             [1, 4]      default 3
//   spacing     - Interval pattern (see choices)             default Octaves

#include "plugin_api.h"
#include <algorithm>
#include <cmath>
#include <vector>
#include <array>

static constexpr int MAX_LAYERS = 4;
static constexpr int MAX_VOICES = 64;  // plenty for 6 octaves of any scale

// ---------------------------------------------------------------------------
// Interval patterns
// Each pattern is a set of semitone offsets within one octave (12 semitones),
// terminated by -1.  They are tiled upward to fill the requested range.
// ---------------------------------------------------------------------------

struct PatternDef {
    const char* name;
    int intervals[13];   // max 12 degrees + sentinel
};

static const PatternDef PATTERNS[] = {
    { "Octaves",          { 0, -1 } },
    { "Maj Triad",        { 0, 4, 7, -1 } },
    { "Min Triad",        { 0, 3, 7, -1 } },
    { "Maj 7th",          { 0, 4, 7, 11, -1 } },
    { "Dom 7th",          { 0, 4, 7, 10, -1 } },
    { "Min 7th",          { 0, 3, 7, 10, -1 } },
    { "Major",            { 0, 2, 4, 5, 7, 9, 11, -1 } },
    { "Natural Minor",    { 0, 2, 3, 5, 7, 8, 10, -1 } },
    { "Major Pentatonic", { 0, 2, 4, 7, 9, -1 } },
    { "Minor Pentatonic", { 0, 3, 5, 7, 10, -1 } },
    { "Blues",            { 0, 3, 5, 6, 7, 10, -1 } },
    { "Whole Tone",       { 0, 2, 4, 6, 8, 10, -1 } },
    { "Chromatic",        { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, -1 } },
};
static constexpr int NUM_PATTERNS = static_cast<int>(sizeof(PATTERNS) / sizeof(PATTERNS[0]));

// Build the voice list: tile the chosen pattern upward until we exceed
// `octaves * 12` semitones above the root.  Returns semitone offsets from
// the root (not absolute MIDI pitches).
static int build_voices(int pattern_idx, int octaves, int out[MAX_VOICES]) {
    const int* ivs = PATTERNS[pattern_idx].intervals;

    // Count intervals in one tile.
    int tile_size = 0;
    while (tile_size < 12 && ivs[tile_size] >= 0) ++tile_size;
    if (tile_size == 0) { out[0] = 0; return 1; }

    const int range = octaves * 12;
    int n = 0;
    for (int oct = 0; oct * 12 < range && n < MAX_VOICES; ++oct) {
        for (int d = 0; d < tile_size && n < MAX_VOICES; ++d) {
            int semi = oct * 12 + ivs[d];
            if (semi >= range) goto done;
            out[n++] = semi;
        }
    }
    // Include the octave-cap note so the top of the bell has a voice to fade into.
    if (n < MAX_VOICES) out[n++] = range;
done:
    return n;
}

// ---------------------------------------------------------------------------

static inline float param_f(PluginBuffers& b, const char* id, float fallback) {
    auto* p = b.control.get(id);
    return p ? p->value : fallback;
}
static inline int param_i(PluginBuffers& b, const char* id, int fallback) {
    auto* p = b.control.get(id);
    return p ? static_cast<int>(p->value + 0.5f) : fallback;
}
static MidiEvent make_note_on(int frame, int ch, int pitch, int vel) {
    MidiEvent e;
    e.frame   = frame;
    e.status  = static_cast<uint8_t>(0x90 | (ch & 0x0F));
    e.data1   = static_cast<uint8_t>(pitch);
    e.data2   = static_cast<uint8_t>(std::clamp(vel, 1, 127));
    e.channel = static_cast<uint8_t>(ch);
    return e;
}
static MidiEvent make_note_off(int frame, int ch, int pitch) {
    MidiEvent e;
    e.frame   = frame;
    e.status  = static_cast<uint8_t>(0x80 | (ch & 0x0F));
    e.data1   = static_cast<uint8_t>(pitch);
    e.data2   = 0;
    e.channel = static_cast<uint8_t>(ch);
    return e;
}

// ---------------------------------------------------------------------------

class ShepardTonePlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.shepard_tone";
        d.display_name = "Shepard Tone";
        d.category     = "EventEffect";
        d.doc =
            "Shepard-tone arpeggiator with configurable step density. "
            "Builds a voice list by tiling a chosen interval pattern (octaves, "
            "triads, scales, etc.) upward across N octaves, then steps through "
            "it with multiple interleaved layers. Gaussian velocity shaping "
            "fades the top and bottom voices so the wrap is inaudible, "
            "creating the illusion of endless ascent or descent.";
        d.author  = "builtin";
        d.version = 1;

        std::vector<std::string> pattern_names;
        for (int i = 0; i < NUM_PATTERNS; ++i)
            pattern_names.push_back(PATTERNS[i].name);

        d.ports = {
            { "events_in", "Events In",
              "Held notes. Pitch class of the lowest note drives the sequence.",
              PluginPortType::Event, PortRole::Input },

            { "events_out", "Events Out", "Shepard arpeggio output",
              PluginPortType::Event, PortRole::Output },

            { "spacing", "Spacing",
              "Interval pattern tiled upward to build the voice list.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, float(NUM_PATTERNS - 1), 1.0f,
              pattern_names },

            { "direction", "Direction",
              "Ascending or descending.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Radio, 0.0f, 0.0f, 1.0f, 1.0f,
              {"Ascending", "Descending"} },

            { "rate", "Rate (beats)",
              "Step length in beats.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.25f, 0.0625f, 4.0f },

            { "gate", "Gate",
              "Note length as a fraction of the step length.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.8f, 0.05f, 0.95f },

            { "octaves", "Octaves",
              "Semitone range to tile the pattern across (in octaves).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 4.0f, 1.0f, 6.0f, 1.0f },

            { "base_octave", "Base Octave",
              "Lowest octave of the voice stack (4 = middle C).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 2.0f, 0.0f, 7.0f, 1.0f },

            { "spread", "Spread (semitones)",
              "Gaussian sigma of the velocity bell. Wider = more voices loud "
              "at once. Should be set relative to the density of the pattern: "
              "~10 for octaves/triads, ~5 for full scales.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 10.0f, 1.0f, 24.0f },

            { "num_layers", "Layers",
              "Interleaved arpeggio streams, evenly spread through the voice list.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 3.0f, 1.0f, float(MAX_LAYERS), 1.0f },

            { "velocity", "Peak Velocity",
              "MIDI velocity at the centre of the bell.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 100.0f, 1.0f, 127.0f, 1.0f },

            { "channel", "Channel", "MIDI channel (0-based)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 0.0f, 0.0f, 15.0f, 1.0f },
        };

        return d;
    }

    void activate(float /*sample_rate*/, int /*max_block_size*/) override {
        pitch_class_   = 0;
        last_n_layers_ = 0;
        last_n_voices_ = 0;
        for (auto& l : layers_) {
            l.step_index      = 0;
            l.current_note    = -1;
            l.current_channel = 0;
            l.last_step_beat  = 0.0;
            l.note_on_beat    = 0.0;
            l.needs_init      = true;
        }
    }

    void deactivate() override {
        for (auto& l : layers_) l.current_note = -1;
    }

    void note_on(int channel, int pitch, int /*velocity*/) override {
        held_notes_.erase(
            std::remove_if(held_notes_.begin(), held_notes_.end(),
                [&](const HeldNote& n){ return n.pitch == pitch && n.channel == channel; }),
            held_notes_.end());
        held_notes_.push_back({channel, pitch});
        update_pitch_class_();
    }

    void note_off(int channel, int pitch) override {
        held_notes_.erase(
            std::remove_if(held_notes_.begin(), held_notes_.end(),
                [&](const HeldNote& n){ return n.pitch == pitch && n.channel == channel; }),
            held_notes_.end());
        update_pitch_class_();
    }

    void all_notes_off(int channel) override {
        if (channel == -1) held_notes_.clear();
        else held_notes_.erase(
            std::remove_if(held_notes_.begin(), held_notes_.end(),
                [&](const HeldNote& n){ return n.channel == channel; }),
            held_notes_.end());
        update_pitch_class_();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* ev_in = buffers.events.get("events_in");
        if (ev_in && ev_in->events) {
            for (const auto& e : *ev_in->events) {
                int status = e.status & 0xF0;
                if (status == 0x90 && e.data2 > 0)
                    note_on(e.channel, e.data1, e.data2);
                else if (status == 0x80 || (status == 0x90 && e.data2 == 0))
                    note_off(e.channel, e.data1);
            }
        }

        auto* ev_out = buffers.events.get("events_out");
        if (!ev_out || !ev_out->output_events) return;
        auto& out = *ev_out->output_events;

        const int   vel_peak   = std::clamp(param_i(buffers, "velocity",    100), 1, 127);
        const int   channel    = std::clamp(param_i(buffers, "channel",     0),   0, 15);
        const int   direction  = std::clamp(param_i(buffers, "direction",   0),   0, 1);
        const float step_beats = std::clamp(param_f(buffers, "rate",        0.25f), 0.0625f, 4.0f);
        const float gate       = std::clamp(param_f(buffers, "gate",        0.8f),  0.05f, 0.95f);
        const int   octaves    = std::clamp(param_i(buffers, "octaves",     4),     1, 6);
        const int   base_oct   = std::clamp(param_i(buffers, "base_octave", 2),     0, 7);
        const float spread     = std::clamp(param_f(buffers, "spread",      10.0f), 1.0f, 24.0f);
        const int   n_layers   = std::clamp(param_i(buffers, "num_layers",  3),     1, MAX_LAYERS);
        const int   pattern    = std::clamp(param_i(buffers, "spacing",     0),     0, NUM_PATTERNS - 1);

        // Pitch class comes from the lowest held note.
        const int   pc         = pitch_class_;

        // Build voice list (semitone offsets from the root).
        int  voice_semis[MAX_VOICES];
        int  n_voices = build_voices(pattern, octaves, voice_semis);

        // Bell centre in semitone space: midpoint of [0, octaves*12].
        // voice_semis[n_voices-1] is always the range cap (== octaves*12),
        // so this is just range/2.
        const float bell_centre = static_cast<float>(octaves * 12) * 0.5f;

        // Re-seed layer offsets when voice count or layer count changes.
        const bool reseed = (n_layers != last_n_layers_ || n_voices != last_n_voices_);
        if (reseed) {
            for (auto& l : layers_) {
                if (l.current_note >= 0) {
                    out.push_back(make_note_off(0, l.current_channel, l.current_note));
                    l.current_note = -1;
                }
                l.needs_init = true;
            }
            for (int k = 0; k < n_layers; ++k)
                layers_[k].step_index = (k * n_voices) / n_layers;
            last_n_layers_ = n_layers;
            last_n_voices_ = n_voices;
        }

        // On first use (or after reseed), anchor each layer's grid to the
        // current beat so all stagger offsets are measured from a live position
        // rather than from -infinity.  This prevents every layer from firing
        // simultaneously on the first block.
        for (int k = 0; k < n_layers; ++k) {
            if (layers_[k].needs_init) {
                const double layer_offset = (static_cast<double>(k) / n_layers) * step_beats;
                const double adj0 = ctx.beat_position - layer_offset;
                layers_[k].last_step_beat = std::floor(adj0 / step_beats) * step_beats;
                layers_[k].needs_init = false;
            }
        }

        const double gate_beats = step_beats * gate;

        for (int i = 0; i < ctx.block_size; ++i) {
            const double beat = ctx.beat_position + i * ctx.beats_per_sample;

            for (int k = 0; k < n_layers; ++k) {
                Layer& L = layers_[k];

                // Time-stagger: layer k's grid is offset by k/n_layers of a step.
                double layer_offset = (static_cast<double>(k) / n_layers) * step_beats;
                double adj_beat     = beat - layer_offset;
                double adj_step     = std::floor(adj_beat / step_beats) * step_beats;

                // Gate: cut note after gate_beats.
                if (L.current_note >= 0 && (beat - L.note_on_beat) >= gate_beats) {
                    out.push_back(make_note_off(i, L.current_channel, L.current_note));
                    L.current_note = -1;
                }

                // New step for this layer.
                if (adj_step > L.last_step_beat + step_beats * 0.5) {
                    L.last_step_beat = adj_step;

                    if (L.current_note >= 0) {
                        out.push_back(make_note_off(i, L.current_channel, L.current_note));
                        L.current_note = -1;
                    }

                    L.step_index = L.step_index % n_voices;

                    // Voice index: ascending steps up the list, descending reverses.
                    int vi = (direction == 0)
                        ? L.step_index
                        : (n_voices - 1 - L.step_index);

                    // Semitone offset for this voice, then absolute MIDI pitch.
                    int semi  = voice_semis[vi];
                    int pitch = (base_oct * 12) + pc + semi;
                    pitch = std::clamp(pitch, 0, 127);

                    // Bell over semitone position.
                    float d   = static_cast<float>(semi) - bell_centre;
                    float env = std::exp(-(d * d) / (2.0f * spread * spread));
                    int   vel = std::clamp(static_cast<int>(std::round(vel_peak * env)), 1, 127);

                    out.push_back(make_note_on(i, L.current_channel = channel, pitch, vel));
                    L.current_note = pitch;
                    L.note_on_beat = beat;

                    L.step_index = (L.step_index + 1) % n_voices;
                }
            }
        }
    }

private:
    struct HeldNote { int channel, pitch; };

    struct Layer {
        int    step_index      = 0;
        int    current_note    = -1;
        int    current_channel = 0;
        double last_step_beat  = 0.0;
        double note_on_beat    = 0.0;
        bool   needs_init      = true;
    };

    std::vector<HeldNote>         held_notes_;
    std::array<Layer, MAX_LAYERS> layers_;
    int  pitch_class_   = 0;
    int  last_n_layers_ = 0;
    int  last_n_voices_ = 0;

    void update_pitch_class_() {
        if (held_notes_.empty()) return;
        int lowest = held_notes_[0].pitch;
        for (const auto& n : held_notes_)
            if (n.pitch < lowest) lowest = n.pitch;
        pitch_class_ = lowest % 12;
    }
};

REGISTER_PLUGIN(ShepardTonePlugin);
REGISTER_PLUGIN_DYNAMIC(ShepardTonePlugin);

std::unique_ptr<Plugin> make_shepard_tone_plugin() {
    return std::make_unique<ShepardTonePlugin>();
}
