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
        d.category     = "Utility";
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
        float a = param(buffers, "a", 0.0f);
        float b = param(buffers, "b", 0.0f);

        auto* out = buffers.control.get("output");
        if (out) out->value = a + b;
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
