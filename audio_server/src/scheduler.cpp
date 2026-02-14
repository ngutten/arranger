// scheduler.cpp
#include "scheduler.h"
#include "nlohmann/json.hpp"
#include <algorithm>
#include <stdexcept>

using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Schedule::from_json
// ---------------------------------------------------------------------------

std::unique_ptr<Schedule> Schedule::from_json(const std::string& j_str, std::string& err) {
    json j;
    try { j = json::parse(j_str); }
    catch (const std::exception& e) {
        err = std::string("Schedule JSON parse error: ") + e.what();
        return nullptr;
    }

    auto sched = std::make_unique<Schedule>();

    for (auto& je : j.value("events", json::array())) {
        SchedEvent evt;
        evt.beat     = je.value("beat", 0.0);
        evt.channel  = static_cast<uint8_t>(je.value("channel", 0));
        evt.pitch    = static_cast<uint8_t>(je.value("pitch", 0));
        evt.velocity = static_cast<uint8_t>(je.value("velocity", 0));
        evt.value    = je.value("value", 0.0f);
        evt.node_id  = je.value("node_id", "");

        // Setup events from the Python client have beat = -1 (program/volume
        // changes that must fire before any note-ons). Clamp to 0.0 so they
        // fire at the start of the arrangement rather than being skipped.
        if (evt.beat < 0.0) evt.beat = 0.0;

        std::string type_str = je.value("type", "note_on");
        if      (type_str == "note_on")  evt.type = EventType::NoteOn;
        else if (type_str == "note_off") evt.type = EventType::NoteOff;
        else if (type_str == "program")  evt.type = EventType::Program;
        else if (type_str == "volume")   evt.type = EventType::Volume;
        else if (type_str == "bend")     evt.type = EventType::Bend;
        else if (type_str == "control")  evt.type = EventType::Control;
        else {
            err = "Unknown event type: " + type_str;
            return nullptr;
        }

        sched->events_.push_back(evt);
        if (evt.beat > sched->total_length_) sched->total_length_ = evt.beat;
    }

    // Sort: beat ascending, then priority (off/bend/prog before on)
    auto priority = [](EventType t) -> int {
        switch (t) {
            case EventType::NoteOff: return 0;
            case EventType::Bend:    return 1;
            case EventType::Program: return 1;
            case EventType::Volume:  return 1;
            case EventType::Control: return 1;
            case EventType::NoteOn:  return 2;
            default:                 return 1;
        }
    };

    std::stable_sort(sched->events_.begin(), sched->events_.end(),
        [&](const SchedEvent& a, const SchedEvent& b) {
            if (a.beat != b.beat) return a.beat < b.beat;
            return priority(a.type) < priority(b.type);
        });

    return sched;
}

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------

Schedule* Dispatcher::swap_schedule(Schedule* next) {
    // Store in pending_ atomically. Returns the old pending (may be null).
    Schedule* old = pending_.exchange(next, std::memory_order_acq_rel);
    return old;
}

bool Dispatcher::check_pending() {
    Schedule* pending = pending_.exchange(nullptr, std::memory_order_acq_rel);
    if (!pending) return false;

    Schedule* old = current_;
    current_ = pending;
    idx_     = 0;
    reindex(0.0);  // reindex from current beat (seek will have been sent separately)
    delete old;
    return true;
}

void Dispatcher::dispatch(double start_beat, double end_beat, Graph* graph) {
    if (!current_ || !graph) return;
    const auto& evts = current_->events();

    while (idx_ < evts.size()) {
        const auto& e = evts[idx_];
        // Setup events (beat < 0) were rewritten to beat 0.0 by Schedule::from_json.
        // Any that slipped through with negative beat are forwarded at beat 0.
        double effective_beat = e.beat < 0.0 ? 0.0 : e.beat;
        if (effective_beat >= end_beat) break;
        if (effective_beat >= start_beat) {
            Node* node = graph->find_node(e.node_id);
            if (node) {
                switch (e.type) {
                    case EventType::NoteOn:
                        node->note_on(e.channel, e.pitch, e.velocity);
                        break;
                    case EventType::NoteOff:
                        node->note_off(e.channel, e.pitch);
                        break;
                    case EventType::Program:
                        node->program_change(e.channel, e.velocity /*bank*/, e.pitch /*prog*/);
                        break;
                    case EventType::Volume:
                        node->channel_volume(e.channel, e.pitch);
                        break;
                    case EventType::Bend:
                        node->pitch_bend(e.channel, e.pitch | (e.velocity << 7));
                        break;
                    case EventType::Control:
                        node->push_control(e.beat, e.value);
                        break;
                }
            }
        }
        idx_++;
    }
}

void Dispatcher::seek(double beat) {
    // Also handle any all_notes_off externally before calling seek.
    reindex(beat);
}

double Dispatcher::arrangement_length() const {
    return current_ ? current_->total_length_beats() : 0.0;
}

void Dispatcher::reindex(double beat) {
    if (!current_) { idx_ = 0; return; }
    const auto& evts = current_->events();
    // Skip setup events (beat < 0), then binary-search for beat position
    idx_ = 0;
    for (size_t i = 0; i < evts.size(); ++i) {
        if (evts[i].beat >= beat) { idx_ = i; return; }
    }
    idx_ = evts.size();
}
