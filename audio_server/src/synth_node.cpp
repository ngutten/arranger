// synth_node.cpp
#include "synth_node.h"
#include <cmath>
#include <algorithm>
#include <cstring>
#include <stdexcept>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// SineNode
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
        it->second.env_release = 30.0f / sample_rate_;  // ~33ms release
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

    // Soft clip
    for (int i = 0; i < ctx.block_size; ++i) {
        L[i] = std::tanh(L[i]);
        R[i] = std::tanh(R[i]);
    }
}

// ---------------------------------------------------------------------------
// MixerNode
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

    // Master soft clip
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
    // "gain_N" → channel N
    if (name.substr(0, 5) == "gain_") {
        int n = std::stoi(name.substr(5));
        if (n >= 0 && n < input_count_)
            channel_gain_[n] = std::max(0.0f, value);
    }
}

// ---------------------------------------------------------------------------
// ControlSourceNode
// ---------------------------------------------------------------------------

ControlSourceNode::ControlSourceNode(const std::string& id_) { id = id_; }

std::vector<Node::PortDecl> ControlSourceNode::declare_ports() const {
    return {
        {"control_out", PortType::Control, true, 0.0f, 0.0f, 1.0f},
    };
}

void ControlSourceNode::push_control(double beat, float value) {
    int wi = write_idx_.load(std::memory_order_relaxed);
    ring_[wi % RING_SIZE] = {beat, value};
    write_idx_.store(wi + 1, std::memory_order_release);
}

void ControlSourceNode::process(const ProcessContext& /*ctx*/,
                                  const std::vector<PortBuffer>& /*inputs*/,
                                  std::vector<PortBuffer>& outputs)
{
    int wi = write_idx_.load(std::memory_order_acquire);
    while (read_idx_ < wi) {
        current_ = ring_[read_idx_ % RING_SIZE].value;
        read_idx_++;
    }
    outputs[0].control = current_;
}

// ---------------------------------------------------------------------------
// FluidSynthNode
// ---------------------------------------------------------------------------

#ifdef AS_ENABLE_SF2
#include <fluidsynth.h>

FluidSynthNode::FluidSynthNode(const std::string& id_, const std::string& sf2_path)
    : sf2_path_(sf2_path)
{
    id = id_;
}

FluidSynthNode::~FluidSynthNode() { deactivate(); }

std::vector<Node::PortDecl> FluidSynthNode::declare_ports() const {
    return {
        {"audio_out_L", PortType::AudioMono, true},
        {"audio_out_R", PortType::AudioMono, true},
    };
}

void FluidSynthNode::activate(float sample_rate, int max_block_size) {
    sample_rate_ = sample_rate;
    block_size_  = max_block_size;

    fset_ = new_fluid_settings();
    fluid_settings_setnum(static_cast<fluid_settings_t*>(fset_),
                          "synth.sample-rate", sample_rate);
    fluid_settings_setnum(static_cast<fluid_settings_t*>(fset_),
                          "synth.gain", 0.15);
    fluid_settings_setint(static_cast<fluid_settings_t*>(fset_),
                          "synth.threadsafe-api", 0);

    fs_ = new_fluid_synth(static_cast<fluid_settings_t*>(fset_));
    sfid_ = fluid_synth_sfload(static_cast<fluid_synth_t*>(fs_),
                                sf2_path_.c_str(), 1);
    if (sfid_ == FLUID_FAILED)
        throw std::runtime_error("FluidSynth: failed to load " + sf2_path_);

    // Pre-select program 0 on melodic channels
    for (int ch = 0; ch < 16; ++ch)
        if (ch != 9)
            fluid_synth_program_select(static_cast<fluid_synth_t*>(fs_),
                                       ch, sfid_, 0, 0);

    raw_buf_.resize(max_block_size * 2);  // stereo int16
}

void FluidSynthNode::deactivate() {
    if (fs_)   { delete_fluid_synth(static_cast<fluid_synth_t*>(fs_));     fs_   = nullptr; }
    if (fset_) { delete_fluid_settings(static_cast<fluid_settings_t*>(fset_)); fset_ = nullptr; }
    sfid_ = -1;
}

