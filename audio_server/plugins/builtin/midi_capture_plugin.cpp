// midi_capture_plugin.cpp
//
// Records the incoming MIDI event stream to a standard type-0 MIDI file.
// Useful for inspecting note timings, velocities, and channel assignments
// produced by other plugins (e.g. the Shepard Tone arpeggiator).
//
// Usage
// -----
//   1. Set the output path via the "output_path" config param.
//   2. Play back.  Events are buffered in memory (audio thread).
//   3. On transport stop the buffer is flushed to disk (main thread).
//
// The file is (over)written on every stop, so each playback run produces a
// clean capture.  If the path is empty nothing is written.
//
// API requirements
// ----------------
//   Requires PluginProcessContext::transport_stopped (to know when to flush)
//   and Plugin::on_transport_stop() (main-thread callback safe for file I/O).
//   Both are defined in the updated plugin_api.h.
//
// MIDI file notes
// ---------------
//   Writes a single-track (type 0) MIDI file.  Tempo is encoded as a Set
//   Tempo meta-event at tick 0 so downstream tools show correct BPM.
//   Ticks per beat (PPQ) = 960.

#include "plugin_api.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// Minimal MIDI file writer (type 0, single track)
// ---------------------------------------------------------------------------

static constexpr int PPQ = 960;   // ticks per beat — high resolution, still compact

// Write a big-endian multi-byte integer into a byte vector.
static void push_be(std::vector<uint8_t>& v, uint32_t val, int bytes) {
    for (int s = (bytes - 1) * 8; s >= 0; s -= 8)
        v.push_back(static_cast<uint8_t>((val >> s) & 0xFF));
}

// Write a MIDI variable-length quantity.
static void push_vlq(std::vector<uint8_t>& v, uint32_t val) {
    uint8_t buf[4];
    int n = 0;
    buf[n++] = val & 0x7F;
    while (val >>= 7) buf[n++] = 0x80 | (val & 0x7F);
    while (--n >= 0) v.push_back(buf[n]);
}

struct MidiTick {
    uint32_t tick;
    uint8_t  status, d1, d2;
};

struct TempoChange {
    uint32_t tick;
    float    bpm;
};

static bool write_midi_file(const std::string& path,
                            const std::vector<MidiTick>& events,
                            float bpm,
                            const std::vector<TempoChange>& tempo_changes = {}) {
    // Build a unified event list: tempo meta-events + MIDI events
    // We'll emit both as (tick, bytes) pairs, then sort and serialize.
    struct TrackEvent {
        uint32_t tick;
        std::vector<uint8_t> data;
    };
    std::vector<TrackEvent> all_events;

    // Tempo events
    if (tempo_changes.empty()) {
        // Single tempo at tick 0
        const uint32_t uspb = static_cast<uint32_t>(std::round(60000000.0f / bpm));
        TrackEvent te;
        te.tick = 0;
        te.data = {0xFF, 0x51, 0x03};
        te.data.push_back(static_cast<uint8_t>((uspb >> 16) & 0xFF));
        te.data.push_back(static_cast<uint8_t>((uspb >> 8) & 0xFF));
        te.data.push_back(static_cast<uint8_t>(uspb & 0xFF));
        all_events.push_back(std::move(te));
    } else {
        for (const auto& tc : tempo_changes) {
            const uint32_t uspb = static_cast<uint32_t>(std::round(60000000.0f / tc.bpm));
            TrackEvent te;
            te.tick = tc.tick;
            te.data = {0xFF, 0x51, 0x03};
            te.data.push_back(static_cast<uint8_t>((uspb >> 16) & 0xFF));
            te.data.push_back(static_cast<uint8_t>((uspb >> 8) & 0xFF));
            te.data.push_back(static_cast<uint8_t>(uspb & 0xFF));
            all_events.push_back(std::move(te));
        }
    }

    // MIDI events
    for (const auto& e : events) {
        TrackEvent te;
        te.tick = e.tick;
        te.data = {e.status, e.d1, e.d2};
        all_events.push_back(std::move(te));
    }

    // Sort by tick (stable sort to preserve insertion order at same tick)
    std::stable_sort(all_events.begin(), all_events.end(),
        [](const TrackEvent& a, const TrackEvent& b){ return a.tick < b.tick; });

    std::vector<uint8_t> track;
    uint32_t prev_tick = 0;
    for (const auto& e : all_events) {
        push_vlq(track, e.tick - prev_tick);
        prev_tick = e.tick;
        track.insert(track.end(), e.data.begin(), e.data.end());
    }

    // End-of-track meta-event.
    push_vlq(track, 0);
    track.push_back(0xFF); track.push_back(0x2F); track.push_back(0x00);

    // Assemble the file.
    std::vector<uint8_t> file;

    // MThd header chunk.
    file.insert(file.end(), {'M','T','h','d'});
    push_be(file, 6,    4);   // chunk length
    push_be(file, 0,    2);   // format 0 (single track)
    push_be(file, 1,    2);   // one track
    push_be(file, PPQ,  2);   // ticks per beat

    // MTrk track chunk.
    file.insert(file.end(), {'M','T','r','k'});
    push_be(file, static_cast<uint32_t>(track.size()), 4);
    file.insert(file.end(), track.begin(), track.end());

    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f.write(reinterpret_cast<const char*>(file.data()),
            static_cast<std::streamsize>(file.size()));
    return f.good();
}

