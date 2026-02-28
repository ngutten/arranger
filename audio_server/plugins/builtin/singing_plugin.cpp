// singing_plugin.cpp
// espeak-ng based singing synthesiser with TD-PSOLA pitch shifting.
//
// THREADING MODEL
// ---------------
// configure() and activate() are called on the MAIN thread.
// process() and note_* are called on the AUDIO thread — must not block.
//
// All espeak synthesis + YIN analysis happens in _render_one() on the main
// thread. The resulting PcmSeq is published via an atomic pointer.
//
// Pitch shifting uses TD-PSOLA (Time-Domain Pitch-Synchronous Overlap-Add):
//   - YIN detects F0 of each rendered syllable → no magic base frequency
//   - Pitch marks placed at period boundaries
//   - Synthesis: Hann-windowed grains placed at target-pitch intervals
//   - Pitch is shifted without changing duration (no chipmunk effect)
//   - Sustain looping allows holding notes indefinitely
//
// Only compiled when AS_ENABLE_ESPEAK is defined (CMake ENABLE_ESPEAK option).

#ifdef AS_ENABLE_ESPEAK

#include "plugin_api.h"

#include <espeak-ng/espeak_ng.h>
#include <espeak-ng/speak_lib.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <list>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <sys/stat.h>
#include <sys/types.h>

// ---------------------------------------------------------------------------
// Minimal WAV writer for debug dumps
// ---------------------------------------------------------------------------

static void write_wav_s16(const char* path, const short* pcm, int len, int sr) {
    FILE* f = std::fopen(path, "wb");
    if (!f) return;
    int data_bytes = len * 2;
    int file_size  = 36 + data_bytes;
    // RIFF header
    std::fwrite("RIFF", 1, 4, f);
    uint32_t v = file_size; std::fwrite(&v, 4, 1, f);
    std::fwrite("WAVE", 1, 4, f);
    // fmt chunk
    std::fwrite("fmt ", 1, 4, f);
    v = 16;           std::fwrite(&v, 4, 1, f);  // chunk size
    uint16_t s = 1;   std::fwrite(&s, 2, 1, f);  // PCM
    s = 1;            std::fwrite(&s, 2, 1, f);  // mono
    v = sr;           std::fwrite(&v, 4, 1, f);  // sample rate
    v = sr * 2;       std::fwrite(&v, 4, 1, f);  // byte rate
    s = 2;            std::fwrite(&s, 2, 1, f);  // block align
    s = 16;           std::fwrite(&s, 2, 1, f);  // bits per sample
    // data chunk
    std::fwrite("data", 1, 4, f);
    v = data_bytes;   std::fwrite(&v, 4, 1, f);
    std::fwrite(pcm, 2, len, f);
    std::fclose(f);
}

static void write_wav_f32(const char* path, const float* pcm, int len, int sr) {
    // Convert to s16 and write
    std::vector<short> buf(len);
    for (int i = 0; i < len; ++i) {
        float s = pcm[i] * 32767.0f;
        if (s > 32767.0f) s = 32767.0f;
        if (s < -32768.0f) s = -32768.0f;
        buf[i] = static_cast<short>(s);
    }
    write_wav_s16(path, buf.data(), len, sr);
}

// ---------------------------------------------------------------------------
// Diagnostic logging
// ---------------------------------------------------------------------------

static auto s_t0 = std::chrono::steady_clock::now();

static void sing_log(const char* msg) {
    auto now = std::chrono::steady_clock::now();
    long ms = static_cast<long>(
        std::chrono::duration_cast<std::chrono::milliseconds>(now - s_t0).count());
    std::ostringstream tid;
    tid << std::this_thread::get_id();
    std::fprintf(stderr, "[SINGING][tid=%s][+%ldms] %s\n",
                 tid.str().c_str(), ms, msg);
    std::fflush(stderr);
}

