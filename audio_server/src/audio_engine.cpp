// audio_engine.cpp
#include "audio_engine.h"
#include "synth_node.h"
#include "plugin_adapter.h"
#include "nlohmann/json.hpp"

#include <portaudio.h>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <cassert>
#include <algorithm>

#ifndef AS_PLATFORM_WINDOWS
#include <unistd.h>
#include <fcntl.h>
#else
#include <windows.h>
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

    // Allocate scratch buffers on the heap so the audio callback doesn't
    // blow the 8 KB ALSA stack with 2×MAX_BLOCK_SIZE floats.
    // Use MAX_BLOCK_SIZE, not cfg_.block_size — PortAudio may deliver more
    // frames than the requested block size (it's a hint, not a guarantee).
    scratch_L_.resize(MAX_BLOCK_SIZE);
    scratch_R_.resize(MAX_BLOCK_SIZE);

    PaError start_err = Pa_StartStream(static_cast<PaStream*>(stream_));
    if (start_err != paNoError) {
        fprintf(stderr, "[AudioEngine] Pa_StartStream warning: %s\n",
                Pa_GetErrorText(start_err));
    }
    return {};
}

void AudioEngine::close() {
    stop();
    if (stream_) {
        PaError err;
        err = Pa_StopStream(static_cast<PaStream*>(stream_));
        if (err != paNoError)
            fprintf(stderr, "[AudioEngine] Pa_StopStream: %s\n", Pa_GetErrorText(err));
        err = Pa_CloseStream(static_cast<PaStream*>(stream_));
        if (err != paNoError)
            fprintf(stderr, "[AudioEngine] Pa_CloseStream: %s\n", Pa_GetErrorText(err));
        stream_ = nullptr;
    }
    // Free any pending loop state
    delete pending_loop_.exchange(nullptr, std::memory_order_relaxed);
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

        // --- Safe retirement ---
        // Record the epoch before we publish the new graph pointer.
        // Once the audio thread observes the new pointer it will increment
        // graph_epoch_ at the end of that block, so waiting for epoch+1
        // guarantees it is no longer executing any code from owned_graph_.
        uint64_t epoch_before = graph_epoch_.load(std::memory_order_acquire);

        retiring_graph_ = std::move(owned_graph_);
        owned_graph_    = std::move(g);
        active_graph_.store(owned_graph_.get(), std::memory_order_release);
        
        // Signal the audio thread to restore setup events at the next block
        setup_restore_pending_.store(true, std::memory_order_release);

        // Wait for the audio thread to complete at least one block with the
        // new graph.  The stream may not be open yet (first set_graph call),
        // in which case no callback will ever fire and we just free immediately.
        if (stream_) {
            // Spin with a short yield — typically resolves in < 1 callback
            // period (~10 ms).  No busy-wait: sched_yield lets the audio
            // thread run.  Timeout after 500 ms to avoid deadlock if the
            // stream has stalled.
            constexpr int MAX_ITER = 5000;
            for (int i = 0; i < MAX_ITER; ++i) {
                if (graph_epoch_.load(std::memory_order_acquire) > epoch_before)
                    break;
#ifndef AS_PLATFORM_WINDOWS
                usleep(100);   // 0.1 ms
#else
                Sleep(1);
#endif
            }
        }

        // Now safe to destroy the old graph: the audio thread has moved on.
        retiring_graph_.reset();
    }
    return {};
}

