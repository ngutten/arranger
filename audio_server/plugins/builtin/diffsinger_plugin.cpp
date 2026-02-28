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
#include <cstdarg>
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
    float max_depth   = 0.6f;
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
                else if (key == "max_depth") max_depth = std::stof(val);
                else if (key == "use_shallow_diffusion")
                    use_shallow_diffusion = (val == "true" || val == "True" || val == "1");
            } catch (...) {}
        }
        ds_log("dsconfig: sr=%d hop=%d mels=%d fmin=%.0f fmax=%.0f k_step=%d max_depth=%.2f",
               sample_rate, hop_size, n_mels, mel_fmin, mel_fmax, k_step, max_depth);
        return true;
    }
};

// ---------------------------------------------------------------------------
// Phoneme dictionary (dsdict.txt)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Phoneme dictionary loader
// ---------------------------------------------------------------------------
// Supports two formats:
//   1. JSON array:  ["AP", "SP", "aa", "ae", ...]  — index = phoneme ID
//      (used by TIGER and newer DiffSinger voices, file is phonemes.json)
//   2. Text:        phoneme ID  (one per line, space/tab separated)
//      (older dsdict.txt format)
//   3. phonemes.txt: one phoneme per line, line number = ID
//      (referenced from dsconfig.yaml)

