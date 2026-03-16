// subtractive_synth_plugin.cpp
// Subtractive synthesizer with pitch-delta-responsive filter.
//
// Two oscillators (selectable waveform) mixed together, through a state-variable
// filter (Cytomic/Simper SVF). Anti-aliased via PolyBLEP for saw and square.
// Filter cutoff modulated by pitch, velocity, and pitch delta.

#include "plugin_api.h"
#include "synth_common.h"

#include <cmath>
#include <cstring>

// ---------------------------------------------------------------------------
// Waveform types
// ---------------------------------------------------------------------------

enum class OscShape { Saw = 0, Square = 1, Sine = 2, Noise = 3 };

// ---------------------------------------------------------------------------
// PolyBLEP antialiasing residual
// ---------------------------------------------------------------------------

static inline float polyblep(double t, double dt) {
    // t in [0,1), dt = phase increment per sample
    if (t < dt) {
        float tn = static_cast<float>(t / dt);
        return tn + tn - tn * tn - 1.0f;
    }
    if (t > 1.0 - dt) {
        float tn = static_cast<float>((t - 1.0) / dt);
        return tn * tn + tn + tn + 1.0f;
    }
    return 0.0f;
}

// ---------------------------------------------------------------------------
// Simple xorshift RNG for noise oscillator
// ---------------------------------------------------------------------------

struct XorShift {
    uint32_t state = 123456789u;
    inline float next() {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        return static_cast<float>(static_cast<int32_t>(state)) / 2147483648.0f;
    }
};

// ---------------------------------------------------------------------------
// Voice extension
// ---------------------------------------------------------------------------

struct SubtractiveExt {
    double osc2_phase   = 0.0;
    // SVF state (Cytomic/Simper form)
    float  svf_ic1eq    = 0.0f;
    float  svf_ic2eq    = 0.0f;
};

// ---------------------------------------------------------------------------
// Filter mode
// ---------------------------------------------------------------------------

