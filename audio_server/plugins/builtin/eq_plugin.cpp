// eq_plugin.cpp
// 4-band parametric EQ with peak meter.
//
// Band layout:
//   Band 1 — low shelf   (default 100 Hz)
//   Band 2 — peaking     (default 400 Hz)
//   Band 3 — peaking     (default 2 kHz)
//   Band 4 — high shelf  (default 8 kHz)
//
// Coefficients follow the Audio EQ Cookbook (RBJ). Coefficients are
// recomputed only when a band's params change to keep the per-sample
// loop cheap.
//
// A `peak_out_db` Monitor port exposes the running peak of the output
// for a live meter in the UI (traditional EQ clip indicator).

#include "plugin_api.h"
#include <algorithm>
#include <atomic>
#include <array>
#include <cmath>

namespace {
constexpr int   NUM_BANDS = 4;
constexpr float TAU       = 6.28318530717958647692f;

struct BiquadCoefs {
    float b0 = 1.0f, b1 = 0.0f, b2 = 0.0f, a1 = 0.0f, a2 = 0.0f;
};

struct BiquadState {
    float z1 = 0.0f, z2 = 0.0f;
    float process(const BiquadCoefs& c, float x) {
        // Transposed Direct Form II.
        float y = c.b0 * x + z1;
        z1 = c.b1 * x - c.a1 * y + z2;
        z2 = c.b2 * x - c.a2 * y;
        return y;
    }
};

enum class BandKind { LowShelf, Peak, HighShelf };

BiquadCoefs design_band(BandKind kind, float freq, float gain_db, float q, float sr) {
    BiquadCoefs c;
    float A     = std::pow(10.0f, gain_db / 40.0f);       // sqrt of linear gain
    float w0    = TAU * std::max(1.0f, std::min(freq, sr * 0.49f)) / sr;
    float cw    = std::cos(w0);
    float sw    = std::sin(w0);
    float alpha = sw / (2.0f * std::max(0.1f, q));

    float b0, b1, b2, a0, a1, a2;
    switch (kind) {
    case BandKind::Peak: {
        b0 = 1.0f + alpha * A;
        b1 = -2.0f * cw;
        b2 = 1.0f - alpha * A;
        a0 = 1.0f + alpha / A;
        a1 = -2.0f * cw;
        a2 = 1.0f - alpha / A;
        break;
    }
    case BandKind::LowShelf: {
        float sqA2alpha = 2.0f * std::sqrt(A) * alpha;
        b0 = A * ((A + 1.0f) - (A - 1.0f) * cw + sqA2alpha);
        b1 = 2.0f * A * ((A - 1.0f) - (A + 1.0f) * cw);
        b2 = A * ((A + 1.0f) - (A - 1.0f) * cw - sqA2alpha);
        a0 = (A + 1.0f) + (A - 1.0f) * cw + sqA2alpha;
        a1 = -2.0f * ((A - 1.0f) + (A + 1.0f) * cw);
        a2 = (A + 1.0f) + (A - 1.0f) * cw - sqA2alpha;
        break;
    }
    case BandKind::HighShelf: {
        float sqA2alpha = 2.0f * std::sqrt(A) * alpha;
        b0 = A * ((A + 1.0f) + (A - 1.0f) * cw + sqA2alpha);
        b1 = -2.0f * A * ((A - 1.0f) + (A + 1.0f) * cw);
        b2 = A * ((A + 1.0f) + (A - 1.0f) * cw - sqA2alpha);
        a0 = (A + 1.0f) - (A - 1.0f) * cw + sqA2alpha;
        a1 = 2.0f * ((A - 1.0f) - (A + 1.0f) * cw);
        a2 = (A + 1.0f) - (A - 1.0f) * cw - sqA2alpha;
        break;
    }
    }
    float inv_a0 = 1.0f / a0;
    c.b0 = b0 * inv_a0;
    c.b1 = b1 * inv_a0;
    c.b2 = b2 * inv_a0;
    c.a1 = a1 * inv_a0;
    c.a2 = a2 * inv_a0;
    return c;
}

constexpr std::array<BandKind, NUM_BANDS> BAND_KINDS = {
    BandKind::LowShelf, BandKind::Peak, BandKind::Peak, BandKind::HighShelf,
};

constexpr std::array<float, NUM_BANDS> DEFAULT_FREQS = { 100.0f, 400.0f, 2000.0f, 8000.0f };

const char* BAND_LABELS[NUM_BANDS] = { "Low Shelf", "Low Mid", "High Mid", "High Shelf" };
}  // namespace


class EQPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.eq";
        d.display_name = "EQ";
        d.category     = "Effect";
        d.doc          = "4-band parametric EQ (low shelf, two peaks, high shelf) with "
                         "a peak output meter. Each band has frequency, gain, and Q "
                         "controls; coefficients follow the Audio EQ Cookbook.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_in",  "Audio In",  "Stereo audio input",
              PluginPortType::AudioStereo, PortRole::Input },
            { "audio_out", "Audio Out", "Equalised stereo output",
              PluginPortType::AudioStereo, PortRole::Output },
        };

        for (int b = 0; b < NUM_BANDS; ++b) {
            std::string sfx = "_" + std::to_string(b + 1);
            std::string label = std::string(BAND_LABELS[b]);

            float min_f = (b == 0) ? 20.0f : 40.0f;
            float max_f = (b == NUM_BANDS - 1) ? 20000.0f : 12000.0f;

            d.ports.push_back({
                "freq" + sfx, label + " Freq",
                "Centre / corner frequency in Hz",
                PluginPortType::Control, PortRole::Input,
                ControlHint::Continuous, DEFAULT_FREQS[b], min_f, max_f,
            });
            d.ports.push_back({
                "gain" + sfx, label + " Gain (dB)",
                "Band gain in decibels.",
                PluginPortType::Control, PortRole::Input,
                ControlHint::Continuous, 0.0f, -24.0f, 24.0f,
            });
            d.ports.push_back({
                "q" + sfx, label + " Q",
                "Filter Q / resonance.",
                PluginPortType::Control, PortRole::Input,
                ControlHint::Continuous, 0.707f, 0.1f, 10.0f,
            });
        }

        d.ports.push_back({
            "peak_out_db", "Peak Out (dBFS)",
            "Peak output level in dBFS — traditional clipping gauge.",
            PluginPortType::Control, PortRole::Monitor,
            ControlHint::Meter, -60.0f, -60.0f, 6.0f,
        });

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        for (int b = 0; b < NUM_BANDS; ++b) {
            for (int ch = 0; ch < 2; ++ch) state_[b][ch] = BiquadState{};
            cur_freq_[b] = DEFAULT_FREQS[b];
            cur_gain_[b] = 0.0f;
            cur_q_[b] = 0.707f;
            coefs_[b] = design_band(BAND_KINDS[b], cur_freq_[b], cur_gain_[b],
                                    cur_q_[b], sample_rate_);
        }
        peak_db_.store(-60.0f, std::memory_order_relaxed);
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* in  = buffers.audio.get("audio_in");
        auto* out = buffers.audio.get("audio_out");
        if (!in || !out) return;

        // Refresh coefficients for any band whose params changed this block.
        for (int b = 0; b < NUM_BANDS; ++b) {
            float f = param(buffers, ("freq_" + std::to_string(b + 1)).c_str(),
                            DEFAULT_FREQS[b]);
            float g = param(buffers, ("gain_" + std::to_string(b + 1)).c_str(), 0.0f);
            float q = param(buffers, ("q_"    + std::to_string(b + 1)).c_str(), 0.707f);
            if (f != cur_freq_[b] || g != cur_gain_[b] || q != cur_q_[b]) {
                cur_freq_[b] = f;
                cur_gain_[b] = g;
                cur_q_[b]    = q;
                coefs_[b] = design_band(BAND_KINDS[b], f, g, q, sample_rate_);
            }
        }

        float peak = 0.0f;

        for (int i = 0; i < ctx.block_size; ++i) {
            float l = in->left[i];
            float r = in->right ? in->right[i] : l;

            for (int b = 0; b < NUM_BANDS; ++b) {
                l = state_[b][0].process(coefs_[b], l);
                r = state_[b][1].process(coefs_[b], r);
            }

            out->left[i] = l;
            if (out->right) out->right[i] = r;

            float ab = std::max(std::fabs(l), std::fabs(r));
            if (ab > peak) peak = ab;
        }

        // Peak → dBFS, then decay toward -60 so the meter doesn't stick.
        float new_peak_db = peak > 1e-5f ? 20.0f * std::log10(peak) : -60.0f;
        float prev = peak_db_.load(std::memory_order_relaxed);
        // Attack: jump instantly to the new peak; release: slow decay (~20 dB/s).
        float decay_per_block = 20.0f * (float(ctx.block_size) / sample_rate_);
        float smoothed = std::max(new_peak_db, prev - decay_per_block);
        smoothed = std::clamp(smoothed, -60.0f, 6.0f);
        peak_db_.store(smoothed, std::memory_order_relaxed);

        if (auto* mon = buffers.control.get("peak_out_db")) {
            mon->value = smoothed;
            if (mon->samples) {
                for (int i = 0; i < ctx.block_size; ++i) mon->samples[i] = smoothed;
                mon->samples_written = true;
            }
        }
    }

    float read_monitor(const std::string& port_id) override {
        if (port_id == "peak_out_db")
            return peak_db_.load(std::memory_order_relaxed);
        return 0.0f;
    }

private:
    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    float sample_rate_ = 44100.0f;
    BiquadCoefs coefs_[NUM_BANDS];
    BiquadState state_[NUM_BANDS][2];
    float cur_freq_[NUM_BANDS] {};
    float cur_gain_[NUM_BANDS] {};
    float cur_q_[NUM_BANDS]    {};

    std::atomic<float> peak_db_{-60.0f};
};

REGISTER_PLUGIN(EQPlugin);
REGISTER_PLUGIN_DYNAMIC(EQPlugin);
