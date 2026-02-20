#include "plugin_api.h"
#include <algorithm>
#include <vector>

struct ScheduledOff {
    double beat;
    int pitch;
    int channel;
};

struct Instance {
    int trigger_pitch;
    int trigger_vel;
    double start_beat;
    int next_note_idx;
    bool is_released = false;
    std::vector<ScheduledOff> pending_offs;
};

class PatternStamperPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.pattern_stamper";
        d.display_name = "Pattern Stamper";
        d.category     = "EventEffect";
        d.doc          = "Stamps a pattern onto incoming MIDI notes. Supports duration clipping.";
        
        d.ports = {
            { "pattern_in", "Template Pattern", "", PluginPortType::Pattern, PortRole::Input },
            { "events_in", "Trigger In", "", PluginPortType::Event, PortRole::Input },
            { "events_out", "Events Out", "", PluginPortType::Event, PortRole::Output },
            { "clip_to_event", "Clip to Event", 
              "If enabled, pattern playback stops immediately when the trigger note is released.",
              PluginPortType::Control, PortRole::Input, ControlHint::Toggle, 0.0f }
        };
        return d;
    }

    void activate(float, int) override { active_instances_.clear(); }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* ev_out_port = buffers.events.get("events_out");
        if (!ev_out_port || !ev_out_port->output_events) return;
        auto& out = *ev_out_port->output_events;

        const PatternData* pattern = nullptr;
        if (auto* p = buffers.patterns.get("pattern_in")) pattern = p->pattern;
        bool clip_to_event = buffers.control.get("clip_to_event")->value > 0.5f;

        // 1. Listen for Note Ons (Start Instance) and Note Offs (Release Instance)
        if (auto* ev_in = buffers.events.get("events_in")) {
            for (const auto& e : *ev_in->events) {
                uint8_t status = e.status & 0xF0;
                if (status == 0x90 && e.data2 > 0) {
                    Instance inst;
                    inst.trigger_pitch = e.data1;
                    inst.trigger_vel   = e.data2;
                    inst.start_beat    = ctx.beat_position;
                    inst.next_note_idx = 0;
                    active_instances_.push_back(inst);
                } else if (status == 0x80 || (status == 0x90 && e.data2 == 0)) {
                    for (auto& inst : active_instances_) {
                        if (inst.trigger_pitch == e.data1) inst.is_released = true;
                    }
                }
            }
        }

        if (!pattern || pattern->notes.empty()) return;

        // 2. Iterate Instances to trigger new notes and handle scheduled offs
        for (auto it = active_instances_.begin(); it != active_instances_.end(); ) {
            double local_beat = ctx.beat_position - it->start_beat;

            // Handle Note Offs for notes already triggered by this instance
            for (auto off_it = it->pending_offs.begin(); off_it != it->pending_offs.end(); ) {
                if (local_beat >= off_it->beat || (clip_to_event && it->is_released)) {
                    out.push_back(make_midi(0, 0x80, off_it->channel, off_it->pitch, 0));
                    off_it = it->pending_offs.erase(off_it);
                } else {
                    ++off_it;
                }
            }

            // Handle New Note Ons from pattern
            bool stop_pattern = clip_to_event && it->is_released;
            if (!stop_pattern) {
                while (it->next_note_idx < (int)pattern->notes.size()) {
                    const auto& pn = pattern->notes[it->next_note_idx];
                    if (local_beat >= pn.beat) {
                        int pitch = it->trigger_pitch + (pn.pitch - pattern->notes[0].pitch);
                        out.push_back(make_midi(0, 0x90, pn.channel, pitch, pn.velocity));
                        
                        it->pending_offs.push_back({pn.beat + pn.duration, pitch, pn.channel});
                        it->next_note_idx++;
                    } else {
                        break;
                    }
                }
            }

            // Cleanup: Instance is dead if trigger is released and all its notes have turned off
            if (it->is_released && it->pending_offs.empty()) {
                it = active_instances_.erase(it);
            } else if (!clip_to_event && it->next_note_idx >= (int)pattern->notes.size() && it->pending_offs.empty()) {
                it = active_instances_.erase(it);
            } else {
                ++it;
            }
        }
    }

private:
    std::vector<Instance> active_instances_;

    MidiEvent make_midi(int f, uint8_t s, uint8_t ch, uint8_t d1, uint8_t d2) {
        MidiEvent e{};
        e.frame = f; e.status = s | (ch & 0x0F); e.data1 = d1; e.data2 = d2;
        return e;
    }
};

REGISTER_PLUGIN(PatternStamperPlugin);
REGISTER_PLUGIN_DYNAMIC(PatternStamperPlugin);

std::unique_ptr<Plugin> make_stamper_plugin() {
    return std::make_unique<PatternStamperPlugin>();
}
