#pragma once
// plugin_adapter.h
// Wraps a Plugin (new API) into a Node (engine API).

#include "graph.h"
#include "plugin_api.h"
#include <memory>
#include <atomic>

class PluginAdapterNode final : public Node {
public:
    PluginAdapterNode(const std::string& node_id, std::unique_ptr<Plugin> plugin);
    ~PluginAdapterNode() override;

    std::vector<PortDecl> declare_ports() const override;
    void activate(float sample_rate, int max_block_size) override;
    void deactivate() override;

    void process(
        const ProcessContext& ctx,
        const std::vector<PortBuffer>& inputs,
        std::vector<PortBuffer>&       outputs
    ) override;

    void set_param(const std::string& name, float value) override;

    void note_on (int channel, int pitch, int velocity) override;
    void note_off(int channel, int pitch) override;
    void all_notes_off(int channel = -1) override;
    void program_change(int channel, int bank, int program) override;
    void pitch_bend(int channel, int value) override;
    void channel_volume(int channel, int volume) override;
    void note_tune(int channel, int note, float semitones) override;
    void push_control(double beat, float normalized_value) override;

    Plugin* plugin() { return plugin_.get(); }
    const Plugin* plugin() const { return plugin_.get(); }

    const PluginDescriptor& plugin_descriptor() const { return desc_; }

    const std::vector<std::pair<std::string, std::vector<MidiEvent>>>& event_outputs() const {
        return event_output_storage_;
    }

    void push_lyric(double beat, const std::string& lyric,
                    int pitch = -1, double duration_beats = 0.0) override;
    void on_schedule_loaded() override;
    void on_seek(double beat) override;

    void set_control_connected(const std::string& port_id, bool connected);

    /// Called by Graph::activate() to inject a pattern into a pattern input port.
    /// The PatternData pointer must outlive this node (owned by PatternSourceNode).
    void set_pattern(const std::string& port_id, const PatternData* data);

private:
    std::unique_ptr<Plugin> plugin_;
    PluginDescriptor        desc_;

    struct AudioPortMapping {
        std::string plugin_port_id;
        bool        is_stereo;
        bool        is_output;
        int         left_decl_index;
        int         right_decl_index;
    };

    struct ControlPortMapping {
        std::string plugin_port_id;
        bool        is_output;
        int         decl_index;
        std::unique_ptr<std::atomic<float>> pending_value;
        bool        has_pending  = false;
        bool        is_connected = false;

        ControlPortMapping()
            : is_output(false), decl_index(0)
            , pending_value(std::make_unique<std::atomic<float>>(0.0f)) {}
    };

    struct EventPortMapping {
        std::string plugin_port_id;
        bool        is_output;
    };

    struct PatternPortMapping {
        std::string plugin_port_id;
        bool        is_output;
        // Pointer to the upstream PatternData; injected by Graph::activate()
        // via set_pattern() after PatternSourceNodes are wired up.
        const PatternData* data = nullptr;
    };

    std::vector<AudioPortMapping>   audio_map_;
    std::vector<ControlPortMapping> control_map_;
    std::vector<EventPortMapping>   event_map_;
    std::vector<PatternPortMapping> pattern_map_;

    PluginBuffers buffers_;

    std::vector<MidiEvent> event_input_accum_;
    std::vector<std::pair<std::string, std::vector<MidiEvent>>> event_output_storage_;

    int n_input_decls_  = 0;
    int n_output_decls_ = 0;

    void build_port_mapping();
};
