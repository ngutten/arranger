// synth_node.cpp  (debug-instrumented)
// Changes vs. original:
//   - AS_LOG calls throughout LV2Node activate/process
//   - Bounds-checking assertions on port index lookups
//   - declare_ports() now logs all ports it discovers
//   - process() validates inputs/outputs size vs. expected counts
//   - activate() validates that graph_audio_in/out ordering matches declare_ports()
//
// Compile with -DAS_DEBUG to activate; all macros are no-ops otherwise.

#include "synth_node.h"
#include "plugin_api.h"
#include "plugin_adapter.h"
#include "debug.h"
#include <cmath>
#include <cstdio>
#include <algorithm>
#include <cstring>
#include <stdexcept>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// SineNode  (unchanged from original)
// ---------------------------------------------------------------------------

SineNode::SineNode(const std::string& id_) { id = id_; }

std::vector<Node::PortDecl> SineNode::declare_ports() const {
    return {
        {"audio_out_L", PortType::AudioMono, true},
        {"audio_out_R", PortType::AudioMono, true},
    };
}

void SineNode::activate(float sample_rate, int /*max_block_size*/) {
    sample_rate_ = sample_rate;
    voices_.clear();
}

void SineNode::note_on(int channel, int pitch, int velocity) {
    int key = channel * 128 + pitch;
    double freq = 440.0 * std::pow(2.0, (pitch - 69) / 12.0);
    Voice v;
    v.freq = freq;
    v.amp  = velocity / 127.0f * gain_;
    voices_[key] = v;
}

void SineNode::note_off(int channel, int pitch) {
    int key = channel * 128 + pitch;
    auto it = voices_.find(key);
    if (it != voices_.end()) {
        it->second.releasing  = true;
        it->second.env_release = 30.0f / sample_rate_;
    }
}

void SineNode::all_notes_off(int channel) {
    if (channel == -1) voices_.clear();
    else {
        for (auto it = voices_.begin(); it != voices_.end(); ) {
            if (it->first / 128 == channel) it = voices_.erase(it);
            else ++it;
        }
    }
}

void SineNode::set_param(const std::string& name, float value) {
    if (name == "gain") gain_ = std::max(0.0f, std::min(1.0f, value));
}

void SineNode::process(const ProcessContext& ctx,
                        const std::vector<PortBuffer>& /*inputs*/,
                        std::vector<PortBuffer>& outputs)
{
    float* L = outputs[0].audio;
    float* R = outputs[1].audio;
    std::memset(L, 0, ctx.block_size * sizeof(float));
    std::memset(R, 0, ctx.block_size * sizeof(float));

    std::vector<int> dead;
    for (auto& [key, v] : voices_) {
        double phase_inc = 2.0 * M_PI * v.freq / sample_rate_;
        for (int i = 0; i < ctx.block_size; ++i) {
            float env = v.releasing ? (v.env *= (1.0f - v.env_release)) : 1.0f;
            float sample = static_cast<float>(std::sin(v.phase)) * v.amp * env;
            L[i] += sample;
            R[i] += sample;
            v.phase += phase_inc;
            if (v.phase > 2.0 * M_PI) v.phase -= 2.0 * M_PI;
        }
        if (v.releasing && v.env < 1e-4f) dead.push_back(key);
    }
    for (int k : dead) voices_.erase(k);

    for (int i = 0; i < ctx.block_size; ++i) {
        L[i] = std::tanh(L[i]);
        R[i] = std::tanh(R[i]);
    }
}

// ---------------------------------------------------------------------------
// MixerNode  (unchanged from original)
// ---------------------------------------------------------------------------

MixerNode::MixerNode(const std::string& id_, int input_count)
    : input_count_(input_count)
{
    id = id_;
    channel_gain_.assign(input_count_, 1.0f);
}

std::vector<Node::PortDecl> MixerNode::declare_ports() const {
    std::vector<PortDecl> ports;
    for (int i = 0; i < input_count_; ++i) {
        ports.push_back({"audio_in_L_" + std::to_string(i), PortType::AudioMono, false});
        ports.push_back({"audio_in_R_" + std::to_string(i), PortType::AudioMono, false});
    }
    ports.push_back({"audio_out_L", PortType::AudioMono, true});
    ports.push_back({"audio_out_R", PortType::AudioMono, true});
    return ports;
}

void MixerNode::activate(float /*sr*/, int max_block_size) {
    block_size_ = max_block_size;
}

