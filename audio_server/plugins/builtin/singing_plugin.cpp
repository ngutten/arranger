// singing_plugin.cpp
// espeak-ng based singing synthesiser.
//
// Each note-on pops the next syllable from the configured lyrics sequence,
// looks it up in the pre-rendered PCM table, and plays it back with simple
// linear-resampling pitch shifting so the phonemes land on the requested MIDI
// pitch.
//
// THREADING MODEL
// ---------------
// configure() and activate() are called on the MAIN thread and may do I/O.
// process() and all note_* methods are called on the AUDIO thread and must
// not allocate, block, or call espeak.
//
// All espeak synthesis therefore happens in _rebuild_pcm_seq(), called from
// configure() / activate() on the main thread.  The resulting PcmSeq is
// published to the audio thread via an atomic pointer.  Old PcmSeqs are kept
// alive in _old_seqs (a std::list, so push_back never invalidates existing
// element addresses) until deactivate() drains all voices.
//
// Quality is intentionally "robotic" — this is a proof-of-concept.  A future
// version could use PSOLA or a vocoder to separate pitch from timbre.
//
// Only compiled when AS_ENABLE_ESPEAK is defined (CMake ENABLE_ESPEAK option).

#ifdef AS_ENABLE_ESPEAK

#include "plugin_api.h"

// espeak-ng public headers
#include <espeak-ng/espeak_ng.h>
#include <espeak-ng/speak_lib.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <list>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

static constexpr int   ESPEAK_SAMPLE_RATE = 22050;  // espeak-ng default
static constexpr float MIDI_A4_FREQ       = 440.0f;
static constexpr int   MIDI_A4_NOTE       = 69;

// Approximate fundamental frequency of espeak's default voice (male, ~120 Hz).
static constexpr float ESPEAK_BASE_HZ = 120.0f;

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

// ---------------------------------------------------------------------------
// Process-global espeak serialisation
// ---------------------------------------------------------------------------
// espeak-ng has a single process-wide synthesiser state and a global callback.
// All calls to espeak_Synth / espeak_Synchronize must be serialised across
// any SingingPlugin instances in the same process.

static std::mutex          s_espeak_mutex;
static std::vector<short>* s_espeak_buf = nullptr;

static int espeak_synth_callback(short* wav, int numsamples,
                                 espeak_EVENT* /*events*/) {
    if (wav && numsamples > 0 && s_espeak_buf)
        s_espeak_buf->insert(s_espeak_buf->end(), wav, wav + numsamples);
    return 0;  // 0 = continue synthesis
}

// ---------------------------------------------------------------------------
// SingingPlugin
// ---------------------------------------------------------------------------

class SingingPlugin final : public Plugin {
    // PcmSeq: one vector<short> per syllable position in the lyrics sequence.
    // Once created and published to the audio thread, a PcmSeq is never
    // modified.  Old instances are kept alive in _old_seqs so that Voice raw
    // pointers into them remain valid even after a re-configure.
    using PcmSeq = std::vector<std::vector<short>>;

public:
    ~SingingPlugin() override { _teardown_espeak(); }

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
            { "lyrics_sequence",
              "Lyrics sequence",
              "Space-separated syllables to sing, one per successive note-on.\n"
              "Example: \"hel lo world\"",
              ConfigType::String,
              "" },
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
    // configure() — MAIN THREAD ONLY.
    void configure(const std::string& key, const std::string& value) override {
        if (key == "lyrics_sequence") {
            _pending_lyrics = value;
            _rebuild_pcm_seq();
        } else if (key == "voice") {
            _voice = value.empty() ? "en" : value;
            _rebuild_pcm_seq();
        } else if (key == "base_pitch_hz") {
            try {
                float v = std::stof(value);
                if (v > 1.0f) _base_pitch_hz.store(v, std::memory_order_relaxed);
            } catch (...) {}
        }
    }

    // ------------------------------------------------------------------
    // activate() — MAIN THREAD ONLY.
    void activate(float sample_rate, int /*max_block_size*/) override {
        _sample_rate = sample_rate;
        _init_espeak();
        _rebuild_pcm_seq();   // pre-render if lyrics already configured
    }

    // deactivate() — MAIN THREAD ONLY.
    void deactivate() override {
        // Silence all voices before freeing PCM storage.
        {
            std::lock_guard<std::mutex> lk(_voice_mutex);
            _voices.clear();
        }
        _current_seq.store(nullptr, std::memory_order_seq_cst);
        _old_seqs.clear();
        _teardown_espeak();
    }

