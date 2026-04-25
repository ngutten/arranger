// audio_delay_line_plugin.cpp
// Simple audio delay line — delays audio signal by a settable time up to 2 seconds.
//
// Unlike traditional delay effects with feedback and filtering, this is a pure
// delay line utility for time-alignment, lookahead processing, or routing tricks.
//
// Parameters:
//   delay_ms    — Delay time in milliseconds [0, 2000]
//   beat_sync   — Toggle: when on, use delay_beats and current BPM instead of delay_ms
//   delay_beats — Delay time in beats [0, 4]; only read when beat_sync is on

#include "plugin_api.h"
#include <cmath>
#include <vector>
#include <algorithm>

class AudioDelayLinePlugin final : public Plugin {
public:
    static constexpr float MAX_DELAY_SEC = 2.0f;

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.audio_delay_line";
        d.display_name = "Audio Delay Line";
        d.category     = "Effect";
        d.doc          = "Simple delay line for time-aligning audio signals. Pure delay with no "
                         "feedback or filtering — useful for lookahead processing, speaker alignment, "
                         "or creative routing. Range: 0-2000 ms.";
        d.author       = "builtin";
        d.version      = 2;

        d.ports = {
            { "audio_in",  "Audio In",  "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Delayed stereo output",
              PluginPortType::AudioStereo, PortRole::Output },

            { "delay_ms", "Delay (ms)", "Delay time in milliseconds (used when Beat Sync is off)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 100.0f, 0.0f, 2000.0f },
            { "beat_sync", "Beat Sync",
              "When on, the delay is measured in beats and follows the project tempo.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Toggle, 0.0f, 0.0f, 1.0f },
            { "delay_beats", "Delay (beats)",
              "Delay in beats (used when Beat Sync is on). 0.25 = 1/16, 0.5 = 1/8, 1.0 = 1/4, etc.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 4.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        
        int max_samples = static_cast<int>(MAX_DELAY_SEC * sample_rate) + 4;  // +4 for interp
        
        for (int ch = 0; ch < 2; ++ch) {
            delay_buf_[ch].assign(max_samples, 0.0f);
            write_pos_[ch] = 0;
        }
    }

    void deactivate() override {
        for (int ch = 0; ch < 2; ++ch)
            delay_buf_[ch].clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        auto* ms_ctl    = buffers.control.get("delay_ms");
        auto* beats_ctl = buffers.control.get("delay_beats");
        auto* sync_ctl  = buffers.control.get("beat_sync");
        bool  beat_sync = sync_ctl && sync_ctl->value > 0.5f;
        bool  ps_ms     = ms_ctl    && ms_ctl->samples;
        bool  ps_beats  = beats_ctl && beats_ctl->samples;
        float const_ms    = std::clamp(
            ms_ctl ? ms_ctl->value : 100.0f, 0.0f, 2000.0f);
        float const_beats = std::clamp(
            beats_ctl ? beats_ctl->value : 0.5f, 0.0f, 4.0f);
        // When beat-sync is on, delay_ms is computed from delay_beats and
        // the block's tempo. We don't track tempo changes at per-sample
        // resolution inside the block; tempo is sampled once here.
        float bpm = ctx.bpm > 0.0f ? ctx.bpm : 120.0f;

        int buf_size = static_cast<int>(delay_buf_[0].size());

        for (int i = 0; i < ctx.block_size; ++i) {
            float dm;
            if (beat_sync) {
                float db = ps_beats ? beats_ctl->samples[i] : const_beats;
                db = std::clamp(db, 0.0f, 4.0f);
                dm = db * 60000.0f / bpm;   // beats → ms at current bpm
            } else {
                dm = ps_ms ? ms_ctl->samples[i] : const_ms;
            }
            dm = std::clamp(dm, 0.0f, 2000.0f);
            float delay_samples = dm * sample_rate_ / 1000.0f;

            float in_l = in->left[i];
            float in_r = in->right ? in->right[i] : in->left[i];

            // Read delayed samples
            float out_l = read_linear(0, delay_samples, buf_size);
            float out_r = read_linear(1, delay_samples, buf_size);

            // Write new samples
            delay_buf_[0][write_pos_[0]] = in_l;
            delay_buf_[1][write_pos_[1]] = in_r;

            write_pos_[0] = (write_pos_[0] + 1) % buf_size;
            write_pos_[1] = (write_pos_[1] + 1) % buf_size;

            // Output
            out->left[i] = out_l;
            if (out->right) out->right[i] = out_r;
        }
    }

private:
    float read_linear(int ch, float delay_samples, int buf_size) const {
        // Read position is write_pos - delay_samples (mod buf_size)
        float read_pos_f = write_pos_[ch] - delay_samples;
        while (read_pos_f < 0.0f) read_pos_f += buf_size;
        
        int   i0   = static_cast<int>(read_pos_f) % buf_size;
        int   i1   = (i0 + 1) % buf_size;
        float frac = read_pos_f - std::floor(read_pos_f);
        
        return delay_buf_[ch][i0] * (1.0f - frac) + delay_buf_[ch][i1] * frac;
    }

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    float  sample_rate_ = 44100.0f;
    
    std::vector<float> delay_buf_[2];
    int                write_pos_[2] = {0, 0};
};

REGISTER_PLUGIN(AudioDelayLinePlugin);
REGISTER_PLUGIN_DYNAMIC(AudioDelayLinePlugin);
