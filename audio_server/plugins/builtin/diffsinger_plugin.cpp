// diffsinger_plugin.cpp
// Neural singing voice synthesis using DiffSinger models via ONNX Runtime.
//
// ARCHITECTURE
// ------------
// DiffSinger uses a diffusion-based acoustic model to generate mel spectrograms
// from phoneme sequences + pitch curves, then a neural vocoder (NSF-HiFiGAN)
// converts mel → waveform.
//
// PIPELINE (all on main thread during on_schedule_loaded):
//   1. Group notes into phrases (consecutive notes without long gaps)
//   2. Phonemize lyrics via espeak-ng → phoneme IDs per phrase
//   3. Run duration predictor ONNX model → phoneme durations (or compute from
//      beat-level durations via bpm/hop_size)
//   4. Build per-phrase F0 curve from MIDI note pitches
//   5. Run acoustic model on full phrase → mel spectrogram
//   6. Run vocoder on full phrase mel → PCM waveform
//   7. Index individual note boundaries within phrase PCM
//   8. Publish for audio thread playback
//
// PHRASE RENDERING
// ----------------
// Rather than rendering each note independently (which produces discontinuous
// mel at note boundaries), we group consecutive notes into phrases and render
// them as a single acoustic model + vocoder pass. This gives the diffusion
// model the full phrase context, producing smooth transitions between notes.
//
// Phrase boundaries are inserted when:
//   - Gap between consecutive notes > phrase_gap_beats (default 0.5 beats)
//   - Phrase length exceeds max_phrase_notes (default 64 notes)
//
// THREADING MODEL
// ---------------
// configure(), activate(), push_lyric(), on_schedule_loaded() → MAIN thread.
// process(), note_on/off → AUDIO thread. Just PCM playback, never blocks.
//
// Only compiled when AS_ENABLE_DIFFSINGER is defined.

#ifdef AS_ENABLE_DIFFSINGER

#include "plugin_api.h"

#include <onnxruntime_cxx_api.h>
#include <espeak-ng/espeak_ng.h>
#include <espeak-ng/speak_lib.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <list>
#include <mutex>
#include <numeric>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

static auto s_ds_t0 = std::chrono::steady_clock::now();

static void ds_log(const char* fmt, ...) {
    auto now = std::chrono::steady_clock::now();
    long ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now - s_ds_t0).count();
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    std::fprintf(stderr, "[DIFFSINGER][+%ldms] %s\n", ms, buf);
    std::fflush(stderr);
}

// ---------------------------------------------------------------------------
// Minimal dsconfig.yaml parser
// ---------------------------------------------------------------------------

struct DsConfig {
    int   sample_rate = 44100;
    int   hop_size    = 512;
    int   n_mels      = 128;
    float mel_fmin    = 40.0f;
    float mel_fmax    = 16000.0f;
    int   k_step      = 0;
    bool  use_shallow_diffusion = true;

    bool load(const std::string& path, std::string& err) {
        std::ifstream f(path);
        if (!f.is_open()) { err = "Cannot open dsconfig: " + path; return false; }
        std::string line;
        while (std::getline(f, line)) {
            auto pound = line.find('#');
            if (pound != std::string::npos) line.resize(pound);
            auto colon = line.find(':');
            if (colon == std::string::npos) continue;
            auto trim = [](std::string s) {
                size_t a = s.find_first_not_of(" \t\r\n");
                size_t b = s.find_last_not_of(" \t\r\n");
                return (a == std::string::npos) ? std::string() : s.substr(a, b - a + 1);
            };
            std::string key = trim(line.substr(0, colon));
            std::string val = trim(line.substr(colon + 1));
            if (key.empty() || val.empty()) continue;
            try {
                if      (key == "sample_rate") sample_rate = std::stoi(val);
                else if (key == "hop_size")    hop_size = std::stoi(val);
                else if (key == "num_mel_bins" || key == "n_mels") n_mels = std::stoi(val);
                else if (key == "fmin" || key == "mel_fmin") mel_fmin = std::stof(val);
                else if (key == "fmax" || key == "mel_fmax") mel_fmax = std::stof(val);
                else if (key == "K_step") k_step = std::stoi(val);
                else if (key == "use_shallow_diffusion")
                    use_shallow_diffusion = (val == "true" || val == "True" || val == "1");
            } catch (...) {}
        }
        ds_log("dsconfig: sr=%d hop=%d mels=%d fmin=%.0f fmax=%.0f k_step=%d",
               sample_rate, hop_size, n_mels, mel_fmin, mel_fmax, k_step);
        return true;
    }
};

// ---------------------------------------------------------------------------
// Phoneme dictionary (dsdict.txt)
// ---------------------------------------------------------------------------

static bool load_phoneme_dict(const std::string& path,
                              std::unordered_map<std::string, int64_t>& out,
                              std::string& err) {
    std::ifstream f(path);
    if (!f.is_open()) { err = "Cannot open dict: " + path; return false; }
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream ss(line);
        std::string phoneme; int64_t id;
        if (ss >> phoneme >> id) out[phoneme] = id;
    }
    ds_log("Loaded %zu phonemes from %s", out.size(), path.c_str());
    return true;
}

// ---------------------------------------------------------------------------
// Memory check
// ---------------------------------------------------------------------------

struct MemInfo { size_t gpu_free=0, gpu_total=0, ram_free=0, ram_total=0; bool gpu=false; };

