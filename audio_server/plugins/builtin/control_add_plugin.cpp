// control_add_plugin.cpp
// Adds two Control streams together.
//
// If only one input is connected, the frontend supplies the default value
// for the unconnected port (configured per-node in the graph editor).
//
// Ports carry no range clamp — values like frequency (20–20000 Hz) or
// arbitrary CV offsets pass through unmodified.

#include "plugin_api.h"

static constexpr float UNBOUNDED = 1e9f;

class ControlAddPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_add";
        d.display_name = "Control Add";
        d.category     = "Control";
        d.doc          = "Adds two Control streams: output = A + B. "
                         "Unconnected ports use the node's default value. "
                         "No range clamping — suitable for frequency, gain, and "
                         "arbitrary CV values.";
        d.author       = "builtin";
        d.version      = 2;

        d.ports = {
            { "a", "A", "First input",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -UNBOUNDED, UNBOUNDED },

            { "b", "B", "Second input",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -UNBOUNDED, UNBOUNDED },

            { "output", "Output", "A + B",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Meter, 0.0f, -UNBOUNDED, UNBOUNDED },
        };

        return d;
    }

    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        auto* a_buf = buffers.control.get("a");
        auto* b_buf = buffers.control.get("b");
        auto* out   = buffers.control.get("output");
        if (!out) return;

        float a_val = a_buf ? a_buf->value : 0.0f;
        float b_val = b_buf ? b_buf->value : 0.0f;
        bool ps_a = a_buf && a_buf->samples;
        bool ps_b = b_buf && b_buf->samples;

        if ((ps_a || ps_b) && out->samples && out->frames > 0) {
            for (int i = 0; i < out->frames; ++i) {
                float a = ps_a ? a_buf->samples[i] : a_val;
                float b = ps_b ? b_buf->samples[i] : b_val;
                out->samples[i] = a + b;
            }
            out->value = out->samples[0];
            out->samples_written = true;
        } else {
            out->value = a_val + b_val;
        }
    }

private:
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }
};

REGISTER_PLUGIN(ControlAddPlugin);
REGISTER_PLUGIN_DYNAMIC(ControlAddPlugin);

std::unique_ptr<Plugin> make_control_add_plugin() {
    return std::make_unique<ControlAddPlugin>();
}
