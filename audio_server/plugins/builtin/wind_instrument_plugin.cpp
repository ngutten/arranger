// wind_instrument_plugin.cpp
// ==========================================================================
// Wind Instrument — physical modeling synthesizer (digital waveguide)
// ==========================================================================
//
// Polyphonic, MIDI-driven digital-waveguide wind synth (clarinet, sax, flute,
// brass), built on the McIntyre-Schumacher-Woodhouse single-delay-loop /
// nonlinear-reed formulation (STK-style).
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
// IMPORTANT: every model is driven by the SAME inverting single-reed exciter
// (nonlinear reed table, inverting closed-open bore, quarter-wave delay).  It
// is the only self-oscillator that stays in tune across the whole range — the
// non-inverting "conical" reed (sax/brass) overblows / runs sharp, the air-jet
// (flute) period-doubles an octave low, and the lip-reed (brass) collapsed to a
// low mode above G4.  Each instrument's timbre is therefore made at the OUTPUT
// stage, not by a distinct exciter:
//   - Clarinet: light loop loss -> sparse odd harmonics, hollow.
//   - Sax:      gentle asymmetric waveshaper -> reedy even harmonics.
//   - Flute:    heavy f0-tracking low-pass -> pure, near-sine.
//   - Brass:    ~1.4 kHz formant + asymmetric saturator -> bright/edgy.
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
              ControlHint::Continuous, 0.025f, 0.0f, 0.5f },

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
            { "Alto Sax",   {{"model",1},{"breath",0.9f},{"embouchure",0.55f},
                             {"brightness",0.6f},{"reed_stiffness",0.5f}} },
            { "Flute",      {{"model",2},{"breath",0.85f},{"embouchure",0.5f},
                             {"brightness",0.7f},{"noise",0.05f}} },
            { "Trumpet",    {{"model",3},{"breath",0.9f},{"embouchure",0.55f},
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
        e.bore_write = 0;
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
        const float noise_amt    = ctrl("noise", 0.025f);
        const float vib_rate     = ctrl("vibrato_rate", 5.5f);
        const float vib_depth    = ctrl("vibrato_depth", 0.04f);
        const int   register_blk = (int)ctrl("register", 0.0f);
        const float reed_stiff   = std::clamp(ctrl("reed_stiffness", 0.55f), 0.1f, 1.0f);
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
        // Clarinet, sax AND brass are all driven by the single-reed exciter
        // (reliable self-oscillation in tune across the whole range).  Brass's
        // brass-ness comes from a bright bore + output formant + saturation,
        // not a lip-reed feedback model (that collapsed to a low mode above G4).
        //
        // EVERY model now uses the inverting (cylindrical closed-open) reed
        // bore — the only self-oscillator that stays in tune across the whole
        // range.  The non-inverting configs all period-double / overblow at
        // high f0: the conical reed (sax/brass) ran sharp / collapsed, and the
        // air-jet flute dropped an octave at most pitches.  So the flute's jet
        // model is replaced by the inverting bore + a heavy output low-pass
        // that yields a pure, near-sine flute.  Each model's distinct timbre
        // comes from the output stage (low-pass / waveshaper / formant).
        // Loop gain just below 1; brighter = less high-frequency loss.
        float loss_pole   = 0.05f + 0.55f * (1.0f - brightness); // 1-pole LP coeff
        float loop_gain   = 0.985f + 0.012f * brightness;        // < 1 for stability
        // Clarinet: very light loop low-pass so its sparse ODD harmonics survive
        // → bright, hollow, reedy (rather than pure/flutey).
        if (model == MODEL_CLARINET)
            loss_pole = std::min(loss_pole, 0.04f + 0.06f * (1.0f - brightness));
        // Sax & brass use the default loop loss (inverting bore → clean, in-tune
        // oscillation); their even harmonics + brightness come from the output
        // waveshaper.  Flute also uses the default loss: a heavy loop low-pass
        // pushed the jet into period-doubling (an octave-low f0/2 subharmonic).

        for (int vi = 0; vi < SYNTH_MAX_VOICES; ++vi) {
            auto& v = vm_.voices[vi];
            if (!v.active) continue;
            WindExt& e = v.ext;

            const int reg = (e.register_mode >= 0) ? e.register_mode : register_blk;

            // Brass output formant: a fixed resonant band-pass (~1.4 kHz — the
            // "brass formant" from the bore flare / bell) applied to the brass
            // OUTPUT for its metallic colour.  Brass is driven by the same
            // robust reed exciter as the sax (reliable pitch across the whole
            // range — the lip-reed feedback model collapsed to a low mode above
            // ~G4); the formant + output saturation are what make it read as
            // brass rather than a bright sax.  lip_q sets the formant sharpness.
            float fc1 = 0.0f, fc2 = 0.0f;
            if (model == MODEL_BRASS) {
                float fw0  = 2.0f * (float)M_PI * std::min(1400.0f, 0.45f * sr) / sr;
                float frad = std::clamp(0.85f + 0.01f * lip_q, 0.80f, 0.97f);
                fc1 = 2.0f * frad * std::cos(fw0);
                fc2 = frad * frad;
            }

            // Per-voice breath-attack coefficient (scaled by the "attack" attr).
            const float v_atk_c = 1.0f - std::exp(
                -1.0f / (std::max(0.001f, br_attack * e.attack_mul) * sr));

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

                // All models use the inverting bore → quarter-wave at f0.
                // The -1 subtracts the loop-filter + allpass group delay.
                float period = sr / f0;
                float target_delay = 0.25f * period - 1.0f;
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
                //  EXCITER — single-reed nonlinear scattering (MSW / Scavone),
                //  inverting bore, shared by every model.  Per-model timbre is
                //  applied at the output stage below.
                // ============================================================
                float r  = -refl;                            // inverting bore
                float dp = r - breath;                        // diff. pressure
                float Rt = reed_offset + reed_slope * dp;     // reed table
                Rt = std::clamp(Rt, -1.0f, 1.0f);
                bore_in = breath + dp * Rt;

                // Stability clamp on loop state.
                bore_in = std::clamp(bore_in, -4.0f, 4.0f);

                // --- Write into bore and advance ---
                e.bore[e.bore_write] = bore_in;
                e.bore_write = (e.bore_write + 1) & BORE_MASK;

                // --- Output stage: each model shapes the (stable, odd-harmonic)
                // bore output into its timbre.  Tap bore_out (the radiated
                // standing wave), not the raw exciter drive.
                float sig;
                if (model == MODEL_FLUTE) {
                    // Flute: a heavy 1-pole low-pass tracking ~1.5*f0 strips the
                    // odd harmonics down to a near-sine, pure flute tone (lip_y1
                    // is the LP state).  Breath noise (above) supplies the air.
                    float fcut = std::min(0.45f, 1.5f * e.f0 / sr);
                    float fcoef = 1.0f - std::exp(-2.0f * (float)M_PI * fcut);
                    e.lip_y1 += fcoef * (bore_out - e.lip_y1);
                    sig = 0.9f * e.lip_y1;
                } else if (model == MODEL_BRASS) {
                    // The inverting reed gives a stable, in-tune but odd-only
                    // (clarinet-like) tone.  Turn it into brass: boost the
                    // ~1.4 kHz brass formant (resonant band-pass, reusing the
                    // lip state), then an ASYMMETRIC saturator adds the even
                    // harmonics and the bright brassy "blat" (a symmetric shaper
                    // on an odd signal stays odd, hence the bias).  DC-blocked.
                    float bf = (bore_out - e.lip_x1) + fc1 * e.lip_y1 - fc2 * e.lip_y2;
                    e.lip_x1 = bore_out;
                    e.lip_y2 = e.lip_y1; e.lip_y1 = bf;
                    float pre = bore_out + 0.8f * bf;
                    // Asymmetric drive (bias -> even harmonics) for the brassy
                    // edge, level-matched to the rest of the family.
                    float shaped = std::tanh(3.0f * pre + 0.4f);
                    sig = 0.42f * dc_block(shaped, e.dc_x1, e.dc_y1);
                } else if (model == MODEL_SAX) {
                    // Sax: the inverting reed is odd-harmonic (clarinet-like); a
                    // gentle asymmetric waveshaper fills in even harmonics for a
                    // fuller, reedier tone — milder than brass, no bright formant.
                    float shaped = std::tanh(2.2f * bore_out + 0.3f);
                    sig = 0.4f * dc_block(shaped, e.dc_x1, e.dc_y1);
                } else {
                    sig = 0.45f * bore_out;   // clarinet
                }

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