void FluidSynthNode::note_on(int ch, int pitch, int vel) {
    fluid_synth_noteon(static_cast<fluid_synth_t*>(fs_), ch, pitch, vel);
}
void FluidSynthNode::note_off(int ch, int pitch) {
    fluid_synth_noteoff(static_cast<fluid_synth_t*>(fs_), ch, pitch);
}
void FluidSynthNode::program_change(int ch, int bank, int prog) {
    fluid_synth_program_select(static_cast<fluid_synth_t*>(fs_), ch, sfid_, bank, prog);
}
void FluidSynthNode::pitch_bend(int ch, int value) {
    // value is 14-bit unsigned (8192=center); FluidSynth wants signed (-8192..+8191)
    fluid_synth_pitch_bend(static_cast<fluid_synth_t*>(fs_), ch, value);
}
void FluidSynthNode::channel_volume(int ch, int vol) {
    fluid_synth_cc(static_cast<fluid_synth_t*>(fs_), ch, 7, std::max(0, std::min(127, vol)));
}
void FluidSynthNode::all_notes_off(int channel) {
    auto* fs = static_cast<fluid_synth_t*>(fs_);
    if (channel == -1) {
        for (int ch = 0; ch < 16; ++ch) {
            fluid_synth_cc(fs, ch, 123, 0);
            fluid_synth_cc(fs, ch, 120, 0);
        }
    } else {
        fluid_synth_cc(fs, channel, 123, 0);
        fluid_synth_cc(fs, channel, 120, 0);
    }
}

void FluidSynthNode::process(const ProcessContext& ctx,
                               const std::vector<PortBuffer>& /*inputs*/,
                               std::vector<PortBuffer>& outputs)
{
    auto* fs = static_cast<fluid_synth_t*>(fs_);
    float* L = outputs[0].audio;
    float* R = outputs[1].audio;

    // FluidSynth renders non-interleaved float directly
    fluid_synth_write_float(fs, ctx.block_size, L, 0, 1, R, 0, 1);

    // Soft clip
    for (int i = 0; i < ctx.block_size; ++i) {
        if (L[i] > 0.95f || L[i] < -0.95f) L[i] = std::tanh(L[i]);
        if (R[i] > 0.95f || R[i] < -0.95f) R[i] = std::tanh(R[i]);
    }
}

#endif // AS_ENABLE_SF2

// ---------------------------------------------------------------------------
// LV2Node
// ---------------------------------------------------------------------------

#ifdef AS_ENABLE_LV2
#include <lilv/lilv.h>

struct LV2Node::Impl {
    LilvWorld*    world    = nullptr;
    LilvInstance* instance = nullptr;
    const LilvPlugin* plugin = nullptr;

    struct PortInfo {
        std::string      symbol;
        PortType         type;
        bool             is_output;
        uint32_t         lv2_index;
        float            value = 0.0f;
        std::vector<float> audio_buf;  // for audio ports
    };
    std::vector<PortInfo> ports;

    // Indices of audio input/output ports (into ports[])
    std::vector<int> audio_in_idx, audio_out_idx;
    float sample_rate = 44100.0f;
};

LV2Node::LV2Node(const std::string& id_, const std::string& uri) {
    id = id_;
    impl_ = std::make_unique<Impl>();

    impl_->world = lilv_world_new();
    lilv_world_load_all(impl_->world);

    auto* plugins = lilv_world_get_all_plugins(impl_->world);
    LilvNode* uri_node = lilv_new_uri(impl_->world, uri.c_str());
    impl_->plugin = lilv_plugins_get_by_uri(plugins, uri_node);
    lilv_node_free(uri_node);

    if (!impl_->plugin)
        throw std::runtime_error("LV2: plugin not found: " + uri);
}

LV2Node::~LV2Node() { deactivate(); }

std::vector<Node::PortDecl> LV2Node::declare_ports() const {
    std::vector<PortDecl> decls;
    if (!impl_->plugin) return decls;

    LilvNode* audio_class   = lilv_new_uri(impl_->world, LILV_URI_AUDIO_PORT);
    LilvNode* control_class = lilv_new_uri(impl_->world, LILV_URI_CONTROL_PORT);
    LilvNode* input_class   = lilv_new_uri(impl_->world, LILV_URI_INPUT_PORT);
    LilvNode* output_class  = lilv_new_uri(impl_->world, LILV_URI_OUTPUT_PORT);

    uint32_t n = lilv_plugin_get_num_ports(impl_->plugin);
    for (uint32_t i = 0; i < n; ++i) {
        const LilvPort* port = lilv_plugin_get_port_by_index(impl_->plugin, i);
        // lilv_port_get_symbol returns a const LilvNode* owned by the plugin
        const LilvNode* sym_node = lilv_port_get_symbol(impl_->plugin, port);
        std::string sym = lilv_node_as_string(sym_node);

        bool is_audio   = lilv_port_is_a(impl_->plugin, port, audio_class);
        bool is_control = lilv_port_is_a(impl_->plugin, port, control_class);
        bool is_output  = lilv_port_is_a(impl_->plugin, port, output_class);

        PortType pt = is_audio ? PortType::AudioMono : PortType::Control;
        decls.push_back({sym, pt, is_output});
    }

    lilv_node_free(audio_class);
    lilv_node_free(control_class);
    lilv_node_free(input_class);
    lilv_node_free(output_class);
    return decls;
}

