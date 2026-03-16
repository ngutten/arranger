// control_shaper_plugin.cpp
// Shapes an incoming control signal with a lagged, damped oscillator model

#include "plugin_api.h"
#include <math.h>

// Sentinel for "effectively unbounded" range on control ports.
// The engine should not clamp values at these limits.
static constexpr float UNBOUNDED = 1e9f;

class ControlResonatorPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_resonator";
        d.display_name = "Control Resonator";
        d.category     = "Control";
        d.doc          = "Drives a damped oscillator with the control input. "
                         "dv/dt = -damping*v + coupling*input - k*out",
                         "dout/dt = v + drag*input - decay*out";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "input", "Input", "Control input",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, -UNBOUNDED, UNBOUNDED },
              
            { "drag", "Drag", "Immediate effect of input on output",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, -5.0f, 5.0f },
              
            { "decay", "Decay", "Direct relaxation of output towards zero",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.01f, 10.0f},
              
            { "coupling", "Coupling", "Effect of input on velocity",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, -5.0f, 5.0f },
              
            { "damping", "Damping", "Damping of velocity towards zero",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.01f, 10.0f },
              
            { "k", "Spring", "Force of output position towards zero",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 10.0f },

            { "output", "Output", "Oscillator output",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Meter, 0.0f, -UNBOUNDED, UNBOUNDED },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        x_ = 0.0;
        v_ = 0.0;
    }
    
    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in_buf  = buffers.control.get("input");
        auto* out = buffers.control.get("output");
        if (!out) return;

        float damp = param(buffers, "damping", 1.0f);
        float decay = param(buffers, "decay", 1.0f);
        float coupling = param(buffers, "coupling", 1.0f);
        float k = param(buffers, "k", 1.0f);
        float drag = param(buffers, "drag", 1.0f);

        bool ps_in = in_buf && in_buf->samples;

        // Per-sample path
        if (out->samples && out->frames > 0) {
            float dt = 1.0f / sample_rate_;
            float exp_damp  = exp(-10*dt*damp);
            float exp_decay = exp(-10*dt*decay);
            float scale = 10*dt;

            for (int i = 0; i < out->frames; ++i) {
                float a = ps_in ? in_buf->samples[i]
                                : (in_buf ? in_buf->value : 1.0f);

                v_ += scale*(coupling*a - k*x_);
                v_ *= exp_damp;
                x_ += scale*(drag*a + v_);
                x_ *= exp_decay;

                out->samples[i] = static_cast<float>(x_);
            }
            out->value = out->samples[0];
            out->samples_written = true;
            return;
        }

        // Block-rate fallback
        float a = in_buf ? in_buf->value : 1.0f;
        float dt = ctx.block_size / sample_rate_;

        v_ += 10*dt*(coupling*a - k*x_);
        v_ *= exp(-10*dt*damp);
        x_ += 10*dt*(drag*a + v_);
        x_ *= exp(-10*dt*decay);

        out->value = static_cast<float>(x_);
    }

private:
    double x_       = 0.0;  // Oscillator position
    double v_       = 0.0;  // Oscillator velocity
    float sample_rate_ = 44100.0;
    
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }
};

REGISTER_PLUGIN(ControlResonatorPlugin);
REGISTER_PLUGIN_DYNAMIC(ControlResonatorPlugin);

std::unique_ptr<Plugin> make_control_resonator_plugin() {
    return std::make_unique<ControlResonatorPlugin>();
}
