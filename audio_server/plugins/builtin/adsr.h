#pragma once
// adsr.h — Shared ADSR envelope for all synthesizer plugins.
//
// Extracted from ddsp_plugin.cpp. Uses exponential decay/release curves
// (multiplied by current level each sample) for natural-sounding envelopes.

#include <algorithm>
#include <cmath>

struct ADSREnvelope {
    enum class Stage { Attack, Decay, Sustain, Release, Off };
    Stage stage = Stage::Off;
    float level = 0.0f;

    float attack_rate   = 0.0f;   // per sample
    float decay_rate    = 0.0f;
    float sustain_level = 0.8f;
    float release_rate  = 0.0f;

    void trigger(float sample_rate, float attack_s, float decay_s,
                 float sustain, float release_s) {
        sustain_level = sustain;
        attack_rate   = (attack_s  > 0.001f) ? 1.0f / (attack_s  * sample_rate) : 1.0f;
        decay_rate    = (decay_s   > 0.001f) ? 1.0f / (decay_s   * sample_rate) : 1.0f;
        release_rate  = (release_s > 0.001f) ? 1.0f / (release_s * sample_rate) : 1.0f;
        stage = Stage::Attack;
        // don't reset level — allows retrigger without click
    }

    void release() {
        if (stage != Stage::Off)
            stage = Stage::Release;
    }

    float next() {
        switch (stage) {
        case Stage::Attack:
            level += attack_rate;
            if (level >= 1.0f) { level = 1.0f; stage = Stage::Decay; }
            break;
        case Stage::Decay:
            level -= decay_rate * (level - sustain_level);
            if (level <= sustain_level + 0.001f) {
                level = sustain_level;
                stage = Stage::Sustain;
            }
            break;
        case Stage::Sustain:
            break;
        case Stage::Release:
            level -= release_rate * level;
            if (level < 0.001f) { level = 0.0f; stage = Stage::Off; }
            break;
        case Stage::Off:
            level = 0.0f;
            break;
        }
        return level;
    }

    bool is_off() const { return stage == Stage::Off; }
};
