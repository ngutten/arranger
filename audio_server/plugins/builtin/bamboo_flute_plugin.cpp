// bamboo_flute_plugin.cpp
// Breath-blown stopped-pipe waveguide — "bamboo flute" / panpipe synthesizer,
// extended with a wood→metal voicing range and expressive controls.
//
// Core model (ported from the syrinx research reference
// instruments/bamboo_flute.py): a LINEAR stopped-pipe resonator (half-period
// delay loop with NEGATED feedback → odd-harmonic "hollow" timbre at every
// pitch) driven by continuous breath NOISE passing *through* the resonance,
// with a small coherent periodic pulse train blended in up the register
// (auto_tonal) so high notes stay tonal. Two pitch schedules (auto_damp /
// auto_tonal) make it playable across the whole keyboard. This is a different
// physical idea from builtin.wind_instrument (a nonlinear self-oscillating
// reed/jet) — the two are intentionally separate.
//
// Extensions over the bare panpipe (all NEUTRAL at their defaults, so the
// default voicing is byte-identical to the original port):
//   - stiffness  : in-loop allpass dispersion → inharmonic partials (wood→bell)
//   - resonance  : exposed loop gain g (ring / decay time)
//   - drive      : in-loop tanh saturation → brassy/metallic overblow
//   - metalness  : inharmonic body-mode resonator bank (+ optional struck onset)
//   - openness   : asymmetric shaper → even-harmonic fill (hollow→open/bright)
//   - air / edge : breath-noise tone and periodic-pulse reediness
//   - vibrato / tremolo / chiff / ensemble : expressive extras
//
// Built on the shared synth_common.h VoiceManager plus the waveguide-string
// allpass fractional delay / one-pole loop / ADSR / voice_amp() scaffolding.

#include "plugin_api.h"
#include "synth_common.h"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstring>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Voice extension — stopped-pipe delay line and filter state
// ---------------------------------------------------------------------------

static constexpr int MAX_DELAY = 4096;        // half-period: f0 >= ~5.4 Hz @ 44.1 kHz
static constexpr int DELAY_MASK = MAX_DELAY - 1;
static constexpr int MAX_DISP   = 4;          // dispersion allpass sections
static constexpr int NUM_MODES  = 3;          // body-formant resonator modes
static constexpr int ENS_SIZE   = 1024;       // ensemble chorus delay buffer
static constexpr int ENS_MASK   = ENS_SIZE - 1;

struct BambooExt {
    float delay_line[MAX_DELAY] = {};
    int   write_pos = 0;

    // First-order allpass fractional delay
    float ap_z1 = 0.0f;
    float prev_raw = 0.0f;

    // Loop wall-loss filter (1-pole lowpass)
    float lp_z1 = 0.0f;

    // Dispersion allpass cascade (first-order sections): x[n-1], y[n-1] per stage
    float disp_x1[MAX_DISP] = {};
    float disp_y1[MAX_DISP] = {};

    // DC blocker after the even-harmonic shaper
    float dc_x1 = 0.0f;
    float dc_y1 = 0.0f;

    // Breath excitation: 1-pole lowpass state on the white noise
    float b_prev = 0.0f;

    // Periodic-drive phase accumulator and Hann-pulse deposit countdown
    float pulse_phase = 0.0f;
    int   pulse_idx = 0;          // samples remaining in the current pulse deposit

    // Per-voice noise RNG (xorshift32)
    uint32_t rng = 12345u;

    // Delay tracking (slew-limited)
    float current_delay = 100.0f;

    // Body-formant resonator bank (biquad state per mode)
    float body_x1[NUM_MODES] = {};
    float body_x2[NUM_MODES] = {};
    float body_y1[NUM_MODES] = {};
    float body_y2[NUM_MODES] = {};
    float strike_env = 0.0f;      // decaying impulse injected into the bank at onset

    // Expressive LFO (vibrato + tremolo) and onset chiff
    double lfo_phase = 0.0;
    int    chiff_idx = 0;

    // Ensemble (detuned chorus) delay line + modulation phase
    float  ens_buf[ENS_SIZE] = {};
    int    ens_pos = 0;
    double ens_lfo = 0.0;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static inline float xorshift_float(uint32_t& state) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return static_cast<float>(static_cast<int32_t>(state)) * (1.0f / 2147483648.0f);
}

