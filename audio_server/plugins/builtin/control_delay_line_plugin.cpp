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
        
        // Max delay in blocks: 2 seconds worth of blocks at worst-case block size
        // We use actual block size at runtime, but allocate for worst case
        int max_blocks = static_cast<int>(MAX_DELAY_SEC * sample_rate / max_block_size) + 2;
        
        delay_buf_.assign(max_blocks, 0.0f);
        write_pos_ = 0;
    }

    void deactivate() override {
        delay_buf_.clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.control.get("control_in");
        auto* out = buffers.control.get("control_out");
        if (!in || !out) return;

        float delay_ms = std::clamp(param(buffers, "delay_ms", 100.0f), 0.0f, 2000.0f);
        
        // Convert delay time to number of blocks
        float delay_seconds = delay_ms / 1000.0f;
        float blocks_per_second = sample_rate_ / ctx.block_size;
        int   delay_blocks = static_cast<int>(delay_seconds * blocks_per_second + 0.5f);  // Round
        
        // Clamp to buffer size
        int buf_size = static_cast<int>(delay_buf_.size());
        delay_blocks = std::clamp(delay_blocks, 0, buf_size - 1);
        
        // Read delayed value
        int read_pos = (write_pos_ - delay_blocks + buf_size) % buf_size;
        out->value = delay_buf_[read_pos];
        
        // Write current input
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
    
    std::vector<float> delay_buf_;
    int                write_pos_ = 0;
};

REGISTER_PLUGIN(ControlDelayLinePlugin);
REGISTER_PLUGIN_DYNAMIC(ControlDelayLinePlugin);
