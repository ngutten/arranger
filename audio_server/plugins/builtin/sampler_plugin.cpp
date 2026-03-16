// sampler_plugin.cpp
// Sample-based synthesizer with multiple pitch-shifting algorithms.
//
// Pitch shifting modes (configurable via pitch_mode):
//   Spectral (default) – STFT bin relocation + phase accumulation + OLA.
//                         Works for all content, preserves duration.
//   WSOLA              – Waveform Similarity Overlap-Add (time-stretch + resample).
//                         Good general-purpose, preserves duration.
//   PSOLA              – TD-PSOLA with skip-repeats fix. Requires pitched content
//                         (falls back to varrate if F0 not detected).
//   Varrate            – Simple variable-rate playback. Changes duration with pitch.
//
// Polyphony: up to MAX_VOICES simultaneous voices (oldest stolen if exceeded).
// Envelope: simple linear ADSR per voice (applied per-sample for all modes).
//
// Supported formats (via libsndfile): WAV, AIFF, OGG, FLAC.
//
// Config params:
//   sample_path  – path to the audio file (string)
//   pitch_mode   – pitch shifting algorithm (spectral/wsola/psola/varrate)
//
// Runtime ports:
//   audio_out    – stereo audio output
//   gain         – output gain control [0..2], default 1.0
//   root_note    – MIDI note of the unshifted sample (default 60 = C4)
//   attack/decay/sustain/release – ADSR envelope

#include "plugin_api.h"
#include "adsr.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#ifdef AS_ENABLE_SNDFILE
#  include <sndfile.h>
#endif

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static constexpr int MAX_VOICES = 32;

// ---------------------------------------------------------------------------
// Spectral pitch-shift constants
// ---------------------------------------------------------------------------
static constexpr int SPECT_FFT_SIZE = 2048;
static constexpr int SPECT_N_BINS   = SPECT_FFT_SIZE / 2 + 1;  // 1025
static constexpr int SPECT_HOP      = 512;  // 4x overlap

// ---------------------------------------------------------------------------
// WSOLA constants
// ---------------------------------------------------------------------------
static constexpr int WSOLA_GRAIN_MS     = 60;    // grain size in ms
static constexpr int WSOLA_SEARCH_MS    = 5;     // cross-correlation search ±ms

// ---------------------------------------------------------------------------
// Minimal radix-2 Cooley-Tukey FFT (in-place, complex interleaved)
// ---------------------------------------------------------------------------
static void fft_complex_inplace(float* data, int N, bool inverse) {
    // Bit-reversal permutation
    for (int i = 1, j = 0; i < N; ++i) {
        int bit = N >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            std::swap(data[2*i],   data[2*j]);
            std::swap(data[2*i+1], data[2*j+1]);
        }
    }
    float sign = inverse ? 1.0f : -1.0f;
    for (int len = 2; len <= N; len <<= 1) {
        float ang = sign * 2.0f * static_cast<float>(M_PI) / len;
        float wR = std::cos(ang), wI = std::sin(ang);
        for (int i = 0; i < N; i += len) {
            float curR = 1.0f, curI = 0.0f;
            for (int j = 0; j < len / 2; ++j) {
                int u = i + j, v = u + len / 2;
                float tR = curR * data[2*v] - curI * data[2*v+1];
                float tI = curR * data[2*v+1] + curI * data[2*v];
                data[2*v]   = data[2*u]   - tR;
                data[2*v+1] = data[2*u+1] - tI;
                data[2*u]   += tR;
                data[2*u+1] += tI;
                float nR = curR * wR - curI * wI;
                curI = curR * wI + curI * wR;
                curR = nR;
            }
        }
    }
    if (inverse) {
        float inv = 1.0f / static_cast<float>(N);
        for (int i = 0; i < 2 * N; ++i) data[i] *= inv;
    }
}

// IRFFT: magnitude + phase arrays → real output.
// scratch must be at least 2*N floats.
static void irfft_mag_phase(const float* mag, const float* phase,
                            float* output, float* scratch, int N) {
    int n_bins = N / 2 + 1;
    std::memset(scratch, 0, 2 * N * sizeof(float));
    for (int k = 0; k < n_bins; ++k) {
        scratch[2*k]   = mag[k] * std::cos(phase[k]);
        scratch[2*k+1] = mag[k] * std::sin(phase[k]);
    }
    // Hermitian mirror for k > N/2
    for (int k = 1; k < N / 2; ++k) {
        scratch[2*(N-k)]   =  scratch[2*k];
        scratch[2*(N-k)+1] = -scratch[2*k+1];
    }
    fft_complex_inplace(scratch, N, true);
    for (int i = 0; i < N; ++i) output[i] = scratch[2*i];
}

// ---------------------------------------------------------------------------
// YIN F0 detection (float version, for engine-rate samples)
// ---------------------------------------------------------------------------

