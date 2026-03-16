#pragma once
// synth_common.h — Shared infrastructure for pitch-delta-responsive synthesizers.
//
// Header-only (inline/template) since each plugin compiles as an independent
// MODULE library with no shared .cpp linking.
//
// Provides:
//   - SynthVoice<Ext>   : Voice struct with pitch tracking, ADSR, phase accumulator
//   - TanhMapping       : Parameter modulation via tanh(k_pitch*pitch + k_vel*vel + k_delta*delta)
//   - VoiceManager<Ext> : 32-voice polyphony with stealing, pitch-delta computation

#include "adsr.h"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstring>

// ===========================================================================
// Constants
// ===========================================================================

static constexpr int SYNTH_MAX_VOICES = 32;

// ===========================================================================
// SynthVoice — per-voice state with pitch-delta tracking
// ===========================================================================

template <typename VoiceExt>
struct SynthVoice {
    bool  active  = false;
    int   channel = 0;
    int   pitch   = 0;       // original MIDI note
    int   age     = 0;       // blocks since trigger (for voice stealing)
    float velocity = 0.0f;   // normalized [0,1]

    // Pitch tracking (in semitones)
    float pitch_semitones  = 0.0f;   // current pitch = MIDI note + note_tune offset
    float target_pitch     = 0.0f;   // from latest note_tune()
    float prev_target      = 0.0f;   // target_pitch at previous tune() call
    bool  target_changed   = false;  // true when tune() updates target this block
    float pitch_interp_start = 0.0f; // block start pitch (for per-sample interp)
    float pitch_interp_end   = 0.0f; // block end pitch

    // Pitch delta signal, normalized so an octave interval or
    // 12 semitones/sec slide rate maps to ~1.0.
    // Composed of two sources:
    //   pitch_delta_note: seeded at note-on from inter-note interval, decays
    //   pitch_delta_tune: computed from note_tune() glide rate, EMA smoothed
    float pitch_delta      = 0.0f;   // combined output (note + tune)
    float pitch_delta_note = 0.0f;   // inter-note component (decays)
    float pitch_delta_tune = 0.0f;   // glide component (EMA)

    // ADSR
    ADSREnvelope env;

    // Phase accumulator (shared by all synth types)
    double phase = 0.0;

    // Tremolo phase for pitch-delta oscillation
    double tremolo_phase = 0.0;

    // Plugin-specific extension
    VoiceExt ext;
};

// ===========================================================================
// TanhMapping — modulate a parameter by pitch, velocity, and pitch delta
// ===========================================================================
//
// P_eff = P_base + P_range * voicing * tanh(k_pitch*(pitch-69) + k_vel*vel + k_delta*delta)

struct TanhMapping {
    float k_pitch = 0.0f;
    float k_vel   = 0.0f;
    float k_delta = 0.0f;

    template <typename VoiceExt>
    inline float compute(const SynthVoice<VoiceExt>& v, float base, float range,
                         float voicing) const {
        float x = k_pitch * (v.pitch_semitones - 69.0f)
                + k_vel   * v.velocity
                + k_delta * v.pitch_delta;
        return base + range * voicing * std::tanh(x);
    }
};

// ===========================================================================
// Pitch utilities
// ===========================================================================

inline float pitch_to_freq(float semitones) {
    return 440.0f * std::pow(2.0f, (semitones - 69.0f) / 12.0f);
}

// ===========================================================================
// VoiceManager — allocation, stealing, pitch-delta computation
// ===========================================================================

template <typename VoiceExt>
class VoiceManager {
public:
    using Voice = SynthVoice<VoiceExt>;

    Voice voices[SYNTH_MAX_VOICES];

    float sample_rate   = 44100.0f;
    int   block_size    = 512;
    float delta_smooth  = 0.2f;   // EMA time constant / half-life in seconds

    // Per-channel memory: last note pitch for inter-note delta seeding.
    float last_note_pitch[16] = {};
    bool  has_last_note[16]   = {};

    // -----------------------------------------------------------------------
    // Lifecycle
    // -----------------------------------------------------------------------

    void init(float sr, int bs) {
        sample_rate = sr;
        block_size  = bs;
        for (auto& v : voices) v = Voice{};
        for (int i = 0; i < 16; ++i) {
            last_note_pitch[i] = 0.0f;
            has_last_note[i] = false;
        }
    }

    // -----------------------------------------------------------------------
    // Called at the start of each process block
    // -----------------------------------------------------------------------

    void begin_block(int bs) {
        block_size = bs;
        float block_dur = static_cast<float>(bs) / sample_rate;
        float ds = std::max(0.001f, delta_smooth);

        // EMA alpha for the glide (note_tune) component
        float tune_alpha = 1.0f - std::exp(-block_dur / ds);

        // Decay alpha for the inter-note component.
        // delta_smooth is the half-life: the note-onset delta decays by
        // 50% after delta_smooth seconds, sustaining audibly over the
        // note's attack and early sustain.
        float note_decay = std::exp(-block_dur * 0.693f / ds);

        for (auto& v : voices) {
            if (!v.active) continue;
            ++v.age;

            // Latch interpolation endpoints
            v.pitch_interp_start = v.pitch_semitones;
            v.pitch_interp_end   = v.target_pitch;

            // --- Glide component (note_tune events) ---
            if (v.target_changed) {
                // Compute rate normalized to 12 semitones/sec = 1.0
                float raw_rate = (v.target_pitch - v.prev_target) / block_dur;
                raw_rate /= 12.0f;
                v.pitch_delta_tune += tune_alpha * (raw_rate - v.pitch_delta_tune);
                v.prev_target = v.target_pitch;
                v.target_changed = false;
            } else {
                // Decay toward zero when no tune events arriving
                v.pitch_delta_tune += tune_alpha * (0.0f - v.pitch_delta_tune);
            }

            // --- Note-onset component (decays with half-life = delta_smooth) ---
            v.pitch_delta_note *= note_decay;

            // --- Combined output: whichever source is dominant ---
            // Use the larger absolute value so glides and note intervals
            // both contribute meaningfully.
            if (std::fabs(v.pitch_delta_tune) > std::fabs(v.pitch_delta_note))
                v.pitch_delta = v.pitch_delta_tune;
            else
                v.pitch_delta = v.pitch_delta_note;

            // Update current pitch
            v.pitch_semitones = v.target_pitch;
        }
    }

