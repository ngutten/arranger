// risset_rhythm_plugin.cpp
//
// Generates a Risset (Shepard) rhythm illusion by maintaining multiple 
// virtual play-heads over an input pattern. Each play-head moves at a 
// different, continuously accelerating (or decelerating) rate.
//
// Design: Play-head Logic
// -----------------------
// Unlike a standard sampler, each of the N layers has its own 'internal_playhead'.
// This play-head progresses through the pattern at a speed determined by the 
// Risset formula. As a layer accelerates, its play-head moves faster through 
// the pattern, causing notes to trigger earlier and durations to shorten, 
// while the spatial relationship (groove) between notes remains intact.
//
// Speed schedule for layer k
// --------------------------
//   base_speed_k = multiplier ^ (k / N)          
//   cycle_phase  = (beat / cycle_beats) mod 1     
//   layer_phase  = (cycle_phase + k/N) mod 1      
//   speed_mult   = base_bpm_mult * multiplier ^ layer_phase
//
// Velocity envelope
// -----------------
// To mask the "wraparound" where a layer resets from its maximum speed back 
// to its minimum, a cos² envelope is applied to the velocity based on the 
// layer_phase, peaking at the center of the speed sweep.

#include "plugin_api.h"
#include <algorithm>
#include <cmath>
#include <array>
#include <vector>

static constexpr int MAX_LAYERS = 8;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static float param_f(PluginBuffers& b, const char* id, float fallback) {
    auto* p = b.control.get(id);
    return p ? p->value : fallback;
}

static int param_i(PluginBuffers& b, const char* id, int fallback) {
    auto* p = b.control.get(id);
    return p ? static_cast<int>(p->value) : fallback;
}

static MidiEvent make_note_on(int frame, int ch, int note, int vel) {
    MidiEvent e{};
    e.frame   = frame;
    e.status  = static_cast<uint8_t>(0x90 | (ch & 0x0F));
    e.data1   = static_cast<uint8_t>(note);
    e.data2   = static_cast<uint8_t>(std::clamp(vel, 1, 127));
    e.channel = static_cast<uint8_t>(ch);
    return e;
}

static MidiEvent make_note_off(int frame, int ch, int note) {
    MidiEvent e{};
    e.frame   = frame;
    e.status  = static_cast<uint8_t>(0x80 | (ch & 0x0F));
    e.data1   = static_cast<uint8_t>(note);
    e.data2   = 0;
    e.channel = static_cast<uint8_t>(ch);
    return e;
}

// ---------------------------------------------------------------------------
// Layer state
// ---------------------------------------------------------------------------

struct LayerNote {
    int    active_note = -1;
    int    channel     = 0;
    double note_off_at = -1e9;
};

struct Layer {
    double stagger           = 0.0;
    int    note_idx          = 0;
    double internal_playhead = 0.0; // The virtual position within the pattern
    
    std::array<LayerNote, 16> active {};