static float yin_detect_f0_float(const float* pcm, int len, int sample_rate) {
    if (len < 256) return 0.0f;

    constexpr float SILENCE_THRESH = 0.01f;
    int start = 0, end = len;
    while (start < len && std::abs(pcm[start]) < SILENCE_THRESH) ++start;
    while (end > start && std::abs(pcm[end - 1]) < SILENCE_THRESH) --end;

    int voiced_len = end - start;
    if (voiced_len < 256) return 0.0f;
    int quarter = voiced_len / 4;
    int win_start = start + quarter;
    int win_len   = voiced_len - 2 * quarter;

    int min_lag = std::max(1, sample_rate / 500);
    int max_lag = std::min(sample_rate / 60, win_len / 2);
    if (max_lag <= min_lag) return 0.0f;

    std::vector<float> d(max_lag + 1, 0.0f);
    int n = win_len - max_lag;
    for (int tau = 1; tau <= max_lag; ++tau) {
        float sum = 0.0f;
        for (int i = 0; i < n; ++i) {
            float diff = pcm[win_start + i] - pcm[win_start + i + tau];
            sum += diff * diff;
        }
        d[tau] = sum;
    }

    std::vector<float> dn(max_lag + 1, 1.0f);
    float running = 0.0f;
    for (int tau = 1; tau <= max_lag; ++tau) {
        running += d[tau];
        dn[tau] = (running > 0.0f) ? d[tau] * tau / running : 1.0f;
    }

    constexpr float YIN_THRESHOLD = 0.15f;
    int best_tau = 0;
    for (int tau = min_lag; tau <= max_lag; ++tau) {
        if (dn[tau] < YIN_THRESHOLD) {
            while (tau + 1 <= max_lag && dn[tau + 1] < dn[tau]) ++tau;
            best_tau = tau;
            break;
        }
    }

    if (best_tau == 0) {
        float best_val = 1e30f;
        for (int tau = min_lag; tau <= max_lag; ++tau) {
            if (dn[tau] < best_val) { best_val = dn[tau]; best_tau = tau; }
        }
        if (best_val > 0.5f) return 0.0f;
    }

    float refined_tau = static_cast<float>(best_tau);
    if (best_tau > 1 && best_tau < max_lag) {
        float a = dn[best_tau - 1];
        float b = dn[best_tau];
        float c = dn[best_tau + 1];
        float denom = 2.0f * (2.0f * b - a - c);
        if (std::abs(denom) > 1e-10f)
            refined_tau += (a - c) / denom;
    }

    return (refined_tau > 0.0f) ? sample_rate / refined_tau : 0.0f;
}

// ---------------------------------------------------------------------------
// Pitch mark placement (float version)
// ---------------------------------------------------------------------------

static std::vector<int> find_pitch_marks_float(const float* pcm, int len,
                                                float f0, int sr) {
    std::vector<int> marks;
    if (f0 < 1.0f || len < 4) return marks;

    int period = static_cast<int>(std::round(static_cast<float>(sr) / f0));
    if (period < 2) return marks;

    int check_len = std::min(len, period * 4);
    double pos_sum = 0, neg_sum = 0;
    for (int i = 0; i < check_len; ++i) {
        if (pcm[i] > 0) pos_sum += pcm[i];
        else             neg_sum -= pcm[i];
    }
    float sign = (pos_sum >= neg_sum) ? 1.0f : -1.0f;

    int search_end = std::min(period, len);
    int best_pos = 0;
    float best_val = 0.0f;
    for (int i = 0; i < search_end; ++i) {
        float v = pcm[i] * sign;
        if (v > best_val) { best_val = v; best_pos = i; }
    }
    marks.push_back(best_pos);

    while (true) {
        int expected = marks.back() + period;
        if (expected >= len) break;

        int margin = period * 15 / 100;
        int lo = std::max(0, expected - margin);
        int hi = std::min(len - 1, expected + margin);

        best_pos = expected;
        best_val = 0.0f;
        for (int i = lo; i <= hi; ++i) {
            float v = pcm[i] * sign;
            if (v > best_val) { best_val = v; best_pos = i; }
        }
        marks.push_back(best_pos);
    }

    return marks;
}

// ---------------------------------------------------------------------------
// Pitch mode enum
// ---------------------------------------------------------------------------

enum class PitchMode { Spectral = 0, WSOLA = 1, PSOLA = 2, Varrate = 3 };

// ---------------------------------------------------------------------------
// Precomputed spectral bank (computed at sample load time)
// ---------------------------------------------------------------------------

struct SpectralBank {
    int n_frames = 0;
    // Frame-major: [frame * SPECT_N_BINS + bin]
    std::vector<float> magnitudes;
    std::vector<float> inst_freqs;
    float bin_width  = 0.0f;
    float ola_norm[SPECT_HOP] = {};
    float window[SPECT_FFT_SIZE] = {};
};

// ---------------------------------------------------------------------------
// Voice
// ---------------------------------------------------------------------------

struct Voice {
    bool     active   = false;
    int      channel  = 0;
    int      pitch    = 0;
    float    vel_gain = 1.0f;

    // ADSR (shared implementation from adsr.h)
    ADSREnvelope env;

    PitchMode mode = PitchMode::Spectral;

    // Variable-rate playback
    double   pos      = 0.0;
    double   rate     = 1.0;

    // PSOLA state
    double   synth_time     = 0.0;
    double   target_period  = 0.0;
    double   analysis_time  = 0.0;
    double   analysis_period = 0.0;
    double   output_time    = 0.0;
    int      last_mark_idx  = -1;   // for skip-repeats

    static constexpr int MAX_OVERLAP = 4096;
    float    overlap[MAX_OVERLAP] = {};
    int      overlap_len = 0;

    // Spectral mode state
    float    phase_acc[SPECT_N_BINS] = {};
    float    ola_buf[SPECT_FFT_SIZE] = {};
    float    spect_out_buf[SPECT_HOP] = {};
    int      spect_out_avail = 0;
    int      spect_out_read  = 0;
    double   spect_frame_pos = 0.0;
    float    irfft_scratch[2 * SPECT_FFT_SIZE] = {};

    // WSOLA state
    //   wsola_ts_buf: circular time-stretched output buffer
    //   Source is read at rate 1.0 (preserving time), grains overlap-added.
    //   Output is resampled from ts_buf at v.rate to shift pitch.
    static constexpr int WSOLA_BUF_SIZE = 16384;
    float    wsola_ts_buf[WSOLA_BUF_SIZE] = {};
    int      wsola_ts_write = 0;     // write position in ts_buf
    double   wsola_ts_read  = 0.0;   // fractional read position in ts_buf
    double   wsola_src_pos  = 0.0;   // read position in source sample
    int      wsola_grain_size = 0;
    int      wsola_hop       = 0;
    int      wsola_search    = 0;
    int      wsola_produced  = 0;    // total samples produced into ts_buf
};

// ---------------------------------------------------------------------------
// SamplerPlugin
// ---------------------------------------------------------------------------

