// ddsp_plugin.cpp
// ==========================================================================
// DDSP (Differentiable Digital Signal Processing) Neural Synthesizer
// ==========================================================================
//
// Standardised to the acids-ircam/ddsp_pytorch model (and compatible forks,
// e.g. hugggof/violin-ddsp). A small ONNX decoder, exported by
// scripts/convert_ddsp.py, converts a per-frame (pitch, loudness) pair into
// synthesis parameters; C++ reproduces the harmonic + filtered-noise synthesis
// exactly as ddsp.realtime_forward.
//
//   ONNX decoder (per frame):
//     in : pitch[1,1,1] (Hz), loudness[1,1,1] (raw; z-score baked in),
//          cache_in[1,1,H] (GRU state)
//     out: amplitudes[1,n_harm]   (nyquist-removed, renormalised, x total_amp)
//          noise_param[1,n_bands] (band magnitudes for the noise FIR)
//          cache_out[1,1,H]       (GRU state for the next frame)
//
// Inference is driven by the AUDIO clock, not wall-clock: a new frame is
// inferred synchronously each time the frame phase crosses a boundary (~one
// small GRU step per block). This keeps offline rendering correct (it runs
// faster than realtime, so a wall-clock helper thread would starve) and the
// GRU cache + oscillator phase advance deterministically.
//
//   - Harmonics: oscillator bank at host SR (amplitudes are SR-independent and
//     already nyquist-filtered by the model).
//   - Noise: amp_to_impulse_response + a direct causal FIR at model SR
//     (algebraically identical to fft_convolve(noise,impulse)[block:]),
//     resampled to host SR via the frame phase.
//
// The model is monophonic (a single stateful pitch/loudness contour), matching
// both the reference design and the instruments it models.
// ==========================================================================

#include "plugin_api.h"
#include "adsr.h"
#include "note_attr_latch.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#ifdef AS_ENABLE_DDSP
#include <onnxruntime_cxx_api.h>
#endif

// ==========================================================================
// Logging
// ==========================================================================

