// sampler_plugin.cpp
// Sample-based synthesizer: loads an audio file and plays it back
// pitch-shifted and amplitude-scaled according to MIDI events.
//
// Pitch shifting is done by varying the playback rate relative to a
// configurable root note (default C4 = MIDI 60).  Each semitone doubles/halves
// the speed by 2^(1/12).  Cubic interpolation provides good quality at
// moderate pitch ratios.
//
// Polyphony: up to MAX_VOICES simultaneous voices (oldest stolen if exceeded).
//
// Envelope: simple linear ADSR per voice.
//
// Supported formats (via libsndfile): WAV, AIFF, OGG, FLAC, and anything else
// libsndfile can open.  MP3 is NOT supported by libsndfile; for MP3 support the
// user should convert to WAV/OGG first.
//
// Config params:
//   sample_path  – path to the audio file (string)
//   root_note    – MIDI note of the unshifted sample (default 60 = C4)
//   attack       – attack time in seconds (default 0.01)
//   decay        – decay time in seconds (default 0.1)
//   sustain      – sustain level 0..1 (default 0.8)
//   release      – release time in seconds (default 0.2)
//
// Runtime ports:
//   audio_out    – stereo audio output
//   gain         – output gain control [0..2], default 1.0

#include "plugin_api.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

// libsndfile — optional; compile with -DAS_ENABLE_SNDFILE
#ifdef AS_ENABLE_SNDFILE
#  include <sndfile.h>
#endif

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
static constexpr int MAX_VOICES = 32;

// ---------------------------------------------------------------------------
// Sample data (shared across all instances since it's read-only after load)
// Actually per-instance because configure() could change the path.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Voice
// ---------------------------------------------------------------------------
struct Voice {
    bool     active   = false;
    int      channel  = 0;
    int      pitch    = 0;
    double   pos      = 0.0;   // fractional sample position
    double   rate     = 1.0;   // playback rate (pitch ratio)
    float    vel_gain = 1.0f;  // velocity scaling

    // ADSR state
    enum class Stage { Attack, Decay, Sustain, Release, Off } stage = Stage::Off;
    float    env      = 0.0f;  // current envelope value
    float    env_rate = 0.0f;  // per-sample change
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
            "Sample-based synthesizer. Loads an audio file and plays it back "
            "pitch-shifted and velocity-scaled according to incoming MIDI events. "
            "Root note sets which MIDI pitch plays the sample at its original pitch. "
            "Supports WAV, AIFF, OGG, FLAC (via libsndfile).";
        d.author  = "builtin";
        d.version = 1;