void LV2Node::activate(float sample_rate, int max_block_size) {
    impl_->sample_rate = sample_rate;

    LilvNode* sr_node = lilv_new_float(impl_->world, sample_rate);
    impl_->instance = lilv_plugin_instantiate(
        impl_->plugin, static_cast<double>(sample_rate), nullptr);
    lilv_node_free(sr_node);

    if (!impl_->instance)
        throw std::runtime_error("LV2: failed to instantiate plugin");

    // Allocate audio buffers and connect ports
    uint32_t n = lilv_plugin_get_num_ports(impl_->plugin);
    LilvNode* audio_class = lilv_new_uri(impl_->world, LILV_URI_AUDIO_PORT);
    LilvNode* ctrl_class  = lilv_new_uri(impl_->world, LILV_URI_CONTROL_PORT);
    LilvNode* in_class    = lilv_new_uri(impl_->world, LILV_URI_INPUT_PORT);
    LilvNode* out_class   = lilv_new_uri(impl_->world, LILV_URI_OUTPUT_PORT);

    impl_->ports.resize(n);
    for (uint32_t i = 0; i < n; ++i) {
        auto& pi = impl_->ports[i];
        const LilvPort* port = lilv_plugin_get_port_by_index(impl_->plugin, i);
        pi.lv2_index = i;
        // lilv_port_get_symbol returns a const LilvNode* owned by the plugin — don't free it
        const LilvNode* sym = lilv_port_get_symbol(impl_->plugin, port);
        pi.symbol = lilv_node_as_string(sym);
        pi.is_output = lilv_port_is_a(impl_->plugin, port, out_class);

        if (lilv_port_is_a(impl_->plugin, port, audio_class)) {
            pi.type = PortType::AudioMono;
            pi.audio_buf.assign(max_block_size, 0.0f);
            lilv_instance_connect_port(impl_->instance, i, pi.audio_buf.data());
            if (pi.is_output) impl_->audio_out_idx.push_back(i);
            else              impl_->audio_in_idx.push_back(i);
        } else {
            pi.type = PortType::Control;
            // Get default value
            LilvNode* def_n = nullptr; LilvNode* min_n = nullptr; LilvNode* max_n = nullptr;
            lilv_port_get_range(impl_->plugin, port, &def_n, &min_n, &max_n);
            if (def_n && lilv_node_is_float(def_n)) pi.value = lilv_node_as_float(def_n);
            if (def_n) lilv_node_free(def_n);
            if (min_n) lilv_node_free(min_n);
            if (max_n) lilv_node_free(max_n);
            lilv_instance_connect_port(impl_->instance, i, &pi.value);
        }
    }

    lilv_node_free(audio_class);
    lilv_node_free(ctrl_class);
    lilv_node_free(in_class);
    lilv_node_free(out_class);

    lilv_instance_activate(impl_->instance);
}

void LV2Node::deactivate() {
    if (impl_->instance) {
        lilv_instance_deactivate(impl_->instance);
        lilv_instance_free(impl_->instance);
        impl_->instance = nullptr;
    }
    if (impl_->world) {
        lilv_world_free(impl_->world);
        impl_->world = nullptr;
    }
}

void LV2Node::set_param(const std::string& name, float value) {
    for (auto& p : impl_->ports) {
        if (p.symbol == name && p.type == PortType::Control && !p.is_output) {
            p.value = value;
            return;
        }
    }
}

void LV2Node::process(const ProcessContext& ctx,
                       const std::vector<PortBuffer>& inputs,
                       std::vector<PortBuffer>& outputs)
{
    if (!impl_->instance) return;  // not yet activated (or activation failed)

    // Copy graph input buffers into LV2 audio input port buffers
    for (size_t i = 0; i < inputs.size() && i < impl_->audio_in_idx.size(); ++i) {
        auto& p = impl_->ports[impl_->audio_in_idx[i]];
        std::memcpy(p.audio_buf.data(), inputs[i].audio, ctx.block_size * sizeof(float));
    }

    lilv_instance_run(impl_->instance, ctx.block_size);

    // Copy LV2 audio output buffers to graph output ports
    for (size_t i = 0; i < outputs.size() && i < impl_->audio_out_idx.size(); ++i) {
        auto& p = impl_->ports[impl_->audio_out_idx[i]];
        std::memcpy(outputs[i].audio, p.audio_buf.data(), ctx.block_size * sizeof(float));
    }
}

#endif // AS_ENABLE_LV2

// ---------------------------------------------------------------------------
// TrackSourceNode
// ---------------------------------------------------------------------------
// Stateless from the graph's perspective (no audio ports, no process work),
// but maintains a preview note set that the IPC thread can inject into.
// Downstream processor nodes are registered by Graph::activate().

TrackSourceNode::TrackSourceNode(const std::string& id_) { id = id_; }

