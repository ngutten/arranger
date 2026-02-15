// graph.cpp
#include "graph.h"
#include "synth_node.h"
#include "plugin_adapter.h"
#include "nlohmann/json.hpp"

#include <stdexcept>
#include <unordered_set>
#include <algorithm>
#include <cstring>

using json = nlohmann::json;

// ---------------------------------------------------------------------------
// BufferPool
// ---------------------------------------------------------------------------

void BufferPool::allocate(int num_buffers, int block_size) {
    buffers_.assign(num_buffers, std::vector<float>(block_size, 0.0f));
}

float* BufferPool::get(int index) {
    return buffers_.at(index).data();
}

// ---------------------------------------------------------------------------
// Graph::from_json
// ---------------------------------------------------------------------------

std::unique_ptr<Graph> Graph::from_json(const std::string& j_str, std::string& err) {
    json j;
    try { j = json::parse(j_str); }
    catch (const std::exception& e) {
        err = std::string("JSON parse error: ") + e.what();
        return nullptr;
    }

    auto g = std::make_unique<Graph>();

    // --- Nodes ---
    for (auto& jn : j.value("nodes", json::array())) {
        NodeDesc desc;
        desc.id          = jn.value("id", "");
        desc.type        = jn.value("type", "sine");
        desc.sf2_path    = jn.value("sf2_path", "");
        desc.lv2_uri     = jn.value("lv2_uri", "");
        desc.sample_path = jn.value("sample_path", "");
        desc.channel_count = jn.value("channel_count", 2);
        desc.pitch_lo    = jn.value("pitch_lo", 0);
        desc.pitch_hi    = jn.value("pitch_hi", 127);
        desc.gate_mode   = jn.value("gate_mode", 0);
        // Collect string params for configure() calls on plugin-backed nodes.
        // Numeric params go into desc.params (applied via set_param after activate).
        std::unordered_map<std::string, std::string> string_params;
        if (jn.contains("params")) {
            for (auto& [k, v] : jn["params"].items()) {
                if (v.is_number())
                    desc.params[k] = v.get<float>();
                else if (v.is_string())
                    string_params[k] = v.get<std::string>();
            }
        }
        // Also forward the dedicated NodeDesc string fields as configure() keys
        // so plugin-backed nodes (e.g. builtin.fluidsynth) receive them even
        // though make_node() only uses them for the legacy hardcoded node types.
        if (!desc.sf2_path.empty())    string_params.emplace("sf2_path",    desc.sf2_path);
        if (!desc.lv2_uri.empty())     string_params.emplace("lv2_uri",     desc.lv2_uri);
        if (!desc.sample_path.empty()) string_params.emplace("sample_path", desc.sample_path);

        std::string node_err;
        auto node = make_node(desc, node_err);
        if (!node) {
            err = "Failed to create node '" + desc.id + "': " + node_err;
            return nullptr;
        }

        // For plugin-backed nodes, deliver string config params via configure().
        // This is how sf2_path reaches FluidSynthPlugin before activate() is called.
        if (auto* adapter = dynamic_cast<PluginAdapterNode*>(node.get())) {
            for (auto& [k, v] : string_params)
                adapter->plugin()->configure(k, v);
        }

        NodeEntry entry;
        entry.node        = std::move(node);
        entry.ports       = entry.node->declare_ports();
        entry.init_params = desc.params;   // applied via set_param() after activate()

        g->node_index_[desc.id] = static_cast<int>(g->nodes_.size());
        g->nodes_.push_back(std::move(entry));
    }

    // --- Connections ---
    for (auto& jc : j.value("connections", json::array())) {
        g->connections_.push_back({
            jc.value("from_node", ""),
            jc.value("from_port", ""),
            jc.value("to_node",   ""),
            jc.value("to_port",   ""),
        });
    }

    return g;
}

// ---------------------------------------------------------------------------
// Graph::~Graph
// ---------------------------------------------------------------------------

Graph::~Graph() {
    // Ensure LV2 instances are properly shut down even if deactivate() was
    // never called explicitly (e.g. when retiring_graph_ unique_ptr is reset).
    deactivate();
}

// ---------------------------------------------------------------------------
// Graph::activate
// ---------------------------------------------------------------------------

