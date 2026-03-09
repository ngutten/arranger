// event_gate_plugin.cpp
// Gates MIDI events based on a control input signal.
//
// When the control input is > 0.5, events pass through unchanged.
// When the control input drops to <= 0.5, the gate closes: new events
// are blocked and note-off is sent for any currently sounding notes.

#include "plugin_api.h"
#include <algorithm>
#include <vector>
#include <unordered_set>

class EventGatePlugin final : public Plugin {
public:

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.event_gate";
        d.display_name = "Event Gate";
        d.category     = "Event";
        d.doc =
            "Gates MIDI events using a control signal. "
            "When the control input exceeds 0.5 the gate is open and "
            "events pass through. When it drops to 0.5 or below, the "
            "gate closes, blocking new events and sending note-off "
            "for any currently sounding notes.";
        d.author  = "builtin";
        d.version = 1;

        d.ports = {
            { "events_in", "Events In",
              "MIDI events to gate",
              PluginPortType::Event, PortRole::Input },
            { "gate", "Gate",
              "Control signal: > 0.5 = open, <= 0.5 = closed",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },
            { "events_out", "Events Out",
              "Gated MIDI events",
              PluginPortType::Event, PortRole::Output },
        };

        return d;
    }

    void note_on(int channel, int pitch, int velocity) override {
        pending_note_ons_.push_back({channel, pitch, velocity});
    }

    void note_off(int channel, int pitch) override {
        pending_note_offs_.push_back({channel, pitch});
    }

    void all_notes_off(int channel) override {
        pending_all_notes_off_ = true;
        all_notes_off_channel_ = channel;
    }

    void note_tune(int channel, int note, float semitones) override {
        pending_note_tunes_.push_back({channel, note, semitones});
    }

    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        auto* ev_out = buffers.events.get("events_out");
        if (!ev_out || !ev_out->output_events) return;

        auto& out = *ev_out->output_events;
        out.clear();

        auto* gate_ctl = buffers.control.get("gate");
        bool open = gate_ctl && gate_ctl->value > 0.5f;

        // All notes off — always forward regardless of gate state
        if (pending_all_notes_off_) {
            int ch = all_notes_off_channel_;
            if (ch == -1) {
                for (int c = 0; c < 16; ++c) {
                    out.push_back({0, static_cast<uint8_t>(0xB0 | c), 123, 0,
                                   static_cast<uint8_t>(c)});
                }
            } else {
                out.push_back({0, static_cast<uint8_t>(0xB0 | (ch & 0x0F)), 123, 0,
                              static_cast<uint8_t>(ch)});
            }
            active_notes_.clear();
            pending_all_notes_off_ = false;
        }

        // Gate just closed — send note-off for all active notes
        if (!open && was_open_) {
            for (int key : active_notes_) {
                int ch    = key >> 8;
                int pitch = key & 0x7F;
                out.push_back({0, static_cast<uint8_t>(0x80 | (ch & 0x0F)),
                              static_cast<uint8_t>(pitch), 0,
                              static_cast<uint8_t>(ch)});
            }
            active_notes_.clear();
        }

        // Note offs — forward if the note is active (was let through)
        for (const auto& [ch, pitch] : pending_note_offs_) {
            int key = (ch << 8) | pitch;
            if (active_notes_.count(key)) {
                out.push_back({0, static_cast<uint8_t>(0x80 | (ch & 0x0F)),
                              static_cast<uint8_t>(pitch), 0,
                              static_cast<uint8_t>(ch)});
                active_notes_.erase(key);
            }
        }
        pending_note_offs_.clear();

        if (open) {
            // Note ons — pass through
            for (const auto& [ch, pitch, vel] : pending_note_ons_) {
                int key = (ch << 8) | pitch;
                active_notes_.insert(key);
                out.push_back({0, static_cast<uint8_t>(0x90 | (ch & 0x0F)),
                              static_cast<uint8_t>(pitch),
                              static_cast<uint8_t>(vel),
                              static_cast<uint8_t>(ch)});
            }

            // Note tunes — pass through
            for (const auto& [ch, note, semitones] : pending_note_tunes_) {
                (void)ch; (void)note; (void)semitones;
                // NoteTune events don't route through MidiEvent yet
            }
        }
        pending_note_ons_.clear();
        pending_note_tunes_.clear();

        was_open_ = open;
    }

private:
    struct NoteOn  { int ch, pitch, vel; };
    struct NoteOff { int ch, pitch; };
    struct NoteTune { int ch, note; float semitones; };

    std::vector<NoteOn>   pending_note_ons_;
    std::vector<NoteOff>  pending_note_offs_;
    std::vector<NoteTune> pending_note_tunes_;
    bool pending_all_notes_off_ = false;
    int  all_notes_off_channel_ = -1;

    std::unordered_set<int> active_notes_;
    bool was_open_ = false;
};

REGISTER_PLUGIN(EventGatePlugin);
REGISTER_PLUGIN_DYNAMIC(EventGatePlugin);
