// pitch_shift_plugin.cpp
// Granular pitch shifter using drift-triggered crossfading.
//
// Algorithm:
//   - Audio is written into a circular delay buffer at sample rate.
//   - A read pointer advances at (write_rate * ratio), providing resampled
//     (pitch-shifted) output via cubic interpolation.
//   - When the read pointer drifts too far from the write pointer (either
//     too close, risking buffer overrun, or too far, increasing latency),
//     a crossfade to a new grain is triggered. The new grain's read pointer
//     is placed at (write_pos - GRAIN_OFFSET) so it starts reading recent
//     audio, then advances at the same ratio.
//   - The crossfade is a raised cosine over XFADE_SAMPLES, blending the
//     outgoing grain out and the incoming grain in. This is the only source
//     of artifact, and it only occurs when drift exceeds the threshold.
//
// At small shift amounts drift is slow and crossfades are rare, so output
// is essentially clean resampled audio. At large shifts crossfades are more
// frequent but the raised cosine keeps them inaudible at musical tempos.
//
// Stereo: both channels share read pointer positions to preserve imaging.
// Each channel has its own buffer but pointer arithmetic is identical.
//
// Parameters:
//   semitones — pitch shift [-24, +24]   default 0
//
// Latency: GRAIN_OFFSET samples (~93ms at 44100 Hz).

#include "plugin_api.h"
#include <cmath>
#include <vector>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

class PitchShiftPlugin final : public Plugin {
public:
    // Crossfade length in samples. ~23ms at 44100.
    static constexpr int XFADE_SAMPLES = 1024;

    // How far behind the write pointer the new grain starts reading.
    // Must be > XFADE_SAMPLES * max_ratio so the incoming grain doesn't
    // itself drift out of range before the crossfade completes.
    // 4096 @ 44100 = ~93ms. Fine for ratio up to ~2x (±12 semitones common
    // use) with headroom for ±24.
    static constexpr int GRAIN_OFFSET = 4096;

    // Drift window relative to write pointer (write_pos - read_pos, mod buf).
    // Trigger crossfade when read pointer falls outside [MIN_DIST, MAX_DIST].
    // MIN_DIST: safety margin so we never read ahead of write (~5ms).
    // MAX_DIST: caps maximum latency accumulation (~185ms).
    static constexpr int MIN_DIST = 256;
    static constexpr int MAX_DIST = 8192;

    // Power-of-2 buffer for fast modulo via bitmask.
    // 2^17 = 131072 samples ≈ 3 seconds at 44100 Hz.
    static constexpr int BUF_SIZE = 131072;
    static constexpr int BUF_MASK = BUF_SIZE - 1;

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.pitch_shift";
        d.display_name = "Pitch Shift";
        d.category     = "Effect";
        d.doc          = "Granular pitch shifter using drift-triggered crossfading. "
                         "Works on arbitrary audio including polyphonic and noisy signals. "
                         "Shift range: ±24 semitones. Artifacts (when they occur) are "
                         "a brief raised-cosine crossfade rather than phase noise.";
        d.author       = "builtin";
        d.version      = 3;

        d.ports = {
            { "audio_in",  "Audio In",  "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Pitch-shifted stereo output",
              PluginPortType::AudioStereo, PortRole::Output },
            { "semitones", "Pitch Shift (semitones)",
              "Pitch shift in semitones. 0 = no shift, ±12 = ±1 octave.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -24.0f, 24.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;

        for (int ch = 0; ch < 2; ++ch)
            buf_[ch].assign(BUF_SIZE, 0.f);

        write_pos_ = GRAIN_OFFSET;  // start write ahead of read by GRAIN_OFFSET
        read_pos_  = 0.0;
        grain_pos_ = 0.0;

        xfade_remaining_ = 0;
        xfade_total_     = 0;
    }

    void deactivate() override {
        for (int ch = 0; ch < 2; ++ch)
            buf_[ch].clear();
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        auto* semi_ctl = buffers.control.get("semitones");
        bool  ps_semi  = semi_ctl && semi_ctl->samples;
        float const_semi = std::clamp(semi_ctl ? semi_ctl->value : 0.f, -24.f, 24.f);
        double ratio     = std::pow(2.0, const_semi / 12.0);

        for (int i = 0; i < ctx.block_size; ++i) {
            // Per-sample semitones: recompute ratio on change
            if (ps_semi) {
                float s = std::clamp(semi_ctl->samples[i], -24.f, 24.f);
                if (s != last_semitones_) {
                    ratio = std::pow(2.0, s / 12.0);
                    last_semitones_ = s;
                }
            }
            // ----- Write current sample -----
            buf_[0][write_pos_] = in->left[i];
            buf_[1][write_pos_] = in->right ? in->right[i] : in->left[i];

            // ----- Drift check -----
            // dist = write_pos - read_pos (mod BUF_SIZE), i.e. how many
            // samples behind write the read pointer currently is.
            int read_int = static_cast<int>(read_pos_);
            int dist     = (write_pos_ - read_int + BUF_SIZE) & BUF_MASK;

            if (dist < MIN_DIST || dist > MAX_DIST) {
                if (xfade_remaining_ == 0) {
                    // Snap grain to GRAIN_OFFSET samples behind write,
                    // carrying over the fractional part of read_pos_ so
                    // the interpolation is continuous at the crossfade start.
                    double frac   = read_pos_ - std::floor(read_pos_);
                    int    new_int = (write_pos_ - GRAIN_OFFSET + BUF_SIZE) & BUF_MASK;
                    grain_pos_       = static_cast<double>(new_int) + frac;
                    xfade_total_     = XFADE_SAMPLES;
                    xfade_remaining_ = XFADE_SAMPLES;
                }
            }

            // ----- Read and crossfade -----
            float out_l, out_r;

            if (xfade_remaining_ > 0) {
                int   pos    = xfade_total_ - xfade_remaining_;
                float t      = static_cast<float>(pos) / static_cast<float>(xfade_total_);
                float w_out  = 0.5f * (1.f + std::cos(static_cast<float>(M_PI) * t));
                float w_in   = 1.f - w_out;

                out_l = read_cubic(0, read_pos_)  * w_out + read_cubic(0, grain_pos_) * w_in;
                out_r = read_cubic(1, read_pos_)  * w_out + read_cubic(1, grain_pos_) * w_in;

                grain_pos_ = wrap(grain_pos_ + ratio);
                --xfade_remaining_;

                if (xfade_remaining_ == 0)
                    read_pos_ = grain_pos_;  // grain takes over as main pointer
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

    // Catmull-Rom cubic interpolation from circular buffer ch at position pos.
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

    float  sample_rate_     = 44100.f;
    float  last_semitones_  = -999.f;   // for on-change ratio recomputation

    std::vector<float> buf_[2];

    int    write_pos_        = 0;
    double read_pos_         = 0.0;
    double grain_pos_        = 0.0;

    int    xfade_remaining_  = 0;
    int    xfade_total_      = 0;
};

REGISTER_PLUGIN(PitchShiftPlugin);
REGISTER_PLUGIN_DYNAMIC(PitchShiftPlugin);

std::unique_ptr<Plugin> make_pitch_shift_plugin() {
    return std::make_unique<PitchShiftPlugin>();
}
