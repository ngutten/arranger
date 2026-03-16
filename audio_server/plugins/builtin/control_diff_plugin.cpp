// control_diff_plugin.cpp
// Smoothed differentiator for control signals via derivative-of-Gaussian kernel.
//
// Convolves the block-rate control signal with a DoG kernel:
//   h[k] = -k · exp(-k²/(2σ²))
// normalized so Σ|h[k]| = 1. Positive output = rising signal.
//
// The width parameter sets the Gaussian sigma in seconds, controlling the
// frequency band of the differentiation. Narrow widths track fast transients,
// wide widths extract slow trends.
//
// Useful for:
//   - Detecting note onsets from envelope followers
//   - Extracting rate of change from pitch or dynamics curves
//   - Creating attack/release triggers from smooth modulation
//
// Parameters:
//   width — Gaussian sigma in seconds [0.01, 2.0], default 0.1

#include "plugin_api.h"
#include <cmath>
#include <vector>
#include <algorithm>

class ControlDiffPlugin final : public Plugin {
public:
    static constexpr int MAX_KERNEL = 512;

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_diff";
        d.display_name = "Control Diff";
        d.category     = "Control";
        d.doc          = "Smoothed differentiator for control signals using a "
                         "derivative-of-Gaussian kernel. Positive output indicates "
                         "a rising input signal. Width controls smoothing bandwidth.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "input", "Control In", "Control signal to differentiate",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -1e6f, 1e6f },

            { "output", "Control Out", "Derivative of input signal",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Continuous, 0.0f, -1e6f, 1e6f },

            { "width", "Width", "Gaussian sigma in seconds (smoothing bandwidth)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.01f, 2.0f },
        };

        return d;
    }

    void activate(float sample_rate, int max_block_size) override {
        sample_rate_    = sample_rate;
        max_block_size_ = max_block_size;
        history_.assign(MAX_KERNEL, 0.0f);
        write_pos_ = 0;
        last_width_ = -1.0f;  // Force kernel rebuild on first process
        ps_smooth_ = 0.0f;
        ps_alpha_ = 0.0f;
        last_ps_width_ = -1.0f;
    }

    void deactivate() override {
        history_.clear();
        kernel_.clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.control.get("input");
        auto* out = buffers.control.get("output");
        if (!in || !out) return;

        float width = std::clamp(param(buffers, "width", 0.1f), 0.01f, 2.0f);

        bool ps_in = in->samples != nullptr;

        // Per-sample path: IIR smoothed differentiator
        // One-pole lowpass + finite difference — O(1) per sample
        if (out->samples && out->frames > 0 && ps_in) {
            if (width != last_ps_width_) {
                float dt = 1.0f / sample_rate_;
                ps_alpha_ = 1.0f - std::exp(-dt / width);
                last_ps_width_ = width;
            }

            float alpha = ps_alpha_;
            float one_minus_alpha = 1.0f - alpha;
            float inv_alpha = 1.0f / alpha;
            float smooth = ps_smooth_;

            for (int i = 0; i < out->frames; ++i) {
                float s = alpha * in->samples[i] + one_minus_alpha * smooth;
                out->samples[i] = (s - smooth) * inv_alpha;
                smooth = s;
            }

            ps_smooth_ = smooth;
            out->value = out->samples[0];
            out->samples_written = true;
            return;
        }

        // Block-rate fallback
        if (width != last_width_ || block_size_ != ctx.block_size) {
            block_size_ = ctx.block_size;
            rebuild_kernel(width);
            last_width_ = width;
        }

        // Write current input into circular buffer
        history_[write_pos_] = in->value;

        // Convolve: output = Σ h[k] · x[n-k]
        float sum = 0.0f;
        int len = static_cast<int>(kernel_.size());
        for (int k = 0; k < len; ++k) {
            int idx = (write_pos_ - k + MAX_KERNEL) % MAX_KERNEL;
            sum += kernel_[k] * history_[idx];
        }

        out->value = sum;
        write_pos_ = (write_pos_ + 1) % MAX_KERNEL;
    }

private:
    float sample_rate_    = 44100.0f;
    int   max_block_size_ = 512;
    int   block_size_     = 512;
    float last_width_     = -1.0f;
    float last_ps_width_  = -1.0f;

    // Block-rate FIR state
    std::vector<float> history_;
    std::vector<float> kernel_;
    int write_pos_ = 0;

    // Per-sample IIR state
    float ps_smooth_ = 0.0f;
    float ps_alpha_  = 0.0f;

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    void rebuild_kernel(float width_sec) {
        // σ in blocks
        float sigma = width_sec * sample_rate_ / block_size_;
        if (sigma < 0.5f) sigma = 0.5f;

        float center = 2.0f * sigma;
        int len = std::min(static_cast<int>(std::ceil(4.0f * sigma)) + 1, MAX_KERNEL);

        kernel_.resize(len);

        float two_sigma_sq = 2.0f * sigma * sigma;
        float abs_sum = 0.0f;
        for (int k = 0; k < len; ++k) {
            float d = static_cast<float>(k) - center;
            kernel_[k] = -d * std::exp(-(d * d) / two_sigma_sq);
            abs_sum += std::abs(kernel_[k]);
        }

        if (abs_sum > 0.0f) {
            for (int k = 0; k < len; ++k) {
                kernel_[k] /= abs_sum;
            }
        }
    }

};

REGISTER_PLUGIN(ControlDiffPlugin);
REGISTER_PLUGIN_DYNAMIC(ControlDiffPlugin);
