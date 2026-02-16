// pitch_shift_plugin.cpp
// STFT phase vocoder pitch shifter.
//
// Preserves audio duration while shifting pitch by up to ±24 semitones.
//
// Fixes vs v1:
//   - Bin accumulation: input bins are summed into output bins (magnitude
//     and frequency weighted by magnitude), not winner-takes-all. This
//     eliminates the spectral holes that caused static at all shift values.
//   - Ratio smoothing: the pitch ratio is filtered through a one-pole LP
//     (~15ms time constant) before each frame. This prevents the discrete
//     phase jumps from control-rate parameter changes that caused warble
//     artifacts during modulation.
//   - Phase reseeding: when the smoothed ratio changes significantly between
//     frames (> ~0.3 semitones), the synthesis phase accumulators are
//     reseeded from the current analysis phases scaled by the new ratio,
//     preventing coherent phase errors from accumulating after a large shift.
//
// FFT size: 4096, hop: 1024 (75% overlap).
// Latency: one FFT frame (~93ms at 44100 Hz).
// The larger FFT gives C6 (~1047 Hz) ~97 bins of resolution vs ~49 at 2048,
// making sub-bin interpolation in the bin mapping much more effective.

#include "plugin_api.h"
#include <cmath>
#include <vector>
#include <algorithm>
#include <complex>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Cooley-Tukey DIT FFT (power-of-2, in-place, self-contained)
// ---------------------------------------------------------------------------
static void fft(std::vector<std::complex<float>>& x, bool inverse) {
    int N = static_cast<int>(x.size());
    for (int i = 1, j = 0; i < N; ++i) {
        int bit = N >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(x[i], x[j]);
    }
    for (int len = 2; len <= N; len <<= 1) {
        float ang = 2.0f * static_cast<float>(M_PI) / len * (inverse ? 1.f : -1.f);
        std::complex<float> wlen(std::cos(ang), std::sin(ang));
        for (int i = 0; i < N; i += len) {
            std::complex<float> w(1.f, 0.f);
            for (int j = 0; j < len / 2; ++j) {
                auto u = x[i + j], v = x[i + j + len/2] * w;
                x[i + j]         = u + v;
                x[i + j + len/2] = u - v;
                w *= wlen;
            }
        }
    }
    if (inverse) {
        float inv = 1.f / static_cast<float>(N);
        for (auto& v : x) v *= inv;
    }
}

static float wrap_phase(float p) {
    const float pi = static_cast<float>(M_PI);
    // Branchless-ish: faster than a loop for small deviations
    p -= 2.f * pi * std::floor((p + pi) / (2.f * pi));
    return p;
}

// ---------------------------------------------------------------------------

class PitchShiftPlugin final : public Plugin {
public:
    static constexpr int FFT_SIZE = 4096;
    static constexpr int HOP_SIZE = 1024;
    static constexpr int HALF_FFT = FFT_SIZE / 2 + 1;
    static constexpr int OVERLAP  = FFT_SIZE / HOP_SIZE;  // 4

    // Ratio change threshold (in ratio space) beyond which we reseed phases.
    // 2^(0.3/12) - 1 ≈ 0.0174 — roughly 0.3 semitones.
    static constexpr float RESEED_THRESHOLD = 0.0174f;

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.pitch_shift";
        d.display_name = "Pitch Shift";
        d.category     = "Effect";
        d.doc          = "STFT phase vocoder pitch shifter. "
                         "Preserves audio duration. Shift range: ±24 semitones. "
                         "Latency: one FFT frame (~93ms at 44100 Hz). "
                         "Dynamic modulation supported with ratio smoothing.";
        d.author       = "builtin";
        d.version      = 3;

        d.ports = {
            { "audio_in",  "Audio In",  "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Pitch-shifted stereo output",
              PluginPortType::AudioStereo, PortRole::Output },
            { "semitones", "Pitch Shift (semitones)",
              "Pitch shift in semitones. 0 = no shift, ±12 = ±1 octave. "
              "Accepts Control input for vibrato/warble effects.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -24.0f, 24.0f },
        };

        return d;
    }

