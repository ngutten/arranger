// audio_engine.cpp
#include "audio_engine.h"
#include "synth_node.h"
#include "nlohmann/json.hpp"

#include <portaudio.h>
#include <cstring>
#include <stdexcept>
#include <cassert>
#include <algorithm>

#ifndef AS_PLATFORM_WINDOWS
#include <unistd.h>
#include <fcntl.h>
#endif

// WAV header writing (offline render)
#include <fstream>
#include <cstdint>

using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static void write_u16le(std::vector<uint8_t>& buf, uint16_t v) {
    buf.push_back(v & 0xFF);
    buf.push_back((v >> 8) & 0xFF);
}
static void write_u32le(std::vector<uint8_t>& buf, uint32_t v) {
    buf.push_back(v & 0xFF);
    buf.push_back((v >> 8) & 0xFF);
    buf.push_back((v >> 16) & 0xFF);
    buf.push_back((v >> 24) & 0xFF);
}

static std::vector<uint8_t> make_wav(const std::vector<float>& pcm_interleaved,
                                      int sample_rate, int channels)
{
    // Convert f32 → s16
    size_t n_samples = pcm_interleaved.size();
    std::vector<int16_t> s16(n_samples);
    for (size_t i = 0; i < n_samples; ++i) {
        float v = std::max(-1.0f, std::min(1.0f, pcm_interleaved[i]));
        s16[i] = static_cast<int16_t>(v * 32767.0f);
    }

    std::vector<uint8_t> wav;
    uint32_t data_bytes = static_cast<uint32_t>(n_samples * 2);
    uint32_t file_size  = 36 + data_bytes;

    wav.reserve(8 + file_size);
    // RIFF chunk
    wav.insert(wav.end(), {'R','I','F','F'});
    write_u32le(wav, file_size);
    wav.insert(wav.end(), {'W','A','V','E'});
    // fmt chunk
    wav.insert(wav.end(), {'f','m','t',' '});
    write_u32le(wav, 16);
    write_u16le(wav, 1);  // PCM
    write_u16le(wav, static_cast<uint16_t>(channels));
    write_u32le(wav, static_cast<uint32_t>(sample_rate));
    write_u32le(wav, static_cast<uint32_t>(sample_rate * channels * 2));
    write_u16le(wav, static_cast<uint16_t>(channels * 2));
    write_u16le(wav, 16);
    // data chunk
    wav.insert(wav.end(), {'d','a','t','a'});
    write_u32le(wav, data_bytes);
    const uint8_t* ptr = reinterpret_cast<const uint8_t*>(s16.data());
    wav.insert(wav.end(), ptr, ptr + data_bytes);
    return wav;
}

// ---------------------------------------------------------------------------
// AudioEngine
// ---------------------------------------------------------------------------

AudioEngine::AudioEngine(const AudioEngineConfig& cfg) : cfg_(cfg) {
    // Pa_Initialize probes all backends (ALSA, JACK, OSS, ...) and spews
    // warnings about missing/misconfigured devices to stderr. Suppress by
    // briefly redirecting stderr to /dev/null around the call.
#ifndef AS_PLATFORM_WINDOWS
    int saved_stderr = dup(STDERR_FILENO);
    int devnull = ::open("/dev/null", O_WRONLY);
    dup2(devnull, STDERR_FILENO);
    ::close(devnull);
#endif
    Pa_Initialize();
#ifndef AS_PLATFORM_WINDOWS
    dup2(saved_stderr, STDERR_FILENO);
    ::close(saved_stderr);
#endif
}

AudioEngine::~AudioEngine() {
    close();
    Pa_Terminate();
}

