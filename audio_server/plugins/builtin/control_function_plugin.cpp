// control_function_plugin.cpp
// Stateless mathematical function applicator for control signals.
//
// Applies one of several mathematical functions to the input:
//   0 — Abs:    |x|
//   1 — Square: x²
//   2 — Sin:    sin(2π·x)
//   3 — Tanh:   tanh(x)
//
// Useful for:
//   - Rectifying bipolar signals (Abs)
//   - Squaring for exponential-feel curves (Square)
//   - Waveshaping control signals into periodic patterns (Sin)
//   - Soft-clipping/saturating control signals (Tanh)
//
// Parameters:
//   function — Which function to apply (categorical, 0..3)

#include "plugin_api.h"
#include <cmath>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

class ControlFunctionPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_function";
        d.display_name = "Control Function";
        d.category     = "Control";
        d.doc          = "Applies a mathematical function to a control signal. "
                         "Functions: Abs, Square, Sin, Tanh.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "input", "Control In", "Input control signal",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -1e6f, 1e6f },

            { "output", "Control Out", "Transformed control signal",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Continuous, 0.0f, -1e6f, 1e6f },

            { "function", "Function", "Mathematical function to apply",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 3.0f, 1.0f,
              {"Abs", "Square", "Sin", "Tanh"} },
        };

        return d;
    }

    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        auto* in  = buffers.control.get("input");
        auto* out = buffers.control.get("output");
        if (!in || !out) return;

        int func = std::clamp(
            static_cast<int>(param(buffers, "function", 0.0f)), 0, 3);

        float x = in->value;
        float y;

        switch (func) {
            case 0:  y = std::abs(x);                                        break;
            case 1:  y = x * x;                                              break;
            case 2:  y = std::sin(2.0f * static_cast<float>(M_PI) * x);      break;
            case 3:  y = std::tanh(x);                                       break;
            default: y = x;                                                  break;
        }

        out->value = y;
    }

private:
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }
};

REGISTER_PLUGIN(ControlFunctionPlugin);
REGISTER_PLUGIN_DYNAMIC(ControlFunctionPlugin);
