// additive_synth_plugin.cpp
// Additive (harmonic bank) synthesizer with pitch-delta-responsive timbre.
//
// Synthesis: Sum of N sinusoidal partials at harmonic ratios (1x, 2x, 3x, ...).
// Spectral tilt (brightness) is modulated by pitch, velocity, and pitch delta
// via a tanh mapping, enabling transitional timbres during pitch slides.

#include "plugin_api.h"
#include "synth_common.h"

#include <cmath>
#include <cstring>

static constexpr int MAX_PARTIALS = 24;

// ---------------------------------------------------------------------------
// Voice extension
// ---------------------------------------------------------------------------

struct AdditiveExt {
    double partial_phase[MAX_PARTIALS] = {};
};

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class AdditiveSynthPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.additive_synth";
        d.display_name = "Additive Synth";
        d.category     = "Synth";
        d.doc          = "Harmonic additive synthesizer. Sums sinusoidal partials "
                         "with brightness modulated by pitch, velocity, and the rate "
                         "of pitch change (pitch delta).";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output.",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain", "Output level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 2.0f },
            { "num_partials", "Partials", "Number of harmonic partials.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 12.0f, 1.0f, 24.0f, 1.0f },
            { "brightness", "Brightness", "Spectral tilt baseline. Higher = more upper harmonics.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },
            { "brightness_range", "Bright Range", "Max brightness modulation range.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "k_bright_pitch", "k Bright Pitch", "Brightness sensitivity to pitch.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "k_bright_vel", "k Bright Vel", "Brightness sensitivity to velocity.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "k_bright_delta", "k Bright Delta", "Brightness sensitivity to pitch delta.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "attack", "Attack", "ADSR attack (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.01f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "decay", "Decay", "ADSR decay (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "sustain", "Sustain", "ADSR sustain level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.8f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "release", "Release", "ADSR release (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.2f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "delta_smooth", "Delta Smooth", "Pitch-delta EMA time constant (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.2f, 0.001f, 0.5f, 0.0f, {}, "", false },
            { "delta_overshoot", "Delta Overshoot", "Pitch overshoot from pitch delta (semitones per unit delta).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 2.0f, 0.0f, {}, "", false },
            { "delta_tremolo", "Delta Tremolo", "Pitch oscillation amplitude from pitch delta.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 2.0f, 0.0f, {}, "", false },
            { "voicing", "Voicing", "Master expressiveness scale.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
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
        vm_.trigger(channel, pitch, velocity, attack_, decay_, sustain_, release_);
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

        // Read control ports
        auto ctrl = [&](const char* id, float fb) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fb;
        };

        float gain        = ctrl("gain", 0.5f);
        int   n_partials  = std::clamp(static_cast<int>(ctrl("num_partials", 12.0f)), 1, MAX_PARTIALS);
        float brightness  = ctrl("brightness", 0.5f);
        float bright_range = ctrl("brightness_range", 0.5f);
        float voicing     = ctrl("voicing", 1.0f);

        attack_  = std::max(0.001f, ctrl("attack",  0.01f));
        decay_   = std::max(0.001f, ctrl("decay",   0.1f));
        sustain_ = ctrl("sustain", 0.8f);
        release_ = std::max(0.001f, ctrl("release", 0.2f));

        vm_.delta_smooth = ctrl("delta_smooth", 0.2f);
        float overshoot  = ctrl("delta_overshoot", 0.0f);
        float tremolo    = ctrl("delta_tremolo", 0.0f);

        TanhMapping bright_map;
        bright_map.k_pitch = ctrl("k_bright_pitch", 0.0f);
        bright_map.k_vel   = ctrl("k_bright_vel",   0.5f);
        bright_map.k_delta = ctrl("k_bright_delta", 0.3f);

        vm_.begin_block(N);

        float nyquist = vm_.sample_rate * 0.5f;

        for (auto& v : vm_.voices) {
            if (!v.active) continue;

            // Effective brightness via tanh mapping (once per block)
            float eff_bright = bright_map.compute(v, brightness, bright_range, voicing);
            eff_bright = std::clamp(eff_bright, 0.0f, 1.0f);

            // Spectral tilt exponent: low brightness = steep rolloff
            float tilt_exp = 1.0f + (1.0f - eff_bright) * 3.0f;

            for (int i = 0; i < N; ++i) {
                float env_val = v.env.next();
                if (v.env.is_off()) { v.active = false; break; }

                float p = VoiceManager<AdditiveExt>::pitch_with_dynamics(v, i, N, vm_.sample_rate, overshoot, tremolo, vm_.delta_smooth);
                float f0 = pitch_to_freq(p);

                float sample = 0.0f;
                for (int k = 0; k < n_partials; ++k) {
                    float partial_freq = f0 * (k + 1);
                    if (partial_freq > nyquist) break;

                    // Amplitude: 1/k^tilt
                    float amp_k = 1.0f / std::pow(static_cast<float>(k + 1), tilt_exp);

                    // Phase accumulator
                    double phase_inc = static_cast<double>(partial_freq) / vm_.sample_rate;
                    v.ext.partial_phase[k] += phase_inc;
                    v.ext.partial_phase[k] -= std::floor(v.ext.partial_phase[k]);

                    sample += amp_k * static_cast<float>(
                        std::sin(2.0 * M_PI * v.ext.partial_phase[k]));
                }

                float out_sample = sample * env_val * v.velocity * gain;
                L[i] += out_sample;
                R[i] += out_sample;
            }
        }

        // Soft clip
        for (int i = 0; i < N; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    VoiceManager<AdditiveExt> vm_;

    // Cached ADSR params (updated from control ports each block)
    float attack_  = 0.01f;
    float decay_   = 0.1f;
    float sustain_ = 0.8f;
    float release_ = 0.2f;
};

REGISTER_PLUGIN(AdditiveSynthPlugin);
REGISTER_PLUGIN_DYNAMIC(AdditiveSynthPlugin);
