// risset_rhythm_plugin.cpp
//
// Generates a Risset rhythm: the rhythmic analogue of a Shepard tone.
// Multiple isochronous layers are stacked in log-tempo space and swept
// continuously in one direction, with velocity envelopes that fade each
// layer in/out at the octave boundaries.  The composite stream gives the
// illusion of perpetual acceleration or deceleration.
//
// Theory
// ------
// N layers share a "tempo window" that spans one rhythmic octave (a 2× range
// of inter-onset intervals, IOI).  Layer k has a base IOI of:
//
//     ioi_k(t) = ioi_center * 2^( phase_k - sweep(t) )
//
// where phase_k = k/N  and  sweep(t) = t / cycle_beats  (mod 1, direction ±1).
//
// Each layer's velocity is shaped by a bell envelope over its fractional
// log-tempo position within the octave window, so layers that are drifting
// off the fast or slow extreme fade out while new ones fade in from the
// opposite extreme — exactly as Shepard tones fade harmonics in/out at
// the spectral extremes.
//
// Parameters
// ----------
//   note           – MIDI note number [0,127]        default 60
//   velocity       – Peak velocity [1,127]           default 100
//   channel        – MIDI channel [0,15]             default 0
//   direction      – 0 = accelerating, 1 = decelerating  default 0
//   ioi_center     – Central inter-onset interval (beats) at which layers
//                    sound loudest.  This is the "perceived" tempo.
//                    [0.125, 4.0]  default 0.5  (eighth-note at base tempo)
//   cycle_beats    – How many beats to traverse one full tempo octave.
//                    Longer = slower sweep = more gradual illusion.
//                    [4, 256]  default 32
//   num_layers     – Number of concurrent streams [2, 8]  default 4
//   gate           – Note length as fraction of IOI [0.05, 0.95]  default 0.4
//   spread         – Width of the velocity bell in log-octave units [0.2, 1.0]
//                    default 0.7.  Wider = more overlap, smoother crossfades;
//                    narrower = more distinct layers.
//
// Notes
// -----
// * The plugin is self-contained (no note_on input); it generates its own
//   rhythmic stream and is meant to feed a synth / sampler Event input.
// * All timing is beat-position based, so it is fully tempo-map aware.
// * Each layer tracks its own next-onset beat independently; they are
//   initialised with fractional offsets so the first few beats do not all
//   pile up at t=0.

#include "plugin_api.h"
#include <algorithm>
#include <cmath>
#include <array>

// Maximum number of layers we ever allocate (num_layers <= MAX_LAYERS).
static constexpr int MAX_LAYERS = 8;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static inline float param_f(PluginBuffers& b, const char* id, float fallback) {
    auto* p = b.control.get(id);
    return p ? p->value : fallback;
}

static inline int param_i(PluginBuffers& b, const char* id, int fallback) {
    auto* p = b.control.get(id);
    return p ? static_cast<int>(p->value) : fallback;
}

// Gaussian bell over x in [0,1] — value is 1 at x=0.5, falls to ~exp(-2)
// at the edges when sigma=spread.  We use a symmetric wrapped version so
// layers rolling past the 0/1 boundary (octave edge) blend correctly.
static float bell(float x, float spread) {
    // Centre at 0.5; wrap x to [-0.5, 0.5] distance from centre.
    float d = x - 0.5f;
    // Exponent: gaussian with half-width = spread.
    float sigma = spread * 0.5f;
    return std::exp(-(d * d) / (2.0f * sigma * sigma));
}

static MidiEvent make_note_on(int frame, int ch, int note, int vel) {
    MidiEvent e;
    e.frame   = frame;
    e.status  = static_cast<uint8_t>(0x90 | (ch & 0x0F));
    e.data1   = static_cast<uint8_t>(note);
    e.data2   = static_cast<uint8_t>(std::clamp(vel, 1, 127));
    e.channel = static_cast<uint8_t>(ch);
    return e;
}