    // ------------------------------------------------------------------
    // note_on() — AUDIO THREAD.  Must not block, allocate, or call espeak.
    void note_on(int /*ch*/, int pitch, int vel) override {
        if (vel == 0) { note_off(0, pitch); return; }

        const PcmSeq* seq = _current_seq.load(std::memory_order_acquire);
        if (!seq || seq->empty()) return;

        size_t idx = _next_syllable.fetch_add(1, std::memory_order_relaxed)
                     % seq->size();
        const std::vector<short>& pcm = (*seq)[idx];
        if (pcm.empty()) return;

        float target_hz   = midi_to_hz(pitch);
        float base_hz     = _base_pitch_hz.load(std::memory_order_relaxed);
        float pitch_ratio = base_hz / target_hz;  // >1 → slower = lower pitch

        Voice v;
        v.pcm         = &pcm;
        v.read_pos    = 0.0;
        v.pitch_ratio = pitch_ratio;
        v.gain        = vel / 127.0f;
        v.active      = true;

        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& slot : _voices) {
            if (!slot.active) { slot = v; return; }
        }
        if (_voices.size() < 16) _voices.push_back(v);
    }

    void note_off(int /*ch*/, int /*pitch*/) override {
        // Voices run to sample exhaustion; note_off is a no-op here.
    }

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

        // Dispatch MIDI events from the event port
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

        // Mix active voices into the output buffer
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

        // Soft clip
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

    // Main-thread-only fields (never accessed from audio thread):
    std::string _pending_lyrics;
    std::string _voice         = "en";
    bool        _espeak_initialised = false;

    // Pre-rendered PCM sequences.
    //
    // _current_seq  — atomic raw pointer; audio thread reads with acquire,
    //                 main thread writes with release.  Points into _old_seqs.
    //
    // _old_seqs     — std::list so push_back never moves existing elements,
    //                 keeping raw pointers from _current_seq and Voice::pcm
    //                 stable.  Freed only in deactivate() after voices cleared.
    std::atomic<const PcmSeq*>  _current_seq{nullptr};
    std::list<PcmSeq>           _old_seqs;    // main thread only

    // Syllable position — incremented atomically by audio thread note_on().
    std::atomic<size_t> _next_syllable{0};

    // ------------------------------------------------------------------
    void _init_espeak() {
        if (_espeak_initialised) return;
        espeak_ng_InitializePath(nullptr);
        if (espeak_ng_Initialize(nullptr) != ENS_OK) return;
        espeak_ng_InitializeOutput(ENOUTPUT_MODE_SYNCHRONOUS, 0, nullptr);
        espeak_SetSynthCallback(espeak_synth_callback);
        _espeak_initialised = true;
    }

    void _teardown_espeak() {
        if (!_espeak_initialised) return;
        espeak_ng_Terminate();
        _espeak_initialised = false;
    }

    // Pre-render all syllables and publish atomically.  MAIN THREAD ONLY.
    void _rebuild_pcm_seq() {
        if (!_espeak_initialised) return;

        auto words = split_words(_pending_lyrics);
        if (words.empty()) {
            _current_seq.store(nullptr, std::memory_order_release);
            _next_syllable.store(0, std::memory_order_relaxed);
            return;
        }

        PcmSeq seq;
        seq.reserve(words.size());
        for (const auto& word : words)
            seq.push_back(_render_one(word));

        // push_back to a std::list never invalidates existing element addresses.
        _old_seqs.push_back(std::move(seq));
        _current_seq.store(&_old_seqs.back(), std::memory_order_release);
        _next_syllable.store(0, std::memory_order_relaxed);
    }

    // Render one syllable to PCM synchronously.  MAIN THREAD ONLY.
    // Holds s_espeak_mutex to serialise against any other plugin instance.
    std::vector<short> _render_one(const std::string& syllable) {
        std::lock_guard<std::mutex> lk(s_espeak_mutex);

        std::vector<short> buf;
        s_espeak_buf = &buf;

        espeak_SetVoiceByName(_voice.c_str());

        // SSML prosody wrapper: slower rate gives espeak more time to produce
        // a fuller phoneme, which survives pitch-shifting better.
        std::string ssml = "<speak><prosody rate=\"slow\">"
                           + syllable + "</prosody></speak>";

        espeak_Synth(ssml.c_str(),
                     ssml.size() + 1,
                     0,              // position
                     POS_CHARACTER,
                     0,              // end position (0 = all)
                     espeakCHARS_UTF8 | espeakSSML,
                     nullptr,        // unique_identifier
                     nullptr);       // user_data

        espeak_Synchronize();
        s_espeak_buf = nullptr;
        return buf;
    }
};

REGISTER_PLUGIN(SingingPlugin);
REGISTER_PLUGIN_DYNAMIC(SingingPlugin);

std::unique_ptr<Plugin> make_singing_plugin() { return std::make_unique<SingingPlugin>(); }

#endif // AS_ENABLE_ESPEAK