static MemInfo query_memory() {
    MemInfo m;
#ifdef __linux__
    if (std::ifstream mi("/proc/meminfo"); mi.is_open()) {
        std::string line; size_t kb;
        while (std::getline(mi, line)) {
            if (line.find("MemTotal:") == 0 && std::sscanf(line.c_str(), "MemTotal: %zu kB", &kb) == 1)
                m.ram_total = kb * 1024;
            else if (line.find("MemAvailable:") == 0 && std::sscanf(line.c_str(), "MemAvailable: %zu kB", &kb) == 1)
                m.ram_free = kb * 1024;
        }
    }
    if (FILE* p = popen("nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader,nounits 2>/dev/null", "r")) {
        char buf[256]; size_t f_mb, t_mb;
        if (fgets(buf, sizeof(buf), p) && std::sscanf(buf, "%zu, %zu", &f_mb, &t_mb) == 2) {
            m.gpu_free = f_mb << 20; m.gpu_total = t_mb << 20; m.gpu = true;
        }
        pclose(p);
    }
#endif
    return m;
}

static size_t estimate_model_mem(const std::string& path) {
    if (path.empty()) return 0;
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    return f.is_open() ? static_cast<size_t>(f.tellg() * 1.5) : 0;
}

// ---------------------------------------------------------------------------
// espeak-ng (process-global, matches singing_plugin.cpp)
// ---------------------------------------------------------------------------

static std::mutex       s_espeak_mtx;
static std::atomic<int> s_espeak_rc{0};

static bool init_espeak() {
    if (s_espeak_rc.fetch_add(1, std::memory_order_seq_cst) == 0) {
        espeak_ng_InitializePath(nullptr);
        if (espeak_ng_Initialize(nullptr) != ENS_OK) {
            s_espeak_rc.fetch_sub(1); return false;
        }
        espeak_ng_InitializeOutput(ENOUTPUT_MODE_SYNCHRONOUS, 0, nullptr);
    }
    return true;
}
static void term_espeak() {
    if (s_espeak_rc.fetch_sub(1, std::memory_order_seq_cst) == 1)
        espeak_ng_Terminate();
}

static std::string phonemize(const std::string& text, const std::string& voice) {
    std::lock_guard<std::mutex> lk(s_espeak_mtx);
    espeak_SetVoiceByName(voice.c_str());
    const char* input = text.c_str();
    const char* ph = espeak_TextToPhonemes(
        reinterpret_cast<const void**>(&input), espeakCHARS_UTF8, 0x02);
    return ph ? std::string(ph) : std::string();
}

// ---------------------------------------------------------------------------
static float midi_to_hz(int note) { return 440.0f * std::pow(2.0f, (note - 69) / 12.0f); }

// ===========================================================================
// DiffSingerPlugin
// ===========================================================================

class DiffSingerPlugin final : public Plugin {

    // ---- Data types ----

    struct NoteInfo {
        double      beat;
        int         pitch;           // MIDI note, or -1 if unknown
        double      duration_beats;  // from paired NoteOff, 0 if unknown
        std::string lyric;
    };

    // A phrase is a group of consecutive notes rendered as a single acoustic
    // model pass. This gives the diffusion model full context for smooth
    // note-to-note transitions.
    struct Phrase {
        std::vector<NoteInfo> notes;
        double start_beat() const { return notes.front().beat; }
        double end_beat() const {
            auto& n = notes.back();
            return n.beat + (n.duration_beats > 0 ? n.duration_beats : 0.5);
        }
    };

    struct NoteEntry {
        double beat;
        int    pcm_start;
        int    pcm_length;
    };

    struct Render {
        std::vector<float>     pcm;
        int                    sample_rate = 44100;
        std::vector<NoteEntry> notes;
    };

    struct Voice {
        const Render* render = nullptr;
        int pcm_start=0, pcm_length=0, pcm_pos=0;
        float gain = 1.0f;
        bool active=false, held=true;
        int midi_pitch=0;
        int64_t birth=0;
        int fade_len=0, fade_pos=0;
    };

    enum class State : int { Created, Loading, Ready, Error, Deactivated };
    static const char* sname(State s) {
        const char* names[] = {"created","loading","ready","error","deactivated"};
        return names[static_cast<int>(s)];
    }

public:
    DiffSingerPlugin()  { ds_log("constructor"); }
    ~DiffSingerPlugin() override { if (espeak_ok_) term_espeak(); }

    // ==================================================================
    // Descriptor
    // ==================================================================

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.diffsinger";
        d.display_name = "DiffSinger (Neural SVS)";
        d.category     = "Synth";
        d.doc =
            "Neural singing voice synthesis using DiffSinger.\n"
            "Diffusion acoustic model + NSF-HiFiGAN vocoder.\n"
            "Pre-renders full phrases before playback.\n"
            "GPU acceleration with automatic CPU fallback.";
        d.author  = "builtin";
        d.version = 2;

        d.ports = {
            {"lyrics_in","Lyrics","Pattern with per-note lyrics",
             PluginPortType::Pattern, PortRole::Input},
            {"events_in","Events","MIDI note input",
             PluginPortType::Event, PortRole::Input},
            {"audio_out","Audio","Stereo output",
             PluginPortType::AudioStereo, PortRole::Output},
        };

