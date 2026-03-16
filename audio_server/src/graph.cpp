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
// Helpers — parse PatternData from JSON
// ---------------------------------------------------------------------------

static PatternData parse_pattern_data(const json& jp) {
    PatternData pd;
    pd.length_beats    = jp.value("length_beats", 0.0);
    pd.subdivision     = jp.value("subdivision", 0);
    pd.is_beat_pattern = jp.value("is_beat_pattern", false);

    for (auto& jn : jp.value("notes", json::array())) {
        PatternNote n;
        n.beat     = jn.value("beat",     0.0);
        n.duration = jn.value("duration", 0.25);
        n.channel  = static_cast<uint8_t>(jn.value("channel",  0));
        n.pitch    = static_cast<uint8_t>(jn.value("pitch",    60));
        n.velocity = static_cast<uint8_t>(jn.value("velocity", 100));
        n.program  = jn.value("program", -1);
        n.bank     = jn.value("bank",    -1);
        n.lyric    = jn.value("lyric",   "");
        pd.notes.push_back(n);
    }

    // Sort by beat for plugins that walk the list.
    std::sort(pd.notes.begin(), pd.notes.end(),
              [](const PatternNote& a, const PatternNote& b){
                  return a.beat < b.beat;
              });

    return pd;
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

    for (auto& jn : j.value("nodes", json::array())) {
        const std::string type = jn.value("type", "sine");
        const std::string nid  = jn.value("id",   "");

        // -------------------------------------------------------------------
        // pattern_source / beat_pattern_source — no plugin, just holds data
        // -------------------------------------------------------------------
        if (type == "pattern_source" || type == "beat_pattern_source") {
            if (!jn.contains("pattern")) {
                err = "Node '" + nid + "' of type '" + type + "' missing 'pattern' field";
                return nullptr;
            }
            PatternData pd = parse_pattern_data(jn["pattern"]);
            auto node = std::make_unique<PatternSourceNode>(nid, std::move(pd));

            NodeEntry entry;
            entry.node  = std::move(node);
            entry.ports = entry.node->declare_ports();

            g->node_index_[nid] = static_cast<int>(g->nodes_.size());
            g->nodes_.push_back(std::move(entry));
            continue;
        }

        // -------------------------------------------------------------------
        // All other node types (existing path, unchanged)
        // -------------------------------------------------------------------
        NodeDesc desc;
        desc.id          = nid;
        desc.type        = type;
        desc.sf2_path    = jn.value("sf2_path", "");
        desc.sample_path = jn.value("sample_path", "");
        desc.channel_count = jn.value("channel_count", 2);
        desc.pitch_lo    = jn.value("pitch_lo", 0);
        desc.pitch_hi    = jn.value("pitch_hi", 127);
        desc.gate_mode   = jn.value("gate_mode", 0);

        std::unordered_map<std::string, std::string> string_params;
        if (jn.contains("params")) {
            for (auto& [k, v] : jn["params"].items()) {
                if (v.is_number())
                    desc.params[k] = v.get<float>();
                else if (v.is_string())
                    string_params[k] = v.get<std::string>();
            }
        }
        if (!desc.sf2_path.empty())    string_params.emplace("sf2_path",    desc.sf2_path);
        if (!desc.sample_path.empty()) string_params.emplace("sample_path", desc.sample_path);

        std::string node_err;
        auto node = make_node(desc, node_err);
        if (!node) {
            err = "Failed to create node '" + desc.id + "': " + node_err;
            return nullptr;
        }

        if (auto* adapter = dynamic_cast<PluginAdapterNode*>(node.get())) {
            for (auto& [k, v] : string_params)
                adapter->plugin()->configure(k, v);
        }

        NodeEntry entry;
        entry.node        = std::move(node);
        entry.ports       = entry.node->declare_ports();
        entry.init_params = desc.params;

        g->node_index_[desc.id] = static_cast<int>(g->nodes_.size());
        g->nodes_.push_back(std::move(entry));
    }

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
    deactivate();
}

// ---------------------------------------------------------------------------
// Graph::activate
// ---------------------------------------------------------------------------