    bool   initialised = false;
    double last_beat   = -1.0; 
};

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class RissetRhythmPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.risset_rhythm";
        d.display_name = "Risset Rhythm";
        d.category     = "EventEffect";
        d.doc =
            "Generates a Risset rhythm illusion while preserving pattern structure. "
            "Each layer acts as a virtual play-head moving through the pattern "
            "at an accelerating or decelerating rate.";
        d.author  = "builtin";
        d.version = 3;

        d.ports = {
            { "pattern_in", "Pattern In",
              "Connect a Pattern Source. The pattern's internal rhythm is preserved.",
              PluginPortType::Pattern, PortRole::Input },

            { "events_out", "Events Out", "MIDI note stream",
              PluginPortType::Event, PortRole::Output },

            { "num_layers", "Layers",
              "Number of concurrent staggered streams [2..8].",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 4.0f, 2.0f, float(MAX_LAYERS), 1.0f },

            { "multiplier", "Speed Multiplier",
              "Speed ratio covered per layer per cycle (e.g., 2.0 = octave).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 2.0f, 1.25f, 4.0f },

            { "cycle_beats", "Cycle Length (beats)",
              "Total host beats for a layer to complete one speed sweep.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 16.0f, 2.0f, 256.0f },

            { "base_bpm_mult", "Base Speed",
              "Starting playback speed relative to host BPM.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.125f, 4.0f },

            { "direction", "Direction",
              "0 = Accelerating, 1 = Decelerating",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Radio, 0.0f, 0.0f, 1.0f, 1.0f,
              {"Accelerating", "Decelerating"} },
        };

        return d;
    }

    void activate(float /*sample_rate*/, int /*max_block_size*/) override {
        for (auto& l : layers_) {
            l.initialised = false;
            l.last_beat   = -1.0;
            l.internal_playhead = 0.0;
            for (auto& n : l.active) n.active_note = -1;
        }
        last_n_layers_ = 0;
    }

    void deactivate() override {
        activate(0, 0);
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* ev_port = buffers.events.get("events_out");
        if (!ev_port || !ev_port->output_events) return;
        auto& out = *ev_port->output_events;

        const int   n_layers    = std::clamp(param_i(buffers, "num_layers",  4),    2, MAX_LAYERS);
        const float multiplier  = std::clamp(param_f(buffers, "multiplier",  2.0f), 1.25f, 4.0f);
        const float cycle       = std::clamp(param_f(buffers, "cycle_beats", 16.f), 2.0f, 256.0f);
        const float base_mult   = std::clamp(param_f(buffers, "base_bpm_mult", 1.f), 0.125f, 4.0f);
        const int   direction   = std::clamp(param_i(buffers, "direction",   0),    0, 1);

        const PatternData* pattern = nullptr;
        auto* pat_buf = buffers.patterns.get("pattern_in");
        if (pat_buf) pattern = pat_buf->pattern;

        if (!pattern || pattern->notes.empty() || pattern->length_beats <= 0.0) return;

        // Reset if layer count changed
        if (n_layers != last_n_layers_) {
            for (int k = 0; k < n_layers; ++k) {
                layers_[k].stagger = double(k) - (double(n_layers - 1) / 2.0); // Per-layer offset
                layers_[k].internal_playhead = 0.0;
                layers_[k].initialised = false;
                for (auto& n : layers_[k].active) n.active_note = -1;
            }
            last_n_layers_ = n_layers;
        }

        const int    n_notes = static_cast<int>(pattern->notes.size());
        const double pat_len = pattern->length_beats;

        for (int i = 0; i < ctx.block_size; ++i) {
            const double beat = ctx.beat_position + i * ctx.beats_per_sample;

            for (int k = 0; k < n_layers; ++k) {
                Layer& L = layers_[k];

                // 1. Process Note-Offs
                for (auto& ln : L.active) {
                    if (ln.active_note >= 0 && beat >= ln.note_off_at) {
                        out.push_back(make_note_off(i, ln.channel, ln.active_note));
                        ln.active_note = -1;
                    }
                }

                if (ctx.is_playing) {
                    // Calculate Risset Speed with per-layer multiplier logic
                    double raw_phase = std::fmod(beat / double(cycle), 1.0);
                    if (direction == 1) raw_phase = 1.0 - raw_phase;
                    
                    // The exponent now ranges relative to the center layer
                    double layer_exponent = L.stagger + raw_phase; 
                    double speed_mult = double(base_mult) * std::pow(double(multiplier), layer_exponent);

                    // 2. Initialize: Sync to pattern phase without multiplying by speed
                    if (!L.initialised) {
                        L.internal_playhead = std::fmod(beat, pat_len);
                        L.note_idx = 0;
                        while (L.note_idx < n_notes && pattern->notes[L.note_idx].beat < L.internal_playhead) {
                            L.note_idx++;
                        }
                        if (L.note_idx >= n_notes) L.note_idx = 0;
                        L.initialised = true;
                    }

                    // 3. Incrementally advance play-head (Instantaneous Speed)
                    L.internal_playhead += (ctx.beats_per_sample * speed_mult);

                    // 4. Trigger Notes
                    while (L.internal_playhead >= pattern->notes[L.note_idx].beat) {
                        const auto& pn = pattern->notes[L.note_idx];
                        
                        // Normalize phase for the volume envelope [0..1]
                        // We want the volume to dip at the very edges of the total speed range
                        double total_range_phase = std::fmod(layer_exponent + (n_layers/2.0), double(n_layers)) / double(n_layers);
                        double env = std::cos(M_PI * (total_range_phase - 0.5));
                        int vel = static_cast<int>(std::round(pn.velocity * env * env));

                        if (vel > 0) {
                            LayerNote& ln = L.active[pn.channel & 0x0F];
                            if (ln.active_note >= 0) {
                                out.push_back(make_note_off(i, ln.channel, ln.active_note));
                            }
                            out.push_back(make_note_on(i, pn.channel, pn.pitch, vel));
                            ln.active_note = pn.pitch;
                            ln.channel     = pn.channel;
                            ln.note_off_at = beat + (pn.duration / speed_mult);
                        }

                        L.note_idx++;
                        if (L.note_idx >= n_notes) {
                            L.note_idx = 0;
                            L.internal_playhead -= pat_len;
                        }
                    }
                } else {
                    L.initialised = false;
                }
            }
        }
    }

private:
    std::array<Layer, MAX_LAYERS> layers_ {};
    int last_n_layers_ = 0;
};

REGISTER_PLUGIN(RissetRhythmPlugin);
REGISTER_PLUGIN_DYNAMIC(RissetRhythmPlugin);

std::unique_ptr<Plugin> make_risset_plugin() {
    return std::make_unique<RissetRhythmPlugin>();
}