static void sing_logf(const char* fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    sing_log(buf);
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

static constexpr int   ESPEAK_SAMPLE_RATE_DEFAULT = 22050;
static constexpr float MIDI_A4_FREQ       = 440.0f;
static constexpr int   MIDI_A4_NOTE       = 69;

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// YIN F0 detection
// ---------------------------------------------------------------------------
// de Cheveigné & Kawahara (2002). Operates on a window of PCM samples.
// Returns detected F0 in Hz, or 0 if unvoiced/silent.

static float yin_detect_f0(const short* pcm, int len, int sample_rate) {
    if (len < 256) return 0.0f;

    // Skip leading/trailing silence (threshold: ~1% of int16 range)
    constexpr int SILENCE_THRESH = 328;
    int start = 0, end = len;
    while (start < len && std::abs(pcm[start]) < SILENCE_THRESH) ++start;
    while (end > start && std::abs(pcm[end - 1]) < SILENCE_THRESH) --end;

    // Use central 50% of the voiced region
    int voiced_len = end - start;
    if (voiced_len < 256) return 0.0f;
    int quarter = voiced_len / 4;
    int win_start = start + quarter;
    int win_len   = voiced_len - 2 * quarter;

    // Lag range: min_lag → max F0 (500 Hz), max_lag → min F0 (60 Hz)
    int min_lag = std::max(1, sample_rate / 500);
    int max_lag = std::min(sample_rate / 60, win_len / 2);
    if (max_lag <= min_lag) return 0.0f;

    // Difference function
    std::vector<float> d(max_lag + 1, 0.0f);
    for (int tau = 1; tau <= max_lag; ++tau) {
        float sum = 0.0f;
        int n = win_len - max_lag;
        for (int i = 0; i < n; ++i) {
            float diff = static_cast<float>(pcm[win_start + i]) -
                         static_cast<float>(pcm[win_start + i + tau]);
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

    // Absolute threshold: find first tau >= min_lag where dn < 0.15
    constexpr float YIN_THRESHOLD = 0.15f;
    int best_tau = 0;
    for (int tau = min_lag; tau <= max_lag; ++tau) {
        if (dn[tau] < YIN_THRESHOLD) {
            // Walk to the local minimum
            while (tau + 1 <= max_lag && dn[tau + 1] < dn[tau]) ++tau;
            best_tau = tau;
            break;
        }
    }

    // Fallback: global minimum
    if (best_tau == 0) {
        float best_val = 1e30f;
        for (int tau = min_lag; tau <= max_lag; ++tau) {
            if (dn[tau] < best_val) { best_val = dn[tau]; best_tau = tau; }
        }
        if (best_val > 0.5f) return 0.0f;
    }

    // Parabolic interpolation for sub-sample accuracy
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
// Pitch mark placement
// ---------------------------------------------------------------------------

static std::vector<int> find_pitch_marks(const short* pcm, int len,
                                         float f0, int sr) {
    std::vector<int> marks;
    if (f0 < 1.0f || len < 4) return marks;

    int period = static_cast<int>(std::round(static_cast<float>(sr) / f0));
    if (period < 2) return marks;

    // Determine dominant polarity from the first few periods:
    // sum signed peaks to see if waveform is predominantly positive or negative.
    int polarity_check_len = std::min(len, period * 4);
    long pos_sum = 0, neg_sum = 0;
    for (int i = 0; i < polarity_check_len; ++i) {
        if (pcm[i] > 0) pos_sum += pcm[i];
        else             neg_sum -= pcm[i];  // make positive
    }
    // Use whichever polarity has larger total energy for peak search.
    // sign = +1: search for positive peaks. sign = -1: search for negative peaks.
    int sign = (pos_sum >= neg_sum) ? 1 : -1;

    // Find first peak of the chosen polarity within the first period
    int search_end = std::min(period, len);
    int best_pos = 0;
    int best_val = 0;
    for (int i = 0; i < search_end; ++i) {
        int v = pcm[i] * sign;  // positive when matching our chosen polarity
        if (v > best_val) { best_val = v; best_pos = i; }
    }
    marks.push_back(best_pos);

    // Subsequent marks: search for peak of same polarity within ±15% of
    // expected position (tighter than ±25% to avoid octave jumps)
    while (true) {
        int expected = marks.back() + period;
        if (expected >= len) break;

        int margin = period * 15 / 100;
        int lo = std::max(0, expected - margin);
        int hi = std::min(len - 1, expected + margin);

        best_pos = expected;
        best_val = 0;
        for (int i = lo; i <= hi; ++i) {
            int v = pcm[i] * sign;
            if (v > best_val) { best_val = v; best_pos = i; }
        }
        marks.push_back(best_pos);
    }

    return marks;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static float midi_to_hz(int midi_note) {
    return MIDI_A4_FREQ * std::pow(2.0f, (midi_note - MIDI_A4_NOTE) / 12.0f);
}

static std::vector<std::string> split_words(const std::string& s) {
    std::vector<std::string> out;
    std::istringstream ss(s);
    std::string tok;
    while (ss >> tok) out.push_back(tok);
    return out;
}

static long ms_since(std::chrono::steady_clock::time_point t) {
    return static_cast<long>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t).count());
}

// ---------------------------------------------------------------------------
// Process-global espeak serialisation
// ---------------------------------------------------------------------------

static std::mutex          s_espeak_mutex;
static std::atomic<int>    s_espeak_refcount{0};
static std::vector<short>* s_espeak_buf = nullptr;

static int espeak_synth_callback(short* wav, int numsamples,
                                 espeak_EVENT* /*events*/) {
    if (wav && numsamples > 0 && s_espeak_buf)
        s_espeak_buf->insert(s_espeak_buf->end(), wav, wav + numsamples);
    return 0;
}

// ---------------------------------------------------------------------------
// SingingPlugin
// ---------------------------------------------------------------------------

class SingingPlugin final : public Plugin {
    struct PcmEntry {
        double             beat = 0.0;
        std::vector<short> pcm;
        float              detected_f0 = 0.0f;
        std::vector<int>   pitch_marks;
        int                loop_start = 0;
        int                loop_end   = 0;
    };
    using PcmSeq = std::vector<PcmEntry>;

    enum class State : int {
        Created = 0, Activating = 1, InitEspeak = 2,
        Rendering = 3, Ready = 4, Error = 5, Deactivated = 6,
    };

    static const char* state_name(State s) {
        switch (s) {
            case State::Created:    return "created";
            case State::Activating: return "activating";
            case State::InitEspeak: return "init_espeak";
            case State::Rendering:  return "rendering";
            case State::Ready:      return "ready";
            case State::Error:      return "error";
            case State::Deactivated:return "deactivated";
        }
        return "?";
    }

public:
    SingingPlugin() { sing_log("constructor"); }

    ~SingingPlugin() override {
        sing_log("destructor");
        _teardown_espeak();
    }

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.singing";
        d.display_name = "Singing (espeak-ng)";
        d.category     = "Synth";
        d.doc =
            "Singing synthesiser using espeak-ng phoneme rendering with TD-PSOLA\n"
            "pitch shifting. Pitch is independent of duration — no chipmunk effect.\n"
            "F0 is auto-detected per syllable; no manual base frequency needed.\n"
            "Notes can be held via sustain looping of the vowel nucleus.\n\n"
            "Connect a Pattern Source to the Lyrics port so the plugin knows\n"
            "which syllable to sing for each note-on event.";
        d.author  = "builtin";
        d.version = 2;

        d.ports = {
            { "lyrics_in", "Lyrics",
              "Pattern containing per-note lyric syllables.\n"
              "Connect a Pattern Source node here so the plugin knows which\n"
              "syllable to sing for each note-on event.",
              PluginPortType::Pattern, PortRole::Input },
            { "events_in",  "Events", "MIDI note input",
              PluginPortType::Event, PortRole::Input },
            { "audio_out",  "Audio",  "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
        };

        d.config_params = {
            { "voice", "Voice",
              "espeak-ng voice name (e.g. \"en\", \"en-us\", \"en+f3\").",
              ConfigType::String, "en" },
            { "debug_dump", "Debug Dump",
              "Set to \"1\" to write raw espeak PCM and PSOLA output as WAV files "
              "to /tmp/singing_debug/. Set to \"0\" or leave empty to disable.",
              ConfigType::String, "" },
        };

        return d;
    }

    // ------------------------------------------------------------------
    void configure(const std::string& key, const std::string& value) override {
        sing_logf("configure: key='%s' value='%.40s'", key.c_str(), value.c_str());
        if (key == "voice") {
            _voice = value.empty() ? "en" : value;
            _rebuild_pcm_seq();
        } else if (key == "debug_dump") {
            _debug_dump = (value == "1" || value == "true");
            if (_debug_dump) {
                mkdir("/tmp/singing_debug", 0755);
                sing_log("configure: debug dump ENABLED → /tmp/singing_debug/");
            }
        }
    }

    void on_pattern_connected(const PatternData& pd) override {
        std::string lyrics;
        for (const auto& note : pd.notes) {
            if (note.lyric.empty()) continue;
            if (!lyrics.empty()) lyrics += ' ';
            lyrics += note.lyric;
        }
        sing_logf("on_pattern_connected: %zu note(s), lyrics='%.60s'",
                  pd.notes.size(), lyrics.c_str());
        _pending_lyrics = std::move(lyrics);
    }

    void push_lyric(double beat, const std::string& lyric,
                    int /*pitch*/, double /*duration_beats*/) override {
        // Engine may call push_lyric before activate() during graph rebuild.
        // Lazily initialise espeak if needed so we can still render.
        if (!_espeak_initialised) {
            sing_log("push_lyric: espeak not initialised, initialising now");
            _init_espeak();
            if (!_espeak_initialised) {
                sing_log("push_lyric: espeak init failed, dropping lyric");
                return;
            }
        }

        PcmEntry entry;
        entry.beat = beat;
        if (!lyric.empty()) {
            sing_logf("push_lyric: beat=%.3f lyric='%.40s'", beat, lyric.c_str());
            entry = _render_one(lyric, beat);
            sing_logf("push_lyric: rendered %zu samples, F0=%.1f Hz, %zu marks",
                      entry.pcm.size(), entry.detected_f0, entry.pitch_marks.size());
        } else {
            sing_logf("push_lyric: beat=%.3f no lyric — inserting null entry", beat);
        }
        _sched_entries.push_back(std::move(entry));
    }

    void on_schedule_loaded() override {
        if (_sched_entries.empty()) return;
        sing_logf("on_schedule_loaded: publishing %zu schedule phoneme(s)",
                  _sched_entries.size());
        _old_seqs.push_back(std::move(_sched_entries));
        _sched_entries.clear();
        _current_seq.store(&_old_seqs.back(), std::memory_order_release);
        _next_syllable.store(0, std::memory_order_relaxed);
        _transport_running.store(true, std::memory_order_relaxed);
        _set_state(State::Ready);
    }

    void on_seek(double beat) override {
        const PcmSeq* seq = _current_seq.load(std::memory_order_acquire);
        if (!seq || seq->empty()) {
            _next_syllable.store(0, std::memory_order_relaxed);
            return;
        }
        size_t idx = seq->size();
        for (size_t i = 0; i < seq->size(); ++i) {
            if ((*seq)[i].beat >= beat) { idx = i; break; }
        }
        _next_syllable.store(idx % seq->size(), std::memory_order_relaxed);
    }

    void activate(float sample_rate, int max_block_size) override {
        sing_logf("activate: sample_rate=%.0f block_size=%d", sample_rate, max_block_size);
        _set_state(State::Activating);
        _sample_rate = sample_rate;
        _init_espeak();
        // Compute sr_ratio AFTER _init_espeak so _espeak_sr is set
        _sr_ratio = sample_rate / static_cast<float>(_espeak_sr);
        sing_logf("activate: sr_ratio=%.4f (host=%g / espeak=%d)",
                   _sr_ratio, sample_rate, _espeak_sr);
        _rebuild_pcm_seq();
        sing_log("activate: done");
    }

    void deactivate() override {
        sing_log("deactivate: start");
        _set_state(State::Deactivated);
        {
            std::lock_guard<std::mutex> lk(_voice_mutex);
            _voices.clear();
        }
        _current_seq.store(nullptr, std::memory_order_seq_cst);
        _old_seqs.clear();
        _teardown_espeak();
        sing_log("deactivate: done");
    }

    // ------------------------------------------------------------------
    // note_on() — AUDIO THREAD
    void note_on(int /*ch*/, int pitch, int vel) override {
        if (vel == 0) { note_off(0, pitch); return; }

        const PcmSeq* seq = _current_seq.load(std::memory_order_acquire);
        if (!seq || seq->empty()) {
            _dropped_notes.fetch_add(1, std::memory_order_relaxed);
            return;
        }

        size_t idx;
        if (_transport_running.load(std::memory_order_relaxed))
            idx = _next_syllable.fetch_add(1, std::memory_order_relaxed) % seq->size();
        else
            idx = _next_syllable.load(std::memory_order_relaxed) % seq->size();
        const PcmEntry& pe = (*seq)[idx];
        if (pe.pcm.empty() || pe.pitch_marks.size() < 2) {
            _dropped_notes.fetch_add(1, std::memory_order_relaxed);
            return;
        }

        float target_hz = midi_to_hz(pitch);

        Voice v;
        v.entry           = &pe;
        v.target_period   = static_cast<double>(_espeak_sr) / target_hz;
        v.analysis_period = static_cast<double>(_espeak_sr) / pe.detected_f0;
        v.synth_time      = 0.0;
        v.analysis_time   = 0.0;
        v.output_time     = 0.0;
        v.gain            = vel / 127.0f;
        v.active          = true;
        v.held            = true;
        v.midi_pitch      = pitch;
        v.overlap_len     = 0;

        _fired_notes.fetch_add(1, std::memory_order_relaxed);

        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& slot : _voices) {
            if (!slot.active) { slot = v; return; }
        }
        if (_voices.size() < 16) _voices.push_back(v);
    }

    void note_off(int /*ch*/, int pitch) override {
        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& v : _voices) {
            if (v.active && v.held && v.midi_pitch == pitch) {
                v.held = false;
                break;
            }
        }
    }

    void all_notes_off(int /*channel*/) override {
        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& v : _voices) v.active = false;
    }

    // ------------------------------------------------------------------
    // process() — AUDIO THREAD, TD-PSOLA synthesis
    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* audio = buffers.audio.get("audio_out");
        if (!audio) return;

        std::fill(audio->left,  audio->left  + ctx.block_size, 0.0f);
        std::fill(audio->right, audio->right + ctx.block_size, 0.0f);

        std::lock_guard<std::mutex> lk(_voice_mutex);

        for (auto& v : _voices) {
            if (!v.active || !v.entry) continue;

            const PcmEntry& pe = *v.entry;
            const std::vector<short>& pcm = pe.pcm;
            const std::vector<int>& marks = pe.pitch_marks;
            int n_marks = static_cast<int>(marks.size());
            int pcm_len = static_cast<int>(pcm.size());

            // Add leftover overlap from previous block
            int overlap_add = std::min(v.overlap_len, ctx.block_size);
            for (int i = 0; i < overlap_add; ++i) {
                audio->left[i]  += v.overlap[i] * v.gain;
                audio->right[i] += v.overlap[i] * v.gain;
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

            double block_start_esp = v.output_time;
            double block_end_esp   = v.output_time +
                ctx.block_size / static_cast<double>(_sr_ratio);

            double target_period_host = v.target_period *
                static_cast<double>(_sr_ratio);
            int half_host = static_cast<int>(std::ceil(target_period_host));
            double period_ratio = v.analysis_period / v.target_period;

            // Place Hann-windowed grains at synthesis-mark intervals.
            int safety = 0;
            while (v.synth_time < block_end_esp && safety < 4096) {
                ++safety;

                // Find nearest pitch mark to current analysis_time
                int ai = _find_nearest_mark(marks, n_marks, v.analysis_time);

                // Handle end of source material
                if (ai < 0 || ai >= n_marks) {
                    if (v.held && pe.loop_end > pe.loop_start + 1 &&
                        pe.loop_start < n_marks && pe.loop_end <= n_marks) {
                        double loop_start_time = static_cast<double>(marks[pe.loop_start]);
                        double loop_end_time = static_cast<double>(marks[pe.loop_end - 1]);
                        double loop_len = loop_end_time - loop_start_time;
                        if (loop_len > 0) {
                            v.analysis_time = loop_start_time +
                                std::fmod(v.analysis_time - loop_end_time, loop_len);
                            if (v.analysis_time < loop_start_time)
                                v.analysis_time += loop_len;
                        } else {
                            v.analysis_time = loop_start_time;
                        }
                        ai = _find_nearest_mark(marks, n_marks, v.analysis_time);
                        if (ai < 0 || ai >= n_marks) {
                            v.active = false;
                            break;
                        }
                    } else {
                        if (v.overlap_len == 0) v.active = false;
                        break;
                    }
                }

                int center = marks[ai];

                double host_center = (v.synth_time - block_start_esp) *
                    static_cast<double>(_sr_ratio);

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
                    double src_pos = center +
                        (hi - host_center) / static_cast<double>(_sr_ratio)
                        * period_ratio;

                    int si = static_cast<int>(std::floor(src_pos));
                    float frac = static_cast<float>(src_pos - si);

                    float s0 = (si >= 0 && si < pcm_len)
                        ? pcm[si] * (1.0f / 32768.0f) : 0.0f;
                    float s1 = (si + 1 >= 0 && si + 1 < pcm_len)
                        ? pcm[si + 1] * (1.0f / 32768.0f) : 0.0f;
                    float sample = (s0 + frac * (s1 - s0)) * w;

                    if (hi >= 0 && hi < ctx.block_size) {
                        audio->left[hi]  += sample * v.gain;
                        audio->right[hi] += sample * v.gain;
                    } else if (hi >= ctx.block_size &&
                               hi < ctx.block_size + Voice::MAX_OVERLAP) {
                        int oi = hi - ctx.block_size;
                        if (oi >= v.overlap_len) {
                            for (int z = v.overlap_len; z < oi; ++z)
                                v.overlap[z] = 0.0f;
                            v.overlap_len = oi + 1;
                        }
                        v.overlap[oi] += sample;
                    }
                }

                v.synth_time    += v.target_period;
                v.analysis_time += v.target_period;
            }

            // Look-ahead: render grains just past block_end whose backward
            // tails reach into this block. Only write to block, not overlap.
            {
                double la_synth = v.synth_time;
                double la_analysis = v.analysis_time;
                int la_safety = 0;
                while (la_synth < block_end_esp +
                       v.target_period && la_safety < 16) {
                    ++la_safety;
                    int ai = _find_nearest_mark(marks, n_marks, la_analysis);
                    if (ai < 0 || ai >= n_marks) break;
                    int center = marks[ai];
                    double host_center = (la_synth - block_start_esp) *
                        static_cast<double>(_sr_ratio);
                    int host_start = static_cast<int>(std::floor(host_center)) - half_host;
                    int host_end   = static_cast<int>(std::ceil(host_center))  + half_host;
                    int grain_host_len = host_end - host_start;
                    if (grain_host_len < 1) grain_host_len = 1;

                    for (int hi = std::max(0, host_start);
                         hi < std::min(ctx.block_size, host_end + 1); ++hi) {
                        float t_win = static_cast<float>(hi - host_start) /
                                      static_cast<float>(grain_host_len);
                        float w = 0.5f * (1.0f - std::cos(
                            2.0f * static_cast<float>(M_PI) * t_win));
                        double src_pos = center +
                            (hi - host_center) / static_cast<double>(_sr_ratio)
                            * period_ratio;
                        int si = static_cast<int>(std::floor(src_pos));
                        float frac = static_cast<float>(src_pos - si);
                        float s0 = (si >= 0 && si < pcm_len)
                            ? pcm[si] * (1.0f / 32768.0f) : 0.0f;
                        float s1 = (si + 1 >= 0 && si + 1 < pcm_len)
                            ? pcm[si + 1] * (1.0f / 32768.0f) : 0.0f;
                        float sample = (s0 + frac * (s1 - s0)) * w;
                        audio->left[hi]  += sample * v.gain;
                        audio->right[hi] += sample * v.gain;
                    }
                    la_synth += v.target_period;
                    la_analysis += v.target_period;
                }
            }

            v.output_time = block_end_esp;
        }

        // Soft clip
        for (int i = 0; i < ctx.block_size; ++i) {
            if (audio->left[i]  >  0.95f || audio->left[i]  < -0.95f)
                audio->left[i]  = std::tanh(audio->left[i]);
            if (audio->right[i] >  0.95f || audio->right[i] < -0.95f)
                audio->right[i] = std::tanh(audio->right[i]);
        }

        // Debug: capture PSOLA output (audio thread, but only appending to a
        // vector — acceptable for debug, not production)
        if (_debug_dump) {
            // Start capturing on first non-silent block
            if (!_debug_capturing) {
                for (int i = 0; i < ctx.block_size; ++i) {
                    if (audio->left[i] != 0.0f) {
                        _debug_capturing = true;
                        break;
                    }
                }
            }
            if (_debug_capturing) {
                _debug_output_buf.insert(_debug_output_buf.end(),
                                          audio->left,
                                          audio->left + ctx.block_size);
            }
        }
    }

    void on_transport_stop() override {
        {
            std::lock_guard<std::mutex> lk(_voice_mutex);
            for (auto& v : _voices) v.active = false;
        }
        _next_syllable.store(0, std::memory_order_relaxed);
        _transport_running.store(false, std::memory_order_relaxed);

        // Flush captured PSOLA output
        if (_debug_dump && !_debug_output_buf.empty()) {
            char path[256];
            std::snprintf(path, sizeof(path),
                          "/tmp/singing_debug/psola_output_%02d.wav",
                          _debug_dump_counter++);
            write_wav_f32(path, _debug_output_buf.data(),
                          static_cast<int>(_debug_output_buf.size()),
                          static_cast<int>(_sample_rate));
            sing_logf("on_transport_stop: dumped %zu PSOLA samples → %s",
                      _debug_output_buf.size(), path);
            _debug_output_buf.clear();
            _debug_capturing = false;
        }
    }

    std::string get_graph_data(const std::string& /*port_id*/) override {
        const PcmSeq* seq = _current_seq.load(std::memory_order_acquire);
        size_t syl_count  = seq ? seq->size() : 0;

        char buf[512];
        std::snprintf(buf, sizeof(buf),
            "{"
            "\"state\":\"%s\","
            "\"espeak_ok\":%s,"
            "\"syllable_count\":%zu,"
            "\"next_syllable\":%zu,"
            "\"fired_notes\":%zu,"
            "\"dropped_notes\":%zu,"
            "\"last_render_ms\":%ld"
            "}",
            state_name(static_cast<State>(_state.load())),
            _espeak_initialised ? "true" : "false",
            syl_count,
            _next_syllable.load(std::memory_order_relaxed) % (syl_count ? syl_count : 1),
            _fired_notes.load(std::memory_order_relaxed),
            _dropped_notes.load(std::memory_order_relaxed),
            _last_render_ms.load()
        );
        return buf;
    }

