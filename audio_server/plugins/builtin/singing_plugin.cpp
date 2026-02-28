// singing_plugin.cpp
// espeak-ng based singing synthesiser.
//
// Each note-on pops the next syllable from the configured lyrics sequence,
// pre-renders it to a PCM buffer via espeak-ng (if not already cached), then
// plays it back with simple linear-resampling pitch shifting so the phonemes
// land on the requested MIDI pitch.
//
// Quality is intentionally "robotic" — this is a proof-of-concept for testing
// whether espeak-ng phoneme audio is a usable starting point for singing
// synthesis.  Resampling changes pitch by altering playback speed which also
// shifts formant frequencies (chipmunk/demon effect).  A future version could
// use PSOLA or a vocoder to separate pitch from timbre.
//
// Only compiled when AS_ENABLE_ESPEAK is defined (CMake ENABLE_ESPEAK option).

#ifdef AS_ENABLE_ESPEAK

#include "plugin_api.h"

#include <espeak-ng/espeak_ng.h>

#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstring>
#include <mutex>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

static constexpr int   ESPEAK_SAMPLE_RATE = 22050;  // espeak-ng default
static constexpr float MIDI_A4_FREQ       = 440.0f;
static constexpr int   MIDI_A4_NOTE       = 69;

// Approximate fundamental frequency of espeak's default voice (male, ~120 Hz).
// Used to compute the resampling ratio needed to reach a target MIDI pitch.
static constexpr float ESPEAK_BASE_HZ = 120.0f;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static float midi_to_hz(int midi_note) {
    return MIDI_A4_FREQ * std::pow(2.0f, (midi_note - MIDI_A4_NOTE) / 12.0f);
}

// Split a string on whitespace, collapsing runs of whitespace.
static std::vector<std::string> split_words(const std::string& s) {
    std::vector<std::string> out;
    std::istringstream ss(s);
    std::string tok;
    while (ss >> tok) out.push_back(tok);
    return out;
}

// ---------------------------------------------------------------------------
// espeak-ng synthesis callback
// ---------------------------------------------------------------------------
// We drive espeak-ng synchronously from the main thread (configure/activate)
// and collect samples into a vector.

struct EspeakSynthState {
    std::vector<short> samples;
};

static int espeak_callback(short* wav, int numsamples, espeak_EVENT* events) {
    (void)events;
    if (!wav || numsamples <= 0) return 0;
    // The user-data pointer is set to an EspeakSynthState* by the caller.
    // espeak_ng passes it through all callback invocations for a single synth.
    // We use the thread-local approach: the callback stores into a thread-local
    // destination set by the synthesising thread.
    // (espeak_ng's SetSynthCallback is process-global, so we protect with a mutex.)
    // The pointer is captured via espeak_SetUserData / UserData field in events.
    if (events && events->user_data) {
        auto* state = static_cast<EspeakSynthState*>(events->user_data);
        state->samples.insert(state->samples.end(), wav, wav + numsamples);
    }
    return 0;
}

// ---------------------------------------------------------------------------
// SingingPlugin
// ---------------------------------------------------------------------------

class SingingPlugin final : public Plugin {
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
    void configure(const std::string& key, const std::string& value) override {
        if (key == "lyrics_sequence") {
            std::lock_guard<std::mutex> lk(_cfg_mutex);
            _lyrics_seq     = split_words(value);
            _syllable_cache.clear();
            _next_syllable  = 0;
        } else if (key == "voice") {
            std::lock_guard<std::mutex> lk(_cfg_mutex);
            _voice = value.empty() ? "en" : value;
            _syllable_cache.clear();
        } else if (key == "base_pitch_hz") {
            try {
                float v = std::stof(value);
                if (v > 1.0f) _base_pitch_hz.store(v);
            } catch (...) {}
        }
    }

    // ------------------------------------------------------------------
    void activate(float sample_rate, int /*max_block_size*/) override {
        _sample_rate = sample_rate;
        _init_espeak();
    }

    void deactivate() override {
        _teardown_espeak();
    }

    // ------------------------------------------------------------------
    void note_on(int /*ch*/, int pitch, int vel) override {
        if (vel == 0) { note_off(0, pitch); return; }

        std::string syllable;
        {
            std::lock_guard<std::mutex> lk(_cfg_mutex);
            if (_lyrics_seq.empty()) return;
            syllable = _lyrics_seq[_next_syllable % _lyrics_seq.size()];
            _next_syllable++;
        }

        // Get (or render) the cached PCM for this syllable+voice.
        const std::vector<short>& pcm = _get_pcm(syllable);
        if (pcm.empty()) return;

        // Compute pitch-shift ratio: target_hz / base_hz
        float target_hz   = midi_to_hz(pitch);
        float base_hz     = _base_pitch_hz.load();
        float pitch_ratio = base_hz / target_hz;   // >1 → slower = lower pitch
        float gain        = vel / 127.0f;

        // Start a new voice
        Voice v;
        v.pcm        = &pcm;
        v.pitch      = pitch;
        v.read_pos   = 0.0;
        v.pitch_ratio = pitch_ratio;
        v.gain       = gain;
        v.active     = true;

        std::lock_guard<std::mutex> lk(_voice_mutex);
        // Recycle a finished slot or append
        for (auto& slot : _voices) {
            if (!slot.active) { slot = v; return; }
        }
        if (_voices.size() < 16) _voices.push_back(v);
    }

