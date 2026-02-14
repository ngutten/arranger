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
    void stop();
    void seek(double beat);
    void set_loop(double start, double end);   // call with (0,0) to disable
    void disable_loop();
    void set_bpm(float bpm) { bpm_ = bpm; }

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

    // -----------------------------------------------------------------------
    // Offline render (main thread — blocking, uses same graph+schedule)
    // -----------------------------------------------------------------------

    // Returns interleaved stereo float32 PCM.
    // Renders until arrangement_length + tail_seconds.
    std::vector<float> render_offline(float tail_seconds = 1.0f);

    // Convenience: returns WAV file bytes.
    std::vector<uint8_t> render_offline_wav(float tail_seconds = 1.0f);

    float sample_rate() const { return cfg_.sample_rate; }
    int   block_size()  const { return cfg_.block_size;  }

    float bpm() const { return bpm_; }

private:
    AudioEngineConfig cfg_;
    void* stream_ = nullptr;  // PaStream* — opaque to avoid PortAudio header in API

    // Graph — swapped atomically. Audio thread reads active_graph_.
    std::unique_ptr<Graph>       pending_graph_;
    std::atomic<Graph*>          active_graph_  { nullptr };
    std::unique_ptr<Graph>       owned_graph_;     // current graph (audio thread reads this)
    std::unique_ptr<Graph>       retiring_graph_;  // previous graph, freed on next set_graph
    std::mutex                   graph_mutex_;     // protects owned_graph_ / retiring_graph_

    // Dispatcher — lives on audio thread
    Dispatcher dispatcher_;

    // Transport state (written by audio thread, readable from main)
    std::atomic<double> current_beat_ { 0.0 };
    std::atomic<bool>   playing_      { false };

    // Loop state (written by main, read by audio — via command queue)
    struct LoopState { double start = 0; double end = 0; bool enabled = false; };
    std::atomic<LoopState*>  pending_loop_ { nullptr };
    LoopState*               active_loop_  { nullptr };

    // Simple command queue (same pattern as Python engine)
    enum class Cmd { Play, Stop, Seek, AllNotesOff };
    struct CmdEntry { Cmd cmd; double arg = 0.0; };
    std::vector<CmdEntry>    cmd_queue_;
    std::mutex               cmd_mutex_;

    float bpm_ = 120.0f;  // set from graph JSON or set_bpm(); read by callback + render

    void send_cmd(Cmd c, double arg = 0.0);

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