        d.config_params = {
            {"acoustic_path","Acoustic Model","Path to acoustic ONNX",
             ConfigType::FilePath,""},
            {"vocoder_path","Vocoder Model","Path to NSF-HiFiGAN ONNX",
             ConfigType::FilePath,""},
            {"duration_path","Duration Model","Path to duration ONNX (optional)",
             ConfigType::FilePath,""},
            {"dsconfig_path","Config File","Path to dsconfig.yaml",
             ConfigType::FilePath,""},
            {"dict_path","Phoneme Dictionary","Path to dsdict.txt",
             ConfigType::FilePath,""},
            {"voice","Voice","espeak-ng voice (e.g. en-us)",
             ConfigType::String,"en-us"},
            {"speaker_id","Speaker ID","Speaker index (multi-speaker)",
             ConfigType::Integer,"0"},
            {"phrase_gap","Phrase Gap (beats)",
             "Beat gap threshold for phrase splitting",
             ConfigType::Float,"0.5"},
        };
        return d;
    }

    // ==================================================================
    // Configuration
    // ==================================================================

    void configure(const std::string& key, const std::string& val) override {
        ds_log("configure: %s = '%.60s'", key.c_str(), val.c_str());
        if      (key == "acoustic_path")  acoustic_path_ = val;
        else if (key == "vocoder_path")   vocoder_path_  = val;
        else if (key == "duration_path")  duration_path_ = val;
        else if (key == "dsconfig_path")  dsconfig_path_ = val;
        else if (key == "dict_path")      dict_path_     = val;
        else if (key == "voice")          voice_ = val.empty() ? "en-us" : val;
        else if (key == "speaker_id")  { try { speaker_id_ = std::stoi(val); } catch (...) {} }
        else if (key == "phrase_gap")  { try { phrase_gap_ = std::stof(val); } catch (...) {} }
    }

    // ==================================================================
    // Lifecycle
    // ==================================================================

    void activate(float sr, int /*bs*/) override {
        ds_log("activate: sr=%.0f", sr);
        set_state(State::Loading);
        host_sr_ = sr;

        espeak_ok_ = init_espeak();
        if (!espeak_ok_) ds_log("WARNING: espeak init failed");

        if (!dsconfig_path_.empty()) {
            std::string err;
            if (!cfg_.load(dsconfig_path_, err)) {
                error_ = err; set_state(State::Error); return;
            }
        }
        if (!dict_path_.empty()) {
            std::string err;
            if (!load_phoneme_dict(dict_path_, phoneme_dict_, err))
                ds_log("WARNING: %s", err.c_str());
        }

        choose_provider();
        if (!load_models()) { set_state(State::Error); return; }

        sr_ratio_ = host_sr_ / static_cast<float>(cfg_.sample_rate);
        set_state(State::Ready);
    }

    void deactivate() override {
        ds_log("deactivate");
        set_state(State::Deactivated);
        acoustic_sess_.reset(); vocoder_sess_.reset(); duration_sess_.reset();
        ort_env_.reset();
        current_render_.store(nullptr, std::memory_order_release);
        old_renders_.clear();
        if (espeak_ok_) { term_espeak(); espeak_ok_ = false; }
    }

    // ==================================================================
    // Schedule: accumulate notes, phrase-render in on_schedule_loaded()
    // ==================================================================

    void push_lyric(double beat, const std::string& lyric,
                    int pitch, double duration_beats) override {
        pending_.push_back({beat,
                            (pitch >= 0) ? pitch : 60,
                            duration_beats,
                            lyric});
    }

    void on_pattern_connected(const PatternData& pd) override {
        ds_log("on_pattern_connected: %zu notes", pd.notes.size());
        for (const auto& pn : pd.notes)
            pat_cache_.push_back({pn.beat, pn.pitch, pn.duration, pn.lyric});
    }

    void on_schedule_loaded() override {
        if (pending_.empty()) return;
        ds_log("on_schedule_loaded: %zu notes", pending_.size());
        auto t0 = std::chrono::steady_clock::now();

        resolve_pitches();

        // Group notes into phrases
        auto phrases = build_phrases(pending_);
        ds_log("  → %zu phrases", phrases.size());

        // Pre-render all phrases
        auto render = pre_render_phrases(phrases);
        pending_.clear();
        pat_cache_.clear();

        if (!render) { ds_log("pre-render FAILED"); return; }

        long ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t0).count();
        float dur_s = render->pcm.size() / static_cast<float>(cfg_.sample_rate);
        ds_log("Rendered %zu samples (%.1fs) in %ldms (%.1fx RT)",
               render->pcm.size(), dur_s, ms,
               (ms > 0) ? dur_s / (ms / 1000.0f) : 0.0f);

        old_renders_.push_back(std::move(*render));
        current_render_.store(&old_renders_.back(), std::memory_order_release);
        next_note_.store(0, std::memory_order_relaxed);
    }

    void on_seek(double beat) override {
        const Render* r = current_render_.load(std::memory_order_acquire);
        if (!r || r->notes.empty()) { next_note_.store(0); return; }
        // Binary search
        size_t lo = 0, hi = r->notes.size();
        while (lo < hi) {
            size_t mid = (lo + hi) / 2;
            if (r->notes[mid].beat < beat) lo = mid + 1; else hi = mid;
        }
        next_note_.store(lo % r->notes.size(), std::memory_order_relaxed);
    }

    // ==================================================================
    // Audio thread
    // ==================================================================

    void note_on(int, int pitch, int vel) override {
        if (vel == 0) { note_off(0, pitch); return; }
        const Render* r = current_render_.load(std::memory_order_acquire);
        if (!r || r->notes.empty()) return;

        size_t idx = next_note_.fetch_add(1) % r->notes.size();
        const auto& ne = r->notes[idx];
        if (ne.pcm_length == 0) return;

        std::lock_guard<std::mutex> lk(voice_mtx_);
        Voice* v = alloc_voice();
        v->render = r;  v->pcm_start = ne.pcm_start;  v->pcm_length = ne.pcm_length;
        v->pcm_pos = 0; v->gain = vel / 127.0f;
        v->active = true; v->held = true; v->midi_pitch = pitch;
        v->birth = total_samples_; v->fade_len = 0; v->fade_pos = 0;
    }

    void note_off(int, int pitch) override {
        std::lock_guard<std::mutex> lk(voice_mtx_);
        for (auto& v : voices_)
            if (v.active && v.held && v.midi_pitch == pitch) {
                v.held = false;
                v.fade_len = static_cast<int>(host_sr_ * 0.025f);
                v.fade_pos = 0;
                break;
            }
    }

    void all_notes_off(int) override {
        std::lock_guard<std::mutex> lk(voice_mtx_);
        for (auto& v : voices_) v.active = false;
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* audio = buffers.audio.get("audio_out");
        if (!audio) return;
        std::fill(audio->left,  audio->left  + ctx.block_size, 0.0f);
        std::fill(audio->right, audio->right + ctx.block_size, 0.0f);

        std::lock_guard<std::mutex> lk(voice_mtx_);
        for (auto& v : voices_) {
            if (!v.active || !v.render) continue;
            const float* src = v.render->pcm.data() + v.pcm_start;
            int len = v.pcm_length;

            for (int i = 0; i < ctx.block_size; ++i) {
                if (v.pcm_pos >= len) {
                    if (!v.held) v.active = false;
                    break;
                }

                // Resample model SR → host SR
                float s;
                if (std::abs(sr_ratio_ - 1.0f) < 0.001f) {
                    s = src[v.pcm_pos];
                } else {
                    double sf = v.pcm_pos / static_cast<double>(sr_ratio_);
                    int si = static_cast<int>(sf);
                    float frac = static_cast<float>(sf - si);
                    float s0 = (si < len) ? src[si] : 0.0f;
                    float s1 = (si+1 < len) ? src[si+1] : s0;
                    s = s0 + frac * (s1 - s0);
                }

                float env = v.gain;
                if (!v.held && v.fade_len > 0) {
                    float t = 1.0f - float(v.fade_pos) / float(v.fade_len);
                    if (t <= 0.0f) { v.active = false; break; }
                    env *= t * t;
                    v.fade_pos++;
                }
                audio->left[i]  += s * env;
                audio->right[i] += s * env;
                v.pcm_pos++;
            }
        }

        // Soft clip
        for (int i = 0; i < ctx.block_size; ++i) {
            auto sc = [](float x) { return (x > 0.95f || x < -0.95f) ? std::tanh(x) : x; };
            audio->left[i]  = sc(audio->left[i]);
            audio->right[i] = sc(audio->right[i]);
        }
        total_samples_ += ctx.block_size;
    }

    void on_transport_stop() override {
        { std::lock_guard<std::mutex> lk(voice_mtx_);
          for (auto& v : voices_) v.active = false; }
        next_note_.store(0, std::memory_order_relaxed);
    }

    std::string get_graph_data(const std::string&) override {
        const Render* r = current_render_.load(std::memory_order_acquire);
        char buf[512];
        std::snprintf(buf, sizeof(buf),
            "{\"state\":\"%s\",\"provider\":\"%s\",\"models_loaded\":%s,"
            "\"render_samples\":%zu,\"render_notes\":%zu,\"error\":\"%s\"}",
            sname(static_cast<State>(state_.load())),
            use_gpu_ ? "CUDA" : "CPU",
            acoustic_sess_ ? "true" : "false",
            r ? r->pcm.size() : size_t(0),
            r ? r->notes.size() : size_t(0),
            error_.c_str());
        return buf;
    }