    void activate(float sample_rate, int max_block_size) override {
        sample_rate_ = sample_rate;

        // Hann window
        hann_.resize(FFT_SIZE);
        for (int i = 0; i < FFT_SIZE; ++i)
            hann_[i] = 0.5f * (1.f - std::cos(2.f * static_cast<float>(M_PI) * i / FFT_SIZE));

        // OLA normalisation for Hann² with 75% overlap: sum of Hann² = 3/8 per sample,
        // summed over OVERLAP frames = OVERLAP * 3/8.
        ola_norm_ = 1.0f / (OVERLAP * 0.375f);

        // Smoothing coefficient: one-pole LP with ~15ms time constant at hop rate.
        // hops_per_second = sample_rate / HOP_SIZE  (at 44100/1024 ≈ 43 hops/sec)
        // alpha = exp(-1 / (hops_per_sec * 0.015))
        // Recomputed from sample_rate so it's correct at any SR.
        float hops_per_sec = sample_rate / static_cast<float>(HOP_SIZE);
        smooth_alpha_ = std::exp(-1.0f / (hops_per_sec * 0.015f));

        int out_buf_size = FFT_SIZE + max_block_size + HOP_SIZE;
        for (int ch = 0; ch < 2; ++ch) {
            in_buf_[ch].assign(FFT_SIZE, 0.f);
            out_buf_[ch].assign(out_buf_size, 0.f);
            phase_accum_[ch].assign(HALF_FFT, 0.f);
            prev_phase_in_[ch].assign(HALF_FFT, 0.f);
        }

        in_write_pos_      = 0;
        out_read_pos_      = 0;
        samples_until_hop_ = HOP_SIZE;
        smoothed_ratio_    = 1.0f;
        prev_ratio_        = 1.0f;
    }