std::string AudioEngine::open() {
    if (stream_) return {};  // already open

    PaStreamParameters out_params;
    out_params.device = cfg_.output_device == -1
        ? Pa_GetDefaultOutputDevice()
        : cfg_.output_device;
    if (out_params.device == paNoDevice)
        return "PortAudio: no output device found";

    out_params.channelCount              = 2;
    out_params.sampleFormat              = paFloat32;
    out_params.suggestedLatency          =
        Pa_GetDeviceInfo(out_params.device)->defaultLowOutputLatency;
    out_params.hostApiSpecificStreamInfo = nullptr;

    PaError err = Pa_OpenStream(
        reinterpret_cast<PaStream**>(&stream_),
        nullptr,
        &out_params,
        cfg_.sample_rate,
        cfg_.block_size,
        paClipOff,
        &AudioEngine::pa_callback,
        this
    );
    if (err != paNoError) {
        stream_ = nullptr;
        return std::string("PortAudio open error: ") + Pa_GetErrorText(err);
    }

    Pa_StartStream(static_cast<PaStream*>(stream_));
    return {};
}

void AudioEngine::close() {
    stop();
    if (stream_) {
        Pa_StopStream(static_cast<PaStream*>(stream_));
        Pa_CloseStream(static_cast<PaStream*>(stream_));
        stream_ = nullptr;
    }
    // Stop the audio thread from seeing either graph before we free them
    active_graph_.store(nullptr, std::memory_order_release);
    if (owned_graph_) {
        owned_graph_->deactivate();
        owned_graph_.reset();
    }
    if (retiring_graph_) {
        retiring_graph_->deactivate();
        retiring_graph_.reset();
    }
}

std::string AudioEngine::set_graph(const std::string& graph_json) {
    std::string err;
    auto g = Graph::from_json(graph_json, err);
    if (!g) return err;

    if (!g->activate(cfg_.sample_rate, cfg_.block_size))
        return "Graph activation failed";

    // Extract BPM from graph JSON if present
    try {
        auto j = nlohmann::json::parse(graph_json);
        if (j.contains("bpm")) bpm_ = j["bpm"].get<float>();
    } catch (...) {}

    {
        std::lock_guard<std::mutex> lk(graph_mutex_);

        // Retire the previous "old" graph now — by the time set_graph is called
        // again, at least one audio callback has completed and moved to the graph
        // that was current_ at that point.  This one-generation lag ensures the
        // audio thread is never inside a graph we're freeing.
        retiring_graph_.reset();

        // Move the currently-active graph to retiring; it will be freed on the
        // next set_graph call (safe because the audio thread will have moved to
        // owned_graph_ before then).
        retiring_graph_ = std::move(owned_graph_);

        owned_graph_ = std::move(g);
        active_graph_.store(owned_graph_.get(), std::memory_order_release);
    }
    return {};
}

std::string AudioEngine::set_schedule(const std::string& schedule_json) {
    std::string err;
    auto sched = Schedule::from_json(schedule_json, err);
    if (!sched) return err;

    // Apply immediately on the calling thread so render_offline() and
    // arrangement_length() work without needing the audio callback to run.
    // If the stream is active the audio thread will pick up check_pending()
    // on the next block — the double-apply is harmless.
    dispatcher_.swap_schedule(sched.release());
    dispatcher_.check_pending();
    return {};
}

void AudioEngine::play() {
    dispatcher_.check_pending();  // make sure latest schedule is active
    playing_.store(true, std::memory_order_relaxed);
    send_cmd(Cmd::Play);
}

void AudioEngine::stop() {
    send_cmd(Cmd::Stop);
}

void AudioEngine::seek(double beat) {
    send_cmd(Cmd::Seek, beat);
}

void AudioEngine::set_loop(double start, double end) {
    auto* ls = new LoopState{start, end, true};
    auto* old = pending_loop_.exchange(ls, std::memory_order_acq_rel);
    delete old;
}

void AudioEngine::disable_loop() {
    auto* ls = new LoopState{0, 0, false};
    auto* old = pending_loop_.exchange(ls, std::memory_order_acq_rel);
    delete old;
}