    // -----------------------------------------------------------------------
    // Per-sample interpolated pitch
    // -----------------------------------------------------------------------

    static inline float interpolated_pitch(const Voice& v, int sample_idx, int bs) {
        float t = static_cast<float>(sample_idx) / static_cast<float>(bs);
        return v.pitch_interp_start + t * (v.pitch_interp_end - v.pitch_interp_start);
    }

    // Per-sample pitch with overshoot and tremolo from pitch delta
    static inline float pitch_with_dynamics(
            Voice& v, int sample_idx, int bs,
            float sr, float overshoot, float tremolo, float delta_smooth) {
        float p = interpolated_pitch(v, sample_idx, bs);
        p += v.pitch_delta * overshoot;
        if (tremolo != 0.0f) {
            float trem_freq = 1.0f / std::max(0.01f, delta_smooth);
            p += v.pitch_delta * tremolo
               * static_cast<float>(std::sin(2.0 * M_PI * v.tremolo_phase));
            v.tremolo_phase += static_cast<double>(trem_freq) / sr;
            v.tremolo_phase -= std::floor(v.tremolo_phase);
        }
        return p;
    }

    // -----------------------------------------------------------------------
    // Note events
    // -----------------------------------------------------------------------

    Voice* trigger(int channel, int pitch, int velocity,
                   float attack, float decay, float sustain, float release) {
        Voice* v = find_free();
        if (!v) v = steal();
        if (!v) return nullptr;

        *v = Voice{};
        v->active   = true;
        v->channel  = channel;
        v->pitch    = pitch;
        v->velocity = velocity / 127.0f;

        float p = static_cast<float>(pitch);
        v->pitch_semitones    = p;
        v->target_pitch       = p;
        v->prev_target        = p;
        v->target_changed     = false;
        v->pitch_interp_start = p;
        v->pitch_interp_end   = p;

        // Seed pitch_delta_note from the melodic interval to the previous
        // note on this channel.  Normalized by 12 semitones (one octave)
        // so an octave jump produces delta ~= 1.0, matching the scale of
        // velocity [0,1].  Set directly (not through EMA) so the full
        // interval is audible at note onset, then decays with delta_smooth
        // as the half-life.
        int ch = channel & 0xF;
        if (has_last_note[ch]) {
            float interval = p - last_note_pitch[ch];
            v->pitch_delta_note = interval / 12.0f;
            v->pitch_delta      = v->pitch_delta_note;
        } else {
            v->pitch_delta_note = 0.0f;
            v->pitch_delta      = 0.0f;
        }
        v->pitch_delta_tune = 0.0f;
        last_note_pitch[ch] = p;
        has_last_note[ch] = true;

        v->env.trigger(sample_rate, attack, decay, sustain, release);

        return v;
    }

    void release_note(int channel, int pitch) {
        for (auto& v : voices) {
            if (v.active && v.channel == channel && v.pitch == pitch
                && v.env.stage != ADSREnvelope::Stage::Release
                && v.env.stage != ADSREnvelope::Stage::Off) {
                v.env.release();
                break;
            }
        }
    }

    void all_notes_off(int channel = -1) {
        for (auto& v : voices) {
            if (!v.active) continue;
            if (channel == -1 || v.channel == channel)
                v.env.release();
        }
    }

    void tune(int channel, int note, float semitones) {
        for (auto& v : voices) {
            if (v.active && v.pitch == note &&
                (channel == -1 || v.channel == channel)) {
                float new_target = static_cast<float>(note) + semitones;
                if (new_target != v.target_pitch) {
                    v.target_pitch = new_target;
                    v.target_changed = true;
                }
            }
        }
    }

private:
    Voice* find_free() {
        for (auto& v : voices)
            if (!v.active) return &v;
        return nullptr;
    }

    Voice* steal() {
        // Prefer Release > Decay > Sustain > Attack, break ties by oldest
        Voice* best = nullptr;
        int best_score = -1;

        for (auto& v : voices) {
            int score = 0;
            switch (v.env.stage) {
                case ADSREnvelope::Stage::Release: score = 400; break;
                case ADSREnvelope::Stage::Off:     score = 500; break;
                case ADSREnvelope::Stage::Decay:   score = 300; break;
                case ADSREnvelope::Stage::Sustain: score = 200; break;
                case ADSREnvelope::Stage::Attack:  score = 100; break;
            }
            score += v.age;  // older voices get higher score
            if (score > best_score) {
                best_score = score;
                best = &v;
            }
        }
        return best;
    }
};
