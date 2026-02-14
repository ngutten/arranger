#pragma once
// graph.h
// Signal graph: nodes, ports, connections, and the evaluation order.
//
// The graph owns all nodes. It is rebuilt from a JSON GraphDesc on the main
// thread, then swapped atomically into the audio engine (same pattern as the
// Python engine's _pending_schedule). The audio thread never mutates the
// graph; it only reads it during process().

#include <string>
#include <vector>
#include <unordered_map>
#include <memory>
#include <functional>
#include <atomic>
#include <optional>

// PortAudio buffer size upper bound (for stack-allocating scratch buffers)
constexpr int MAX_BLOCK_SIZE = 4096;

// ---------------------------------------------------------------------------
// Port types
// ---------------------------------------------------------------------------

enum class PortType {
    AudioMono,   // float[block_size] — one channel of audio
    Control,     // single float, updated at control rate (~every block)
    Midi,        // structured MIDI events within a block (future)
};

// A buffer that flows between nodes on the audio thread.
// For audio ports: pointer into a pre-allocated pool (no heap allocation in hot path).
// For control ports: just a float.
struct PortBuffer {
    PortType type = PortType::AudioMono;
    float*   audio = nullptr;   // non-owning pointer, valid for one process() call
    float    control = 0.0f;    // used when type == Control
};

// ---------------------------------------------------------------------------
// Node interface
// ---------------------------------------------------------------------------

struct ProcessContext {
    int   block_size;
    float sample_rate;
    float bpm;
    double beat_position;  // beat at start of this block
    double beats_per_sample;
};

class Node {
public:
    std::string id;

    // Ports declared by the node
    struct PortDecl {
        std::string name;
        PortType    type;
        bool        is_output;
        float       default_value = 0.0f;
        float       min_value     = 0.0f;
        float       max_value     = 1.0f;
    };

    virtual ~Node() = default;

    // Called once after construction to declare ports.
    virtual std::vector<PortDecl> declare_ports() const = 0;

    // Called once when the graph is activated (sample_rate is now known).
    virtual void activate(float sample_rate, int max_block_size) {}

    // Called once when the graph is deactivated.
    virtual void deactivate() {}

    // Audio thread: process one block.
    // inputs/outputs are indexed by the order returned from declare_ports().
    virtual void process(
        const ProcessContext& ctx,
        const std::vector<PortBuffer>& inputs,
        std::vector<PortBuffer>&       outputs
    ) = 0;

    // Main thread: set a named parameter (thread-safe via atomic where needed).
    virtual void set_param(const std::string& name, float value) {}

    // Note events — called from audio thread before process().
    virtual void note_on (int channel, int pitch, int velocity) {}
    virtual void note_off(int channel, int pitch) {}
    virtual void program_change(int channel, int bank, int program) {}
    virtual void pitch_bend(int channel, int value) {}     // 14-bit, 8192=center
    virtual void channel_volume(int channel, int volume) {}
    virtual void all_notes_off(int channel = -1) {}

    // Control event — sets a queued value that will be applied at process() time.
    // normalized_value is 0..1; the node maps it to its internal range.
    virtual void push_control(double beat, float normalized_value) {}
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

// Scratch buffer pool — pre-allocated on graph activation, handed out to ports.
// One pool per graph instance; audio thread uses it exclusively.
class BufferPool {
public:
    void allocate(int num_buffers, int block_size);
    float* get(int index);  // panics if index out of range
    int    count() const { return static_cast<int>(buffers_.size()); }
private:
    std::vector<std::vector<float>> buffers_;
};

class Graph {
public:
    // Build from JSON (runs on main thread). Returns nullptr on error.
    static std::unique_ptr<Graph> from_json(
        const std::string& json,
        std::string& error_out
    );

    // Activate: allocate buffers, call node->activate(), compute eval order.
    bool activate(float sample_rate, int max_block_size);
    void deactivate();

    // Audio thread: process one block.
    // MIDI events for this block should be injected via node->note_on() etc.
    // before calling process().
    void process(const ProcessContext& ctx);

    // After process(), read the mixer output here.
    // Returns nullptr if graph has no mixer or is not activated.
    const float* output_L() const;
    const float* output_R() const;

    // Main thread: parameter updates (atomic).
    void set_param(const std::string& node_id, const std::string& param, float value);

    // Look up a node by id (main thread or audio thread, read-only).
    Node* find_node(const std::string& id) const;

    // Evaluation order (computed by activate()).
    const std::vector<std::string>& eval_order() const { return eval_order_; }

private:
    struct NodeEntry {
        std::unique_ptr<Node>        node;
        std::vector<Node::PortDecl>  ports;
        std::vector<int>             input_buf_indices;   // index into pool
        std::vector<int>             output_buf_indices;
    };

    std::vector<NodeEntry>                        nodes_;
    std::unordered_map<std::string, int>          node_index_;  // id → nodes_ index
    std::vector<Connection>                       connections_;
    std::vector<std::string>                      eval_order_;

    BufferPool                                    pool_;
    float*                                        output_L_ = nullptr;
    float*                                        output_R_ = nullptr;
    int                                           block_size_ = 0;
    bool                                          activated_ = false;

    // Build topological eval order from connections_.
    bool topo_sort(std::string& error_out);

    // Wire up buffer indices from pool after topo sort.
    void assign_buffers();
};
