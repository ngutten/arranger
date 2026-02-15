// note_gate_plugin.cpp
// Port of NoteGateNode to the Plugin API.
// Converts MIDI note events into a control signal.
//
// Modes:
//   0 — Gate:      1.0 while any in-band note is held, 0.0 otherwise
//   1 — Velocity:  normalized velocity of most recent note-on in band
//   2 — Pitch:     position of most recent note within [pitch_lo, pitch_hi] → [0,1]
//   3 — NoteCount: simultaneous held notes / band width, clamped to [0,1]

#include "plugin_api.h"
#include <algorithm>
#include <unordered_map>

class NoteGatePlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.note_gate";
        d.display_name = "Note Gate";
        d.category     = "Utility";
        d.doc          = "Converts MIDI note events into a control signal. "
                         "Modes: Gate, Velocity, Pitch, NoteCount.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "event_in", "MIDI In", "Note events to convert",
              PluginPortType::Event, PortRole::Input },
            { "control_out", "Control Out", "Output control signal",
              PluginPortType::Control, PortRole::Output,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "mode", "Mode", "Output mode",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Categorical, 0.0f, 0.0f, 3.0f, 1.0f,
              {"Gate", "Velocity", "Pitch", "NoteCount"} },
            { "pitch_lo", "Pitch Low", "Lower bound of pitch band",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 0.0f, 0.0f, 127.0f, 1.0f },
            { "pitch_hi", "Pitch High", "Upper bound of pitch band",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Integer, 127.0f, 0.0f, 127.0f, 1.0f },
        };

        return d;
    }

    void note_on(int channel, int pitch, int velocity) override {
        if (!in_band(pitch)) return;
        active_[channel * 128 + pitch] = velocity;
        recompute();
    }

    void note_off(int channel, int pitch) override {
        if (!in_band(pitch)) return;
        active_.erase(channel * 128 + pitch);
        recompute();
    }

    void all_notes_off(int channel) override {
        if (channel == -1) {
            active_.clear();
        } else {
            for (auto it = active_.begin(); it != active_.end(); ) {
                if (it->first / 128 == channel) it = active_.erase(it);
                else ++it;
            }
        }
        recompute();
    }

    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        // Read control inputs (allows modulating mode/band from other nodes)
        auto* mode_ctl = buffers.control.get("mode");
        auto* lo_ctl   = buffers.control.get("pitch_lo");
        auto* hi_ctl   = buffers.control.get("pitch_hi");

        if (mode_ctl) mode_     = std::max(0, std::min(3, static_cast<int>(mode_ctl->value)));
        if (lo_ctl)   pitch_lo_ = std::max(0, std::min(127, static_cast<int>(lo_ctl->value)));
        if (hi_ctl)   pitch_hi_ = std::max(0, std::min(127, static_cast<int>(hi_ctl->value)));

        // Recompute in case band changed
        recompute();

        auto* out = buffers.control.get("control_out");
        if (out) out->value = current_value_;
    }

private:
    int   mode_     = 0;
    int   pitch_lo_ = 0;
    int   pitch_hi_ = 127;
    float current_value_ = 0.0f;

    // key = channel*128 + pitch, value = velocity
    std::unordered_map<int, int> active_;

    bool in_band(int pitch) const {
        return pitch >= pitch_lo_ && pitch <= pitch_hi_;
    }

    void recompute() {
        if (active_.empty()) { current_value_ = 0.0f; return; }
        switch (mode_) {
            case 0:  // Gate
                current_value_ = 1.0f;
                break;
            case 1: { // Velocity
                int max_vel = 0;
                for (auto& [k, v] : active_) max_vel = std::max(max_vel, v);
                current_value_ = max_vel / 127.0f;
                break;
            }
            case 2: { // Pitch
                int bw = pitch_hi_ - pitch_lo_;
                if (bw <= 0) { current_value_ = 0.0f; break; }
                int highest = -1;
                for (auto& [k, v] : active_) {
                    int p = k % 128;
                    if (p > highest) highest = p;
                }
                current_value_ = std::clamp(
                    static_cast<float>(highest - pitch_lo_) / bw, 0.0f, 1.0f);
                break;
            }
            case 3: { // NoteCount
                int bw = pitch_hi_ - pitch_lo_ + 1;
                if (bw <= 0) { current_value_ = 0.0f; break; }
                current_value_ = std::min(1.0f,
                    static_cast<float>(active_.size()) / bw);
                break;
            }
            default:
                current_value_ = 0.0f;
        }
    }
};

REGISTER_PLUGIN(NoteGatePlugin);
REGISTER_PLUGIN_DYNAMIC(NoteGatePlugin);

std::unique_ptr<Plugin> make_note_gate_plugin() { return std::make_unique<NoteGatePlugin>(); }