    void deactivate() override {
        for (int ch = 0; ch < 2; ++ch) {
            in_buf_[ch].clear();
            out_buf_[ch].clear();
            phase_accum_[ch].clear();
            prev_phase_in_[ch].clear();
        }
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        float semitones = std::clamp(param(buffers, "semitones", 0.f), -24.f, 24.f);
        // Target ratio — updated once per block (control rate), then smoothed per hop
        target_ratio_ = std::pow(2.f, semitones / 12.f);

        int n = ctx.block_size;

        for (int i = 0; i < n; ++i) {
            float l = in->left[i];
            float r = in->right ? in->right[i] : l;
            in_buf_[0][in_write_pos_] = l;
            in_buf_[1][in_write_pos_] = r;
            in_write_pos_ = (in_write_pos_ + 1) % FFT_SIZE;

            if (--samples_until_hop_ == 0) {
                // Advance smoothed ratio one hop step toward target
                smoothed_ratio_ = smooth_alpha_ * smoothed_ratio_
                                + (1.f - smooth_alpha_) * target_ratio_;
                process_frame(smoothed_ratio_);
                prev_ratio_ = smoothed_ratio_;
                samples_until_hop_ = HOP_SIZE;
            }
        }

        int out_buf_size = static_cast<int>(out_buf_[0].size());
        for (int i = 0; i < n; ++i) {
            out->left[i] = out_buf_[0][out_read_pos_] * ola_norm_;
            out_buf_[0][out_read_pos_] = 0.f;
            if (out->right) {
                out->right[i] = out_buf_[1][out_read_pos_] * ola_norm_;
                out_buf_[1][out_read_pos_] = 0.f;
            }
            out_read_pos_ = (out_read_pos_ + 1) % out_buf_size;
        }
    }

private:
    void process_frame(float ratio) {
        const float hop_f  = static_cast<float>(HOP_SIZE);
        const float fft_f  = static_cast<float>(FFT_SIZE);
        const float two_pi = 2.f * static_cast<float>(M_PI);

        // Has the ratio shifted enough to warrant a phase reseed?
        bool reseed = std::abs(ratio - prev_ratio_) > RESEED_THRESHOLD;

        for (int ch = 0; ch < 2; ++ch) {
            // ----- 1. Windowed analysis -----
            std::vector<std::complex<float>> frame(FFT_SIZE, {0.f, 0.f});
            for (int i = 0; i < FFT_SIZE; ++i) {
                int idx = (in_write_pos_ - FFT_SIZE + i + FFT_SIZE) % FFT_SIZE;
                frame[i] = {in_buf_[ch][idx] * hann_[i], 0.f};
            }
            fft(frame, false);

            // ----- 2. Phase vocoder: true instantaneous frequency per bin -----
            std::vector<float> magnitude(HALF_FFT);
            std::vector<float> true_freq(HALF_FFT);

            for (int k = 0; k < HALF_FFT; ++k) {
                float mag   = std::abs(frame[k]);
                float phase = std::arg(frame[k]);

                float dp       = phase - prev_phase_in_[ch][k];
                prev_phase_in_[ch][k] = phase;

                float expected   = two_pi * static_cast<float>(k) * hop_f / fft_f;
                float deviation  = wrap_phase(dp - expected);
                true_freq[k]     = (expected + deviation) / hop_f;
                magnitude[k]     = mag;
            }

            // ----- 3. Pitch-shift: interpolated bin mapping -----
            // k * ratio is rarely an integer, so instead of rounding to the
            // nearest output bin we split each input bin's contribution across
            // the two bracketing output bins weighted by fractional proximity.
            // This eliminates the spectral gaps that cause crackle on high
            // frequencies (where bin spacing is large relative to the shift).
            //
            // Magnitude is split linearly; frequency is weighted by the same
            // fractions so phase velocity tracks the dominant partial correctly.
            std::vector<float> synth_mag(HALF_FFT, 0.f);
            std::vector<float> synth_freq(HALF_FFT, 0.f);
            std::vector<float> synth_weight(HALF_FFT, 0.f);

            for (int k = 0; k < HALF_FFT; ++k) {
                float k2f  = static_cast<float>(k) * ratio;
                int   k2lo = static_cast<int>(std::floor(k2f));
                int   k2hi = k2lo + 1;
                float frac = k2f - static_cast<float>(k2lo);  // weight for hi bin

                float mag  = magnitude[k];
                float freq = true_freq[k] * ratio;

                if (k2lo >= 0 && k2lo < HALF_FFT) {
                    float w = mag * (1.f - frac);
                    synth_mag[k2lo]    += w;
                    synth_freq[k2lo]   += freq * w;
                    synth_weight[k2lo] += w;
                }
                if (k2hi >= 0 && k2hi < HALF_FFT) {
                    float w = mag * frac;
                    synth_mag[k2hi]    += w;
                    synth_freq[k2hi]   += freq * w;
                    synth_weight[k2hi] += w;
                }
            }

            // Normalise accumulated frequencies
            for (int k = 0; k < HALF_FFT; ++k) {
                if (synth_weight[k] > 1e-8f)
                    synth_freq[k] /= synth_weight[k];
            }

            // ----- 4. Phase synthesis -----
            // On a reseed, reinitialise accumulators from current analysis phases
            // scaled to the new output-bin positions.
            if (reseed) {
                for (int k = 0; k < HALF_FFT; ++k) {
                    int k_src = static_cast<int>(std::round(static_cast<float>(k) / ratio));
                    k_src = std::clamp(k_src, 0, HALF_FFT - 1);
                    phase_accum_[ch][k] = std::arg(frame[k_src]);
                }
            }

            std::vector<std::complex<float>> synth_frame(FFT_SIZE, {0.f, 0.f});
            for (int k = 0; k < HALF_FFT; ++k) {
                phase_accum_[ch][k] += synth_freq[k] * hop_f;
                synth_frame[k] = std::polar(synth_mag[k], phase_accum_[ch][k]);
            }

            // Hermitian symmetry for real IFFT output
            for (int k = 1; k < HALF_FFT - 1; ++k)
                synth_frame[FFT_SIZE - k] = std::conj(synth_frame[k]);

            // ----- 5. ISTFT + OLA -----
            fft(synth_frame, true);

            int out_buf_size = static_cast<int>(out_buf_[ch].size());
            int write_start  = (out_read_pos_ + FFT_SIZE - HOP_SIZE) % out_buf_size;
            for (int i = 0; i < FFT_SIZE; ++i) {
                int pos = (write_start + i) % out_buf_size;
                out_buf_[ch][pos] += synth_frame[i].real() * hann_[i];
            }
        }
    }

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    float sample_rate_  = 44100.f;
    float ola_norm_     = 1.f;
    float smooth_alpha_ = 0.9f;
    float smoothed_ratio_ = 1.0f;
    float prev_ratio_     = 1.0f;
    float target_ratio_   = 1.0f;

    std::vector<float> hann_;

    std::vector<float> in_buf_[2];
    std::vector<float> out_buf_[2];
    std::vector<float> phase_accum_[2];
    std::vector<float> prev_phase_in_[2];

    int in_write_pos_      = 0;
    int out_read_pos_      = 0;
    int samples_until_hop_ = HOP_SIZE;
};

REGISTER_PLUGIN(PitchShiftPlugin);
REGISTER_PLUGIN_DYNAMIC(PitchShiftPlugin);

std::unique_ptr<Plugin> make_pitch_shift_plugin() {
    return std::make_unique<PitchShiftPlugin>();
}
