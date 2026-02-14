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
// ControlSourceNode  (unchanged from original)
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

    for (int ch = 0; ch < 16; ++ch)
        if (ch != 9)
            fluid_synth_program_select(static_cast<fluid_synth_t*>(fs_),
                                       ch, sfid_, 0, 0);

    raw_buf_.resize(max_block_size * 2);
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
    fluid_synth_write_float(fs, ctx.block_size, L, 0, 1, R, 0, 1);
    for (int i = 0; i < ctx.block_size; ++i) {
        if (L[i] > 0.95f || L[i] < -0.95f) L[i] = std::tanh(L[i]);
        if (R[i] > 0.95f || R[i] < -0.95f) R[i] = std::tanh(R[i]);
    }
}

#endif // AS_ENABLE_SF2

// ---------------------------------------------------------------------------
// LV2Node  — proper atom/event sequence I/O with URID map
// ---------------------------------------------------------------------------

#ifdef AS_ENABLE_LV2
#include <lilv/lilv.h>
#include <lv2/atom/atom.h>
#include <lv2/atom/forge.h>
#include <lv2/atom/util.h>
#include <lv2/midi/midi.h>
#include <lv2/urid/urid.h>
#include <lv2/core/lv2.h>

// ---------------------------------------------------------------------------
// Minimal URID map implementation
// ---------------------------------------------------------------------------
// LV2 plugins request a URID map feature so they can work with compact integer
// IDs for URIs (atom types, MIDI event type, etc.) instead of string comparison.
// We maintain a single global bidirectional map; the map only grows.

#include <unordered_map>

static std::mutex                                   s_urid_mutex;
static std::unordered_map<std::string, LV2_URID>   s_uri_to_urid;
static std::unordered_map<LV2_URID, std::string>   s_urid_to_uri;
static LV2_URID                                     s_next_urid = 1;

static LV2_URID urid_map_func(LV2_URID_Map_Handle /*handle*/, const char* uri) {
    std::lock_guard<std::mutex> lk(s_urid_mutex);
    auto it = s_uri_to_urid.find(uri);
    if (it != s_uri_to_urid.end()) return it->second;
    LV2_URID id = s_next_urid++;
    s_uri_to_urid[uri] = id;
    s_urid_to_uri[id]  = uri;
    return id;
}

static const char* urid_unmap_func(LV2_URID_Unmap_Handle /*handle*/, LV2_URID urid) {
    std::lock_guard<std::mutex> lk(s_urid_mutex);
    auto it = s_urid_to_uri.find(urid);
    if (it != s_urid_to_uri.end()) return it->second.c_str();
    return nullptr;
}

static LV2_URID_Map   s_urid_map   = { nullptr, urid_map_func };
static LV2_URID_Unmap s_urid_unmap = { nullptr, urid_unmap_func };

static const LV2_Feature s_map_feature   = { LV2_URID__map,   &s_urid_map };
static const LV2_Feature s_unmap_feature = { LV2_URID__unmap, &s_urid_unmap };
static const LV2_Feature* s_features[] = { &s_map_feature, &s_unmap_feature, nullptr };

// ---------------------------------------------------------------------------
// Read a numeric value from a LilvNode (float or integer)
// ---------------------------------------------------------------------------
// lilv_node_is_float() returns false for integer-typed and bare (untyped)
// literals. lilv_node_as_float() however does internal string→float
// conversion for any literal node, so we use it as a last-resort fallback.
static float lilv_node_as_number(const LilvNode* n, float fallback) {
    if (!n) return fallback;
    float v;
    if (lilv_node_is_float(n))        v = lilv_node_as_float(n);
    else if (lilv_node_is_int(n))     v = static_cast<float>(lilv_node_as_int(n));
    else if (lilv_node_is_literal(n)) v = lilv_node_as_float(n);
    else return fallback;
    // Guard against NaN/Inf from malformed TTL
    if (std::isnan(v) || std::isinf(v)) return fallback;
    return v;
}