bool Graph::activate(float sample_rate, int max_block_size) {
    block_size_ = max_block_size;

    std::string err;
    if (!topo_sort(err)) {
        eval_order_.clear();
        for (auto& e : nodes_) eval_order_.push_back(e.node->id);
    }

    assign_buffers();

    // Notify plugin adapters which control ports have live upstream connections.
    for (auto& c : connections_) {
        auto ni = node_index_.find(c.to_node);
        if (ni == node_index_.end()) continue;
        auto* adapter = dynamic_cast<PluginAdapterNode*>(nodes_[ni->second].node.get());
        if (adapter) adapter->set_control_connected(c.to_port, true);
    }

    // Wire pattern source outputs into downstream plugin pattern input ports.
    wire_pattern_ports();

    for (auto& entry : nodes_) {
        entry.node->activate(sample_rate, max_block_size);
        for (auto& [k, v] : entry.init_params)
            entry.node->set_param(k, v);
    }

    // Wire downstream processor nodes into each TrackSourceNode.
    for (auto& entry : nodes_) {
        auto* src = dynamic_cast<TrackSourceNode*>(entry.node.get());
        if (!src) continue;

        std::vector<Node*> downstream;
        for (auto& c : connections_) {
            if (c.from_node != entry.node->id) continue;
            auto ni = node_index_.find(c.to_node);
            if (ni == node_index_.end()) continue;
            Node* dest = nodes_[ni->second].node.get();
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
// Graph::wire_pattern_ports
// ---------------------------------------------------------------------------
// Walk all connections.  Whenever a PatternSourceNode's output is connected
// to a PluginAdapterNode, inject the pattern data pointer via set_pattern().
// The port name on the pattern source side is conventional ("pattern_out");
// the port name on the plugin side is whatever the plugin declared.

void Graph::wire_pattern_ports() {
    for (auto& c : connections_) {
        auto src_it = node_index_.find(c.from_node);
        if (src_it == node_index_.end()) continue;
        Node* src_node = nodes_[src_it->second].node.get();

        const PatternData* pd = src_node->get_pattern_data();
        if (!pd) continue;

        auto dst_it = node_index_.find(c.to_node);
        if (dst_it == node_index_.end()) continue;
        auto* adapter = dynamic_cast<PluginAdapterNode*>(nodes_[dst_it->second].node.get());
        if (!adapter) continue;

        adapter->set_pattern(c.to_port, pd);
        // Notify the plugin so it can inspect the full pattern data (including
        // per-note lyrics) before activate() is called.
        adapter->plugin()->on_pattern_connected(*pd);
    }
}

// ---------------------------------------------------------------------------
// Graph::topo_sort  (Kahn's algorithm)
// ---------------------------------------------------------------------------

bool Graph::topo_sort(std::string& err) {
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
    int buf_count = 1;

    for (auto& entry : nodes_) {
        int in_count = 0, out_count = 0;
        for (auto& p : entry.ports) {
            if (p.is_output) out_count++;
            else             in_count++;
        }
        entry.output_buf_indices.assign(out_count, 0);
        entry.input_buf_indices.assign(in_count, 0);

        for (auto& idx : entry.output_buf_indices)
            idx = buf_count++;
    }

    pool_.allocate(buf_count, block_size_);

    std::unordered_map<std::string, int> port_buf;

    for (auto& entry : nodes_) {
        int out_i = 0;
        for (auto& p : entry.ports) {
            if (p.is_output) {
                std::string key = entry.node->id + "/" + p.name;
                port_buf[key] = entry.output_buf_indices[out_i++];
            }
        }
    }

    for (auto& c : connections_) {
        std::string src_key = c.from_node + "/" + c.from_port;
        auto it = port_buf.find(src_key);
        if (it == port_buf.end()) continue;
        int src_buf = it->second;

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

    auto mixer_it = node_index_.find("mixer");
    if (mixer_it != node_index_.end()) {
        auto& me = nodes_[mixer_it->second];
        int out_i = 0;
        for (auto& p : me.ports) {
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

    std::memset(pool_.get(0), 0, ctx.block_size * sizeof(float));

    for (auto& node_id : eval_order_) {
        auto ni = node_index_.find(node_id);
        if (ni == node_index_.end()) continue;
        auto& entry = nodes_[ni->second];

        std::vector<PortBuffer> inputs, outputs;

        int in_i = 0, out_i = 0;
        for (auto& p : entry.ports) {
            PortBuffer pb;
            pb.type = p.type;
            if (p.is_output) {
                int buf_idx = entry.output_buf_indices[out_i++];
                pb.audio = pool_.get(buf_idx);
                if (p.type == PortType::Control) {
                    pb.control = 0.0f;
                    pb.control_buf = pool_.get(buf_idx);
                    pb.control_per_sample = false;
                }
                outputs.push_back(pb);
            } else {
                int buf_idx = entry.input_buf_indices[in_i++];
                pb.audio = pool_.get(buf_idx);
                if (p.type == PortType::Control) {
                    pb.control = pool_.get(buf_idx)[0];
                    pb.control_buf = pool_.get(buf_idx);
                }
                inputs.push_back(pb);
            }
        }

        entry.node->process(ctx, inputs, outputs);

        out_i = 0;
        for (auto& p : entry.ports) {
            if (!p.is_output) continue;
            if (p.type == PortType::Control) {
                float* buf = pool_.get(entry.output_buf_indices[out_i]);
                float val = outputs[out_i].control;
                buf[0] = val;
                if (!outputs[out_i].control_per_sample) {
                    for (int s = 1; s < ctx.block_size; ++s) buf[s] = val;
                }
            }
            out_i++;
        }

        auto* adapter = dynamic_cast<PluginAdapterNode*>(entry.node.get());
        if (adapter) {
            for (auto& [port_id, events] : adapter->event_outputs()) {
                if (events.empty()) continue;
                for (auto& c : connections_) {
                    if (c.from_node != node_id || c.from_port != port_id) continue;
                    auto dest_it = node_index_.find(c.to_node);
                    if (dest_it == node_index_.end()) continue;
                    Node* dest = nodes_[dest_it->second].node.get();
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

void Graph::notify_transport_stop() {
    for (auto& entry : nodes_) {
        auto* adapter = dynamic_cast<PluginAdapterNode*>(entry.node.get());
        if (adapter && adapter->plugin())
            adapter->plugin()->on_transport_stop();
    }
}

Node* Graph::find_node(const std::string& id) const {
    auto it = node_index_.find(id);
    if (it == node_index_.end()) return nullptr;
    return nodes_[it->second].node.get();
}
