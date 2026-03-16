// control_delay_line_plugin.cpp
// Control signal delay line — delays control-rate signals up to 2 seconds.
//
// Unlike audio delay lines that process sample-rate buffers, this delays
// control-rate parameters (one value per block). Useful for:
//   - Creating rhythmic modulation patterns
//   - Time-offset automation lanes
//   - Feedback loops in modulation graphs
//   - Sample-and-hold effects with variable hold time
//
// The delay is specified in milliseconds and rounded to the nearest block boundary.
//
// Parameters:
//   delay_ms — Delay time in milliseconds [0, 2000]

#include "plugin_api.h"
#include <cmath>
#include <vector>
#include <algorithm>

class ControlDelayLinePlugin final : public Plugin {
public:
    static constexpr float MAX_DELAY_SEC = 2.0f;

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_delay_line";
        d.display_name = "Control Delay Line";
        d.category     = "Control";
        d.doc          = "Delays control-rate signals by a settable time up to 2 seconds. "
                         "Operates at block rate (one value per audio block), so delay time is "
                         "quantized to block boundaries. Useful for rhythmic modulation patterns, "
                         "time-offset automation, or modulation feedback loops.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "control_in",  "Control In",  "Control signal input",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -1.0f, 1.0f },
            
            { "control_out", "Control Out", "Delayed control signal output",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Continuous, 0.0f, -1.0f, 1.0f },
            
            { "delay_ms", "Delay (ms)", "Delay time in milliseconds (quantized to block size)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 100.0f, 0.0f, 2000.0f },
        };

        return d;
    }

    void activate(float sample_rate, int max_block_size) override {
        sample_rate_    = sample_rate;
        max_block_size_ = max_block_size;

        // Block-rate buffer
        int max_blocks = static_cast<int>(MAX_DELAY_SEC * sample_rate / max_block_size) + 2;
        delay_buf_.assign(max_blocks, 0.0f);
        write_pos_ = 0;

        // Per-sample buffer: max_delay_samples = max_delay_ms * sample_rate / 1000
        int max_delay_samples = static_cast<int>(MAX_DELAY_SEC * sample_rate) + 4;
        ps_delay_buf_.assign(max_delay_samples, 0.0f);
        ps_write_pos_ = 0;
    }

    void deactivate() override {
        delay_buf_.clear();
        ps_delay_buf_.clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.control.get("control_in");
        auto* out = buffers.control.get("control_out");
        if (!in || !out) return;

        auto* delay_ctl = buffers.control.get("delay_ms");
        bool ps_in    = in->samples != nullptr;
        bool ps_delay = delay_ctl && delay_ctl->samples;

        // Per-sample path
        if (out->samples && out->frames > 0) {
            int ps_buf_size = static_cast<int>(ps_delay_buf_.size());
            float const_delay_ms = std::clamp(
                delay_ctl ? delay_ctl->value : 100.0f, 0.0f, 2000.0f);

            for (int i = 0; i < out->frames; ++i) {
                float x = ps_in ? in->samples[i] : in->value;
                float dm = ps_delay ? delay_ctl->samples[i] : const_delay_ms;
                dm = std::clamp(dm, 0.0f, 2000.0f);

                float delay_samples = dm * sample_rate_ / 1000.0f;
                delay_samples = std::min(delay_samples, static_cast<float>(ps_buf_size - 2));

                // Read with linear interpolation
                float read_pos_f = ps_write_pos_ - delay_samples;
                while (read_pos_f < 0.0f) read_pos_f += ps_buf_size;
                int   i0   = static_cast<int>(read_pos_f) % ps_buf_size;
                int   i1   = (i0 + 1) % ps_buf_size;
                float frac = read_pos_f - std::floor(read_pos_f);
                out->samples[i] = ps_delay_buf_[i0] * (1.0f - frac)
                                + ps_delay_buf_[i1] * frac;

                // Write
                ps_delay_buf_[ps_write_pos_] = x;
                ps_write_pos_ = (ps_write_pos_ + 1) % ps_buf_size;
            }
            out->value = out->samples[0];
            out->samples_written = true;
            return;
        }

        // Block-rate fallback
        float delay_ms = std::clamp(
            delay_ctl ? delay_ctl->value : 100.0f, 0.0f, 2000.0f);

        float delay_seconds = delay_ms / 1000.0f;
        float blocks_per_second = sample_rate_ / ctx.block_size;
        int   delay_blocks = static_cast<int>(delay_seconds * blocks_per_second + 0.5f);

        int buf_size = static_cast<int>(delay_buf_.size());
        delay_blocks = std::clamp(delay_blocks, 0, buf_size - 1);

        int read_pos = (write_pos_ - delay_blocks + buf_size) % buf_size;
        out->value = delay_buf_[read_pos];

        delay_buf_[write_pos_] = in->value;
        write_pos_ = (write_pos_ + 1) % buf_size;
    }

private:
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    float  sample_rate_    = 44100.0f;
    int    max_block_size_ = 512;

    // Block-rate state
    std::vector<float> delay_buf_;
    int                write_pos_ = 0;

    // Per-sample state
    std::vector<float> ps_delay_buf_;
    int                ps_write_pos_ = 0;
};

REGISTER_PLUGIN(ControlDelayLinePlugin);
REGISTER_PLUGIN_DYNAMIC(ControlDelayLinePlugin);