std::vector<Node::PortDecl> TrackSourceNode::declare_ports() const {
    // No audio ports — this node drives downstream nodes via direct method calls,
    // not through the buffer graph. The graph system only needs to know about
    // it for eval-order purposes (it has no inputs, so it sorts first).
    return {};
}

void TrackSourceNode::process(const ProcessContext& /*ctx*/,
                               const std::vector<PortBuffer>& /*inputs*/,
                               std::vector<PortBuffer>& /*outputs*/)
{
    // Drain preview pending queues and forward to downstream nodes.
    // Called on the audio thread once per block, before downstream nodes process.
    std::lock_guard<std::mutex> lk(preview_mutex_);

    for (auto& [ch, pitch] : pending_off_) {
        if (ch == -1) {
            for (auto* n : downstream_) n->all_notes_off(-1);
        } else {
            for (auto* n : downstream_) n->note_off(ch, pitch);
        }
    }
    pending_off_.clear();

    for (auto& pn : pending_on_) {
        for (auto* n : downstream_) n->note_on(pn.channel, pn.pitch, pn.velocity);
    }
    pending_on_.clear();
}

void TrackSourceNode::set_downstream(std::vector<Node*> nodes) {
    downstream_ = std::move(nodes);
}

// Scheduled event forwarding — called from the Dispatcher on the audio thread.
// No locking needed: the audio thread owns downstream_ during graph lifetime.

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

void TrackSourceNode::all_notes_off(int channel) {
    // Transport stop/seek path — forward to downstream synths only.
    // Does NOT clear preview notes; those are managed separately.
    for (auto* n : downstream_) n->all_notes_off(channel);
}

// Preview interface — called from the IPC thread.

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

// ---------------------------------------------------------------------------
// NoteGateNode
// ---------------------------------------------------------------------------

NoteGateNode::NoteGateNode(const std::string& id_, int pitch_lo, int pitch_hi, int mode)
    : pitch_lo_(pitch_lo), pitch_hi_(pitch_hi), mode_(mode)
{
    id = id_;
}

std::vector<Node::PortDecl> NoteGateNode::declare_ports() const {
    // No audio ports — event input is handled via note_on/note_off virtuals.
    // Control output goes into the buffer graph.
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
    if (channel == -1) {
        active_.clear();
    } else {
        for (auto it = active_.begin(); it != active_.end(); ) {
            if (it->first / 128 == channel) it = active_.erase(it);
            else ++it;
        }
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
    // Recompute after param change (in case band or mode changed mid-play)
    recompute_value_();
}

void NoteGateNode::recompute_value_() {
    if (active_.empty()) {
        current_value_ = 0.0f;
        return;
    }

    switch (mode_) {
        case 0: // Gate
            current_value_ = 1.0f;
            break;

        case 1: { // Velocity — most recent note-on (highest key in map as proxy)
            // We want the most recently triggered note; since we don't track
            // insertion order, use highest velocity among active notes as a
            // reasonable approximation (avoids a separate queue).
            int max_vel = 0;
            for (auto& [k, v] : active_) max_vel = std::max(max_vel, v);
            current_value_ = max_vel / 127.0f;
            break;
        }

        case 2: { // Pitch — active note closest to pitch_hi (highest pitch in band)
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

        case 3: { // NoteCount — normalised by band width
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
    if (desc.type == "sine") {
        return std::make_unique<SineNode>(desc.id);
    }
    if (desc.type == "mixer") {
        return std::make_unique<MixerNode>(desc.id, desc.channel_count);
    }
    if (desc.type == "control_source") {
        return std::make_unique<ControlSourceNode>(desc.id);
    }
    if (desc.type == "track_source") {
        return std::make_unique<TrackSourceNode>(desc.id);
    }
    if (desc.type == "note_gate") {
        return std::make_unique<NoteGateNode>(desc.id,
            desc.pitch_lo, desc.pitch_hi, desc.gate_mode);
    }
#ifdef AS_ENABLE_SF2
    if (desc.type == "fluidsynth") {
        if (desc.sf2_path.empty()) { err = "fluidsynth node requires sf2_path"; return nullptr; }
        try { return std::make_unique<FluidSynthNode>(desc.id, desc.sf2_path); }
        catch (const std::exception& e) { err = e.what(); return nullptr; }
    }
#endif
#ifdef AS_ENABLE_LV2
    if (desc.type == "lv2") {
        if (desc.lv2_uri.empty()) { err = "lv2 node requires lv2_uri"; return nullptr; }
        try { return std::make_unique<LV2Node>(desc.id, desc.lv2_uri); }
        catch (const std::exception& e) { err = e.what(); return nullptr; }
    }
#endif
    err = "Unknown node type: " + desc.type;
    return nullptr;
}