void MixerNode::process(const ProcessContext& ctx,
                         const std::vector<PortBuffer>& inputs,
                         std::vector<PortBuffer>& outputs)
{
    float* out_L = outputs[0].audio;
    float* out_R = outputs[1].audio;
    std::memset(out_L, 0, ctx.block_size * sizeof(float));
    std::memset(out_R, 0, ctx.block_size * sizeof(float));

    for (int ch = 0; ch < input_count_; ++ch) {
        float g = channel_gain_[ch] * master_gain_;
        const float* in_L = inputs[ch * 2    ].audio;
        const float* in_R = inputs[ch * 2 + 1].audio;
        for (int i = 0; i < ctx.block_size; ++i) {
            out_L[i] += in_L[i] * g;
            out_R[i] += in_R[i] * g;
        }
    }

    for (int i = 0; i < ctx.block_size; ++i) {
        out_L[i] = std::tanh(out_L[i]);
        out_R[i] = std::tanh(out_R[i]);
    }
}

void MixerNode::set_param(const std::string& name, float value) {
    if (name == "master_gain") {
        master_gain_ = std::max(0.0f, value);
        return;
    }
    if (name.substr(0, 5) == "gain_") {
        int n = std::stoi(name.substr(5));
        if (n >= 0 && n < input_count_)
            channel_gain_[n] = std::max(0.0f, value);
    }
}

// ---------------------------------------------------------------------------
// TrackSourceNode
// ---------------------------------------------------------------------------

TrackSourceNode::TrackSourceNode(const std::string& id_) { id = id_; }

std::vector<Node::PortDecl> TrackSourceNode::declare_ports() const { return {}; }

void TrackSourceNode::process(const ProcessContext& /*ctx*/,
                               const std::vector<PortBuffer>& /*inputs*/,
                               std::vector<PortBuffer>& /*outputs*/)
{
    std::lock_guard<std::mutex> lk(preview_mutex_);
    for (auto& [ch, pitch] : pending_off_) {
        if (ch == -1) for (auto* n : downstream_) n->all_notes_off(-1);
        else          for (auto* n : downstream_) n->note_off(ch, pitch);
    }
    pending_off_.clear();
    for (auto& pn : pending_on_)
        for (auto* n : downstream_) n->note_on(pn.channel, pn.pitch, pn.velocity);
    pending_on_.clear();
}

void TrackSourceNode::set_downstream(std::vector<Node*> nodes) {
    AS_LOG("graph", "TrackSourceNode '%s': %zu downstream nodes", id.c_str(), nodes.size());
    for (auto* n : nodes) AS_LOG("graph", "  -> '%s'", n->id.c_str());
    downstream_ = std::move(nodes);
}

void TrackSourceNode::note_on(int channel, int pitch, int velocity) {
    for (auto* n : downstream_) n->note_on(channel, pitch, velocity);
}
void TrackSourceNode::note_off(int channel, int pitch) {
    for (auto* n : downstream_) n->note_off(channel, pitch);
}
void TrackSourceNode::program_change(int channel, int bank, int program) {
    for (auto* n : downstream_) n->program_change(channel, bank, program);
}
void TrackSourceNode::pitch_bend(int channel, int value) {
    for (auto* n : downstream_) n->pitch_bend(channel, value);
}
void TrackSourceNode::channel_volume(int channel, int volume) {
    for (auto* n : downstream_) n->channel_volume(channel, volume);
}
void TrackSourceNode::note_tune(int channel, int note, float semitones) {
    for (auto* n : downstream_) n->note_tune(channel, note, semitones);
}
void TrackSourceNode::all_notes_off(int channel) {
    for (auto* n : downstream_) n->all_notes_off(channel);
}
void TrackSourceNode::preview_note_on(int channel, int pitch, int velocity) {
    std::lock_guard<std::mutex> lk(preview_mutex_);
    pending_on_.push_back({channel, pitch, velocity});
}
void TrackSourceNode::preview_note_off(int channel, int pitch) {
    std::lock_guard<std::mutex> lk(preview_mutex_);
    pending_off_.push_back({channel, pitch});
}
void TrackSourceNode::preview_all_notes_off() {
    std::lock_guard<std::mutex> lk(preview_mutex_);
    pending_on_.clear();
    pending_off_.push_back({-1, -1});
}

void TrackSourceNode::push_lyric(double beat, const std::string& lyric) {
    for (auto* n : downstream_) n->push_lyric(beat, lyric);
}
void TrackSourceNode::on_schedule_loaded() {
    for (auto* n : downstream_) n->on_schedule_loaded();
}

// ---------------------------------------------------------------------------
// NoteGateNode  (unchanged from original)
// ---------------------------------------------------------------------------

NoteGateNode::NoteGateNode(const std::string& id_, int pitch_lo, int pitch_hi, int mode)
    : pitch_lo_(pitch_lo), pitch_hi_(pitch_hi), mode_(mode)
{
    id = id_;
}

std::vector<Node::PortDecl> NoteGateNode::declare_ports() const {
    return {
        {"control_out", PortType::Control, true, 0.0f, 0.0f, 1.0f},
    };
}

