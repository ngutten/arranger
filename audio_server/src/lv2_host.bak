// src/lv2_host.cpp
// LV2 plugin hosting via lilv.
//
// Shared LilvWorld singleton
// --------------------------
// All LV2Node instances and list_lv2_plugins() share one LilvWorld loaded
// once at first use and kept alive until lv2_world_release() is called at
// shutdown. This avoids:
//   - per-instance world construction cost
//   - concurrent lilv/librdf init that triggers libgomp thread-pool races
//     (TSan observed malloc/free collisions in libgomp when multiple worlds
//      are constructed from different threads simultaneously)
//
// lv2_world_acquire() / lv2_world_release() use a reference-count so the
// world is freed only after the last user is done. A mutex serialises
// construction and destruction; once constructed the world is read-only
// and requires no locking for queries.

#ifdef AS_ENABLE_LV2

#include "synth_node.h"
#include "nlohmann/json.hpp"
#include <lilv/lilv.h>
#include <lv2/atom/atom.h>
#include <lv2/midi/midi.h>
#include <lv2/urid/urid.h>
#include <string>
#include <mutex>
#include <atomic>
#include <cmath>

// ---------------------------------------------------------------------------
// Shared world singleton
// ---------------------------------------------------------------------------

static std::mutex        s_world_mutex;
static LilvWorld*        s_world     = nullptr;
static int               s_world_ref = 0;

void* lv2_world_acquire() {
    std::lock_guard<std::mutex> lk(s_world_mutex);
    if (s_world_ref == 0) {
        s_world = lilv_world_new();
        lilv_world_load_all(s_world);
    }
    ++s_world_ref;
    return s_world;
}

