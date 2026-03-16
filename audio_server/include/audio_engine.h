#pragma once
// audio_engine.h
// Owns the PortAudio stream, the signal graph, and the event dispatcher.
//
// Threading model mirrors the Python engine exactly:
//   Main thread: set_graph(), set_schedule(), play/stop/seek, set_param()
//   Audio thread: callback only — reads graph + dispatcher, never allocates

#include <portaudio.h>
#include "graph.h"
#include "scheduler.h"
#include <memory>
#include <atomic>
#include <string>
#include <functional>
#include <mutex>
#include <vector>

struct AudioEngineConfig {
    float sample_rate  = 44100.0f;
    int   block_size   = 512;
    int   output_device = -1;    // -1 = default
};

// Piecewise-constant tempo map: holds BPM at each beat position.
// Between points, BPM is constant at the last point's value.
struct TempoPoint { double beat; float bpm; };

struct TempoMap {
    std::vector<TempoPoint> points;  // sorted by beat

    bool empty() const { return points.empty(); }

    // Return BPM at the given beat (last point at or before beat).
    // Falls back to fallback_bpm if before the first point or map is empty.
    float bpm_at(double beat, float fallback_bpm) const {
        if (points.empty()) return fallback_bpm;
        // Binary search for last point <= beat
        // std::upper_bound finds first point > beat, then we go back one
        auto it = std::upper_bound(points.begin(), points.end(), beat,
            [](double b, const TempoPoint& tp) { return b < tp.beat; });
        if (it == points.begin()) return fallback_bpm;
        --it;
        return it->bpm;
    }

    // Advance from start_beat by 'frames' samples, integrating the
    // piecewise-constant tempo curve.  Returns the end beat.
    double advance(double start_beat, int frames, float sample_rate,
                   float fallback_bpm) const {
        if (points.empty()) {
            double bps = fallback_bpm / 60.0 / sample_rate;
            return start_beat + frames * bps;
        }
        double beat = start_beat;
        int remaining = frames;
        while (remaining > 0) {
            float bpm = bpm_at(beat, fallback_bpm);
            double bps = bpm / 60.0 / sample_rate;
            // Find next tempo change after current beat
            auto it = std::upper_bound(points.begin(), points.end(), beat,
                [](double b, const TempoPoint& tp) { return b < tp.beat; });
            if (it != points.end()) {
                // How many samples until the next tempo point?
                double beats_to_next = it->beat - beat;
                int samples_to_next = static_cast<int>(beats_to_next / bps);
                if (samples_to_next <= 0) samples_to_next = 1;
                if (samples_to_next <= remaining) {
                    beat += samples_to_next * bps;
                    remaining -= samples_to_next;
                    continue;
                }
            }
            // Consume all remaining samples at current BPM
            beat += remaining * bps;
            remaining = 0;
        }
        return beat;
    }

    // Integrate tempo from beat 0 to target_beat and return seconds.
    double beat_to_seconds(double target_beat, float fallback_bpm) const {
        if (target_beat <= 0.0) return 0.0;
        if (points.empty()) return target_beat * 60.0 / fallback_bpm;
        double seconds = 0.0;
        double prev_beat = 0.0;
        float cur_bpm = fallback_bpm;
        for (auto& tp : points) {
            if (tp.beat >= target_beat) break;
            if (tp.beat > prev_beat) {
                seconds += (tp.beat - prev_beat) * 60.0 / cur_bpm;
                prev_beat = tp.beat;
            }
            cur_bpm = tp.bpm;
        }
        // Remaining segment
        seconds += (target_beat - prev_beat) * 60.0 / cur_bpm;
        return seconds;
    }
};

class AudioEngine {
public:
    explicit AudioEngine(const AudioEngineConfig& cfg = {});
    ~AudioEngine();

    // Not copyable or movable — owns a PortAudio stream.
    AudioEngine(const AudioEngine&) = delete;
    AudioEngine& operator=(const AudioEngine&) = delete;

    // -----------------------------------------------------------------------
    // Setup (main thread)
    // -----------------------------------------------------------------------

    // Open the PortAudio stream. Call before play().
    // Returns error string on failure, empty on success.
    std::string open();

    // Close stream and free resources.
    void close();

    bool is_open() const { return stream_ != nullptr; }

    // -----------------------------------------------------------------------
    // Graph management (main thread)
    // -----------------------------------------------------------------------

    // Parse graph JSON, build nodes, activate, swap in atomically.
    // Returns error string on failure.
    std::string set_graph(const std::string& graph_json);

    // -----------------------------------------------------------------------
    // Schedule management (main thread)
    // -----------------------------------------------------------------------

    std::string set_schedule(const std::string& schedule_json);

    // -----------------------------------------------------------------------
    // Transport (main thread — thread-safe)
    // -----------------------------------------------------------------------

    void play();
    void prerender();
    void stop();
    void seek(double beat);
    void set_loop(double start, double end);   // call with (0,0) to disable
    void disable_loop();
    void set_bpm(float bpm) { bpm_ = bpm; }
    void set_tempo_map(TempoMap map);
    float current_bpm() const;

    // Poll for pending transport callbacks (on_transport_stop etc.).
    // Call from any main-thread entry point; no-op if nothing is pending.
    void poll();

    double current_beat() const { return current_beat_.load(std::memory_order_relaxed); }
    bool   is_playing()   const { return playing_.load(std::memory_order_relaxed); }

    // -----------------------------------------------------------------------
    // Parameter control (main thread — forwarded to graph atomically)
    // -----------------------------------------------------------------------

