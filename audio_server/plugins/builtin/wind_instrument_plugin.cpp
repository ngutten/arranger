// wind_instrument_plugin.cpp
// ==========================================================================
// Wind Instrument — physical modeling synthesizer (digital waveguide)
// ==========================================================================
//
// Polyphonic, MIDI-driven physical model of single-reed (clarinet/sax),
// air-jet (flute), and lip-reed (brass) wind instruments, built on the
// McIntyre-Schumacher-Woodhouse single-delay-loop formulation used by the
// STK ClariNet / Flute / Brass / Saxofony classes.
//
// Each voice is one acoustic feedback loop:
//
//      breath(t)
//          |              +----------------------------------+
//          v              |          BORE DELAY (D samples)  |
//      [ EXCITER ]--in---->| z^-D  +  fractional (allpass)    |---out--+
//          ^              +----------------------------------+         |
//          |                                                           |
//          +----------[ loss filter ] <--- [ reflection ] <------------+
//
//   - Single reed: EXCITER = nonlinear reed table  (reflection = -g, inverting)
//   - Flute:       EXCITER = jet delay + cubic (x^3 - x) (reflection lowpass)
//   - Brass:       EXCITER = 2nd-order lip resonator + area^2 valve
//
// References:
//   McIntyre, Schumacher & Woodhouse, "On the oscillations of musical
//     instruments", JASA 74(5), 1983 — unified nonlinear-exciter / bore loop.
//   J.O. Smith III, "Physical Audio Signal Processing", CCRMA — waveguide
//     bore, loop/loss filter design, fractional (allpass/Lagrange) delay.
//   Smith, "Efficient Simulation of the Reed-Bore ... Digital Waveguide", ICMC 1986.
//   G. Scavone, "An Acoustic Analysis of Single-Reed Woodwind Instruments",
//     PhD thesis, Stanford 1997 — reed reflection table, conical bore.
//   Verge, Fabre, Hirschberg & Wijnands, "Jet oscillations and jet drive
//     in recorder-like instruments", JASA 97(2), 1995 — flute jet drive.
//   P. Cook, "A Meta-Wind-Instrument Physical Model", ICMC 1991.
//   The Synthesis ToolKit in C++ (STK), Cook & Scavone — Clarinet, Flute,
//     Brass, Saxofony reference implementations.

#include "plugin_api.h"
#include "synth_common.h"

#include <algorithm>
#include <cmath>
#include <cstring>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Sizing
// ---------------------------------------------------------------------------

// Bore delay max: supports f0 down to ~20 Hz at 96 kHz.
static constexpr int MAX_BORE  = 8192;
static constexpr int BORE_MASK = MAX_BORE - 1;

// Jet (convective) delay is short — a couple of ms is plenty.
static constexpr int MAX_JET   = 512;
static constexpr int JET_MASK  = MAX_JET - 1;

// Instrument model selector.
enum WindModel { MODEL_CLARINET = 0, MODEL_SAX = 1, MODEL_FLUTE = 2, MODEL_BRASS = 3 };

// ---------------------------------------------------------------------------
// Per-voice waveguide / exciter state
// ---------------------------------------------------------------------------

struct WindExt {
    // --- Bore delay line (carries the round-trip pressure wave) ---
    float bore[MAX_BORE] = {};
    int   bore_write = 0;
    float bore_last  = 0.0f;     // last sample read out of the bore

    // First-order allpass fractional-delay state (used for brass slide tuning
    // and for smooth pitch glides on all models).
    float ap_z1   = 0.0f;
    float ap_prev = 0.0f;
    float cur_delay = 100.0f;    // current bore length in samples (slewed)

    // --- Loop loss / reflection filter (1-pole / 1-zero) ---
    float loss_z1 = 0.0f;

    // --- Jet delay (flute only) ---
    float jet[MAX_JET] = {};
    int   jet_write = 0;
    float jet_delay = 30.0f;

    // --- Lip resonator (brass only): 2nd-order bandpass biquad ---
    float lip_x1 = 0.0f, lip_x2 = 0.0f;
    float lip_y1 = 0.0f, lip_y2 = 0.0f;

    // --- DC blockers (one for the exciter output, one in the bore path) ---
    float dc_x1 = 0.0f, dc_y1 = 0.0f;

