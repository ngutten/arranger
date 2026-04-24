// percussion_synth_plugin.cpp
// Noise/pitched drum synthesizer.
//
// A noise source (white / pink / brown) plus an optional pitched component
// (sine / triangle) routed through a state-variable filter whose cutoff is
// swept by the amp envelope. Default ADSR is tuned for percussion
// (1ms attack, 150ms decay, 0 sustain).
//
// Granular impact gating: when `impact_rate` is below its max value, the
// noise source is gated by a PhISEM-style stochastic process (Perry Cook,
// 1996) — discrete impacts kick an internal amplitude level that decays
// between them, producing distinct grains hitting the filter rather than
// continuous noise. Used for maraca / shaker / cabasa textures.
//
// Preset-driven usage covers kick / snare / hat / tom / triangle / cymbal /
// maracas / shaker; see the preset list at the bottom of descriptor().

#include "plugin_api.h"
#include "synth_common.h"

#include <cmath>
#include <cstdint>
#include <cstring>

// ---------------------------------------------------------------------------
// Random source
// ---------------------------------------------------------------------------

struct XorShift {
    uint32_t state = 0x9E3779B9u;
    inline float next() {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        return static_cast<float>(static_cast<int32_t>(state)) / 2147483648.0f;
    }
    // Uniform [0, 1) — useful for probability tests.
    inline float next_uniform() {
        return 0.5f * (next() + 1.0f);
    }
};

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

enum class NoiseColor { White = 0, Pink = 1, Brown = 2 };
enum class ToneShape  { Sine = 0, Triangle = 1 };
enum class FilterMode { LP = 0, HP = 1, BP = 2 };

// Top of the impact_rate range. At or above this Hz, gating is skipped
// entirely and noise passes through as pure continuous signal — matches
// the pre-granular behavior for presets that don't want any grain texture.
static constexpr float IMPACT_RATE_CONTINUOUS = 19999.0f;

// ---------------------------------------------------------------------------
// Voice extension
// ---------------------------------------------------------------------------

struct PercussionExt {
    // SVF (Cytomic/Simper)
    float svf_ic1eq = 0.0f;
    float svf_ic2eq = 0.0f;

    // Pink noise state (Paul Kellet approximation, 7 coefficients)
    float pink_b0 = 0.0f, pink_b1 = 0.0f, pink_b2 = 0.0f, pink_b3 = 0.0f;
    float pink_b4 = 0.0f, pink_b5 = 0.0f, pink_b6 = 0.0f;

    // Brown noise state (leaky integrator of white)
    float brown = 0.0f;