// ---------------------------------------------------------------------------
// Atom event buffer — used for atom/event ports
// ---------------------------------------------------------------------------
// Capacity is sized for a reasonable number of MIDI events per block.
// 4KB is enough for ~200 3-byte MIDI events with atom headers.
static constexpr size_t ATOM_BUF_SIZE = 4096;

struct AtomBuffer {
    alignas(8) uint8_t data[ATOM_BUF_SIZE];

    // Reset to an empty sequence
    void clear(LV2_URID sequence_type, LV2_URID beat_unit) {
        auto* seq = reinterpret_cast<LV2_Atom_Sequence*>(data);
        seq->atom.type = sequence_type;
        seq->atom.size = sizeof(LV2_Atom_Sequence_Body);
        seq->body.unit = beat_unit;  // 0 = frames
        seq->body.pad  = 0;
    }

    LV2_Atom_Sequence* as_sequence() {
        return reinterpret_cast<LV2_Atom_Sequence*>(data);
    }

    // Append a MIDI event at the given frame offset. Returns false if full.
    bool append_midi(int64_t frames, const uint8_t* midi_data, uint32_t midi_size,
                     LV2_URID midi_event_type)
    {
        auto* seq = as_sequence();
        uint32_t body_end = sizeof(LV2_Atom_Sequence_Body) + seq->atom.size - sizeof(LV2_Atom_Sequence_Body);
        // Actually: atom.size = total size of body content (including sequence body header)
        // Events start after the body header.
        uint32_t event_size = sizeof(LV2_Atom_Event) + midi_size;
        // Pad to 8 bytes
        uint32_t padded = (event_size + 7u) & ~7u;
        if (sizeof(LV2_Atom_Sequence_Body) + (seq->atom.size - sizeof(LV2_Atom_Sequence_Body)) + padded
            > ATOM_BUF_SIZE - sizeof(LV2_Atom))
            return false;  // buffer full

        auto* ev = reinterpret_cast<LV2_Atom_Event*>(
            data + sizeof(LV2_Atom) + seq->atom.size);
        ev->time.frames = frames;
        ev->body.type = midi_event_type;
        ev->body.size = midi_size;
        memcpy(ev + 1, midi_data, midi_size);

        seq->atom.size += padded;
        return true;
    }
};

// ---------------------------------------------------------------------------
// LV2Node::Impl
// ---------------------------------------------------------------------------

struct LV2Node::Impl {
    LilvWorld*        world    = nullptr;
    LilvInstance*     instance = nullptr;
    const LilvPlugin* plugin   = nullptr;

    struct PortInfo {
        std::string symbol;
        PortType    type;
        bool        is_output;
        uint32_t    lv2_index;
        float       value = 0.0f;
        std::unique_ptr<float[]> audio_buf;
        // True if this port was emitted by declare_ports() and therefore has
        // a slot in the inputs[]/outputs[] vectors the graph passes to process().
        // False for atom/event ports that are handled internally.
        bool        graph_visible = false;
        // For atom/event ports
        bool        is_atom = false;
        std::unique_ptr<AtomBuffer> atom_buf;
    };

    std::vector<PortInfo> ports;
    std::vector<uint32_t> graph_audio_in;
    std::vector<uint32_t> graph_audio_out;

    // Debug arrays for verifying port ordering
    std::vector<uint32_t> graph_input_lv2_indices;
    std::vector<uint32_t> graph_output_lv2_indices;

    int   max_block_size = 0;
    float sample_rate    = 44100.0f;

    // Cached URIDs for atom sequence operations
    LV2_URID urid_atom_sequence = 0;
    LV2_URID urid_atom_chunk    = 0;
    LV2_URID urid_midi_event    = 0;
    LV2_URID urid_time_frame    = 0;

    // Indices of atom input ports that accept MIDI (for note event injection)
    std::vector<uint32_t> midi_input_ports;