    // --- Breath / blow envelope (separate from the synth ADSR amplitude) ---
    float breath = 0.0f;         // current normalized breath pressure (0..1)
    float breath_target = 0.0f;  // attack/release target

    // --- Per-voice turbulence noise RNG ---
    uint32_t rng = 22222u;

    // --- Vibrato/tremolo phase ---
    float vib_phase = 0.0f;

    // Per-note register/overblow override: -1 use block param, else 0/1.
    int   register_mode = -1;
    // Per-note multiplier on the breath-attack time (from the "attack" attr).
    float attack_mul = 1.0f;

    float f0 = 440.0f;           // target playing frequency
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static inline float xorshift_float(uint32_t& s) {
    s ^= s << 13; s ^= s >> 17; s ^= s << 5;
    return static_cast<float>(static_cast<int32_t>(s)) * (1.0f / 2147483648.0f);
}

// First-order DC blocker: y[n] = x[n] - x[n-1] + R*y[n-1]
static inline float dc_block(float x, float& x1, float& y1, float R = 0.995f) {
    float y = x - x1 + R * y1;
    x1 = x; y1 = y;
    return y;
}

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class WindInstrumentPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.wind_instrument";
        d.display_name = "Wind Instrument";
        d.category     = "Synth";
        d.doc          = "Physical model (digital waveguide) of wind instruments: "
                         "single-reed (clarinet/sax), air-jet (flute), and lip-reed "
                         "(brass), using the McIntyre-Schumacher-Woodhouse nonlinear "
                         "exciter / bore feedback loop (STK-style).";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output.",
              PluginPortType::AudioStereo, PortRole::Output },

            // --- Primary controls (visible on canvas) ---
            { "model", "Instrument", "Excitation / bore model.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 3.0f, 1.0f,
              {"Clarinet", "Saxophone", "Flute", "Brass"} },

            { "gain", "Gain", "Output level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 2.0f },

            { "breath", "Breath", "Steady blowing pressure (drives the exciter).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.9f, 0.0f, 1.5f },

            { "embouchure", "Embouchure", "Reed bite / lip tension / jet bias "
              "(0=loose & dark, 1=tight & bright).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },

            { "breath_attack", "Breath Attack", "Time (s) for breath to rise on note-on.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.02f, 0.001f, 1.0f },

            { "breath_release", "Breath Release", "Time (s) for breath to fall on note-off.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.06f, 0.001f, 1.0f },

            { "brightness", "Brightness", "Bore loss filter (0=dark/mellow, 1=bright).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },

            { "noise", "Breath Noise", "Turbulence noise injected into the breath.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.06f, 0.0f, 0.5f },

            { "vibrato_rate", "Vibrato Rate", "Vibrato/tremolo frequency (Hz).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 5.5f, 0.1f, 12.0f },

            { "vibrato_depth", "Vibrato Depth", "Breath-pressure vibrato depth.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.04f, 0.0f, 0.5f },

            { "register", "Overblow", "Force the upper register (octave / 12th).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Toggle, 0.0f, 0.0f, 1.0f, 1.0f },

            // --- Hidden / advanced ---
            { "reed_stiffness", "Reed Stiffness", "Reed-table slope magnitude.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.55f, 0.1f, 1.0f, 0.0f, {}, "", false },
            { "jet_ratio", "Jet Ratio", "Flute jet delay as a fraction of bore delay.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.1f, 1.0f, 0.0f, {}, "", false },
            { "lip_q", "Lip Q", "Brass lip resonator quality factor.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 4.0f, 1.0f, 12.0f, 0.0f, {}, "", false },
            { "glide", "Glide", "Pitch glide slew (0=instant, 1=slow). Smooth slides.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.05f, 0.0f, 1.0f, 0.0f, {}, "", false },
        };

        d.presets = {
            { "Clarinet",   {{"model",0},{"breath",0.9f},{"embouchure",0.55f},
                             {"brightness",0.45f},{"reed_stiffness",0.55f}} },
            { "Alto Sax",   {{"model",1},{"breath",1.0f},{"embouchure",0.6f},
                             {"brightness",0.6f},{"reed_stiffness",0.5f}} },
            { "Flute",      {{"model",2},{"breath",0.85f},{"embouchure",0.5f},
                             {"brightness",0.7f},{"noise",0.12f},{"jet_ratio",0.5f}} },
            { "Trumpet",    {{"model",3},{"breath",1.05f},{"embouchure",0.6f},
                             {"brightness",0.65f},{"lip_q",5.0f}} },
        };

        d.note_attrs = {
            standard_attack_attr(),
            NoteAttrDecl{ "register", "Overblow",
                "Per-note overblow; forces the upper register for this note.",
                ControlHint::Categorical, 0.0f, 0.0f, 1.0f, {"Normal", "Overblow"} },
        };
        d.config_params = { standard_attr_remap_param() };
        return d;
    }

    void activate(float sample_rate, int max_block_size) override {
        vm_.init(sample_rate, max_block_size);
    }

    void deactivate() override {
        for (auto& v : vm_.voices) v = {};
    }

    // -----------------------------------------------------------------------
    // Note / control handling
    // -----------------------------------------------------------------------

    void note_on(int channel, int pitch, int velocity) override {
        if (velocity == 0) { note_off(channel, pitch); return; }

        // We use the synth ADSR only as a gentle amplitude gate; the physical
        // breath envelope (per voice) does the real dynamics work, so we give
        // the ADSR a fast attack and a long sustain.
        auto* v = vm_.trigger(channel, pitch, velocity,
                              /*attack*/ 0.001f, /*decay*/ 0.01f,
                              /*sustain*/ 1.0f,  /*release*/ 0.05f);
        if (!v) return;

        WindExt& e = v->ext;

        // Clear loop state.
        std::memset(e.bore, 0, sizeof(e.bore));
        std::memset(e.jet,  0, sizeof(e.jet));
        e.bore_write = e.jet_write = 0;
        e.bore_last = e.ap_z1 = e.ap_prev = e.loss_z1 = 0.0f;
        e.lip_x1 = e.lip_x2 = e.lip_y1 = e.lip_y2 = 0.0f;
        e.dc_x1  = e.dc_y1 = 0.0f;
        e.vib_phase = 0.0f;

        // Playing frequency and bore length.
        e.f0 = pitch_to_freq(static_cast<float>(pitch));
        e.cur_delay = vm_.sample_rate / e.f0;

        // Velocity → breath target (a firmer attack for harder notes).
        e.breath = 0.0f;
        e.breath_target = std::clamp(0.4f + 0.6f * v->velocity, 0.0f, 1.5f);

        e.rng = 22222u ^ (static_cast<uint32_t>(pitch) * 2654435761u)
                       ^ (static_cast<uint32_t>(velocity) * 40503u);

        if (const float* r = v->attrs.get("register"))
            e.register_mode = static_cast<int>(*r);
        else
            e.register_mode = -1;

        // Per-note "attack" attr scales this voice's BREATH attack time — the
        // perceptually meaningful onset on a wind instrument (the ADSR here is
        // only a 1 ms anti-click gate).  Neutral 1.0; >1 = slower swell.
        if (const float* a = v->attrs.get("attack"))
            e.attack_mul = std::max(0.05f, *a);
        else
            e.attack_mul = 1.0f;
    }

    void note_off(int channel, int pitch) override {
        // Mark for breath release; the voice frees itself once breath ~ 0.
        // Match on the original MIDI note (v.pitch), not the (possibly
        // note_tune-detuned) pitch_semitones, so microtonal bends don't
        // prevent the release — consistent with vm_.release_note below.
        for (auto& v : vm_.voices) {
            if (v.active && v.channel == channel && v.pitch == pitch)
                v.ext.breath_target = 0.0f;
        }
        vm_.release_note(channel, pitch);
    }

    void note_attr(int channel, int note, const std::string& id, float value) override {
        vm_.set_pending_attr(channel, note, id.c_str(), value);
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "attr_remap") vm_.configure_attr_remap(value);
    }

    void all_notes_off(int channel) override { vm_.all_notes_off(channel); }
    void note_tune(int channel, int note, float semitones) override { vm_.tune(channel, note, semitones); }

    // -----------------------------------------------------------------------
    // Audio processing
    // -----------------------------------------------------------------------

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* out = buffers.audio.get("audio_out");
        if (!out) return;

        float* L = out->left;
        float* R = out->right ? out->right : out->left;
        const int   N  = ctx.block_size;
        const float sr = vm_.sample_rate;

        std::memset(L, 0, N * sizeof(float));
        if (out->right) std::memset(R, 0, N * sizeof(float));

        auto ctrl = [&](const char* id, float fb) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fb;
        };

        const int   model        = std::clamp((int)ctrl("model", 0.0f), 0, 3);
        const float gain         = ctrl("gain", 0.5f);
        const float breath_max   = ctrl("breath", 0.9f);
        const float embouchure   = std::clamp(ctrl("embouchure", 0.5f), 0.0f, 1.0f);
        const float br_attack    = std::max(0.001f, ctrl("breath_attack", 0.02f));
        const float br_release   = std::max(0.001f, ctrl("breath_release", 0.06f));
        const float brightness   = std::clamp(ctrl("brightness", 0.5f), 0.0f, 1.0f);
        const float noise_amt    = ctrl("noise", 0.06f);
        const float vib_rate     = ctrl("vibrato_rate", 5.5f);
        const float vib_depth    = ctrl("vibrato_depth", 0.04f);
        const int   register_blk = (int)ctrl("register", 0.0f);
        const float reed_stiff   = std::clamp(ctrl("reed_stiffness", 0.55f), 0.1f, 1.0f);
        const float jet_ratio    = std::clamp(ctrl("jet_ratio", 0.5f), 0.1f, 1.0f);
        const float lip_q        = std::clamp(ctrl("lip_q", 4.0f), 1.0f, 12.0f);
        const float glide        = std::clamp(ctrl("glide", 0.05f), 0.0f, 1.0f);

        vm_.begin_block(N);

        // Per-sample breath-release coefficient (one-pole).  The breath ATTACK
        // coefficient is computed per-voice below because the per-note "attack"
        // attr scales each voice's breath-attack time individually.
        const float rel_c = 1.0f - std::exp(-1.0f / (br_release * sr));

        // Vibrato phase increment.
        const float vib_inc = 2.0f * (float)M_PI * vib_rate / sr;

        // --- Reed table (clarinet/sax): R = clamp(offset + slope*dp, -1, 1) ---
        // Coefficients follow STK's ReedTable as used by Clarinet (offset 0.7,
        // slope ~-0.3).  The slope MUST stay gentle: a steep slope saturates
        // R to 1.0 in the operating region, which makes bore_in = breath + dp*1
        // ≈ 0 and the reed never injects energy (no self-oscillation).
        const float reed_offset = 0.6f + 0.2f * embouchure;            // ~0.7 nominal
        // Keep the reed slope MODERATE so it oscillates robustly across the
        // breath/velocity range (a steep slope chokes at high breath).  The
        // reedy/hollow clarinet character instead comes from a lighter bore
        // loop low-pass (below), which preserves the odd harmonics the reed
        // generates rather than damping them away.
        const float reed_slope  = -(0.24f + 0.22f * reed_stiff);       // ~-0.36 nominal

        // --- Bore reflection sign / loss: single reeds invert (cylindrical,
        // closed-open), flute & brass do not. Brightness sets the 1-pole loss. ---
        const bool  is_reed     = (model == MODEL_CLARINET || model == MODEL_SAX);
        // Clarinet (cylindrical, closed-open) inverts at the open end → ODD
        // harmonics only + 12th overblow.  Saxophone (conical) behaves like an
        // open bore → ALL harmonics + octave overblow, so it does NOT invert —
        // this is what makes the sax brighter and reedier than the clarinet.
        const bool  invert_refl = (model == MODEL_CLARINET);
        // Loop gain just below 1; brighter = less high-frequency loss.
        float loss_pole   = 0.05f + 0.55f * (1.0f - brightness); // 1-pole LP coeff
        float loop_gain   = 0.985f + 0.012f * brightness;        // < 1 for stability
        // Brass is a lip-reed: the lip resonator (not a lossy bore) selects the
        // pitch, and the exciter's 0.85 bore-reflection is the dominant loss
        // (per STK Brass).  Stacking the cylindrical-bore loss filter on top
        // over-damps the loop so it can't bootstrap — so keep the brass bore
        // nearly lossless and let the lip filter + 0.85 reflection shape it.
        if (model == MODEL_BRASS) {
            loss_pole = 0.02f + 0.08f * (1.0f - brightness);
            loop_gain = 0.999f;
        }
        // Flute: a real flute is the PUREST of the family (near-sinusoidal).
        // The air-jet cubic injects a rich harmonic series, so damp the bore
        // loop harder (stronger 1-pole lowpass) to roll the upper harmonics
        // off and keep the flute darker than the reeds, not brighter.
        if (model == MODEL_FLUTE)
            loss_pole = std::max(loss_pole, 0.68f + 0.20f * (1.0f - brightness));
        // Clarinet: very light loop low-pass so its sparse ODD harmonics
        // survive — this makes it read as reedy/hollow rather than pure/flutey.
        // (The sax keeps the default, heavier loop loss: its conical bore makes
        // ALL harmonics, so a light loss there would be a harsh buzzy sawtooth;
        // the moderate loss rolls off the very top into a natural reedy tone
        // that's still brighter than the flute.)
        if (model == MODEL_CLARINET)
            loss_pole = std::min(loss_pole, 0.04f + 0.06f * (1.0f - brightness));

        // --- Flute jet nonlinearity gain ---
        // Kept modest: driving the cubic jet hard pushes the oscillator through
        // a period-doubling bifurcation (a strong f0/2 subharmonic that makes
        // the flute sound an octave low from ~C4 up).
        const float jet_drive = 0.3f + 0.3f * embouchure;

        // --- Brass lip resonator: tuned slightly above f0, Q from control. ---
        // (coefficients are recomputed per-voice below since f0 differs.)

        for (int vi = 0; vi < SYNTH_MAX_VOICES; ++vi) {
            auto& v = vm_.voices[vi];
            if (!v.active) continue;
            WindExt& e = v.ext;

            const int reg = (e.register_mode >= 0) ? e.register_mode : register_blk;

            // Precompute brass lip biquad (bandpass) for this voice's pitch.
            // Lip frequency tracks the note; overblow pushes it up an octave.
            float lip_f = e.f0 * (reg ? 2.0f : 1.0f);
            // Embouchure nudges the lip resonance for "lipping" / bend feel.
            lip_f *= (0.95f + 0.1f * embouchure);
            // Brass: the lip resonator (with its DC-blocking zero) actually
            // SETS the played pitch, and the zero pushes its effective peak up
            // ~14%.  Pre-detune the lip so the note plays in tune.
            if (model == MODEL_BRASS) lip_f *= 0.877f;
            float lw0  = 2.0f * (float)M_PI * std::min(lip_f, 0.45f * sr) / sr;
            // Lip = 2-pole resonator (STK BiQuad::setResonance, NON-normalized).
            // The large resonant gain of a high pole radius is what bootstraps
            // the lip oscillation; a normalized (unity-peak) bandpass cannot.
            // lip_q nudges the radius toward 1 (STK uses 0.997).
            //
            // KNOWN LIMITATION: the brass lip-reed model does not yet reach a
            // clean pitched limit cycle.  With a high resonant gain the squared
            // valve term (area = y*y) saturates fully open and the tone is a
            // weak, off-mode rumble; with low gain it never bootstraps.  A
            // correct fix needs the relaxation-oscillator coupling between the
            // lip resonance and the bore standing wave to be balanced (cf. STK
            // Brass + Cook's meta-wind model).  Clarinet/Sax/Flute are working.
            float lrad = std::clamp(1.0f - 0.012f / lip_q, 0.90f, 0.997f);
            float lc1  = 2.0f * lrad * std::cos(lw0);   // y[n-1] coefficient
            float lc2  = lrad * lrad;                   // y[n-2] coefficient

            // Per-voice breath-attack coefficient (scaled by the "attack" attr).
            const float v_atk_c = 1.0f - std::exp(
                -1.0f / (std::max(0.001f, br_attack * e.attack_mul) * sr));

            // Per-voice flute jet drive, rolled off in the high register where
            // the short bore drives the cubic into period-doubling.  The bore
            // round-trip is `cur_delay` samples; below ~150 samples (~C5) the
            // drive is progressively reduced so the oscillator stays period-1.
            float jet_drive_v = jet_drive *
                std::clamp(e.cur_delay / 150.0f, 0.72f, 1.0f);

            for (int i = 0; i < N; ++i) {
                float amp = v.env.next();
                if (v.env.is_off()) { v.active = false; break; }

                // --- Breath envelope (physical blowing pressure) ---
                e.breath += (e.breath_target - e.breath) *
                            ((e.breath_target > e.breath) ? v_atk_c : rel_c);

                // Free the voice once it has been released and gone quiet.
                if (e.breath_target <= 0.0f && e.breath < 1e-4f &&
                    std::fabs(e.bore_last) < 1e-4f) {
                    v.active = false; break;
                }

                // Vibrato + turbulence modulate the breath.
                e.vib_phase += vib_inc;
                if (e.vib_phase > 2.0f * (float)M_PI) e.vib_phase -= 2.0f * (float)M_PI;
                float vib   = vib_depth * std::sin(e.vib_phase);
                float turb  = noise_amt * xorshift_float(e.rng);
                float breath = e.breath * breath_max * (1.0f + vib) + turb * e.breath;

                // --- Bore tuning: glide the delay length toward target ---
                float p  = VoiceManager<WindExt>::interpolated_pitch(v, i, N);
                float f0 = pitch_to_freq(p);
                if (reg && (model == MODEL_FLUTE || model == MODEL_BRASS))
                    f0 *= 2.0f;                         // flute/brass overblow: octave
                else if (reg && (model == MODEL_CLARINET))
                    f0 *= 3.0f;                          // clarinet overblows a 12th
                else if (reg && (model == MODEL_SAX))
                    f0 *= 2.0f;                          // conical sax: octave
                e.f0 = f0;

                // Cylindrical reed bores (clarinet/sax) are closed-open: the
                // inverting reed reflection makes a HALF-period delay sound at
                // f0 (quarter-wave resonance), matching STK's sr/(2f).  Open
                // bores (flute) and the lip-driven brass use the full period.
                // The -1 subtracts the loop-filter + allpass group delay.
                float period = sr / f0;
                float target_delay;
                if (model == MODEL_BRASS)
                    // Bore aligned with the lip resonance; the 1.12 factor trims
                    // a uniform ~12% sharpness so the note plays in tune.
                    target_delay = 1.12f * period - 1.0f;
                else if (invert_refl)
                    // Cylindrical reed bore (closed-open): quarter-wave at f0.
                    target_delay = 0.25f * period - 1.0f;
                else
                    // Open bore (flute): half-wave at f0.
                    target_delay = period - 1.0f;
                target_delay = std::clamp(target_delay, 4.0f, (float)(MAX_BORE - 4));

                // Slew toward target (glide=0 → fast; glide=1 → slow).
                float slew = 0.5f * (1.0f - glide) + 0.0008f;
                e.cur_delay += (target_delay - e.cur_delay) * slew;

                // --- Read returning wave from the bore (fractional delay) ---
                int   id  = (int)e.cur_delay;
                float fr  = e.cur_delay - (float)id;
                float a   = (1.0f - fr) / (1.0f + fr);   // 1st-order allpass coeff
                int   rp  = (e.bore_write - id - 1) & BORE_MASK;
                float raw = e.bore[rp];
                float bore_out = a * raw + e.ap_prev - a * e.ap_z1;
                e.ap_z1 = bore_out; e.ap_prev = raw;

                // --- Loop loss filter (1-pole lowpass), then reflection ---
                float lp = (1.0f - loss_pole) * bore_out + loss_pole * e.loss_z1;
                e.loss_z1 = lp;
                float refl = loop_gain * lp;
                e.bore_last = refl;

                float bore_in = 0.0f;

                // ============================================================
                //  EXCITER
                // ============================================================
                if (is_reed) {
                    // ---- Single-reed nonlinear scattering (MSW / Scavone) ----
                    // Clarinet inverts the returning wave (cylindrical, odd
                    // harmonics); sax does not (conical → all harmonics).
                    float r  = invert_refl ? -refl : refl;
                    float dp = r - breath;                       // diff. pressure
                    // Reed reflection coefficient (clamped affine table).
                    float Rt = reed_offset + reed_slope * dp;
                    Rt = std::clamp(Rt, -1.0f, 1.0f);
                    bore_in = breath + dp * Rt;

                } else if (model == MODEL_FLUTE) {
                    // ---- Air-jet drive (Verge/Fabre, STK Flute) ----
                    // Jet delay tracks ~ fraction of the bore length, modulated
                    // by breath (faster breath -> shorter jet delay).
                    float jr = jet_ratio * e.cur_delay /
                               (0.5f + 0.5f * std::max(0.05f, breath));
                    e.jet_delay = std::clamp(jr, 2.0f, (float)(MAX_JET - 2));

                    float pdiff = breath - 0.5f * refl;          // jet reflection ~0.5
                    // Push through the convective jet delay (linear interp).
                    int   jid = (int)e.jet_delay;
                    float jfr = e.jet_delay - (float)jid;
                    int   j0  = (e.jet_write - jid)     & JET_MASK;
                    int   j1  = (e.jet_write - jid - 1) & JET_MASK;
                    float jd  = e.jet[j0] * (1.0f - jfr) + e.jet[j1] * jfr;
                    e.jet[e.jet_write] = pdiff;
                    e.jet_write = (e.jet_write + 1) & JET_MASK;

                    // Cubic jet nonlinearity x^3 - x (saturated), DC-blocked.
                    float x  = std::clamp(jet_drive_v * jd, -1.0f, 1.0f);
                    float nl = x * (x * x - 1.0f);
                    nl = dc_block(nl, e.dc_x1, e.dc_y1);
                    bore_in = nl + refl;                         // + end reflection

                } else { // MODEL_BRASS
                    // ---- Lip-reed resonator + outward-striking valve ----
                    float mouth = 0.3f * breath;
                    float bore  = 0.92f * refl;      // low loss so the bore builds
                    float dpr   = mouth - bore;
                    // Lip resonator with a DC-blocking zero (1 - z^-1): the lip
                    // DISPLACEMENT oscillates around zero.  (A plain all-pole
                    // resonator has huge DC gain that pins the valve fully open
                    // on the steady mouth pressure, so it never oscillates.)
                    float y = (dpr - e.lip_x1) + lc1 * e.lip_y1 - lc2 * e.lip_y2;
                    e.lip_x1 = dpr;
                    e.lip_y2 = e.lip_y1; e.lip_y1 = y;
                    // Outward-striking lip: the opening modulates around a rest
                    // point (linear, clamped) rather than slamming via y^2.
                    float area = std::clamp(0.45f + 3.0f * y, 0.0f, 1.0f);
                    // Flow scattering across the lip valve.
                    float s = area * mouth + (1.0f - area) * bore;
                    bore_in = dc_block(s, e.dc_x1, e.dc_y1);
                }

                // Stability clamp on loop state.
                bore_in = std::clamp(bore_in, -4.0f, 4.0f);

                // --- Write into bore and advance ---
                e.bore[e.bore_write] = bore_in;
                e.bore_write = (e.bore_write + 1) & BORE_MASK;

                // --- Output: tap the bore OUTPUT (the standing-wave pressure
                // radiated at the bell), not the raw exciter drive.  Tapping
                // the flute's jet drive (bore_in) injected the cubic
                // nonlinearity's full harmonic series directly, making the
                // flute too bright/reedy ("brassy"); the bore output is the
                // smoother, more sinusoidal radiated tone a flute should have.
                // Per-model output gains: the steep reed runs at a much lower
                // oscillation amplitude than the flute jet, so the reeds get a
                // large make-up gain to bring the family into level balance.
                float sig;
                if (model == MODEL_FLUTE)      sig = 0.22f * bore_out;
                else if (model == MODEL_BRASS) sig = 2.2f  * bore_out;   // low osc. amplitude → make-up gain
                else                           sig = 0.45f * bore_out;   // clarinet/sax

                float sample = sig * amp * gain;
                L[i] += sample;
                if (out->right) R[i] += sample;   // avoid double-add when mono (R aliases L)
            }
        }

        // Soft clip for safety / brassy saturation.
        for (int i = 0; i < N; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    VoiceManager<WindExt> vm_;
};

REGISTER_PLUGIN(WindInstrumentPlugin);
REGISTER_PLUGIN_DYNAMIC(WindInstrumentPlugin);