private:
    // ------------------------------------------------------------------
    // ------------------------------------------------------------------
    // Find the pitch mark index nearest to the given time (in samples).
    // Returns -1 if analysis_time is past the end of the mark array.
    static int _find_nearest_mark(const std::vector<int>& marks, int n_marks,
                                   double analysis_time) {
        if (n_marks == 0) return -1;
        int t = static_cast<int>(std::round(analysis_time));
        if (t > marks[n_marks - 1]) return n_marks;  // past end

        // Binary search for nearest mark
        int lo = 0, hi = n_marks - 1;
        while (lo < hi) {
            int mid = (lo + hi) / 2;
            if (marks[mid] < t)
                lo = mid + 1;
            else
                hi = mid;
        }
        // lo is the first mark >= t; check if lo-1 is closer
        if (lo > 0 && (t - marks[lo - 1]) < (marks[lo] - t))
            return lo - 1;
        return lo;
    }

    struct Voice {
        const PcmEntry* entry = nullptr;

        // Synthesis time: where we place grains in the output stream.
        // Advances by target_period per grain → controls output pitch.
        double synth_time    = 0.0;
        double target_period = 0.0;

        // Analysis time: where we read grains from the source PCM.
        // Advances by analysis_period per grain → controls playback speed.
        // With analysis_period = source period and time_ratio = 1.0,
        // the source is consumed at real-time rate (no time stretch).
        double analysis_time = 0.0;
        double analysis_period = 0.0;  // = ESPEAK_SR / detected_f0

        double output_time   = 0.0;    // tracks block boundaries in espeak-sample space

        static constexpr int MAX_OVERLAP = 4096;
        float  overlap[MAX_OVERLAP] = {};
        int    overlap_len = 0;

        float  gain       = 1.0f;
        bool   active     = false;
        bool   held       = true;
        int    midi_pitch = 0;
    };

    float               _sample_rate = 44100.0f;
    int                 _espeak_sr   = ESPEAK_SAMPLE_RATE_DEFAULT;
    float               _sr_ratio    = 44100.0f / ESPEAK_SAMPLE_RATE_DEFAULT;

    std::mutex          _voice_mutex;
    std::vector<Voice>  _voices;

    std::string      _pending_lyrics;
    PcmSeq           _sched_entries;
    std::string      _voice              = "en";
    bool             _espeak_initialised = false;

    std::atomic<const PcmSeq*>  _current_seq{nullptr};
    std::list<PcmSeq>           _old_seqs;

    std::atomic<size_t>  _next_syllable{0};
    std::atomic<bool>    _transport_running{false};

    std::atomic<int>    _state{static_cast<int>(State::Created)};
    std::atomic<size_t> _fired_notes{0};
    std::atomic<size_t> _dropped_notes{0};
    std::atomic<long>   _last_render_ms{0};

    // Debug dump
    bool                _debug_dump = false;
    int                 _debug_dump_counter = 0;
    // PSOLA output capture: accumulate on audio thread, flush on transport stop.
    // Only active when _debug_dump is true.
    std::vector<float>  _debug_output_buf;
    bool                _debug_capturing = false;

    void _set_state(State s) {
        sing_logf("state: %s → %s",
                  state_name(static_cast<State>(_state.load())),
                  state_name(s));
        _state.store(static_cast<int>(s));
    }

    // ------------------------------------------------------------------
    void _init_espeak() {
        if (_espeak_initialised) {
            sing_log("_init_espeak: already initialised, skipping");
            return;
        }
        _set_state(State::InitEspeak);
        auto t0 = std::chrono::steady_clock::now();

        if (s_espeak_refcount.fetch_add(1, std::memory_order_seq_cst) == 0) {
            sing_log("_init_espeak: calling espeak_ng_InitializePath");
            espeak_ng_InitializePath(nullptr);

            sing_log("_init_espeak: calling espeak_ng_Initialize");
            espeak_ng_STATUS status = espeak_ng_Initialize(nullptr);
            sing_logf("_init_espeak: espeak_ng_Initialize returned %d (ENS_OK=%d)",
                      (int)status, (int)ENS_OK);
            if (status != ENS_OK) {
                sing_logf("_init_espeak: FAILED — espeak_ng_Initialize returned %d", (int)status);
                s_espeak_refcount.fetch_sub(1, std::memory_order_seq_cst);
                _set_state(State::Error);
                return;
            }

            sing_log("_init_espeak: calling espeak_ng_InitializeOutput");
            espeak_ng_InitializeOutput(ENOUTPUT_MODE_SYNCHRONOUS, 0, nullptr);

            sing_log("_init_espeak: calling espeak_SetSynthCallback");
            espeak_SetSynthCallback(espeak_synth_callback);
        } else {
            sing_log("_init_espeak: espeak already globally initialised, skipping");
        }

        _espeak_initialised = true;

        // espeak-ng defaults to 22050 Hz. There's no clean API to query
        // the actual sample rate, so we use the default. If a build uses
        // a non-standard rate, ESPEAK_SAMPLE_RATE_DEFAULT should be updated.
        _espeak_sr = ESPEAK_SAMPLE_RATE_DEFAULT;
        sing_logf("_init_espeak: using espeak sample rate = %d", _espeak_sr);

        sing_logf("_init_espeak: done in %ldms", ms_since(t0));
    }

    void _teardown_espeak() {
        if (!_espeak_initialised) return;
        _espeak_initialised = false;
        if (s_espeak_refcount.fetch_sub(1, std::memory_order_seq_cst) == 1) {
            sing_log("_teardown_espeak: calling espeak_ng_Terminate");
            espeak_ng_Terminate();
            sing_log("_teardown_espeak: done");
        } else {
            sing_log("_teardown_espeak: espeak still held by another instance, skipping Terminate");
        }
    }

    // Compute sustain loop region — vowel nucleus heuristic.
    static void _find_loop_region(PcmEntry& entry) {
        int n = static_cast<int>(entry.pitch_marks.size());
        if (n < 4) {
            entry.loop_start = 0;
            entry.loop_end   = n;
            return;
        }
        entry.loop_start = n * 40 / 100;
        entry.loop_end   = n * 80 / 100;
        if (entry.loop_end <= entry.loop_start + 1)
            entry.loop_end = std::min(n, entry.loop_start + 2);
    }

    void _rebuild_pcm_seq() {
        if (!_espeak_initialised) {
            sing_log("_rebuild_pcm_seq: skipped (espeak not initialised)");
            return;
        }

        auto words = split_words(_pending_lyrics);
        sing_logf("_rebuild_pcm_seq: %zu word(s) to render: '%s'",
                  words.size(), _pending_lyrics.substr(0, 60).c_str());

        if (words.empty()) {
            _current_seq.store(nullptr, std::memory_order_release);
            _next_syllable.store(0, std::memory_order_relaxed);
            _set_state(State::Ready);
            return;
        }

        _set_state(State::Rendering);
        auto t_total = std::chrono::steady_clock::now();

        PcmSeq seq;
        seq.reserve(words.size());
        for (size_t i = 0; i < words.size(); ++i) {
            sing_logf("_rebuild_pcm_seq: rendering word %zu/%zu: '%s'",
                      i + 1, words.size(), words[i].c_str());
            auto t_word = std::chrono::steady_clock::now();
            PcmEntry entry = _render_one(words[i], 0.0);
            sing_logf("_rebuild_pcm_seq: '%s' done — %zu samples, F0=%.1f Hz in %ldms",
                      words[i].c_str(), entry.pcm.size(), entry.detected_f0, ms_since(t_word));
            seq.push_back(std::move(entry));
        }

        long total_ms = ms_since(t_total);
        _last_render_ms.store(total_ms);
        sing_logf("_rebuild_pcm_seq: all done in %ldms", total_ms);

        _old_seqs.push_back(std::move(seq));
        _current_seq.store(&_old_seqs.back(), std::memory_order_release);
        _next_syllable.store(0, std::memory_order_relaxed);
        _set_state(State::Ready);
    }

    // Render one syllable at a fixed known pitch.  MAIN THREAD ONLY.
    // We use a fixed reference pitch so the PSOLA pitch ratio is always
    // computed from a known F0, eliminating the fragile YIN detection.
    static constexpr float REFERENCE_F0 = 200.0f;  // Hz — espeak target pitch

    PcmEntry _render_one(const std::string& syllable, double beat) {
        PcmEntry entry;
        entry.beat = beat;

        {
            sing_logf("_render_one: acquiring espeak mutex for '%s'...",
                      syllable.c_str());
            std::lock_guard<std::mutex> lk(s_espeak_mutex);

            std::vector<short> buf;
            s_espeak_buf = &buf;

            espeak_SetVoiceByName(_voice.c_str());

            // Request a specific pitch from espeak via SSML prosody.
            // This gives us a known F0 so the PSOLA period_ratio is reliable.
            char pitch_str[32];
            std::snprintf(pitch_str, sizeof(pitch_str), "%.0f", REFERENCE_F0);
            std::string ssml = "<speak><prosody rate=\"slow\" pitch=\""
                               + std::string(pitch_str) + "Hz\">"
                               + syllable + "</prosody></speak>";

            espeak_Synth(ssml.c_str(), ssml.size() + 1,
                         0, POS_CHARACTER, 0,
                         espeakCHARS_UTF8 | espeakSSML,
                         nullptr, nullptr);
            espeak_Synchronize();
            s_espeak_buf = nullptr;

            entry.pcm = std::move(buf);
        }

        // Use the known reference pitch instead of YIN detection.
        // YIN on speech is unreliable (varying F0, unvoiced segments).
        entry.detected_f0 = REFERENCE_F0;

        // Pitch marks at the known period
        entry.pitch_marks = find_pitch_marks(entry.pcm.data(),
                                              static_cast<int>(entry.pcm.size()),
                                              entry.detected_f0, _espeak_sr);

        // Loop region
        _find_loop_region(entry);

        sing_logf("_render_one: '%s' — %zu samples, F0=%.1f Hz, %zu marks, loop=[%d,%d]",
                  syllable.c_str(), entry.pcm.size(), entry.detected_f0,
                  entry.pitch_marks.size(), entry.loop_start, entry.loop_end);

        // Debug dump: raw espeak PCM + pitch-mark annotated version
        if (_debug_dump && !entry.pcm.empty()) {
            int idx = _debug_dump_counter++;
            char path[256];

            // Raw espeak output
            std::snprintf(path, sizeof(path),
                          "/tmp/singing_debug/%02d_raw_%s.wav",
                          idx, syllable.c_str());
            write_wav_s16(path, entry.pcm.data(),
                          static_cast<int>(entry.pcm.size()), _espeak_sr);
            sing_logf("_render_one: dumped raw → %s", path);

            // Copy with clicks at pitch marks so you can see/hear them
            std::vector<short> marked = entry.pcm;
            for (int m : entry.pitch_marks) {
                if (m >= 0 && m < static_cast<int>(marked.size()))
                    marked[m] = 32767;  // spike at each mark
            }
            std::snprintf(path, sizeof(path),
                          "/tmp/singing_debug/%02d_marks_%s.wav",
                          idx, syllable.c_str());
            write_wav_s16(path, marked.data(),
                          static_cast<int>(marked.size()), _espeak_sr);
            sing_logf("_render_one: dumped marks → %s", path);
        }

        return entry;
    }
};

REGISTER_PLUGIN(SingingPlugin);
REGISTER_PLUGIN_DYNAMIC(SingingPlugin);

std::unique_ptr<Plugin> make_singing_plugin() { return std::make_unique<SingingPlugin>(); }

#endif // AS_ENABLE_ESPEAK