private:
    static constexpr int MAX_VOICES = 8;
    static constexpr int MAX_PHRASE_NOTES = 64;

    // ---- Config ----
    std::string acoustic_path_, vocoder_path_, duration_path_;
    std::string dsconfig_path_, dict_path_;
    std::string voice_ = "en-us";
    int   speaker_id_ = 0;
    float phrase_gap_  = 0.5f;  // beats
    DsConfig cfg_;
    std::unordered_map<std::string, int64_t> phoneme_dict_;

    // ---- ONNX ----
    std::unique_ptr<Ort::Env>     ort_env_;
    std::unique_ptr<Ort::Session> acoustic_sess_, vocoder_sess_, duration_sess_;
    Ort::MemoryInfo mem_info_ = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
    bool use_gpu_ = false;

    // ---- State ----
    float host_sr_ = 44100.0f, sr_ratio_ = 1.0f;
    bool  espeak_ok_ = false;
    std::atomic<int> state_{static_cast<int>(State::Created)};
    std::string error_;
    void set_state(State s) { state_.store(static_cast<int>(s)); }

    // ---- Schedule (main thread) ----
    std::vector<NoteInfo> pending_;
    std::vector<NoteInfo> pat_cache_;

    // ---- Render (atomic publish) ----
    std::atomic<const Render*> current_render_{nullptr};
    std::list<Render> old_renders_;

    // ---- Audio thread ----
    std::mutex voice_mtx_;
    std::vector<Voice> voices_;
    std::atomic<size_t> next_note_{0};
    int64_t total_samples_ = 0;

    Voice* alloc_voice() {
        for (auto& v : voices_) if (!v.active) return &v;
        if ((int)voices_.size() < MAX_VOICES) { voices_.emplace_back(); return &voices_.back(); }
        Voice* oldest = &voices_[0];
        for (size_t i = 1; i < voices_.size(); ++i)
            if (voices_[i].birth < oldest->birth) oldest = &voices_[i];
        return oldest;
    }

    // ==================================================================
    // Resolve pitches from pattern cache when push_lyric had pitch = -1
    // ==================================================================

    void resolve_pitches() {
        if (pat_cache_.empty()) return;
        int matched = 0;
        for (auto& ni : pending_) {
            if (ni.pitch != 60 || ni.duration_beats > 0) continue;
            double best_d = 1e9; int best_p = 60; double best_dur = 0;
            for (const auto& pc : pat_cache_) {
                double d = std::abs(pc.beat - ni.beat);
                if (d < best_d) { best_d = d; best_p = pc.pitch; best_dur = pc.duration_beats; }
            }
            if (best_d < 0.05) {
                ni.pitch = best_p;
                if (ni.duration_beats <= 0) ni.duration_beats = best_dur;
                ++matched;
            }
        }
        ds_log("resolve_pitches: matched %d/%zu", matched, pending_.size());
    }

    // ==================================================================
    // Phrase grouping
    // ==================================================================

    std::vector<Phrase> build_phrases(const std::vector<NoteInfo>& notes) {
        std::vector<Phrase> phrases;
        if (notes.empty()) return phrases;

        Phrase cur;
        cur.notes.push_back(notes[0]);

        for (size_t i = 1; i < notes.size(); ++i) {
            double gap = notes[i].beat - cur.end_beat();
            bool split = gap > phrase_gap_
                      || (int)cur.notes.size() >= MAX_PHRASE_NOTES;
            if (split) {
                phrases.push_back(std::move(cur));
                cur = Phrase{};
            }
            cur.notes.push_back(notes[i]);
        }
        if (!cur.notes.empty()) phrases.push_back(std::move(cur));
        return phrases;
    }

    // ==================================================================
    // Phrase-level pre-rendering
    // ==================================================================

    std::unique_ptr<Render> pre_render_phrases(const std::vector<Phrase>& phrases) {
        if (!acoustic_sess_ || !vocoder_sess_) return nullptr;

        auto render = std::make_unique<Render>();
        render->sample_rate = cfg_.sample_rate;

        for (size_t pi = 0; pi < phrases.size(); ++pi) {
            const auto& phrase = phrases[pi];
            ds_log("  phrase %zu/%zu: %zu notes, beats %.2f–%.2f",
                   pi+1, phrases.size(), phrase.notes.size(),
                   phrase.start_beat(), phrase.end_beat());

            // 1. Phonemize all notes in the phrase, concatenate phoneme IDs
            //    Keep track of which phoneme IDs belong to which note.
            struct PhonemeSpan { size_t start, count; };
            std::vector<int64_t> all_ph_ids;
            std::vector<PhonemeSpan> spans;

            for (const auto& ni : phrase.notes) {
                size_t ph_start = all_ph_ids.size();
                if (ni.lyric.empty()) {
                    // Silent note: insert silence/breath phoneme
                    all_ph_ids.push_back(0);  // SP token
                } else {
                    std::string ipa = espeak_ok_
                        ? phonemize(ni.lyric, voice_) : ni.lyric;
                    if (ipa.empty()) ipa = ni.lyric;
                    auto ids = to_phoneme_ids(ipa);
                    all_ph_ids.insert(all_ph_ids.end(), ids.begin(), ids.end());
                }
                spans.push_back({ph_start, all_ph_ids.size() - ph_start});
            }

            if (all_ph_ids.empty()) {
                // Whole phrase empty — skip
                for (const auto& ni : phrase.notes)
                    render->notes.push_back({ni.beat, (int)render->pcm.size(), 0});
                continue;
            }

            int64_t T = static_cast<int64_t>(all_ph_ids.size());

            // 2. Compute phoneme durations
            //    If we have duration_beats from the engine, use those to
            //    compute frame counts. Otherwise use duration model or defaults.
            std::vector<int64_t> durs(T, 6);  // default: 6 frames per phoneme
            int64_t N = 0;

            // Try beat-based duration computation first
            if (phrase.notes[0].duration_beats > 0) {
                compute_durations_from_beats(phrase.notes, spans, durs, N);
            } else if (duration_sess_) {
                infer_duration(all_ph_ids, durs, N);
            }

            // Ensure N is computed
            if (N == 0) { for (auto d : durs) N += d; }

            // 3. Build F0 curve: one value per mel frame
            //    Map each note's duration in frames to a segment of the F0 curve.
            std::vector<float> f0(N, midi_to_hz(60));
            build_f0_curve(phrase.notes, spans, durs, f0);

            // 4. Acoustic model → mel (full phrase)
            std::vector<float> mel;
            int mel_frames = 0;
            if (!infer_acoustic(all_ph_ids, durs, f0, N, mel, mel_frames)) {
                ds_log("    acoustic failed for phrase %zu", pi+1);
                for (const auto& ni : phrase.notes)
                    render->notes.push_back({ni.beat, (int)render->pcm.size(), 0});
                continue;
            }

            // 5. Vocoder → PCM (full phrase)
            std::vector<float> pcm;
            if (!infer_vocoder(mel, mel_frames, f0, pcm)) {
                ds_log("    vocoder failed for phrase %zu", pi+1);
                for (const auto& ni : phrase.notes)
                    render->notes.push_back({ni.beat, (int)render->pcm.size(), 0});
                continue;
            }

            ds_log("    → %zu samples (%.2fs)", pcm.size(),
                   pcm.size() / (float)cfg_.sample_rate);

            // 6. Index individual note boundaries within the phrase PCM.
            //    Each note's PCM range = [note_frame_start * hop_size,
            //                             note_frame_end * hop_size)
            int phrase_pcm_start = static_cast<int>(render->pcm.size());
            render->pcm.insert(render->pcm.end(), pcm.begin(), pcm.end());

            int64_t frame_cursor = 0;
            for (size_t ni_idx = 0; ni_idx < phrase.notes.size(); ++ni_idx) {
                // Sum frames for this note's phonemes
                int64_t note_frames = 0;
                const auto& span = spans[ni_idx];
                for (size_t j = 0; j < span.count; ++j)
                    note_frames += durs[span.start + j];

                int sample_start = static_cast<int>(frame_cursor * cfg_.hop_size);
                int sample_end   = static_cast<int>((frame_cursor + note_frames) * cfg_.hop_size);
                sample_end = std::min(sample_end, static_cast<int>(pcm.size()));

                render->notes.push_back({
                    phrase.notes[ni_idx].beat,
                    phrase_pcm_start + sample_start,
                    std::max(0, sample_end - sample_start)
                });

                frame_cursor += note_frames;
            }
        }

        return render;
    }

    // ==================================================================
    // Duration computation from beat-level data
    // ==================================================================

    void compute_durations_from_beats(const std::vector<NoteInfo>& notes,
                                      const std::vector<PhonemeSpan>& spans,
                                      std::vector<int64_t>& durs, int64_t& N) {
        // Convert each note's duration_beats → mel frames, then distribute
        // evenly across that note's phonemes.
        //
        // frames_per_beat = sample_rate / (hop_size * bpm / 60)
        // We don't know bpm here directly, but we can estimate from note
        // spacing. Use a default of 120 bpm if we can't estimate.
        // TODO: pass bpm through from engine (maybe via configure)

        float bpm = 120.0f;
        // Quick estimate: if we have at least 2 notes with known durations
        // and the first note has a non-trivial beat position, we can try
        // to infer bpm. For now, just use 120.
        float frames_per_beat = cfg_.sample_rate / (cfg_.hop_size * bpm / 60.0f);

        N = 0;
        for (size_t i = 0; i < notes.size(); ++i) {
            double dur_beats = notes[i].duration_beats;
            if (dur_beats <= 0) dur_beats = 0.5;  // fallback half-beat

            int64_t total_frames = std::max(int64_t(1),
                static_cast<int64_t>(std::round(dur_beats * frames_per_beat)));

            // Distribute evenly across phonemes
            const auto& span = spans[i];
            int64_t per_ph = total_frames / std::max(size_t(1), span.count);
            int64_t remainder = total_frames - per_ph * (int64_t)span.count;

            for (size_t j = 0; j < span.count; ++j) {
                durs[span.start + j] = per_ph + (j == 0 ? remainder : 0);
                N += durs[span.start + j];
            }
        }

        ds_log("    durations from beats: %lld total frames (%.0f frames/beat)",
               (long long)N, frames_per_beat);
    }

    // ==================================================================
    // Build F0 curve for a phrase
    // ==================================================================

    void build_f0_curve(const std::vector<NoteInfo>& notes,
                        const std::vector<PhonemeSpan>& spans,
                        const std::vector<int64_t>& durs,
                        std::vector<float>& f0) {
        int64_t frame = 0;
        for (size_t i = 0; i < notes.size(); ++i) {
            float hz = midi_to_hz(notes[i].pitch);

            // Sum frames for this note
            int64_t note_frames = 0;
            const auto& span = spans[i];
            for (size_t j = 0; j < span.count; ++j)
                note_frames += durs[span.start + j];

            // Fill F0 for these frames
            for (int64_t k = 0; k < note_frames && (frame + k) < (int64_t)f0.size(); ++k)
                f0[frame + k] = hz;

            // Optional: smooth transitions between notes (2-frame linear ramp)
            if (i + 1 < notes.size()) {
                float next_hz = midi_to_hz(notes[i + 1].pitch);
                int64_t ramp = std::min(int64_t(2), note_frames / 2);
                int64_t ramp_start = frame + note_frames - ramp;
                for (int64_t k = 0; k < ramp && ramp_start + k < (int64_t)f0.size(); ++k) {
                    float t = static_cast<float>(k + 1) / static_cast<float>(ramp + 1);
                    f0[ramp_start + k] = hz * (1.0f - t) + next_hz * t;
                }
            }

            frame += note_frames;
        }
    }

    // ==================================================================
    // Phoneme → ID
    // ==================================================================

    std::vector<int64_t> to_phoneme_ids(const std::string& ipa) {
        std::vector<int64_t> ids;
        if (phoneme_dict_.empty()) {
            for (unsigned char c : ipa) ids.push_back(c);
        } else {
            std::istringstream ss(ipa);
            std::string tok;
            while (ss >> tok) {
                auto it = phoneme_dict_.find(tok);
                if (it != phoneme_dict_.end()) {
                    ids.push_back(it->second);
                } else {
                    for (char c : tok) {
                        auto it2 = phoneme_dict_.find(std::string(1, c));
                        if (it2 != phoneme_dict_.end()) ids.push_back(it2->second);
                    }
                }
            }
        }
        if (ids.empty()) ids.push_back(0);
        return ids;
    }

    // ==================================================================
    // ONNX: acoustic (phrase-level)
    // ==================================================================
    // Inputs: tokens[1,T], durations[1,T], f0[1,N], speedup[1], spk_id[1]
    // Output: mel[1, N, n_mels]

    bool infer_acoustic(const std::vector<int64_t>& ph_ids,
                        const std::vector<int64_t>& durs,
                        const std::vector<float>& f0, int64_t N,
                        std::vector<float>& mel_out, int& mel_frames) {
        try {
            int64_t T = (int64_t)ph_ids.size();
            std::array<int64_t,2> s1T = {1, T};
            std::array<int64_t,2> s1N = {1, N};
            std::array<int64_t,1> s1  = {1};

            auto t_tok = Ort::Value::CreateTensor<int64_t>(
                mem_info_, const_cast<int64_t*>(ph_ids.data()), T, s1T.data(), 2);
            auto t_dur = Ort::Value::CreateTensor<int64_t>(
                mem_info_, const_cast<int64_t*>(durs.data()), T, s1T.data(), 2);
            auto t_f0  = Ort::Value::CreateTensor<float>(
                mem_info_, const_cast<float*>(f0.data()), N, s1N.data(), 2);

            int64_t speedup = 10;
            auto t_spd = Ort::Value::CreateTensor<int64_t>(
                mem_info_, &speedup, 1, s1.data(), 1);
            int64_t spk = speaker_id_;
            auto t_spk = Ort::Value::CreateTensor<int64_t>(
                mem_info_, &spk, 1, s1.data(), 1);

            Ort::AllocatorWithDefaultOptions alloc;
            size_t n_in = acoustic_sess_->GetInputCount();
            std::vector<std::string>             in_strs(n_in);
            std::vector<Ort::AllocatedStringPtr> in_ptrs;
            std::vector<const char*>             in_names(n_in);
            std::vector<Ort::Value>              in_vals;
            in_vals.reserve(n_in);

            for (size_t i = 0; i < n_in; ++i) {
                in_ptrs.push_back(acoustic_sess_->GetInputNameAllocated(i, alloc));
                in_strs[i] = in_ptrs.back().get();
                in_names[i] = in_strs[i].c_str();
            }

            for (size_t i = 0; i < n_in; ++i) {
                const auto& nm = in_strs[i];
                if      (nm == "tokens" || nm == "phone" || nm == "ph_seq")
                    in_vals.push_back(std::move(t_tok));
                else if (nm == "durations" || nm == "ph_dur" || nm == "dur_seq")
                    in_vals.push_back(std::move(t_dur));
                else if (nm == "f0" || nm == "f0_seq")
                    in_vals.push_back(std::move(t_f0));
                else if (nm == "speedup" || nm == "speed")
                    in_vals.push_back(std::move(t_spd));
                else if (nm == "spk_id" || nm == "spk_embed")
                    in_vals.push_back(std::move(t_spk));
                else {
                    ds_log("    unknown input '%s' → dummy", nm.c_str());
                    float d = 0; in_vals.push_back(
                        Ort::Value::CreateTensor<float>(mem_info_, &d, 1, s1.data(), 1));
                }
            }

            size_t n_out = acoustic_sess_->GetOutputCount();
            std::vector<Ort::AllocatedStringPtr> out_ptrs;
            std::vector<const char*> out_names(n_out);
            for (size_t i = 0; i < n_out; ++i) {
                out_ptrs.push_back(acoustic_sess_->GetOutputNameAllocated(i, alloc));
                out_names[i] = out_ptrs.back().get();
            }

            auto outs = acoustic_sess_->Run(Ort::RunOptions{nullptr},
                in_names.data(), in_vals.data(), n_in, out_names.data(), n_out);
            if (outs.empty()) return false;

            auto shape = outs[0].GetTensorTypeAndShapeInfo().GetShape();
            mel_frames = (shape.size() >= 2) ? (int)shape[1] : 0;
            int n_mels = (shape.size() >= 3) ? (int)shape[2] : cfg_.n_mels;
            const float* d = outs[0].GetTensorData<float>();
            mel_out.assign(d, d + mel_frames * n_mels);
            ds_log("    acoustic: %d frames × %d mels", mel_frames, n_mels);
            return true;

        } catch (const Ort::Exception& e) {
            ds_log("acoustic error: %s", e.what()); return false;
        }
    }

    void infer_duration(const std::vector<int64_t>& ph_ids,
                        std::vector<int64_t>& durs, int64_t& N) {
        if (!duration_sess_) return;
        try {
            int64_t T = (int64_t)ph_ids.size();
            std::array<int64_t,2> s = {1, T};
            auto t = Ort::Value::CreateTensor<int64_t>(
                mem_info_, const_cast<int64_t*>(ph_ids.data()), T, s.data(), 2);
            Ort::AllocatorWithDefaultOptions alloc;
            auto in_nm = duration_sess_->GetInputNameAllocated(0, alloc);
            auto out_nm = duration_sess_->GetOutputNameAllocated(0, alloc);
            const char* ins[] = {in_nm.get()}, *outs[] = {out_nm.get()};
            auto res = duration_sess_->Run(Ort::RunOptions{nullptr}, ins, &t, 1, outs, 1);
            if (!res.empty()) {
                const float* d = res[0].GetTensorData<float>();
                auto sh = res[0].GetTensorTypeAndShapeInfo().GetShape();
                int64_t len = (sh.size() >= 2) ? sh[1] : T;
                N = 0; durs.resize(std::min(T, len));
                for (int64_t i = 0; i < (int64_t)durs.size(); ++i) {
                    durs[i] = std::max(int64_t(1), (int64_t)std::round(d[i]));
                    N += durs[i];
                }
            }
        } catch (const Ort::Exception& e) {
            ds_log("duration error: %s", e.what());
        }
    }

    // ==================================================================
    // ONNX: vocoder (phrase-level)
    // ==================================================================
    // Input: mel[1, frames, n_mels], f0[1, frames]
    // Output: audio[1, 1, samples]

    bool infer_vocoder(const std::vector<float>& mel, int mel_frames,
                       const std::vector<float>& f0,
                       std::vector<float>& pcm_out) {
        try {
            std::array<int64_t,3> mel_shape = {1, (int64_t)mel_frames, (int64_t)cfg_.n_mels};
            auto t_mel = Ort::Value::CreateTensor<float>(
                mem_info_, const_cast<float*>(mel.data()), mel.size(),
                mel_shape.data(), 3);

            // Vocoder F0: use the full phrase F0 (may need truncating to mel_frames)
            std::vector<float> voc_f0(f0.begin(), f0.begin() + std::min((int)f0.size(), mel_frames));
            voc_f0.resize(mel_frames, voc_f0.empty() ? 0.0f : voc_f0.back());
            std::array<int64_t,2> f0_shape = {1, (int64_t)mel_frames};
            auto t_f0 = Ort::Value::CreateTensor<float>(
                mem_info_, voc_f0.data(), voc_f0.size(), f0_shape.data(), 2);

            Ort::AllocatorWithDefaultOptions alloc;
            size_t n_in = vocoder_sess_->GetInputCount();
            std::vector<Ort::AllocatedStringPtr> in_ptrs;
            std::vector<std::string> in_strs(n_in);
            std::vector<const char*> in_names(n_in);
            std::vector<Ort::Value>  in_vals;

            for (size_t i = 0; i < n_in; ++i) {
                in_ptrs.push_back(vocoder_sess_->GetInputNameAllocated(i, alloc));
                in_strs[i] = in_ptrs.back().get();
                in_names[i] = in_strs[i].c_str();
            }
            for (size_t i = 0; i < n_in; ++i) {
                const auto& nm = in_strs[i];
                if (nm == "mel" || nm == "c") in_vals.push_back(std::move(t_mel));
                else if (nm == "f0") in_vals.push_back(std::move(t_f0));
                else {
                    float d = 0; std::array<int64_t,1> s = {1};
                    in_vals.push_back(Ort::Value::CreateTensor<float>(
                        mem_info_, &d, 1, s.data(), 1));
                }
            }

            size_t n_out = vocoder_sess_->GetOutputCount();
            std::vector<Ort::AllocatedStringPtr> out_ptrs;
            std::vector<const char*> out_names(n_out);
            for (size_t i = 0; i < n_out; ++i) {
                out_ptrs.push_back(vocoder_sess_->GetOutputNameAllocated(i, alloc));
                out_names[i] = out_ptrs.back().get();
            }

            auto outs = vocoder_sess_->Run(Ort::RunOptions{nullptr},
                in_names.data(), in_vals.data(), n_in, out_names.data(), n_out);
            if (outs.empty()) return false;

            auto shape = outs[0].GetTensorTypeAndShapeInfo().GetShape();
            size_t n = 1; for (auto d : shape) n *= d;
            const float* data = outs[0].GetTensorData<float>();
            pcm_out.assign(data, data + n);
            return true;
        } catch (const Ort::Exception& e) {
            ds_log("vocoder error: %s", e.what()); return false;
        }
    }

    // ==================================================================
    // Provider selection & model loading (unchanged)
    // ==================================================================

    void choose_provider() {
        size_t model_mem = estimate_model_mem(acoustic_path_)
                         + estimate_model_mem(vocoder_path_)
                         + estimate_model_mem(duration_path_);
        size_t needed = model_mem + (200 << 20);
        size_t needed_125 = needed + needed / 4;
        MemInfo m = query_memory();
        if (m.gpu && m.gpu_free >= needed_125) { use_gpu_ = true; return; }
        if (m.ram_free > 0 && m.ram_free < needed_125)
            error_ = "Low memory: " + std::to_string(m.ram_free >> 20)
                   + "MB free, need ~" + std::to_string(needed_125 >> 20) + "MB";
        use_gpu_ = false;
    }

    bool load_models() {
        if (acoustic_path_.empty() || vocoder_path_.empty()) {
            error_ = "acoustic_path and vocoder_path required"; return false;
        }
        try {
            ort_env_ = std::make_unique<Ort::Env>(ORT_LOGGING_LEVEL_WARNING, "diffsinger");
            Ort::SessionOptions opts;
            opts.SetIntraOpNumThreads(4);
            opts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
            if (use_gpu_) {
                try {
                    OrtCUDAProviderOptionsV2* co = nullptr;
                    Ort::GetApi().CreateCUDAProviderOptions(&co);
                    opts.AppendExecutionProvider_CUDA_V2(*co);
                    Ort::GetApi().ReleaseCUDAProviderOptions(co);
                } catch (const Ort::Exception& e) {
                    ds_log("CUDA failed (%s) → CPU", e.what());
                    use_gpu_ = false;
                    opts = Ort::SessionOptions();
                    opts.SetIntraOpNumThreads(4);
                    opts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
                }
            }
            ds_log("Loading acoustic: %s", acoustic_path_.c_str());
            acoustic_sess_ = std::make_unique<Ort::Session>(*ort_env_, acoustic_path_.c_str(), opts);
            ds_log("Loading vocoder: %s", vocoder_path_.c_str());
            vocoder_sess_  = std::make_unique<Ort::Session>(*ort_env_, vocoder_path_.c_str(), opts);
            if (!duration_path_.empty()) {
                ds_log("Loading duration: %s", duration_path_.c_str());
                duration_sess_ = std::make_unique<Ort::Session>(*ort_env_, duration_path_.c_str(), opts);
            }
            ds_log("All models loaded"); return true;
        } catch (const Ort::Exception& e) {
            error_ = std::string("ONNX error: ") + e.what();
            ds_log("ERROR: %s", error_.c_str()); return false;
        }
    }
};

REGISTER_PLUGIN(DiffSingerPlugin);
REGISTER_PLUGIN_DYNAMIC(DiffSingerPlugin);

std::unique_ptr<Plugin> make_diffsinger_plugin() {
    return std::make_unique<DiffSingerPlugin>();
}

#endif // AS_ENABLE_DIFFSINGER