    // Pending MIDI events to inject at next process() call
    struct MidiEvent {
        uint8_t data[3];
        uint32_t size;
    };
    std::vector<MidiEvent> pending_midi;
    std::mutex midi_mutex;
};

LV2Node::LV2Node(const std::string& id_, const std::string& uri) {
    id = id_;
    impl_ = std::make_unique<Impl>();
    impl_->world = static_cast<LilvWorld*>(lv2_world_acquire());

    auto* plugins = lilv_world_get_all_plugins(impl_->world);
    LilvNode* uri_node = lilv_new_uri(impl_->world, uri.c_str());
    impl_->plugin = lilv_plugins_get_by_uri(plugins, uri_node);
    lilv_node_free(uri_node);

    if (!impl_->plugin) {
        lv2_world_release();
        impl_->world = nullptr;
        throw std::runtime_error("LV2: plugin not found: " + uri);
    }

    AS_LOG("lv2", "LV2Node '%s': found plugin '%s'", id_.c_str(), uri.c_str());
}

LV2Node::~LV2Node() {
    deactivate();
    if (impl_->world) {
        lv2_world_release();
        impl_->world = nullptr;
    }
}

std::vector<Node::PortDecl> LV2Node::declare_ports() const {
    std::vector<PortDecl> decls;
    if (!impl_->plugin) return decls;

    LilvNode* audio_class   = lilv_new_uri(impl_->world, LILV_URI_AUDIO_PORT);
    LilvNode* control_class = lilv_new_uri(impl_->world, LILV_URI_CONTROL_PORT);
    LilvNode* input_class   = lilv_new_uri(impl_->world, LILV_URI_INPUT_PORT);
    LilvNode* output_class  = lilv_new_uri(impl_->world, LILV_URI_OUTPUT_PORT);

    uint32_t n = lilv_plugin_get_num_ports(impl_->plugin);
    AS_LOG("lv2", "declare_ports '%s': %u total LV2 ports", id.c_str(), n);

    for (uint32_t i = 0; i < n; ++i) {
        const LilvPort* port = lilv_plugin_get_port_by_index(impl_->plugin, i);
        const LilvNode* sym_node = lilv_port_get_symbol(impl_->plugin, port);
        std::string sym = lilv_node_as_string(sym_node);

        bool is_audio   = lilv_port_is_a(impl_->plugin, port, audio_class);
        bool is_control = lilv_port_is_a(impl_->plugin, port, control_class);
        bool is_output  = lilv_port_is_a(impl_->plugin, port, output_class);
        bool is_input   = lilv_port_is_a(impl_->plugin, port, input_class);

        // Skip atom/event/CV ports — they're handled internally, not graph-visible.
        if (!is_audio && !is_control) {
            AS_LOG("lv2", "  port %u '%s': non-audio/control — handled internally",
                          i, sym.c_str());
            continue;
        }

        PortType pt = is_audio ? PortType::AudioMono : PortType::Control;
        const char* type_s = is_audio ? "audio" : "control";
        const char* dir_s  = is_output ? "out" : "in";
        AS_LOG("lv2", "  port %u '%s': %s %s -> graph slot %zu",
               i, sym.c_str(), type_s, dir_s, decls.size());

        decls.push_back({sym, pt, is_output});
    }

    AS_LOG("lv2", "declare_ports '%s': emitting %zu graph ports total", id.c_str(), decls.size());

    lilv_node_free(audio_class);
    lilv_node_free(control_class);
    lilv_node_free(input_class);
    lilv_node_free(output_class);
    return decls;
}

