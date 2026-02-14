#pragma once
// synth_node.h
// Concrete Node implementations.
//
// Factory function at the bottom: make_node() dispatches on NodeDesc.type.

#include "graph.h"
#include <string>
#include <memory>
#include <mutex>
#include <queue>

// ---------------------------------------------------------------------------
// SineNode — built-in sine fallback, no external dependencies
// ---------------------------------------------------------------------------

class SineNode final : public Node {
public:
    explicit SineNode(const std::string& id_);

    std::vector<PortDecl> declare_ports() const override;
    void activate(float sample_rate, int max_block_size) override;
    void process(const ProcessContext& ctx,
                 const std::vector<PortBuffer>& inputs,
                 std::vector<PortBuffer>& outputs) override;

    void note_on(int channel, int pitch, int velocity) override;
    void note_off(int channel, int pitch) override;
    void all_notes_off(int channel = -1) override;
    void set_param(const std::string& name, float value) override;

private:
    struct Voice {
        double phase       = 0.0;
        double freq        = 440.0;
        float  amp         = 0.5f;
        bool   releasing   = false;
        float  env         = 1.0f;
        float  env_release = 0.0f;  // per-sample decay rate
        bool   done        = false;
    };

    float  sample_rate_ = 44100.0f;
    float  gain_        = 0.15f;
    // key = channel*128 + pitch
    std::unordered_map<int, Voice> voices_;
};

// ---------------------------------------------------------------------------
// MixerNode — sums N stereo pairs into one stereo output
// ---------------------------------------------------------------------------

class MixerNode final : public Node {
public:
    // input_count: number of stereo input pairs
    MixerNode(const std::string& id_, int input_count);

    std::vector<PortDecl> declare_ports() const override;
    void activate(float sample_rate, int max_block_size) override;
    void process(const ProcessContext& ctx,
                 const std::vector<PortBuffer>& inputs,
                 std::vector<PortBuffer>& outputs) override;
    void set_param(const std::string& name, float value) override;  // "gain_N" → channel N gain

private:
    int              input_count_;
    std::vector<float> channel_gain_;
    float              master_gain_ = 1.0f;
    int              block_size_   = 0;
};

// ---------------------------------------------------------------------------
// TrackSourceNode — addressable event source for one sequencer track
// ---------------------------------------------------------------------------
// Has no audio ports. Receives scheduled events (note_on, note_off, etc.)
// from the Dispatcher and preview injections from the note_on/note_off IPC
// commands. Fans both out to a registered list of downstream processor nodes.
//
// Downstream nodes are registered by Graph::activate() after reading the
// connection list — the source node does not parse connections itself.
//
// Preview state (injected via note_on IPC command) is kept in a separate set
// from schedule-driven notes. stop/seek/all_notes_off on the transport does
// NOT clear preview notes; the explicit note_off or all_notes_off IPC command
// does.

class TrackSourceNode final : public Node {
public:
    explicit TrackSourceNode(const std::string& id_);

    std::vector<PortDecl> declare_ports() const override;
    void process(const ProcessContext& ctx,
                 const std::vector<PortBuffer>& inputs,
                 std::vector<PortBuffer>& outputs) override;

    // Called by Graph::activate() to register downstream synth nodes.
    void set_downstream(std::vector<Node*> nodes);

    // Scheduled events — forwarded immediately to all downstream nodes.
    void note_on (int channel, int pitch, int velocity) override;
    void note_off(int channel, int pitch) override;
    void program_change(int channel, int bank, int program) override;
    void pitch_bend(int channel, int value) override;
    void channel_volume(int channel, int volume) override;

    // Transport all_notes_off — clears schedule-driven notes downstream,
    // but leaves preview notes alive.
    void all_notes_off(int channel = -1) override;

    // Preview injection (from IPC note_on / note_off commands).
    // Thread-safe: uses a lock so the IPC thread can call these.
    void preview_note_on (int channel, int pitch, int velocity);
    void preview_note_off(int channel, int pitch);
    void preview_all_notes_off();  // called by all_notes_off IPC with no transport flag

private:
    struct PreviewNote { int channel; int pitch; int velocity; };

    std::vector<Node*>      downstream_;   // non-owning, valid for graph lifetime
    std::mutex              preview_mutex_;
    std::vector<PreviewNote> pending_on_;   // injected but not yet forwarded
    std::vector<std::pair<int,int>> pending_off_; // (channel, pitch) — -1,-1 = all
};

// ---------------------------------------------------------------------------
// ControlSourceNode — delivers scheduled control values to connected params
// ---------------------------------------------------------------------------
// This is the "event output" concept: a node with no audio output, only a
// control output port. The scheduler pushes timestamped values via
// push_control(); process() outputs the interpolated value each block.

class ControlSourceNode final : public Node {
public:
    explicit ControlSourceNode(const std::string& id_);

    std::vector<PortDecl> declare_ports() const override;
    void process(const ProcessContext& ctx,
                 const std::vector<PortBuffer>& inputs,
                 std::vector<PortBuffer>& outputs) override;

    // Called from audio thread (in Dispatcher::dispatch) before process().
    void push_control(double beat, float normalized_value) override;

private:
    struct ControlPoint { double beat; float value; };
    // Small ring buffer — no heap allocation after construction.
    // Audio thread writes, process() consumes.
    static constexpr int RING_SIZE = 64;
    ControlPoint ring_[RING_SIZE];
    std::atomic<int> write_idx_ { 0 };
    int              read_idx_  { 0 };
    float            current_   { 0.0f };
};

