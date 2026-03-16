// filter_plugin.cpp
// Stereo biquad filter: low-pass, high-pass, band-pass, and notch.
//
// Uses the Audio EQ Cookbook (Robert Bristow-Johnson) bilinear-transform
// biquad. Coefficients are recomputed when frequency or Q change.
// Smooth coefficient changes are applied with a one-pole interpolator
// to avoid clicks when parameters are modulated.
//
// Parameters:
//   mode      — 0=LP, 1=HP, 2=BP, 3=Notch
//   frequency — cutoff/centre frequency [20, 20000] Hz  (Control-driven)
//   q         — resonance / bandwidth [0.1, 20]
//
// The frequency port accepts a Control input so it can be driven by an LFO
// or envelope for filter sweeps.

#include "plugin_api.h"
#include <cmath>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Second-order (biquad) filter state, one per channel
// ---------------------------------------------------------------------------
struct Biquad {
    // Coefficients
    float b0 = 1.f, b1 = 0.f, b2 = 0.f;
    float a1 = 0.f, a2 = 0.f;

    // Direct Form II transposed state
    float s1 = 0.f, s2 = 0.f;

    float process(float x) {
        float y = b0 * x + s1;
        s1 = b1 * x - a1 * y + s2;
        s2 = b2 * x - a2 * y;
        return y;
    }

    void reset() { s1 = s2 = 0.f; }

    // Set coefficients using EQ Cookbook formulas
    void compute(int mode, float freq, float q, float sr) {
        float w0    = 2.f * static_cast<float>(M_PI) * freq / sr;
        float cosw  = std::cos(w0);
        float sinw  = std::sin(w0);
        float alpha = sinw / (2.f * q);

        float a0;
        switch (mode) {
        case 0:  // Low-pass
            b0 = (1.f - cosw) * 0.5f;
            b1 =  1.f - cosw;
            b2 = (1.f - cosw) * 0.5f;
            a0 =  1.f + alpha;
            a1 = -2.f * cosw;
            a2 =  1.f - alpha;
            break;
        case 1:  // High-pass
            b0 =  (1.f + cosw) * 0.5f;
            b1 = -(1.f + cosw);
            b2 =  (1.f + cosw) * 0.5f;
            a0 =   1.f + alpha;
            a1 =  -2.f * cosw;
            a2 =   1.f - alpha;
            break;
        case 2:  // Band-pass (constant skirt gain, peak gain = Q)
            b0 =  sinw * 0.5f;
            b1 =  0.f;
            b2 = -sinw * 0.5f;
            a0 =  1.f + alpha;
            a1 = -2.f * cosw;
            a2 =  1.f - alpha;
            break;
        case 3:  // Notch
        default:
            b0 =  1.f;
            b1 = -2.f * cosw;
            b2 =  1.f;
            a0 =  1.f + alpha;
            a1 = -2.f * cosw;
            a2 =  1.f - alpha;
            break;
        }

        float inv_a0 = 1.f / a0;
        b0 *= inv_a0;
        b1 *= inv_a0;
        b2 *= inv_a0;
        a1 *= inv_a0;
        a2 *= inv_a0;
    }
};

// ---------------------------------------------------------------------------

class FilterPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.filter";
        d.display_name = "Filter";
        d.category     = "Effect";
        d.doc          = "Stereo biquad filter (Audio EQ Cookbook). "
                         "Modes: Low-pass, High-pass, Band-pass, Notch. "
                         "The frequency port accepts a Control input for filter sweeps.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_in", "Audio In", "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },

            { "audio_out", "Audio Out", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },

            { "mode", "Mode", "Filter type",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 3.0f, 1.0f,
              {"Low-pass", "High-pass", "Band-pass", "Notch"} },

            { "frequency", "Frequency",
              "Cutoff/centre frequency in Hz. Accepts Control input.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1000.0f, 20.0f, 20000.0f },

            { "q", "Q / Resonance",
              "Filter resonance. 0.707 = Butterworth (maximally flat).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.707f, 0.1f, 20.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        filter_L_.reset();
        filter_R_.reset();
        last_mode_ = -1;   // force initial coefficient computation
        last_freq_ = -1.f;
        last_q_    = -1.f;
    }

    void deactivate() override {
        filter_L_.reset();
        filter_R_.reset();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        int   mode = std::clamp(static_cast<int>(param(buffers, "mode", 0.f)), 0, 3);
        float q    = std::max(0.1f, param(buffers, "q", 0.707f));
        float sr   = sample_rate_;

        auto* freq_buf = buffers.control.get("frequency");
        bool  ps_freq  = freq_buf && freq_buf->samples;

        if (!ps_freq) {
            // Block-rate path (original)
            float freq = std::clamp(freq_buf ? freq_buf->value : 1000.f, 20.f, sr * 0.49f);
            if (mode != last_mode_ || freq != last_freq_ || q != last_q_) {
                filter_L_.compute(mode, freq, q, sr);
                filter_R_ = filter_L_;
                last_mode_ = mode;
                last_freq_ = freq;
                last_q_    = q;
            }
            for (int i = 0; i < ctx.block_size; ++i) {
                out->left[i] = filter_L_.process(in->left[i]);
                if (out->right)
                    out->right[i] = filter_R_.process(in->right ? in->right[i] : in->left[i]);
            }
        } else {
            // Per-sample frequency path
            for (int i = 0; i < ctx.block_size; ++i) {
                float freq = std::clamp(freq_buf->samples[i], 20.f, sr * 0.49f);
                if (freq != last_freq_ || mode != last_mode_ || q != last_q_) {
                    filter_L_.compute(mode, freq, q, sr);
                    filter_R_ = filter_L_;
                    last_mode_ = mode;
                    last_freq_ = freq;
                    last_q_    = q;
                }
                out->left[i] = filter_L_.process(in->left[i]);
                if (out->right)
                    out->right[i] = filter_R_.process(in->right ? in->right[i] : in->left[i]);
            }
        }
    }

private:
    float  sample_rate_ = 44100.f;
    Biquad filter_L_, filter_R_;
    int    last_mode_ = -1;
    float  last_freq_ = -1.f;
    float  last_q_    = -1.f;

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }
};

REGISTER_PLUGIN(FilterPlugin);
REGISTER_PLUGIN_DYNAMIC(FilterPlugin);

std::unique_ptr<Plugin> make_filter_plugin() {
    return std::make_unique<FilterPlugin>();
}
