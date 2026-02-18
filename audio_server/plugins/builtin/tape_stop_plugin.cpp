// tape_stop_plugin.cpp
// Tape stop effect — simulates the sound of a tape machine slowing down.
//
// Algorithm:
//   - When triggered, playback rate smoothly ramps from 1.0 down to 0.0
//   - Uses the same granular resampling as the pitch shifter but with
//     time-varying ratio
//   - Stop curve is exponential (sounds natural) or linear (more abrupt)
//   - Retrigger resets to full speed instantly
//
// Parameters:
//   trigger      — Gate signal: rising edge starts the stop
//   stop_time    — Duration of the slowdown in seconds [0.1, 10]
//   curve        — Stop curve shape: 0 = linear, 1 = exponential
//   auto_reset   — If 1, automatically return to full speed when stopped

#include "plugin_api.h"
#include <cmath>
#include <vector>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

class TapeStopPlugin final : public Plugin {
public:
    static constexpr int XFADE_SAMPLES = 512;
    static constexpr int GRAIN_OFFSET  = 2048;
    static constexpr int MIN_DIST      = 128;
    static constexpr int MAX_DIST      = 4096;
    static constexpr int BUF_SIZE      = 65536;
    static constexpr int BUF_MASK      = BUF_SIZE - 1;

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.tape_stop";
        d.display_name = "Tape Stop";
        d.category     = "Effect";
        d.doc          = "Simulates tape machine slowdown effect. Trigger to ramp playback "
                         "speed from normal to stopped over a settable duration. Uses granular "
                         "resampling for clean pitch shift during the slowdown.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_in",  "Audio In",  "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Tape-stopped stereo output",
              PluginPortType::AudioStereo, PortRole::Output },
            
            { "trigger", "Trigger", "Rising edge starts the stop (0→1)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Toggle, 0.0f, 0.0f, 1.0f },
            
            { "stop_time", "Stop Time (s)", "Duration of the slowdown in seconds",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 2.0f, 0.1f, 10.0f },
            
            { "curve", "Curve", "Stop curve (0 = linear, 1 = exponential/natural)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f },
            
            { "auto_reset", "Auto Reset", "Return to full speed when stop completes",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Toggle, 1.0f, 0.0f, 1.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;

        for (int ch = 0; ch < 2; ++ch)
            buf_[ch].assign(BUF_SIZE, 0.0f);

        write_pos_ = GRAIN_OFFSET;
        read_pos_  = 0.0;
        grain_pos_ = 0.0;

        xfade_remaining_ = 0;
        xfade_total_     = 0;
        
        is_stopping_     = false;
        stop_progress_   = 0.0;
        last_trigger_    = 0.0f;
    }

    void deactivate() override {
        for (int ch = 0; ch < 2; ++ch)
            buf_[ch].clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        float trigger    = param(buffers, "trigger", 0.0f) > 0.5f ? 1.0f : 0.0f;
        float stop_time  = std::clamp(param(buffers, "stop_time", 2.0f), 0.1f, 10.0f);
        float curve      = std::clamp(param(buffers, "curve", 1.0f), 0.0f, 1.0f);
        float auto_reset = param(buffers, "auto_reset", 1.0f) > 0.5f ? 1.0f : 0.0f;

        // Detect trigger edge
        if (trigger > 0.5f && last_trigger_ < 0.5f) {
            // Rising edge: start or restart the stop
            is_stopping_   = true;
            stop_progress_ = 0.0;
        }
        last_trigger_ = trigger;

        double stop_increment = 1.0 / (stop_time * sample_rate_);

        for (int i = 0; i < ctx.block_size; ++i) {
            // Update stop progress
            if (is_stopping_) {
                stop_progress_ += stop_increment;
                if (stop_progress_ >= 1.0) {
                    stop_progress_ = 1.0;
                    is_stopping_ = false;
                    if (auto_reset > 0.5f) {
                        // Reset to full speed
                        stop_progress_ = 0.0;
                    }
                }
            }

            // Compute current playback ratio based on stop curve
            double ratio;
            if (stop_progress_ >= 1.0) {
                ratio = 0.0;  // Fully stopped
            } else {
                // Blend between linear (curve=0) and exponential (curve=1)
                double t = stop_progress_;
                double linear_ratio = 1.0 - t;
                double exp_ratio    = std::exp(-5.0 * t);  // Fast exponential decay
                ratio = linear_ratio * (1.0 - curve) + exp_ratio * curve;
            }

            // Write current sample
            buf_[0][write_pos_] = in->left[i];
            buf_[1][write_pos_] = in->right ? in->right[i] : in->left[i];

            // Drift check (same as pitch shifter)
            int read_int = static_cast<int>(read_pos_);
            int dist     = (write_pos_ - read_int + BUF_SIZE) & BUF_MASK;

            if (dist < MIN_DIST || dist > MAX_DIST) {
                if (xfade_remaining_ == 0) {
                    double frac    = read_pos_ - std::floor(read_pos_);
                    int    new_int = (write_pos_ - GRAIN_OFFSET + BUF_SIZE) & BUF_MASK;
                    grain_pos_       = static_cast<double>(new_int) + frac;
                    xfade_total_     = XFADE_SAMPLES;
                    xfade_remaining_ = XFADE_SAMPLES;
                }
            }

            // Read and crossfade
            float out_l, out_r;

            if (xfade_remaining_ > 0) {
                int   pos    = xfade_total_ - xfade_remaining_;
                float t      = static_cast<float>(pos) / static_cast<float>(xfade_total_);
                float w_out  = 0.5f * (1.0f + std::cos(static_cast<float>(M_PI) * t));
                float w_in   = 1.0f - w_out;

                out_l = read_cubic(0, read_pos_)  * w_out + read_cubic(0, grain_pos_) * w_in;
                out_r = read_cubic(1, read_pos_)  * w_out + read_cubic(1, grain_pos_) * w_in;

                grain_pos_ = wrap(grain_pos_ + ratio);
                --xfade_remaining_;

                if (xfade_remaining_ == 0)
                    read_pos_ = grain_pos_;
            } else {
                out_l = read_cubic(0, read_pos_);
                out_r = read_cubic(1, read_pos_);
            }

            out->left[i] = out_l;
            if (out->right) out->right[i] = out_r;

            // Advance pointers
            read_pos_  = wrap(read_pos_  + ratio);
            write_pos_ = (write_pos_ + 1) & BUF_MASK;
        }
    }

private:
    static double wrap(double pos) {
        if (pos >= BUF_SIZE) pos -= BUF_SIZE;
        if (pos < 0.0)       pos += BUF_SIZE;
        return pos;
    }

    float read_cubic(int ch, double pos) const {
        int   i1 = static_cast<int>(pos) & BUF_MASK;
        float t  = static_cast<float>(pos - std::floor(pos));

        float y0 = buf_[ch][(i1 - 1 + BUF_SIZE) & BUF_MASK];
        float y1 = buf_[ch][ i1];
        float y2 = buf_[ch][(i1 + 1) & BUF_MASK];
        float y3 = buf_[ch][(i1 + 2) & BUF_MASK];

        float a0 = -0.5f*y0 + 1.5f*y1 - 1.5f*y2 + 0.5f*y3;
        float a1 =       y0 - 2.5f*y1 + 2.0f*y2 - 0.5f*y3;
        float a2 = -0.5f*y0            + 0.5f*y2;
        float a3 =                 y1;

        return ((a0*t + a1)*t + a2)*t + a3;
    }

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    float  sample_rate_ = 44100.0f;

    std::vector<float> buf_[2];

    int    write_pos_        = 0;
    double read_pos_         = 0.0;
    double grain_pos_        = 0.0;

    int    xfade_remaining_  = 0;
    int    xfade_total_      = 0;
    
    bool   is_stopping_      = false;
    double stop_progress_    = 0.0;
    float  last_trigger_     = 0.0f;
};

REGISTER_PLUGIN(TapeStopPlugin);
REGISTER_PLUGIN_DYNAMIC(TapeStopPlugin);
