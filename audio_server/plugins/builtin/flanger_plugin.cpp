// flanger_plugin.cpp
// Classic flanger effect with feedback and LFO modulation.
//
// Algorithm:
//   - Input signal is mixed with a short delayed copy (1-15ms range)
//   - Delay time is modulated by a low-frequency oscillator (sine wave)
//   - Feedback loop creates resonant comb-filter peaks
//   - When delay sweeps through very short times, generates the classic
//     jet-plane whooshing flanger sound via phase cancellation
//
// Parameters:
//   rate      — LFO frequency in Hz [0.01, 10]
//   depth     — LFO modulation depth [0, 1]
//   feedback  — Amount of delayed signal fed back [-0.95, 0.95]
//   delay_ms  — Center delay time in milliseconds [1, 15]
//   mix       — Dry/wet mix [0, 1]

#include "plugin_api.h"
#include <cmath>
#include <vector>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

class FlangerPlugin final : public Plugin {
public:
    static constexpr int MAX_DELAY_MS = 20;  // Safety margin above max user setting

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.flanger";
        d.display_name = "Flanger";
        d.category     = "Effect";
        d.doc          = "Classic flanger effect with LFO-modulated short delay line. "
                         "Creates sweeping comb-filter sounds via phase cancellation.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_in",  "Audio In",  "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Flanged stereo output",
              PluginPortType::AudioStereo, PortRole::Output },
            
            { "rate", "Rate (Hz)", "LFO frequency in Hz",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.01f, 10.0f },
            
            { "depth", "Depth", "LFO modulation depth (0 = no modulation, 1 = full range)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.7f, 0.0f, 1.0f },
            
            { "feedback", "Feedback", "Feedback amount (negative = inverted phase)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, -0.95f, 0.95f },
            
            { "delay_ms", "Delay (ms)", "Center delay time in milliseconds",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 5.0f, 1.0f, 15.0f },
            
            { "mix", "Mix", "Dry/wet mix (0 = all dry, 1 = all wet)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        
        int max_samples = static_cast<int>(MAX_DELAY_MS * sample_rate / 1000.0f) + 4;  // +4 for interp
        
        for (int ch = 0; ch < 2; ++ch) {
            delay_buf_[ch].assign(max_samples, 0.0f);
            write_pos_[ch] = 0;
        }
        
        lfo_phase_ = 0.0;
    }

    void deactivate() override {
        for (int ch = 0; ch < 2; ++ch)
            delay_buf_[ch].clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        auto* rate_ctl     = buffers.control.get("rate");
        auto* depth_ctl    = buffers.control.get("depth");
        auto* feedback_ctl = buffers.control.get("feedback");
        auto* delay_ctl    = buffers.control.get("delay_ms");
        auto* mix_ctl      = buffers.control.get("mix");

        bool ps_rate     = rate_ctl     && rate_ctl->samples;
        bool ps_depth    = depth_ctl    && depth_ctl->samples;
        bool ps_feedback = feedback_ctl && feedback_ctl->samples;
        bool ps_delay    = delay_ctl    && delay_ctl->samples;
        bool ps_mix      = mix_ctl      && mix_ctl->samples;

        float const_rate     = std::clamp(rate_ctl     ? rate_ctl->value     : 0.5f,   0.01f, 10.0f);
        float const_depth    = std::clamp(depth_ctl    ? depth_ctl->value    : 0.7f,   0.0f,  1.0f);
        float const_feedback = std::clamp(feedback_ctl ? feedback_ctl->value : 0.5f,  -0.95f, 0.95f);
        float const_delay_ms = std::clamp(delay_ctl    ? delay_ctl->value    : 5.0f,   1.0f,  15.0f);
        float const_mix      = std::clamp(mix_ctl      ? mix_ctl->value      : 0.5f,   0.0f,  1.0f);

        for (int i = 0; i < ctx.block_size; ++i) {
            float rate     = ps_rate     ? std::clamp(rate_ctl->samples[i],     0.01f, 10.0f)  : const_rate;
            float depth    = ps_depth    ? std::clamp(depth_ctl->samples[i],    0.0f,  1.0f)   : const_depth;
            float fb       = ps_feedback ? std::clamp(feedback_ctl->samples[i],-0.95f, 0.95f)  : const_feedback;
            float delay_ms = ps_delay    ? std::clamp(delay_ctl->samples[i],    1.0f,  15.0f)  : const_delay_ms;
            float mix      = ps_mix      ? std::clamp(mix_ctl->samples[i],      0.0f,  1.0f)   : const_mix;

            float delay_center = delay_ms * sample_rate_ / 1000.0f;
            float delay_range  = delay_center * depth;

            // LFO modulation
            float lfo = static_cast<float>(std::sin(lfo_phase_));
            float delay_samples = delay_center + delay_range * lfo;

            // Process both channels
            float in_l  = in->left[i];
            float in_r  = in->right ? in->right[i] : in->left[i];

            float delayed_l = read_linear(0, delay_samples);
            float delayed_r = read_linear(1, delay_samples);

            // Feedback
            float feedback_l = in_l + delayed_l * fb;
            float feedback_r = in_r + delayed_r * fb;

            // Write to delay line
            int buf_size = static_cast<int>(delay_buf_[0].size());
            delay_buf_[0][write_pos_[0]] = feedback_l;
            delay_buf_[1][write_pos_[1]] = feedback_r;

            write_pos_[0] = (write_pos_[0] + 1) % buf_size;
            write_pos_[1] = (write_pos_[1] + 1) % buf_size;

            // Mix dry and wet
            out->left[i] = in_l * (1.0f - mix) + delayed_l * mix;
            if (out->right)
                out->right[i] = in_r * (1.0f - mix) + delayed_r * mix;

            // Advance LFO phase with per-sample rate
            double phase_inc = 2.0 * M_PI * rate / sample_rate_;
            lfo_phase_ += phase_inc;
            if (lfo_phase_ >= 2.0 * M_PI)
                lfo_phase_ -= 2.0 * M_PI;
        }
    }

private:
    // Linear interpolation read from circular delay buffer
    float read_linear(int ch, float delay_samples) const {
        int buf_size = static_cast<int>(delay_buf_[ch].size());
        
        // Read position is write_pos - delay_samples (mod buf_size)
        float read_pos_f = write_pos_[ch] - delay_samples;
        while (read_pos_f < 0.0f) read_pos_f += buf_size;
        
        int   i0 = static_cast<int>(read_pos_f) % buf_size;
        int   i1 = (i0 + 1) % buf_size;
        float frac = read_pos_f - std::floor(read_pos_f);
        
        return delay_buf_[ch][i0] * (1.0f - frac) + delay_buf_[ch][i1] * frac;
    }

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    float  sample_rate_ = 44100.0f;
    double lfo_phase_   = 0.0;
    
    std::vector<float> delay_buf_[2];
    int                write_pos_[2] = {0, 0};
};

REGISTER_PLUGIN(FlangerPlugin);
REGISTER_PLUGIN_DYNAMIC(FlangerPlugin);
