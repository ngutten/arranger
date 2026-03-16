#pragma once
// graph.h
// Signal graph: nodes, ports, connections, and the evaluation order.

#include <string>
#include <vector>
#include <unordered_map>
#include <memory>
#include <functional>
#include <atomic>
#include <optional>
#include "plugin_api.h"   // PatternData, PatternNote

constexpr int MAX_BLOCK_SIZE = 4096;

// ---------------------------------------------------------------------------
// Port types
// ---------------------------------------------------------------------------

enum class PortType {
    AudioMono,
    Control,
    Midi,
};

struct PortBuffer {
    PortType type = PortType::AudioMono;
    float*   audio = nullptr;
    float    control = 0.0f;
    float*   control_buf = nullptr;       // raw pool buffer for control ports
    bool     control_per_sample = false;  // set if producer wrote per-sample data
};

// ---------------------------------------------------------------------------
// Node interface
// ---------------------------------------------------------------------------

struct ProcessContext {
    int   block_size;
    float sample_rate;
    float bpm;
    double beat_position;
    double beats_per_sample;

    bool  is_playing        = false;
    bool  transport_started = false;
    bool  transport_stopped = false;
};

class Node {
public:
    std::string id;

    struct PortDecl {
        std::string name;
        PortType    type;
        bool        is_output;
        float       default_value = 0.0f;
        float       min_value     = 0.0f;
        float       max_value     = 1.0f;
    };

    virtual ~Node() = default;

    virtual std::vector<PortDecl> declare_ports() const = 0;
    virtual void activate(float sample_rate, int max_block_size) {}
    virtual void deactivate() {}

    virtual void process(
        const ProcessContext& ctx,
        const std::vector<PortBuffer>& inputs,
        std::vector<PortBuffer>&       outputs
    ) = 0;

    virtual void set_param(const std::string& name, float value) {}

    virtual void note_on (int channel, int pitch, int velocity) {}
    virtual void note_off(int channel, int pitch) {}
    virtual void program_change(int channel, int bank, int program) {}
    virtual void pitch_bend(int channel, int value) {}
    virtual void channel_volume(int channel, int volume) {}
    virtual void note_tune(int channel, int note, float semitones) {}
    virtual void all_notes_off(int channel = -1) {}
    virtual void push_control(double beat, float normalized_value) {}

    /// Called from main thread when a NoteOn lyric syllable should be
    /// pre-rendered.  TrackSourceNode fans this out to downstream nodes;
    /// PluginAdapterNode forwards it to the plugin.
    virtual void push_lyric(double beat, const std::string& lyric,
                            int pitch = -1, double duration_beats = 0.0) {}

    /// Called from main thread after all push_lyric() calls for a schedule.
    /// Signals the node to publish its pre-rendered phoneme sequence.
    virtual void on_schedule_loaded() {}

    /// Called from main thread before playback or offline render.
    /// Audio thread is guaranteed not running.  Heavy pre-computation goes here.
    virtual void prerender() {}

    /// Called from main thread before prerender() to provide the current BPM.
    virtual void set_bpm(float /*bpm*/) {}

    /// Called from audio thread on a transport seek.  The node should
    /// reposition any beat-indexed cursor to the note at or after beat.
    virtual void on_seek(double beat) {}

    /// Return the pattern data stored in this node, if it is a pattern source.
    /// Returns nullptr for all other node types.
    virtual const PatternData* get_pattern_data() const { return nullptr; }
};

// ---------------------------------------------------------------------------
// PatternSourceNode
// ---------------------------------------------------------------------------
// A source node that holds a complete PatternData snapshot.  It has no
// audio/control ports visible to the graph engine; its data is injected into
// downstream PluginAdapterNodes by Graph::activate() after the normal
// buffer-assignment pass.
//
// The pattern data is immutable once the node is created; it lives for the
// lifetime of the graph (which is fine — graphs are rebuilt on any change).

class PatternSourceNode final : public Node {
public:
    explicit PatternSourceNode(const std::string& node_id, PatternData data)
        : data_(std::move(data))
    {
        id = node_id;
    }

    std::vector<PortDecl> declare_ports() const override { return {}; }

    void process(const ProcessContext&,
                 const std::vector<PortBuffer>&,
                 std::vector<PortBuffer>&) override {}

    const PatternData* get_pattern_data() const override { return &data_; }

private:
    PatternData data_;
};

// ---------------------------------------------------------------------------
// Graph
// ---------------------------------------------------------------------------

struct Connection {
    std::string from_node;
    std::string from_port;
    std::string to_node;
    std::string to_port;
};

class BufferPool {
public:
    void allocate(int num_buffers, int block_size);
    float* get(int index);
    int    count() const { return static_cast<int>(buffers_.size()); }
private:
    std::vector<std::vector<float>> buffers_;
};

class Graph {
public:
    Graph()  = default;
    ~Graph();

    static std::unique_ptr<Graph> from_json(
        const std::string& json,
        std::string& error_out
    );

    bool activate(float sample_rate, int max_block_size);
    void deactivate();

    void process(const ProcessContext& ctx);

    const float* output_L() const;
    const float* output_R() const;

    void set_param(const std::string& node_id, const std::string& param, float value);
    void notify_transport_stop();

    Node* find_node(const std::string& id) const;

    const std::vector<std::string>& eval_order() const { return eval_order_; }

private:
    struct NodeEntry {
        std::unique_ptr<Node>        node;
        std::vector<Node::PortDecl>  ports;
        std::vector<int>             input_buf_indices;
        std::vector<int>             output_buf_indices;
        std::unordered_map<std::string, float> init_params;
    };

    std::vector<NodeEntry>                        nodes_;
    std::unordered_map<std::string, int>          node_index_;
    std::vector<Connection>                       connections_;
    std::vector<std::string>                      eval_order_;

    BufferPool                                    pool_;
    float*                                        output_L_ = nullptr;
    float*                                        output_R_ = nullptr;
    int                                           block_size_ = 0;
    bool                                          activated_ = false;

    bool topo_sort(std::string& error_out);
    void assign_buffers();

    /// After assign_buffers(), wire pattern source outputs into pattern
    /// input ports on downstream PluginAdapterNodes.
    void wire_pattern_ports();
};
