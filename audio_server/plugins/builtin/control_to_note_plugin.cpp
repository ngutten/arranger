// control_to_note_plugin.cpp
// Converts a Control stream into MIDI note_on / note_off events.
//
// Two trigger modes:
//
//   Threshold mode (fixed_duration = 0):
//     note_on  fires when the control value RISES above `threshold`
//     note_off fires when the control value FALLS below `threshold`
//     Optional hysteresis: separate on/off thresholds to prevent chatter.
//
//   Fixed-duration mode (fixed_duration > 0):
//     note_on fires on a rising edge above threshold.
//     note_off fires `fixed_duration` beats later regardless of the signal.
//     Retriggerable: a second rising edge before the note ends retriggers it.
//
// Parameters:
//   note           — MIDI note number [0, 127]        default 60 (C4)
//   velocity       — MIDI velocity [1, 127]           default 100
//   channel        — MIDI channel [0, 15]             default 0
//   threshold      — rising-edge trigger level [0,1]  default 0.5
//   hysteresis     — fall-off offset below threshold  [0, 0.5] default 0.05
//                    note_off fires at (threshold - hysteresis)
//   fixed_duration — note length in beats (0 = use fall threshold)
//                    [0, 16]  default 0
//
// The output is an Event port so it plugs directly into any Event input
// (synths, arpeggiators, etc.).

#include "plugin_api.h"
#include <algorithm>
#include <cmath>

class ControlToNotePlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_to_note";
        d.display_name = "Control → Note";
        d.category     = "Utility";
        d.doc          = "Fires MIDI note_on/off when a Control stream crosses a "
                         "threshold. Two modes: threshold (note held while signal "
                         "is above threshold) and fixed-duration (note_off fires "
                         "after a set number of beats).";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "control_in", "Control In", "Control signal to monitor",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },

            { "events_out", "Events Out", "MIDI note events",
              PluginPortType::Event, PortRole::Output },

            { "note", "Note", "MIDI note number to trigger",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 60.0f, 0.0f, 127.0f, 1.0f },

            { "velocity", "Velocity", "MIDI velocity",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 100.0f, 1.0f, 127.0f, 1.0f },

            { "channel", "Channel", "MIDI channel (0-based)",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 0.0f, 0.0f, 15.0f, 1.0f },

            { "threshold", "Threshold",
              "Control value that triggers note_on on a rising edge",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },

            { "hysteresis", "Hysteresis",
              "How far below threshold the signal must fall before note_off fires "
              "(threshold mode only). 0 = no hysteresis.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.05f, 0.0f, 0.5f },

            { "fixed_duration", "Fixed Duration (beats)",
              "If > 0, note_off fires this many beats after note_on regardless of "
              "signal level. 0 = use fall threshold instead.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 16.0f },
        };

        return d;
    }

    void activate(float /*sample_rate*/, int /*max_block_size*/) override {
        note_active_  = false;
        prev_value_   = 0.0f;
        note_on_beat_ = -1e9;
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* ev_port = buffers.events.get("events_out");
        if (!ev_port || !ev_port->output_events) return;

        float ctl_val   = param(buffers, "control_in",      0.0f);
        int   note      = std::clamp(static_cast<int>(param(buffers, "note",     60.f)),  0, 127);
        int   vel       = std::clamp(static_cast<int>(param(buffers, "velocity", 100.f)), 1, 127);
        int   channel   = std::clamp(static_cast<int>(param(buffers, "channel",  0.f)),   0, 15);
        float threshold = param(buffers, "threshold",      0.5f);
        float hyst      = std::clamp(param(buffers, "hysteresis", 0.05f), 0.f, threshold);
        float fixed_dur = param(buffers, "fixed_duration", 0.0f);

        auto& out_events = *ev_port->output_events;

        // Fixed-duration mode: check if the active note's duration has expired.
        // We emit note_off at sample 0 of the first block past the deadline.
        if (note_active_ && fixed_dur > 0.0f) {
            double note_end_beat = note_on_beat_ + fixed_dur;
            if (ctx.beat_position >= note_end_beat) {
                out_events.push_back(make_note_off(0, channel, note));
                note_active_ = false;
            }
        }

        // Evaluate threshold crossing (once per block, control-rate)
        float on_thresh  = threshold;
        float off_thresh = threshold - hyst;

        if (!note_active_) {
            // Rising edge: was below, now above
            if (prev_value_ < on_thresh && ctl_val >= on_thresh) {
                out_events.push_back(make_note_on(0, channel, note, vel));
                note_active_  = true;
                note_on_beat_ = ctx.beat_position;
            }
        } else {
            if (fixed_dur <= 0.0f) {
                // Threshold mode: falling edge
                if (ctl_val < off_thresh) {
                    out_events.push_back(make_note_off(0, channel, note));
                    note_active_ = false;
                }
            } else {
                // Fixed-duration mode: retrigger on a second rising edge
                if (prev_value_ < on_thresh && ctl_val >= on_thresh) {
                    out_events.push_back(make_note_off(0, channel, note));
                    out_events.push_back(make_note_on(0, channel, note, vel));
                    note_on_beat_ = ctx.beat_position;
                }
            }
        }

        prev_value_ = ctl_val;
    }

    void deactivate() override {
        note_active_ = false;
    }

private:
    bool   note_active_  = false;
    float  prev_value_   = 0.0f;
    double note_on_beat_ = -1e9;

    static float param(PluginBuffers& b, const char* id, float fallback) {
        auto* p = b.control.get(id);
        return p ? p->value : fallback;
    }

    static MidiEvent make_note_on(int frame, int channel, int note, int vel) {
        MidiEvent e;
        e.frame   = frame;
        e.status  = static_cast<uint8_t>(0x90 | (channel & 0x0F));
        e.data1   = static_cast<uint8_t>(note);
        e.data2   = static_cast<uint8_t>(vel);
        e.channel = static_cast<uint8_t>(channel);
        return e;
    }

    static MidiEvent make_note_off(int frame, int channel, int note) {
        MidiEvent e;
        e.frame   = frame;
        e.status  = static_cast<uint8_t>(0x80 | (channel & 0x0F));
        e.data1   = static_cast<uint8_t>(note);
        e.data2   = 0;
        e.channel = static_cast<uint8_t>(channel);
        return e;
    }
};

REGISTER_PLUGIN(ControlToNotePlugin);
REGISTER_PLUGIN_DYNAMIC(ControlToNotePlugin);

std::unique_ptr<Plugin> make_control_to_note_plugin() {
    return std::make_unique<ControlToNotePlugin>();
}