    void set_param(const std::string& node_id, const std::string& param, float value);

    // -----------------------------------------------------------------------
    // Preview note injection (main thread — bypasses schedule/transport)
    // -----------------------------------------------------------------------
    // These route to TrackSourceNode::preview_note_on/off, which are
    // thread-safe and queue events for the next audio block.

    // node_id should be a track_source node (e.g. "track_abc").
    // If empty, routes to the first track_source found (convenience fallback).
    void preview_note_on (const std::string& node_id, int channel, int pitch, int velocity);
    void preview_note_off(const std::string& node_id, int channel, int pitch);
    // Silence all preview notes on the given source node (or all if node_id is empty).
    void preview_all_notes_off(const std::string& node_id);

    // -----------------------------------------------------------------------
    // Live node reconfiguration (main thread)
    // -----------------------------------------------------------------------
    // Update mutable config on an existing processor node without rebuilding
    // the graph. Supported keys by type — see protocol.h / API spec.
    // Returns error string on failure, empty on success.
    std::string set_node_config(const std::string& node_id, const std::string& config_json);

    /// Retrieve plugin graph/monitor data.  port_id is passed to Plugin::get_graph_data().
    /// Returns the JSON string returned by the plugin, or "[]" if node not found.
    std::string get_node_data(const std::string& node_id, const std::string& port_id);

    // -----------------------------------------------------------------------
    // Offline render (main thread — blocking, uses same graph+schedule)
    // -----------------------------------------------------------------------

    // Returns interleaved stereo float32 PCM.
    // Renders until arrangement_length + tail_seconds.
    // duration_beats overrides arrangement_length when > 0 (useful for graphs
    // with no scheduled events, e.g. an LFO with no note data).
    std::vector<float> render_offline(float tail_seconds = 1.0f,
                                      double duration_beats = 0.0);

    // Convenience: returns WAV file bytes.
    std::vector<uint8_t> render_offline_wav(float tail_seconds = 1.0f,
                                            double duration_beats = 0.0);

    float sample_rate() const { return cfg_.sample_rate; }
    int   block_size()  const { return cfg_.block_size;  }

    float bpm() const { return bpm_; }

private:
    AudioEngineConfig cfg_;
    void* stream_ = nullptr;  // PaStream* — opaque to avoid PortAudio header in API

    // Graph — swapped atomically. Audio thread reads active_graph_.
    //
    // Retirement protocol
    // -------------------
    // graph_epoch_ is incremented by the audio thread at the END of every
    // process_block() call (after graph->process() returns).  set_graph()
    // records the epoch before storing the new graph pointer, then waits
    // until the epoch advances before destroying the old graph.  This
    // guarantees the audio thread has completed at least one full block with
    // the new graph (and therefore is no longer inside any old-graph code)
    // before we free the retiring graph.
    std::unique_ptr<Graph>       pending_graph_;
    std::atomic<Graph*>          active_graph_  { nullptr };
    std::unique_ptr<Graph>       owned_graph_;
    std::unique_ptr<Graph>       retiring_graph_;
    std::mutex                   graph_mutex_;
    std::atomic<uint64_t>        graph_epoch_   { 0 };

    // Dispatcher — lives on audio thread
    Dispatcher dispatcher_;

    // Transport state (written by audio thread, readable from main)
    std::atomic<double> current_beat_ { 0.0 };
    std::atomic<bool>   playing_      { false };

    // Loop state (written by main, read by audio — via command queue)
    struct LoopState { double start = 0; double end = 0; bool enabled = false; };
    std::atomic<LoopState*>  pending_loop_ { nullptr };
    LoopState                active_loop_;  // audio-thread-only value copy

    // Simple command queue (same pattern as Python engine)
    enum class Cmd { Play, Stop, Seek, AllNotesOff, SetParam };
    struct CmdEntry {
        Cmd         cmd;
        double      arg = 0.0;
        std::string node_id;
        std::string param;
        float       value = 0.0f;
    };
    std::vector<CmdEntry>    cmd_queue_;
    std::mutex               cmd_mutex_;

    float bpm_ = 120.0f;  // set from graph JSON or set_bpm(); read by callback + render
    TempoMap tempo_map_;  // piecewise-constant tempo automation; empty = use bpm_

    // Transport edge detection — audio thread only, no atomics needed.
    bool  prev_playing_  = false;

    // Set by the audio thread when it transitions playing→stopped; cleared by
    // drain_transport_callbacks() on the main thread after calling
    // on_transport_stop() on all plugin adapters.
    std::atomic<bool> transport_stop_pending_ { false };

    // Set by set_graph() or play() to trigger setup event restoration on the
    // audio thread at the next block.
    std::atomic<bool> setup_restore_pending_ { false };

    // Call on_transport_stop() on all plugins in the current graph.
    // Must be called from the main thread only.
    void drain_transport_callbacks();

    // Heap-allocated scratch buffers for the PortAudio callback.
    // Allocated in open() to avoid the 32 KB stack overflow that two
    // MAX_BLOCK_SIZE float arrays would cause in an 8 KB ALSA callback stack.
    std::vector<float> scratch_L_;
    std::vector<float> scratch_R_;

    void send_cmd(Cmd c, double arg = 0.0);
    void send_param_cmd(const std::string& node_id, const std::string& param, float value);

    // PortAudio callback — static trampoline
    static int pa_callback(
        const void* input, void* output,
        unsigned long frames,
        const PaStreamCallbackTimeInfo* time_info,
        PaStreamCallbackFlags status_flags,
        void* user_data
    );

    void process_block(float* out_L, float* out_R, int frames);
};