static bool load_phoneme_dict(const std::string& path,
                              std::unordered_map<std::string, int64_t>& out,
                              std::string& err) {
    std::ifstream f(path);
    if (!f.is_open()) { err = "Cannot open dict: " + path; return false; }

    // Read entire file
    std::string content((std::istreambuf_iterator<char>(f)),
                         std::istreambuf_iterator<char>());

    // Detect format by looking for JSON
    size_t first_nonws = content.find_first_not_of(" \t\r\n");
    if (first_nonws != std::string::npos &&
        (content[first_nonws] == '[' || content[first_nonws] == '{')) {

        bool is_object = (content[first_nonws] == '{');

        if (is_object) {
            // JSON object format: {"AP": 1, "SP": 2, "en/b": 7, ...}
            // Minimal parser: find "key": value pairs
            size_t pos = 0;
            while (pos < content.size()) {
                size_t q1 = content.find('"', pos);
                if (q1 == std::string::npos) break;
                size_t q2 = content.find('"', q1 + 1);
                if (q2 == std::string::npos) break;
                std::string key = content.substr(q1 + 1, q2 - q1 - 1);

                // Find the colon and then the integer value
                size_t colon = content.find(':', q2 + 1);
                if (colon == std::string::npos) break;

                // Skip whitespace after colon
                size_t vstart = content.find_first_not_of(" \t\r\n", colon + 1);
                if (vstart == std::string::npos) break;

                // Read integer (or next quote if it's a string value)
                if (content[vstart] == '"') {
                    // String value — skip (we want int IDs)
                    pos = content.find('"', vstart + 1);
                    if (pos != std::string::npos) pos++;
                    continue;
                }

                // Parse integer
                char* end = nullptr;
                long long id = std::strtoll(content.c_str() + vstart, &end, 10);
                if (end != content.c_str() + vstart) {
                    out[key] = static_cast<int64_t>(id);
                }
                pos = (end != nullptr) ? (end - content.c_str()) : (vstart + 1);
            }
        } else {
            // JSON array format: ["AP", "SP", "aa", ...]
            int64_t id = 0;
            size_t pos = content.find('[');
            while (pos < content.size()) {
                size_t q1 = content.find('"', pos + 1);
                if (q1 == std::string::npos) break;
                size_t q2 = content.find('"', q1 + 1);
                if (q2 == std::string::npos) break;
                std::string phoneme = content.substr(q1 + 1, q2 - q1 - 1);
                if (!phoneme.empty()) out[phoneme] = id;
                id++;
                pos = q2;
            }
        }
        ds_log("Loaded %zu phonemes from JSON array: %s", out.size(), path.c_str());
        return true;
    }

    // Try text format: either "phoneme ID" or one-phoneme-per-line
    std::istringstream ss(content);
    std::string line;
    int64_t line_num = 0;
    bool has_explicit_ids = false;

    // Peek at first non-empty line to detect format
    std::vector<std::string> lines;
    while (std::getline(ss, line)) {
        if (!line.empty() && line[0] != '#') lines.push_back(line);
    }

    if (!lines.empty()) {
        // Check if first line has two tokens (phoneme + ID)
        std::istringstream first(lines[0]);
        std::string tok1, tok2;
        if ((first >> tok1 >> tok2) && !tok2.empty()) {
            // Try to parse tok2 as integer
            try { std::stoll(tok2); has_explicit_ids = true; } catch (...) {}
        }
    }

    for (const auto& l : lines) {
        if (has_explicit_ids) {
            // Format: "phoneme ID"
            std::istringstream ls(l);
            std::string phoneme; int64_t id;
            if (ls >> phoneme >> id) out[phoneme] = id;
        } else {
            // Format: one phoneme per line, line number = ID
            // (skip empty lines but count them for ID)
            std::string trimmed = l;
            size_t a = trimmed.find_first_not_of(" \t\r\n");
            size_t b = trimmed.find_last_not_of(" \t\r\n");
            if (a != std::string::npos)
                trimmed = trimmed.substr(a, b - a + 1);
            if (!trimmed.empty())
                out[trimmed] = line_num;
            line_num++;
        }
    }

    ds_log("Loaded %zu phonemes from text: %s", out.size(), path.c_str());
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
            double mx = 0;
            for (const auto& n : notes) {
                double e = n.beat + (n.duration_beats > 0 ? n.duration_beats : 0.5);
                if (e > mx) mx = e;
            }
            return mx;
        }
    };

    struct NoteEntry {
        double beat;
        int    pcm_start;
        int    pcm_length;
    };

    // Tracks which phoneme IDs in a phrase belong to which note.
    struct PhonemeSpan { size_t start, count; };

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
            "Set 'model_dir' to a DiffSinger voice package directory\n"
            "(containing dsconfig.yaml) and the plugin auto-discovers\n"
            "all model files. Individual paths can still be overridden.\n"
            "GPU acceleration with automatic CPU fallback.";
        d.author  = "builtin";
        d.version = 3;

        d.ports = {
            {"lyrics_in","Lyrics","Pattern with per-note lyrics",
             PluginPortType::Pattern, PortRole::Input},
            {"events_in","Events","MIDI note input",
             PluginPortType::Event, PortRole::Input},
            {"audio_out","Audio","Stereo output",
             PluginPortType::AudioStereo, PortRole::Output},
        };

        d.config_params = {
            // Primary: just point at the voice package root
            {"model_dir","Model Directory",
             "Path to DiffSinger voice package (contains dsconfig.yaml). "
             "Auto-discovers all model files.",
             ConfigType::DirPath,""},
            // Phoneme system
            {"phoneme_set","Phoneme Set",
             "auto = detect from dict, arpabet = ARPAbet (TIGER/EN), "
             "ipa = raw IPA, xsampa = X-SAMPA",
             ConfigType::String,"auto"},
            // Voice & speaker
            {"voice","Voice","espeak-ng voice (e.g. en-us)",
             ConfigType::String,"en-us"},
            {"speaker_id","Speaker ID","Speaker index (multi-speaker)",
             ConfigType::Integer,"0"},
            {"phrase_gap","Phrase Gap (beats)",
             "Beat gap threshold for phrase splitting",
             ConfigType::Float,"0.5"},
            // Overrides (optional — auto-discovered from dsconfig.yaml)
            {"acoustic_path","Acoustic Model (override)",
             "Override auto-discovered acoustic ONNX path",
             ConfigType::FilePath,"", "", false, true},
            {"vocoder_path","Vocoder Model (override)",
             "Override auto-discovered vocoder ONNX path",
             ConfigType::FilePath,"", "", false, true},
            {"duration_path","Duration Model (override)",
             "Override auto-discovered duration ONNX path",
             ConfigType::FilePath,"", "", false, true},
            {"dict_path","Phoneme Dict (override)",
             "Override auto-discovered phoneme dict path",
             ConfigType::FilePath,"", "", false, true},
        };
        return d;
    }

    // ==================================================================
    // Configuration
    // ==================================================================

    void configure(const std::string& key, const std::string& val) override {
        ds_log("configure: %s = '%.60s'", key.c_str(), val.c_str());
        if      (key == "model_dir")      model_dir_      = val;
        else if (key == "acoustic_path")  acoustic_path_  = val;
        else if (key == "vocoder_path")   vocoder_path_   = val;
        else if (key == "duration_path")  duration_path_  = val;
        else if (key == "dict_path")      dict_path_      = val;
        else if (key == "phoneme_set")    phoneme_set_    = val;
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

        // --- Auto-discover model files from model_dir ---
        if (!model_dir_.empty()) {
            discover_models();
        }

        // --- Load dsconfig.yaml ---
        if (!dsconfig_path_.empty()) {
            std::string err;
            if (!cfg_.load(dsconfig_path_, err)) {
                error_ = err; set_state(State::Error); return;
            }
        }

        // --- Load phoneme dict ---
        if (!dict_path_.empty()) {
            std::string err;
            if (!load_phoneme_dict(dict_path_, phoneme_dict_, err))
                ds_log("WARNING: %s", err.c_str());
        }

        // --- Auto-detect phoneme set if "auto" ---
        if (phoneme_set_ == "auto" || phoneme_set_.empty()) {
            detect_phoneme_set();
        }
        ds_log("phoneme_set: %s", phoneme_set_.c_str());

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

        resolve_pitches();

        // Stash the resolved notes for prerender().
        // We do NOT render here — prerender() is called separately
        // before playback or offline render.
        pending_notes_ = pending_;
        pending_.clear();
        pat_cache_.clear();
    }

    // ==================================================================
    // prerender() — called from main thread before play/render.
    // Audio thread is guaranteed not running.
    // ==================================================================

    void prerender() override {
        if (pending_notes_.empty() && !cache_valid_) {
            ds_log("prerender: no notes and no cache");
            return;
        }

        // Check if notes have changed since last render (cache check).
        // Include BPM in hash so tempo changes invalidate cache.
        uint64_t hash = hash_notes(pending_notes_.empty()
                                     ? cached_notes_ : pending_notes_);
        // Mix in BPM
        { float b = bpm_;
          auto* p = reinterpret_cast<const uint8_t*>(&b);
          for (int i = 0; i < 4; ++i) { hash ^= p[i]; hash *= 1099511628211ULL; }
        }
        if (hash == cached_hash_ && cache_valid_) {
            ds_log("prerender: cache hit (hash=%llu, %zu samples)",
                   (unsigned long long)hash,
                   old_renders_.empty() ? 0 : old_renders_.back().pcm.size());
            // Re-publish the existing render (in case graph was rebuilt)
            if (!old_renders_.empty())
                current_render_.store(&old_renders_.back(),
                                      std::memory_order_release);
            next_note_.store(0, std::memory_order_relaxed);
            return;
        }

        // Notes changed — need to re-render.
        if (!pending_notes_.empty()) {
            cached_notes_ = pending_notes_;
            pending_notes_.clear();
        }

        ds_log("prerender: rendering %zu notes (hash=%llu)",
               cached_notes_.size(), (unsigned long long)hash);
        auto t0 = std::chrono::steady_clock::now();

        auto phrases = build_phrases(cached_notes_);
        ds_log("  → %zu phrases", phrases.size());

        auto render = pre_render_phrases(phrases);

        if (!render) { ds_log("prerender FAILED"); return; }

        long ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t0).count();
        float dur_s = render->pcm.size() / static_cast<float>(cfg_.sample_rate);
        ds_log("Rendered %zu samples (%.1fs) in %ldms (%.1fx RT)",
               render->pcm.size(), dur_s, ms,
               (ms > 0) ? dur_s / (ms / 1000.0f) : 0.0f);

        old_renders_.push_back(std::move(*render));
        current_render_.store(&old_renders_.back(), std::memory_order_release);
        next_note_.store(0, std::memory_order_relaxed);

        cached_hash_ = hash;
        cache_valid_ = true;
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

    void set_bpm(float bpm) override {
        if (bpm > 0) bpm_ = bpm;
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
    std::string model_dir_;
    std::string acoustic_path_, vocoder_path_, duration_path_;
    std::string dsconfig_path_, dict_path_;
    std::string voice_ = "en-us";
    std::string phoneme_set_ = "auto";
    int   speaker_id_ = 0;
    float phrase_gap_  = 0.5f;  // beats
    float bpm_         = 120.0f;
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
    std::vector<NoteInfo> pending_notes_;  // stashed by on_schedule_loaded for prerender
    std::vector<NoteInfo> cached_notes_;   // notes from last successful render
    uint64_t cached_hash_ = 0;
    bool     cache_valid_  = false;

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
            // Keep track of which phoneme IDs belong to which note.
            std::vector<int64_t> all_ph_ids;
            std::vector<PhonemeSpan> spans;

            for (const auto& ni : phrase.notes) {
                size_t ph_start = all_ph_ids.size();
                if (ni.lyric.empty()) {
                    // Silent note: insert SP (silence) token
                    int64_t sp_id = phoneme_dict_.count("SP") ?
                                    phoneme_dict_.at("SP") : 2;
                    all_ph_ids.push_back(sp_id);
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
            }
            // Note: duration_sess_ is NOT used here. The TIGER/OpenUtau
            // duration model expects encoder hidden states as input, not
            // raw phoneme IDs. We always use beat-based durations from
            // the score, which are more accurate for our use case.

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
        float bpm = bpm_;
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
    // Phoneme → ID (with language prefix and IPA→ARPAbet mapping)
    // ==================================================================

    // IPA → ARPAbet mapping for English (covers espeak-ng output)
    // This handles the most common mappings. Extended phonemes like
    // TIGER's [ax], [dx] are included.
    struct PhonemeMapping { const char* ipa; const char* arpa; };
    static constexpr PhonemeMapping ipa_to_arpa[] = {
        // Vowels
        {"ɑː", "aa"},  {"ɑ", "aa"},   {"æ", "ae"},   {"ʌ", "ah"},
        {"ɔː", "ao"},  {"ɔ", "ao"},   {"aʊ", "aw"},  {"ə", "ax"},
        {"ɚ", "er"},   {"ɝ", "er"},   {"eɪ", "ey"},  {"ɛ", "eh"},
        {"ɪ", "ih"},   {"iː", "iy"},  {"i", "iy"},   {"oʊ", "ow"},
        {"ʊ", "uh"},   {"uː", "uw"},  {"u", "uw"},   {"aɪ", "ay"},
        {"ɔɪ", "oy"},  {"ɒ", "oh"},
        // Consonants
        {"b", "b"},     {"d", "d"},    {"f", "f"},     {"ɡ", "g"},
        {"g", "g"},     {"h", "hh"},   {"dʒ", "jh"},   {"k", "k"},
        {"l", "l"},     {"m", "m"},    {"n", "n"},     {"ŋ", "ng"},
        {"p", "p"},     {"ɹ", "r"},    {"r", "r"},     {"s", "s"},
        {"ʃ", "sh"},   {"t", "t"},    {"tʃ", "ch"},   {"θ", "th"},
        {"ð", "dh"},   {"v", "v"},    {"w", "w"},     {"j", "y"},
        {"z", "z"},     {"ʒ", "zh"},   {"ɾ", "dx"},    {"ʔ", "q"},
        // Diphthongs that may appear as single tokens
        {"eː", "ey"},  {"oː", "ow"},
    };

    // Try to find a phoneme in the dict, with language prefix fallback
    int64_t lookup_phoneme(const std::string& ph) const {
        // Direct lookup first
        auto it = phoneme_dict_.find(ph);
        if (it != phoneme_dict_.end()) return it->second;

        // Try with language prefix (en/ by default, configurable via voice_)
        std::string lang_prefix;
        if (voice_.find("en") != std::string::npos) lang_prefix = "en/";
        else if (voice_.find("zh") != std::string::npos) lang_prefix = "zh/";
        else if (voice_.find("ja") != std::string::npos) lang_prefix = "ja/";
        else if (voice_.find("de") != std::string::npos) lang_prefix = "de/";
        else if (voice_.find("fr") != std::string::npos) lang_prefix = "fr/";
        else if (voice_.find("es") != std::string::npos) lang_prefix = "es/";
        else if (voice_.find("ko") != std::string::npos) lang_prefix = "ko/";
        else if (voice_.find("ru") != std::string::npos) lang_prefix = "ru/";
        else if (voice_.find("pt") != std::string::npos) lang_prefix = "pt/";
        else if (voice_.find("th") != std::string::npos) lang_prefix = "th/";
        else lang_prefix = "en/";

        it = phoneme_dict_.find(lang_prefix + ph);
        if (it != phoneme_dict_.end()) return it->second;

        return -1;  // not found
    }

    std::vector<int64_t> to_phoneme_ids(const std::string& ipa) {
        std::vector<int64_t> ids;

        if (phoneme_dict_.empty()) {
            for (unsigned char c : ipa) ids.push_back(c);
            if (ids.empty()) ids.push_back(0);
            return ids;
        }

        // If phoneme_set is "ipa", the dict keys are already IPA —
        // try direct lookup of space-separated tokens first
        if (phoneme_set_ == "ipa") {
            std::istringstream ss(ipa);
            std::string tok;
            while (ss >> tok) {
                int64_t id = lookup_phoneme(tok);
                if (id >= 0) ids.push_back(id);
            }
            if (ids.empty()) ids.push_back(phoneme_dict_.count("SP") ?
                                            phoneme_dict_.at("SP") : 0);
            return ids;
        }

        // For arpabet/xsampa: parse IPA string via greedy longest-match,
        // map to ARPAbet, then look up with language prefix
        size_t pos = 0;
        while (pos < ipa.size()) {
            // Skip whitespace and stress marks
            if (ipa[pos] == ' ' || ipa[pos] == '\t' ||
                ipa[pos] == '\xCB' ||  // start of ˈ or ˌ (stress marks)
                ipa[pos] == '\xCC' ||  // combining characters
                ipa[pos] == '\xCD') {  // more combining
                pos++;
                // Skip continuation bytes of multi-byte UTF-8
                while (pos < ipa.size() && (ipa[pos] & 0xC0) == 0x80) pos++;
                continue;
            }

            // Try longest match against IPA→ARPAbet table
            int best_len = 0;
            const char* best_arpa = nullptr;

            for (const auto& m : ipa_to_arpa) {
                int mlen = std::strlen(m.ipa);
                if (pos + mlen <= ipa.size() &&
                    std::strncmp(ipa.c_str() + pos, m.ipa, mlen) == 0 &&
                    mlen > best_len) {
                    best_len = mlen;
                    best_arpa = m.arpa;
                }
            }

            if (best_arpa) {
                int64_t id = lookup_phoneme(best_arpa);
                if (id >= 0) ids.push_back(id);
                else ds_log("    phoneme '%s' (from IPA) not in dict", best_arpa);
                pos += best_len;
            } else {
                // Try the raw character(s) as a single phoneme
                // Determine UTF-8 character length
                int clen = 1;
                unsigned char c = ipa[pos];
                if ((c & 0xE0) == 0xC0) clen = 2;
                else if ((c & 0xF0) == 0xE0) clen = 3;
                else if ((c & 0xF8) == 0xF0) clen = 4;
                clen = std::min(clen, (int)(ipa.size() - pos));

                std::string raw = ipa.substr(pos, clen);
                int64_t id = lookup_phoneme(raw);
                if (id >= 0) {
                    ids.push_back(id);
                } else {
                    // Skip unknown character silently
                }
                pos += clen;
            }
        }

        if (ids.empty()) ids.push_back(phoneme_dict_.count("SP") ?
                                        phoneme_dict_.at("SP") : 0);
        return ids;
    }

    // ==================================================================
    // ONNX: acoustic (phrase-level)
    // ==================================================================
    // Queries actual model inputs by name and type, builds tensors accordingly.
    // Known DiffSinger acoustic inputs:
    //   tokens/ph_seq:    int64[1,T]   phoneme IDs
    //   durations/ph_dur: int64[1,T]   frames per phoneme
    //   f0/f0_seq:        float[1,N]   Hz pitch curve (N = sum(durations))
    //   languages:        int64[1,T]   language ID per phoneme (multi-lang models)
    //   spk_id:           int64[1]     speaker index
    //   spk_embed:        float[1,H]   speaker embedding (alt to spk_id)
    //   gender:           float[1,N]   key shift / gender (-1 to 1)
    //   velocity:         float[1,N]   speed/velocity
    //   depth:            float[1]     shallow diffusion depth (0-1)
    //   steps:            int64[1]     diffusion steps
    //   speedup:          int64[1]     diffusion speedup factor

    // Helper: create a tensor of the right type filled with a default value,
    // matching the model's expected shape for a given input index.
    Ort::Value make_default_tensor(Ort::Session& sess, size_t input_idx,
                                   int64_t T, int64_t N,
                                   // Backing storage — caller must keep alive
                                   std::vector<int64_t>& i64_buf,
                                   std::vector<float>& f32_buf,
                                   int64_t default_i64, float default_f32) {
        auto type_info = sess.GetInputTypeInfo(input_idx);
        auto tensor_info = type_info.GetTensorTypeAndShapeInfo();
        auto elem_type = tensor_info.GetElementType();
        auto shape = tensor_info.GetShape();

        // Replace dynamic dims (-1) with our known sizes
        for (auto& d : shape) {
            if (d <= 0) {
                // Heuristic: if shape has 2 dims [1, ?], second is T or N
                // Use T for token-length inputs, N for frame-length inputs
                d = (shape.size() == 2) ? T : 1;  // conservative default
            }
        }

        int64_t numel = 1;
        for (auto d : shape) numel *= d;
        if (numel <= 0) numel = 1;

        if (elem_type == ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64) {
            size_t off = i64_buf.size();
            i64_buf.resize(off + numel, default_i64);
            return Ort::Value::CreateTensor<int64_t>(
                mem_info_, i64_buf.data() + off, numel, shape.data(), shape.size());
        } else {
            size_t off = f32_buf.size();
            f32_buf.resize(off + numel, default_f32);
            return Ort::Value::CreateTensor<float>(
                mem_info_, f32_buf.data() + off, numel, shape.data(), shape.size());
        }
    }

    bool infer_acoustic(const std::vector<int64_t>& ph_ids,
                        const std::vector<int64_t>& durs,
                        const std::vector<float>& f0, int64_t N,
                        std::vector<float>& mel_out, int& mel_frames) {
        try {
            int64_t T = (int64_t)ph_ids.size();

            // Backing storage for tensors we build (must outlive the Run call)
            std::vector<int64_t> i64_store;
            std::vector<float> f32_store;
            i64_store.reserve(T * 4 + N + 16);
            f32_store.reserve(N * 4 + 16);

            // Pre-built tensors for known inputs
            std::array<int64_t,2> s1T = {1, T};
            std::array<int64_t,2> s1N = {1, N};
            std::array<int64_t,1> s1  = {1};

            // Language IDs: all same language (0 = first language)
            std::vector<int64_t> lang_ids(T, 0);  // TODO: map from voice_ config

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

                if (nm == "tokens" || nm == "phone" || nm == "ph_seq") {
                    in_vals.push_back(Ort::Value::CreateTensor<int64_t>(
                        mem_info_, const_cast<int64_t*>(ph_ids.data()), T,
                        s1T.data(), 2));
                }
                else if (nm == "durations" || nm == "ph_dur" || nm == "dur_seq") {
                    in_vals.push_back(Ort::Value::CreateTensor<int64_t>(
                        mem_info_, const_cast<int64_t*>(durs.data()), T,
                        s1T.data(), 2));
                }
                else if (nm == "f0" || nm == "f0_seq") {
                    in_vals.push_back(Ort::Value::CreateTensor<float>(
                        mem_info_, const_cast<float*>(f0.data()), N,
                        s1N.data(), 2));
                }
                else if (nm == "languages" || nm == "lang_seq") {
                    in_vals.push_back(Ort::Value::CreateTensor<int64_t>(
                        mem_info_, lang_ids.data(), T, s1T.data(), 2));
                }
                else if (nm == "spk_id") {
                    size_t off = i64_store.size();
                    i64_store.push_back(static_cast<int64_t>(speaker_id_));
                    in_vals.push_back(Ort::Value::CreateTensor<int64_t>(
                        mem_info_, i64_store.data() + off, 1, s1.data(), 1));
                }
                else if (nm == "gender" || nm == "key_shift") {
                    // float[1,N] — 0.0 = neutral
                    size_t off = f32_store.size();
                    f32_store.resize(off + N, 0.0f);
                    in_vals.push_back(Ort::Value::CreateTensor<float>(
                        mem_info_, f32_store.data() + off, N, s1N.data(), 2));
                }
                else if (nm == "velocity" || nm == "speed") {
                    // float[1,N] — 1.0 = normal speed
                    size_t off = f32_store.size();
                    f32_store.resize(off + N, 1.0f);
                    in_vals.push_back(Ort::Value::CreateTensor<float>(
                        mem_info_, f32_store.data() + off, N, s1N.data(), 2));
                }
                else if (nm == "depth") {
                    // float[1] — shallow diffusion depth from dsconfig
                    size_t off = f32_store.size();
                    f32_store.push_back(cfg_.max_depth);
                    in_vals.push_back(Ort::Value::CreateTensor<float>(
                        mem_info_, f32_store.data() + off, 1, s1.data(), 1));
                }
                else if (nm == "steps") {
                    // int64[1] — diffusion steps
                    size_t off = i64_store.size();
                    i64_store.push_back(1000);  // full steps; depth controls actual depth
                    in_vals.push_back(Ort::Value::CreateTensor<int64_t>(
                        mem_info_, i64_store.data() + off, 1, s1.data(), 1));
                }
                else if (nm == "speedup") {
                    size_t off = i64_store.size();
                    i64_store.push_back(10);
                    in_vals.push_back(Ort::Value::CreateTensor<int64_t>(
                        mem_info_, i64_store.data() + off, 1, s1.data(), 1));
                }
                else if (nm == "spk_embed") {
                    // float[1,H] — speaker embedding, use zeros
                    in_vals.push_back(make_default_tensor(
                        *acoustic_sess_, i, T, N, i64_store, f32_store, 0, 0.0f));
                }
                else {
                    // Unknown input — query type and provide correct-typed default
                    ds_log("    unknown input '%s' → typed default", nm.c_str());
                    in_vals.push_back(make_default_tensor(
                        *acoustic_sess_, i, T, N, i64_store, f32_store, 0, 0.0f));
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

    // ==================================================================
    // Note hashing for render cache
    // ==================================================================

    static uint64_t hash_notes(const std::vector<NoteInfo>& notes) {
        // FNV-1a 64-bit hash over the note data that affects rendering:
        // beat, pitch, duration, lyric
        uint64_t h = 14695981039346656037ULL;
        auto mix = [&](const void* data, size_t len) {
            auto* p = static_cast<const uint8_t*>(data);
            for (size_t i = 0; i < len; ++i) {
                h ^= p[i];
                h *= 1099511628211ULL;
            }
        };
        for (const auto& n : notes) {
            mix(&n.beat, sizeof(n.beat));
            mix(&n.pitch, sizeof(n.pitch));
            mix(&n.duration_beats, sizeof(n.duration_beats));
            mix(n.lyric.data(), n.lyric.size());
        }
        return h;
    }

    // ==================================================================
    // Auto-discovery from model_dir + dsconfig.yaml
    // ==================================================================

    // Join path components, ensuring exactly one separator
    static std::string pjoin(const std::string& a, const std::string& b) {
        if (a.empty()) return b;
        if (b.empty()) return a;
        bool a_slash = (a.back() == '/');
        bool b_slash = (b.front() == '/');
        if (a_slash && b_slash) return a + b.substr(1);
        if (!a_slash && !b_slash) return a + "/" + b;
        return a + b;
    }

    static bool file_exists(const std::string& p) {
        std::ifstream f(p); return f.good();
    }

    // Find first *.onnx in a directory
    static std::string find_onnx_in_dir(const std::string& dir) {
        // Use a simple approach: try common names, then glob
        for (const char* name : {"model.onnx", "vocoder.onnx"}) {
            std::string p = pjoin(dir, name);
            if (file_exists(p)) return p;
        }
        // Try shell glob
        std::string cmd = "ls " + dir + "/*.onnx 2>/dev/null | head -1";
        if (FILE* fp = popen(cmd.c_str(), "r")) {
            char buf[512]; buf[0] = 0;
            if (fgets(buf, sizeof(buf), fp)) {
                size_t len = std::strlen(buf);
                if (len > 0 && buf[len-1] == '\n') buf[len-1] = 0;
            }
            pclose(fp);
            if (buf[0]) return std::string(buf);
        }
        return {};
    }

    void discover_models() {
        std::string base = model_dir_;
        // Strip trailing slash
        while (!base.empty() && base.back() == '/') base.pop_back();

        ds_log("discover_models: base='%s'", base.c_str());

        // 1. Find dsconfig.yaml
        if (dsconfig_path_.empty()) {
            std::string p = pjoin(base, "dsconfig.yaml");
            if (file_exists(p)) dsconfig_path_ = p;
            else ds_log("  dsconfig.yaml not found in %s", base.c_str());
        }

        // 2. Parse dsconfig.yaml to discover relative paths
        if (!dsconfig_path_.empty()) {
            std::ifstream f(dsconfig_path_);
            if (f.is_open()) {
                std::string line;
                while (std::getline(f, line)) {
                    auto pound = line.find('#');
                    if (pound != std::string::npos) line.resize(pound);
                    auto colon = line.find(':');
                    if (colon == std::string::npos) continue;
                    // Only look at top-level keys (no leading whitespace)
                    if (line[0] == ' ' || line[0] == '\t') continue;

                    auto trim = [](std::string s) {
                        size_t a = s.find_first_not_of(" \t\r\n");
                        size_t b = s.find_last_not_of(" \t\r\n");
                        return (a == std::string::npos) ? std::string() : s.substr(a, b - a + 1);
                    };
                    std::string key = trim(line.substr(0, colon));
                    std::string val = trim(line.substr(colon + 1));

                    if (key == "acoustic" && acoustic_path_.empty()) {
                        std::string p = pjoin(base, val);
                        if (file_exists(p)) {
                            acoustic_path_ = p;
                            ds_log("  acoustic: %s", p.c_str());
                        }
                    }
                    else if (key == "phonemes" && dict_path_.empty()) {
                        std::string p = pjoin(base, val);
                        if (file_exists(p)) {
                            dict_path_ = p;
                            ds_log("  phonemes: %s", p.c_str());
                        }
                    }
                    else if (key == "vocoder" && vocoder_path_.empty()) {
                        // May be a directory name or a .onnx path
                        std::string p = pjoin(base, val);
                        if (file_exists(p) && val.find(".onnx") != std::string::npos) {
                            vocoder_path_ = p;
                        } else {
                            // It's a directory — find .onnx inside
                            std::string onnx = find_onnx_in_dir(p);
                            if (!onnx.empty()) vocoder_path_ = onnx;
                        }
                        if (!vocoder_path_.empty())
                            ds_log("  vocoder: %s", vocoder_path_.c_str());
                    }
                }
            }
        }

        // 3. Look for duration model in common locations
        if (duration_path_.empty()) {
            for (const char* sub : {
                "dsdur/files/dur.onnx", "dsdur/dur.onnx",
                "dsdur/files/duration.onnx", "duration.onnx"
            }) {
                std::string p = pjoin(base, sub);
                if (file_exists(p)) {
                    duration_path_ = p;
                    ds_log("  duration: %s", p.c_str());
                    break;
                }
            }
        }

        // 4. Fallback: look for acoustic.onnx directly if dsconfig didn't specify
        if (acoustic_path_.empty()) {
            for (const char* sub : {
                "dsacoustic/acoustic.onnx", "acoustic.onnx",
                "dsacoustic/model.onnx"
            }) {
                std::string p = pjoin(base, sub);
                if (file_exists(p)) {
                    acoustic_path_ = p;
                    ds_log("  acoustic (fallback): %s", p.c_str());
                    break;
                }
            }
        }

        // 5. Fallback: find phoneme dict
        if (dict_path_.empty()) {
            for (const char* sub : {
                "dsacoustic/phonemes.json", "phonemes.json",
                "dsacoustic/phonemes.txt", "phonemes.txt",
                "dsdict.txt"
            }) {
                std::string p = pjoin(base, sub);
                if (file_exists(p)) {
                    dict_path_ = p;
                    ds_log("  dict (fallback): %s", p.c_str());
                    break;
                }
            }
        }

        ds_log("discover_models: acoustic=%s vocoder=%s duration=%s dict=%s",
               acoustic_path_.empty() ? "(none)" : "OK",
               vocoder_path_.empty() ? "(none)" : "OK",
               duration_path_.empty() ? "(none)" : "OK",
               dict_path_.empty() ? "(none)" : "OK");
    }

    // ==================================================================
    // Phoneme set auto-detection
    // ==================================================================

    void detect_phoneme_set() {
        if (phoneme_dict_.empty()) {
            phoneme_set_ = "ipa";
            return;
        }

        // Check if dict keys have language prefixes (en/xx style)
        // and what the unprefixed phonemes look like
        int arpa_hits = 0, ipa_hits = 0, xsampa_hits = 0, total = 0;

        // ARPAbet indicators: aa, ae, ah, ao, aw, ax, ay, eh, er, ey, ih, iy, ow, uh, uw
        static const char* arpa_vowels[] = {
            "aa","ae","ah","ao","aw","ax","ay","eh","er","ey",
            "ih","iy","ow","uh","uw","oy","jh","hh","ng","sh","ch","zh","th","dh"
        };

        for (const auto& [key, id] : phoneme_dict_) {
            // Strip language prefix if present
            std::string ph = key;
            auto slash = ph.find('/');
            if (slash != std::string::npos) ph = ph.substr(slash + 1);
            if (ph.empty() || ph == "AP" || ph == "SP") continue;

            total++;
            for (const char* a : arpa_vowels) {
                if (ph == a) { arpa_hits++; break; }
            }
            // IPA indicator: contains non-ASCII
            for (char c : ph) {
                if (static_cast<unsigned char>(c) > 127) { ipa_hits++; break; }
            }
        }

        if (total > 0) {
            float arpa_frac = arpa_hits / (float)total;
            float ipa_frac  = ipa_hits / (float)total;

            if (arpa_frac > 0.05f) phoneme_set_ = "arpabet";
            else if (ipa_frac > 0.3f) phoneme_set_ = "ipa";
            else phoneme_set_ = "arpabet";  // default for ASCII-only dicts
        } else {
            phoneme_set_ = "ipa";
        }

        ds_log("detect_phoneme_set: %d arpa/%d ipa/%d total → %s",
               arpa_hits, ipa_hits, total, phoneme_set_.c_str());
    }

    // ==================================================================
    // Model I/O logging
    // ==================================================================

    void log_model_io(const char* label, Ort::Session& sess) {
        Ort::AllocatorWithDefaultOptions alloc;
        auto type_str = [](ONNXTensorElementDataType t) -> const char* {
            switch (t) {
                case ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT:   return "float32";
                case ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64:   return "int64";
                case ONNX_TENSOR_ELEMENT_DATA_TYPE_INT32:   return "int32";
                case ONNX_TENSOR_ELEMENT_DATA_TYPE_DOUBLE:  return "float64";
                case ONNX_TENSOR_ELEMENT_DATA_TYPE_INT8:    return "int8";
                case ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT16: return "float16";
                default: return "other";
            }
        };
        size_t n_in = sess.GetInputCount();
        for (size_t i = 0; i < n_in; ++i) {
            auto name = sess.GetInputNameAllocated(i, alloc);
            auto info = sess.GetInputTypeInfo(i).GetTensorTypeAndShapeInfo();
            auto shape = info.GetShape();
            std::string sh;
            for (size_t j = 0; j < shape.size(); ++j) {
                if (j) sh += ",";
                sh += (shape[j] < 0) ? "?" : std::to_string(shape[j]);
            }
            ds_log("  %s input[%zu]: %-20s %s[%s]", label, i,
                   name.get(), type_str(info.GetElementType()), sh.c_str());
        }
        size_t n_out = sess.GetOutputCount();
        for (size_t i = 0; i < n_out; ++i) {
            auto name = sess.GetOutputNameAllocated(i, alloc);
            auto info = sess.GetOutputTypeInfo(i).GetTensorTypeAndShapeInfo();
            auto shape = info.GetShape();
            std::string sh;
            for (size_t j = 0; j < shape.size(); ++j) {
                if (j) sh += ",";
                sh += (shape[j] < 0) ? "?" : std::to_string(shape[j]);
            }
            ds_log("  %s output[%zu]: %-20s %s[%s]", label, i,
                   name.get(), type_str(info.GetElementType()), sh.c_str());
        }
    }

    // ==================================================================
    // Provider selection & model loading
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
                    auto status = Ort::GetApi().CreateCUDAProviderOptions(&co);
                    if (status == nullptr) {
                        opts.AppendExecutionProvider_CUDA_V2(*co);
                        Ort::GetApi().ReleaseCUDAProviderOptions(co);
                        ds_log("CUDA execution provider added");
                    } else {
                        ds_log("CUDA provider creation failed → CPU");
                        use_gpu_ = false;
                    }
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
            log_model_io("acoustic", *acoustic_sess_);
            ds_log("Loading vocoder: %s", vocoder_path_.c_str());
            vocoder_sess_  = std::make_unique<Ort::Session>(*ort_env_, vocoder_path_.c_str(), opts);
            log_model_io("vocoder", *vocoder_sess_);
            if (!duration_path_.empty()) {
                ds_log("Loading duration: %s", duration_path_.c_str());
                duration_sess_ = std::make_unique<Ort::Session>(*ort_env_, duration_path_.c_str(), opts);
                log_model_io("duration", *duration_sess_);
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
