// control_smooth_plugin.cpp
// One-pole IIR low-pass filter for control signals.
//
// Smooths control-rate signals using exponential smoothing:
//   y[n] = α·x[n] + (1-α)·y[n-1]
// where α = 1 - exp(-dt/τ), dt = block_size/sample_rate, τ = time_constant.
//
// Useful for:
//   - Smoothing gate/pulse signals into attack/release envelopes
//   - Removing noise or jitter from modulation sources
//   - Creating portamento-like glides on control signals
//
// Parameters:
//   time_constant — Smoothing time constant in seconds [0.001, 5.0], default 0.5

#include "plugin_api.h"
#include <cmath>
#include <algorithm>

class ControlSmoothPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_smooth";
        d.display_name = "Control Smooth";
        d.category     = "Control";
        d.doc          = "One-pole exponential smoother for control signals. "
                         "Smooths step changes into exponential curves with a "
                         "configurable time constant.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "input", "Control In", "Control signal to smooth",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -1e6f, 1e6f },

            { "output", "Control Out", "Smoothed control signal",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Continuous, 0.0f, -1e6f, 1e6f },

            { "time_constant", "Time Constant", "Smoothing time constant in seconds",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.001f, 5.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        y_ = 0.0f;
    }

    void deactivate() override {
        y_ = 0.0f;
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.control.get("input");
        auto* out = buffers.control.get("output");
        if (!in || !out) return;

        float tau = std::max(0.001f, param(buffers, "time_constant", 0.5f));
        float dt  = static_cast<float>(ctx.block_size) / sample_rate_;
        float alpha = 1.0f - std::exp(-dt / tau);

        y_ = alpha * in->value + (1.0f - alpha) * y_;
        out->value = y_;
    }

private:
    float sample_rate_ = 44100.0f;
    float y_ = 0.0f;

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }
};

REGISTER_PLUGIN(ControlSmoothPlugin);
REGISTER_PLUGIN_DYNAMIC(ControlSmoothPlugin);