static MidiEvent make_note_off(int frame, int ch, int note) {
    MidiEvent e;
    e.frame   = frame;
    e.status  = static_cast<uint8_t>(0x80 | (ch & 0x0F));
    e.data1   = static_cast<uint8_t>(note);
    e.data2   = 0;
    e.channel = static_cast<uint8_t>(ch);
    return e;
}

// ---------------------------------------------------------------------------

class RissetRhythmPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.risset_rhythm";
        d.display_name = "Risset Rhythm";
        d.category     = "EventGen";
        d.doc =
            "Generates a Risset (Shepard-tone) rhythm: N isochronous layers "
            "sweeping through a rhythmic octave in log-tempo space with "
            "velocity crossfades, creating the illusion of endless acceleration "
            "or deceleration.  Connect to any synth or sampler Event input.";
        d.author  = "builtin";
        d.version = 1;

        d.ports = {
            { "events_out", "Events Out", "MIDI note stream",
              PluginPortType::Event, PortRole::Output },

            { "note", "Note", "MIDI note to trigger",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 60.0f, 0.0f, 127.0f, 1.0f },

            { "velocity", "Peak Velocity",
              "Maximum MIDI velocity (envelope scales this per layer)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 100.0f, 1.0f, 127.0f, 1.0f },

            { "channel", "Channel", "MIDI channel (0-based)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 0.0f, 0.0f, 15.0f, 1.0f },

            { "direction", "Direction",
              "Accelerating: layers sweep toward faster tempos. "
              "Decelerating: layers sweep toward slower tempos.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Radio, 0.0f, 0.0f, 1.0f, 1.0f,
              {"Accelerating", "Decelerating"} },

            { "ioi_center", "IOI Center (beats)",
              "Inter-onset interval (in beats) at which layers sound loudest — "
              "the perceived rhythmic tempo. E.g. 0.5 = eighth notes at current BPM.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.125f, 4.0f },

            { "cycle_beats", "Cycle Length (beats)",
              "How many beats to complete one full tempo-octave sweep. "
              "Longer = more gradual illusion.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 32.0f, 4.0f, 256.0f },

            { "num_layers", "Layers",
              "Number of concurrent isochronous streams [2..8]. "
              "More layers = denser, smoother crossfade.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 4.0f, 2.0f, float(MAX_LAYERS), 1.0f },

            { "gate", "Gate",
              "Note length as a fraction of the layer's current IOI.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.4f, 0.05f, 0.95f },

            { "spread", "Velocity Spread",
              "Width of the per-layer velocity bell in log-octave units. "
              "Wider = more overlap and smoother crossfades between layers.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.7f, 0.2f, 1.0f },
        };

        return d;
    }

    void activate(float /*sample_rate*/, int /*max_block_size*/) override {
        initialised_ = false;
    }

    void deactivate() override {
        initialised_ = false;
        for (auto& l : layers_) l.active_note = -1;
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* ev_port = buffers.events.get("events_out");
        if (!ev_port || !ev_port->output_events) return;
        auto& out = *ev_port->output_events;

        const int   note       = std::clamp(param_i(buffers, "note",       60),  0, 127);
        const int   vel_peak   = std::clamp(param_i(buffers, "velocity",   100), 1, 127);
        const int   channel    = std::clamp(param_i(buffers, "channel",    0),   0, 15);
        const int   direction  = std::clamp(param_i(buffers, "direction",  0),   0, 1);
        const float ioi_center = std::clamp(param_f(buffers, "ioi_center", 0.5f), 0.125f, 4.0f);
        const float cycle      = std::clamp(param_f(buffers, "cycle_beats",32.f), 4.0f, 256.0f);
        const int   n_layers   = std::clamp(param_i(buffers, "num_layers", 4),    2, MAX_LAYERS);
        const float gate       = std::clamp(param_f(buffers, "gate",       0.4f), 0.05f, 0.95f);
        const float spread     = std::clamp(param_f(buffers, "spread",     0.7f), 0.2f, 1.0f);

        // On first process call (or after layer-count change), seed per-layer
        // next-onset times.  We stagger them uniformly over one ioi_center
        // interval so the first few beats don't all pile up.
        if (!initialised_ || n_layers != last_n_layers_) {
            const double now = ctx.beat_position;
            for (int k = 0; k < n_layers; ++k) {
                float phase = float(k) / float(n_layers); // evenly spaced in [0,1)
                // Simple uniform stagger: layer k fires k/N * ioi_center beats
                // into the future.  This is approximate but avoids a burst on
                // the first block regardless of layer IOI spread.
                layers_[k].next_onset   = now + phase * double(ioi_center);
                layers_[k].active_note  = -1;
                layers_[k].note_off_at  = -1e9;
                layers_[k].phase_offset = phase;
            }
            initialised_   = true;
            last_n_layers_ = n_layers;
        }

        // Process sample by sample so onset and note-off times are
        // placed accurately within the block.
        for (int i = 0; i < ctx.block_size; ++i) {
            const double beat = ctx.beat_position + i * ctx.beats_per_sample;

            for (int k = 0; k < n_layers; ++k) {
                Layer& L = layers_[k];

                // Note-off for this layer's active note
                if (L.active_note >= 0 && beat >= L.note_off_at) {
                    out.push_back(make_note_off(i, channel, L.active_note));
                    L.active_note = -1;
                }

                // Onset: fire and schedule next
                if (beat >= L.next_onset) {
                    // Sweep position in [0,1) over the cycle.
                    // Accelerating: sweep grows → per-layer pos grows → IOI shrinks.
                    // Decelerating: sweep grows → pos shrinks → IOI grows.
                    double sweep_raw = std::fmod(beat / cycle, 1.0);
                    float  sweep     = (direction == 0)
                                        ? float(sweep_raw)
                                        : 1.0f - float(sweep_raw);

                    // Fractional log-octave position of this layer within [0,1).
                    float pos = std::fmod(L.phase_offset + sweep, 1.0f);

                    // Current IOI for this layer.
                    double ioi = ioi_at(ioi_center, pos, 0.0f);

                    // Velocity envelope — bell centred at 0.5 in log-octave space.
                    float env = bell(pos, spread);
                    int   vel = static_cast<int>(std::round(vel_peak * env));
                    vel = std::clamp(vel, 0, 127);

                    if (vel > 0) {
                        // Kill any lingering note on this layer.
                        if (L.active_note >= 0) {
                            out.push_back(make_note_off(i, channel, L.active_note));
                        }
                        out.push_back(make_note_on(i, channel, note, vel));
                        L.active_note = note;
                        L.note_off_at = L.next_onset + ioi * gate;
                    }

                    // Schedule next onset from the current one (not from now),
                    // so drift doesn't accumulate.
                    L.next_onset += ioi;
                }
            }
        }
    }

private:
    struct Layer {
        double next_onset  = 0.0;
        double note_off_at = -1e9;
        float  phase_offset = 0.0f;
        int    active_note  = -1;
    };

    std::array<Layer, MAX_LAYERS> layers_ {};
    bool initialised_   = false;
    int  last_n_layers_ = 0;

    // IOI for a layer at log-octave position `pos` in [0,1).
    // pos=0.5 → ioi_center; pos=0 → ioi_center*2^(-0.5); pos=1 → ioi_center*2^(+0.5).
    // The sweep parameter shifts all layers; here we fold it into pos already
    // at the call site, so sweep=0 here.
    static double ioi_at(float ioi_center, float pos, float /*sweep*/) {
        // Map pos in [0,1) to log-octave offset in [-0.5, 0.5).
        float log_offset = pos - 0.5f;
        return double(ioi_center) * std::pow(2.0, double(log_offset));
    }
};

REGISTER_PLUGIN(RissetRhythmPlugin);
REGISTER_PLUGIN_DYNAMIC(RissetRhythmPlugin);

std::unique_ptr<Plugin> make_risset_rhythm_plugin() {
    return std::make_unique<RissetRhythmPlugin>();
}