std::string AudioEngine::set_schedule(const std::string& schedule_json) {
    std::string err;
    auto sched = Schedule::from_json(schedule_json, err);
    if (!sched) return err;

    // Pre-deliver lyric syllables to singing plugins so they can pre-render
    // phonemes before playback starts.  NoteOn events with non-empty lyrics
    // are pushed in beat order to the target track_source node, which fans
    // them out to its downstream plugin nodes.
    //
    // Hold graph_mutex_ while traversing the graph so that a concurrent
    // set_graph() cannot destroy the graph (and its plugin nodes) while we
    // are still calling push_lyric()/on_schedule_loaded() on them.
    {
        std::lock_guard<std::mutex> lk(graph_mutex_);
        Graph* g = active_graph_.load(std::memory_order_acquire);
        if (g) {
            const auto& evts = sched->events();
            for (size_t i = 0; i < evts.size(); ++i) {
                const auto& evt = evts[i];
                if (evt.type == EventType::NoteOn) {
                    Node* n = g->find_node(evt.node_id);
                    if (!n) continue;

                    // Compute duration by scanning forward for the matching NoteOff
                    // (same node, channel, pitch).
                    double dur = 0.0;
                    for (size_t j = i + 1; j < evts.size(); ++j) {
                        const auto& e2 = evts[j];
                        if (e2.type == EventType::NoteOff &&
                            e2.node_id == evt.node_id &&
                            e2.channel == evt.channel &&
                            e2.pitch   == evt.pitch) {
                            dur = e2.beat - evt.beat;
                            break;
                        }
                    }
                    n->push_lyric(evt.beat, evt.lyric,
                                  static_cast<int>(evt.pitch), dur);
                }
            }
            // Signal end of lyric delivery so plugins can publish their sequences.
            for (const auto& nid : g->eval_order()) {
                if (Node* n = g->find_node(nid)) n->on_schedule_loaded();
            }
        }
    }

    dispatcher_.swap_schedule(sched.release());

    // When the stream is idle, apply immediately on the calling thread so
    // render_offline() and arrangement_length() work without needing the audio
    // callback to run.  When the stream is live, leave the swap pending: the
    // audio thread picks it up in process_block(), where it can both restore
    // channel state and release notes orphaned by the new schedule (e.g. a
    // track that was just muted) — neither of which is safe to do from here.
    if (!playing_.load(std::memory_order_relaxed)) {
        dispatcher_.check_pending();
    }
    return {};
}

void AudioEngine::play() {
    drain_transport_callbacks();  // flush any pending stop from previous run
    dispatcher_.check_pending();  // make sure latest schedule is active
    setup_restore_pending_.store(true, std::memory_order_release);  // restore channel state
    send_cmd(Cmd::Play);
}

void AudioEngine::prerender() {
    // Call prerender() on all nodes in the active graph.
    // Called from the main thread before play() — the audio thread is
    // guaranteed not to be running (or at least not processing this graph).
    std::lock_guard<std::mutex> lk(graph_mutex_);
    Graph* g = active_graph_.load(std::memory_order_acquire);
    if (!g) return;

    // Forward BPM to all plugins so they can compute frame durations.
    float bpm = bpm_;
    for (const auto& nid : g->eval_order()) {
        if (Node* n = g->find_node(nid))
            n->set_bpm(bpm);
    }

    for (const auto& nid : g->eval_order()) {
        if (Node* n = g->find_node(nid)) n->prerender();
    }
}

void AudioEngine::stop() {
    send_cmd(Cmd::Stop);
    // Give the audio thread one block to process the Stop command and set
    // transport_stop_pending_.  Typical block is ~10 ms; 50 ms is safe.
#ifndef AS_PLATFORM_WINDOWS
    usleep(50000);
#else
    Sleep(50);
#endif
    drain_transport_callbacks();
}

void AudioEngine::seek(double beat) {
    // Update the reported position immediately so get_position reflects the
    // seek even without a running audio stream.  The command queue entry is
    // still needed for the audio thread to reindex the dispatcher and send
    // all_notes_off.
    current_beat_.store(beat, std::memory_order_relaxed);
    send_cmd(Cmd::Seek, beat);
}

void AudioEngine::set_loop(double start, double end) {
    auto* ls = new LoopState{start, end, true};
    delete pending_loop_.exchange(ls, std::memory_order_acq_rel);
}

void AudioEngine::disable_loop() {
    auto* ls = new LoopState{0, 0, false};
    delete pending_loop_.exchange(ls, std::memory_order_acq_rel);
}

void AudioEngine::set_tempo_map(TempoMap map) {
    // Sort by beat just in case
    std::sort(map.points.begin(), map.points.end(),
        [](const TempoPoint& a, const TempoPoint& b) { return a.beat < b.beat; });
    tempo_map_ = std::move(map);
}

float AudioEngine::current_bpm() const {
    double beat = current_beat_.load(std::memory_order_relaxed);
    return tempo_map_.bpm_at(beat, bpm_);
}

MeterSnapshot AudioEngine::master_meter() const {
    Graph* g = active_graph_.load(std::memory_order_acquire);
    if (!g) return {};
    // The terminal mixer node always serialises with id "mixer".
    Node* n = g->find_node("mixer");
    return n ? n->read_meter() : MeterSnapshot{};
}

