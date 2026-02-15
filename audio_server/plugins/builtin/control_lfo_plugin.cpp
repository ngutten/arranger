// control_lfo_plugin.cpp
// Generates a periodic waveform on a Control output port.
//
// Useful as a diagnostic: if ctrl_mon shows non-zero output with this plugin
// feeding it but not with note_gate, the control routing is fine and the bug
// is upstream of note_gate (event delivery / note_on not being called).
//
// Waveforms (shape param):
//   0 — Sine
//   1 — Square
//   2 — Triangle
//   3 — Sawtooth (rising ramp)
//
// Parameters:
//   frequency  — Hz  [0.01, 100]  default 1.0
//   amplitude  — [0, 1]           default 1.0
//   offset     — DC bias [0, 1]   default 0.5
//   shape      — 0..3 (categorical)
//   sync       — if 1, phase derived from beat_position (BPM-synced)
//                if 0, free-running (uses sample_rate accumulator)
//   beats      — when sync=1, LFO period in beats  [0.0625, 64]  default 4.0

#include "plugin_api.h"
#include <cmath>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

class ControlLfoPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_lfo";
        d.display_name = "Control LFO";
        d.category     = "Utility";
        d.doc          = "Generates a periodic waveform on a Control output port. "
                         "Useful for modulation and as a diagnostic to verify "
                         "that the control signal path is functional.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "control_out", "Control Out", "LFO output [0, 1]",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Meter, 0.0f, 0.0f, 1.0f },

            { "frequency", "Frequency", "LFO rate in Hz (free-running mode)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.01f, 100.0f },

            { "amplitude", "Amplitude", "Peak deviation from offset",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },

            { "offset", "Offset", "DC bias added to waveform",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },

            { "shape", "Shape", "Waveform shape",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 3.0f, 1.0f,
              {"Sine", "Square", "Triangle", "Sawtooth"} },

            { "sync", "Sync to BPM", "If 1, period set by 'beats' param, else free-running",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Toggle, 0.0f, 0.0f, 1.0f },

            { "beats", "Period (beats)", "LFO period in beats when sync=1",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 4.0f, 0.0625f, 64.0f },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;
        phase_ = 0.0;
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        // Read params (with fallback to defaults for unconnected ports)
        float freq  = param(buffers, "frequency", 1.0f);
        float amp   = param(buffers, "amplitude", 0.5f);
        float off   = param(buffers, "offset",    0.5f);
        int   shape = std::clamp(static_cast<int>(param(buffers, "shape", 0.0f)), 0, 3);
        bool  sync  = param(buffers, "sync",  0.0f) >= 0.5f;
        float beats = std::max(0.0625f, param(buffers, "beats", 4.0f));

        double phase;
        if (sync) {
            // Phase driven by beat_position — no state needed, always coherent
            phase = std::fmod(ctx.beat_position / beats, 1.0);
        } else {
            // Free-running: advance by freq/sample_rate per block
            // We evaluate once per block (control rate), so advance by
            // one block's worth of phase.
            double inc = (freq * ctx.block_size) / sample_rate_;
            phase_ = std::fmod(phase_ + inc, 1.0);
            phase = phase_;
        }

        float raw = evaluate(shape, static_cast<float>(phase));
        // raw is in [-1, 1]; map to [offset - amplitude, offset + amplitude]
        float value = std::clamp(off + amp * raw, 0.0f, 1.0f);

        auto* out = buffers.control.get("control_out");
        if (out) out->value = value;
    }

private:
    float  sample_rate_ = 44100.0f;
    double phase_       = 0.0;  // free-running phase accumulator [0, 1)

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    // Returns a value in [-1, 1] for phase in [0, 1)
    static float evaluate(int shape, float phase) {
        switch (shape) {
        case 0: // Sine
            return std::sin(2.0f * static_cast<float>(M_PI) * phase);
        case 1: // Square
            return phase < 0.5f ? 1.0f : -1.0f;
        case 2: // Triangle
            return (phase < 0.5f)
                ? (4.0f * phase - 1.0f)
                : (3.0f - 4.0f * phase);
        case 3: // Sawtooth (rising ramp)
            return 2.0f * phase - 1.0f;
        default:
            return 0.0f;
        }
    }
};

REGISTER_PLUGIN(ControlLfoPlugin);

std::unique_ptr<Plugin> make_control_lfo_plugin() { return std::make_unique<ControlLfoPlugin>(); }