class SamplerPlugin final : public Plugin {
public:
    ~SamplerPlugin() override = default;

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.sampler";
        d.display_name = "Sampler";
        d.category     = "Synth";
        d.doc =
            "Sample-based synthesizer with multiple pitch-shifting algorithms. "
            "Spectral (default) works for all content and preserves duration. "
            "WSOLA is a waveform-based alternative. PSOLA uses pitch detection. "
            "Varrate is simple resampling (changes duration). "
            "Supports WAV, AIFF, OGG, FLAC (via libsndfile).";
        d.author  = "builtin";
        d.version = 3;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain", "Output gain multiplier. 1.0 = unity.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f },
            { "root_note", "Root Note",
              "MIDI note number played at original pitch (0-127). "
              "Auto-detected from sample when possible.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 60.0f, 0.0f, 127.0f, 1.0f,
              {}, "", false },
            { "attack",  "Attack (s)",  "Envelope attack time in seconds.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.01f, 0.0f, 4.0f },
            { "decay",   "Decay (s)",   "Envelope decay time in seconds.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.0f, 4.0f },
            { "sustain", "Sustain",     "Envelope sustain level (0..1).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.8f, 0.0f, 1.0f },
            { "release", "Release (s)", "Envelope release time in seconds.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.2f, 0.0f, 4.0f },
        };

        d.config_params = {
            { "sample_path", "Sample File",
              "Path to the audio file to load (WAV, AIFF, OGG, FLAC).",
              ConfigType::FilePath, "",
              "Audio Files (*.wav *.aiff *.aif *.ogg *.flac *.W64 *.w64);;All Files (*)" },
            { "pitch_mode", "Pitch Mode",
              "Algorithm for pitch shifting. Spectral works for all content and "
              "preserves duration. "
              "PSOLA uses pitch detection (falls back to Varrate for unpitched). "
              "Varrate is simple resampling (changes duration with pitch).",
              ConfigType::Categorical, "spectral", "",
              false, false,
              { "spectral", "psola", "varrate" } },
        };

        return d;
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "sample_path") {
            pending_path_ = value;
            path_dirty_.store(true, std::memory_order_release);
        } else if (key == "pitch_mode") {
            if (value == "spectral")       pitch_mode_ = PitchMode::Spectral;
            else if (value == "wsola")     pitch_mode_ = PitchMode::WSOLA;
            else if (value == "psola")     pitch_mode_ = PitchMode::PSOLA;
            else if (value == "varrate")   pitch_mode_ = PitchMode::Varrate;
        }
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        for (auto& v : voices_) v = Voice{};
        if (path_dirty_.load()) {
            _load_sample(pending_path_);
            path_dirty_.store(false);
        }
    }

    void deactivate() override {
        sample_L_.clear();
        sample_R_.clear();
        sample_frames_ = 0;
        pitch_marks_.clear();
        detected_f0_ = 0.0f;
        spectral_bank_ = SpectralBank{};
    }

    // -----------------------------------------------------------------------
    // MIDI events (audio thread)
    // -----------------------------------------------------------------------
    void note_on(int channel, int pitch, int velocity) override {
        if (sample_frames_ == 0) return;
        if (velocity == 0) { note_off(channel, pitch); return; }

        if (path_dirty_.load(std::memory_order_acquire)) {
            _load_sample(pending_path_);
            path_dirty_.store(false, std::memory_order_release);
        }

        Voice* v = _find_free_voice();
        if (!v) v = _steal_voice();
        if (!v) return;

        int root = root_note_cached_.load();
        double semitones = static_cast<double>(pitch - root);
        double freq_ratio = std::pow(2.0, semitones / 12.0);

        *v = Voice{};
        v->active   = true;
        v->channel  = channel;
        v->pitch    = pitch;
        v->vel_gain = velocity / 127.0f;
        float att = std::max(0.001f, att_cached_.load());
        float dec = std::max(0.001f, dec_cached_.load());
        float sus = sus_cached_.load();
        float rel = std::max(0.001f, rel_cached_.load());
        v->env.trigger(sample_rate_, att, dec, sus, rel);

        // Select mode with fallbacks
        PitchMode mode = pitch_mode_;
        if (mode == PitchMode::PSOLA &&
                (detected_f0_ <= 0.0f || pitch_marks_.size() < 2))
            mode = PitchMode::Varrate;
        if (mode == PitchMode::Spectral && spectral_bank_.n_frames < 2)
            mode = PitchMode::Varrate;

        v->mode = mode;

        switch (mode) {
            case PitchMode::Spectral:
                v->rate = freq_ratio;
                v->spect_frame_pos = 0.0;
                v->spect_out_avail = 0;
                v->spect_out_read  = 0;
                break;

            case PitchMode::WSOLA: {
                v->rate = freq_ratio;
                v->wsola_src_pos   = 0.0;
                v->wsola_ts_write  = 0;
                v->wsola_ts_read   = 0.0;
                v->wsola_produced  = 0;
                int sr_i = static_cast<int>(sample_rate_);
                v->wsola_grain_size = WSOLA_GRAIN_MS * sr_i / 1000;
                v->wsola_hop        = v->wsola_grain_size / 2;
                v->wsola_search     = WSOLA_SEARCH_MS * sr_i / 1000;
                // Pre-fill some time-stretched content
                _wsola_fill(v, v->wsola_grain_size * 4);
                break;
            }

            case PitchMode::PSOLA: {
                float target_hz = 440.0f * std::pow(2.0f, (pitch - 69) / 12.0f);
                v->target_period   = static_cast<double>(sample_rate_) / target_hz;
                v->analysis_period = static_cast<double>(sample_rate_) / detected_f0_;
                v->synth_time      = 0.0;
                v->analysis_time   = 0.0;
                v->output_time     = 0.0;
                v->overlap_len     = 0;
                v->last_mark_idx   = -1;
                break;
            }

            case PitchMode::Varrate:
                v->pos  = 0.0;
                v->rate = freq_ratio;
                break;
        }
    }

    void note_off(int channel, int pitch) override {
        for (auto& v : voices_) {
            if (v.active && v.channel == channel && v.pitch == pitch
                    && v.env.stage != ADSREnvelope::Stage::Release
                    && v.env.stage != ADSREnvelope::Stage::Off) {
                v.env.release();
                break;
            }
        }
    }

    void all_notes_off(int channel) override {
        for (auto& v : voices_) {
            if (!v.active) continue;
            if (channel == -1 || v.channel == channel)
                v.env.release();
        }
    }

    // -----------------------------------------------------------------------
    // Process (audio thread)
    // -----------------------------------------------------------------------
    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* out = buffers.audio.get("audio_out");
        if (!out) return;

        float* L = out->left;
        float* R = out->right ? out->right : out->left;

        std::memset(L, 0, ctx.block_size * sizeof(float));
        if (out->right) std::memset(R, 0, ctx.block_size * sizeof(float));

        if (sample_frames_ == 0) return;

        auto ctrl = [&](const char* id, float fallback) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fallback;
        };

        float gain     = std::max(0.0f, std::min(2.0f, ctrl("gain",    1.0f)));
        float root_fallback = static_cast<float>(auto_root_note_.load(std::memory_order_relaxed));
        int   root     = static_cast<int>(std::round(ctrl("root_note", root_fallback)));
        float att_s    = std::max(0.001f, ctrl("attack",  0.01f));
        float dec_s    = std::max(0.001f, ctrl("decay",   0.1f));
        float dec_lvl  = std::max(0.0f, std::min(1.0f, ctrl("sustain", 0.8f)));
        float rel_s    = std::max(0.001f, ctrl("release", 0.2f));

        root_note_cached_.store(root, std::memory_order_relaxed);
        att_cached_.store(att_s, std::memory_order_relaxed);
        dec_cached_.store(dec_s, std::memory_order_relaxed);
        sus_cached_.store(dec_lvl, std::memory_order_relaxed);
        rel_cached_.store(rel_s, std::memory_order_relaxed);

        // Update envelopes of active voices when ADSR params change mid-note
        for (auto& v : voices_) {
            if (v.active)
                v.env.update(sample_rate_, att_s, dec_s, dec_lvl, rel_s);
        }

        bool stereo = (sample_R_.size() == static_cast<size_t>(sample_frames_));

        for (auto& v : voices_) {
            if (!v.active) continue;

            switch (v.mode) {
                case PitchMode::Spectral:
                    _process_voice_spectral(v, ctx, L, R, gain);
                    break;
                case PitchMode::WSOLA:
                    _process_voice_wsola(v, ctx, L, R, gain);
                    break;
                case PitchMode::PSOLA:
                    _process_voice_psola(v, ctx, L, R, gain, stereo);
                    break;
                case PitchMode::Varrate:
                    _process_voice_varrate(v, ctx, L, R, gain,
                                            stereo, out->right != nullptr);
                    break;
            }
        }

        // Soft clip
        for (int i = 0; i < ctx.block_size; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    // -----------------------------------------------------------------------
    // Pitch mark helpers
    // -----------------------------------------------------------------------
    static int _find_nearest_mark(const std::vector<int>& marks, int n_marks,
                                   double analysis_time) {
        if (n_marks == 0) return -1;
        int t = static_cast<int>(std::round(analysis_time));
        if (t > marks[n_marks - 1]) return n_marks;

        int lo = 0, hi = n_marks - 1;
        while (lo < hi) {
            int mid = (lo + hi) / 2;
            if (marks[mid] < t) lo = mid + 1;
            else hi = mid;
        }
        if (lo > 0 && (t - marks[lo - 1]) < (marks[lo] - t))
            return lo - 1;
        return lo;
    }

    // -----------------------------------------------------------------------
    // Variable-rate playback
    // -----------------------------------------------------------------------
    void _process_voice_varrate(Voice& v, const PluginProcessContext& ctx,
                                 float* L, float* R, float gain,
                                 bool stereo, bool has_right) {
        for (int i = 0; i < ctx.block_size; ++i) {
            float env = v.env.next();
            if (v.env.is_off()) { v.active = false; break; }

            double fp = v.pos;
            long   ip = static_cast<long>(fp);
            float  t  = static_cast<float>(fp - static_cast<double>(ip));

            auto sL = [&](long n) -> float {
                n = std::clamp(n, 0L, sample_frames_ - 1);
                return sample_L_[static_cast<size_t>(n)];
            };
            auto sR = [&](long n) -> float {
                if (!stereo) return sL(n);
                n = std::clamp(n, 0L, sample_frames_ - 1);
                return sample_R_[static_cast<size_t>(n)];
            };

            auto cubic = [](float y0, float y1, float y2, float y3, float tc) {
                float a0 = -0.5f*y0 + 1.5f*y1 - 1.5f*y2 + 0.5f*y3;
                float a1 =       y0 - 2.5f*y1 + 2.0f*y2 - 0.5f*y3;
                float a2 = -0.5f*y0            + 0.5f*y2;
                float a3 =                  y1;
                return ((a0*tc + a1)*tc + a2)*tc + a3;
            };

            float sl = cubic(sL(ip-1), sL(ip), sL(ip+1), sL(ip+2), t);
            float sr = cubic(sR(ip-1), sR(ip), sR(ip+1), sR(ip+2), t);

            float amp = env * v.vel_gain * gain;
            L[i] += sl * amp;
            if (has_right) R[i] += sr * amp;

            v.pos += v.rate;
            if (v.pos >= static_cast<double>(sample_frames_)) {
                v.active = false;
                break;
            }
        }
    }

    // -----------------------------------------------------------------------
    // PSOLA playback (with skip-repeats fix)
    // -----------------------------------------------------------------------
    void _process_voice_psola(Voice& v, const PluginProcessContext& ctx,
                               float* L, float* R, float gain, bool stereo) {
        int n_marks = static_cast<int>(pitch_marks_.size());
        int bs = ctx.block_size;

        float env_start = v.env.level;
        for (int i = 0; i < bs; ++i)
            v.env.next();
        if (v.env.is_off() && v.overlap_len == 0) { v.active = false; return; }
        float env_avg = (env_start + v.env.level) * 0.5f;
        float amp = env_avg * v.vel_gain * gain;

        // Add leftover overlap
        int overlap_add = std::min(v.overlap_len, bs);
        for (int i = 0; i < overlap_add; ++i) {
            L[i] += v.overlap[i] * amp;
            R[i] += v.overlap[i] * amp;
        }
        if (overlap_add < v.overlap_len) {
            int rem = v.overlap_len - overlap_add;
            std::memmove(v.overlap, v.overlap + overlap_add, rem * sizeof(float));
            std::memset(v.overlap + rem, 0, (v.overlap_len - rem) * sizeof(float));
            v.overlap_len = rem;
        } else {
            std::memset(v.overlap, 0, v.overlap_len * sizeof(float));
            v.overlap_len = 0;
        }

        double block_end = v.output_time + bs;
        int half_host = static_cast<int>(std::ceil(v.target_period));
        double period_ratio = v.analysis_period / v.target_period;

        int safety = 0;
        while (v.synth_time < block_end && safety < 4096) {
            ++safety;

            int ai = _find_nearest_mark(pitch_marks_, n_marks, v.analysis_time);
            if (ai < 0 || ai >= n_marks) {
                v.active = false;
                break;
            }

            // Skip-repeats: if same mark as last grain, advance to next
            if (ai == v.last_mark_idx && ai + 1 < n_marks)
                ai += 1;
            v.last_mark_idx = ai;

            int center = pitch_marks_[ai];
            double host_center = v.synth_time - v.output_time;

            int host_start = static_cast<int>(std::floor(host_center)) - half_host;
            int host_end   = static_cast<int>(std::ceil(host_center))  + half_host;
            int grain_host_len = host_end - host_start;
            if (grain_host_len < 1) grain_host_len = 1;

            for (int hi = host_start; hi <= host_end; ++hi) {
                float t_win = static_cast<float>(hi - host_start) /
                              static_cast<float>(grain_host_len);
                float w = 0.5f * (1.0f - std::cos(
                    2.0f * static_cast<float>(M_PI) * t_win));

                double src_pos = center + (hi - host_center) * period_ratio;
                int si = static_cast<int>(std::floor(src_pos));
                float frac = static_cast<float>(src_pos - si);

                auto safe_L = [&](int n) -> float {
                    return (n >= 0 && n < sample_frames_) ? sample_L_[n] : 0.0f;
                };
                auto safe_R = [&](int n) -> float {
                    if (!stereo) return safe_L(n);
                    return (n >= 0 && n < sample_frames_) ? sample_R_[n] : 0.0f;
                };

                float sl = (safe_L(si) + frac * (safe_L(si+1) - safe_L(si))) * w;
                float sr_samp = (safe_R(si) + frac * (safe_R(si+1) - safe_R(si))) * w;

                if (hi >= 0 && hi < bs) {
                    L[hi] += sl * amp;
                    R[hi] += sr_samp * amp;
                } else if (hi >= bs && hi < bs + Voice::MAX_OVERLAP) {
                    int oi = hi - bs;
                    if (oi >= v.overlap_len) {
                        std::memset(v.overlap + v.overlap_len, 0,
                                    (oi - v.overlap_len) * sizeof(float));
                        v.overlap_len = oi + 1;
                    }
                    v.overlap[oi] += (sl + sr_samp) * 0.5f;
                }
            }

            v.synth_time    += v.target_period;
            v.analysis_time += v.target_period;
        }

        // Look-ahead
        {
            double la_synth = v.synth_time;
            double la_analysis = v.analysis_time;
            int la_safety = 0;
            while (la_synth < block_end + half_host && la_safety < 16) {
                ++la_safety;
                int ai = _find_nearest_mark(pitch_marks_, n_marks, la_analysis);
                if (ai < 0 || ai >= n_marks) break;
                int center = pitch_marks_[ai];
                double host_center = la_synth - v.output_time;
                int host_start = static_cast<int>(std::floor(host_center)) - half_host;
                int host_end   = static_cast<int>(std::ceil(host_center))  + half_host;
                int grain_host_len = host_end - host_start;
                if (grain_host_len < 1) grain_host_len = 1;

                for (int hi = std::max(0, host_start); hi < std::min(bs, host_end + 1); ++hi) {
                    float t_win = static_cast<float>(hi - host_start) /
                                  static_cast<float>(grain_host_len);
                    float w = 0.5f * (1.0f - std::cos(
                        2.0f * static_cast<float>(M_PI) * t_win));
                    double src_pos = center + (hi - host_center) * period_ratio;
                    int si = static_cast<int>(std::floor(src_pos));
                    float frac = static_cast<float>(src_pos - si);
                    auto safe_L = [&](int n) -> float {
                        return (n >= 0 && n < sample_frames_) ? sample_L_[n] : 0.0f;
                    };
                    auto safe_R = [&](int n) -> float {
                        if (!stereo) return safe_L(n);
                        return (n >= 0 && n < sample_frames_) ? sample_R_[n] : 0.0f;
                    };
                    float sl = (safe_L(si) + frac * (safe_L(si+1) - safe_L(si))) * w;
                    float sr_samp = (safe_R(si) + frac * (safe_R(si+1) - safe_R(si))) * w;
                    L[hi] += sl * amp;
                    R[hi] += sr_samp * amp;
                }
                la_synth += v.target_period;
                la_analysis += v.target_period;
            }
        }

        v.output_time += bs;
    }

    // -----------------------------------------------------------------------
    // Spectral pitch-shift playback
    // -----------------------------------------------------------------------
    void _process_voice_spectral(Voice& v, const PluginProcessContext& ctx,
                                  float* L, float* R, float gain) {
        float shift = static_cast<float>(v.rate);
        int remaining = ctx.block_size;
        int out_pos = 0;

        while (remaining > 0) {
            // Drain output buffer first (with per-sample envelope)
            while (v.spect_out_avail > 0 && remaining > 0) {
                float env = v.env.next();
                if (v.env.is_off()) { v.active = false; return; }

                float s = v.spect_out_buf[v.spect_out_read++] * env * v.vel_gain * gain;
                L[out_pos] += s;
                R[out_pos] += s;
                ++out_pos;
                --remaining;
                --v.spect_out_avail;
            }
            v.spect_out_read = 0;

            if (remaining == 0) break;

            // Generate next hop
            double fi = v.spect_frame_pos;
            if (fi >= spectral_bank_.n_frames - 1) {
                v.active = false;
                break;
            }

            int i0 = static_cast<int>(fi);
            int i1 = std::min(i0 + 1, spectral_bank_.n_frames - 1);
            float frac = static_cast<float>(fi - i0);

            const float* mag0  = &spectral_bank_.magnitudes[i0 * SPECT_N_BINS];
            const float* mag1  = &spectral_bank_.magnitudes[i1 * SPECT_N_BINS];
            const float* freq0 = &spectral_bank_.inst_freqs[i0 * SPECT_N_BINS];
            const float* freq1 = &spectral_bank_.inst_freqs[i1 * SPECT_N_BINS];

            float new_mag[SPECT_N_BINS] = {};
            float new_ifreq_num[SPECT_N_BINS] = {};
            float new_weight[SPECT_N_BINS] = {};

            float bw = spectral_bank_.bin_width;

            // Bin relocation
            for (int k = 0; k < SPECT_N_BINS; ++k) {
                float m   = (1.0f - frac) * mag0[k]  + frac * mag1[k];
                float ifr = (1.0f - frac) * freq0[k] + frac * freq1[k];
                float target_freq = ifr * shift;
                float tb = target_freq / bw;
                int tb_lo = static_cast<int>(std::floor(tb));
                float f2 = tb - tb_lo;

                float w0 = m * (1.0f - f2);
                float w1 = m * f2;
                if (tb_lo >= 0 && tb_lo < SPECT_N_BINS) {
                    new_mag[tb_lo]       += w0;
                    new_ifreq_num[tb_lo] += target_freq * w0;
                    new_weight[tb_lo]    += w0;
                }
                if (tb_lo + 1 >= 0 && tb_lo + 1 < SPECT_N_BINS) {
                    new_mag[tb_lo + 1]       += w1;
                    new_ifreq_num[tb_lo + 1] += target_freq * w1;
                    new_weight[tb_lo + 1]    += w1;
                }
            }

            // Phase accumulation
            float hop_time = static_cast<float>(SPECT_HOP) / sample_rate_;
            for (int k = 0; k < SPECT_N_BINS; ++k) {
                float ifreq;
                if (new_weight[k] > 0)
                    ifreq = new_ifreq_num[k] / new_weight[k];
                else
                    ifreq = k * bw;

                v.phase_acc[k] += 2.0f * static_cast<float>(M_PI) * ifreq * hop_time;
                // Wrap to prevent float precision loss
                if (v.phase_acc[k] > 1e6f || v.phase_acc[k] < -1e6f)
                    v.phase_acc[k] = std::fmod(v.phase_acc[k],
                        2.0f * static_cast<float>(M_PI));
            }

            // IRFFT
            float frame_out[SPECT_FFT_SIZE];
            irfft_mag_phase(new_mag, v.phase_acc, frame_out,
                            v.irfft_scratch, SPECT_FFT_SIZE);

            // Synthesis window
            for (int i = 0; i < SPECT_FFT_SIZE; ++i)
                frame_out[i] *= spectral_bank_.window[i];

            // Overlap-add
            for (int i = 0; i < SPECT_FFT_SIZE; ++i)
                v.ola_buf[i] += frame_out[i];

            // Extract hop, normalize by OLA norm
            for (int i = 0; i < SPECT_HOP; ++i)
                v.spect_out_buf[i] = v.ola_buf[i] / spectral_bank_.ola_norm[i];
            v.spect_out_avail = SPECT_HOP;
            v.spect_out_read  = 0;

            // Shift OLA buffer left by hop
            std::memmove(v.ola_buf, v.ola_buf + SPECT_HOP,
                         (SPECT_FFT_SIZE - SPECT_HOP) * sizeof(float));
            std::memset(v.ola_buf + SPECT_FFT_SIZE - SPECT_HOP, 0,
                        SPECT_HOP * sizeof(float));

            v.spect_frame_pos += 1.0;  // advance one frame
        }
    }

    // -----------------------------------------------------------------------
    // WSOLA pitch-shift playback
    //
    // Strategy: time-stretch source by factor 1.0 (identity) into ts_buf
    // using WSOLA overlap-add, then resample from ts_buf at v.rate.
    // Actually: time-stretch by beta, resample by 1/beta.
    // For pitch up (rate>1): stretch by rate (longer), resample at rate (faster).
    // -----------------------------------------------------------------------

    // Produce WSOLA time-stretched samples into v.wsola_ts_buf
    void _wsola_fill(Voice* v, int needed) {
        int grain_size = v->wsola_grain_size;
        int hop = v->wsola_hop;
        int search = v->wsola_search;
        float beta = static_cast<float>(v->rate);
        // Analysis hop: how far to advance in source per grain
        // For time-stretch by beta: analysis_hop = hop / beta
        // But we want to stretch by beta (make it longer), so
        // we consume source slower: src_advance = hop / beta
        float src_advance = static_cast<float>(hop) / beta;

        while (v->wsola_produced < needed + grain_size) {
            int src_center = static_cast<int>(std::round(v->wsola_src_pos));

            // Cross-correlation search for best overlap
            int best_offset = 0;
            if (v->wsola_produced > 0) {
                float best_corr = -1e30f;
                int overlap_len = std::min(hop, grain_size / 2);

                // Reference: what's already in the tail of ts_buf
                int ref_start = v->wsola_ts_write - overlap_len;
                if (ref_start < 0) ref_start += Voice::WSOLA_BUF_SIZE;

                for (int offset = -search; offset <= search; ++offset) {
                    int cand = src_center + offset;
                    if (cand < 0 || cand + grain_size > sample_frames_) continue;

                    float corr = 0.0f, norm_a = 0.0f, norm_b = 0.0f;
                    for (int j = 0; j < overlap_len; ++j) {
                        int ri = (ref_start + j) % Voice::WSOLA_BUF_SIZE;
                        float a = v->wsola_ts_buf[ri];
                        float b = sample_L_[cand + j];
                        corr   += a * b;
                        norm_a += a * a;
                        norm_b += b * b;
                    }
                    float denom = std::sqrt(norm_a * norm_b);
                    if (denom > 1e-12f) corr /= denom;
                    else corr = 0.0f;

                    if (corr > best_corr) {
                        best_corr = corr;
                        best_offset = offset;
                    }
                }
            }

            int actual_src = src_center + best_offset;
            actual_src = std::max(0, std::min(actual_src,
                static_cast<int>(sample_frames_) - grain_size));

            if (actual_src + grain_size > sample_frames_) {
                // Source exhausted
                break;
            }

            // Hann window for this grain
            float inv_gs = 1.0f / static_cast<float>(grain_size);

            // If first grain, just copy; otherwise crossfade overlap region
            if (v->wsola_produced == 0) {
                for (int j = 0; j < grain_size; ++j) {
                    float w = 0.5f * (1.0f - std::cos(2.0f * static_cast<float>(M_PI)
                              * j * inv_gs));
                    int wi = (v->wsola_ts_write + j) % Voice::WSOLA_BUF_SIZE;
                    v->wsola_ts_buf[wi] = sample_L_[actual_src + j] * w;
                }
            } else {
                // Overlap region: crossfade with existing content
                int overlap_len = std::min(hop, grain_size);
                for (int j = 0; j < grain_size; ++j) {
                    float w = 0.5f * (1.0f - std::cos(2.0f * static_cast<float>(M_PI)
                              * j * inv_gs));
                    float s = sample_L_[actual_src + j] * w;
                    int wi = (v->wsola_ts_write + j) % Voice::WSOLA_BUF_SIZE;
                    if (j < overlap_len) {
                        // Add to existing (overlap-add)
                        v->wsola_ts_buf[wi] += s;
                    } else {
                        v->wsola_ts_buf[wi] = s;
                    }
                }
            }

            v->wsola_ts_write = (v->wsola_ts_write + hop) % Voice::WSOLA_BUF_SIZE;
            v->wsola_produced += hop;
            v->wsola_src_pos += src_advance;

            if (v->wsola_src_pos + grain_size >= sample_frames_)
                break;
        }
    }

    void _process_voice_wsola(Voice& v, const PluginProcessContext& ctx,
                               float* L, float* R, float gain) {
        for (int i = 0; i < ctx.block_size; ++i) {
            float env = v.env.next();
            if (v.env.is_off()) { v.active = false; break; }

            // Ensure we have enough time-stretched data ahead
            int read_int = static_cast<int>(v.wsola_ts_read);
            if (read_int + 4 >= v.wsola_produced) {
                _wsola_fill(&v, read_int + v.wsola_grain_size * 4);
                if (read_int + 2 >= v.wsola_produced) {
                    v.active = false;
                    break;
                }
            }

            // Linear interpolation from ts_buf (resampling at v.rate)
            int ip = static_cast<int>(v.wsola_ts_read);
            float frac = static_cast<float>(v.wsola_ts_read - ip);
            int i0 = ip % Voice::WSOLA_BUF_SIZE;
            int i1 = (ip + 1) % Voice::WSOLA_BUF_SIZE;
            float s = v.wsola_ts_buf[i0] + frac * (v.wsola_ts_buf[i1] - v.wsola_ts_buf[i0]);

            float amp = env * v.vel_gain * gain;
            L[i] += s * amp;
            R[i] += s * amp;

            v.wsola_ts_read += v.rate;
        }
    }

    // -----------------------------------------------------------------------
    // Spectral bank computation (at load time)
    // -----------------------------------------------------------------------
    void _compute_spectral_bank() {
        spectral_bank_ = SpectralBank{};

        if (sample_frames_ < SPECT_FFT_SIZE) return;

        // Periodic Hann window
        for (int i = 0; i < SPECT_FFT_SIZE; ++i)
            spectral_bank_.window[i] = 0.5f * (1.0f - std::cos(
                2.0f * static_cast<float>(M_PI) * i / SPECT_FFT_SIZE));

        // OLA normalization: sum of squared windows per hop position
        std::memset(spectral_bank_.ola_norm, 0, sizeof(spectral_bank_.ola_norm));
        for (int k = 0; k < SPECT_FFT_SIZE / SPECT_HOP; ++k) {
            for (int i = 0; i < SPECT_HOP; ++i) {
                float w = spectral_bank_.window[k * SPECT_HOP + i];
                spectral_bank_.ola_norm[i] += w * w;
            }
        }

        spectral_bank_.bin_width = sample_rate_ / SPECT_FFT_SIZE;
        int n_frames = (static_cast<int>(sample_frames_) - SPECT_FFT_SIZE) / SPECT_HOP + 1;
        if (n_frames < 2) return;

        spectral_bank_.n_frames = n_frames;
        spectral_bank_.magnitudes.resize(n_frames * SPECT_N_BINS);
        spectral_bank_.inst_freqs.resize(n_frames * SPECT_N_BINS);

        std::vector<float> cplx(2 * SPECT_FFT_SIZE);
        std::vector<float> prev_phase(SPECT_N_BINS, 0.0f);
        float expected_advance_base = 2.0f * static_cast<float>(M_PI) *
            SPECT_HOP / SPECT_FFT_SIZE;

        for (int f = 0; f < n_frames; ++f) {
            int offset = f * SPECT_HOP;

            // Window + pack for FFT
            for (int i = 0; i < SPECT_FFT_SIZE; ++i) {
                int si = offset + i;
                float s = (si < sample_frames_) ? sample_L_[si] : 0.0f;
                cplx[2*i]   = s * spectral_bank_.window[i];
                cplx[2*i+1] = 0.0f;
            }
            fft_complex_inplace(cplx.data(), SPECT_FFT_SIZE, false);

            float* mag_row  = &spectral_bank_.magnitudes[f * SPECT_N_BINS];
            float* freq_row = &spectral_bank_.inst_freqs[f * SPECT_N_BINS];

            for (int k = 0; k < SPECT_N_BINS; ++k) {
                float re = cplx[2*k], im = cplx[2*k+1];
                mag_row[k] = std::sqrt(re * re + im * im);

                float phase = std::atan2(im, re);
                float bin_freq = k * spectral_bank_.bin_width;

                if (f == 0) {
                    freq_row[k] = bin_freq;
                } else {
                    float expected = k * expected_advance_base;
                    float diff = phase - prev_phase[k] - expected;
                    // Wrap to [-pi, pi]
                    diff = diff - 2.0f * static_cast<float>(M_PI) *
                           std::round(diff / (2.0f * static_cast<float>(M_PI)));
                    freq_row[k] = bin_freq + diff * sample_rate_ /
                        (2.0f * static_cast<float>(M_PI) * SPECT_HOP);
                }
                prev_phase[k] = phase;
            }
        }
    }

    // -----------------------------------------------------------------------
    // Sample loading
    // -----------------------------------------------------------------------
    void _load_sample(const std::string& path) {
        sample_L_.clear();
        sample_R_.clear();
        sample_frames_ = 0;
        pitch_marks_.clear();
        detected_f0_ = 0.0f;
        spectral_bank_ = SpectralBank{};

        if (path.empty()) return;

#ifdef AS_ENABLE_SNDFILE
        SF_INFO info{};
        SNDFILE* sf = sf_open(path.c_str(), SFM_READ, &info);
        if (!sf) return;

        long frames = static_cast<long>(info.frames);
        int  ch     = info.channels;

        std::vector<float> interleaved(static_cast<size_t>(frames) * ch);
        sf_count_t got = sf_readf_float(sf, interleaved.data(), frames);
        sf_close(sf);

        frames = static_cast<long>(got);

        // Resample to engine sample rate
        double ratio = static_cast<double>(info.samplerate) /
                       static_cast<double>(sample_rate_);
        long out_frames = static_cast<long>(std::ceil(frames / ratio));

        sample_L_.resize(static_cast<size_t>(out_frames));
        sample_R_.resize(static_cast<size_t>(out_frames));

        for (long i = 0; i < out_frames; ++i) {
            double src_pos = static_cast<double>(i) * ratio;
            long   src_i   = static_cast<long>(src_pos);
            float  t       = static_cast<float>(src_pos - src_i);
            if (src_i >= frames - 1) src_i = frames - 1;

            float s0L = interleaved[static_cast<size_t>(src_i * ch + 0)];
            float s1L = (src_i + 1 < frames)
                        ? interleaved[static_cast<size_t>((src_i+1) * ch + 0)]
                        : s0L;
            sample_L_[static_cast<size_t>(i)] = s0L + t * (s1L - s0L);

            int ri = (ch >= 2) ? 1 : 0;
            float s0R = interleaved[static_cast<size_t>(src_i * ch + ri)];
            float s1R = (src_i + 1 < frames)
                        ? interleaved[static_cast<size_t>((src_i+1) * ch + ri)]
                        : s0R;
            sample_R_[static_cast<size_t>(i)] = s0R + t * (s1R - s0R);
        }

        sample_frames_ = out_frames;

        // F0 detection (for PSOLA mode + auto root note)
        detected_f0_ = yin_detect_f0_float(sample_L_.data(),
                                            static_cast<int>(out_frames),
                                            static_cast<int>(sample_rate_));

        if (detected_f0_ > 0.0f) {
            pitch_marks_ = find_pitch_marks_float(sample_L_.data(),
                                                   static_cast<int>(out_frames),
                                                   detected_f0_,
                                                   static_cast<int>(sample_rate_));

            // Auto-detect root note from F0: MIDI note = 69 + 12*log2(f/440)
            float midi_f = 69.0f + 12.0f * std::log2(detected_f0_ / 440.0f);
            int auto_root = std::clamp(static_cast<int>(std::round(midi_f)), 0, 127);
            auto_root_note_.store(auto_root, std::memory_order_release);
        }

        // Spectral bank (for spectral mode)
        _compute_spectral_bank();
#else
        (void)path;
#endif
    }

    Voice* _find_free_voice() {
        for (auto& v : voices_) if (!v.active) return &v;
        return nullptr;
    }

    Voice* _steal_voice() {
        for (auto& v : voices_)
            if (v.env.stage == ADSREnvelope::Stage::Release) return &v;
        for (auto& v : voices_)
            if (v.env.stage == ADSREnvelope::Stage::Sustain) return &v;
        return &voices_[0];
    }

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    float             sample_rate_   = 44100.0f;

    std::vector<float> sample_L_;
    std::vector<float> sample_R_;
    long               sample_frames_ = 0;

    // PSOLA analysis data
    float              detected_f0_  = 0.0f;
    std::vector<int>   pitch_marks_;

    // Spectral analysis data
    SpectralBank       spectral_bank_;

    // Mode selection
    PitchMode          pitch_mode_ = PitchMode::Spectral;

    std::string              pending_path_;
    std::atomic<bool>        path_dirty_{false};

    std::atomic<int>         auto_root_note_{60};   // from YIN F0 detection
    std::atomic<int>         root_note_cached_{60};
    std::atomic<float>       att_cached_{0.01f};
    std::atomic<float>       dec_cached_{0.1f};
    std::atomic<float>       sus_cached_{0.8f};
    std::atomic<float>       rel_cached_{0.2f};

    Voice voices_[MAX_VOICES] = {};
};

REGISTER_PLUGIN(SamplerPlugin);
REGISTER_PLUGIN_DYNAMIC(SamplerPlugin);

std::unique_ptr<Plugin> make_sampler_plugin() {
    return std::make_unique<SamplerPlugin>();
}