void LV2Node::activate(float sample_rate, int max_block_size) {
    impl_->sample_rate    = sample_rate;
    impl_->max_block_size = max_block_size;

    AS_LOG("lv2", "activate '%s': sr=%.0f block=%d", id.c_str(), sample_rate, max_block_size);

    // Cache URIDs we'll need for atom sequence operations
    impl_->urid_atom_sequence = urid_map_func(nullptr, LV2_ATOM__Sequence);
    impl_->urid_atom_chunk    = urid_map_func(nullptr, LV2_ATOM__Chunk);
    impl_->urid_midi_event    = urid_map_func(nullptr, LV2_MIDI__MidiEvent);
    impl_->urid_time_frame    = urid_map_func(nullptr, "http://lv2plug.in/ns/ext/time#frame");

    // Instantiate with URID map/unmap features
    impl_->instance = lilv_plugin_instantiate(
        impl_->plugin, static_cast<double>(sample_rate), s_features);

    if (!impl_->instance)
        throw std::runtime_error("LV2: failed to instantiate plugin: " + id);

    uint32_t n = lilv_plugin_get_num_ports(impl_->plugin);

    LilvNode* audio_class = lilv_new_uri(impl_->world, LILV_URI_AUDIO_PORT);
    LilvNode* ctrl_class  = lilv_new_uri(impl_->world, LILV_URI_CONTROL_PORT);
    LilvNode* in_class    = lilv_new_uri(impl_->world, LILV_URI_INPUT_PORT);
    LilvNode* out_class   = lilv_new_uri(impl_->world, LILV_URI_OUTPUT_PORT);
    LilvNode* atom_class  = lilv_new_uri(impl_->world, LV2_ATOM__AtomPort);
    LilvNode* event_class = lilv_new_uri(impl_->world, LILV_URI_EVENT_PORT);
    LilvNode* atom_supports = lilv_new_uri(impl_->world, LV2_ATOM__supports);
    LilvNode* midi_event_uri = lilv_new_uri(impl_->world, LV2_MIDI__MidiEvent);

    impl_->ports.clear();
    impl_->ports.reserve(n);
    impl_->graph_audio_in.clear();
    impl_->graph_audio_out.clear();
    impl_->graph_input_lv2_indices.clear();
    impl_->graph_output_lv2_indices.clear();
    impl_->midi_input_ports.clear();

    for (uint32_t i = 0; i < n; ++i) {
        const LilvPort* port = lilv_plugin_get_port_by_index(impl_->plugin, i);
        const LilvNode* sym_node = lilv_port_get_symbol(impl_->plugin, port);

        bool is_audio   = lilv_port_is_a(impl_->plugin, port, audio_class);
        bool is_control = lilv_port_is_a(impl_->plugin, port, ctrl_class);
        bool is_output  = lilv_port_is_a(impl_->plugin, port, out_class);
        bool is_input   = lilv_port_is_a(impl_->plugin, port, in_class);
        bool is_atom    = lilv_port_is_a(impl_->plugin, port, atom_class);
        bool is_event   = lilv_port_is_a(impl_->plugin, port, event_class);

        Impl::PortInfo pi;
        pi.lv2_index = i;
        pi.symbol    = lilv_node_as_string(sym_node);
        pi.is_output = is_output;

        if (is_audio) {
            pi.type = PortType::AudioMono;
            pi.graph_visible = true;
            pi.audio_buf = std::make_unique<float[]>(max_block_size);
            std::fill_n(pi.audio_buf.get(), max_block_size, 0.0f);
            lilv_instance_connect_port(impl_->instance, i, pi.audio_buf.get());
            AS_LOG("lv2", "  activate port %u '%s': audio %s -> buf=%p",
                   i, pi.symbol.c_str(), is_output ? "out" : "in",
                   (void*)pi.audio_buf.get());

            if (pi.is_output) impl_->graph_audio_out.push_back(i);
            else              impl_->graph_audio_in.push_back(i);

        } else if (is_control) {
            pi.type = PortType::Control;
            pi.graph_visible = true;
            LilvNode *def_n = nullptr, *min_n = nullptr, *max_n = nullptr;
            lilv_port_get_range(impl_->plugin, port, &def_n, &min_n, &max_n);
            float def_val = lilv_node_as_number(def_n, 0.0f);
            float min_val = lilv_node_as_number(min_n, -1e9f);
            float max_val = lilv_node_as_number(max_n,  1e9f);
            lilv_node_free(def_n);
            lilv_node_free(min_n);
            lilv_node_free(max_n);

            if (!is_output) {
                // Clamp to [min, max]: some plugins (e.g. Calf) report defaults
                // outside their own stated range (level_in/level_out default=0,
                // min=0.015625) which would silently zero the signal.
                pi.value = std::max(min_val, std::min(max_val, def_val));

                // Bypass / enable toggles: boolean [0,1] ports whose name
                // implies an on/off switch. Force them to a sensible active
                // state so plugins produce output without explicit param config.
                if (min_val == 0.0f && max_val == 1.0f) {
                    const auto& s = pi.symbol;
                    if (s == "on" || s == "enable" || s == "enabled" || s == "active")
                        pi.value = 1.0f;
                    else if (s == "bypass")
                        pi.value = 0.0f;  // bypass=0 → not bypassed
                }
            } else {
                pi.value = def_val;
            }

            lilv_instance_connect_port(impl_->instance, i, &pi.value);
            AS_LOG("lv2", "  activate port %u '%s': control %s val=%.4f range=[%.4f,%.4f]",
                   i, pi.symbol.c_str(), is_output ? "out" : "in",
                   pi.value, min_val, max_val);

        } else if (is_atom || is_event) {
            // Atom/event port — allocate a proper LV2_Atom_Sequence buffer
            pi.type = PortType::Control;  // not graph-visible
            pi.graph_visible = false;
            pi.is_atom = true;
            pi.atom_buf = std::make_unique<AtomBuffer>();

            if (is_input) {
                // Input atom: present an empty sequence each block
                pi.atom_buf->clear(impl_->urid_atom_sequence, 0);
            } else {
                // Output atom: plugin writes events here; we clear before each run
                pi.atom_buf->clear(impl_->urid_atom_sequence, 0);
                // For output ports, set capacity in the atom header
                auto* seq = pi.atom_buf->as_sequence();
                seq->atom.type = impl_->urid_atom_chunk;
                seq->atom.size = ATOM_BUF_SIZE - sizeof(LV2_Atom);
            }

            lilv_instance_connect_port(impl_->instance, i, pi.atom_buf->data);
            AS_LOG("lv2", "  activate port %u '%s': atom %s -> atom_buf=%p",
                   i, pi.symbol.c_str(), is_output ? "out" : "in",
                   (void*)pi.atom_buf->data);

            // Check if this input atom port supports MIDI events
            if (is_input) {
                LilvNodes* supported = lilv_port_get_value(
                    impl_->plugin, port, atom_supports);
                if (supported) {
                    LILV_FOREACH(nodes, ni, supported) {
                        const LilvNode* sn = lilv_nodes_get(supported, ni);
                        if (lilv_node_equals(sn, midi_event_uri)) {
                            impl_->midi_input_ports.push_back(i);
                            AS_LOG("lv2", "    -> supports MIDI events");
                            break;
                        }
                    }
                    lilv_nodes_free(supported);
                }
            }

        } else {
            // Unknown port type (CV, etc.) — connect to a dummy float value
            pi.type = PortType::Control;
            pi.graph_visible = false;
            pi.value = 0.0f;
            lilv_instance_connect_port(impl_->instance, i, &pi.value);
            AS_LOG("lv2", "  activate port %u '%s': unknown type — connected to dummy float",
                   i, pi.symbol.c_str());
        }

        impl_->ports.push_back(std::move(pi));
    }

    lilv_node_free(audio_class);
    lilv_node_free(ctrl_class);
    lilv_node_free(in_class);
    lilv_node_free(out_class);
    lilv_node_free(atom_class);
    lilv_node_free(event_class);
    lilv_node_free(atom_supports);
    lilv_node_free(midi_event_uri);

    // Debug verification of port ordering
#ifdef AS_DEBUG
    {
        std::vector<uint32_t> check_in, check_out;
        for (auto& pi : impl_->ports) {
            if (pi.type != PortType::AudioMono || !pi.graph_visible) continue;
            if (pi.is_output) check_out.push_back(pi.lv2_index);
            else              check_in.push_back(pi.lv2_index);
        }
        AS_ASSERT(check_in  == impl_->graph_audio_in,
                  "LV2Node '%s': graph_audio_in ordering mismatch", id.c_str());
        AS_ASSERT(check_out == impl_->graph_audio_out,
                  "LV2Node '%s': graph_audio_out ordering mismatch", id.c_str());
        AS_LOG("lv2", "activate '%s': %zu audio in, %zu audio out, %zu midi-atom in — ordering OK",
               id.c_str(), impl_->graph_audio_in.size(),
               impl_->graph_audio_out.size(), impl_->midi_input_ports.size());
    }
#endif

    // Dump all control port values to stderr for debugging plugin crashes
    fprintf(stderr, "[lv2] activate '%s': control port values before activation:\n", id.c_str());
    for (auto& p : impl_->ports) {
        if (p.type != PortType::Control || !p.graph_visible) continue;
        fprintf(stderr, "[lv2]   port %u '%s' (%s) = %.6f\n",
                p.lv2_index, p.symbol.c_str(),
                p.is_output ? "out" : "in", p.value);
    }

    lilv_instance_activate(impl_->instance);
    AS_LOG("lv2", "activate '%s': done", id.c_str());
}