    // Granular impact gate state — amplitude envelope that decays between
    // discrete collisions. Unused when impact_rate is continuous.
    float snd_level = 0.0f;
    // Countdown (in samples) until the next impact fires. At note-on it
    // starts at 0 so the first process() sample fires the opening grain;
    // after that, each fire resets the countdown based on impact_rate +
    // regularity.
    float next_impact_in_samples = 0.0f;
};

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class PercussionSynthPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.percussion_synth";
        d.display_name = "Percussion Synth";
        d.category     = "Synth";
        d.doc          = "Noise-based drum synth. Selectable noise color "
                         "(white / pink / brown), optional tonal layer, "
                         "state-variable filter with envelope-swept cutoff, "
                         "and PhISEM-style granular impact gating for "
                         "maraca/shaker textures.";
        d.author       = "builtin";
        d.version      = 2;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output.",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain", "Output level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.8f, 0.0f, 2.0f },

            // Noise source
            { "noise_type", "Noise Type", "Noise spectrum.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 2.0f, 1.0f,
              { "White", "Pink", "Brown" } },
            { "noise_level", "Noise Level", "Noise source gain.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f },

            // Granular impact gating (PhISEM)
            { "impact_rate", "Impact Rate",
              "Collisions per second. 20000 (max) = continuous noise; "
              "lower values produce distinct grains.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 20000.0f, 1.0f, 20000.0f },
            { "impact_ring", "Impact Ring",
              "Ring time of each impact (seconds). Higher = longer audible "
              "tail between collisions.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.05f, 0.001f, 2.0f },
            { "regularity", "Regularity",
              "0 = Poisson (random bead-like hits); 1 = evenly spaced "
              "(rasp / guiro); intermediate blends between the two.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },

            // Tonal layer
            { "tone_level", "Tone Level", "Pitched component gain.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "tone_shape", "Tone Shape", "Pitched component waveform.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 1.0f, 1.0f,
              { "Sine", "Triangle" } },
            { "tone_transpose", "Tone Transpose",
              "Pitched component offset (semitones). Goes up 4 octaves "
              "so bell/triangle-style presets can place the fundamental "
              "directly in the BP passband.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -48.0f, 48.0f },

            // Filter
            { "filter_mode", "Filter Mode", "Filter type.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 2.0f, 0.0f, 2.0f, 1.0f,
              { "LP", "HP", "BP" } },
            { "cutoff", "Cutoff", "Filter cutoff Hz (baseline).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1000.0f, 20.0f, 20000.0f },
            { "resonance", "Resonance", "Filter resonance.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.4f, 0.0f, 1.0f },
            { "env_cutoff", "Env → Cut",
              "Env-driven cutoff sweep in octaves.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 3.0f, -6.0f, 6.0f },

            // ADSR — percussion defaults
            { "attack", "Attack", "ADSR attack (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.001f, 0.0f, 2.0f, 0.0f, {}, "", false },
            { "decay", "Decay", "ADSR decay (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.15f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "sustain", "Sustain", "ADSR sustain level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "release", "Release", "ADSR release (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.05f, 0.0f, 4.0f, 0.0f, {}, "", false },
        };

        // Noise color indices: 0 = White, 1 = Pink, 2 = Brown
        // Tone shape indices:   0 = Sine,  1 = Triangle
        // Filter mode indices:  0 = LP,    1 = HP,     2 = BP
        d.presets = {
            { "Taiko", {
                { "noise_type",     2.0f    },  // Brown
                { "noise_level",    0.2f    },  // was 0.4 — let tone dominate
                { "tone_level",     1.0f    },  // was 0.9 — more present
                { "tone_shape",     0.0f    },  // Sine
                { "tone_transpose", -12.0f  },  // was -24 — C3 not C2
                { "filter_mode",    0.0f    },  // LP
                { "cutoff",         450.0f  },
                { "resonance",      0.1f    },
                { "env_cutoff",     0.0f    },  // static LP — no doppler
                { "attack",         0.001f  },
                { "decay",          0.7f    },
                { "sustain",        0.0f    },
                { "release",        0.2f    },
            }},
            { "Triangle", {
                // Pure tone: sine at ~4186 Hz (MIDI 60 + 48 = E8) for a
                // high bright bell. No noise means the BP trick from
                // earlier presets doesn't help — the BP's output is the
                // input attenuated by bandwidth, not amplified. Use a
                // gentle LP so the sine passes unattenuated. Triangle
                // character comes from the pitch register + envelope.
                { "noise_type",     0.0f    },
                { "noise_level",    0.0f    },
                { "tone_level",     1.0f    },
                { "tone_shape",     0.0f    },  // Sine
                { "tone_transpose", 48.0f   },
                { "filter_mode",    0.0f    },  // LP
                { "cutoff",         8000.0f },
                { "resonance",      0.1f    },
                { "env_cutoff",     0.0f    },
                { "attack",         0.001f  },
                { "decay",          0.6f    },
                { "sustain",        0.15f   },
                { "release",        0.5f    },
            }},
            { "Cymbal", {
                // BP at mid-high with gentle resonance, replacing the
                // previous broadband-HP design that was piercing. Real
                // cymbals have focused spectral peaks, not raw hiss.
                { "gain",           0.55f   },  // lower: noise-heavy preset
                { "noise_type",     0.0f    },  // White
                { "noise_level",    1.0f    },
                { "tone_level",     0.0f    },
                { "filter_mode",    2.0f    },  // BP (was HP)
                { "cutoff",         4500.0f },
                { "resonance",      0.2f    },  // was 0.5 — no squeal
                { "env_cutoff",     0.8f    },  // was 2.0 — gentle sweep
                { "attack",         0.002f  },
                { "decay",          1.2f    },
                { "sustain",        0.0f    },
                { "release",        1.0f    },
            }},
            { "Hi-hat (closed)", {
                { "noise_type",     0.0f    },  // White
                { "noise_level",    1.0f    },
                { "tone_level",     0.0f    },
                { "filter_mode",    1.0f    },  // HP
                { "cutoff",         7000.0f },
                { "resonance",      0.3f    },
                { "env_cutoff",     1.5f    },
                { "attack",         0.0005f },
                { "decay",          0.04f   },
                { "sustain",        0.0f    },
                { "release",        0.02f   },
            }},
            { "Maracas", {
                { "noise_type",     0.0f    },  // White
                { "noise_level",    1.0f    },
                { "impact_rate",    300.0f  },  // was 55 — denser
                { "impact_ring",    0.018f  },
                { "regularity",     0.15f   },  // slight coherence
                { "tone_level",     0.0f    },
                { "filter_mode",    2.0f    },  // BP
                { "cutoff",         5000.0f },
                { "resonance",      0.25f   },
                { "env_cutoff",     0.5f    },
                { "attack",         0.015f  },
                { "decay",          0.3f    },
                { "sustain",        0.0f    },
                { "release",        0.05f   },
            }},
            { "Shaker", {
                // Sparser, woodier cousin of maracas — like a cabasa.
                { "noise_type",     1.0f    },  // Pink
                { "noise_level",    1.0f    },
                { "impact_rate",    80.0f   },  // was 28 — denser
                { "impact_ring",    0.035f  },
                { "regularity",     0.35f   },  // more coherent rhythm
                { "tone_level",     0.0f    },
                { "filter_mode",    2.0f    },  // BP
                { "cutoff",         3200.0f },
                { "resonance",      0.35f   },
                { "env_cutoff",     0.8f    },
                { "attack",         0.01f   },
                { "decay",          0.35f   },
                { "sustain",        0.0f    },
                { "release",        0.06f   },
            }},
            { "Guiro", {
                // Evenly-spaced scrapes — the regularity=1 end of the
                // spectrum. Each "scrape" is a sharp pink-noise burst
                // through a narrow bandpass at scraping-texture rate.
                { "noise_type",     1.0f    },  // Pink
                { "noise_level",    1.0f    },
                { "impact_rate",    45.0f   },  // regular rasp ticks
                { "impact_ring",    0.015f  },
                { "regularity",     1.0f    },  // fully periodic
                { "tone_level",     0.0f    },
                { "filter_mode",    2.0f    },  // BP
                { "cutoff",         2200.0f },
                { "resonance",      0.55f   },  // resonant for "scrape" quality
                { "env_cutoff",     0.4f    },
                { "attack",         0.005f  },
                { "decay",          0.25f   },
                { "sustain",        0.0f    },
                { "release",        0.04f   },
            }},
        };

        return d;
    }

    void activate(float sample_rate, int max_block_size) override {
        vm_.init(sample_rate, max_block_size);
    }

    void deactivate() override {
        for (auto& v : vm_.voices) v = {};
    }

    void note_on(int channel, int pitch, int velocity) override {
        if (velocity == 0) { note_off(channel, pitch); return; }
        vm_.trigger(channel, pitch, velocity,
                    attack_, decay_, sustain_, release_);
        // Voice fields default to zero, so next_impact_in_samples starts
        // at 0 — the first process() sample fires the opening grain,
        // regardless of regularity mode.
    }

    void note_off(int channel, int pitch) override {
        vm_.release_note(channel, pitch);
    }

    void all_notes_off(int channel) override {
        vm_.all_notes_off(channel);
    }

    void note_tune(int channel, int note, float semitones) override {
        vm_.tune(channel, note, semitones);
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* out = buffers.audio.get("audio_out");
        if (!out) return;

        float* L = out->left;
        float* R = out->right ? out->right : out->left;
        const int N = ctx.block_size;

        std::memset(L, 0, N * sizeof(float));
        if (out->right) std::memset(R, 0, N * sizeof(float));

        auto ctrl = [&](const char* id, float fb) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fb;
        };

        float gain          = ctrl("gain", 0.8f);
        NoiseColor noise_t  = static_cast<NoiseColor>(
            std::clamp(static_cast<int>(ctrl("noise_type", 0.0f)), 0, 2));
        float noise_level   = ctrl("noise_level", 1.0f);
        float impact_rate_hz = ctrl("impact_rate", 20000.0f);
        float impact_ring_s  = std::max(0.0005f, ctrl("impact_ring", 0.05f));
        float regularity     = std::clamp(ctrl("regularity", 0.0f), 0.0f, 1.0f);
        float tone_level    = ctrl("tone_level", 0.0f);
        ToneShape tshape    = static_cast<ToneShape>(
            std::clamp(static_cast<int>(ctrl("tone_shape", 0.0f)), 0, 1));
        float tone_trans    = ctrl("tone_transpose", 0.0f);
        FilterMode fmode    = static_cast<FilterMode>(
            std::clamp(static_cast<int>(ctrl("filter_mode", 2.0f)), 0, 2));
        float cutoff        = ctrl("cutoff", 1000.0f);
        float resonance     = ctrl("resonance", 0.4f);
        float env_cutoff    = ctrl("env_cutoff", 3.0f);

        attack_  = std::max(0.0005f, ctrl("attack",  0.001f));
        decay_   = std::max(0.001f,  ctrl("decay",   0.15f));
        sustain_ = ctrl("sustain", 0.0f);
        release_ = std::max(0.001f, ctrl("release", 0.05f));

        vm_.begin_block(N);

        const float sr = vm_.sample_rate;
        const float Q  = 0.5f + resonance * 19.5f;
        const float k  = 1.0f / Q;

        const bool granular = impact_rate_hz < IMPACT_RATE_CONTINUOUS;
        // Mean wait between impacts, in samples.
        const float impact_period = sr / std::max(0.1f, impact_rate_hz);
        // Convert impact_ring (seconds) to per-sample decay factor via
        // exp(-1/(tau*sr)). A 50ms ring time → per-sample factor ~0.9998,
        // giving the impact an audible tail rather than a sub-millisecond
        // click that dies before the filter can resonate.
        const float impact_decay = std::exp(-1.0f / (impact_ring_s * sr));

        for (auto& v : vm_.voices) {
            if (!v.active) continue;

            for (int i = 0; i < N; ++i) {
                float env_val = v.env.next();
                if (v.env.is_off()) { v.active = false; break; }

                // Per-sample filter coefficient update — the envelope
                // sweeps the cutoff, which is the main thing that makes
                // a short noise burst sound drum-like.
                float env_mod    = env_cutoff * env_val;
                float eff_cutoff = cutoff * std::pow(2.0f, env_mod);
                eff_cutoff = std::clamp(eff_cutoff, 20.0f, sr * 0.49f);
                float g  = std::tan(static_cast<float>(M_PI) * eff_cutoff / sr);
                float a1 = 1.0f / (1.0f + g * (g + k));
                float a2 = g * a1;
                float a3 = g * a2;

                // Noise, optionally gated by PhISEM-style impact process.
                float noise = gen_noise(v, noise_t);
                if (granular) {
                    v.ext.snd_level *= impact_decay;
                    v.ext.next_impact_in_samples -= 1.0f;
                    if (v.ext.next_impact_in_samples <= 0.0f) {
                        v.ext.snd_level = 1.0f;
                        // Wait until the next impact = blend of a
                        // deterministic period (regularity=1) and an
                        // exponential random wait with the same mean
                        // (regularity=0 → Poisson process). Same mean
                        // either way, so changing regularity doesn't
                        // change the long-run impact rate.
                        float u = rng_.next_uniform();
                        if (u < 1e-6f) u = 1e-6f;
                        float exp_wait = -std::log(u) * impact_period;
                        v.ext.next_impact_in_samples +=
                            regularity * impact_period
                            + (1.0f - regularity) * exp_wait;
                    }
                    noise *= v.ext.snd_level;
                }
                float src = noise * noise_level;

                if (tone_level > 0.0f) {
                    float p = VoiceManager<PercussionExt>::interpolated_pitch(v, i, N);
                    p += tone_trans;
                    float f0 = pitch_to_freq(p);
                    double dt = static_cast<double>(f0) / sr;
                    float tone_val;
                    if (tshape == ToneShape::Sine) {
                        tone_val = static_cast<float>(
                            std::sin(2.0 * M_PI * v.phase));
                    } else {
                        float ph = static_cast<float>(v.phase);
                        tone_val = 4.0f * std::fabs(ph - 0.5f) - 1.0f;
                    }
                    v.phase += dt;
                    v.phase -= std::floor(v.phase);
                    src += tone_val * tone_level;
                }

                // SVF filter (Cytomic/Simper topology).
                float v3 = src - v.ext.svf_ic2eq;
                float v1 = a1 * v.ext.svf_ic1eq + a2 * v3;
                float v2 = v.ext.svf_ic2eq + a2 * v.ext.svf_ic1eq + a3 * v3;
                v.ext.svf_ic1eq = 2.0f * v1 - v.ext.svf_ic1eq;
                v.ext.svf_ic2eq = 2.0f * v2 - v.ext.svf_ic2eq;

                float filtered;
                switch (fmode) {
                    case FilterMode::LP: filtered = v2; break;
                    case FilterMode::HP: filtered = src - k * v1 - v2; break;
                    case FilterMode::BP: filtered = v1; break;
                    default: filtered = v2; break;
                }

                float s = filtered * env_val * v.velocity * gain;
                L[i] += s;
                R[i] += s;
            }
        }

        // Soft clip.
        for (int i = 0; i < N; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    float gen_noise(SynthVoice<PercussionExt>& v, NoiseColor color) {
        float white = rng_.next();
        switch (color) {
            case NoiseColor::White:
                return white;
            case NoiseColor::Pink: {
                auto& e = v.ext;
                e.pink_b0 = 0.99886f * e.pink_b0 + white * 0.0555179f;
                e.pink_b1 = 0.99332f * e.pink_b1 + white * 0.0750759f;
                e.pink_b2 = 0.96900f * e.pink_b2 + white * 0.1538520f;
                e.pink_b3 = 0.86650f * e.pink_b3 + white * 0.3104856f;
                e.pink_b4 = 0.55000f * e.pink_b4 + white * 0.5329522f;
                e.pink_b5 = -0.7616f * e.pink_b5 - white * 0.0168980f;
                float pink = e.pink_b0 + e.pink_b1 + e.pink_b2 + e.pink_b3
                           + e.pink_b4 + e.pink_b5 + e.pink_b6
                           + white * 0.5362f;
                e.pink_b6 = white * 0.115926f;
                return pink * 0.11f;
            }
            case NoiseColor::Brown: {
                auto& e = v.ext;
                e.brown = 0.997f * e.brown + 0.05f * white;
                return std::clamp(e.brown * 3.5f, -1.0f, 1.0f);
            }
        }
        return 0.0f;
    }

    VoiceManager<PercussionExt> vm_;
    XorShift rng_;

    float attack_  = 0.001f;
    float decay_   = 0.15f;
    float sustain_ = 0.0f;
    float release_ = 0.05f;
};

REGISTER_PLUGIN(PercussionSynthPlugin);
REGISTER_PLUGIN_DYNAMIC(PercussionSynthPlugin);
