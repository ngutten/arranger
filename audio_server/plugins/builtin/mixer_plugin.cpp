// mixer_plugin.cpp
// Port of MixerNode to the Plugin API.
//
// Sums N stereo input pairs into one stereo output.
// Channel count is set via configure("channel_count", "N") before activate().
//
// Each channel has a gain control port (gain_0, gain_1, ...) plus a master_gain.
// The descriptor is built dynamically based on channel_count_.

#include "plugin_api.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <vector>

class MixerPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.mixer";
        d.display_name = "Mixer";
        d.category     = "Mixer";
        d.doc          = "Sums N stereo input pairs into one stereo output with per-channel gain.";
        d.author       = "builtin";
        d.version      = 1;

        // Dynamic ports based on channel count
        for (int i = 0; i < channel_count_; ++i) {
            std::string idx = std::to_string(i);
            d.ports.push_back({
                "audio_in_" + idx, "Input " + idx, "Stereo input channel " + idx,
                PluginPortType::AudioStereo, PortRole::Input
            });
            d.ports.push_back({
                "gain_" + idx, "Gain " + idx, "Gain for input channel " + idx,
                PluginPortType::Control, PortRole::Input,
                ControlHint::Continuous, 1.0f, 0.0f, 2.0f
            });
        }

        d.ports.push_back({
            "master_gain", "Master Gain", "Master output gain",
            PluginPortType::Control, PortRole::Input,
            ControlHint::Continuous, 1.0f, 0.0f, 2.0f
        });

        d.ports.push_back({
            "audio_out", "Audio Out", "Stereo mix output",
            PluginPortType::AudioStereo, PortRole::Output
        });

        d.config_params = {
            { "channel_count", "Channels", "Number of stereo input channels",
              ConfigType::Integer, std::to_string(channel_count_) }
        };

        return d;
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "channel_count") {
            int n = std::stoi(value);
            if (n >= 1 && n <= 64) channel_count_ = n;
        }
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* out = buffers.audio.get("audio_out");
        if (!out) return;

        // Output is pre-zeroed by the adapter

        auto* master = buffers.control.get("master_gain");
        float mg = master ? master->value : 1.0f;

        for (int ch = 0; ch < channel_count_; ++ch) {
            std::string idx = std::to_string(ch);
            auto* in   = buffers.audio.get("audio_in_" + idx);
            auto* gain = buffers.control.get("gain_" + idx);
            if (!in) continue;

            float g = (gain ? gain->value : 1.0f) * mg;

            for (int i = 0; i < ctx.block_size; ++i) {
                out->left[i]  += in->left[i]  * g;
                out->right[i] += in->right[i] * g;
            }
        }

        // Soft clip
        for (int i = 0; i < ctx.block_size; ++i) {
            out->left[i]  = std::tanh(out->left[i]);
            out->right[i] = std::tanh(out->right[i]);
        }
    }

private:
    int channel_count_ = 2;
};

REGISTER_PLUGIN(MixerPlugin);

std::unique_ptr<Plugin> make_mixer_plugin() { return std::make_unique<MixerPlugin>(); }
