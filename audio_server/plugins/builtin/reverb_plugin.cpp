// reverb_plugin.cpp
// Schroeder-style stereo reverb: 8 comb filters (4 per channel, slightly
// detuned L vs R for stereo width) feeding into 2 series allpass filters.
//
// Controls: room_size (feedback), damping (lowpass in comb), dry/wet mix.

#include "plugin_api.h"
#include <cmath>
#include <cstring>
#include <algorithm>
#include <vector>

// Delay line with integrated one-pole lowpass (for comb filter damping)
struct DelayLine {
    std::vector<float> buf;
    int write_pos = 0;
    int length    = 0;
    float filter_state = 0.0f;

    void resize(int len) {
        length = len;
        buf.assign(len, 0.0f);
        write_pos = 0;
        filter_state = 0.0f;
    }

    void clear() {
        std::fill(buf.begin(), buf.end(), 0.0f);
        filter_state = 0.0f;
        write_pos = 0;
    }

    // Comb filter: read delayed, apply lowpass, feedback
    float process_comb(float input, float feedback, float damp) {
        float delayed = buf[write_pos];
        // One-pole lowpass on the feedback path
        filter_state = delayed * (1.0f - damp) + filter_state * damp;
        buf[write_pos] = input + filter_state * feedback;
        if (++write_pos >= length) write_pos = 0;
        return delayed;
    }

    // Allpass filter
    float process_allpass(float input, float feedback) {
        float delayed = buf[write_pos];
        float output = delayed - input;
        buf[write_pos] = input + delayed * feedback;
        if (++write_pos >= length) write_pos = 0;
        return output;
    }
};

// Comb filter delay lengths in samples at 44100 Hz (Freeverb-derived primes)
static constexpr int COMB_LENGTHS[8] = {
    1116, 1188, 1277, 1356,   // L channel
    1139, 1211, 1300, 1379,   // R channel (slightly detuned for stereo)
};

// Allpass delay lengths
static constexpr int ALLPASS_LENGTHS[4] = {
    556, 441,    // L channel
    579, 464,    // R channel
};

class ReverbPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.reverb";
        d.display_name = "Reverb";
        d.category     = "Effect";
        d.doc          = "Schroeder/Freeverb-style stereo reverb.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            // Audio
            { "audio_in", "Audio In", "Stereo input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo output",
              PluginPortType::AudioStereo, PortRole::Output },
            // Controls
            { "room_size", "Room Size", "Reverb tail length (feedback amount)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.7f, 0.0f, 1.0f },
            { "damping", "Damping", "High-frequency absorption in the reverb tail",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },
            { "wet", "Wet", "Wet signal level",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, 0.0f, 1.0f },
            { "dry", "Dry", "Dry signal level",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f },
            { "width", "Width", "Stereo width of reverb (0=mono, 1=full stereo)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        float sr_scale = sample_rate / 44100.0f;
        for (int i = 0; i < 4; ++i) {
            combs_L_[i].resize(static_cast<int>(COMB_LENGTHS[i] * sr_scale));
            combs_R_[i].resize(static_cast<int>(COMB_LENGTHS[i + 4] * sr_scale));
        }
        for (int i = 0; i < 2; ++i) {
            allpass_L_[i].resize(static_cast<int>(ALLPASS_LENGTHS[i] * sr_scale));
            allpass_R_[i].resize(static_cast<int>(ALLPASS_LENGTHS[i + 2] * sr_scale));
        }
    }

    void deactivate() override {
        for (auto& c : combs_L_) c.clear();
        for (auto& c : combs_R_) c.clear();
        for (auto& a : allpass_L_) a.clear();
        for (auto& a : allpass_R_) a.clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        auto* room_ctl  = buffers.control.get("room_size");
        auto* damp_ctl  = buffers.control.get("damping");
        auto* wet_ctl   = buffers.control.get("wet");
        auto* dry_ctl   = buffers.control.get("dry");
        auto* width_ctl = buffers.control.get("width");

        float room_size = room_ctl  ? room_ctl->value  : 0.7f;
        float damping   = damp_ctl  ? damp_ctl->value  : 0.5f;
        float wet       = wet_ctl   ? wet_ctl->value   : 0.3f;
        float dry       = dry_ctl   ? dry_ctl->value   : 1.0f;
        float width     = width_ctl ? width_ctl->value : 1.0f;

        // Scale room_size to a usable feedback range
        float feedback = room_size * 0.28f + 0.7f;  // maps [0,1] → [0.7, 0.98]
        feedback = std::min(feedback, 0.98f);

        float wet1 = wet * (width * 0.5f + 0.5f);
        float wet2 = wet * ((1.0f - width) * 0.5f);

        for (int i = 0; i < ctx.block_size; ++i) {
            float in_L = in->left[i];
            float in_R = in->right ? in->right[i] : in_L;
            // Mix to mono for reverb input (standard Freeverb approach)
            float input = (in_L + in_R) * 0.5f;

            // Parallel comb filters
            float sum_L = 0.0f, sum_R = 0.0f;
            for (int c = 0; c < 4; ++c) {
                sum_L += combs_L_[c].process_comb(input, feedback, damping);
                sum_R += combs_R_[c].process_comb(input, feedback, damping);
            }

            // Series allpass filters
            for (int a = 0; a < 2; ++a) {
                sum_L = allpass_L_[a].process_allpass(sum_L, 0.5f);
                sum_R = allpass_R_[a].process_allpass(sum_R, 0.5f);
            }

            // Mix with stereo width control
            out->left[i]  = in_L * dry + sum_L * wet1 + sum_R * wet2;
            if (out->right)
                out->right[i] = in_R * dry + sum_R * wet1 + sum_L * wet2;
        }
    }

private:
    DelayLine combs_L_[4];
    DelayLine combs_R_[4];
    DelayLine allpass_L_[2];
    DelayLine allpass_R_[2];
};

REGISTER_PLUGIN(ReverbPlugin);
REGISTER_PLUGIN_DYNAMIC(ReverbPlugin);

// Factory for explicit registration from builtin_plugins.cpp
std::unique_ptr<Plugin> make_reverb_plugin() {
    return std::make_unique<ReverbPlugin>();
}