#define DDSP_LOG(fmt, ...) \
    std::fprintf(stderr, "[DDSP] " fmt "\n", ##__VA_ARGS__)

// ==========================================================================
// Constants
// ==========================================================================

static constexpr int MAX_HARMONICS   = 128;
static constexpr int MAX_NOISE_BANDS = 128;
static constexpr int MAX_BLOCK       = 2048;  // model-rate noise block ceiling
static constexpr int SIN_TABLE_SIZE  = 1024;
static constexpr int F0_MAX_HARM     = 8;     // ceiling on f0 phase harmonics
static constexpr int EXPR_MAX_HARM   = 8;     // ceiling on tremolo phase harmonics

// Loudness-calibration node-param defaults. These double as the "not calibrated"
// sentinel: when the incoming param still equals its default we let the model's
// config.json calibration win (see resolve_loudness_calibration). Keep in sync
// with the config_params defaults in descriptor().
static constexpr float DEFAULT_LOUD_FLOOR_DB = -60.0f;
static constexpr float DEFAULT_LOUD_CEIL_DB  =   0.0f;

// ==========================================================================
// Wavetable sine lookup
// ==========================================================================

static float g_sin_table[SIN_TABLE_SIZE + 1];  // +1 for interpolation guard

static void init_sin_table() {
    static bool done = false;
    if (done) return;
    for (int i = 0; i <= SIN_TABLE_SIZE; ++i)
        g_sin_table[i] = std::sin(2.0f * static_cast<float>(M_PI) * i / SIN_TABLE_SIZE);
    done = true;
}

static inline float fast_sin(float phase) {
    // phase in [0,1)
    float idx = phase * SIN_TABLE_SIZE;
    int i = static_cast<int>(idx);
    float frac = idx - i;
    i &= (SIN_TABLE_SIZE - 1);
    return g_sin_table[i] + frac * (g_sin_table[i + 1] - g_sin_table[i]);
}

// ==========================================================================
// DDSPFrame — per-frame parameter snapshot from inference
// ==========================================================================

struct DDSPFrame {
    float amplitudes[MAX_HARMONICS]    = {};  // per-harmonic linear amplitude
    float noise_param[MAX_NOISE_BANDS] = {};  // band magnitudes for the FIR
    int   n_harm  = 0;
    int   n_bands = 0;
    bool  valid   = false;
};

// ==========================================================================
// RNG — xorshift for white noise
// ==========================================================================

struct XorShift32 {
    uint32_t state = 0x9e3779b9u;
    float next() {  // [-1, 1)
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        return static_cast<float>(static_cast<int32_t>(state)) / 2147483648.0f;
    }
};

#ifdef AS_ENABLE_DDSP
// True iff the loaded graph declares an input with the given name. Used to
// confirm a latent 'style' input is actually present before feeding one (the
// config block declares intent; the graph is authoritative).
static bool session_has_input(Ort::Session& s, const char* name) {
    Ort::AllocatorWithDefaultOptions alloc;
    size_t n = s.GetInputCount();
    for (size_t i = 0; i < n; ++i) {
        auto in = s.GetInputNameAllocated(i, alloc);
        if (std::strcmp(in.get(), name) == 0) return true;
    }
    return false;
}
#endif

// ==========================================================================
// DDSPPlugin (monophonic)
// ==========================================================================

class DDSPPlugin : public Plugin {
public:
    DDSPPlugin() { init_sin_table(); }
    ~DDSPPlugin() override = default;

    // ------------------------------------------------------------------
    // Descriptor
    // ------------------------------------------------------------------

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.ddsp";
        d.display_name = "DDSP Synth";
        d.category     = "Synth";
        d.doc          = "Monophonic neural synthesizer using the acids-ircam DDSP "
                         "model (and compatible forks). An ONNX decoder maps pitch + "
                         "loudness to a harmonic oscillator bank and a filtered-noise "
                         "band, reproducing the modelled instrument. Convert models "
                         "with scripts/convert_ddsp.py.";
        d.author       = "builtin";
        d.version      = 3;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output.",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain", "Output gain multiplier. 1.0 = unity.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 4.0f },
            { "expression", "Expression",
              "Loudness expression (0-1), scales the loudness fed to the model.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f },
            { "noise_gain", "Noise Gain",
              "Scales the filtered-noise (breath/bow) component. 1.0 = as modelled; "
              "lower to tame noise when driving the model with static MIDI notes.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f },
            { "attack", "Attack",
              "Attack time in seconds. Ramps the loudness fed to the model (the "
              "swell that produces a natural onset) and the output gain.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.05f, 0.001f, 2.0f },
            { "release", "Release",
              "Output envelope release time in seconds.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.001f, 5.0f },
            { "vibrato", "Vibrato",
              "Scales the learned f0 (vibrato/scoop) deviation. 1.0 = as modelled; "
              "0 = flat pitch. Only effective for models with an f0 expression net.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f },
            { "style_x", "Timbre X",
              "Timbre style-pad X coordinate (normalised -1..1, mapped onto the "
              "model's useful range). 0 = average timbre. Only for latent decoders.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -1.0f, 1.0f },
            { "style_y", "Timbre Y",
              "Timbre style-pad Y coordinate (normalised -1..1). 0 = average timbre. "
              "Only for latent decoders.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, -1.0f, 1.0f },
            // NOTE: the performance style pad (perf_x/perf_y -> expression +
            // f0_expression 'style') was dropped — it barely altered the output
            // for our instruments. Models exported with a perf-latent 'style'
            // input are still fed the mean (0,0) for backwards compatibility
            // (see process_block); only the user-facing pad is gone.
        };

        d.config_params = {
            { "model_dir", "Model Directory",
              "DDSP model directory containing decoder.onnx and config.json "
              "(produced by scripts/convert_ddsp.py).",
              ConfigType::DirPath, "" },
            { "use_expression", "Use Expression Model",
              "When on, the model's bundled expression network (if any) drives "
              "loudness (and pitch). Turn off to use the simple "
              "velocity/expression -> loudness mapping below.",
              ConfigType::Bool, "true" },
            { "note_mode", "Note Priority",
              "How a new note behaves while another is still held (the model is "
              "monophonic). 'retrigger' re-articulates each note with a fresh "
              "onset; 'legato' glides pitch into the new note, keeping the model's "
              "evolving timbre/loudness state and oscillator phase — a slur, no "
              "re-attack. Releasing back onto a still-held note glides too.",
              ConfigType::Categorical, "retrigger", "",
              false, false,
              { "retrigger", "legato" } },
            { "loud_floor_db", "Loudness Floor (dB)",
              "Raw loudness (model units) at velocity/expression = 0. Calibrate "
              "to the model's training loudness range.",
              ConfigType::Float, "-60", "", false, true },
            { "loud_ceil_db", "Loudness Ceiling (dB)",
              "Raw loudness (model units) at full velocity/expression.",
              ConfigType::Float, "0", "", false, true },
        };

        // Per-note (onset-latched) lanes. Each MULTIPLIES the corresponding
        // control (neutral 1.0), composing on top of the knob/automation value —
        // so a per-note value and a track-level curve coexist. Only controls that
        // are independent of velocity and meaningful as a single note value are
        // exposed (expression is omitted: it's redundant with velocity; attack is
        // omitted: inert under the expression model). Ranges are log-symmetric
        // around 1.0 so the lane's neutral sits mid-height.
        d.note_attrs = {
            { "vibrato", "Vibrato",
              "Per-note multiplier on vibrato depth (1.0 = unchanged). Composes on "
              "top of the Vibrato control; effective only on models with an f0 "
              "expression net.",
              ControlHint::Continuous, 1.0f, 0.25f, 4.0f, {} },
            { "breath", "Breath",
              "Per-note multiplier on breath/noise gain (1.0 = unchanged). Composes "
              "on top of the Noise Gain control — airy vs pure tone per note.",
              ControlHint::Continuous, 1.0f, 0.25f, 4.0f, {} },
            { "release", "Release",
              "Per-note multiplier on release time (1.0 = unchanged): shorter = "
              "more staccato, longer = more let-ring. Composes on top of the "
              "Release control.",
              ControlHint::Continuous, 1.0f, 0.25f, 4.0f, {} },
        };

        // Keep the node visually clean: default-hide every control-port DOT on the
        // canvas except the commonly-automated 'expression'. The inspector still
        // renders every knob (so values stay editable), and a hidden dot can be
        // revealed from its port context menu. This only seeds NEW nodes.
        for (auto& p : d.ports) {
            if (p.type == PluginPortType::Control && p.role == PortRole::Input &&
                p.id != "expression")
                p.show_port_default = false;
        }

        return d;
    }

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    void configure(const std::string& key, const std::string& value) override {
        if (key == "model_dir") {
            pending_model_dir_ = value;
            model_dirty_ = true;
            maybe_load_model();   // configure() runs on the control thread, not audio
        } else if (key == "use_expression") {
            use_expression_ = (value == "true" || value == "1" || value == "True");
        } else if (key == "note_mode") {
            legato_ = (value == "legato");
        } else if (key == "loud_floor_db") {
            // The host serialises EVERY config param each push, including ones the
            // user never touched — so a value equal to the default is NOT a user
            // override. Treat default == "uncalibrated" and let config.json win;
            // only a real edit (value != default) pins the param. (Bug: previously
            // the default push set loud_floor_user_, shadowing the model's
            // calibrated loud_floor_db and leaving renders ~15 dB too quiet.)
            if (!value.empty()) { try {
                float v = std::stof(value);
                loud_floor_user_ = std::fabs(v - DEFAULT_LOUD_FLOOR_DB) > 1e-4f;
                user_loud_floor_db_ = v;
                resolve_loudness_calibration();
            } catch (...) {} }
        } else if (key == "loud_ceil_db") {
            if (!value.empty()) { try {
                float v = std::stof(value);
                loud_ceil_user_ = std::fabs(v - DEFAULT_LOUD_CEIL_DB) > 1e-4f;
                user_loud_ceil_db_ = v;
                resolve_loudness_calibration();
            } catch (...) {} }
        }
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        host_sr_ = sample_rate;
        activated_ = true;
        reset_voice();
        maybe_load_model();       // config may have arrived before activation
        recompute_frame_advance();
    }

    void deactivate() override { reset_voice(); }

    // ------------------------------------------------------------------
    // MIDI events (audio thread)
    // ------------------------------------------------------------------

    void note_on(int channel, int pitch, int velocity) override {
        if (velocity == 0) { note_off(channel, pitch); return; }
        // Legato: if a note is already sounding (held, not yet releasing), glide
        // into the new one instead of re-articulating. Otherwise (retrigger mode,
        // or the previous note already released) start fresh.
        bool glide = legato_ && v_.active && !v_.releasing;
        held_.push_back({channel, pitch, velocity});
        if (glide) glide_to_note(channel, pitch, velocity);
        else       start_note(channel, pitch, velocity);
    }

    void note_off(int channel, int pitch) override {
        for (auto it = held_.begin(); it != held_.end(); ++it) {
            if (it->ch == channel && it->pitch == pitch) {
                bool was_top = (it + 1 == held_.end());
                held_.erase(it);
                if (was_top) {
                    if (!held_.empty()) {
                        // Fall back to the next held note. The voice is still
                        // sounding, so legato glides; retrigger re-articulates.
                        auto& n = held_.back();
                        if (legato_) glide_to_note(n.ch, n.pitch, n.vel);
                        else         start_note(n.ch, n.pitch, n.vel);
                    } else {
                        v_.releasing = true;
                        v_.env.release();
                        expr_gate_ = false;          // expression model: enter release
                        expr_t_rel_sec_ = 0.0f;
                    }
                }
                break;
            }
        }
    }

    void all_notes_off(int /*channel*/) override {
        held_.clear();
        v_.releasing = true;
        v_.env.release();
        expr_gate_ = false;
        expr_t_rel_sec_ = 0.0f;
    }

    void note_tune(int /*channel*/, int note, float semitones) override {
        if (v_.active && v_.pitch == note) {
            v_.tune_semitones = semitones;
            v_.f0_hz = midi_to_hz(v_.pitch + semitones);
        }
    }

    // Per-note attribute (dispatched just before its note_on). Stash until the
    // note starts; start_note/glide_to_note drain it into the voice multipliers.
    void note_attr(int channel, int note, const std::string& id, float value) override {
        pending_attrs_.set(channel, note, id.c_str(), value);
    }

    // ------------------------------------------------------------------
    // Process (audio thread) — inference is driven here, synchronously
    // ------------------------------------------------------------------

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* out = buffers.audio.get("audio_out");
        if (!out) return;

        float* L = out->left;
        float* R = out->right ? out->right : out->left;
        const int N = ctx.block_size;

        std::memset(L, 0, N * sizeof(float));
        if (out->right) std::memset(R, 0, N * sizeof(float));

        auto ctrl = [&](const char* id, float fallback) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fallback;
        };
        float gain       = ctrl("gain", 1.0f);
        float expression = ctrl("expression", 1.0f);
        float noise_gain = ctrl("noise_gain", 1.0f);
        attack_time_     = ctrl("attack", 0.05f);
        release_time_    = ctrl("release", 0.1f);
        vibrato_amount_  = ctrl("vibrato", 1.0f);

        // Timbre style pad: normalised [-1,1] node params mapped onto the
        // model's useful pad range (from config extent). Held constant within
        // the block; (0,0) == the mean embedding. Read once per block (timbre
        // selector, not a per-sample modulation). Harmless when the model has
        // no latent input. The performance pad was dropped; perf_px_/perf_py_
        // stay at the mean (0,0) and are still fed to perf-latent models below.
        if (has_style_) {
            style_px_ = map_pad(ctrl("style_x", 0.0f), style_x_lo_, style_x_hi_);
            style_py_ = map_pad(ctrl("style_y", 0.0f), style_y_lo_, style_y_hi_);
        }

        // Target loudness level [0,1] from velocity + expression. The actual
        // loudness fed to the model is this scaled by the attack/release envelope
        // (below), so the model sees a SWELL into the note rather than a step —
        // that rising-loudness cue is what makes it render a natural (breathy /
        // bowed) attack instead of slamming out full harmonics on frame one.
        cur_level_ = std::clamp((v_.velocity / 127.0f) * expression, 0.0f, 1.0f);

        if (!v_.active) return;

        noise_gain *= v_.attr_breath;   // per-note breath/noise multiplier

        const int block = std::max(1, std::min(block_size_, MAX_BLOCK));

        for (int i = 0; i < N; ++i) {
            // Infer a new frame each time the phase crosses a boundary.
            while (v_.frame_phase >= 1.0f) {
                DDSPFrame nf;
                // Pitch expression (vibrato/scoop) bends f0 for BOTH the decoder
                // inference (nyquist-correct amplitudes) and the oscillator.
                if (has_f0_expr_ && use_expression_) {
                    float midi = static_cast<float>(v_.pitch) + v_.tune_semitones;
                    v_.f0_hz = midi_to_hz(midi + f0_dev_for_frame());
                }
                float loud_db = loudness_for_frame();
                if (!infer_frame(v_.f0_hz, loud_db, nf))
                    make_default_frame(nf);
                // Advance the causal note timers once per frame (shared by both
                // expression models), regardless of which are present.
                expr_t_onset_sec_ += frame_sec_;
                if (!expr_gate_) expr_t_rel_sec_ += frame_sec_;
                v_.last_frame = v_.cur_frame;
                v_.cur_frame  = nf;
                v_.frame_phase -= 1.0f;
                rebuild_noise_block(block);
            }

            float env_val = v_.env.next();
            if (v_.env.is_off()) { v_.active = false; break; }

            float t = std::min(v_.frame_phase, 1.0f);

            // --- Harmonic oscillator bank (host SR) ---
            float s = 0.0f;
            const float base_inc = v_.f0_hz / host_sr_;
            int nh = v_.cur_frame.valid ? v_.cur_frame.n_harm : 0;
            for (int h = 0; h < nh; ++h) {
                float a = v_.last_frame.amplitudes[h] * (1.0f - t) +
                          v_.cur_frame.amplitudes[h] * t;
                s += a * fast_sin(v_.harmonic_phase[h]);
                float ph = v_.harmonic_phase[h] + base_inc * (h + 1);
                v_.harmonic_phase[h] = ph - std::floor(ph);
            }

            // --- Filtered noise (model SR block, resampled via frame phase) ---
            float ns = 0.0f;
            if (v_.noise_valid) {
                float nidx = t * block;
                int i0 = static_cast<int>(nidx);
                float fr = nidx - i0;
                if (i0 < 0) i0 = 0;
                if (i0 > block - 1) i0 = block - 1;
                int i1 = std::min(i0 + 1, block - 1);
                ns = (v_.noise_block[i0] * (1.0f - fr) + v_.noise_block[i1] * fr) * noise_gain;
            }

            float smp = (s + ns) * env_val * gain;
            L[i] += smp;
            if (out->right) R[i] += smp;

            v_.frame_phase += frame_advance_per_sample_;
        }

        // Soft clip.
        for (int i = 0; i < N; ++i) {
            auto sc = [](float x) { return (x > 0.95f || x < -0.95f) ? std::tanh(x) : x; };
            L[i] = sc(L[i]);
            if (out->right) R[i] = sc(R[i]);
        }
    }

private:
    struct HeldNote { int ch, pitch, vel; };

    static inline float midi_to_hz(float note) {
        return 440.0f * std::pow(2.0f, (note - 69.0f) / 12.0f);
    }

    // Map a normalised pad axis n in [-1,1] onto the model's pad range [lo,hi],
    // passing through 0 (the mean embedding) at n == 0. lo is typically < 0 and
    // hi > 0, so each half is scaled independently to keep the centre at 0.
    static inline float map_pad(float n, float lo, float hi) {
        n = std::clamp(n, -1.0f, 1.0f);
        return (n >= 0.0f) ? n * hi : n * (-lo);
    }

    // ------------------------------------------------------------------
    // Note lifecycle
    // ------------------------------------------------------------------

    // Drain any per-note attrs latched for (channel,pitch) into the voice's
    // multipliers (neutral 1.0 when none). Onset-latched: read once per note.
    void drain_note_attrs(int channel, int pitch) {
        NoteAttrSet a;
        pending_attrs_.take(channel, pitch, a);
        v_.attr_vibrato = a.get_or("vibrato", 1.0f);
        v_.attr_breath  = a.get_or("breath",  1.0f);
        v_.attr_release = a.get_or("release", 1.0f);
    }

    void start_note(int channel, int pitch, int velocity) {
        v_.channel  = channel;
        v_.pitch    = pitch;
        v_.velocity = velocity;
        drain_note_attrs(channel, pitch);
        v_.tune_semitones = 0.0f;
        v_.f0_hz    = midi_to_hz(static_cast<float>(pitch));
        v_.frame_phase = 1.0f;          // force inference on the first sample
        v_.noise_valid = false;
        std::memset(v_.harmonic_phase, 0, sizeof(v_.harmonic_phase));

        // Reset the GRU caches so a new note starts from a clean recurrent state.
        std::fill(cache_.begin(), cache_.end(), 0.0f);
        std::fill(expr_cache_.begin(), expr_cache_.end(), 0.0f);
        std::fill(f0_expr_cache_.begin(), f0_expr_cache_.end(), 0.0f);

        // Expression-model note state (causal: onset now, no lookahead).
        expr_gate_ = true;
        expr_t_onset_sec_ = 0.0f;
        expr_t_rel_sec_ = 0.0f;
        f0_lfo_phase_ = 0.0f;            // restart the vibrato LFO at note-on
        tremolo_lfo_phase_ = 0.0f;       // restart the tremolo LFO at note-on

        make_default_frame(v_.cur_frame);   // immediate sound before first infer
        v_.last_frame = v_.cur_frame;

        // When the expression model owns the musical attack, the output ADSR is
        // just a short declick; otherwise it carries the attack swell itself.
        float atk = has_expr_ ? std::min(attack_time_, 0.005f) : attack_time_;
        v_.env.trigger(host_sr_, atk, 0.0f, 1.0f, release_time_ * v_.attr_release);
        v_.releasing = false;
        v_.active = true;
    }

    // Legato transition: retune the sounding voice into a new note WITHOUT
    // re-articulating. Everything that carries the model's evolving state is kept
    // — GRU caches, the output envelope, oscillator phases, the onset timer and
    // the vibrato/tremolo LFO phases — so the contour slides continuously into the
    // new pitch (a slur), and phase continuity avoids a click. Only the
    // pitch/velocity targets change; the decoder picks up the new f0 on its next
    // frame inference (for f0-expression models process() re-derives f0_hz anyway).
    void glide_to_note(int channel, int pitch, int velocity) {
        v_.channel  = channel;
        v_.pitch    = pitch;
        v_.velocity = velocity;
        drain_note_attrs(channel, pitch);   // a slurred note may re-shape vib/breath/release
        v_.tune_semitones = 0.0f;
        v_.f0_hz    = midi_to_hz(static_cast<float>(pitch));
        expr_gate_  = true;
        v_.releasing = false;
        v_.active   = true;
    }

    void reset_voice() {
        v_ = Voice{};
        std::fill(cache_.begin(), cache_.end(), 0.0f);
        std::fill(expr_cache_.begin(), expr_cache_.end(), 0.0f);
        std::fill(f0_expr_cache_.begin(), f0_expr_cache_.end(), 0.0f);
        expr_gate_ = false;
        expr_t_onset_sec_ = 0.0f;
        expr_t_rel_sec_ = 0.0f;
        f0_lfo_phase_ = 0.0f;
        tremolo_lfo_phase_ = 0.0f;
        held_.clear();
        pending_attrs_.clear();
    }

    // Resolve effective loudness calibration from the three sources, in priority
    // order: an explicit user edit > the model's config.json calibration > the
    // built-in default. Called whenever any of those inputs change, so the result
    // is independent of config-key / param arrival order.
    void resolve_loudness_calibration() {
        loud_floor_db_ = loud_floor_user_ ? user_loud_floor_db_
                       : (has_cfg_loud_floor_ ? cfg_loud_floor_db_ : DEFAULT_LOUD_FLOOR_DB);
        loud_ceil_db_  = loud_ceil_user_  ? user_loud_ceil_db_
                       : (has_cfg_loud_ceil_  ? cfg_loud_ceil_db_  : DEFAULT_LOUD_CEIL_DB);
    }

    void recompute_frame_advance() {
        float fps = (block_size_ > 0)
                        ? (static_cast<float>(model_sr_) / block_size_)
                        : 100.0f;
        frame_advance_per_sample_ = (host_sr_ > 0) ? (fps / host_sr_) : 0.0f;
        frame_sec_ = (fps > 0.0f) ? (1.0f / fps) : 0.01f;   // seconds per model frame
        // Vibrato/tremolo LFO increments in turns ([0,1)) per model frame: rate / frame_rate.
        f0_lfo_inc_      = (fps > 0.0f) ? (f0_vibrato_rate_ / fps) : 0.0f;
        tremolo_lfo_inc_ = (fps > 0.0f) ? (tremolo_rate_ / fps) : 0.0f;
    }

    // ------------------------------------------------------------------
    // Noise synthesis: amp_to_impulse_response + causal FIR (== reference
    // fft_convolve(noise, impulse)[block:]).
    // ------------------------------------------------------------------

    void rebuild_noise_block(int block) {
        if (!v_.cur_frame.valid || v_.cur_frame.n_bands < 2) {
            v_.noise_valid = false;
            return;
        }
        const int n_bands = v_.cur_frame.n_bands;
        const int F = 2 * (n_bands - 1);           // filter_size (irfft length)
        if (F < 2 || F > block) { v_.noise_valid = false; return; }

        const float* param = v_.cur_frame.noise_param;
        const float two_pi = 2.0f * static_cast<float>(M_PI);

        // irfft of the real magnitude spectrum (imag = 0) -> F real taps.
        float tmp[MAX_BLOCK];
        for (int n = 0; n < F; ++n) {
            float acc = param[0];
            for (int k = 1; k < F / 2; ++k)
                acc += 2.0f * param[k] * std::cos(two_pi * k * n / F);
            acc += param[F / 2] * std::cos(static_cast<float>(M_PI) * n);  // k = F/2
            tmp[n] = acc / F;
        }

        // roll right by F/2, Hann window, pad to block, roll left by F/2.
        const int half = F / 2;
        float rolled[MAX_BLOCK];
        for (int i = 0; i < F; ++i) {
            int src = ((i - half) % F + F) % F;
            float win = 0.5f * (1.0f - std::cos(two_pi * i / F));  // hann(F), periodic
            rolled[i] = tmp[src] * win;
        }
        float impulse[MAX_BLOCK];
        for (int i = 0; i < block; ++i) {
            int src = (i + half) % block;            // roll(-half) on length block
            impulse[i] = (src < F) ? rolled[src] : 0.0f;
        }

        // Causal convolution of a fresh white-noise block with the impulse.
        float white[MAX_BLOCK];
        for (int i = 0; i < block; ++i) white[i] = rng_.next();
        for (int p = 0; p < block; ++p) {
            float acc = 0.0f;
            for (int j = 0; j <= p; ++j) acc += white[p - j] * impulse[j];
            v_.noise_block[p] = acc;
        }
        v_.noise_valid = true;
    }

    // Silent placeholder frame. The first real inference runs on sample 0 of a
    // note (frame_phase is forced to 1.0 at note-on), so this frame's only audible
    // role is the <=1-frame (~10 ms) linear interpolation INTO that first
    // inference: starting from silence makes that a clean fade-in / declick.
    // (Previously this emitted a fixed harmonic bank summing to 0.3; once the body
    // sat ~24 dB lower it became a sharp note-on spike. It's also the fallback when
    // an inference fails mid-note, where silence is likewise the safe choice.)
    void make_default_frame(DDSPFrame& f) {
        int nh = std::min(n_harmonic_ > 0 ? n_harmonic_ : 64, MAX_HARMONICS);
        int nb = std::min(n_bands_ > 0 ? n_bands_ : 65, MAX_NOISE_BANDS);
        f.n_harm = nh; f.n_bands = nb; f.valid = true;
        for (int h = 0; h < nh; ++h) f.amplitudes[h]  = 0.0f;
        for (int b = 0; b < nb; ++b) f.noise_param[b] = 0.0f;
    }

    // ------------------------------------------------------------------
    // Loudness for the current frame: learned expression model if present,
    // else the velocity*expression -> dB mapping enveloped by the ADSR.
    // The expression path is CAUSAL and event-driven (no score lookahead), so
    // preview notes and upstream event-stream plugins behave normally.
    // ------------------------------------------------------------------

    float loudness_for_frame() {
        if (has_expr_ && use_expression_) {
            if (expr_gate_) {
                // Note held: the model drives the attack + sustain contour.
                // The velocity feature is scaled by the expression control
                // (cur_level_ = velocity/127 * expression), so 'expression' is a
                // live dynamics/swell knob, not just a fallback-path control.
                float midi = static_cast<float>(v_.pitch) + v_.tune_semitones;
                // Base note features (5); for tremolo models append the H phase
                // harmonics [sin kφ, cos kφ] of a free LFO the plugin runs at the
                // model's calibrated tremolo_rate (phase reset at note-on, advanced
                // once per frame — exactly the amplitude analogue of the f0 LFO).
                float feat[5 + 2 * EXPR_MAX_HARM] = {
                    (midi - 69.0f) / 12.0f, 1.0f, cur_level_,
                    std::min(expr_t_onset_sec_, expr_t_clip_) / expr_t_clip_, 0.0f,
                };
                if (has_tremolo_) {
                    const float ph = tremolo_lfo_phase_;
                    for (int k = 1; k <= tremolo_n_harm_; ++k) {
                        feat[5 + 2 * (k - 1)]     = fast_sin(k * ph);
                        feat[5 + 2 * (k - 1) + 1] = fast_sin(k * ph + 0.25f);
                    }
                    tremolo_lfo_phase_ += tremolo_lfo_inc_;
                    if (tremolo_lfo_phase_ >= 1.0f) tremolo_lfo_phase_ -= 1.0f;
                }
                float loud;
                if (run_expr(feat, loud)) { last_loud_db_ = loud; return loud; }
            } else {
                // Released: deterministic monotonic decay from the held loudness
                // to the floor. We do NOT query the model here — its learned
                // release is unreliable (self-supervised note offsets don't align
                // with loudness drops, so it tends to swell back up), which caused
                // a double-articulation at note transitions.
                float eff_release = release_time_ * v_.attr_release;
                float r = (eff_release > 1e-4f)
                              ? std::min(expr_t_rel_sec_ / eff_release, 1.0f) : 1.0f;
                return last_loud_db_ + (loud_floor_db_ - last_loud_db_) * r;
            }
        }
        // Fallback: velocity*expression -> dB, swelled by the ADSR level.
        float lvl = cur_level_ * v_.env.level;
        return loud_floor_db_ + (loud_ceil_db_ - loud_floor_db_) * lvl;
    }

    bool run_expr([[maybe_unused]] const float* feat, [[maybe_unused]] float& loud) {
#ifdef AS_ENABLE_DDSP
        if (!expr_session_ || expr_hidden_ <= 0) return false;
        try {
            int64_t sfeat[3]  = {1, 1, static_cast<int64_t>(expr_feat_dim_)};
            int64_t scache[3] = {1, 1, static_cast<int64_t>(expr_hidden_)};
            int64_t sstyle[3] = {1, 1, 2};
            std::vector<Ort::Value> ins;
            ins.reserve(3);
            // Input order matches the graph: feat, [style,] cache_in.
            ins.push_back(Ort::Value::CreateTensor<float>(
                mem_info_, const_cast<float*>(feat), expr_feat_dim_, sfeat, 3));
            if (has_perf_style_expr_) {
                perf_style_[0] = perf_px_; perf_style_[1] = perf_py_;
                ins.push_back(Ort::Value::CreateTensor<float>(
                    mem_info_, perf_style_, 2, sstyle, 3));
            }
            ins.push_back(Ort::Value::CreateTensor<float>(
                mem_info_, expr_cache_.data(), expr_cache_.size(), scache, 3));
            const char* in_names_p[] = {"feat", "style", "cache_in"};
            const char* in_names_n[] = {"feat", "cache_in"};
            const char** in_names = has_perf_style_expr_ ? in_names_p : in_names_n;
            const char* out_names[] = {"loudness", "cache_out"};
            auto outs = expr_session_->Run(Ort::RunOptions{nullptr},
                                           in_names, ins.data(), ins.size(),
                                           out_names, 2);
            if (outs.size() < 2) return false;
            loud = outs[0].GetTensorData<float>()[0];
            std::memcpy(expr_cache_.data(), outs[1].GetTensorData<float>(),
                        static_cast<size_t>(expr_hidden_) * sizeof(float));
            return true;
        } catch (const Ort::Exception& e) {
            DDSP_LOG("Expression inference error: %s", e.what());
            return false;
        }
#else
        return false;
#endif
    }

    // ------------------------------------------------------------------
    // f0 expression: per-frame pitch deviation (vibrato / scoop) in semitones,
    // relative to the nominal note pitch. Phase-conditioned (NOT autoregressive):
    // the plugin drives a free-running LFO at the model's calibrated vibrato_rate
    // and feeds its phase harmonics [sin kφ, cos kφ] as features, so the net rides
    // a supplied oscillation and cannot drift. Bounded by max_dev·tanh in-graph;
    // the host 'vibrato' control scales the depth.
    // ------------------------------------------------------------------

    float f0_dev_for_frame() {
        if (!has_f0_expr_) return 0.0f;
        float midi = static_cast<float>(v_.pitch) + v_.tune_semitones;
        float feat[5 + 2 * F0_MAX_HARM] = {
            (midi - 69.0f) / 12.0f,
            expr_gate_ ? 1.0f : 0.0f,
            v_.velocity / 127.0f,
            std::min(expr_t_onset_sec_, expr_t_clip_) / expr_t_clip_,
            expr_gate_ ? 0.0f : std::min(expr_t_rel_sec_, expr_t_clip_) / expr_t_clip_,
        };
        // Phase harmonics [sin kφ, cos kφ], k = 1..H. Phase kept in turns [0,1);
        // fast_sin(p) == sin(2πp), so cos(2πp) == fast_sin(p + 0.25).
        const float ph = f0_lfo_phase_;
        for (int k = 1; k <= f0_n_harm_; ++k) {
            feat[5 + 2 * (k - 1)]     = fast_sin(k * ph);
            feat[5 + 2 * (k - 1) + 1] = fast_sin(k * ph + 0.25f);
        }
        f0_lfo_phase_ += f0_lfo_inc_;
        if (f0_lfo_phase_ >= 1.0f) f0_lfo_phase_ -= 1.0f;

        float dev;
        if (run_f0_expr(feat, dev)) {
            dev *= vibrato_amount_ * v_.attr_vibrato;        // knob × per-note depth
            return std::clamp(dev, -f0_max_dev_, f0_max_dev_);
        }
        return 0.0f;
    }

    bool run_f0_expr([[maybe_unused]] const float* feat, [[maybe_unused]] float& dev) {
#ifdef AS_ENABLE_DDSP
        if (!f0_expr_session_ || f0_expr_hidden_ <= 0) return false;
        try {
            int64_t sfeat[3]  = {1, 1, static_cast<int64_t>(f0_feat_dim_)};
            int64_t scache[3] = {1, 1, static_cast<int64_t>(f0_expr_hidden_)};
            int64_t sstyle[3] = {1, 1, 2};
            std::vector<Ort::Value> ins;
            ins.reserve(3);
            // Input order matches the graph: feat, [style,] cache_in.
            ins.push_back(Ort::Value::CreateTensor<float>(
                mem_info_, const_cast<float*>(feat), f0_feat_dim_, sfeat, 3));
            if (has_perf_style_f0_) {
                perf_style_[0] = perf_px_; perf_style_[1] = perf_py_;
                ins.push_back(Ort::Value::CreateTensor<float>(
                    mem_info_, perf_style_, 2, sstyle, 3));
            }
            ins.push_back(Ort::Value::CreateTensor<float>(
                mem_info_, f0_expr_cache_.data(), f0_expr_cache_.size(), scache, 3));
            const char* in_names_p[] = {"feat", "style", "cache_in"};
            const char* in_names_n[] = {"feat", "cache_in"};
            const char** in_names = has_perf_style_f0_ ? in_names_p : in_names_n;
            const char* out_names[] = {"deviation", "cache_out"};
            auto outs = f0_expr_session_->Run(Ort::RunOptions{nullptr},
                                              in_names, ins.data(), ins.size(),
                                              out_names, 2);
            if (outs.size() < 2) return false;
            dev = outs[0].GetTensorData<float>()[0];
            std::memcpy(f0_expr_cache_.data(), outs[1].GetTensorData<float>(),
                        static_cast<size_t>(f0_expr_hidden_) * sizeof(float));
            return true;
        } catch (const Ort::Exception& e) {
            DDSP_LOG("f0 expression inference error: %s", e.what());
            return false;
        }
#else
        return false;
#endif
    }

    // ------------------------------------------------------------------
    // Inference (audio thread, synchronous)
    // ------------------------------------------------------------------

    bool infer_frame([[maybe_unused]] float pitch_hz,
                     [[maybe_unused]] float loud_db,
                     [[maybe_unused]] DDSPFrame& frame) {
#ifdef AS_ENABLE_DDSP
        if (!ort_session_ || hidden_ <= 0) return false;
        try {
            float pitch_v = pitch_hz;
            float loud_v  = loud_db;
            int64_t s111[3]   = {1, 1, 1};
            int64_t scache[3] = {1, 1, static_cast<int64_t>(hidden_)};
            int64_t sstyle[3] = {1, 1, 2};

            std::vector<Ort::Value> ins;
            ins.reserve(4);
            // Input order matches the graph: pitch, loudness, [style,] cache_in.
            ins.push_back(Ort::Value::CreateTensor<float>(mem_info_, &pitch_v, 1, s111, 3));
            ins.push_back(Ort::Value::CreateTensor<float>(mem_info_, &loud_v, 1, s111, 3));
            if (has_style_) {
                style_[0] = style_px_; style_[1] = style_py_;
                ins.push_back(Ort::Value::CreateTensor<float>(
                    mem_info_, style_, 2, sstyle, 3));
            }
            ins.push_back(Ort::Value::CreateTensor<float>(
                mem_info_, cache_.data(), cache_.size(), scache, 3));

            const char* in_names_s[] = {"pitch", "loudness", "style", "cache_in"};
            const char* in_names_n[] = {"pitch", "loudness", "cache_in"};
            const char** in_names = has_style_ ? in_names_s : in_names_n;
            const char* out_names[] = {"amplitudes", "noise_param", "cache_out"};

            auto outs = ort_session_->Run(Ort::RunOptions{nullptr},
                                          in_names, ins.data(), ins.size(),
                                          out_names, 3);
            if (outs.size() < 3) return false;

            const float* amps  = outs[0].GetTensorData<float>();
            const float* noise = outs[1].GetTensorData<float>();
            const float* cache = outs[2].GetTensorData<float>();
            int na = static_cast<int>(outs[0].GetTensorTypeAndShapeInfo().GetElementCount());
            int nb = static_cast<int>(outs[1].GetTensorTypeAndShapeInfo().GetElementCount());

            frame.n_harm  = std::min(na, MAX_HARMONICS);
            frame.n_bands = std::min(nb, MAX_NOISE_BANDS);
            for (int i = 0; i < frame.n_harm; ++i)  frame.amplitudes[i]  = amps[i];
            for (int i = 0; i < frame.n_bands; ++i) frame.noise_param[i] = noise[i];
            frame.valid = true;

            std::memcpy(cache_.data(), cache,
                        static_cast<size_t>(hidden_) * sizeof(float));
            return true;
        } catch (const Ort::Exception& e) {
            DDSP_LOG("Inference error: %s", e.what());
            return false;
        }
#else
        return false;
#endif
    }

    // ------------------------------------------------------------------
    // Model loading (main thread)
    // ------------------------------------------------------------------

    // Load the configured model once we know the host sample rate. Runs on the
    // control thread (configure/activate), never the audio thread.
    void maybe_load_model() {
        if (!activated_ || !model_dirty_) return;
        load_model(pending_model_dir_);
        model_dirty_ = false;
    }

    void load_model(const std::string& dir) {
        model_loaded_ = false;
        has_expr_ = false;
        want_expr_ = false;
        has_tremolo_ = false;
        tremolo_n_harm_ = 0;
        expr_feat_dim_ = 5;
        has_f0_expr_ = false;
        want_f0_expr_ = false;
        has_style_ = false;
        want_style_ = false;
        has_perf_style_ = false;
        has_perf_style_expr_ = false;
        has_perf_style_f0_ = false;
        if (dir.empty()) { DDSP_LOG("No model directory specified"); return; }

        std::ifstream cfg_file(dir + "/config.json");
        if (!cfg_file.is_open()) {
            DDSP_LOG("Cannot open %s/config.json", dir.c_str());
            return;
        }
        try {
            nlohmann::json cfg; cfg_file >> cfg;
            if (cfg.contains("sample_rate"))   model_sr_   = cfg["sample_rate"].get<int>();
            if (cfg.contains("sampling_rate")) model_sr_   = cfg["sampling_rate"].get<int>();
            if (cfg.contains("block_size"))    block_size_ = cfg["block_size"].get<int>();
            if (cfg.contains("n_harmonic"))    n_harmonic_ = cfg["n_harmonic"].get<int>();
            if (cfg.contains("n_bands"))       n_bands_    = cfg["n_bands"].get<int>();
            if (cfg.contains("hidden_size"))   hidden_     = cfg["hidden_size"].get<int>();
            // Suggested loudness calibration (the converter derives it from the
            // model's loudness stats). A real user edit wins; an untouched param
            // (still at its default) yields to this calibration. resolve_*()
            // applies the precedence so config-key / param arrival order is moot.
            has_cfg_loud_floor_ = cfg.contains("loud_floor_db");
            has_cfg_loud_ceil_  = cfg.contains("loud_ceil_db");
            if (has_cfg_loud_floor_) cfg_loud_floor_db_ = cfg["loud_floor_db"].get<float>();
            if (has_cfg_loud_ceil_)  cfg_loud_ceil_db_  = cfg["loud_ceil_db"].get<float>();
            // Optional learned expression model (notes -> loudness). Tremolo head
            // (n_tremolo_harmonics > 0) mirrors the f0/vibrato design for amplitude:
            // the plugin runs a free LFO at tremolo_rate and feeds its phase
            // harmonics, so feat_dim = 5 + 2*Ht. Models with no tremolo head keep 5.
            if (cfg.contains("expression") && cfg["expression"].is_object()) {
                auto& ec = cfg["expression"];
                expr_hidden_  = ec.value("hidden_size", 128);
                expr_t_clip_  = ec.value("t_clip", 2.0f);
                tremolo_n_harm_ = std::min(ec.value("n_tremolo_harmonics", 0),
                                           EXPR_MAX_HARM);
                tremolo_rate_ = ec.value("tremolo_rate", 4.0f);
                expr_feat_dim_ = 5 + 2 * tremolo_n_harm_;
                has_tremolo_   = tremolo_n_harm_ > 0;
                want_expr_ = true;
            }
            // Optional learned pitch-expression model (notes -> f0 deviation).
            // Phase-conditioned (variant #3): the plugin drives the LFO; the net
            // rides the supplied phase harmonics, so it cannot drift. Old AR models
            // (feat_dim==6, no vibrato_rate) are deprecated -> refuse them.
            if (cfg.contains("f0_expression") && cfg["f0_expression"].is_object()) {
                auto& fc = cfg["f0_expression"];
                if (!fc.contains("vibrato_rate")) {
                    DDSP_LOG("f0_expression is a deprecated autoregressive model "
                             "(no vibrato_rate) — skipping; pitch will be flat. "
                             "Re-export with the phase-conditioned trainer.");
                } else {
                    f0_expr_hidden_  = fc.value("hidden_size", 128);
                    f0_max_dev_      = fc.value("max_dev", 2.0f);
                    f0_vibrato_rate_ = fc.value("vibrato_rate", 5.5f);
                    f0_n_harm_       = std::min(fc.value("n_phase_harmonics", 2),
                                                F0_MAX_HARM);
                    f0_feat_dim_     = 5 + 2 * f0_n_harm_;
                    want_f0_expr_ = true;
                }
            }
            // Optional 2D timbre style pad on the decoder. Read the extent for
            // the [-1,1] -> pad mapping; presence of the graph 'style' input is
            // confirmed after load. (The performance pad was dropped; perf-latent
            // models are still fed the mean (0,0) — see process_block.)
            want_style_ = false;
            if (cfg.contains("latent") && cfg["latent"].is_object() &&
                cfg["latent"].contains("extent")) {
                auto& ext = cfg["latent"]["extent"];
                style_x_lo_ = ext["x"][0].get<float>(); style_x_hi_ = ext["x"][1].get<float>();
                style_y_lo_ = ext["y"][0].get<float>(); style_y_hi_ = ext["y"][1].get<float>();
                want_style_ = true;
            }
            n_harmonic_ = std::min(n_harmonic_, MAX_HARMONICS);
            n_bands_    = std::min(n_bands_, MAX_NOISE_BANDS);
            block_size_ = std::min(block_size_, MAX_BLOCK);
            resolve_loudness_calibration();
            DDSP_LOG("Config: model_sr=%d block=%d n_harm=%d n_bands=%d hidden=%d expr=%d "
                     "loud[floor=%.2f ceil=%.2f]",
                     model_sr_, block_size_, n_harmonic_, n_bands_, hidden_, (int)want_expr_,
                     loud_floor_db_, loud_ceil_db_);
        } catch (const std::exception& e) {
            DDSP_LOG("Error parsing config.json: %s", e.what());
            return;
        }
        recompute_frame_advance();

#ifdef AS_ENABLE_DDSP
        try {
            if (!ort_env_)
                ort_env_ = std::make_unique<Ort::Env>(ORT_LOGGING_LEVEL_WARNING, "ddsp");
            Ort::SessionOptions opts;
            opts.SetIntraOpNumThreads(1);
            opts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
            ort_session_ = std::make_unique<Ort::Session>(
                *ort_env_, (dir + "/decoder.onnx").c_str(), opts);
            cache_.assign(hidden_, 0.0f);
            model_loaded_ = true;
            has_style_ = session_has_input(*ort_session_, "style");
            if (has_style_ != want_style_)
                DDSP_LOG("decoder 'style' input %s graph but %s config — using graph",
                         has_style_ ? "present in" : "absent from",
                         want_style_ ? "declared in" : "absent from");
            DDSP_LOG("Loaded decoder: %s/decoder.onnx (timbre pad=%d)",
                     dir.c_str(), (int)has_style_);
        } catch (const Ort::Exception& e) {
            DDSP_LOG("ONNX error loading %s/decoder.onnx: %s", dir.c_str(), e.what());
        }

        // Optional expression model (loudness renderer). Absent -> fall back to
        // the velocity*expression -> dB mapping.
        expr_session_.reset();
        if (want_expr_ && model_loaded_) {
            try {
                Ort::SessionOptions eopts;
                eopts.SetIntraOpNumThreads(1);
                eopts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
                expr_session_ = std::make_unique<Ort::Session>(
                    *ort_env_, (dir + "/expression.onnx").c_str(), eopts);
                expr_cache_.assign(expr_hidden_, 0.0f);
                has_expr_ = true;
                has_perf_style_expr_ = session_has_input(*expr_session_, "style");
                DDSP_LOG("Loaded expression model (hidden=%d, perf pad=%d)",
                         expr_hidden_, (int)has_perf_style_expr_);
            } catch (const Ort::Exception& e) {
                DDSP_LOG("expression.onnx load failed (%s) — using fallback loudness", e.what());
            }
        }

        // Optional pitch-expression model. Absent -> flat (nominal) pitch.
        f0_expr_session_.reset();
        if (want_f0_expr_ && model_loaded_) {
            try {
                Ort::SessionOptions eopts;
                eopts.SetIntraOpNumThreads(1);
                eopts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
                f0_expr_session_ = std::make_unique<Ort::Session>(
                    *ort_env_, (dir + "/f0_expression.onnx").c_str(), eopts);
                f0_expr_cache_.assign(f0_expr_hidden_, 0.0f);
                has_f0_expr_ = true;
                has_perf_style_f0_ = session_has_input(*f0_expr_session_, "style");
                DDSP_LOG("Loaded f0 expression model (hidden=%d, max_dev=%.1f, "
                         "vibrato_rate=%.2f, H=%d, perf pad=%d)",
                         f0_expr_hidden_, f0_max_dev_, f0_vibrato_rate_,
                         f0_n_harm_, (int)has_perf_style_f0_);
            } catch (const Ort::Exception& e) {
                DDSP_LOG("f0_expression.onnx load failed (%s) — using flat pitch", e.what());
            }
        }

        // Performance pad is active if either expression net consumes it.
        has_perf_style_ = has_perf_style_expr_ || has_perf_style_f0_;
#else
        cache_.assign(hidden_, 0.0f);
        DDSP_LOG("ONNX Runtime not available — using default frames");
#endif
    }

    // ------------------------------------------------------------------
    // Member data
    // ------------------------------------------------------------------

    float host_sr_ = 44100.0f;
    bool  activated_ = false;

    // Model config (from config.json)
    std::string pending_model_dir_;
    bool model_dirty_  = false;
    bool model_loaded_ = false;
    int  n_harmonic_ = 64;
    int  n_bands_    = 65;
    int  model_sr_   = 16000;
    int  block_size_ = 160;
    int  hidden_     = 512;
    float frame_advance_per_sample_ = 100.0f / 44100.0f;
    float frame_sec_ = 0.01f;                 // seconds per model frame

    // Note priority: false = retrigger (re-articulate every note), true = legato
    // (glide into a new note while one is held, keeping model state). Config param.
    bool  legato_ = false;

    // Optional learned expression model (notes -> loudness)
    bool  use_expression_ = true;             // user toggle (config param)
    bool  want_expr_   = false;               // config.json declares one
    bool  has_expr_    = false;               // and it loaded
    int   expr_hidden_ = 128;
    float expr_t_clip_ = 2.0f;                // seconds clip for onset/release feats
    bool  expr_gate_   = false;               // note held (1) vs released (0)
    float expr_t_onset_sec_ = 0.0f;
    float expr_t_rel_sec_   = 0.0f;
    float last_loud_db_     = 0.0f;           // loudness at note-off, for release decay
    // Optional tremolo head on the loudness model (amplitude analogue of f0
    // vibrato): plugin drives a free LFO at tremolo_rate, feeds H phase harmonics.
    bool  has_tremolo_    = false;            // expression model consumes tremolo phase
    int   tremolo_n_harm_ = 0;                // # phase harmonics fed in (Ht)
    int   expr_feat_dim_  = 5;                // 5 + 2*Ht
    float tremolo_rate_   = 4.0f;             // LFO rate (Hz), calibrated from data
    float tremolo_lfo_phase_ = 0.0f;          // free LFO phase, in turns [0,1)
    float tremolo_lfo_inc_   = 0.0f;          // per-frame phase increment (turns)

    // Optional learned pitch-expression model (notes -> f0 deviation).
    // Phase-conditioned: the plugin drives a free LFO and feeds its phase
    // harmonics; the net predicts a bounded depth/scoop that cannot drift.
    bool  want_f0_expr_   = false;
    bool  has_f0_expr_    = false;
    int   f0_expr_hidden_ = 128;
    float f0_max_dev_     = 2.0f;             // clamp on |deviation| (semitones)
    float f0_vibrato_rate_ = 5.5f;           // LFO rate (Hz), calibrated from data
    int   f0_n_harm_      = 2;               // # phase harmonics fed in
    int   f0_feat_dim_    = 9;               // 5 + 2*H
    float f0_lfo_phase_   = 0.0f;            // free LFO phase, in turns [0,1)
    float f0_lfo_inc_     = 0.0f;            // per-frame phase increment (turns)
    float vibrato_amount_ = 1.0f;            // host vibrato-depth knob (control)

    // Optional 2D timbre style pad -> decoder.onnx 'style'. Node params are
    // normalised [-1,1] (0 == mean embedding), mapped onto the per-axis extent
    // below. Held constant within a note (timbre selector).
    bool  want_style_  = false;              // config declares a timbre latent
    bool  has_style_   = false;              // decoder graph has a 'style' input
    float style_x_lo_ = -1.0f, style_x_hi_ = 1.0f;
    float style_y_lo_ = -1.0f, style_y_hi_ = 1.0f;
    float style_px_ = 0.0f, style_py_ = 0.0f;   // current pad coord (model units)
    float style_[2] = {0.0f, 0.0f};

    // The performance style pad was dropped (negligible effect for our
    // instruments). We still detect a 'style' input on the expression / f0 nets
    // and feed it the mean (0,0) so perf-latent models keep loading/running.
    bool  has_perf_style_       = false;     // either expression net has 'style'
    bool  has_perf_style_expr_  = false;     // loudness net has 'style'
    bool  has_perf_style_f0_    = false;     // f0 net has 'style'
    float perf_px_ = 0.0f, perf_py_ = 0.0f;  // pinned at the mean (pad dropped)
    float perf_style_[2] = {0.0f, 0.0f};

    // Loudness mapping (raw model units). Effective values resolved by
    // resolve_loudness_calibration() from: user edit > config.json > default.
    float loud_floor_db_ = DEFAULT_LOUD_FLOOR_DB;   // effective floor
    float loud_ceil_db_  = DEFAULT_LOUD_CEIL_DB;    // effective ceiling
    float cur_level_     = 0.0f;   // target velocity*expression level [0,1]
    bool  loud_floor_user_ = false;   // user edited loud_floor_db (≠ default)
    bool  loud_ceil_user_  = false;
    float user_loud_floor_db_ = DEFAULT_LOUD_FLOOR_DB;  // last user-edit value
    float user_loud_ceil_db_  = DEFAULT_LOUD_CEIL_DB;
    bool  has_cfg_loud_floor_ = false;   // config.json supplied a calibration
    bool  has_cfg_loud_ceil_  = false;
    float cfg_loud_floor_db_  = DEFAULT_LOUD_FLOOR_DB;  // config.json calibration
    float cfg_loud_ceil_db_   = DEFAULT_LOUD_CEIL_DB;

    // Output envelope params (from control ports each block)
    float attack_time_  = 0.05f;
    float release_time_ = 0.1f;

    // Monophonic voice
    struct Voice {
        bool  active = false;
        bool  releasing = false;
        int   channel = 0, pitch = 0, velocity = 0;
        float f0_hz = 0.0f;
        float tune_semitones = 0.0f;
        float harmonic_phase[MAX_HARMONICS] = {};
        DDSPFrame last_frame, cur_frame;
        float frame_phase = 0.0f;
        ADSREnvelope env;
        float noise_block[MAX_BLOCK] = {};
        bool  noise_valid = false;
        // Per-note (onset-latched) multipliers, neutral 1.0. See d.note_attrs.
        float attr_vibrato = 1.0f;
        float attr_breath  = 1.0f;
        float attr_release = 1.0f;
    } v_;

    std::vector<HeldNote> held_;
    XorShift32 rng_;
    PendingAttrStore pending_attrs_;   // note-attrs awaiting their note_on

    // GRU caches (threaded frame-to-frame)
    std::vector<float> cache_;          // decoder
    std::vector<float> expr_cache_;     // loudness expression model
    std::vector<float> f0_expr_cache_;  // pitch expression model

#ifdef AS_ENABLE_DDSP
    std::unique_ptr<Ort::Env>     ort_env_;
    std::unique_ptr<Ort::Session> ort_session_;
    std::unique_ptr<Ort::Session> expr_session_;
    std::unique_ptr<Ort::Session> f0_expr_session_;
    Ort::MemoryInfo mem_info_ = Ort::MemoryInfo::CreateCpu(
        OrtArenaAllocator, OrtMemTypeDefault);
#endif
};

// ==========================================================================
// Registration
// ==========================================================================

REGISTER_PLUGIN(DDSPPlugin);
REGISTER_PLUGIN_DYNAMIC(DDSPPlugin);
