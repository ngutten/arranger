// fm_synth_plugin.cpp
// 2-operator FM synthesizer with pitch-delta-responsive timbre.
//
// Architecture: Modulator -> Carrier with configurable frequency ratios.
// Modulator has self-feedback (one-sample delay).
// Modulation index and feedback are modulated by pitch, velocity, and
// pitch delta via tanh mapping.

#include "plugin_api.h"
#include "synth_common.h"

#include <cmath>
#include <cstring>

// ---------------------------------------------------------------------------
// Voice extension
// ---------------------------------------------------------------------------

struct FMExt {
    double mod_phase    = 0.0;
    float  prev_mod_out = 0.0f;
};

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class FMSynthPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.fm_synth";
        d.display_name = "FM Synth";
        d.category     = "Synth";
        d.doc          = "Two-operator FM synthesizer. Modulation index and feedback "
                         "are modulated by pitch, velocity, and pitch delta for "
                         "expressive timbral control during pitch slides.";
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
            { "mod_ratio", "Mod Ratio", "Modulator freq = fundamental * ratio.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 2.0f, 0.5f, 16.0f },
            { "mod_index", "Mod Index", "FM modulation depth baseline.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 10.0f },
            { "index_range", "Index Range", "Max modulation index modulation.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 3.0f, 0.0f, 10.0f, 0.0f, {}, "", false },
            { "feedback", "Feedback", "Modulator self-feedback baseline.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "fb_range", "FB Range", "Max feedback modulation.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "k_index_pitch", "k Index Pitch", "Index sensitivity to pitch.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "k_index_vel", "k Index Vel", "Index sensitivity to velocity.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "k_index_delta", "k Index Delta", "Index sensitivity to pitch delta.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.4f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "k_fb_delta", "k FB Delta", "Feedback sensitivity to pitch delta.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.2f, -2.0f, 2.0f, 0.0f, {}, "", false },
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
              ControlHint::Continuous, 0.0f, -2.0f, 2.0f, 0.0f, {}, "", false },
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

        auto ctrl = [&](const char* id, float fb) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fb;
        };

        float gain          = ctrl("gain", 0.5f);
        float mod_ratio     = ctrl("mod_ratio", 2.0f);
        float mod_index     = ctrl("mod_index", 1.0f);
        float index_range   = ctrl("index_range", 3.0f);
        float feedback      = ctrl("feedback", 0.0f);
        float fb_range      = ctrl("fb_range", 0.3f);
        float voicing       = ctrl("voicing", 1.0f);

        attack_  = std::max(0.001f, ctrl("attack",  0.01f));
        decay_   = std::max(0.001f, ctrl("decay",   0.1f));
        sustain_ = ctrl("sustain", 0.8f);
        release_ = std::max(0.001f, ctrl("release", 0.2f));

        vm_.delta_smooth = ctrl("delta_smooth", 0.2f);
        float overshoot  = ctrl("delta_overshoot", 0.0f);
        float tremolo    = ctrl("delta_tremolo", 0.0f);

        // Index modulation mapping
        TanhMapping index_map;
        index_map.k_pitch = ctrl("k_index_pitch", 0.3f);
        index_map.k_vel   = ctrl("k_index_vel",   0.5f);
        index_map.k_delta = ctrl("k_index_delta", 0.4f);

        // Feedback modulation mapping (only delta-sensitive by default)
        TanhMapping fb_map;
        fb_map.k_pitch = 0.0f;
        fb_map.k_vel   = 0.0f;
        fb_map.k_delta = ctrl("k_fb_delta", 0.2f);

        vm_.begin_block(N);

        double inv_sr = 1.0 / vm_.sample_rate;

        for (auto& v : vm_.voices) {
            if (!v.active) continue;

            // Effective parameters via tanh mapping (once per block)
            float eff_index = index_map.compute(v, mod_index, index_range, voicing);
            eff_index = std::max(0.0f, eff_index);

            float eff_fb = fb_map.compute(v, feedback, fb_range, voicing);
            eff_fb = std::clamp(eff_fb, 0.0f, 1.0f);

            for (int i = 0; i < N; ++i) {
                float env_val = v.env.next();
                if (v.env.is_off()) { v.active = false; break; }

                float p = VoiceManager<FMExt>::pitch_with_dynamics(v, i, N, vm_.sample_rate, overshoot, tremolo, vm_.delta_smooth);
                float f0 = pitch_to_freq(p);

                // Modulator
                double mod_freq = f0 * mod_ratio;
                double mod_phase_inc = mod_freq * inv_sr;

                float mod_out = static_cast<float>(std::sin(
                    2.0 * M_PI * v.ext.mod_phase + eff_fb * v.ext.prev_mod_out));
                v.ext.prev_mod_out = mod_out;

                v.ext.mod_phase += mod_phase_inc;
                v.ext.mod_phase -= std::floor(v.ext.mod_phase);

                // Carrier (always tracks note pitch to stay in tune)
                double carrier_phase_inc = static_cast<double>(f0) * inv_sr;

                float carrier_out = static_cast<float>(std::sin(
                    2.0 * M_PI * v.phase + eff_index * mod_out));

                v.phase += carrier_phase_inc;
                v.phase -= std::floor(v.phase);

                float out_sample = carrier_out * env_val * v.velocity * gain;
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
    VoiceManager<FMExt> vm_;

    float attack_  = 0.01f;
    float decay_   = 0.1f;
    float sustain_ = 0.8f;
    float release_ = 0.2f;
};

REGISTER_PLUGIN(FMSynthPlugin);
REGISTER_PLUGIN_DYNAMIC(FMSynthPlugin);