void AudioEngine::set_param(const std::string& nid, const std::string& param, float val) {
    Graph* g = active_graph_.load(std::memory_order_acquire);
    if (g) g->set_param(nid, param, val);
}

// ---------------------------------------------------------------------------
// Preview note injection
// ---------------------------------------------------------------------------

static TrackSourceNode* find_track_source(Graph* g, const std::string& node_id) {
    if (!g) return nullptr;
    if (!node_id.empty()) {
        return dynamic_cast<TrackSourceNode*>(g->find_node(node_id));
    }
    // Fallback: return the first track_source in eval order
    for (auto& nid : g->eval_order()) {
        auto* n = dynamic_cast<TrackSourceNode*>(g->find_node(nid));
        if (n) return n;
    }
    return nullptr;
}

void AudioEngine::preview_note_on(const std::string& node_id, int channel,
                                   int pitch, int velocity)
{
    Graph* g = active_graph_.load(std::memory_order_acquire);
    auto* src = find_track_source(g, node_id);
    if (src) src->preview_note_on(channel, pitch, velocity);
}

void AudioEngine::preview_note_off(const std::string& node_id, int channel, int pitch) {
    Graph* g = active_graph_.load(std::memory_order_acquire);
    auto* src = find_track_source(g, node_id);
    if (src) src->preview_note_off(channel, pitch);
}

void AudioEngine::preview_all_notes_off(const std::string& node_id) {
    Graph* g = active_graph_.load(std::memory_order_acquire);
    if (!node_id.empty()) {
        auto* src = dynamic_cast<TrackSourceNode*>(g ? g->find_node(node_id) : nullptr);
        if (src) src->preview_all_notes_off();
        return;
    }
    // Silence all track_source nodes
    if (!g) return;
    for (auto& nid : g->eval_order()) {
        auto* src = dynamic_cast<TrackSourceNode*>(g->find_node(nid));
        if (src) src->preview_all_notes_off();
    }
}

// ---------------------------------------------------------------------------
// Live node reconfiguration
// ---------------------------------------------------------------------------

std::string AudioEngine::set_node_config(const std::string& node_id,
                                          const std::string& config_json)
{
    Graph* g = active_graph_.load(std::memory_order_acquire);
    if (!g) return "no active graph";

    Node* node = g->find_node(node_id);
    if (!node) return "unknown node: " + node_id;

    nlohmann::json cfg;
    try { cfg = nlohmann::json::parse(config_json); }
    catch (const std::exception& e) { return std::string("config JSON error: ") + e.what(); }

#ifdef AS_ENABLE_SF2
    // FluidSynthNode: sf2_path reload
    if (auto* fs = dynamic_cast<FluidSynthNode*>(node)) {
        if (cfg.contains("sf2_path")) {
            // Reload is destructive — rebuild a new node and swap it in.
            // For now we delegate to a re-set_graph from the caller; here we
            // just report that sf2_path changes require set_graph. A proper
            // hot-reload can be added later when FluidSynthNode exposes a
            // reload() method.
            return "sf2_path changes require a set_graph call (hot-reload not yet implemented)";
        }
        return {};
    }
#endif

    // MixerNode: master_gain, channel_count
    if (auto* mx = dynamic_cast<MixerNode*>(node)) {
        if (cfg.contains("master_gain"))
            mx->set_param("master_gain", cfg["master_gain"].get<float>());
        // channel_count changes require a graph rebuild; flag as unsupported live
        if (cfg.contains("channel_count"))
            return "channel_count changes require a set_graph call";
        return {};
    }

#ifdef AS_ENABLE_LV2
    // LV2Node: named parameter updates
    if (auto* lv = dynamic_cast<LV2Node*>(node)) {
        for (auto& [key, val] : cfg.items()) {
            if (key == "lv2_uri") return "lv2_uri changes require a set_graph call";
            lv->set_param(key, val.get<float>());
        }
        return {};
    }
#endif

    return "node type does not support set_node_config";
}