void LV2Node::deactivate() {
    if (impl_->instance) {
        AS_LOG("lv2", "deactivate '%s'", id.c_str());
        lilv_instance_deactivate(impl_->instance);
        lilv_instance_free(impl_->instance);
        impl_->instance = nullptr;
    }
}

void LV2Node::set_param(const std::string& name, float value) {
    for (auto& p : impl_->ports) {
        if (p.symbol == name && p.type == PortType::Control && !p.is_output && p.graph_visible) {
            AS_LOG("lv2", "set_param '%s': %s = %.4f", id.c_str(), name.c_str(), value);
            p.value = value;
            return;
        }
    }
    AS_LOG("lv2", "set_param '%s': unknown param '%s' (no-op)", id.c_str(), name.c_str());
}

// ---------------------------------------------------------------------------
// MIDI event injection
// ---------------------------------------------------------------------------

void LV2Node::note_on(int channel, int pitch, int velocity) {
    Impl::MidiEvent ev;
    ev.data[0] = static_cast<uint8_t>(0x90 | (channel & 0x0F));
    ev.data[1] = static_cast<uint8_t>(pitch & 0x7F);
    ev.data[2] = static_cast<uint8_t>(velocity & 0x7F);
    ev.size = 3;
    std::lock_guard<std::mutex> lk(impl_->midi_mutex);
    impl_->pending_midi.push_back(ev);
}