bool Graph::activate(float sample_rate, int max_block_size) {
    block_size_ = max_block_size;

    std::string err;
    if (!topo_sort(err)) {
        // topo_sort failure is non-fatal: fall back to declaration order
        // (connections may still work for simple linear chains)
        eval_order_.clear();
        for (auto& e : nodes_) eval_order_.push_back(e.node->id);
    }

    assign_buffers();

    for (auto& entry : nodes_) {
        entry.node->activate(sample_rate, max_block_size);
        // Apply initial params from the JSON NodeDesc (must be after activate
        // so that LV2 port buffers are allocated and connected)
        for (auto& [k, v] : entry.init_params)
            entry.node->set_param(k, v);
    }

    // Wire downstream processor nodes into each TrackSourceNode.
    // Any node connected from a track_source (regardless of type) receives
    // note events. This covers synth nodes AND NoteGateNodes.
    for (auto& entry : nodes_) {
        auto* src = dynamic_cast<TrackSourceNode*>(entry.node.get());
        if (!src) continue;

        std::vector<Node*> downstream;
        for (auto& c : connections_) {
            if (c.from_node != entry.node->id) continue;
            auto ni = node_index_.find(c.to_node);
            if (ni == node_index_.end()) continue;
            Node* dest = nodes_[ni->second].node.get();
            // Avoid duplicates (multiple ports from same source → same dest)
            bool already = false;
            for (auto* d : downstream) if (d == dest) { already = true; break; }
            if (!already) downstream.push_back(dest);
        }
        src->set_downstream(std::move(downstream));
    }

    activated_ = true;
    return true;
}

void Graph::deactivate() {
    for (auto& entry : nodes_) entry.node->deactivate();
    activated_ = false;
}

// ---------------------------------------------------------------------------
// Graph::topo_sort  (Kahn's algorithm)
// ---------------------------------------------------------------------------