        d.ports = {
            { "events_in", "Events In", "MIDI event input (note on/off, pitch bend, etc.).",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain",
              "Output gain multiplier. 1.0 = unity.",
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

    // -----------------------------------------------------------------------
    // Configuration (main thread)
    // -----------------------------------------------------------------------
    void configure(const std::string& key, const std::string& value) override {
        if (key == "sample_path") {
            pending_path_ = value;
            path_dirty_.store(true, std::memory_order_release);
        }
    }

    // -----------------------------------------------------------------------
    // Lifecycle
    // -----------------------------------------------------------------------
    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        for (auto& v : voices_) v = Voice{};
        // Load sample if path is already set
        if (path_dirty_.load()) {
            _load_sample(pending_path_);
            path_dirty_.store(false);
        }
    }

    void deactivate() override {
        sample_L_.clear();
        sample_R_.clear();
        sample_frames_ = 0;
    }

    // -----------------------------------------------------------------------
    // MIDI events (audio thread)
    // -----------------------------------------------------------------------
    void note_on(int channel, int pitch, int velocity) override {
        if (sample_frames_ == 0) return;
        if (velocity == 0) { note_off(channel, pitch); return; }

        // Check for path reload request
        if (path_dirty_.load(std::memory_order_acquire)) {
            _load_sample(pending_path_);
            path_dirty_.store(false, std::memory_order_release);
        }

        // Find a free voice; steal oldest if all busy
        Voice* v = _find_free_voice();
        if (!v) v = _steal_voice();
        if (!v) return;

        // root_note_cached_ is updated by process() each block
        int root = root_note_cached_.load();
        double semitones = static_cast<double>(pitch - root);
        v->active   = true;
        v->channel  = channel;
        v->pitch    = pitch;
        v->pos      = 0.0;
        v->rate     = std::pow(2.0, semitones / 12.0);
        v->vel_gain = velocity / 127.0f;
        v->stage    = Voice::Stage::Attack;
        v->env      = 0.0f;
        float att = std::max(0.001f, att_cached_.load());
        v->env_rate = 1.0f / (att * sample_rate_);
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

        // Read control port values (or use atomics if disconnected/undriven)
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

        // Cache for note_on/note_off (which run on audio thread between blocks)
        root_note_cached_.store(root, std::memory_order_relaxed);
        att_cached_.store(att_s, std::memory_order_relaxed);
        rel_cached_.store(rel_s, std::memory_order_relaxed);

        bool stereo = (sample_R_.size() == static_cast<size_t>(sample_frames_));

        for (auto& v : voices_) {
            if (!v.active) continue;

            for (int i = 0; i < ctx.block_size; ++i) {
                // Envelope
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

                if (!v.active) break;

                // Cubic interpolation
                double fp = v.pos;
                long   ip = static_cast<long>(fp);
                float  t  = static_cast<float>(fp - static_cast<double>(ip));

                auto sampleL = [&](long n) -> float {
                    if (n < 0) n = 0;
                    if (n >= sample_frames_) n = sample_frames_ - 1;
                    return sample_L_[static_cast<size_t>(n)];
                };
                auto sampleR = [&](long n) -> float {
                    if (!stereo) return sampleL(n);
                    if (n < 0) n = 0;
                    if (n >= sample_frames_) n = sample_frames_ - 1;
                    return sample_R_[static_cast<size_t>(n)];
                };

                auto cubic = [&](float y0, float y1, float y2, float y3, float tc) {
                    float a0 = -0.5f*y0 + 1.5f*y1 - 1.5f*y2 + 0.5f*y3;
                    float a1 =       y0 - 2.5f*y1 + 2.0f*y2 - 0.5f*y3;
                    float a2 = -0.5f*y0            + 0.5f*y2;
                    float a3 =                  y1;
                    return ((a0*tc + a1)*tc + a2)*tc + a3;
                };

                float sl = cubic(sampleL(ip-1), sampleL(ip), sampleL(ip+1), sampleL(ip+2), t);
                float sr = cubic(sampleR(ip-1), sampleR(ip), sampleR(ip+1), sampleR(ip+2), t);

                float amp = env * v.vel_gain * gain;
                L[i] += sl * amp;
                if (out->right) R[i] += sr * amp;

                // Advance playback position
                v.pos += v.rate;
                if (v.pos >= static_cast<double>(sample_frames_)) {
                    // End of sample — enter release
                    v.active = false;
                    v.stage  = Voice::Stage::Off;
                    break;
                }
            }
        }

        // Soft clip to prevent clipping
        for (int i = 0; i < ctx.block_size; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    // -----------------------------------------------------------------------
    // Sample loading  (called on audio thread at first note after configure)
    // or on activate().  libsndfile is NOT realtime-safe, but loading only
    // happens once per path change (during activate or at first note if lazy).
    // -----------------------------------------------------------------------
    void _load_sample(const std::string& path) {
        sample_L_.clear();
        sample_R_.clear();
        sample_frames_ = 0;

        if (path.empty()) return;

#ifdef AS_ENABLE_SNDFILE
        SF_INFO info{};
        SNDFILE* sf = sf_open(path.c_str(), SFM_READ, &info);
        if (!sf) {
            // Silently fail — no audio until a valid file is set
            return;
        }

        long frames = static_cast<long>(info.frames);
        int  ch     = info.channels;

        // Read interleaved floats
        std::vector<float> interleaved(static_cast<size_t>(frames) * ch);
        sf_count_t got = sf_readf_float(sf, interleaved.data(), frames);
        sf_close(sf);

        frames = static_cast<long>(got);  // actual frames read

        // Resample if file SR != engine SR
        // Simple linear resampling for now (good enough for ±2 octave shifts)
        double ratio = static_cast<double>(info.samplerate) / static_cast<double>(sample_rate_);
        long   out_frames = static_cast<long>(std::ceil(frames / ratio));

        sample_L_.resize(static_cast<size_t>(out_frames));
        sample_R_.resize(static_cast<size_t>(out_frames));

        for (long i = 0; i < out_frames; ++i) {
            double src_pos = static_cast<double>(i) * ratio;
            long   src_i   = static_cast<long>(src_pos);
            float  t       = static_cast<float>(src_pos - src_i);
            if (src_i >= frames - 1) src_i = frames - 1;

            // Left channel
            float s0L = interleaved[static_cast<size_t>(src_i * ch + 0)];
            float s1L = (src_i + 1 < frames)
                        ? interleaved[static_cast<size_t>((src_i+1) * ch + 0)]
                        : s0L;
            sample_L_[static_cast<size_t>(i)] = s0L + t * (s1L - s0L);

            // Right channel (use channel 1 if stereo, else mirror left)
            int ri = (ch >= 2) ? 1 : 0;
            float s0R = interleaved[static_cast<size_t>(src_i * ch + ri)];
            float s1R = (src_i + 1 < frames)
                        ? interleaved[static_cast<size_t>((src_i+1) * ch + ri)]
                        : s0R;
            sample_R_[static_cast<size_t>(i)] = s0R + t * (s1R - s0R);
        }

        sample_frames_ = out_frames;
#else
        (void)path;
        // libsndfile not available — sampler produces silence
#endif
    }

    Voice* _find_free_voice() {
        for (auto& v : voices_) if (!v.active) return &v;
        return nullptr;
    }

    // Steal the voice that started longest ago (earliest note-on).
    // Simple heuristic: just pick the first voice in Release or Sustain,
    // otherwise the first active voice.
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

    // Sample data (engine sample rate, not file SR)
    std::vector<float> sample_L_;
    std::vector<float> sample_R_;
    long               sample_frames_ = 0;

    // Path change signaling (configure() called from main thread)
    std::string              pending_path_;
    std::atomic<bool>        path_dirty_{false};

    // Cached control port values for note_on/note_off (updated by process())
    std::atomic<int>         root_note_cached_{60};
    std::atomic<float>       att_cached_{0.01f};
    std::atomic<float>       rel_cached_{0.2f};

    Voice voices_[MAX_VOICES] = {};
};

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------
REGISTER_PLUGIN(SamplerPlugin);
REGISTER_PLUGIN_DYNAMIC(SamplerPlugin);

std::unique_ptr<Plugin> make_sampler_plugin() {
    return std::make_unique<SamplerPlugin>();
}