void LV2Node::note_off(int channel, int pitch) {
    Impl::MidiEvent ev;
    ev.data[0] = static_cast<uint8_t>(0x80 | (channel & 0x0F));
    ev.data[1] = static_cast<uint8_t>(pitch & 0x7F);
    ev.data[2] = 0;
    ev.size = 3;
    std::lock_guard<std::mutex> lk(impl_->midi_mutex);
    impl_->pending_midi.push_back(ev);
}

void LV2Node::all_notes_off(int channel) {
    // CC 123 = All Notes Off
    if (channel < 0) {
        for (int ch = 0; ch < 16; ++ch) {
            Impl::MidiEvent ev;
            ev.data[0] = static_cast<uint8_t>(0xB0 | ch);
            ev.data[1] = 123;
            ev.data[2] = 0;
            ev.size = 3;
            std::lock_guard<std::mutex> lk(impl_->midi_mutex);
            impl_->pending_midi.push_back(ev);
        }
    } else {
        Impl::MidiEvent ev;
        ev.data[0] = static_cast<uint8_t>(0xB0 | (channel & 0x0F));
        ev.data[1] = 123;
        ev.data[2] = 0;
        ev.size = 3;
        std::lock_guard<std::mutex> lk(impl_->midi_mutex);
        impl_->pending_midi.push_back(ev);
    }
}

