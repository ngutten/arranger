// plugin_adapter.cpp
// Bridges a Plugin (new API) into the existing Node-based graph engine.

#include "plugin_adapter.h"
#include "debug.h"
#include <cstring>
#include <algorithm>

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

PluginAdapterNode::PluginAdapterNode(const std::string& node_id,
                                     std::unique_ptr<Plugin> plugin)
    : plugin_(std::move(plugin))
    , desc_(plugin_->descriptor())
{
    id = node_id;
    build_port_mapping();
}

PluginAdapterNode::~PluginAdapterNode() {
    // Plugin is destroyed via unique_ptr.
}

// ---------------------------------------------------------------------------
// Port mapping
// ---------------------------------------------------------------------------
// Translates the plugin's PortDescriptor list into:
//   1. A flat vector of Node::PortDecl (what the graph engine sees)
//   2. Mapping tables so process() can wire PluginBuffers from PortBuffer arrays

void PluginAdapterNode::build_port_mapping() {
    audio_map_.clear();
    control_map_.clear();
    event_map_.clear();

    // We'll accumulate PortDecls in declaration order, tracking input/output
    // indices separately (the graph engine separates them).
    int decl_index = 0;  // running index into the PortDecl vector

    for (auto& pd : desc_.ports) {
        bool is_out = (pd.role == PortRole::Output ||
                       pd.role == PortRole::Monitor);

        switch (pd.type) {
        case PluginPortType::AudioMono: {
            AudioPortMapping m;
            m.plugin_port_id  = pd.id;
            m.is_stereo       = false;
            m.is_output       = is_out;
            m.left_decl_index = decl_index++;
            m.right_decl_index = -1;
            audio_map_.push_back(std::move(m));
            break;
        }
        case PluginPortType::AudioStereo: {
            AudioPortMapping m;
            m.plugin_port_id  = pd.id;
            m.is_stereo       = true;
            m.is_output       = is_out;
            m.left_decl_index  = decl_index++;
            m.right_decl_index = decl_index++;
            audio_map_.push_back(std::move(m));
            break;
        }
        case PluginPortType::Control: {
            ControlPortMapping m;
            m.plugin_port_id  = pd.id;
            m.is_output       = is_out;
            m.decl_index      = decl_index++;
            m.pending_value->store(pd.default_value, std::memory_order_relaxed);
            m.has_pending      = false;
            control_map_.push_back(std::move(m));
            break;
        }
        case PluginPortType::Event: {
            EventPortMapping m;
            m.plugin_port_id = pd.id;
            m.is_output      = is_out;
            event_map_.push_back(std::move(m));
            // Event ports do NOT create PortDecls — they use the Node event
            // virtual methods for input, and the engine reads event_outputs()
            // after process() for output.
            break;
        }
        }
    }

    // Pre-allocate PluginBuffers entries
    buffers_.audio.entries.clear();
    buffers_.control.entries.clear();
    buffers_.events.entries.clear();

    for (auto& m : audio_map_)
        buffers_.audio.entries.push_back({m.plugin_port_id, {}});
    for (auto& m : control_map_)
        buffers_.control.entries.push_back({m.plugin_port_id, {}});
    for (auto& m : event_map_) {
        buffers_.events.entries.push_back({m.plugin_port_id, {}});
    }

    // Pre-allocate event output storage
    event_output_storage_.clear();
    for (auto& m : event_map_) {
        if (m.is_output) {
            event_output_storage_.push_back({m.plugin_port_id, {}});
        }
    }
}

// ---------------------------------------------------------------------------
// declare_ports — expose to graph engine
// ---------------------------------------------------------------------------