// ---------------------------------------------------------------------------

class MidiCapturePlugin final : public Plugin {
public:

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.midi_capture";
        d.display_name = "MIDI Capture";
        d.category     = "Outputs";
        d.doc =
            "Records the incoming MIDI event stream to a standard type-0 MIDI "
            "file.  Set the output path, then play back.  The file is written "
            "when the transport stops.  Useful for inspecting note timings and "
            "velocities from generative plugins.";
        d.author  = "builtin";
        d.version = 1;

        d.ports = {
            { "events_in", "Events In",
              "MIDI event stream to capture.",
              PluginPortType::Event, PortRole::Input },
        };

        d.config_params = {
            { "output_path", "Output MIDI File",
              "Path to write the captured MIDI file on transport stop. "
              "Overwritten on each playback run.",
              ConfigType::FilePath,
              /* default */ "",
              /* file_filter */ "MIDI Files (*.mid *.midi);;All Files (*)",
              /* save_mode */ true },
        };

        return d;
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_   = sample_rate;
        bpm_           = 120.0f;
        events_.clear();
    }

    void deactivate() override {
        events_.clear();
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "output_path")
            output_path_ = value;
    }

    // -----------------------------------------------------------------------
    // Audio thread
    // -----------------------------------------------------------------------

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        // Track BPM changes for tempo meta-events
        if (ctx.bpm != last_bpm_) {
            double beat = ctx.beat_position;
            uint32_t tick = static_cast<uint32_t>(std::max(0.0, beat) * PPQ);
            tempo_changes_.push_back({tick, ctx.bpm});
            last_bpm_ = ctx.bpm;
        }
        bpm_ = ctx.bpm;

        auto* ev_in  = buffers.events.get("events_in");

        // On transport start, clear the previous capture.
        if (ctx.transport_started) {
            events_.clear();
            tempo_changes_.clear();
            last_bpm_ = 0.0f;  // force re-record of initial tempo
        }

        if (ev_in && ev_in->events) {
            for (const auto& e : *ev_in->events) {
                // Only capture note-on, note-off, aftertouch, CC, pitch bend —
                // the things that are meaningful in a MIDI file.
                const uint8_t type = e.status & 0xF0;
                const bool capturable =
                    type == 0x80 || type == 0x90 ||
                    type == 0xA0 || type == 0xB0 || type == 0xE0;

                if (capturable) {
                    // Convert sample offset to absolute beat, then to PPQ ticks.
                    const double beat =
                        ctx.beat_position + e.frame * ctx.beats_per_sample;
                    const uint32_t tick =
                        static_cast<uint32_t>(std::max(0.0, beat) * PPQ);
                    events_.push_back({ tick, e.status, e.data1, e.data2 });
                }
            }
        }
    }

    // -----------------------------------------------------------------------
    // Main thread — safe for file I/O
    // -----------------------------------------------------------------------

    void on_transport_stop() override {
        if (output_path_.empty()) return;
        if (events_.empty())      return;

        write_midi_file(output_path_, events_, bpm_, tempo_changes_);
        // Intentionally not clearing events_ here — they remain readable
        // until the next transport_started clears them, so repeated stops
        // (e.g. punch-in/out) don't lose data.
    }

private:
    std::string                output_path_;
    float                      sample_rate_ = 44100.0f;
    float                      bpm_         = 120.0f;
    float                      last_bpm_    = 0.0f;
    std::vector<MidiTick>      events_;
    std::vector<TempoChange>   tempo_changes_;
};

REGISTER_PLUGIN(MidiCapturePlugin);
REGISTER_PLUGIN_DYNAMIC(MidiCapturePlugin);

std::unique_ptr<Plugin> make_midi_capture_plugin() {
    return std::make_unique<MidiCapturePlugin>();
}