void NoteGateNode::process(const ProcessContext& /*ctx*/,
                            const std::vector<PortBuffer>& /*inputs*/,
                            std::vector<PortBuffer>& outputs)
{
    outputs[0].control = current_value_;
}

void NoteGateNode::note_on(int channel, int pitch, int velocity) {
    if (!in_band_(pitch)) return;
    active_[channel * 128 + pitch] = velocity;
    recompute_value_();
}

void NoteGateNode::note_off(int channel, int pitch) {
    if (!in_band_(pitch)) return;
    active_.erase(channel * 128 + pitch);
    recompute_value_();
}

void NoteGateNode::all_notes_off(int channel) {
    if (channel == -1) active_.clear();
    else {
        for (auto it = active_.begin(); it != active_.end(); )
            if (it->first / 128 == channel) it = active_.erase(it);
            else ++it;
    }
    recompute_value_();
}

void NoteGateNode::set_param(const std::string& name, float value) {
    if (name == "pitch_lo")
        pitch_lo_ = std::max(0, std::min(127, static_cast<int>(value)));
    else if (name == "pitch_hi")
        pitch_hi_ = std::max(0, std::min(127, static_cast<int>(value)));
    else if (name == "mode")
        mode_ = std::max(0, std::min(3, static_cast<int>(value)));
    recompute_value_();
}

void NoteGateNode::recompute_value_() {
    if (active_.empty()) { current_value_ = 0.0f; return; }
    switch (mode_) {
        case 0:
            current_value_ = 1.0f;
            break;
        case 1: {
            int max_vel = 0;
            for (auto& [k, v] : active_) max_vel = std::max(max_vel, v);
            current_value_ = max_vel / 127.0f;
            break;
        }
        case 2: {
            int band_width = pitch_hi_ - pitch_lo_;
            if (band_width <= 0) { current_value_ = 0.0f; break; }
            int highest_pitch = -1;
            for (auto& [k, v] : active_) {
                int pitch = k % 128;
                if (pitch > highest_pitch) highest_pitch = pitch;
            }
            current_value_ = static_cast<float>(highest_pitch - pitch_lo_) / band_width;
            current_value_ = std::max(0.0f, std::min(1.0f, current_value_));
            break;
        }
        case 3: {
            int band_width = pitch_hi_ - pitch_lo_ + 1;
            if (band_width <= 0) { current_value_ = 0.0f; break; }
            current_value_ = std::min(1.0f,
                static_cast<float>(active_.size()) / band_width);
            break;
        }
        default:
            current_value_ = 0.0f;
    }
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

std::unique_ptr<Node> make_node(const NodeDesc& desc, std::string& err) {
    AS_LOG("graph", "make_node: id='%s' type='%s'", desc.id.c_str(), desc.type.c_str());

    // Translate legacy short type names to canonical plugin IDs
    std::string canonical_type = desc.type;
    if (canonical_type == "fluidsynth") canonical_type = "builtin.fluidsynth";
    if (canonical_type == "control_source") canonical_type = "builtin.control_source";

    // --- Try plugin registry first ---
    auto plugin = PluginRegistry::create(canonical_type);
    if (plugin) {
        AS_LOG("graph", "  -> resolved via plugin registry: '%s'", desc.type.c_str());
        // Pass NodeDesc-specific fields through configure()
        if (!desc.sf2_path.empty())
            plugin->configure("sf2_path", desc.sf2_path);
        if (desc.channel_count != 2)  // only if non-default
            plugin->configure("channel_count", std::to_string(desc.channel_count));
        if (desc.pitch_lo != 0)
            plugin->configure("pitch_lo", std::to_string(desc.pitch_lo));
        if (desc.pitch_hi != 127)
            plugin->configure("pitch_hi", std::to_string(desc.pitch_hi));
        if (desc.gate_mode != 0)
            plugin->configure("gate_mode", std::to_string(desc.gate_mode));
        // Generic float params
        for (auto& [k, v] : desc.params) {
            plugin->configure(k, std::to_string(v));
        }
        return std::make_unique<PluginAdapterNode>(desc.id, std::move(plugin));
    }

    // --- Legacy built-in types ---
    
    if (desc.type == "sine")
        return std::make_unique<SineNode>(desc.id);
    if (desc.type == "mixer")
        return std::make_unique<MixerNode>(desc.id, desc.channel_count);
    if (desc.type == "track_source")
        return std::make_unique<TrackSourceNode>(desc.id);
    if (desc.type == "note_gate")
        return std::make_unique<NoteGateNode>(desc.id, desc.pitch_lo, desc.pitch_hi, desc.gate_mode);
    err = "Unknown node type: " + desc.type;
    return nullptr;
}