std::vector<Node::PortDecl> PluginAdapterNode::declare_ports() const {
    std::vector<PortDecl> decls;

    for (auto& pd : desc_.ports) {
        bool is_out = (pd.role == PortRole::Output ||
                       pd.role == PortRole::Monitor);

        switch (pd.type) {
        case PluginPortType::AudioMono:
            decls.push_back({
                pd.id, PortType::AudioMono, is_out,
                pd.default_value, pd.min_value, pd.max_value
            });
            break;

        case PluginPortType::AudioStereo:
            // Expand to two mono ports: id_L and id_R
            decls.push_back({
                pd.id + "_L", PortType::AudioMono, is_out
            });
            decls.push_back({
                pd.id + "_R", PortType::AudioMono, is_out
            });
            break;

        case PluginPortType::Control:
            decls.push_back({
                pd.id, PortType::Control, is_out,
                pd.default_value, pd.min_value, pd.max_value
            });
            break;

        case PluginPortType::Event:
            // Event ports don't create graph-level PortDecls.
            // Input events come via note_on/off virtuals.
            // Output events are read from event_outputs() by the engine.
            break;
        }
    }

    return decls;
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

void PluginAdapterNode::activate(float sample_rate, int max_block_size) {
    AS_LOG("plugin", "PluginAdapterNode '%s' activate (sr=%.0f, bs=%d)",
           id.c_str(), sample_rate, max_block_size);
    plugin_->activate(sample_rate, max_block_size);
}

void PluginAdapterNode::deactivate() {
    AS_LOG("plugin", "PluginAdapterNode '%s' deactivate", id.c_str());
    plugin_->deactivate();
}

// ---------------------------------------------------------------------------
// process — the hot path
// ---------------------------------------------------------------------------

void PluginAdapterNode::process(
    const ProcessContext& ctx,
    const std::vector<PortBuffer>& inputs,
    std::vector<PortBuffer>&       outputs)
{
    // Translate ProcessContext
    PluginProcessContext pctx;
    pctx.block_size       = ctx.block_size;
    pctx.sample_rate      = ctx.sample_rate;
    pctx.bpm              = ctx.bpm;
    pctx.beat_position    = ctx.beat_position;
    pctx.beats_per_sample = ctx.beats_per_sample;

    // --- Wire buffers ---
    // We must walk the descriptor in the same order as declare_ports()
    // because the graph engine builds inputs[] and outputs[] by splitting
    // the PortDecl list into is_output=false and is_output=true sequences.
    int in_i = 0, out_i = 0;
    int audio_map_i = 0, ctrl_map_i = 0;

    for (auto& pd : desc_.ports) {
        bool is_out = (pd.role == PortRole::Output ||
                       pd.role == PortRole::Monitor);

        switch (pd.type) {
        case PluginPortType::AudioMono: {
            auto& ab = buffers_.audio.entries[audio_map_i].second;
            ab.frames = ctx.block_size;
            if (is_out) {
                ab.left = outputs[out_i++].audio;
                ab.right = nullptr;
                std::memset(ab.left, 0, ctx.block_size * sizeof(float));
            } else {
                ab.left = const_cast<float*>(inputs[in_i++].audio);
                ab.right = nullptr;
            }
            audio_map_i++;
            break;
        }
        case PluginPortType::AudioStereo: {
            auto& ab = buffers_.audio.entries[audio_map_i].second;
            ab.frames = ctx.block_size;
            if (is_out) {
                ab.left  = outputs[out_i++].audio;
                ab.right = outputs[out_i++].audio;
                std::memset(ab.left,  0, ctx.block_size * sizeof(float));
                std::memset(ab.right, 0, ctx.block_size * sizeof(float));
            } else {
                ab.left  = const_cast<float*>(inputs[in_i++].audio);
                ab.right = const_cast<float*>(inputs[in_i++].audio);
            }
            audio_map_i++;
            break;
        }
        case PluginPortType::Control: {
            auto& cb = buffers_.control.entries[ctrl_map_i].second;
            if (is_out) {
                cb.value = 0.0f;
                out_i++;  // reserve the output slot
            } else {
                // Read from graph connection
                cb.value = inputs[in_i++].control;
                // Override with set_param() value if pending
                if (control_map_[ctrl_map_i].has_pending) {
                    cb.value = control_map_[ctrl_map_i].pending_value->load(
                        std::memory_order_relaxed);
                }
            }
            ctrl_map_i++;
            break;
        }
        case PluginPortType::Event:
            // Event ports don't have PortDecl slots
            break;
        }
    }

    // --- Wire event buffers ---
    int evt_out_i = 0;
    for (size_t i = 0; i < event_map_.size(); ++i) {
        auto& eb = buffers_.events.entries[i].second;
        if (event_map_[i].is_output) {
            event_output_storage_[evt_out_i].second.clear();
            eb.output_events = &event_output_storage_[evt_out_i].second;
            eb.events = nullptr;
            evt_out_i++;
        } else {
            eb.events = &event_input_accum_;
            eb.output_events = nullptr;
        }
    }

    // --- Call plugin process ---
    plugin_->process(pctx, buffers_);

    // --- Write back control output values ---
    out_i = 0;
    ctrl_map_i = 0;
    for (auto& pd : desc_.ports) {
        bool is_out = (pd.role == PortRole::Output ||
                       pd.role == PortRole::Monitor);
        if (!is_out) continue;

        switch (pd.type) {
        case PluginPortType::AudioMono:
            out_i++;
            break;
        case PluginPortType::AudioStereo:
            out_i += 2;
            break;
        case PluginPortType::Control: {
            // Find which control_map_ entry this is
            for (size_t ci = 0; ci < control_map_.size(); ++ci) {
                if (control_map_[ci].plugin_port_id == pd.id) {
                    outputs[out_i].control =
                        buffers_.control.entries[ci].second.value;
                    break;
                }
            }
            out_i++;
            break;
        }
        case PluginPortType::Event:
            break;
        }
    }

    // Clear input event accumulator for next block
    event_input_accum_.clear();
}

// ---------------------------------------------------------------------------
// Parameter control
// ---------------------------------------------------------------------------

void PluginAdapterNode::set_param(const std::string& name, float value) {
    for (auto& m : control_map_) {
        if (m.plugin_port_id == name && !m.is_output) {
            m.pending_value->store(value, std::memory_order_relaxed);
            m.has_pending = true;
            return;
        }
    }
    AS_LOG("plugin", "PluginAdapterNode '%s': unknown param '%s'",
           id.c_str(), name.c_str());
}

// ---------------------------------------------------------------------------
// MIDI events (from TrackSourceNode fan-out)
// ---------------------------------------------------------------------------
// These are called on the audio thread before process().
// We accumulate them and make them available in the EventPortBuffer.
// We also forward them to the plugin's convenience virtuals.

void PluginAdapterNode::note_on(int channel, int pitch, int velocity) {
    // Accumulate for EventPortBuffer
    MidiEvent ev;
    ev.frame   = 0;  // start of block (we don't have sub-block timing here)
    ev.status  = 0x90 | (channel & 0x0F);
    ev.data1   = static_cast<uint8_t>(pitch);
    ev.data2   = static_cast<uint8_t>(velocity);
    ev.channel = static_cast<uint8_t>(channel);
    event_input_accum_.push_back(ev);

    // Also call the convenience method
    plugin_->note_on(channel, pitch, velocity);
}

void PluginAdapterNode::note_off(int channel, int pitch) {
    MidiEvent ev;
    ev.frame   = 0;
    ev.status  = 0x80 | (channel & 0x0F);
    ev.data1   = static_cast<uint8_t>(pitch);
    ev.data2   = 0;
    ev.channel = static_cast<uint8_t>(channel);
    event_input_accum_.push_back(ev);

    plugin_->note_off(channel, pitch);
}

void PluginAdapterNode::all_notes_off(int channel) {
    plugin_->all_notes_off(channel);
}

void PluginAdapterNode::program_change(int channel, int bank, int program) {
    plugin_->program_change(channel, bank, program);
}

void PluginAdapterNode::pitch_bend(int channel, int value) {
    MidiEvent ev;
    ev.frame   = 0;
    ev.status  = 0xE0 | (channel & 0x0F);
    ev.data1   = static_cast<uint8_t>(value & 0x7F);
    ev.data2   = static_cast<uint8_t>((value >> 7) & 0x7F);
    ev.channel = static_cast<uint8_t>(channel);
    event_input_accum_.push_back(ev);

    plugin_->pitch_bend(channel, value);
}

void PluginAdapterNode::channel_volume(int channel, int volume) {
    plugin_->channel_volume(channel, volume);
}

void PluginAdapterNode::push_control(double beat, float normalized_value) {
    // Forward to the first non-output control port
    for (auto& m : control_map_) {
        if (!m.is_output) {
            m.pending_value->store(normalized_value, std::memory_order_relaxed);
            m.has_pending = true;
            return;
        }
    }
}
