#pragma once
// plugin_adapter.h
// Wraps a Plugin (new API) into a Node (engine API) so that plugins can
// participate in the existing signal graph without changes to Graph or
// AudioEngine.
//
// The adapter:
//   - Translates PluginDescriptor ports into Node::PortDecl
//   - Manages buffer mapping between PluginBuffers and the flat PortBuffer vectors
//   - Handles stereo expansion (AudioStereo → two AudioMono PortDecls)
//   - Routes MIDI events from TrackSourceNode fan-out to the plugin
//   - Manages event output ports (plugin → downstream nodes)
//   - Bridges set_param() to control port values

#include "graph.h"
#include "plugin_api.h"
#include <memory>
#include <atomic>

class PluginAdapterNode final : public Node {
public:
    /// Takes ownership of the Plugin instance.
    PluginAdapterNode(const std::string& node_id, std::unique_ptr<Plugin> plugin);
    ~PluginAdapterNode() override;

    // --- Node interface ---

    std::vector<PortDecl> declare_ports() const override;
    void activate(float sample_rate, int max_block_size) override;
    void deactivate() override;

    void process(
        const ProcessContext& ctx,
        const std::vector<PortBuffer>& inputs,
        std::vector<PortBuffer>&       outputs
    ) override;

    void set_param(const std::string& name, float value) override;

    // MIDI events from TrackSourceNode fan-out
    void note_on (int channel, int pitch, int velocity) override;
    void note_off(int channel, int pitch) override;
    void all_notes_off(int channel = -1) override;
    void program_change(int channel, int bank, int program) override;
    void pitch_bend(int channel, int value) override;
    void channel_volume(int channel, int volume) override;

    // Push control values from ControlSourceNode
    void push_control(double beat, float normalized_value) override;

    // --- Additional API for the engine ---

    /// Access the underlying plugin (e.g. for configure(), read_monitor()).
    Plugin* plugin() { return plugin_.get(); }
    const Plugin* plugin() const { return plugin_.get(); }

    /// The descriptor (cached at construction).
    const PluginDescriptor& plugin_descriptor() const { return desc_; }

    /// Get event output buffers after process() — for the engine to route
    /// events to downstream nodes.
    const std::vector<std::pair<std::string, std::vector<MidiEvent>>>& event_outputs() const {
        return event_output_storage_;
    }

    /// Called by Graph::activate() after assign_buffers() to tell the adapter
    /// which control input ports have live upstream connections.  Connected
    /// ports use the graph value; unconnected ports use the pending default.
    void set_control_connected(const std::string& port_id, bool connected);

private:
    std::unique_ptr<Plugin> plugin_;
    PluginDescriptor        desc_;

    // --- Port mapping ---
    // Maps between the flat index-based PortDecl/PortBuffer arrays that the
    // graph engine uses and the named PluginBuffers that the plugin sees.

    struct AudioPortMapping {
        std::string plugin_port_id;     // PortDescriptor::id
        bool        is_stereo;          // true = AudioStereo (2 PortDecls)
        bool        is_output;
        int         left_decl_index;    // index in the PortDecl vector
        int         right_decl_index;   // -1 for mono
    };

    struct ControlPortMapping {
        std::string plugin_port_id;
        bool        is_output;
        int         decl_index;
        // Heap-allocated because std::atomic is not movable, and this struct
        // lives in a std::vector.
        std::unique_ptr<std::atomic<float>> pending_value;
        bool        has_pending  = false;
        // Set by Graph::activate() after assign_buffers() — true if a live
        // upstream connection was wired to this input port.  When true, the
        // graph value takes priority over the pending default/set_param value.
        bool        is_connected = false;

        ControlPortMapping()
            : is_output(false), decl_index(0)
            , pending_value(std::make_unique<std::atomic<float>>(0.0f)) {}
    };

    struct EventPortMapping {
        std::string plugin_port_id;
        bool        is_output;
        // Event ports don't appear in PortDecl (they use the note_on/off virtuals
        // for input, and are routed by the engine for output).
    };

    std::vector<AudioPortMapping>   audio_map_;
    std::vector<ControlPortMapping> control_map_;
    std::vector<EventPortMapping>   event_map_;

    // Pre-allocated PluginBuffers (reused each process call)
    PluginBuffers buffers_;

    // Event input accumulator — filled by note_on/off etc, consumed in process()
    std::vector<MidiEvent> event_input_accum_;

    // Event output storage — filled by plugin in process(), read by engine after
    std::vector<std::pair<std::string, std::vector<MidiEvent>>> event_output_storage_;

    // Input index counters for mapping flat arrays
    int n_input_decls_  = 0;
    int n_output_decls_ = 0;

    // Build mappings from descriptor
    void build_port_mapping();
};