// ---------------------------------------------------------------------------
// LV2Node::process
// ---------------------------------------------------------------------------

void LV2Node::process(const ProcessContext& ctx,
                       const std::vector<PortBuffer>& inputs,
                       std::vector<PortBuffer>& outputs)
{
    if (!impl_->instance) return;

    // Prepare atom input ports: clear to empty sequence, then inject pending MIDI
    {
        std::vector<Impl::MidiEvent> midi_events;
        {
            std::lock_guard<std::mutex> lk(impl_->midi_mutex);
            midi_events.swap(impl_->pending_midi);
        }

        for (auto& p : impl_->ports) {
            if (!p.is_atom || !p.atom_buf) continue;
            if (p.is_output) {
                // Output atom ports: set to chunk type with full capacity
                auto* seq = p.atom_buf->as_sequence();
                seq->atom.type = impl_->urid_atom_chunk;
                seq->atom.size = ATOM_BUF_SIZE - sizeof(LV2_Atom);
            } else {
                // Input atom ports: clear to empty sequence
                p.atom_buf->clear(impl_->urid_atom_sequence, 0);
            }
        }

        // Inject MIDI events into all MIDI-capable input atom ports
        if (!midi_events.empty()) {
            for (uint32_t port_idx : impl_->midi_input_ports) {
                auto& p = impl_->ports[port_idx];
                if (!p.atom_buf) continue;
                for (auto& ev : midi_events) {
                    p.atom_buf->append_midi(0, ev.data, ev.size,
                                            impl_->urid_midi_event);
                }
            }
        }
    }

    // Copy audio inputs from graph buffers into LV2 port buffers
    int input_slot  = 0;
    int audio_in_idx = 0;

    for (auto& p : impl_->ports) {
        if (!p.graph_visible) continue;
        if (p.is_output)      continue;

        if (p.type == PortType::AudioMono) {
            if (input_slot < static_cast<int>(inputs.size()) && inputs[input_slot].audio) {
                std::memcpy(p.audio_buf.get(), inputs[input_slot].audio,
                            ctx.block_size * sizeof(float));
            }
            audio_in_idx++;
        }
        input_slot++;
    }

    AS_WARN(input_slot == static_cast<int>(inputs.size()),
            "lv2", "process '%s': counted %d visible input slots but got %zu inputs[]",
            id.c_str(), input_slot, inputs.size());

    lilv_instance_run(impl_->instance, ctx.block_size);

    // Copy LV2 output buffers → graph outputs
    const int n_out = static_cast<int>(
        std::min(outputs.size(), impl_->graph_audio_out.size()));
    for (int i = 0; i < n_out; ++i) {
        if (!outputs[i].audio) continue;
        AS_ASSERT(impl_->graph_audio_out[i] < impl_->ports.size(),
                  "LV2Node '%s': graph_audio_out[%d]=%u out of range",
                  id.c_str(), i, impl_->graph_audio_out[i]);
        auto& p = impl_->ports[impl_->graph_audio_out[i]];
        std::memcpy(outputs[i].audio, p.audio_buf.get(),
                    ctx.block_size * sizeof(float));
    }
}

#endif // AS_ENABLE_LV2

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

    // --- Try plugin registry first ---
    auto plugin = PluginRegistry::create(desc.type);
    if (plugin) {
        AS_LOG("graph", "  -> resolved via plugin registry: '%s'", desc.type.c_str());
        // Apply config params from the NodeDesc
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
    if (desc.type == "control_source")
        return std::make_unique<ControlSourceNode>(desc.id);
    if (desc.type == "track_source")
        return std::make_unique<TrackSourceNode>(desc.id);
    if (desc.type == "note_gate")
        return std::make_unique<NoteGateNode>(desc.id, desc.pitch_lo, desc.pitch_hi, desc.gate_mode);
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
