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
            { "control_in", "Value", "Scheduled automation value (normalized 0-1)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "control_out", "Control Out", "Automation output (scaled to pattern's min/max)",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
        };

        // Configuration parameter for selecting which automation track to read from
        // The min/max scaling is pulled from the pattern at runtime, not stored as config
        d.config_params = {
            { "automation_track_id", "Automation Track", 
              "Select which automation track to read control values from. "
              "The UI should show a dropdown of available automation tracks.",
              ConfigType::Integer, "0" }
        };

        return d;
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "automation_track_id") {
            automation_track_id_ = std::stoi(value);
        }
    }


    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        auto* in  = buffers.control.get("control_in");
        auto* out = buffers.control.get("control_out");
        if (!out) return;

        float cur_val = in ? in->value : 0.0f;

        // Per-sample path
        if (out->samples && out->frames > 0) {
            if (in && in->samples) {
                // Input already per-sample — copy through
                for (int i = 0; i < out->frames; ++i)
                    out->samples[i] = in->samples[i];
            } else {
                // Block-rate input: linear interpolation from prev to current
                float delta = (out->frames > 1)
                    ? (cur_val - prev_value_) / static_cast<float>(out->frames)
                    : 0.0f;
                for (int i = 0; i < out->frames; ++i)
                    out->samples[i] = prev_value_ + delta * (i + 1);
            }
            out->value = out->samples[0];
            out->samples_written = true;
        } else {
            out->value = cur_val;
        }

        prev_value_ = cur_val;
    }

private:
    int automation_track_id_ = 0;    // 0 = no track selected
    float prev_value_ = 0.0f;
};

REGISTER_PLUGIN(ControlSourcePlugin);
REGISTER_PLUGIN_DYNAMIC(ControlSourcePlugin);

std::unique_ptr<Plugin> make_control_source_plugin() { return std::make_unique<ControlSourcePlugin>(); }
