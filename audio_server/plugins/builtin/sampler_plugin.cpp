// sampler_plugin.cpp
// Sample-based synthesizer with TD-PSOLA pitch shifting for pitched content.
//
// Pitch shifting strategy:
//   - At load time, YIN F0 detection runs on the sample.
//   - If a clear fundamental is detected: PSOLA is used for pitch-independent
//     time preservation. No chipmunk effect.
//   - If unvoiced/unpitched (drums, noise, etc.): falls back to variable-rate
//     playback (original behavior), which is appropriate for percussive content.
//
// Polyphony: up to MAX_VOICES simultaneous voices (oldest stolen if exceeded).
// Envelope: simple linear ADSR per voice.
//
// Supported formats (via libsndfile): WAV, AIFF, OGG, FLAC.
//
// Config params:
//   sample_path  – path to the audio file (string)
//
// Runtime ports:
//   audio_out    – stereo audio output
//   gain         – output gain control [0..2], default 1.0
//   root_note    – MIDI note of the unshifted sample (default 60 = C4)
//   attack/decay/sustain/release – ADSR envelope

#include "plugin_api.h"
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
// YIN F0 detection (float version, for engine-rate samples)
// ---------------------------------------------------------------------------

static float yin_detect_f0_float(const float* pcm, int len, int sample_rate) {
    if (len < 256) return 0.0f;

    // Skip leading/trailing silence
    constexpr float SILENCE_THRESH = 0.01f;
    int start = 0, end = len;
    while (start < len && std::abs(pcm[start]) < SILENCE_THRESH) ++start;
    while (end > start && std::abs(pcm[end - 1]) < SILENCE_THRESH) --end;

    int voiced_len = end - start;
    if (voiced_len < 256) return 0.0f;
    int quarter = voiced_len / 4;
    int win_start = start + quarter;
    int win_len   = voiced_len - 2 * quarter;

    int min_lag = std::max(1, sample_rate / 500);   // max F0 = 500 Hz
    int max_lag = std::min(sample_rate / 60, win_len / 2);  // min F0 = 60 Hz
    if (max_lag <= min_lag) return 0.0f;

    // Difference function
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

    // Cumulative mean normalized difference
    std::vector<float> dn(max_lag + 1, 1.0f);
    float running = 0.0f;
    for (int tau = 1; tau <= max_lag; ++tau) {
        running += d[tau];
        dn[tau] = (running > 0.0f) ? d[tau] * tau / running : 1.0f;
    }

    // Absolute threshold
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

    // Parabolic interpolation
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

    // Determine dominant polarity
    int check_len = std::min(len, period * 4);
    double pos_sum = 0, neg_sum = 0;
    for (int i = 0; i < check_len; ++i) {
        if (pcm[i] > 0) pos_sum += pcm[i];
        else             neg_sum -= pcm[i];
    }
    float sign = (pos_sum >= neg_sum) ? 1.0f : -1.0f;

    // Find first peak of chosen polarity
    int search_end = std::min(period, len);
    int best_pos = 0;
    float best_val = 0.0f;
    for (int i = 0; i < search_end; ++i) {
        float v = pcm[i] * sign;
        if (v > best_val) { best_val = v; best_pos = i; }
    }
    marks.push_back(best_pos);

    // Subsequent marks: ±15% search window, same polarity
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
// Voice
// ---------------------------------------------------------------------------

struct Voice {
    bool     active   = false;
    int      channel  = 0;
    int      pitch    = 0;
    float    vel_gain = 1.0f;

    // ADSR
    enum class Stage { Attack, Decay, Sustain, Release, Off } stage = Stage::Off;
    float    env      = 0.0f;
    float    env_rate = 0.0f;

    // Variable-rate playback (unpitched fallback)
    double   pos      = 0.0;
    double   rate     = 1.0;

    // PSOLA state (used when sample has detected F0)
    bool     use_psola = false;
    double   synth_time     = 0.0;
    double   target_period  = 0.0;
    double   analysis_time  = 0.0;   // position in source (sample-space)
    double   analysis_period = 0.0;  // = sr / detected_f0
    double   output_time    = 0.0;

    static constexpr int MAX_OVERLAP = 4096;
    float    overlap[MAX_OVERLAP] = {};
    int      overlap_len = 0;
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
            "Sample-based synthesizer with TD-PSOLA pitch shifting for pitched "
            "samples (preserves duration independently of pitch). Falls back to "
            "variable-rate playback for unpitched content (drums, noise). "
            "Supports WAV, AIFF, OGG, FLAC (via libsndfile).";
        d.author  = "builtin";
        d.version = 2;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain", "Output gain multiplier. 1.0 = unity.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f },
            { "root_note", "Root Note",
              "MIDI note number played at original pitch (0-127). Default 60 = C4.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 60.0f, 0.0f, 127.0f, 1.0f },
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
        };

        return d;
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "sample_path") {
            pending_path_ = value;
            path_dirty_.store(true, std::memory_order_release);
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

        *v = Voice{};  // reset all fields
        v->active   = true;
        v->channel  = channel;
        v->pitch    = pitch;
        v->vel_gain = velocity / 127.0f;
        v->stage    = Voice::Stage::Attack;
        v->env      = 0.0f;
        float att = std::max(0.001f, att_cached_.load());
        v->env_rate = 1.0f / (att * sample_rate_);

        if (detected_f0_ > 0.0f && pitch_marks_.size() >= 2) {
            // PSOLA mode
            v->use_psola = true;
            float target_hz = 440.0f * std::pow(2.0f, (pitch - 69) / 12.0f);
            v->target_period  = static_cast<double>(sample_rate_) / target_hz;
            v->analysis_period = static_cast<double>(sample_rate_) / detected_f0_;
            v->synth_time     = 0.0;
            v->analysis_time  = 0.0;
            v->output_time    = 0.0;
            v->overlap_len    = 0;
        } else {
            // Variable-rate fallback
            v->use_psola = false;
            v->pos  = 0.0;
            v->rate = std::pow(2.0, semitones / 12.0);
        }
    }

    void note_off(int channel, int pitch) override {
        for (auto& v : voices_) {
            if (v.active && v.channel == channel && v.pitch == pitch
                    && v.stage != Voice::Stage::Release
                    && v.stage != Voice::Stage::Off) {
                v.stage = Voice::Stage::Release;
                float rel = std::max(0.001f, rel_cached_.load());
                v.env_rate = (v.env > 0.0f) ? v.env / (rel * sample_rate_) : 0.001f;
                break;
            }
        }
    }

    void all_notes_off(int channel) override {
        for (auto& v : voices_) {
            if (!v.active) continue;
            if (channel == -1 || v.channel == channel) {
                v.stage = Voice::Stage::Release;
                float rel = std::max(0.001f, rel_cached_.load());
                if (v.env > 0.0f)
                    v.env_rate = v.env / (rel * sample_rate_);
                else
                    v.active = false;
            }
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
        int   root     = static_cast<int>(std::round(ctrl("root_note", 60.0f)));
        float att_s    = std::max(0.001f, ctrl("attack",  0.01f));
        float dec_s    = std::max(0.001f, ctrl("decay",   0.1f));
        float dec_lvl  = std::max(0.0f, std::min(1.0f, ctrl("sustain", 0.8f)));
        float rel_s    = std::max(0.001f, ctrl("release", 0.2f));

        root_note_cached_.store(root, std::memory_order_relaxed);
        att_cached_.store(att_s, std::memory_order_relaxed);
        rel_cached_.store(rel_s, std::memory_order_relaxed);

        bool stereo = (sample_R_.size() == static_cast<size_t>(sample_frames_));

        for (auto& v : voices_) {
            if (!v.active) continue;

            if (v.use_psola)
                _process_voice_psola(v, ctx, L, R, gain, dec_s, dec_lvl, stereo);
            else
                _process_voice_varrate(v, ctx, L, R, gain, dec_s, dec_lvl,
                                        stereo, out->right != nullptr);
        }

        // Soft clip
        for (int i = 0; i < ctx.block_size; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    // -----------------------------------------------------------------------
    // Find the pitch mark index nearest to the given time (in samples).
    // Returns n_marks if past end, -1 if empty.
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
    // Variable-rate playback (original method, for unpitched content)
    // -----------------------------------------------------------------------
    void _process_voice_varrate(Voice& v, const PluginProcessContext& ctx,
                                 float* L, float* R, float gain,
                                 float dec_s, float dec_lvl,
                                 bool stereo, bool has_right) {
        for (int i = 0; i < ctx.block_size; ++i) {
            float env = _advance_envelope(v, dec_s, dec_lvl);
            if (!v.active) break;

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
                v.stage  = Voice::Stage::Off;
                break;
            }
        }
    }

    // -----------------------------------------------------------------------
    // PSOLA playback (for pitched content)
    // -----------------------------------------------------------------------
    void _process_voice_psola(Voice& v, const PluginProcessContext& ctx,
                               float* L, float* R, float gain,
                               float dec_s, float dec_lvl, bool stereo) {
        int n_marks = static_cast<int>(pitch_marks_.size());
        int bs = ctx.block_size;

        // Advance envelope for the block (simplified: compute per-sample in
        // the grain placement loop would be more accurate, but envelope is
        // slow-moving relative to grains)
        // We compute an average envelope for this block.
        float env_start = v.env;
        for (int i = 0; i < bs; ++i)
            _advance_envelope(v, dec_s, dec_lvl);
        if (!v.active && v.overlap_len == 0) return;
        float env_avg = (env_start + v.env) * 0.5f;
        float amp = env_avg * v.vel_gain * gain;

        // Add leftover overlap from previous block
        int overlap_add = std::min(v.overlap_len, bs);
        for (int i = 0; i < overlap_add; ++i) {
            L[i] += v.overlap[i] * amp;
            R[i] += v.overlap[i] * amp;  // mono overlap, applied to both
        }
        if (overlap_add < v.overlap_len) {
            int rem = v.overlap_len - overlap_add;
            for (int i = 0; i < rem; ++i)
                v.overlap[i] = v.overlap[i + overlap_add];
            // Zero vacated tail to prevent stale data accumulation
            for (int i = rem; i < v.overlap_len; ++i)
                v.overlap[i] = 0.0f;
            v.overlap_len = rem;
        } else {
            // Zero ALL consumed slots
            for (int i = 0; i < v.overlap_len; ++i)
                v.overlap[i] = 0.0f;
            v.overlap_len = 0;
        }

        double block_end = v.output_time + bs;
        int half_host = static_cast<int>(std::ceil(v.target_period));
        double period_ratio = v.analysis_period / v.target_period;

        int safety = 0;
        while (v.synth_time < block_end && safety < 4096) {
            ++safety;

            // Find nearest pitch mark to current analysis_time
            int ai = _find_nearest_mark(pitch_marks_, n_marks, v.analysis_time);
            if (ai < 0 || ai >= n_marks) {
                v.active = false;
                break;
            }

            int center = pitch_marks_[ai];

            double host_center = v.synth_time - v.output_time;

            // Hann window sized to 2 × target_period so adjacent grains
            // overlap at ~50% and window sum ≈ 1.0.
            int host_start = static_cast<int>(std::floor(host_center)) - half_host;
            int host_end   = static_cast<int>(std::ceil(host_center))  + half_host;
            int grain_host_len = host_end - host_start;
            if (grain_host_len < 1) grain_host_len = 1;

            for (int hi = host_start; hi <= host_end; ++hi) {
                float t_win = static_cast<float>(hi - host_start) /
                              static_cast<float>(grain_host_len);
                float w = 0.5f * (1.0f - std::cos(
                    2.0f * static_cast<float>(M_PI) * t_win));

                // Scale source offset by period ratio for pitch shifting
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
                        for (int z = v.overlap_len; z < oi; ++z)
                            v.overlap[z] = 0.0f;
                        v.overlap_len = oi + 1;
                    }
                    v.overlap[oi] += (sl + sr_samp) * 0.5f;
                }
            }

            v.synth_time    += v.target_period;
            v.analysis_time += v.target_period;
        }

        // Look-ahead: render grains just past block_end whose backward
        // tails reach into this block. Only write to [0, bs), not overlap.
        // These grains will be re-processed by the next block's main loop.
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
    // Envelope helper (returns current env value, advances state)
    // -----------------------------------------------------------------------
    float _advance_envelope(Voice& v, float dec_s, float dec_lvl) {
        float env = v.env;
        switch (v.stage) {
            case Voice::Stage::Attack:
                env += v.env_rate;
                if (env >= 1.0f) {
                    env = 1.0f;
                    v.stage = Voice::Stage::Decay;
                    v.env_rate = (1.0f - dec_lvl) / (dec_s * sample_rate_);
                }
                break;
            case Voice::Stage::Decay:
                env -= v.env_rate;
                if (env <= dec_lvl) {
                    env = dec_lvl;
                    v.stage = Voice::Stage::Sustain;
                    v.env_rate = 0.0f;
                }
                break;
            case Voice::Stage::Sustain:
                env = dec_lvl;
                break;
            case Voice::Stage::Release:
                env -= v.env_rate;
                if (env <= 0.0f) {
                    env = 0.0f;
                    v.stage  = Voice::Stage::Off;
                    v.active = false;
                }
                break;
            case Voice::Stage::Off:
                v.active = false;
                break;
        }
        v.env = env;
        return env;
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

        // Resample to engine sample rate (linear interpolation)
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

        // F0 detection on the loaded (engine-rate) sample.
        // Use left channel; for most musical content L and R have same pitch.
        detected_f0_ = yin_detect_f0_float(sample_L_.data(),
                                            static_cast<int>(out_frames),
                                            static_cast<int>(sample_rate_));

        if (detected_f0_ > 0.0f) {
            pitch_marks_ = find_pitch_marks_float(sample_L_.data(),
                                                   static_cast<int>(out_frames),
                                                   detected_f0_,
                                                   static_cast<int>(sample_rate_));
        }
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
            if (v.stage == Voice::Stage::Release) return &v;
        for (auto& v : voices_)
            if (v.stage == Voice::Stage::Sustain) return &v;
        return &voices_[0];
    }

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    float             sample_rate_   = 44100.0f;

    std::vector<float> sample_L_;
    std::vector<float> sample_R_;
    long               sample_frames_ = 0;

    // PSOLA analysis data (computed at load time)
    float              detected_f0_  = 0.0f;   // 0 = unpitched → use varrate
    std::vector<int>   pitch_marks_;

    std::string              pending_path_;
    std::atomic<bool>        path_dirty_{false};

    std::atomic<int>         root_note_cached_{60};
    std::atomic<float>       att_cached_{0.01f};
    std::atomic<float>       rel_cached_{0.2f};

    Voice voices_[MAX_VOICES] = {};
};

REGISTER_PLUGIN(SamplerPlugin);
REGISTER_PLUGIN_DYNAMIC(SamplerPlugin);

std::unique_ptr<Plugin> make_sampler_plugin() {
    return std::make_unique<SamplerPlugin>();
}
