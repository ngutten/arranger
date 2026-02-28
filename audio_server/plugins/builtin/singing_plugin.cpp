// singing_plugin.cpp
// espeak-ng based singing synthesiser.
//
// THREADING MODEL
// ---------------
// configure() and activate() are called on the MAIN thread.
// process() and note_* are called on the AUDIO thread — must not block.
//
// All espeak synthesis happens in _rebuild_pcm_seq() on the main thread.
// The resulting PcmSeq is published via an atomic pointer.
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

// ---------------------------------------------------------------------------
// Diagnostic logging
// ---------------------------------------------------------------------------
// All output goes to stderr so it appears in the audio server's terminal.
// Format: [SINGING][tid=<id>][+<ms>ms] <message>

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

// Convenience: format then log
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

static constexpr int   ESPEAK_SAMPLE_RATE = 22050;
static constexpr float MIDI_A4_FREQ       = 440.0f;
static constexpr int   MIDI_A4_NOTE       = 69;
static constexpr float ESPEAK_BASE_HZ     = 120.0f;

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
    using PcmSeq = std::vector<std::vector<short>>;

    // States for diagnostic reporting (transitions logged to stderr)
    enum class State : int {
        Created       = 0,
        Activating    = 1,
        InitEspeak    = 2,
        Rendering     = 3,
        Ready         = 4,
        Error         = 5,
        Deactivated   = 6,
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
    SingingPlugin() {
        sing_log("constructor");
    }

    ~SingingPlugin() override {
        sing_log("destructor");
        _teardown_espeak();
    }

    // ------------------------------------------------------------------
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.singing";
        d.display_name = "Singing (espeak-ng)";
        d.category     = "Synth";
        d.doc =
            "Prototype singing synthesiser using espeak-ng phoneme rendering.\n"
            "Each successive note-on consumes the next syllable from the\n"
            "lyrics_sequence parameter.  Pitch is set by resampling the\n"
            "espeak audio to match the MIDI note frequency.\n\n"
            "Quality is intentionally robotic — this is a proof-of-concept.\n"
            "Right-click the plugin in the graph editor to configure lyrics.";
        d.author  = "builtin";
        d.version = 1;

        d.ports = {
            { "events_in",  "Events", "MIDI note input",
              PluginPortType::Event, PortRole::Input },
            { "audio_out",  "Audio",  "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
        };

        d.config_params = {
            { "voice",
              "Voice",
              "espeak-ng voice name (e.g. \"en\", \"en-us\", \"en+f3\").",
              ConfigType::String,
              "en" },
            { "base_pitch_hz",
              "Base pitch (Hz)",
              "Fundamental frequency of the configured voice at default settings.\n"
              "Used to compute the resampling ratio.  Typical: 80-130 Hz.",
              ConfigType::Float,
              "120.0" },
        };

        return d;
    }

    // ------------------------------------------------------------------
    // configure() — MAIN THREAD ONLY
    void configure(const std::string& key, const std::string& value) override {
        sing_logf("configure: key='%s' value='%.40s'", key.c_str(), value.c_str());

        if (key == "voice") {
            _voice = value.empty() ? "en" : value;
            _rebuild_pcm_seq();
        } else if (key == "base_pitch_hz") {
            try {
                float v = std::stof(value);
                if (v > 1.0f) {
                    _base_pitch_hz.store(v, std::memory_order_relaxed);
                    sing_logf("configure: base_pitch_hz = %.1f", v);
                }
            } catch (...) {
                sing_logf("configure: base_pitch_hz parse error for '%s'", value.c_str());
            }
        }
    }

    // ------------------------------------------------------------------
    // on_pattern_connected() — MAIN THREAD, called before activate()
    //
    // Extracts per-note lyrics from the connected pattern (notes are already
    // sorted by beat) and assembles them into _pending_lyrics so that the
    // activate() → _rebuild_pcm_seq() pass pre-renders the correct syllables.
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
        // Don't call _rebuild_pcm_seq() here — espeak isn't initialised yet.
        // activate() will call it once _init_espeak() has run.
    }

    // ------------------------------------------------------------------
    // activate() — MAIN THREAD ONLY
    void activate(float sample_rate, int max_block_size) override {
        sing_logf("activate: sample_rate=%.0f block_size=%d", sample_rate, max_block_size);
        _set_state(State::Activating);
        _sample_rate = sample_rate;
        _init_espeak();
        _rebuild_pcm_seq();
        sing_log("activate: done");
    }

    // deactivate() — MAIN THREAD ONLY
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
    // note_on() — AUDIO THREAD.  Must not block, allocate, or call espeak.
    void note_on(int /*ch*/, int pitch, int vel) override {
        if (vel == 0) { note_off(0, pitch); return; }

        const PcmSeq* seq = _current_seq.load(std::memory_order_acquire);
        if (!seq || seq->empty()) {
            // Count dropped notes for diagnostic readout
            _dropped_notes.fetch_add(1, std::memory_order_relaxed);
            return;
        }

        size_t idx = _next_syllable.fetch_add(1, std::memory_order_relaxed)
                     % seq->size();
        const std::vector<short>& pcm = (*seq)[idx];
        if (pcm.empty()) {
            _dropped_notes.fetch_add(1, std::memory_order_relaxed);
            return;
        }

        float target_hz   = midi_to_hz(pitch);
        float base_hz     = _base_pitch_hz.load(std::memory_order_relaxed);
        float pitch_ratio = base_hz / target_hz;

        Voice v;
        v.pcm         = &pcm;
        v.read_pos    = 0.0;
        v.pitch_ratio = pitch_ratio;
        v.gain        = vel / 127.0f;
        v.active      = true;

        _fired_notes.fetch_add(1, std::memory_order_relaxed);

        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& slot : _voices) {
            if (!slot.active) { slot = v; return; }
        }
        if (_voices.size() < 16) _voices.push_back(v);
    }

    void note_off(int /*ch*/, int /*pitch*/) override {}

    void all_notes_off(int /*channel*/) override {
        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& v : _voices) v.active = false;
    }

    // ------------------------------------------------------------------
    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* audio = buffers.audio.get("audio_out");
        if (!audio) return;

        std::fill(audio->left,  audio->left  + ctx.block_size, 0.0f);
        std::fill(audio->right, audio->right + ctx.block_size, 0.0f);

        if (auto* evport = buffers.events.get("events_in")) {
            if (evport->events) {
                for (const auto& ev : *evport->events) {
                    uint8_t status = ev.status & 0xF0;
                    if (status == 0x90 && ev.data2 > 0)
                        note_on(ev.channel, ev.data1, ev.data2);
                    else if (status == 0x80 || (status == 0x90 && ev.data2 == 0))
                        note_off(ev.channel, ev.data1);
                }
            }
        }

        std::lock_guard<std::mutex> lk(_voice_mutex);
        float sr_ratio = _sample_rate / static_cast<float>(ESPEAK_SAMPLE_RATE);

        for (auto& v : _voices) {
            if (!v.active || !v.pcm) continue;

            const std::vector<short>& pcm = *v.pcm;
            double step = v.pitch_ratio / sr_ratio;

            for (int i = 0; i < ctx.block_size; ++i) {
                size_t idx0 = static_cast<size_t>(v.read_pos);
                if (idx0 + 1 >= pcm.size()) { v.active = false; break; }

                double frac = v.read_pos - static_cast<double>(idx0);
                float  s0   = pcm[idx0]     * (1.0f / 32768.0f);
                float  s1   = pcm[idx0 + 1] * (1.0f / 32768.0f);
                float  samp = static_cast<float>(s0 + frac * (s1 - s0)) * v.gain;

                audio->left[i]  += samp;
                audio->right[i] += samp;
                v.read_pos += step;
            }
        }

        for (int i = 0; i < ctx.block_size; ++i) {
            if (audio->left[i]  >  0.95f || audio->left[i]  < -0.95f)
                audio->left[i]  = std::tanh(audio->left[i]);
            if (audio->right[i] >  0.95f || audio->right[i] < -0.95f)
                audio->right[i] = std::tanh(audio->right[i]);
        }
    }

    void on_transport_stop() override {
        {
            std::lock_guard<std::mutex> lk(_voice_mutex);
            for (auto& v : _voices) v.active = false;
        }
        _next_syllable.store(0, std::memory_order_relaxed);
    }

    // ------------------------------------------------------------------
    // get_graph_data() — MAIN THREAD.
    // Returns a JSON blob the frontend can query to inspect plugin state.
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
    struct Voice {
        const std::vector<short>* pcm   = nullptr;
        double read_pos    = 0.0;
        double pitch_ratio = 1.0;
        float  gain        = 1.0f;
        bool   active      = false;
    };

    float               _sample_rate = 44100.0f;
    std::atomic<float>  _base_pitch_hz{ESPEAK_BASE_HZ};

    std::mutex          _voice_mutex;
    std::vector<Voice>  _voices;

    // Main-thread-only:
    std::string _pending_lyrics;
    std::string _voice              = "en";
    bool        _espeak_initialised = false;

    // Pre-rendered PCM: audio thread reads _current_seq atomically.
    // _old_seqs is a std::list (push_back never moves elements) so raw
    // pointers into its elements remain valid until deactivate().
    std::atomic<const PcmSeq*>  _current_seq{nullptr};
    std::list<PcmSeq>           _old_seqs;

    std::atomic<size_t>  _next_syllable{0};

    // Diagnostics (all atomic so get_graph_data can read from main thread
    // while audio thread increments)
    std::atomic<int>    _state{static_cast<int>(State::Created)};
    std::atomic<size_t> _fired_notes{0};
    std::atomic<size_t> _dropped_notes{0};
    std::atomic<long>   _last_render_ms{0};

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
            // First instance: actually initialise the process-global espeak state.
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
            // espeak-ng is a process-global singleton; a concurrent instance has
            // already initialised it.  Reuse the global state — do NOT call
            // espeak_ng_Initialize again or the internal thread bookkeeping
            // will be corrupted and espeak_ng_Terminate will deadlock.
            sing_log("_init_espeak: espeak already globally initialised (shared instance), skipping");
        }

        _espeak_initialised = true;
        sing_logf("_init_espeak: done in %ldms", ms_since(t0));
    }

    void _teardown_espeak() {
        if (!_espeak_initialised) return;
        _espeak_initialised = false;
        if (s_espeak_refcount.fetch_sub(1, std::memory_order_seq_cst) == 1) {
            // Last instance: safe to terminate the global espeak state.
            sing_log("_teardown_espeak: calling espeak_ng_Terminate");
            espeak_ng_Terminate();
            sing_log("_teardown_espeak: done");
        } else {
            sing_log("_teardown_espeak: espeak still held by another instance, skipping Terminate");
        }
    }

    // Pre-render all syllables and publish atomically.  MAIN THREAD ONLY.
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
            seq.push_back(_render_one(words[i]));
            sing_logf("_rebuild_pcm_seq: '%s' done — %zu samples in %ldms",
                      words[i].c_str(), seq.back().size(), ms_since(t_word));
        }

        long total_ms = ms_since(t_total);
        _last_render_ms.store(total_ms);
        sing_logf("_rebuild_pcm_seq: all done in %ldms", total_ms);

        // push_back on std::list never invalidates existing element addresses
        _old_seqs.push_back(std::move(seq));
        _current_seq.store(&_old_seqs.back(), std::memory_order_release);
        _next_syllable.store(0, std::memory_order_relaxed);
        _set_state(State::Ready);
    }

    // Render one syllable synchronously via espeak.  MAIN THREAD ONLY.
    std::vector<short> _render_one(const std::string& syllable) {
        std::lock_guard<std::mutex> lk(s_espeak_mutex);

        std::vector<short> buf;
        s_espeak_buf = &buf;

        sing_logf("_render_one: SetVoiceByName('%s')", _voice.c_str());
        espeak_SetVoiceByName(_voice.c_str());

        std::string ssml = "<speak><prosody rate=\"slow\">"
                           + syllable + "</prosody></speak>";

        sing_logf("_render_one: espeak_Synth for '%s'", syllable.c_str());
        espeak_Synth(ssml.c_str(), ssml.size() + 1,
                     0, POS_CHARACTER, 0,
                     espeakCHARS_UTF8 | espeakSSML,
                     nullptr, nullptr);

        sing_log("_render_one: espeak_Synchronize...");
        espeak_Synchronize();
        s_espeak_buf = nullptr;

        sing_logf("_render_one: done, %zu samples", buf.size());
        return buf;
    }
};

REGISTER_PLUGIN(SingingPlugin);
REGISTER_PLUGIN_DYNAMIC(SingingPlugin);

std::unique_ptr<Plugin> make_singing_plugin() { return std::make_unique<SingingPlugin>(); }

#endif // AS_ENABLE_ESPEAK
