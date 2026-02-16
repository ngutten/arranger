// amp_plugin.cpp
// Stereo amplifier with tanh soft-clipping protection.
//
// output = tanh(input * gain * drive) / tanh(drive)
//
// The tanh normalisation means that at unity gain the output amplitude
// matches the input exactly (within the linear region of tanh).  As gain
// or drive increase, the output is progressively compressed near ±1 rather
// than hard-clipping.
//
// The `gain` port can be driven by a Control stream (e.g. from an envelope
// or LFO) for tremolo, VCA, and similar uses.
//
// Parameters:
//   gain   — linear amplitude scale [0, 4]  (Control-driven, default 1.0)
//   drive  — soft-clip knee: higher = harder knee, lower = gentler
//             [0.5, 10]  default 1.0  (1.0 ≈ gentle tanh, 6+ ≈ near hard clip)
//   pan    — stereo panning [-1, 1], 0 = centre

#include "plugin_api.h"
#include <cmath>
#include <algorithm>

class AmpPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.amp";
        d.display_name = "Amp";
        d.category     = "Utility";
        d.doc          = "Stereo amplifier with tanh soft-clipping. "
                         "The gain port accepts Control input for VCA/tremolo use. "
                         "Drive controls the softness of the clipping knee.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_in", "Audio In", "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },

            { "audio_out", "Audio Out", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },

            // Control-rate gain — the primary use is CV-driven amplitude
            { "gain", "Gain",
              "Linear gain multiplier. Accepts Control input for VCA use.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 4.0f },

            { "drive", "Drive",
              "Soft-clip knee hardness. 1 = gentle tanh, higher = harder knee.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.5f, 10.0f },

            { "pan", "Pan",
              "Stereo panning: -1 = hard left, 0 = centre, +1 = hard right",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -1.0f, 1.0f },
        };

        return d;
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        float gain  = param(buffers, "gain",  1.0f);
        float drive = std::max(0.5f, param(buffers, "drive", 1.0f));
        float pan   = param(buffers, "pan",   0.0f);

        // Equal-power panning
        // pan in [-1, 1] → angle in [0, π/2]
        // Use a simple approximation: sqrt-law panning
        float pan_norm = (pan + 1.0f) * 0.5f;  // [0, 1]
        float pan_l = std::sqrt(1.0f - pan_norm);
        float pan_r = std::sqrt(pan_norm);

        // Normalisation constant so tanh soft-clipping is unity at small signals
        // output = tanh(x * drive) / tanh(drive) — at x=1, gain=1 this saturates
        // gracefully. We pre-compute the denominator once per block.
        float norm = 1.0f / std::tanh(drive);

        float effective_gain = gain * drive;

        for (int i = 0; i < ctx.block_size; ++i) {
            float l = in->left[i];
            float r = in->right ? in->right[i] : l;

            // Soft clip
            float sl = std::tanh(l * effective_gain) * norm;
            float sr = std::tanh(r * effective_gain) * norm;

            out->left[i] = sl * pan_l;
            if (out->right)
                out->right[i] = sr * pan_r;
        }
    }

private:
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }
};

REGISTER_PLUGIN(AmpPlugin);
REGISTER_PLUGIN_DYNAMIC(AmpPlugin);

std::unique_ptr<Plugin> make_amp_plugin() {
    return std::make_unique<AmpPlugin>();
}