// ---------------------------------------------------------------------------
// FluidSynthNode — SF2-backed MIDI synth  (compiled only with AS_ENABLE_SF2)
// ---------------------------------------------------------------------------

#ifdef AS_ENABLE_SF2

class FluidSynthNode final : public Node {
public:
    FluidSynthNode(const std::string& id_, const std::string& sf2_path);
    ~FluidSynthNode() override;

    std::vector<PortDecl> declare_ports() const override;
    void activate(float sample_rate, int max_block_size) override;
    void deactivate() override;
    void process(const ProcessContext& ctx,
                 const std::vector<PortBuffer>& inputs,
                 std::vector<PortBuffer>& outputs) override;

    void note_on(int channel, int pitch, int velocity) override;
    void note_off(int channel, int pitch) override;
    void program_change(int channel, int bank, int program) override;
    void pitch_bend(int channel, int value) override;
    void channel_volume(int channel, int volume) override;
    void all_notes_off(int channel = -1) override;

private:
    std::string sf2_path_;
    void*       fs_   = nullptr;  // fluid_synth_t* (opaque to avoid header dep)
    void*       fset_ = nullptr;  // fluid_settings_t*
    int         sfid_ = -1;
    float       sample_rate_ = 44100.0f;
    int         block_size_  = 0;

    // Temp interleaved buffer: fluidsynth gives us int16 interleaved
    std::vector<int16_t> raw_buf_;
};

#endif // AS_ENABLE_SF2

// ---------------------------------------------------------------------------
// LV2Node — LV2 plugin host  (compiled only with AS_ENABLE_LV2)
// ---------------------------------------------------------------------------

#ifdef AS_ENABLE_LV2

class LV2Node final : public Node {
public:
    LV2Node(const std::string& id_, const std::string& uri);
    ~LV2Node() override;

    std::vector<PortDecl> declare_ports() const override;
    void activate(float sample_rate, int max_block_size) override;
    void deactivate() override;
    void process(const ProcessContext& ctx,
                 const std::vector<PortBuffer>& inputs,
                 std::vector<PortBuffer>& outputs) override;
    void set_param(const std::string& name, float value) override;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// List all installed LV2 plugins via lilv.
// Returns JSON array: [{uri, name, author, ports:[{symbol,type,direction}]}]
std::string list_lv2_plugins(const std::string& uri_prefix = "");

#endif // AS_ENABLE_LV2

// ---------------------------------------------------------------------------
// NoteGateNode — converts a MIDI event stream into a control signal
// ---------------------------------------------------------------------------
// Sits on a TrackSourceNode's downstream list (receives note_on/note_off like
// a synth) and emits a control_out buffer port (like ControlSourceNode).
//
// Output modes (set via set_param("mode", N)):
//   0 — Gate:       1.0 while any in-band note is held, 0.0 otherwise  (default)
//   1 — Velocity:   normalised velocity (vel/127) of the most recent note-on
//                   in band; 0.0 when no notes are held
//   2 — Pitch:      position of the most recent note within [pitch_lo, pitch_hi]
//                   mapped to [0, 1]; 0.0 when no notes are held
//   3 — NoteCount:  number of simultaneously held in-band notes, normalised by
//                   the band width (pitch_hi - pitch_lo + 1); clamped to [0,1]
//
// Pitch band: only notes with pitch_lo <= pitch <= pitch_hi are counted.
// Default band is 0..127 (all notes).
// Latch behaviour: gate stays high while ANY in-band note is held.
//
// All event methods are called on the audio thread before process(); no locking
// is needed — the graph guarantees single-threaded event delivery per block.

class NoteGateNode final : public Node {
public:
    explicit NoteGateNode(const std::string& id_,
                          int pitch_lo = 0, int pitch_hi = 127,
                          int mode = 0);

    std::vector<PortDecl> declare_ports() const override;
    void process(const ProcessContext& ctx,
                 const std::vector<PortBuffer>& inputs,
                 std::vector<PortBuffer>& outputs) override;

    void note_on (int channel, int pitch, int velocity) override;
    void note_off(int channel, int pitch) override;
    void all_notes_off(int channel = -1) override;

    // set_param: "pitch_lo", "pitch_hi" (0-127), "mode" (0-3)
    void set_param(const std::string& name, float value) override;

private:
    int   pitch_lo_  = 0;
    int   pitch_hi_  = 127;
    int   mode_      = 0;

    // Active notes in band: key = channel*128 + pitch, value = velocity
    std::unordered_map<int, int> active_;

    float current_value_ = 0.0f;   // written by note events, read by process()

    void  recompute_value_();
    bool  in_band_(int pitch) const { return pitch >= pitch_lo_ && pitch <= pitch_hi_; }
};


// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

struct NodeDesc {
    std::string id;
    std::string type;          // "fluidsynth"|"sine"|"lv2"|"mixer"|"control_source"|"track_source"|"note_gate"
    std::string sf2_path;      // fluidsynth
    std::string lv2_uri;       // lv2
    std::string sample_path;   // sampler (future)
    int         channel_count = 2;  // mixer
    int         pitch_lo      = 0;   // note_gate
    int         pitch_hi      = 127; // note_gate
    int         gate_mode     = 0;   // note_gate: 0=gate 1=velocity 2=pitch 3=note_count
    std::unordered_map<std::string, float> params;
};

// Returns nullptr + fills error on failure.
std::unique_ptr<Node> make_node(const NodeDesc& desc, std::string& error_out);
