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

PluginAdapterNode::~PluginAdapterNode() = default;

// ---------------------------------------------------------------------------
// Port mapping
// ---------------------------------------------------------------------------

void PluginAdapterNode::build_port_mapping() {
    audio_map_.clear();
    control_map_.clear();
    event_map_.clear();
    pattern_map_.clear();

    int decl_index = 0;

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
            m.has_pending = !is_out;
            control_map_.push_back(std::move(m));
            break;
        }
        case PluginPortType::Event: {
            EventPortMapping m;
            m.plugin_port_id = pd.id;
            m.is_output      = is_out;
            event_map_.push_back(std::move(m));
            // No PortDecl — handled via note_on/off virtuals (in) or
            // event_outputs() (out).
            break;
        }
        case PluginPortType::Pattern: {
            PatternPortMapping m;
            m.plugin_port_id = pd.id;
            m.is_output      = is_out;
            m.data           = nullptr;
            pattern_map_.push_back(std::move(m));
            // No PortDecl — wired by Graph::activate() via set_pattern().
            // Pattern ports are never in the PortBuffer flat arrays.
            break;
        }
        }
    }

    // Pre-allocate PluginBuffers entries
    buffers_.audio.entries.clear();
    buffers_.control.entries.clear();
    buffers_.events.entries.clear();
    buffers_.patterns.entries.clear();

    for (auto& m : audio_map_)
        buffers_.audio.entries.push_back({m.plugin_port_id, {}});
    for (auto& m : control_map_)
        buffers_.control.entries.push_back({m.plugin_port_id, {}});
    for (auto& m : event_map_)
        buffers_.events.entries.push_back({m.plugin_port_id, {}});
    for (auto& m : pattern_map_)
        buffers_.patterns.entries.push_back({m.plugin_port_id, {}});

    event_output_storage_.clear();
    for (auto& m : event_map_) {
        if (m.is_output)
            event_output_storage_.push_back({m.plugin_port_id, {}});
    }
}

// ---------------------------------------------------------------------------
// declare_ports
// ---------------------------------------------------------------------------

std::vector<Node::PortDecl> PluginAdapterNode::declare_ports() const {
    std::vector<PortDecl> decls;

    for (auto& pd : desc_.ports) {
        bool is_out = (pd.role == PortRole::Output ||
                       pd.role == PortRole::Monitor);

        switch (pd.type) {
        case PluginPortType::AudioMono:
            decls.push_back({pd.id, PortType::AudioMono, is_out,
                             pd.default_value, pd.min_value, pd.max_value});
            break;
        case PluginPortType::AudioStereo:
            decls.push_back({pd.id + "_L", PortType::AudioMono, is_out});
            decls.push_back({pd.id + "_R", PortType::AudioMono, is_out});
            break;
        case PluginPortType::Control:
            decls.push_back({pd.id, PortType::Control, is_out,
                             pd.default_value, pd.min_value, pd.max_value});
            break;
        case PluginPortType::Event:
        case PluginPortType::Pattern:
            // Neither creates a PortDecl in the flat buffer arrays.
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
// set_pattern — called by Graph::activate() to inject pattern data
// ---------------------------------------------------------------------------

void PluginAdapterNode::set_pattern(const std::string& port_id, const PatternData* data) {
    for (auto& m : pattern_map_) {
        if (m.plugin_port_id == port_id && !m.is_output) {
            m.data = data;
            // Also update the pre-allocated buffer entry so process() sees it.
            for (auto& entry : buffers_.patterns.entries) {
                if (entry.first == port_id) {
                    entry.second.pattern = data;
                    break;
                }
            }
            return;
        }
    }
    AS_LOG("plugin", "PluginAdapterNode '%s': unknown pattern port '%s'",
           id.c_str(), port_id.c_str());
}

// ---------------------------------------------------------------------------
// process
// ---------------------------------------------------------------------------

void PluginAdapterNode::process(
    const ProcessContext& ctx,
    const std::vector<PortBuffer>& inputs,
    std::vector<PortBuffer>&       outputs)
{
    PluginProcessContext pctx;
    pctx.block_size        = ctx.block_size;
    pctx.sample_rate       = ctx.sample_rate;
    pctx.bpm               = ctx.bpm;
    pctx.beat_position     = ctx.beat_position;
    pctx.beats_per_sample  = ctx.beats_per_sample;
    pctx.is_playing        = ctx.is_playing;
    pctx.transport_started = ctx.transport_started;
    pctx.transport_stopped = ctx.transport_stopped;

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
                out_i++;
            } else {
                cb.value = inputs[in_i++].control;
                if (control_map_[ctrl_map_i].has_pending &&
                    !control_map_[ctrl_map_i].is_connected) {
                    cb.value = control_map_[ctrl_map_i].pending_value->load(
                        std::memory_order_relaxed);
                }
            }
            ctrl_map_i++;
            break;
        }
        case PluginPortType::Event:
        case PluginPortType::Pattern:
            // No PortDecl slots; buffers pre-filled below / by set_pattern().
            break;
        }
    }

    // Wire event buffers
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

    // Pattern buffers are updated in set_pattern() at activate time and
    // remain stable; nothing to do per-block.

    plugin_->process(pctx, buffers_);

    // Write back control outputs
    out_i = 0;
    ctrl_map_i = 0;
    for (auto& pd : desc_.ports) {
        bool is_out = (pd.role == PortRole::Output ||
                       pd.role == PortRole::Monitor);
        if (!is_out) continue;

        switch (pd.type) {
        case PluginPortType::AudioMono:   out_i++;   break;
        case PluginPortType::AudioStereo: out_i += 2; break;
        case PluginPortType::Control: {
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
        case PluginPortType::Pattern:
            break;
        }
    }

    event_input_accum_.clear();
}

// ---------------------------------------------------------------------------
// Parameter control
// ---------------------------------------------------------------------------

void PluginAdapterNode::set_control_connected(const std::string& port_id, bool connected) {
    for (auto& m : control_map_) {
        if (m.plugin_port_id == port_id && !m.is_output) {
            m.is_connected = connected;
            return;
        }
    }
}

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
// MIDI events
// ---------------------------------------------------------------------------

void PluginAdapterNode::note_on(int channel, int pitch, int velocity) {
    MidiEvent ev;
    ev.frame   = 0;
    ev.status  = 0x90 | (channel & 0x0F);
    ev.data1   = static_cast<uint8_t>(pitch);
    ev.data2   = static_cast<uint8_t>(velocity);
    ev.channel = static_cast<uint8_t>(channel);
    event_input_accum_.push_back(ev);
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

void PluginAdapterNode::note_tune(int channel, int note, float semitones) {
    plugin_->note_tune(channel, note, semitones);
}

void PluginAdapterNode::push_control(double beat, float normalized_value) {
    for (auto& m : control_map_) {
        if (!m.is_output) {
            m.pending_value->store(normalized_value, std::memory_order_relaxed);
            m.has_pending = true;
            return;
        }
    }
}
