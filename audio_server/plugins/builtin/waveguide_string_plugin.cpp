// waveguide_string_plugin.cpp
// Digital waveguide string synthesizer with friction noise and sympathetic coupling.
//
// Models plucked/struck stringed instruments using an extended Karplus-Strong
// waveguide. The defining feature is a friction noise model that generates
// realistic fret/slide noise during pitch changes, driven by pitch-delta.
//
// References:
//   - J.O. Smith III, "Physical Audio Signal Processing", CCRMA Stanford
//     — core waveguide model, loop filter design, fractional delay
//   - Evangelista & Eckerholm, "Player–Instrument Interaction Models for
//     Digital Waveguide Synthesis of Guitar: Touch and Collisions"
//     — two-polarization friction model, fret noise
//   - Evangelista, "Physical Model of the String-Fret Interaction", DAFx 2008
//     — friction force at fret, scattering junction, stick-slip
//   - Välimäki et al., "Elimination of Transients in Time-Varying Allpass
//     Fractional Delay Filters" — click-free pitch bending in waveguides
//   - Jaffe & Smith, "Extensions of the Karplus-Strong Plucked String Algorithm"
//     — fractional delay via first-order allpass

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
// Voice extension — waveguide delay line and filter state
// ---------------------------------------------------------------------------

static constexpr int MAX_DELAY = 4096;        // supports f0 >= ~10.8 Hz @ 44.1 kHz
static constexpr int DELAY_MASK = MAX_DELAY - 1;

// Small ring buffer for excitation comb (pick-position effect).
// Sized to cover pick_offset for pitches down to ~86 Hz at pick_position=0.5.
static constexpr int EXC_BUF_SIZE = 256;
static constexpr int EXC_BUF_MASK = EXC_BUF_SIZE - 1;

struct WaveguideExt {
    float delay_line[MAX_DELAY] = {};
    int   write_pos = 0;

    // First-order allpass fractional delay
    float ap_z1 = 0.0f;
    float prev_raw = 0.0f;

    // Loop filter (1-pole lowpass)
    float lpf_z1 = 0.0f;

    // Friction bandpass state (biquad)
    float fric_x1 = 0.0f;
    float fric_x2 = 0.0f;
    float fric_y1 = 0.0f;
    float fric_y2 = 0.0f;

    // Per-voice noise RNG (xorshift32)
    uint32_t rng = 12345u;

    // Delay tracking
    float current_delay = 100.0f;

    // Excitation
    int   exc_remaining = 0;
    float exc_level = 1.0f;
    // Per-note excitation override from the "excitation" note-attr:
    // 0=Pluck, 1=Strike, -1=use the block-level Excitation param.
    int   excitation_mode = -1;

    // Excitation comb buffer for pick-position effect
    float exc_buf[EXC_BUF_SIZE] = {};
    int   exc_buf_pos = 0;

    // Body resonance (2-pole bandpass biquad state)
    float body_x1 = 0.0f;
    float body_x2 = 0.0f;
    float body_y1 = 0.0f;
    float body_y2 = 0.0f;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static inline float xorshift_float(uint32_t& state) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    // Map to [-1, 1]
    return static_cast<float>(static_cast<int32_t>(state)) * (1.0f / 2147483648.0f);
}

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

class WaveguideStringPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.waveguide_string";
        d.display_name = "Waveguide String";
        d.category     = "Synth";
        d.doc          = "Digital waveguide string model (extended Karplus-Strong) with "
                         "friction noise during pitch slides, sympathetic string coupling, "
                         "and body resonance filter.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output.",
              PluginPortType::AudioStereo, PortRole::Output },

            // --- Primary (visible on canvas) ---
            { "gain", "Gain", "Output level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 2.0f },
            { "damping", "Damping", "Loop filter cutoff (0=bright, 1=dark).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, 0.0f, 1.0f },
            { "excitation", "Excitation", "Excitation type.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 1.0f, 1.0f,
              {"Pluck", "Strike"} },
            { "pluck_width", "Pluck Width", "Noise burst duration (ms).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 2.0f, 0.5f, 10.0f },
            { "pick_position", "Pick Position", "Comb filter on excitation (0.01–0.5).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.13f, 0.01f, 0.5f },
            { "friction_amount", "Friction", "Fret noise level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, 0.0f, 1.0f },
            { "friction_tone", "Friction Tone", "Friction bandpass center (Hz).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 3500.0f, 1000.0f, 8000.0f },
            { "body_resonance", "Body Reso", "Body filter mix.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, 0.0f, 1.0f },
            { "body_freq", "Body Freq", "Body bandpass center (Hz).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 200.0f, 50.0f, 1000.0f },
            { "sympathetic", "Sympathetic", "Coupling between voices.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.0f, 0.5f },

            // --- Hidden (show_port_default=false) ---
            { "friction_response", "Friction Resp", "Friction sensitivity to pitch delta.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.1f, 4.0f, 0.0f, {}, "", false },
            { "attack", "Attack", "ADSR attack (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.005f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "decay", "Decay", "ADSR decay (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "sustain", "Sustain", "ADSR sustain level.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.9f, 0.0f, 1.0f, 0.0f, {}, "", false },
            { "release", "Release", "ADSR release (s).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.3f, 0.0f, 4.0f, 0.0f, {}, "", false },
            { "delta_smooth", "Delta Smooth", "Pitch-delta EMA time constant (s). Controls friction response timing.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.15f, 0.001f, 0.5f, 0.0f, {}, "", false },
        };

        d.note_attrs = {
            standard_attack_attr(),
            NoteAttrDecl{ "excitation", "Excitation",
                "Per-note excitation; overrides the Excitation param for this note.",
                ControlHint::Categorical, 0.0f, 0.0f, 1.0f, {"Pluck", "Strike"} },
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

    void note_on(int channel, int pitch, int velocity) override {
        if (velocity == 0) { note_off(channel, pitch); return; }
        auto* v = vm_.trigger(channel, pitch, velocity, attack_, decay_, sustain_, release_);
        if (!v) return;

        // Clear delay line and filter state
        std::memset(v->ext.delay_line, 0, sizeof(v->ext.delay_line));
        v->ext.write_pos = 0;
        v->ext.ap_z1 = 0.0f;
        v->ext.prev_raw = 0.0f;
        v->ext.lpf_z1 = 0.0f;
        v->ext.fric_x1 = 0.0f;
        v->ext.fric_x2 = 0.0f;
        v->ext.fric_y1 = 0.0f;
        v->ext.fric_y2 = 0.0f;
        v->ext.body_x1 = 0.0f;
        v->ext.body_x2 = 0.0f;
        v->ext.body_y1 = 0.0f;
        v->ext.body_y2 = 0.0f;
        std::memset(v->ext.exc_buf, 0, sizeof(v->ext.exc_buf));
        v->ext.exc_buf_pos = 0;

        // Initialize delay from pitch
        float f0 = pitch_to_freq(static_cast<float>(pitch));
        float d = vm_.sample_rate / f0;
        v->ext.current_delay = d;

        // Excitation burst
        int burst = static_cast<int>(pluck_width_ * 0.001f * vm_.sample_rate);
        v->ext.exc_remaining = std::max(1, burst);
        v->ext.exc_level = 1.0f;

        // Seed RNG uniquely per note
        v->ext.rng = 12345u ^ (static_cast<uint32_t>(pitch) * 65537u)
                   ^ (static_cast<uint32_t>(velocity) * 2654435761u);

        // Per-note excitation override (categorical attr): the note picks
        // Pluck/Strike if present, else -1 falls back to the Excitation param.
        if (const float* e = v->attrs.get("excitation"))
            v->ext.excitation_mode = static_cast<int>(*e);
        else
            v->ext.excitation_mode = -1;
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

        float gain            = ctrl("gain", 0.5f);
        float damping         = ctrl("damping", 0.3f);
        int   excitation_type = static_cast<int>(ctrl("excitation", 0.0f));
        pluck_width_          = ctrl("pluck_width", 2.0f);
        float pick_position   = ctrl("pick_position", 0.13f);
        float friction_amount = ctrl("friction_amount", 0.3f);
        float friction_tone   = ctrl("friction_tone", 3500.0f);
        float friction_resp   = ctrl("friction_response", 1.0f);
        float body_reso       = ctrl("body_resonance", 0.3f);
        float body_freq       = ctrl("body_freq", 200.0f);
        float sympathetic     = ctrl("sympathetic", 0.1f);

        attack_  = std::max(0.001f, ctrl("attack",  0.005f));
        decay_   = std::max(0.001f, ctrl("decay",   0.1f));
        sustain_ = ctrl("sustain", 0.9f);
        release_ = std::max(0.001f, ctrl("release", 0.3f));

        vm_.delta_smooth = ctrl("delta_smooth", 0.15f);

        vm_.begin_block(N);

        // --- Precompute friction bandpass coefficients (constant across block) ---
        // 2nd-order BPF: H(z) via bilinear transform
        float fric_w0 = 2.0f * static_cast<float>(M_PI) * friction_tone / sr;
        float fric_Q  = 2.0f;
        float fric_alpha = std::sin(fric_w0) / (2.0f * fric_Q);
        float fric_a0 = 1.0f + fric_alpha;
        float fric_b0 = fric_alpha / fric_a0;           // = sin(w0)/(2*Q) / a0
        float fric_b1 = 0.0f;
        float fric_b2 = -fric_alpha / fric_a0;
        float fric_a1 = (-2.0f * std::cos(fric_w0)) / fric_a0;
        float fric_a2 = (1.0f - fric_alpha) / fric_a0;

        // --- Precompute body bandpass coefficients ---
        float body_w0 = 2.0f * static_cast<float>(M_PI) * body_freq / sr;
        float body_Q  = 1.5f;
        float body_alpha = std::sin(body_w0) / (2.0f * body_Q);
        float body_a0 = 1.0f + body_alpha;
        float body_b0 = body_alpha / body_a0;
        float body_b1 = 0.0f;
        float body_b2 = -body_alpha / body_a0;
        float body_a1 = (-2.0f * std::cos(body_w0)) / body_a0;
        float body_a2 = (1.0f - body_alpha) / body_a0;

        // --- Sympathetic coupling: compute per-voice injection (once per block) ---
        float symp_inject[SYNTH_MAX_VOICES] = {};
        if (sympathetic > 0.0f) {
            for (int i = 0; i < SYNTH_MAX_VOICES; ++i) {
                auto& vi = vm_.voices[i];
                if (!vi.active) continue;
                for (int j = i + 1; j < SYNTH_MAX_VOICES; ++j) {
                    auto& vj = vm_.voices[j];
                    if (!vj.active) continue;

                    float fi = pitch_to_freq(vi.pitch_semitones);
                    float fj = pitch_to_freq(vj.pitch_semitones);
                    float ratio = (fj > fi) ? fj / fi : fi / fj;

                    int nearest = static_cast<int>(std::round(ratio));
                    if (nearest >= 1 && nearest <= 6) {
                        float deviation = std::fabs(ratio - static_cast<float>(nearest));
                        if (deviation < 0.05f) {
                            float coupling = sympathetic * (1.0f - deviation / 0.05f);

                            // Read current energy from each delay line
                            int ri = (vi.ext.write_pos - 1) & DELAY_MASK;
                            int rj = (vj.ext.write_pos - 1) & DELAY_MASK;
                            float energy_i = vi.ext.delay_line[ri];
                            float energy_j = vj.ext.delay_line[rj];

                            // Spread injection over the block
                            float inv_n = 1.0f / static_cast<float>(N);
                            symp_inject[i] += energy_j * coupling * inv_n;
                            symp_inject[j] += energy_i * coupling * inv_n;
                        }
                    }
                }
            }
        }

        // --- Per-voice, per-sample processing ---
        for (int vi = 0; vi < SYNTH_MAX_VOICES; ++vi) {
            auto& v = vm_.voices[vi];
            if (!v.active) continue;

            // Track fader/pan: per-channel L/R gains, constant across the block.
            float gl, gr; vm_.voice_amp(v, gl, gr);

            // Loop filter coefficient: g=0 bright, g→1 dark
            float g = std::clamp(damping, 0.0f, 0.999f);

            float symp_per_sample = symp_inject[vi];

            for (int i = 0; i < N; ++i) {
                float env_val = v.env.next();
                if (v.env.is_off()) { v.active = false; break; }

                // Pitch → delay length (use direct interpolated pitch —
                // the waveguide handles pitch changes physically)
                float p = VoiceManager<WaveguideExt>::interpolated_pitch(v, i, N);
                float f0 = pitch_to_freq(p);
                float target_delay = sr / f0 - 0.5f;  // subtract allpass group delay
                target_delay = std::clamp(target_delay, 2.0f, static_cast<float>(MAX_DELAY - 2));

                // Slew-limit delay change (prevents clicks)
                float max_rate = v.ext.current_delay * 0.0005f;
                max_rate = std::max(max_rate, 0.001f);
                float delta = target_delay - v.ext.current_delay;
                delta = std::clamp(delta, -max_rate, max_rate);
                v.ext.current_delay += delta;

                // Read from delay line: integer part + allpass fractional
                int int_delay = static_cast<int>(v.ext.current_delay);
                float frac = v.ext.current_delay - static_cast<float>(int_delay);

                // Thiran first-order allpass coefficient
                float a = (1.0f - frac) / (1.0f + frac);

                int read_pos = (v.ext.write_pos - int_delay - 1) & DELAY_MASK;
                float raw = v.ext.delay_line[read_pos];

                // First-order allpass interpolation
                float ap_out = a * raw + v.ext.prev_raw - a * v.ext.ap_z1;
                v.ext.ap_z1 = ap_out;
                v.ext.prev_raw = raw;

                // Loop filter: 1-pole lowpass
                float lpf_out = (1.0f - g) * ap_out + g * v.ext.lpf_z1;
                v.ext.lpf_z1 = lpf_out;

                // Friction noise injection
                if (friction_amount > 0.0f) {
                    float drive = std::fabs(v.pitch_delta) * friction_resp;
                    float fric_gain = friction_amount * std::tanh(drive);

                    if (fric_gain > 0.001f) {
                        float noise = xorshift_float(v.ext.rng);

                        // Biquad bandpass: y = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2
                        float bp_out = fric_b0 * noise + fric_b1 * v.ext.fric_x1
                                     + fric_b2 * v.ext.fric_x2
                                     - fric_a1 * v.ext.fric_y1
                                     - fric_a2 * v.ext.fric_y2;
                        v.ext.fric_x2 = v.ext.fric_x1;
                        v.ext.fric_x1 = noise;
                        v.ext.fric_y2 = v.ext.fric_y1;
                        v.ext.fric_y1 = bp_out;

                        lpf_out += bp_out * fric_gain;
                    }
                }

                // Write loop output to delay line (ASSIGN, not accumulate —
                // the buffer is larger than the delay so += would accumulate
                // stale data from ~4096 samples ago, causing runaway gain).
                v.ext.delay_line[v.ext.write_pos] = lpf_out + symp_per_sample;

                // Excitation (noise burst with pick-position comb)
                if (v.ext.exc_remaining > 0) {
                    float noise = xorshift_float(v.ext.rng);
                    float exc_raw;
                    // Per-note override wins; -1 falls back to the block param.
                    int et = v.ext.excitation_mode >= 0 ? v.ext.excitation_mode
                                                        : excitation_type;
                    if (et == 0) {
                        // Pluck: filtered noise burst
                        exc_raw = noise * v.ext.exc_level * v.velocity;
                    } else {
                        // Strike: decaying impulse
                        exc_raw = v.ext.exc_level * v.velocity;
                        v.ext.exc_level *= 0.995f;
                    }

                    // Pick-position comb applied to excitation signal:
                    // exc_out = exc_raw - exc_raw_delayed_by_pick_offset
                    // Creates notches at harmonics of 1/pick_position.
                    int pick_offset = std::clamp(static_cast<int>(
                        std::round(v.ext.current_delay * pick_position)),
                        1, EXC_BUF_SIZE - 1);
                    int read_idx = (v.ext.exc_buf_pos - pick_offset) & EXC_BUF_MASK;
                    float exc = exc_raw - v.ext.exc_buf[read_idx];

                    v.ext.exc_buf[v.ext.exc_buf_pos] = exc_raw;
                    v.ext.exc_buf_pos = (v.ext.exc_buf_pos + 1) & EXC_BUF_MASK;

                    // Inject combed excitation into delay line (additive on
                    // top of the loop output already assigned above).
                    v.ext.delay_line[v.ext.write_pos] += exc;

                    v.ext.exc_remaining--;
                }

                v.ext.write_pos = (v.ext.write_pos + 1) & DELAY_MASK;

                // Body resonance bandpass on output
                float output;
                if (body_reso > 0.001f) {
                    float bp = body_b0 * lpf_out + body_b1 * v.ext.body_x1
                             + body_b2 * v.ext.body_x2
                             - body_a1 * v.ext.body_y1
                             - body_a2 * v.ext.body_y2;
                    v.ext.body_x2 = v.ext.body_x1;
                    v.ext.body_x1 = lpf_out;
                    v.ext.body_y2 = v.ext.body_y1;
                    v.ext.body_y1 = bp;
                    output = lpf_out * (1.0f - body_reso) + bp * body_reso;
                } else {
                    output = lpf_out;
                }

                // Accumulate
                float sample = output * env_val * v.velocity * gain;
                L[i] += sample * gl;
                R[i] += sample * gr;
            }
        }

        // Soft clip
        for (int i = 0; i < N; ++i) {
            L[i] = std::tanh(L[i]);
            if (out->right) R[i] = std::tanh(R[i]);
        }
    }

private:
    VoiceManager<WaveguideExt> vm_;

    float attack_     = 0.005f;
    float decay_      = 0.1f;
    float sustain_    = 0.9f;
    float release_    = 0.3f;
    float pluck_width_ = 2.0f;
};

REGISTER_PLUGIN(WaveguideStringPlugin);
REGISTER_PLUGIN_DYNAMIC(WaveguideStringPlugin);
