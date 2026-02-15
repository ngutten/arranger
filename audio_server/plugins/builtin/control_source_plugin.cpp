// control_source_plugin.cpp
// Port of ControlSourceNode to the Plugin API.
//
// Receives scheduled automation values via push_control() (forwarded by the
// adapter as a pending value on the "control_in" input port) and passes
// them through to the "control_out" output port.
//
// The control_in port is typically unconnected in the graph — the Dispatcher
// pushes values via push_control(), which the adapter routes to the first
// non-output control port's atomic pending_value.

#include "plugin_api.h"

class ControlSourcePlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_source";
        d.display_name = "Control Source";
        d.category     = "Utility";
        d.doc          = "Outputs scheduled control values from sequencer automation lanes.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            // Input that receives push_control values via the adapter.
            // Not typically connected in the graph — automation comes from
            // the Dispatcher/scheduler path.
            { "control_in", "Value", "Scheduled automation value",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "control_out", "Control Out", "Automation output",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
        };

        return d;
    }

    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        auto* in  = buffers.control.get("control_in");
        auto* out = buffers.control.get("control_out");
        if (out) out->value = in ? in->value : 0.0f;
    }
};

REGISTER_PLUGIN(ControlSourcePlugin);
REGISTER_PLUGIN_DYNAMIC(ControlSourcePlugin);

std::unique_ptr<Plugin> make_control_source_plugin() { return std::make_unique<ControlSourcePlugin>(); }