void AudioEngine::send_cmd(Cmd c, double arg) {
    std::lock_guard<std::mutex> lk(cmd_mutex_);
    cmd_queue_.push_back({c, arg});
}

// ---------------------------------------------------------------------------
// PortAudio callback (audio thread)
// ---------------------------------------------------------------------------

int AudioEngine::pa_callback(const void* /*input*/, void* output,
                              unsigned long frames,
                              const PaStreamCallbackTimeInfo* /*time_info*/,
                              PaStreamCallbackFlags /*status_flags*/,
                              void* user_data)
{
    auto* self = static_cast<AudioEngine*>(user_data);
    float* out = static_cast<float*>(output);

    // De-interleave into L/R scratch — stack-allocated for lock-free path
    alignas(16) float L[MAX_BLOCK_SIZE];
    alignas(16) float R[MAX_BLOCK_SIZE];

    self->process_block(L, R, static_cast<int>(frames));

    // Interleave into PortAudio output buffer
    for (unsigned long i = 0; i < frames; ++i) {
        out[i*2    ] = L[i];
        out[i*2 + 1] = R[i];
    }
    return paContinue;
}

void AudioEngine::process_block(float* L, float* R, int frames) {
    // Process pending commands
    {
        std::lock_guard<std::mutex> lk(cmd_mutex_);
        for (auto& ce : cmd_queue_) {
            switch (ce.cmd) {
                case Cmd::Play:
                    playing_.store(true, std::memory_order_relaxed);
                    break;
                case Cmd::Stop: {
                    playing_.store(false, std::memory_order_relaxed);
                    Graph* g = active_graph_.load(std::memory_order_acquire);
                    if (g) {
                        // all notes off on all synth nodes
                        for (auto& nid : g->eval_order()) {
                            auto* n = g->find_node(nid);
                            if (n) n->all_notes_off(-1);
                        }
                    }
                    break;
                }
                case Cmd::Seek:
                    dispatcher_.seek(ce.arg);
                    current_beat_.store(ce.arg, std::memory_order_relaxed);
                    {
                        Graph* g = active_graph_.load(std::memory_order_acquire);
                        if (g) for (auto& nid : g->eval_order()) {
                            auto* n = g->find_node(nid);
                            if (n) n->all_notes_off(-1);
                        }
                    }
                    break;
                case Cmd::AllNotesOff: {
                    Graph* g = active_graph_.load(std::memory_order_acquire);
                    if (g) for (auto& nid : g->eval_order()) {
                        auto* n = g->find_node(nid);
                        if (n) n->all_notes_off(-1);
                    }
                    break;
                }
            }
        }
        cmd_queue_.clear();
    }

    // Check for pending loop state
    {
        LoopState* ls = pending_loop_.exchange(nullptr, std::memory_order_acq_rel);
        if (ls) {
            delete active_loop_;
            active_loop_ = ls;
        }
    }

    // Check for pending schedule swap
    dispatcher_.check_pending();

    Graph* graph = active_graph_.load(std::memory_order_acquire);

    if (!playing_.load(std::memory_order_relaxed) || !graph) {
        // Still process graph (for preview notes) but without advancing beat
        if (graph) {
            double beat = current_beat_.load(std::memory_order_relaxed);
            double bps  = bpm_ / 60.0 / cfg_.sample_rate;
            ProcessContext ctx { frames, cfg_.sample_rate, bpm_,
                                 beat, bps };
            graph->process(ctx);
            const float* gL = graph->output_L();
            const float* gR = graph->output_R();
            if (gL && gR) {
                std::memcpy(L, gL, frames * sizeof(float));
                std::memcpy(R, gR, frames * sizeof(float));
            } else {
                std::memset(L, 0, frames * sizeof(float));
                std::memset(R, 0, frames * sizeof(float));
            }
        } else {
            std::memset(L, 0, frames * sizeof(float));
            std::memset(R, 0, frames * sizeof(float));
        }
        return;
    }

    // We use a fixed BPM stored in bpm_, set from the graph JSON via set_graph().
    float bpm = bpm_;

    double beat_pos = current_beat_.load(std::memory_order_relaxed);
    double bps      = bpm / 60.0 / cfg_.sample_rate;  // beats per sample
    double end_beat = beat_pos + frames * bps;

    // Dispatch events to graph nodes
    dispatcher_.dispatch(beat_pos, end_beat, graph);

    // Process graph
    ProcessContext ctx { frames, cfg_.sample_rate, bpm, beat_pos, bps };
    graph->process(ctx);

    const float* gL = graph->output_L();
    const float* gR = graph->output_R();
    if (gL && gR) {
        std::memcpy(L, gL, frames * sizeof(float));
        std::memcpy(R, gR, frames * sizeof(float));
    } else {
        std::memset(L, 0, frames * sizeof(float));
        std::memset(R, 0, frames * sizeof(float));
    }

    // Advance beat
    current_beat_.store(end_beat, std::memory_order_relaxed);

    // Loop / end-of-arrangement
    double arr_len = dispatcher_.arrangement_length();
    if (active_loop_ && active_loop_->enabled) {
        if (end_beat >= active_loop_->end) {
            dispatcher_.seek(active_loop_->start);
            current_beat_.store(active_loop_->start, std::memory_order_relaxed);
        }
    } else if (arr_len > 0 && end_beat >= arr_len) {
        playing_.store(false, std::memory_order_relaxed);
        if (graph) for (auto& nid : graph->eval_order()) {
            auto* n = graph->find_node(nid);
            if (n) n->all_notes_off(-1);
        }
        current_beat_.store(0.0, std::memory_order_relaxed);
    }
}