void lv2_world_release() {
    std::lock_guard<std::mutex> lk(s_world_mutex);
    if (--s_world_ref == 0) {
        lilv_world_free(s_world);
        s_world = nullptr;
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Read a numeric value from a LilvNode, handling both float and integer types.
// Many plugins (Calf, etc.) declare port ranges as xsd:integer in their TTL;
// lilv_node_is_float() returns false for those, causing us to silently drop
// values and fall back to bad defaults.
static float lilv_node_as_number(const LilvNode* n, float fallback) {
    if (!n) return fallback;
    float v;
    if (lilv_node_is_float(n))        v = lilv_node_as_float(n);
    else if (lilv_node_is_int(n))     v = static_cast<float>(lilv_node_as_int(n));
    else if (lilv_node_is_literal(n)) v = lilv_node_as_float(n);
    else return fallback;
    if (std::isnan(v) || std::isinf(v)) return fallback;
    return v;
}

// ---------------------------------------------------------------------------
// list_lv2_plugins
// ---------------------------------------------------------------------------
// Returns a JSON array of installed LV2 plugins with full port metadata
// including range, default, and UI hint properties (toggled, integer,
// logarithmic, enumeration, scale points).

std::string list_lv2_plugins(const std::string& uri_prefix) {
    LilvWorld* world = static_cast<LilvWorld*>(lv2_world_acquire());

    const LilvPlugins* plugins = lilv_world_get_all_plugins(world);

    LilvNode* audio_class   = lilv_new_uri(world, LILV_URI_AUDIO_PORT);
    LilvNode* control_class = lilv_new_uri(world, LILV_URI_CONTROL_PORT);
    LilvNode* input_class   = lilv_new_uri(world, LILV_URI_INPUT_PORT);
    LilvNode* output_class  = lilv_new_uri(world, LILV_URI_OUTPUT_PORT);
    LilvNode* atom_class    = lilv_new_uri(world, LV2_ATOM__AtomPort);
    LilvNode* event_class   = lilv_new_uri(world, LILV_URI_EVENT_PORT);

    // Port property URIs for UI hints
    LilvNode* prop_toggled    = lilv_new_uri(world, LV2_CORE__toggled);
    LilvNode* prop_integer    = lilv_new_uri(world, LV2_CORE__integer);
    LilvNode* prop_logarithmic = lilv_new_uri(world, "http://lv2plug.in/ns/ext/port-props#logarithmic");
    LilvNode* prop_enumeration = lilv_new_uri(world, LV2_CORE__enumeration);

    // For detecting MIDI atom ports
    LilvNode* atom_supports   = lilv_new_uri(world, LV2_ATOM__supports);
    LilvNode* midi_event_uri  = lilv_new_uri(world, LV2_MIDI__MidiEvent);

    nlohmann::json arr = nlohmann::json::array();

    LILV_FOREACH(plugins, i, plugins) {
        const LilvPlugin* p = lilv_plugins_get(plugins, i);

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

            const LilvNode* sym   = lilv_port_get_symbol(p, port);
            LilvNode*       pname = lilv_port_get_name(p, port);

            bool is_audio   = lilv_port_is_a(p, port, audio_class);
            bool is_control = lilv_port_is_a(p, port, control_class);
            bool is_input   = lilv_port_is_a(p, port, input_class);
            bool is_output  = lilv_port_is_a(p, port, output_class);
            bool is_atom    = lilv_port_is_a(p, port, atom_class);
            bool is_event   = lilv_port_is_a(p, port, event_class);

            std::string type_str;
            if (is_audio)        type_str = "audio";
            else if (is_control) type_str = "control";
            else if (is_atom)    type_str = "atom";
            else if (is_event)   type_str = "event";
            else                 type_str = "other";

            std::string dir_str  = is_input  ? "input"
                                 : is_output ? "output"
                                 :             "unknown";

            nlohmann::json port_obj = {
                {"symbol",    sym   ? lilv_node_as_string(sym)   : ""},
                {"name",      pname ? lilv_node_as_string(pname) : ""},
                {"type",      type_str},
                {"direction", dir_str},
            };

            // For control ports, collect range and UI hints
            if (is_control) {
                LilvNode *def_n = nullptr, *min_n = nullptr, *max_n = nullptr;
                lilv_port_get_range(p, port, &def_n, &min_n, &max_n);
                float def_val = lilv_node_as_number(def_n, 0.0f);
                float min_val = lilv_node_as_number(min_n, 0.0f);
                float max_val = lilv_node_as_number(max_n, 1.0f);
                lilv_node_free(def_n);
                lilv_node_free(min_n);
                lilv_node_free(max_n);

                port_obj["default"] = def_val;
                port_obj["min"]     = min_val;
                port_obj["max"]     = max_val;

                // UI property hints
                if (lilv_port_has_property(p, port, prop_toggled))
                    port_obj["is_toggle"] = true;
                if (lilv_port_has_property(p, port, prop_integer))
                    port_obj["is_integer"] = true;
                if (lilv_port_has_property(p, port, prop_logarithmic))
                    port_obj["is_logarithmic"] = true;
                if (lilv_port_has_property(p, port, prop_enumeration))
                    port_obj["is_enumeration"] = true;

                // Scale points (named values for enums and discrete controls)
                LilvScalePoints* sps = lilv_port_get_scale_points(p, port);
                if (sps) {
                    nlohmann::json sp_arr = nlohmann::json::array();
                    LILV_FOREACH(scale_points, si, sps) {
                        const LilvScalePoint* sp = lilv_scale_points_get(sps, si);
                        const LilvNode* sp_val   = lilv_scale_point_get_value(sp);
                        const LilvNode* sp_label = lilv_scale_point_get_label(sp);
                        sp_arr.push_back({
                            {"value", lilv_node_as_number(sp_val, 0.0f)},
                            {"label", sp_label ? lilv_node_as_string(sp_label) : ""},
                        });
                    }
                    lilv_scale_points_free(sps);
                    if (!sp_arr.empty())
                        port_obj["scale_points"] = sp_arr;
                }
            }

            // For atom/event ports, flag if they support MIDI
            if (is_atom || is_event) {
                // Check if port supports MIDI events
                LilvNodes* supported = lilv_port_get_value(p, port, atom_supports);
                if (supported) {
                    LILV_FOREACH(nodes, ni, supported) {
                        const LilvNode* sn = lilv_nodes_get(supported, ni);
                        if (lilv_node_equals(sn, midi_event_uri)) {
                            port_obj["supports_midi"] = true;
                            break;
                        }
                    }
                    lilv_nodes_free(supported);
                }
            }

            ports_arr.push_back(port_obj);
            lilv_node_free(pname);
        }

        // Plugin class / category
        const LilvPluginClass* cls       = lilv_plugin_get_class(p);
        const LilvNode*        cls_label = lilv_plugin_class_get_label(cls);
        std::string category = cls_label ? lilv_node_as_string(cls_label) : "Plugin";

        arr.push_back({
            {"uri",      uri},
            {"name",     name},
            {"category", category},
            {"ports",    ports_arr},
        });
    }

    lilv_node_free(audio_class);
    lilv_node_free(control_class);
    lilv_node_free(input_class);
    lilv_node_free(output_class);
    lilv_node_free(atom_class);
    lilv_node_free(event_class);
    lilv_node_free(prop_toggled);
    lilv_node_free(prop_integer);
    lilv_node_free(prop_logarithmic);
    lilv_node_free(prop_enumeration);
    lilv_node_free(atom_supports);
    lilv_node_free(midi_event_uri);

    lv2_world_release();
    return arr.dump();
}

#endif // AS_ENABLE_LV2