std::vector<MeterSnapshot> AudioEngine::channel_meters() const {
    std::vector<MeterSnapshot> out;
    Graph* g = active_graph_.load(std::memory_order_acquire);
    if (!g) return out;
    Node* n = g->find_node("mixer");
    if (!n) return out;
    int count = n->meter_channel_count();
    out.reserve(count);
    for (int i = 0; i < count; ++i) out.push_back(n->read_channel_meter(i));
    return out;
}

void AudioEngine::set_param(const std::string& nid, const std::string& param, float val) {
    // Enqueue so the audio thread applies this at the start of the next block,
    // avoiding a data race between this (IPC/main) thread and the audio thread
    // which may be simultaneously reading &pi.value inside lilv_instance_run().
    send_param_cmd(nid, param, val);
}

void AudioEngine::send_cmd(Cmd c, double arg) {
    std::lock_guard<std::mutex> lk(cmd_mutex_);
    cmd_queue_.push_back({c, arg});
}

void AudioEngine::send_param_cmd(const std::string& node_id,
                                  const std::string& param, float value) {
    std::lock_guard<std::mutex> lk(cmd_mutex_);
    CmdEntry e;
    e.cmd     = Cmd::SetParam;
    e.node_id = node_id;
    e.param   = param;
    e.value   = value;
    cmd_queue_.push_back(std::move(e));
}

void AudioEngine::poll() {
    drain_transport_callbacks();
}

void AudioEngine::drain_transport_callbacks() {
    if (!transport_stop_pending_.exchange(false, std::memory_order_acq_rel))
        return;    
    // Prefer owned_graph_ (main-thread view) over active_graph_ to avoid the
    // atomic load and stay on the same thread as graph_mutex_.
    std::lock_guard<std::mutex> lk(graph_mutex_);
    if (owned_graph_) { owned_graph_->notify_transport_stop(); }
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

    // PluginAdapterNode: route config changes through plugin->configure()
    if (auto* pa = dynamic_cast<PluginAdapterNode*>(node)) {
        for (auto& [key, val] : cfg.items()) {
            std::string str_val;
            if (val.is_string()) str_val = val.get<std::string>();
            else str_val = val.dump();
            pa->plugin()->configure(key, str_val);
        }
        return {};
    }

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
    // LV2Node: named parameter updates — route through the command queue to
    // avoid a data race between this (IPC) thread and the audio thread which
    // may simultaneously read &pi.value inside lilv_instance_run().
    if (auto* lv = dynamic_cast<LV2Node*>(node)) {
        for (auto& [key, val] : cfg.items()) {
            if (key == "lv2_uri") return "lv2_uri changes require a set_graph call";
            send_param_cmd(node_id, key, val.get<float>());
        }
        return {};
    }
#endif

    return "node type does not support set_node_config";
}

