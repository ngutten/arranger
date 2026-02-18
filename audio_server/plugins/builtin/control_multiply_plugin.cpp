// control_multiply_plugin.cpp
// Multiplies two Control streams together.
//
// If only one input is connected, the frontend supplies the default value
// for the unconnected port (configured per-node in the graph editor).
//
// Ports carry no range clamp — values like frequency (20–20000 Hz) or
// arbitrary CV offsets pass through unmodified.

#include "plugin_api.h"

// Sentinel for "effectively unbounded" range on control ports.
// The engine should not clamp values at these limits.
static constexpr float UNBOUNDED = 1e9f;

class ControlMultiplyPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_multiply";
        d.display_name = "Control Multiply";
        d.category     = "Control";
        d.doc          = "Multiplies two Control streams: output = A × B. "
                         "Unconnected ports use the node's default value. "
                         "No range clamping — suitable for frequency, gain, and "
                         "arbitrary CV values.";
        d.author       = "builtin";
        d.version      = 2;

        d.ports = {
            { "a", "A", "First input",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, -UNBOUNDED, UNBOUNDED },

            { "b", "B", "Second input",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, -UNBOUNDED, UNBOUNDED },

            { "output", "Output", "A × B",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Meter, 0.0f, -UNBOUNDED, UNBOUNDED },
        };

        return d;
    }

    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        float a = param(buffers, "a", 1.0f);
        float b = param(buffers, "b", 1.0f);

        auto* out = buffers.control.get("output");
        if (out) out->value = a * b;
    }

private:
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }
};

REGISTER_PLUGIN(ControlMultiplyPlugin);
REGISTER_PLUGIN_DYNAMIC(ControlMultiplyPlugin);

std::unique_ptr<Plugin> make_control_multiply_plugin() {
    return std::make_unique<ControlMultiplyPlugin>();
}
