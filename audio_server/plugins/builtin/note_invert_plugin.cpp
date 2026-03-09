// note_invert_plugin.cpp
// Inverts (mirrors) MIDI note pitches around a configurable center note.
//
// For center note C, input pitch P becomes 2*C - P, clamped to 0-127.
// E.g. with center=60: 59->61, 61->59, 48->72, 72->48, 60->60.

#include "plugin_api.h"
#include <algorithm>
#include <vector>

class NoteInvertPlugin final : public Plugin {
public:

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.note_invert";
        d.display_name = "Note Invert";
        d.category     = "Event";
        d.doc =
            "Mirrors MIDI note pitches around a chosen center note. "
            "Notes above the center are reflected below and vice versa. "
            "Useful for creating contrary-motion counterpoint or "
            "twelve-tone inversions.";
        d.author  = "builtin";
        d.version = 1;

        d.ports = {
            { "events_in", "Events In",
              "MIDI events to invert",
              PluginPortType::Event, PortRole::Input },
            { "events_out", "Events Out",
              "Inverted MIDI events",
              PluginPortType::Event, PortRole::Output },
        };

        d.config_params = {
            { "center_note", "Center Note",
              "MIDI note number for the axis of inversion (0-127). "
              "Default: 60 (middle C).",
              ConfigType::Integer, "60" },
        };

        return d;
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "center_note") {
            center_note_ = std::max(0, std::min(127, std::stoi(value)));
        }
    }

    void note_on(int channel, int pitch, int velocity) override {
        pending_note_ons_.push_back({channel, invert(pitch), velocity});
    }

    void note_off(int channel, int pitch) override {
        pending_note_offs_.push_back({channel, invert(pitch)});
    }

    void all_notes_off(int channel) override {
        pending_all_notes_off_ = true;
        all_notes_off_channel_ = channel;
    }

    void note_tune(int channel, int note, float semitones) override {
        pending_note_tunes_.push_back({channel, invert(note), semitones});
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* ev_out = buffers.events.get("events_out");
        if (!ev_out || !ev_out->output_events) return;

        auto& out = *ev_out->output_events;
        out.clear();

        // All notes off
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
            pending_all_notes_off_ = false;
        }

        // Note offs
        for (const auto& [ch, pitch] : pending_note_offs_) {
            out.push_back({0, static_cast<uint8_t>(0x80 | (ch & 0x0F)),
                          static_cast<uint8_t>(pitch), 0,
                          static_cast<uint8_t>(ch)});
        }
        pending_note_offs_.clear();

        // Note ons
        for (const auto& [ch, pitch, vel] : pending_note_ons_) {
            out.push_back({0, static_cast<uint8_t>(0x90 | (ch & 0x0F)),
                          static_cast<uint8_t>(pitch),
                          static_cast<uint8_t>(vel),
                          static_cast<uint8_t>(ch)});
        }
        pending_note_ons_.clear();

        // Note tunes — forward with inverted note number
        // (NoteTune events are not standard MIDI; handled by the engine)
        pending_note_tunes_.clear();
    }

private:
    int center_note_ = 60;

    struct NoteOn  { int ch, pitch, vel; };
    struct NoteOff { int ch, pitch; };
    struct NoteTune { int ch, note; float semitones; };

    std::vector<NoteOn>   pending_note_ons_;
    std::vector<NoteOff>  pending_note_offs_;
    std::vector<NoteTune> pending_note_tunes_;
    bool pending_all_notes_off_ = false;
    int  all_notes_off_channel_ = -1;

    int invert(int pitch) const {
        int inv = 2 * center_note_ - pitch;
        return std::max(0, std::min(127, inv));
    }
};

REGISTER_PLUGIN(NoteInvertPlugin);
REGISTER_PLUGIN_DYNAMIC(NoteInvertPlugin);