std::string AudioEngine::get_node_data(const std::string& node_id,
                                        const std::string& port_id) {
    // Access from main thread; use the mutex-guarded owned graph
    // rather than the atomic active_graph_ (which the audio thread reads).
    std::lock_guard<std::mutex> lk(graph_mutex_);
    Graph* g = owned_graph_.get();
    if (!g) return "[]";
    Node* node = g->find_node(node_id);
    if (!node) return "[]";
    auto* adapter = dynamic_cast<PluginAdapterNode*>(node);
    if (!adapter || !adapter->plugin()) return "[]";
    return adapter->plugin()->get_graph_data(port_id);
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

    // Clamp: PortAudio may deliver more frames than requested block size.
    if (frames > MAX_BLOCK_SIZE) frames = MAX_BLOCK_SIZE;

    // Use pre-allocated heap buffers — stack arrays of MAX_BLOCK_SIZE floats
    // would blow the 8 KB ALSA callback stack.
    float* L = self->scratch_L_.data();
    float* R = self->scratch_R_.data();

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
                    dispatcher_.clear_active();
                    break;
                }
                case Cmd::Seek:
                    dispatcher_.seek(ce.arg);
                    current_beat_.store(ce.arg, std::memory_order_relaxed);
                    {
                        Graph* g = active_graph_.load(std::memory_order_acquire);
                        if (g) for (auto& nid : g->eval_order()) {
                            auto* n = g->find_node(nid);
                            if (n) {
                                n->all_notes_off(-1);
                                n->on_seek(ce.arg);
                            }
                        }
                    }
                    break;
                case Cmd::AllNotesOff: {
                    Graph* g = active_graph_.load(std::memory_order_acquire);
                    if (g) for (auto& nid : g->eval_order()) {
                        auto* n = g->find_node(nid);
                        if (n) n->all_notes_off(-1);
                    }
                    dispatcher_.clear_active();
                    break;
                }
                case Cmd::SetParam: {
                    Graph* g = active_graph_.load(std::memory_order_acquire);
                    if (g) g->set_param(ce.node_id, ce.param, ce.value);
                    break;
                }
            }
        }
        cmd_queue_.clear();
    }

    // Check for pending loop state — copy by value to avoid use-after-free
    {
        LoopState* ls = pending_loop_.exchange(nullptr, std::memory_order_acq_rel);
        if (ls) {
            active_loop_ = *ls;
            delete ls;
        }
    }

    // Check for pending schedule swap
    bool schedule_swapped = dispatcher_.check_pending();

    Graph* graph = active_graph_.load(std::memory_order_acquire);

    // Restore channel setup state if:
    // 1. A schedule was just swapped during playback, OR
    // 2. setup_restore_pending_ is set (graph swap or play command)
    bool setup_needed = setup_restore_pending_.exchange(false, std::memory_order_acq_rel);
    if (schedule_swapped) {
        bool now_playing = playing_.load(std::memory_order_relaxed);
        if (now_playing) setup_needed = true;
    }
    
    if (setup_needed && graph) {
        const Schedule* sched = dispatcher_.current_schedule();
        if (sched) {
            double beat = current_beat_.load(std::memory_order_relaxed);
            auto setup_events = sched->get_setup_events_before(beat);
            dispatcher_.apply_setup_events(setup_events, graph, beat);
        }
    }

    // Release notes left hanging by the swap: if the new schedule has no future
    // note_off for a still-sounding note (its track was muted, its placement
    // deleted, etc.), the note would otherwise sustain forever.  Most audible on
    // sustained instruments (DDSP/wind); decaying samples masked it by fading out.
    if (schedule_swapped && graph && playing_.load(std::memory_order_relaxed)) {
        double beat = current_beat_.load(std::memory_order_relaxed);
        std::vector<Dispatcher::ActiveNote> orphans;
        dispatcher_.collect_orphaned_notes(beat, orphans);
        for (const auto& an : orphans) {
            if (Node* n = graph->find_node(an.node_id))
                n->note_off(an.channel, an.pitch);
        }
    }

    bool now_playing = playing_.load(std::memory_order_relaxed);

    // Detect transport edges.  prev_playing_ is audio-thread-only state.
    bool just_started = now_playing  && !prev_playing_;
    bool just_stopped = !now_playing &&  prev_playing_;
    prev_playing_     = now_playing;

    if (just_stopped)
        transport_stop_pending_.store(true, std::memory_order_release);

    if (!now_playing || !graph) {
        // Still process graph (for preview notes) but without advancing beat
        if (graph) {
            double beat = current_beat_.load(std::memory_order_relaxed);
            float cur_bpm = tempo_map_.bpm_at(beat, bpm_);
            double bps  = cur_bpm / 60.0 / cfg_.sample_rate;
            ProcessContext ctx { frames, cfg_.sample_rate, cur_bpm,
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
        graph_epoch_.fetch_add(1, std::memory_order_release);
        return;
    }

    // BPM: use tempo map if available, otherwise fixed bpm_
    double beat_pos = current_beat_.load(std::memory_order_relaxed);
    float bpm = tempo_map_.bpm_at(beat_pos, bpm_);
    double bps      = bpm / 60.0 / cfg_.sample_rate;  // beats per sample
    double end_beat = tempo_map_.advance(beat_pos, frames, cfg_.sample_rate, bpm_);

    // Dispatch events to graph nodes
    dispatcher_.dispatch(beat_pos, end_beat, graph);

    // Process graph
    ProcessContext ctx { frames, cfg_.sample_rate, bpm, beat_pos, bps,
                         /*is_playing=*/true, just_started, /*transport_stopped=*/false };
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
    if (active_loop_.enabled) {
        if (end_beat >= active_loop_.end) {
            dispatcher_.seek(active_loop_.start);
            current_beat_.store(active_loop_.start, std::memory_order_relaxed);
        }
    } else if (arr_len > 0 && end_beat >= arr_len) {
        playing_.store(false, std::memory_order_relaxed);
        transport_stop_pending_.store(true, std::memory_order_release);
        if (graph) for (auto& nid : graph->eval_order()) {
            auto* n = graph->find_node(nid);
            if (n) n->all_notes_off(-1);
        }
        dispatcher_.clear_active();
        current_beat_.store(0.0, std::memory_order_relaxed);
    }

    // Signal to set_graph() that this block is complete and the audio thread
    // is no longer touching the graph pointer that was active at block start.
    graph_epoch_.fetch_add(1, std::memory_order_release);
}

// ---------------------------------------------------------------------------
// Offline render
// ---------------------------------------------------------------------------

std::vector<float> AudioEngine::render_offline(float tail_seconds, double duration_beats) {
    // Grab current graph and build a fresh schedule-driven render.
    // This runs on the IPC thread.  The PortAudio callback also calls
    // graph->process() on the audio thread, so we must stop the stream for
    // the duration of this render to avoid a data race on the buffer pool.

    Graph* graph = active_graph_.load(std::memory_order_acquire);
    if (!graph) return {};

    float bpm     = bpm_;
    double length = (duration_beats > 0.0) ? duration_beats
                                            : dispatcher_.arrangement_length();
    if (length <= 0.0) return {};

    double total_seconds = tempo_map_.empty()
        ? (length * 60.0 / bpm + tail_seconds)
        : (tempo_map_.beat_to_seconds(length, bpm) + tail_seconds);
    int    total_frames  = static_cast<int>(total_seconds * cfg_.sample_rate);
    int    block         = cfg_.block_size;

    // Pause real-time stream so the callback doesn't race with our render.
    // Pa_StopStream waits for the current callback to finish before returning.
    bool stream_was_running = stream_ != nullptr;
    double saved_beat = current_beat_.load(std::memory_order_relaxed);
    if (stream_was_running) {
        PaError stop_err = Pa_StopStream(static_cast<PaStream*>(stream_));
        if (stop_err != paNoError)
            fprintf(stderr, "[AudioEngine] render_offline Pa_StopStream: %s\n",
                    Pa_GetErrorText(stop_err));
    }

    std::vector<float> output;
    output.reserve(total_frames * 2);

    dispatcher_.seek(0.0);

    double beat_pos = 0.0;
    int    frames_done = 0;

    while (frames_done < total_frames) {
        int n = std::min(block, total_frames - frames_done);
        float block_bpm = tempo_map_.bpm_at(beat_pos, bpm);
        double bps      = block_bpm / 60.0 / cfg_.sample_rate;
        double end_beat = tempo_map_.advance(beat_pos, n, cfg_.sample_rate, bpm);

        dispatcher_.dispatch(beat_pos, end_beat, graph);

        bool is_first = (frames_done == 0);
        bool is_last  = (frames_done + n >= total_frames);

        ProcessContext ctx { n, cfg_.sample_rate, block_bpm, beat_pos, bps,
                             /*is_playing=*/true,
                             /*transport_started=*/is_first,
                             /*transport_stopped=*/is_last };
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

    // Notify plugins that the render is done — safe here as we're on the
    // main thread and the PA stream is stopped for the render duration.
    graph->notify_transport_stop();

    // Resume the real-time stream, restoring beat position to pre-render state
    // so live playback isn't disrupted by the offline render scrub.
    if (stream_was_running) {
        dispatcher_.seek(saved_beat);
        current_beat_.store(saved_beat, std::memory_order_relaxed);
        PaError resume_err = Pa_StartStream(static_cast<PaStream*>(stream_));
        if (resume_err != paNoError)
            fprintf(stderr, "[AudioEngine] render_offline Pa_StartStream: %s\n",
                    Pa_GetErrorText(resume_err));
    }

    return output;
}

std::vector<uint8_t> AudioEngine::render_offline_wav(float tail_seconds, double duration_beats) {
    auto pcm = render_offline(tail_seconds, duration_beats);
    if (pcm.empty()) return {};
    return make_wav(pcm, static_cast<int>(cfg_.sample_rate), 2);
}