enum class FilterMode { LP = 0, HP = 1, BP = 2 };

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class SubtractiveSynthPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.subtractive_synth";
        d.display_name = "Subtractive Synth";
        d.category     = "Synth";
        d.doc          = "Two-oscillator subtractive synthesizer with state-variable "
                         "filter. Filter cutoff is modulated by pitch, velocity, and "
                         "pitch delta for expressive timbral response during slides.";
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
            { "osc1_shape", "Osc 1 Shape", "Oscillator 1 waveform.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 3.0f, 1.0f,
              { "Saw", "Square", "Sine", "Noise" } },
            { "osc2_shape", "Osc 2 Shape", "Oscillator 2 waveform.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 1.0f, 0.0f, 3.0f, 1.0f,
              { "Saw", "Square", "Sine", "Noise" } },
            { "osc_mix", "Osc Mix", "0 = osc1 only, 1 = osc2 only.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },
            { "osc2_detune", "Osc 2 Detune", "Osc 2 detune in semitones.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -24.0f, 24.0f },
            { "filter_mode", "Filter Mode", "Filter type.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 2.0f, 1.0f,
              { "LP", "HP", "BP" } },
            { "cutoff", "Cutoff", "Filter cutoff Hz (baseline).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 2000.0f, 20.0f, 20000.0f },
            { "resonance", "Resonance", "Filter resonance.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, 0.0f, 1.0f },
            { "cutoff_range", "Cutoff Range", "Max cutoff modulation in octaves.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 4.0f, 0.0f, 8.0f, 0.0f, {}, "", false },
            { "k_cutoff_pitch", "k Cut Pitch", "Cutoff sensitivity to pitch.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "k_cutoff_vel", "k Cut Vel", "Cutoff sensitivity to velocity.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, -2.0f, 2.0f, 0.0f, {}, "", false },
            { "k_cutoff_delta", "k Cut Delta", "Cutoff sensitivity to pitch delta.",
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

        float gain         = ctrl("gain", 0.5f);
        OscShape osc1_shape = static_cast<OscShape>(
            std::clamp(static_cast<int>(ctrl("osc1_shape", 0.0f)), 0, 3));
        OscShape osc2_shape = static_cast<OscShape>(
            std::clamp(static_cast<int>(ctrl("osc2_shape", 1.0f)), 0, 3));
        float osc_mix      = ctrl("osc_mix", 0.5f);
        float osc2_detune  = ctrl("osc2_detune", 0.0f);
        FilterMode fmode   = static_cast<FilterMode>(
            std::clamp(static_cast<int>(ctrl("filter_mode", 0.0f)), 0, 2));
        float cutoff       = ctrl("cutoff", 2000.0f);
        float resonance    = ctrl("resonance", 0.3f);
        float cutoff_range = ctrl("cutoff_range", 4.0f);
        float voicing      = ctrl("voicing", 1.0f);

        attack_  = std::max(0.001f, ctrl("attack",  0.01f));
        decay_   = std::max(0.001f, ctrl("decay",   0.1f));
        sustain_ = ctrl("sustain", 0.8f);
        release_ = std::max(0.001f, ctrl("release", 0.2f));

        // Per-sample ADSR detection
        auto* att_ctl = buffers.control.get("attack");
        auto* dec_ctl = buffers.control.get("decay");
        auto* sus_ctl = buffers.control.get("sustain");
        auto* rel_ctl = buffers.control.get("release");
        bool ps_adsr = (att_ctl && att_ctl->samples) || (dec_ctl && dec_ctl->samples)
                    || (sus_ctl && sus_ctl->samples) || (rel_ctl && rel_ctl->samples);

        vm_.delta_smooth = ctrl("delta_smooth", 0.2f);
        float overshoot  = ctrl("delta_overshoot", 0.0f);
        float tremolo    = ctrl("delta_tremolo", 0.0f);

        TanhMapping cutoff_map;
        cutoff_map.k_pitch = ctrl("k_cutoff_pitch", 0.5f);
        cutoff_map.k_vel   = ctrl("k_cutoff_vel",   0.3f);
        cutoff_map.k_delta = ctrl("k_cutoff_delta", 0.2f);

        vm_.begin_block(N);

        float sr = vm_.sample_rate;

        for (auto& v : vm_.voices) {
            if (!v.active) continue;

            // Effective cutoff in log-space via tanh mapping (once per block)
            float cutoff_mod = cutoff_map.compute(v, 0.0f, cutoff_range, voicing);
            float eff_cutoff = cutoff * std::pow(2.0f, cutoff_mod);
            eff_cutoff = std::clamp(eff_cutoff, 20.0f, sr * 0.49f);

            // SVF coefficients (Cytomic/Simper form)
            // Q from resonance: Q = 0.5 .. 20
            float Q = 0.5f + resonance * 19.5f;
            float g = std::tan(static_cast<float>(M_PI) * eff_cutoff / sr);
            float k = 1.0f / Q;
            float a1 = 1.0f / (1.0f + g * (g + k));
            float a2 = g * a1;
            float a3 = g * a2;

            float last_a = attack_, last_d = decay_, last_s = sustain_, last_r = release_;

            for (int i = 0; i < N; ++i) {
                // Per-sample ADSR update
                if (ps_adsr) {
                    float a = att_ctl && att_ctl->samples ? std::max(0.001f, att_ctl->samples[i]) : attack_;
                    float d = dec_ctl && dec_ctl->samples ? std::max(0.001f, dec_ctl->samples[i]) : decay_;
                    float s = sus_ctl && sus_ctl->samples ? sus_ctl->samples[i] : sustain_;
                    float r = rel_ctl && rel_ctl->samples ? std::max(0.001f, rel_ctl->samples[i]) : release_;
                    if (a != last_a || d != last_d || s != last_s || r != last_r) {
                        v.env.update(vm_.sample_rate, a, d, s, r);
                        last_a = a; last_d = d; last_s = s; last_r = r;
                    }
                }

                float env_val = v.env.next();
                if (v.env.is_off()) { v.active = false; break; }

                float p = VoiceManager<SubtractiveExt>::pitch_with_dynamics(v, i, N, sr, overshoot, tremolo, vm_.delta_smooth);
                float f0 = pitch_to_freq(p);
                double dt1 = static_cast<double>(f0) / sr;

                // Oscillator 1
                float s1 = generate_osc(v.phase, dt1, osc1_shape);

                // Oscillator 2
                float f2 = pitch_to_freq(p + osc2_detune);
                double dt2 = static_cast<double>(f2) / sr;
                float s2 = generate_osc(v.ext.osc2_phase, dt2, osc2_shape);

                // Mix
                float mix = s1 * (1.0f - osc_mix) + s2 * osc_mix;

                // SVF filter (Cytomic/Simper topology)
                float v3 = mix - v.ext.svf_ic2eq;
                float v1 = a1 * v.ext.svf_ic1eq + a2 * v3;
                float v2 = v.ext.svf_ic2eq + a2 * v.ext.svf_ic1eq + a3 * v3;
                v.ext.svf_ic1eq = 2.0f * v1 - v.ext.svf_ic1eq;
                v.ext.svf_ic2eq = 2.0f * v2 - v.ext.svf_ic2eq;

                float filtered;
                switch (fmode) {
                    case FilterMode::LP: filtered = v2; break;
                    case FilterMode::HP: filtered = mix - k * v1 - v2; break;
                    case FilterMode::BP: filtered = v1; break;
                    default: filtered = v2; break;
                }

                float out_sample = filtered * env_val * v.velocity * gain;
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
    // -----------------------------------------------------------------------
    // Oscillator generation with PolyBLEP
    // -----------------------------------------------------------------------

    float generate_osc(double& phase, double dt, OscShape shape) {
        float sample;
        switch (shape) {
            case OscShape::Saw: {
                sample = 2.0f * static_cast<float>(phase) - 1.0f;
                sample -= polyblep(phase, dt);
                break;
            }
            case OscShape::Square: {
                sample = (phase < 0.5) ? 1.0f : -1.0f;
                sample += polyblep(phase, dt);
                double phase2 = phase + 0.5;
                if (phase2 >= 1.0) phase2 -= 1.0;
                sample -= polyblep(phase2, dt);
                break;
            }
            case OscShape::Sine:
                sample = static_cast<float>(std::sin(2.0 * M_PI * phase));
                break;
            case OscShape::Noise:
                sample = rng_.next();
                break;
            default:
                sample = 0.0f;
                break;
        }

        phase += dt;
        phase -= std::floor(phase);
        return sample;
    }

    VoiceManager<SubtractiveExt> vm_;
    XorShift rng_;

    float attack_  = 0.01f;
    float decay_   = 0.1f;
    float sustain_ = 0.8f;
    float release_ = 0.2f;
};

REGISTER_PLUGIN(SubtractiveSynthPlugin);
REGISTER_PLUGIN_DYNAMIC(SubtractiveSynthPlugin);
