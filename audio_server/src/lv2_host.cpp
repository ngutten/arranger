// src/lv2_host.cpp
// LV2 plugin hosting via lilv.
//
// This file is compiled only when AS_ENABLE_LV2 is defined (see CMakeLists.txt).
// All LV2-specific code that doesn't fit cleanly inside the LV2Node class
// lives here: world lifecycle management and the plugin listing helper.
//
// LV2Node itself is defined in synth_node.h / synth_node.cpp — most of the
// per-instance work (port wiring, run loop) lives there. This file holds
// the one global operation (list_lv2_plugins) and a shared world helper.
//
// NOTE: Each LV2Node currently creates its own LilvWorld. For a production
// build with many plugins this should be refactored to a shared singleton
// world (loaded once, read-only after that). Left as a TODO since the
// prototype only deals with one or two plugin nodes at a time.

#ifdef AS_ENABLE_LV2

#include "synth_node.h"
#include "nlohmann/json.hpp"
#include <lilv/lilv.h>
#include <string>

// ---------------------------------------------------------------------------
// list_lv2_plugins
// ---------------------------------------------------------------------------
// Returns a JSON array of installed LV2 plugins. Loads its own temporary
// LilvWorld — this is a main-thread / one-shot call, not realtime.

std::string list_lv2_plugins(const std::string& uri_prefix) {
    LilvWorld* world = lilv_world_new();
    lilv_world_load_all(world);

    const LilvPlugins* plugins = lilv_world_get_all_plugins(world);

    LilvNode* audio_class   = lilv_new_uri(world, LILV_URI_AUDIO_PORT);
    LilvNode* control_class = lilv_new_uri(world, LILV_URI_CONTROL_PORT);
    LilvNode* input_class   = lilv_new_uri(world, LILV_URI_INPUT_PORT);
    LilvNode* output_class  = lilv_new_uri(world, LILV_URI_OUTPUT_PORT);

    nlohmann::json arr = nlohmann::json::array();

    LILV_FOREACH(plugins, i, plugins) {
        const LilvPlugin* p = lilv_plugins_get(plugins, i);

        // lilv_plugin_get_uri returns a const LilvNode* owned by the plugin
        const LilvNode* uri_node = lilv_plugin_get_uri(p);
        std::string uri = lilv_node_as_uri(uri_node);

        if (!uri_prefix.empty() && uri.find(uri_prefix) != 0)
            continue;

        LilvNode* name_node = lilv_plugin_get_name(p);
        std::string name = name_node ? lilv_node_as_string(name_node) : "";
        lilv_node_free(name_node);

        // Collect port descriptors
        nlohmann::json ports_arr = nlohmann::json::array();
        uint32_t n_ports = lilv_plugin_get_num_ports(p);
        for (uint32_t pi = 0; pi < n_ports; ++pi) {
            const LilvPort* port = lilv_plugin_get_port_by_index(p, pi);

            // lilv_port_get_symbol returns const LilvNode* owned by plugin — don't free
            const LilvNode* sym   = lilv_port_get_symbol(p, port);
            LilvNode*       pname = lilv_port_get_name(p, port);

            bool is_audio   = lilv_port_is_a(p, port, audio_class);
            bool is_control = lilv_port_is_a(p, port, control_class);
            bool is_input   = lilv_port_is_a(p, port, input_class);
            bool is_output  = lilv_port_is_a(p, port, output_class);

            std::string type_str = is_audio   ? "audio"
                                 : is_control ? "control"
                                 :              "other";
            std::string dir_str  = is_input  ? "input"
                                 : is_output ? "output"
                                 :             "unknown";

            // For control input ports, collect range info
            float def_val = 0.0f, min_val = 0.0f, max_val = 1.0f;
            if (is_control && is_input) {
                LilvNode *def_n = nullptr, *min_n = nullptr, *max_n = nullptr;
                lilv_port_get_range(p, port, &def_n, &min_n, &max_n);
                if (def_n && lilv_node_is_float(def_n)) def_val = lilv_node_as_float(def_n);
                if (min_n && lilv_node_is_float(min_n)) min_val = lilv_node_as_float(min_n);
                if (max_n && lilv_node_is_float(max_n)) max_val = lilv_node_as_float(max_n);
                lilv_node_free(def_n);
                lilv_node_free(min_n);
                lilv_node_free(max_n);
            }

            nlohmann::json port_obj = {
                {"symbol",    sym  ? lilv_node_as_string(sym)  : ""},
                {"name",      pname ? lilv_node_as_string(pname) : ""},
                {"type",      type_str},
                {"direction", dir_str},
            };
            if (is_control && is_input) {
                port_obj["default"] = def_val;
                port_obj["min"]     = min_val;
                port_obj["max"]     = max_val;
            }

            ports_arr.push_back(port_obj);

            lilv_node_free(pname);
            // sym is owned by the plugin — do not free it
        }

        arr.push_back({
            {"uri",   uri},
            {"name",  name},
            {"ports", ports_arr},
        });
    }

    lilv_node_free(audio_class);
    lilv_node_free(control_class);
    lilv_node_free(input_class);
    lilv_node_free(output_class);

    lilv_world_free(world);
    return arr.dump();
}

#endif // AS_ENABLE_LV2
