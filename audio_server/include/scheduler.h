#pragma once
// scheduler.h
// Converts a sorted list of beat-timed events into per-block node dispatches.
//
// The schedule is built on the main thread from JSON and swapped atomically,
// exactly mirroring the Python engine's build_schedule / _pending_schedule
// pattern. The audio thread calls dispatch() once per block.

#include <vector>
#include <string>
#include <atomic>
#include <memory>
#include "graph.h"

// ---------------------------------------------------------------------------
// Event types (mirror protocol.h / Python engine constants)
// ---------------------------------------------------------------------------

enum class EventType : uint8_t {
    NoteOn      = 0,
    NoteOff     = 1,
    Program     = 2,   // pitch=program, velocity=bank
    Volume      = 3,   // pitch=volume
    Bend        = 4,   // pitch=14-bit bend value (8192=center)
    Control     = 5,   // value=normalized 0..1, delivered to control_source node
};

struct SchedEvent {
    double    beat;
    EventType type;
    uint8_t   channel;
    uint8_t   pitch;
    uint8_t   velocity;
    float     value;       // used by Control events
    std::string node_id;   // target node
};

// ---------------------------------------------------------------------------
// Schedule
// ---------------------------------------------------------------------------

class Schedule {
public:
    // Build from JSON EventBatch. Returns nullptr on parse error.
    static std::unique_ptr<Schedule> from_json(
        const std::string& json,
        std::string& error_out
    );

    // Sorted event list (by beat, then type priority: off < bend/prog < on).
    const std::vector<SchedEvent>& events() const { return events_; }

    double total_length_beats() const { return total_length_; }

private:
    std::vector<SchedEvent> events_;
    double total_length_ = 0.0;
};

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------
// Lives on the audio thread. Holds a reference to the current schedule
// and dispatches events to nodes in the graph.

class Dispatcher {
public:
    // Swap in a new schedule (called from main thread; atomic pointer swap).
    // The old schedule is returned so the caller can delete it off the audio thread.
    Schedule* swap_schedule(Schedule* next);

    // Called at the start of each block to check for a pending schedule swap.
    // Returns true if a swap occurred.
    bool check_pending();

    // Dispatch all events in [start_beat, end_beat) to graph nodes.
    void dispatch(double start_beat, double end_beat, Graph* graph);

    // Seek: reindex to the given beat position.
    void seek(double beat);

    // Total arrangement length from current schedule (0 if no schedule).
    double arrangement_length() const;

private:
    std::atomic<Schedule*>   pending_  { nullptr };
    Schedule*                current_  { nullptr };
    size_t                   idx_      { 0 };

    void reindex(double beat);
};
