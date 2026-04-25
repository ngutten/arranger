// compressor_plugin.cpp
// Stereo feed-forward compressor with soft knee, log-domain gain
// computation, and a gain-reduction monitor.
//
// Detector: peak of |L+R|/2, smoothed by an attack/release follower.
// Gain: computed in dB with a quadratic soft knee. Makeup gain applied
// after reduction.
//
// Parameters:
//   threshold_db   — level at which compression begins [-60, 0] dB
//   ratio          — compression ratio [1, 20]
//   attack_ms      — time for the detector to rise [0.1, 200] ms
//   release_ms     — time for the detector to fall [1, 2000] ms
//   knee_db        — width of the soft knee [0, 24] dB
//   makeup_db      — output gain after reduction [0, 24] dB
//
// Monitor:
//   gain_reduction_db — current gain reduction in dB (positive = attenuation)

#include "plugin_api.h"
#include <algorithm>
#include <atomic>
#include <cmath>

class CompressorPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.compressor";
        d.display_name = "Compressor";
        d.category     = "Effect";
        d.doc          = "Feed-forward stereo compressor with soft knee, attack/release "
                         "smoothing, and a gain-reduction monitor output. Detector is the "
                         "peak of (L+R)/2; both channels receive the same gain reduction.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_in",  "Audio In",  "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Compressed stereo output",
              PluginPortType::AudioStereo, PortRole::Output },

            { "threshold_db", "Threshold (dB)",
              "Level above which compression begins.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, -12.0f, -60.0f, 0.0f },
            { "ratio", "Ratio",
              "Compression ratio. 1:1 = off, higher = more reduction.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 4.0f, 1.0f, 20.0f },
            { "attack_ms", "Attack (ms)",
              "Time constant for the detector to respond to level increases.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 10.0f, 0.1f, 200.0f },
            { "release_ms", "Release (ms)",
              "Time constant for the detector to decay.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 100.0f, 1.0f, 2000.0f },
            { "knee_db", "Knee (dB)",
              "Width of the soft knee around the threshold. 0 = hard knee.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 6.0f, 0.0f, 24.0f },
            { "makeup_db", "Makeup (dB)",
              "Output gain applied after reduction.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 24.0f },

            { "gain_reduction_db", "Gain Reduction",
              "Current gain reduction in dB (positive = attenuation).",
              PluginPortType::Control, PortRole::Monitor,
              ControlHint::Meter, 0.0f, 0.0f, 24.0f },
        };
        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        env_ = 0.0f;
        gr_db_.store(0.0f, std::memory_order_relaxed);
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        float thresh   = std::clamp(param(buffers, "threshold_db", -12.0f), -60.0f, 0.0f);
        float ratio    = std::clamp(param(buffers, "ratio",         4.0f),   1.0f, 20.0f);
        float att_ms   = std::clamp(param(buffers, "attack_ms",    10.0f),   0.1f, 200.0f);
        float rel_ms   = std::clamp(param(buffers, "release_ms", 100.0f),    1.0f, 2000.0f);
        float knee     = std::clamp(param(buffers, "knee_db",      6.0f),    0.0f, 24.0f);
        float makeup   = std::clamp(param(buffers, "makeup_db",    0.0f),    0.0f, 24.0f);

        // One-pole coefficients — e^{-1 / (time_sec * sr)}.
        float att_coef = std::exp(-1.0f / (att_ms * 0.001f * sample_rate_));
        float rel_coef = std::exp(-1.0f / (rel_ms * 0.001f * sample_rate_));

        float slope = 1.0f - 1.0f / ratio;
        float knee_half = knee * 0.5f;
        float makeup_lin = std::pow(10.0f, makeup / 20.0f);

        float env = env_;
        float peak_gr_db = 0.0f;

        for (int i = 0; i < ctx.block_size; ++i) {
            float l = in->left[i];
            float r = in->right ? in->right[i] : l;
            float det = 0.5f * std::fabs(l + r);

            // Envelope follower — attack when rising, release when falling.
            if (det > env)
                env = det + (env - det) * att_coef;
            else
                env = det + (env - det) * rel_coef;

            // dB domain gain computation.
            float env_db = 20.0f * std::log10(std::max(env, 1e-9f));
            float over = env_db - thresh;
            float gr_db;  // dB of attenuation, always >= 0
            if (knee > 0.0f && over > -knee_half && over < knee_half) {
                // Quadratic soft knee: smooth transition from 0 to full slope
                // over ±knee/2 around the threshold.
                float x = over + knee_half;  // [0, knee]
                gr_db = slope * x * x / (2.0f * knee);
            } else if (over > knee_half) {
                gr_db = slope * over;
            } else {
                gr_db = 0.0f;
            }

            if (gr_db > peak_gr_db) peak_gr_db = gr_db;

            float gain_lin = std::pow(10.0f, -gr_db / 20.0f) * makeup_lin;
            out->left[i] = l * gain_lin;
            if (out->right)
                out->right[i] = r * gain_lin;
        }

        env_ = env;
        gr_db_.store(peak_gr_db, std::memory_order_relaxed);

        // Write monitor output — both block-rate value and (if the
        // engine wired per-sample buffers) a constant-filled run.
        if (auto* mon = buffers.control.get("gain_reduction_db")) {
            mon->value = peak_gr_db;
            if (mon->samples) {
                for (int i = 0; i < ctx.block_size; ++i) mon->samples[i] = peak_gr_db;
                mon->samples_written = true;
            }
        }
    }

    float read_monitor(const std::string& port_id) override {
        if (port_id == "gain_reduction_db")
            return gr_db_.load(std::memory_order_relaxed);
        return 0.0f;
    }

private:
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    float sample_rate_ = 44100.0f;
    float env_ = 0.0f;
    std::atomic<float> gr_db_{0.0f};
};

REGISTER_PLUGIN(CompressorPlugin);
REGISTER_PLUGIN_DYNAMIC(CompressorPlugin);