// auto_damp: one-pole wall-loss coefficient, scheduled by pitch. Loop loss
// DECREASES with pitch so the resonance Q holds up the register — a fixed damp
// over-damps high notes (they go noisy). 0.55 @A3 (the good low voicing),
// floored at 0.12. `tilt` scales the deviation from the A3 base (default 1).
static inline float auto_damp(float f0, float tilt) {
    float ref = 0.55f * (220.0f / f0);
    float d = 0.55f + tilt * (ref - 0.55f);
    return std::clamp(d, 0.12f, 0.55f);
}

// auto_tonal: coherent periodic-drive strength. 0 at/below A3 (pure breath —
// the original voicing, preserved exactly), ramping up with pitch so high
// notes get a tonal seed. `tilt` scales the slope (default 1).
static inline float auto_tonal(float f0, float tilt) {
    float t = tilt * 0.028f * std::log2(std::max(f0, 220.0f) / 220.0f);
    return std::clamp(t, 0.0f, 0.07f);
}

// A constant-skirt-gain bandpass biquad (RBJ), coefficients normalized by a0.
struct Biquad {
    float b0 = 0.0f, b2 = 0.0f, a1 = 0.0f, a2 = 0.0f;
    void bandpass(float freq, float Q, float sr) {
        float w0 = 2.0f * static_cast<float>(M_PI) * freq / sr;
        float alpha = std::sin(w0) / (2.0f * std::max(0.1f, Q));
        float a0 = 1.0f + alpha;
        b0 =  alpha / a0;
        b2 = -alpha / a0;
        a1 = (-2.0f * std::cos(w0)) / a0;
        a2 = (1.0f - alpha) / a0;
    }
};

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class BambooFlutePlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.bamboo_flute";
        d.display_name = "Bamboo Flute";
        d.category     = "Synth";
        d.doc          = "Breath-blown stopped-pipe waveguide (panpipe / shakuhachi). "
                         "A linear odd-harmonic resonator driven by continuous breath "
                         "noise with a pitch-scheduled coherent drive so tonality holds "
                         "across the register. Stiffness/resonance/drive/metalness extend "
                         "the voicing from bamboo toward struck metal tube and bell; air, "
                         "edge, vibrato, chiff and ensemble add expression. Distinct from "
                         "the self-oscillating Wind Instrument reed/jet model.";
        d.author       = "builtin";
        d.version      = 2;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output.",
              PluginPortType::AudioStereo, PortRole::Output },

            // --- Primary (visible on canvas) ---
            { "gain", "Gain", "Output level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 2.0f },
            { "breath", "Breath", "Level of the breath-noise excitation.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f },
            { "brightness", "Brightness", "Scales loop loss (higher = brighter/less damped).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.5f, 1.5f },
            { "tonal", "Tonal", "Periodic-drive amount (0=pure breath, >1=more pitched).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f },
            { "stiffness", "Stiffness", "In-loop dispersion: stretches upper partials sharp (inharmonic shimmer, strongest up high). Pair with Metalness for full metal.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "resonance", "Resonance", "Loop gain / ring time (higher = longer metallic ring).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.99f, 0.90f, 0.999f },
            { "drive", "Drive", "In-loop saturation / overblow (1=clean, >1=brassy/metallic).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 1.0f, 8.0f },
            { "metalness", "Metalness", "Inharmonic body-mode bank mix (struck-metal clang).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "body_resonance", "Body Reso", "Woody body-formant mix (single formant at Body Freq).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },

            // --- Hidden (show_port_default=false) ---
            { "body_freq", "Body Freq", "Body formant / mode-1 center (Hz).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 689.0f, 200.0f, 1200.0f, 0.0f, {}, "", false },
            { "body_strike", "Body Strike", "Struck impulse into the body bank at note onset.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "dispersion_modes", "Disp Stages", "Allpass dispersion sections (more = stronger stretch).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 2.0f, 1.0f, 4.0f, 0.0f, {}, "", false },
            { "openness", "Openness", "Even-harmonic fill (0=hollow/odd, 1=open/bright).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "air", "Air", "Breath-noise lowpass cutoff (Hz): dark/airy → hissy.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1240.0f, 200.0f, 8000.0f, 0.0f, {}, "", false },
            { "edge", "Edge", "Periodic-pulse reediness (0=round, 1=narrow/buzzy).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "vibrato_rate", "Vib Rate", "Vibrato/tremolo LFO rate (Hz).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 5.0f, 0.1f, 12.0f, 0.0f, {}, "", false },
            { "vibrato_depth", "Vib Depth", "Vibrato pitch depth (semitones).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "tremolo_depth", "Trem Depth", "Tremolo amplitude depth.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "chiff", "Chiff", "Onset breath-noise burst (panpipe/recorder chiff).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "ensemble", "Ensemble", "Detuned stereo doubling (multi-pipe width).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "vel_sens", "Vel Sens", "Velocity → breath pressure (0=flat, 1=full dynamics).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.6f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "register_tilt", "Register Tilt", "Scales the slope of both pitch schedules.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f, 0.0f, {}, "", false },
            { "attack", "Attack", "ADSR attack (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.04f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "decay", "Decay", "ADSR decay (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "sustain", "Sustain", "ADSR sustain level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "release", "Release", "ADSR release (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.08f, 0.0f, 4.0f, 0.0f, {}, "", false },
        };

        d.note_attrs = { standard_attack_attr() };
        d.config_params = { standard_attr_remap_param() };
        return d;
    }

    void activate(float sample_rate, int max_block_size) override {
        vm_.init(sample_rate, max_block_size);
        build_pulse_window(0.0f);   // edge=0 → reference ~0.2 ms Hann pulse
        edge_cached_ = 0.0f;
    }

    void deactivate() override {
        for (auto& v : vm_.voices) v = {};
    }

    void note_on(int channel, int pitch, int velocity) override {
        if (velocity == 0) { note_off(channel, pitch); return; }
        auto* v = vm_.trigger(channel, pitch, velocity, attack_, decay_, sustain_, release_);
        if (!v) return;

        std::memset(v->ext.delay_line, 0, sizeof(v->ext.delay_line));
        v->ext.write_pos    = 0;
        v->ext.ap_z1        = 0.0f;
        v->ext.prev_raw     = 0.0f;
        v->ext.lp_z1        = 0.0f;
        std::memset(v->ext.disp_x1, 0, sizeof(v->ext.disp_x1));
        std::memset(v->ext.disp_y1, 0, sizeof(v->ext.disp_y1));
        v->ext.dc_x1        = 0.0f;
        v->ext.dc_y1        = 0.0f;
        v->ext.b_prev       = 0.0f;
        v->ext.pulse_phase  = 0.0f;
        v->ext.pulse_idx    = 0;
        std::memset(v->ext.body_x1, 0, sizeof(v->ext.body_x1));
        std::memset(v->ext.body_x2, 0, sizeof(v->ext.body_x2));
        std::memset(v->ext.body_y1, 0, sizeof(v->ext.body_y1));
        std::memset(v->ext.body_y2, 0, sizeof(v->ext.body_y2));
        v->ext.lfo_phase    = 0.0;
        std::memset(v->ext.ens_buf, 0, sizeof(v->ext.ens_buf));
        v->ext.ens_pos      = 0;
        v->ext.ens_lfo      = 0.0;

        // Struck-onset impulse into the body bank (scaled by velocity).
        v->ext.strike_env = body_strike_ * (velocity / 127.0f);
        // Onset chiff burst: a few ms of extra breath gated by the attack.
        v->ext.chiff_idx = static_cast<int>(chiff_ * 0.03f * vm_.sample_rate);

        float f0 = pitch_to_freq(static_cast<float>(pitch));
        v->ext.current_delay = vm_.sample_rate / (2.0f * f0);  // HALF-period

        v->ext.rng = 12345u ^ (static_cast<uint32_t>(pitch) * 65537u)
                   ^ (static_cast<uint32_t>(velocity) * 2654435761u);
    }

    void note_off(int channel, int pitch) override {
        vm_.release_note(channel, pitch);
    }

    void note_attr(int channel, int note, const std::string& id, float value) override {
        vm_.set_pending_attr(channel, note, id.c_str(), value);
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "attr_remap") vm_.configure_attr_remap(value);
    }

    void all_notes_off(int channel) override {
        vm_.all_notes_off(channel);
    }

    void note_tune(int channel, int note, float semitones) override {
        vm_.tune(channel, note, semitones);
    }

    void channel_volume(int channel, int volume) override {
        vm_.set_channel_volume(channel, volume);
    }

    void channel_pan(int channel, int pan) override {
        vm_.set_channel_pan(channel, pan);
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* out = buffers.audio.get("audio_out");
        if (!out) return;

        float* L = out->left;
        float* R = out->right ? out->right : out->left;
        const int N = ctx.block_size;
        const float sr = vm_.sample_rate;

        std::memset(L, 0, N * sizeof(float));
        if (out->right) std::memset(R, 0, N * sizeof(float));

        auto ctrl = [&](const char* id, float fb) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fb;
        };

        float gain       = ctrl("gain", 0.5f);
        float breath     = ctrl("breath", 1.0f);
        float brightness = std::max(0.01f, ctrl("brightness", 1.0f));
        float tonal_user = ctrl("tonal", 1.0f);
        float stiffness  = std::clamp(ctrl("stiffness", 0.0f), 0.0f, 1.0f);
        float g_loop     = std::clamp(ctrl("resonance", 0.99f), 0.0f, 0.9995f);
        float drive      = std::max(1.0f, ctrl("drive", 1.0f));
        float metalness  = std::clamp(ctrl("metalness", 0.0f), 0.0f, 1.0f);
        float body_reso  = std::clamp(ctrl("body_resonance", 0.0f), 0.0f, 1.0f);
        float body_freq  = ctrl("body_freq", 689.0f);
        float openness   = std::clamp(ctrl("openness", 0.0f), 0.0f, 1.0f);
        float air        = ctrl("air", 1240.0f);
        float edge       = std::clamp(ctrl("edge", 0.0f), 0.0f, 1.0f);
        float vib_rate   = ctrl("vibrato_rate", 5.0f);
        float vib_depth  = ctrl("vibrato_depth", 0.0f);
        float trem_depth = std::clamp(ctrl("tremolo_depth", 0.0f), 0.0f, 1.0f);
        float ensemble   = std::clamp(ctrl("ensemble", 0.0f), 0.0f, 1.0f);
        float tilt       = ctrl("register_tilt", 1.0f);
        float vel_sens   = std::clamp(ctrl("vel_sens", 0.6f), 0.0f, 1.0f);
        body_strike_     = std::clamp(ctrl("body_strike", 0.0f), 0.0f, 1.0f);
        chiff_           = std::clamp(ctrl("chiff", 0.0f), 0.0f, 1.0f);
        int   disp_modes = std::clamp(static_cast<int>(std::lround(ctrl("dispersion_modes", 2.0f))),
                                      1, MAX_DISP);

        attack_  = std::max(0.001f, ctrl("attack",  0.04f));
        decay_   = std::max(0.001f, ctrl("decay",   0.1f));
        sustain_ = ctrl("sustain", 1.0f);
        release_ = std::max(0.001f, ctrl("release", 0.08f));

        vm_.begin_block(N);

        // --- Breath lowpass coefficient from the Air cutoff (per block) ---
        float exc_lp = std::exp(-2.0f * static_cast<float>(M_PI) * air / sr);

        // --- Periodic-pulse window: rebuild only when Edge changes ---
        if (edge != edge_cached_) { build_pulse_window(edge); edge_cached_ = edge; }

        // --- Body-mode bank: mode frequencies & coefficients (constant/block) ---
        // metalness stretches modes 2,3 to inharmonic free-bar-ish ratios and
        // raises their Q (longer ring); at metalness=0 the bank is a single
        // woody formant. The effective wet mix screens body_reso with metalness
        // so Metalness alone is audible without touching Body Reso.
        float body_mix = body_reso + metalness * (1.0f - body_reso);
        const float ratio2 = 1.0f + metalness * (2.76f - 1.0f);
        const float ratio3 = 1.0f + metalness * (5.40f - 1.0f);
        float mode_freq[NUM_MODES] = { body_freq, body_freq * ratio2, body_freq * ratio3 };
        float mode_Q   [NUM_MODES] = { 1.5f, 1.5f + 6.0f * metalness, 1.5f + 8.0f * metalness };
        float mode_gain[NUM_MODES] = { 1.0f, metalness, 0.7f * metalness };
        Biquad mode_bq[NUM_MODES];
        for (int m = 0; m < NUM_MODES; ++m) {
            mode_freq[m] = std::clamp(mode_freq[m], 20.0f, 0.45f * sr);
            mode_bq[m].bandpass(mode_freq[m], mode_Q[m], sr);
        }

        // --- Dispersion allpass coefficient (negative → partials stretch sharp) ---
        const bool  use_disp = stiffness > 1.0e-4f;
        const float disp_a = -0.5f * stiffness;
        // Couple stiffness → less coherent drive: a harmonic pulse train pins
        // partials to exact harmonics and fights the dispersion, so as the pipe
        // stiffens we back the periodic drive off and let the resonator's own
        // (inharmonic) modes ring — that is what reads as bell/metal.
        const float tonal_stiff = 1.0f - 0.85f * stiffness;

        const bool use_vib  = vib_depth  > 1.0e-4f;
        const bool use_trem = trem_depth > 1.0e-4f;
        const bool use_ens  = ensemble   > 1.0e-4f;
        const bool use_open = openness    > 1.0e-4f;
        const bool use_body = body_mix    > 1.0e-3f;

        // Internal makeup (see original port): the breath drive levels are
        // arbitrary; fold a makeup gain so the default `gain` lands sanely.
        constexpr float kNoise  = 1.0e-3f;
        constexpr float kMakeup = 6.0f;

        for (int vi = 0; vi < SYNTH_MAX_VOICES; ++vi) {
            auto& v = vm_.voices[vi];
            if (!v.active) continue;

            float gl, gr; vm_.voice_amp(v, gl, gr);
            const float lfo_inc = static_cast<float>(vib_rate) / sr;

            // Velocity → breath pressure: scale the excitation that drives the
            // resonator (louder, and brighter via the in-loop saturation), not a
            // post-gain. vel_sens=0 → flat (velocity-independent); =1 → full,
            // softest note → silence. Constant per note, so hoist it here.
            const float vel_gain = std::clamp(1.0f - vel_sens * (1.0f - v.velocity),
                                              0.0f, 1.0f);

            for (int i = 0; i < N; ++i) {
                float env_val = v.env.next();
                if (v.env.is_off()) { v.active = false; break; }

                float p  = VoiceManager<BambooExt>::interpolated_pitch(v, i, N);
                float f0_base = pitch_to_freq(p);

                // --- Vibrato / tremolo LFO ---
                float lfo = 0.0f, trem_gain = 1.0f;
                if (use_vib || use_trem) {
                    lfo = std::sin(2.0f * static_cast<float>(M_PI)
                                   * static_cast<float>(v.ext.lfo_phase));
                    v.ext.lfo_phase += lfo_inc;
                    if (v.ext.lfo_phase >= 1.0) v.ext.lfo_phase -= 1.0;
                    if (use_trem) trem_gain = 1.0f - 0.5f * trem_depth * (1.0f - lfo);
                }
                float f0 = use_vib
                    ? f0_base * std::pow(2.0f, (vib_depth * lfo) / 12.0f)
                    : f0_base;

                // Pitch schedules (use steady base pitch, not the vibrato'd f0)
                float damp  = auto_damp(f0_base, tilt) / brightness;
                damp = std::clamp(damp, 0.0f, 0.95f);
                float tonal = auto_tonal(f0_base, tilt) * tonal_user * tonal_stiff;

                // --- Breath excitation: lowpassed white noise, every sample ---
                float w = xorshift_float(v.ext.rng) * kNoise;
                v.ext.b_prev = exc_lp * v.ext.b_prev + (1.0f - exc_lp) * w;
                float e = v.ext.b_prev * 3.0f * breath;

                // --- Onset chiff: extra breath burst gated by attack ---
                if (v.ext.chiff_idx > 0) {
                    e += xorshift_float(v.ext.rng) * kNoise * 8.0f * chiff_ * breath;
                    v.ext.chiff_idx--;
                }

                // --- Coherent periodic drive: short Hann pulse once per period ---
                v.ext.pulse_phase += f0 / sr;
                if (v.ext.pulse_phase >= 1.0f) {
                    v.ext.pulse_phase -= 1.0f;
                    if (tonal > 0.0f) v.ext.pulse_idx = pulse_width_;
                }
                if (v.ext.pulse_idx > 0) {
                    e += tonal * pulse_win_[pulse_width_ - v.ext.pulse_idx];
                    v.ext.pulse_idx--;
                }

                // Velocity as breath pressure on the whole excitation.
                e *= vel_gain;

                // --- Stopped-pipe loop delay length ---
                // Total loop delay = sr/(2*f0); subtract the filter group delays
                // (allpass frac ~0.5, one-pole loss ~ damp/(1-damp), and the
                // dispersion cascade) so the fundamental stays in tune while
                // higher partials stretch sharp (inharmonic).
                float group_delay = 0.5f + damp / std::max(1.0e-3f, 1.0f - damp);
                if (use_disp) {
                    float w0 = 2.0f * static_cast<float>(M_PI) * f0 / sr;
                    float gd = (1.0f - disp_a * disp_a)
                             / (1.0f + 2.0f * disp_a * std::cos(w0) + disp_a * disp_a);
                    group_delay += disp_modes * gd;
                }
                float target_delay = sr / (2.0f * f0) - group_delay;
                target_delay = std::clamp(target_delay, 2.0f,
                                          static_cast<float>(MAX_DELAY - 2));

                float max_rate = std::max(v.ext.current_delay * 0.0005f, 0.001f);
                float ddl = std::clamp(target_delay - v.ext.current_delay,
                                       -max_rate, max_rate);
                v.ext.current_delay += ddl;

                int int_delay = static_cast<int>(v.ext.current_delay);
                float frac = v.ext.current_delay - static_cast<float>(int_delay);
                float a = (1.0f - frac) / (1.0f + frac);   // frac-delay allpass coeff

                int read_pos = (v.ext.write_pos - int_delay - 1) & DELAY_MASK;
                float raw = v.ext.delay_line[read_pos];

                // First-order allpass fractional delay
                float d = a * raw + v.ext.prev_raw - a * v.ext.ap_z1;
                v.ext.ap_z1 = d;
                v.ext.prev_raw = raw;

                // Dispersion allpass cascade (in the loop) → inharmonic stretch
                if (use_disp) {
                    for (int s = 0; s < disp_modes; ++s) {
                        float x = d;
                        d = disp_a * x + v.ext.disp_x1[s] - disp_a * v.ext.disp_y1[s];
                        v.ext.disp_x1[s] = x;
                        v.ext.disp_y1[s] = d;
                    }
                }

                // One-pole wall-loss filter
                float lp = damp * v.ext.lp_z1 + (1.0f - damp) * d;
                v.ext.lp_z1 = lp;

                // In-loop saturation / overblow (unity small-signal gain, so
                // drive=1 is the clean linear pipe — neutral default)
                float fb = lp;
                if (drive > 1.0001f) fb = std::tanh(drive * lp) / drive;

                // NEGATED feedback → stopped pipe (odd-harmonic comb)
                float y = e - g_loop * fb;
                v.ext.delay_line[v.ext.write_pos] = y;   // assign, not accumulate
                v.ext.write_pos = (v.ext.write_pos + 1) & DELAY_MASK;

                // Even-harmonic fill (openness): blend in full-wave-rectified
                // |y|, whose even-harmonic content is FIRST-order in amplitude
                // (so it stays audible for the small loop signal — y*y would be
                // ~26 dB down). The DC block removes the rectifier's DC offset.
                float sig = y;
                if (use_open) {
                    float shaped = y + openness * 2.0f * std::fabs(y);
                    float db = shaped - v.ext.dc_x1 + 0.995f * v.ext.dc_y1;
                    v.ext.dc_x1 = shaped;
                    v.ext.dc_y1 = db;
                    sig = db;
                }

                // Body-formant resonator bank (woody single formant → metal modes).
                // metalness injects broadband breath + a struck impulse into the
                // bank so the (inharmonic) modes RING at their own frequencies
                // rather than only filtering wherever the pipe already has
                // energy — that is what gives audible metal clang. At
                // metalness=0 the bank is purely the pipe-fed woody formant.
                if (use_body) {
                    // Struck onset = a short noise burst (mallet) gated by the
                    // decaying strike envelope; excites every mode at once.
                    float excite = 0.0f;
                    if (v.ext.strike_env > 1.0e-5f)
                        excite += xorshift_float(v.ext.rng) * v.ext.strike_env * 0.5f;
                    if (metalness > 1.0e-4f)
                        excite += xorshift_float(v.ext.rng) * kNoise * 5.0f
                                  * metalness * breath;
                    float bank_in = sig + excite;
                    float bp_sum = 0.0f;
                    for (int m = 0; m < NUM_MODES; ++m) {
                        const Biquad& bq = mode_bq[m];
                        float bp = bq.b0 * bank_in + bq.b2 * v.ext.body_x2[m]
                                 - bq.a1 * v.ext.body_y1[m] - bq.a2 * v.ext.body_y2[m];
                        v.ext.body_x2[m] = v.ext.body_x1[m];
                        v.ext.body_x1[m] = bank_in;
                        v.ext.body_y2[m] = v.ext.body_y1[m];
                        v.ext.body_y1[m] = bp;
                        bp_sum += mode_gain[m] * bp;
                    }
                    sig = sig * (1.0f - body_mix) + bp_sum * body_mix;
                    v.ext.strike_env *= 0.9997f;  // ~0.5 s struck-mode ring @48k
                }

                // --- Output / ensemble (detuned stereo doubling) ---
                float outL = sig * gl, outR = sig * gr;
                if (use_ens) {
                    v.ext.ens_buf[v.ext.ens_pos] = sig;
                    float base = 0.006f * sr;                 // ~6 ms
                    float depth = 0.0025f * sr;               // ±2.5 ms
                    float ml = std::sin(2.0f * static_cast<float>(M_PI)
                                        * static_cast<float>(v.ext.ens_lfo));
                    float mr = std::sin(2.0f * static_cast<float>(M_PI)
                                        * static_cast<float>(v.ext.ens_lfo) + 2.094f); // +120°
                    float dl = base + depth * ml, dr = base + depth * mr;
                    float wetL = ens_read(v.ext, dl);
                    float wetR = ens_read(v.ext, dr);
                    v.ext.ens_pos = (v.ext.ens_pos + 1) & ENS_MASK;
                    v.ext.ens_lfo += 0.6f / sr;               // slow ~0.6 Hz shimmer
                    if (v.ext.ens_lfo >= 1.0) v.ext.ens_lfo -= 1.0;
                    outL = (sig * (1.0f - 0.5f * ensemble) + wetL * 0.5f * ensemble) * gl;
                    outR = (sig * (1.0f - 0.5f * ensemble) + wetR * 0.5f * ensemble) * gr;
                }

                float amp = kMakeup * env_val * gain * trem_gain;
                L[i] += outL * amp;
                R[i] += outR * amp;
            }
        }

        for (int i = 0; i < N; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    // Build the periodic-drive Hann pulse window. edge∈[0,1] narrows it from
    // ~0.2 ms (round) to ~0.05 ms (buzzy/reedy); always normalized to unit area.
    void build_pulse_window(float edge) {
        float ms = 0.2f - 0.15f * std::clamp(edge, 0.0f, 1.0f);
        pulse_width_ = std::max(2, static_cast<int>(std::round(ms * 0.001f * vm_.sample_rate)));
        pulse_width_ = std::min(pulse_width_, MAX_PULSE);
        float sum = 0.0f;
        for (int k = 0; k < pulse_width_; ++k) {
            float ph = static_cast<float>(k + 1) / static_cast<float>(pulse_width_ + 1);
            pulse_win_[k] = 0.5f * (1.0f - std::cos(2.0f * static_cast<float>(M_PI) * ph));
            sum += pulse_win_[k];
        }
        if (sum > 0.0f)
            for (int k = 0; k < pulse_width_; ++k) pulse_win_[k] /= sum;
    }

    // Linear-interpolated read from the ensemble delay line, `delay` samples back.
    static inline float ens_read(const BambooExt& x, float delay) {
        delay = std::clamp(delay, 1.0f, static_cast<float>(ENS_SIZE - 2));
        int di = static_cast<int>(delay);
        float fr = delay - static_cast<float>(di);
        int i0 = (x.ens_pos - di) & ENS_MASK;
        int i1 = (i0 - 1) & ENS_MASK;
        return x.ens_buf[i0] * (1.0f - fr) + x.ens_buf[i1] * fr;
    }

    VoiceManager<BambooExt> vm_;

    static constexpr int MAX_PULSE = 64;
    float pulse_win_[MAX_PULSE] = {};
    int   pulse_width_ = 10;
    float edge_cached_ = -1.0f;

    float body_strike_ = 0.0f;
    float chiff_       = 0.0f;

    float attack_  = 0.04f;
    float decay_   = 0.1f;
    float sustain_ = 1.0f;
    float release_ = 0.08f;
};

REGISTER_PLUGIN(BambooFlutePlugin);
REGISTER_PLUGIN_DYNAMIC(BambooFlutePlugin);