// ---------------------------------------------------------------------------
// Offline render
// ---------------------------------------------------------------------------

std::vector<float> AudioEngine::render_offline(float tail_seconds) {
    // Grab current graph and build a fresh schedule-driven render.
    // This runs on the main thread; no PortAudio stream needed.

    Graph* graph = active_graph_.load(std::memory_order_acquire);
    if (!graph) return {};

    float bpm     = bpm_;
    double length = dispatcher_.arrangement_length();
    if (length <= 0.0) return {};

    double total_seconds = length * 60.0 / bpm + tail_seconds;
    int    total_frames  = static_cast<int>(total_seconds * cfg_.sample_rate);
    int    block         = cfg_.block_size;

    std::vector<float> output;
    output.reserve(total_frames * 2);

    // We need a fresh Dispatcher pointed at a copy of the schedule.
    // For now, use the current dispatcher but reset its position.
    // (A proper offline render would clone the graph + schedule; this is
    //  sufficient for the prototype since we're not playing in parallel.)
    dispatcher_.seek(0.0);

    double beat_pos = 0.0;
    double bps      = bpm / 60.0 / cfg_.sample_rate;
    int    frames_done = 0;

    while (frames_done < total_frames) {
        int n = std::min(block, total_frames - frames_done);
        double end_beat = beat_pos + n * bps;

        dispatcher_.dispatch(beat_pos, end_beat, graph);

        ProcessContext ctx { n, cfg_.sample_rate, bpm, beat_pos, bps };
        graph->process(ctx);

        const float* gL = graph->output_L();
        const float* gR = graph->output_R();
        if (gL && gR) {
            for (int i = 0; i < n; ++i) {
                output.push_back(gL[i]);
                output.push_back(gR[i]);
            }
        } else {
            output.insert(output.end(), n * 2, 0.0f);
        }

        beat_pos = end_beat;
        frames_done += n;
    }

    return output;
}

std::vector<uint8_t> AudioEngine::render_offline_wav(float tail_seconds) {
    auto pcm = render_offline(tail_seconds);
    if (pcm.empty()) return {};
    return make_wav(pcm, static_cast<int>(cfg_.sample_rate), 2);
}