bool Graph::topo_sort(std::string& err) {
    // Build adjacency: for each connection, from_node must come before to_node
    std::unordered_map<std::string, std::vector<std::string>> adj;
    std::unordered_map<std::string, int> in_degree;

    for (auto& e : nodes_) {
        in_degree[e.node->id] = 0;
        adj[e.node->id] = {};
    }

    for (auto& c : connections_) {
        if (c.from_node == c.to_node) continue;
        adj[c.from_node].push_back(c.to_node);
        in_degree[c.to_node]++;
    }

    std::vector<std::string> queue;
    for (auto& [id, deg] : in_degree) {
        if (deg == 0) queue.push_back(id);
    }

    eval_order_.clear();
    while (!queue.empty()) {
        auto n = queue.back(); queue.pop_back();
        eval_order_.push_back(n);
        for (auto& m : adj[n]) {
            if (--in_degree[m] == 0) queue.push_back(m);
        }
    }

    if (eval_order_.size() != nodes_.size()) {
        err = "Cycle detected in signal graph";
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Graph::assign_buffers
// ---------------------------------------------------------------------------

void Graph::assign_buffers() {
    // Count total buffers needed: one per output port across all nodes,
    // plus a "null" buffer at index 0 for unconnected inputs.
    int buf_count = 1; // 0 = silent/zero buffer

    for (auto& entry : nodes_) {
        int in_count  = 0, out_count = 0;
        for (auto& p : entry.ports) {
            if (p.is_output) out_count++;
            else             in_count++;
        }
        entry.output_buf_indices.assign(out_count, 0);
        entry.input_buf_indices.assign(in_count, 0);

        for (auto& idx : entry.output_buf_indices) {
            idx = buf_count++;
        }
    }

    pool_.allocate(buf_count, block_size_);

    // Wire input buffers from connections
    // Build port-name → buffer index map for outputs
    std::unordered_map<std::string, int> port_buf; // "node_id/port_name" → buf_idx

    for (auto& entry : nodes_) {
        int out_i = 0;
        for (int pi = 0; pi < (int)entry.ports.size(); ++pi) {
            auto& p = entry.ports[pi];
            if (p.is_output) {
                std::string key = entry.node->id + "/" + p.name;
                port_buf[key] = entry.output_buf_indices[out_i++];
            }
        }
    }

    // Assign input buffers from connections
    for (auto& c : connections_) {
        std::string src_key = c.from_node + "/" + c.from_port;
        auto it = port_buf.find(src_key);
        if (it == port_buf.end()) continue;
        int src_buf = it->second;

        // Find to_node entry
        auto ni = node_index_.find(c.to_node);
        if (ni == node_index_.end()) continue;
        auto& to_entry = nodes_[ni->second];

        int in_i = 0;
        for (auto& p : to_entry.ports) {
            if (p.is_output) continue;
            if (p.name == c.to_port) {
                to_entry.input_buf_indices[in_i] = src_buf;
                break;
            }
            in_i++;
        }
    }

    // Cache mixer output pointers
    auto mixer_it = node_index_.find("mixer");
    if (mixer_it != node_index_.end()) {
        auto& me = nodes_[mixer_it->second];
        int out_i = 0;
        for (int pi = 0; pi < (int)me.ports.size(); ++pi) {
            auto& p = me.ports[pi];
            if (!p.is_output) continue;
            if (p.name == "audio_out_L")
                output_L_ = pool_.get(me.output_buf_indices[out_i]);
            else if (p.name == "audio_out_R")
                output_R_ = pool_.get(me.output_buf_indices[out_i]);
            out_i++;
        }
    }
}

// ---------------------------------------------------------------------------
// Graph::process
// ---------------------------------------------------------------------------

void Graph::process(const ProcessContext& ctx) {
    if (!activated_) return;

    // Zero the null buffer (index 0)
    std::memset(pool_.get(0), 0, ctx.block_size * sizeof(float));

    for (auto& node_id : eval_order_) {
        auto ni = node_index_.find(node_id);
        if (ni == node_index_.end()) continue;
        auto& entry = nodes_[ni->second];

        // Build PortBuffer vectors for this node
        std::vector<PortBuffer> inputs, outputs;

        int in_i = 0, out_i = 0;
        for (auto& p : entry.ports) {
            PortBuffer pb;
            pb.type = p.type;
            if (p.is_output) {
                pb.audio = pool_.get(entry.output_buf_indices[out_i++]);
                outputs.push_back(pb);
            } else {
                pb.audio = pool_.get(entry.input_buf_indices[in_i++]);
                inputs.push_back(pb);
            }
        }

        entry.node->process(ctx, inputs, outputs);

        // --- Route event outputs from PluginAdapterNodes ---
        // If this node produced events on output ports, forward them to
        // connected downstream nodes via note_on/off/etc.
        auto* adapter = dynamic_cast<PluginAdapterNode*>(entry.node.get());
        if (adapter) {
            for (auto& [port_id, events] : adapter->event_outputs()) {
                if (events.empty()) continue;
                // Find all connections from this node's event output port
                for (auto& c : connections_) {
                    if (c.from_node != node_id || c.from_port != port_id) continue;
                    auto dest_it = node_index_.find(c.to_node);
                    if (dest_it == node_index_.end()) continue;
                    Node* dest = nodes_[dest_it->second].node.get();
                    // Deliver events via the MIDI convenience interface
                    for (auto& ev : events) {
                        uint8_t type = ev.status & 0xF0;
                        int ch = ev.channel;
                        if (type == 0x90 && ev.data2 > 0) {
                            dest->note_on(ch, ev.data1, ev.data2);
                        } else if (type == 0x80 || (type == 0x90 && ev.data2 == 0)) {
                            dest->note_off(ch, ev.data1);
                        } else if (type == 0xE0) {
                            dest->pitch_bend(ch, ev.data1 | (ev.data2 << 7));
                        } else if (type == 0xC0) {
                            dest->program_change(ch, 0, ev.data1);
                        }
                    }
                }
            }
        }
    }
}

const float* Graph::output_L() const { return output_L_; }
const float* Graph::output_R() const { return output_R_; }

void Graph::set_param(const std::string& nid, const std::string& param, float val) {
    auto* n = find_node(nid);
    if (n) n->set_param(param, val);
}

Node* Graph::find_node(const std::string& id) const {
    auto it = node_index_.find(id);
    if (it == node_index_.end()) return nullptr;
    return nodes_[it->second].node.get();
}
