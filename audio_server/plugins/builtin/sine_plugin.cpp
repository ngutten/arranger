// sine_plugin.cpp
// Port of SineNode to the Plugin API.
// Simple polyphonic sine synth with per-voice release envelope.

#include "plugin_api.h"
#include <cmath>
#include <cstring>
#include <unordered_map>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

class SinePlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.sine";
        d.display_name = "Sine Synth";
        d.category     = "Synth";
        d.doc          = "Simple polyphonic sine wave synthesizer with release envelope.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_out", "Audio Out", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain", "Output volume",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.15f, 0.0f, 1.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        voices_.clear();
    }

    void note_on(int channel, int pitch, int velocity) override {
        int key = channel * 128 + pitch;
        Voice v;
        v.freq = 440.0 * std::pow(2.0, (pitch - 69) / 12.0);
        v.amp  = velocity / 127.0f;
        voices_[key] = v;
    }

    void note_off(int channel, int pitch) override {
        int key = channel * 128 + pitch;
        auto it = voices_.find(key);
        if (it != voices_.end()) {
            it->second.releasing   = true;
            it->second.env_release = 30.0f / sample_rate_;
        }
    }

    void all_notes_off(int channel) override {
        if (channel == -1) {
            voices_.clear();
        } else {
            for (auto it = voices_.begin(); it != voices_.end(); ) {
                if (it->first / 128 == channel) it = voices_.erase(it);
                else ++it;
            }
        }
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* audio = buffers.audio.get("audio_out");
        auto* gain  = buffers.control.get("gain");

        float g = gain ? gain->value : 0.15f;

        float* L = audio->left;
        float* R = audio->right;
        // Outputs are pre-zeroed by the adapter

        std::vector<int> dead;
        for (auto& [key, v] : voices_) {
            double phase_inc = 2.0 * M_PI * v.freq / sample_rate_;
            float amp = v.amp * g;
            for (int i = 0; i < ctx.block_size; ++i) {
                float env = v.releasing ? (v.env *= (1.0f - v.env_release)) : 1.0f;
                float sample = static_cast<float>(std::sin(v.phase)) * amp * env;
                L[i] += sample;
                R[i] += sample;
                v.phase += phase_inc;
                if (v.phase > 2.0 * M_PI) v.phase -= 2.0 * M_PI;
            }
            if (v.releasing && v.env < 1e-4f) dead.push_back(key);
        }
        for (int k : dead) voices_.erase(k);

        // Soft clip
        for (int i = 0; i < ctx.block_size; ++i) {
            L[i] = std::tanh(L[i]);
            R[i] = std::tanh(R[i]);
        }
    }

private:
    struct Voice {
        double phase       = 0.0;
        double freq        = 440.0;
        float  amp         = 0.5f;
        bool   releasing   = false;
        float  env         = 1.0f;
        float  env_release = 0.0f;
        bool   done        = false;
    };

    float sample_rate_ = 44100.0f;
    std::unordered_map<int, Voice> voices_;
};

REGISTER_PLUGIN(SinePlugin);
REGISTER_PLUGIN_DYNAMIC(SinePlugin);

std::unique_ptr<Plugin> make_sine_plugin() { return std::make_unique<SinePlugin>(); }