    void note_off(int /*ch*/, int pitch) override {
        // Singing voices run to completion; note_off just lets them fade.
        // (No action needed — they'll finish when samples are exhausted.)
        (void)pitch;
    }

    void all_notes_off(int /*channel*/) override {
        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& v : _voices) v.active = false;
    }

    // ------------------------------------------------------------------
    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* audio = buffers.audio.get("audio_out");
        if (!audio) return;

        // Zero the output buffers first
        std::fill(audio->left,  audio->left  + ctx.block_size, 0.0f);
        std::fill(audio->right, audio->right + ctx.block_size, 0.0f);

        // Mix MIDI note events (note-on / note-off)
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

        // Render active voices
        std::lock_guard<std::mutex> lk(_voice_mutex);
        float sr_ratio = _sample_rate / static_cast<float>(ESPEAK_SAMPLE_RATE);

        for (auto& v : _voices) {
            if (!v.active || !v.pcm) continue;

            const std::vector<short>& pcm = *v.pcm;
            const double step = v.pitch_ratio / sr_ratio;   // src samples per output sample

            for (int i = 0; i < ctx.block_size; ++i) {
                size_t idx0 = static_cast<size_t>(v.read_pos);
                if (idx0 + 1 >= pcm.size()) { v.active = false; break; }

                // Linear interpolation between adjacent PCM samples
                double frac  = v.read_pos - idx0;
                float  s0    = pcm[idx0]     * (1.0f / 32768.0f);
                float  s1    = pcm[idx0 + 1] * (1.0f / 32768.0f);
                float  samp  = static_cast<float>(s0 + frac * (s1 - s0)) * v.gain;

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
        std::lock_guard<std::mutex> lk(_voice_mutex);
        for (auto& v : _voices) v.active = false;
        // Reset lyric index so replay starts from the beginning.
        std::lock_guard<std::mutex> lk2(_cfg_mutex);
        _next_syllable = 0;
    }

private:
    // ------------------------------------------------------------------
    // Voice state (audio thread)
    // ------------------------------------------------------------------
    struct Voice {
        const std::vector<short>* pcm   = nullptr;
        int    pitch      = 60;
        double read_pos   = 0.0;
        double pitch_ratio = 1.0;
        float  gain       = 1.0f;
        bool   active     = false;
    };

    float                       _sample_rate = 44100.0f;
    std::atomic<float>          _base_pitch_hz{ESPEAK_BASE_HZ};

    std::mutex                  _voice_mutex;
    std::vector<Voice>          _voices;

    // ------------------------------------------------------------------
    // Configuration (main thread, protected by _cfg_mutex)
    // ------------------------------------------------------------------
    std::mutex                  _cfg_mutex;
    std::vector<std::string>    _lyrics_seq;
    size_t                      _next_syllable = 0;
    std::string                 _voice         = "en";

    // Cache: syllable text → pre-rendered PCM at ESPEAK_SAMPLE_RATE
    std::unordered_map<std::string, std::vector<short>> _syllable_cache;

    // ------------------------------------------------------------------
    // espeak-ng lifecycle
    // ------------------------------------------------------------------

    bool _espeak_initialised = false;

    void _init_espeak() {
        if (_espeak_initialised) return;
        espeak_ng_InitializePath(nullptr);
        espeak_ng_ERROR_CODE err = espeak_ng_Initialize(nullptr);
        if (err != ENS_OK) return;
        espeak_ng_InitializeOutput(ENOUTPUT_MODE_SYNCHRONOUS, 0, nullptr);
        espeak_SetSynthCallback(espeak_callback);
        _espeak_initialised = true;
    }

    void _teardown_espeak() {
        if (!_espeak_initialised) return;
        espeak_ng_Terminate();
        _espeak_initialised = false;
    }

    // ------------------------------------------------------------------
    // Render a syllable to PCM (cached)
    // ------------------------------------------------------------------
    const std::vector<short>& _get_pcm(const std::string& syllable) {
        std::lock_guard<std::mutex> lk(_cfg_mutex);

        auto it = _syllable_cache.find(syllable);
        if (it != _syllable_cache.end()) return it->second;

        std::vector<short>& out = _syllable_cache[syllable];
        if (!_espeak_initialised) return out;

        // Set voice
        espeak_SetVoiceByName(_voice.c_str());

        // Prepare callback state: espeak_ng requires that user_data be
        // threaded through the event list.  We embed a pointer to `out`
        // in a local EspeakSynthState and set it as the UserData on the
        // synth call.  The callback retrieves it from events[0].user_data.
        EspeakSynthState synth_state;
        espeak_SetUserData(&synth_state);   // sets the user_data in events

        unsigned int flags = espeakCHARS_UTF8 | espeakSSML;
        // Wrap in SSML to suppress prosody variations (monotone helps for pitch shift)
        std::string ssml = "<speak><prosody pitch=\"medium\" rate=\"slow\">"
                           + syllable + "</prosody></speak>";

        espeak_Synth(ssml.c_str(),
                     ssml.size() + 1,
                     0,            // position (chars from start)
                     POS_CHARACTER,
                     0,            // end position (0 = all)
                     flags,
                     nullptr,      // unique_identifier
                     &synth_state);
        espeak_Synchronize();

        out = std::move(synth_state.samples);
        return out;
    }
};

REGISTER_PLUGIN(SingingPlugin);
REGISTER_PLUGIN_DYNAMIC(SingingPlugin);

std::unique_ptr<Plugin> make_singing_plugin() { return std::make_unique<SingingPlugin>(); }

#endif // AS_ENABLE_ESPEAK
