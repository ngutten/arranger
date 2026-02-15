// server_handler.cpp
// JSON command dispatcher extracted from main.cpp.  No logic changes.

#include "server_handler.h"
#include "protocol.h"
#include "plugin_api.h"

#ifdef AS_ENABLE_LV2
#include "synth_node.h"  // list_lv2_plugins
#endif

#include <iostream>

using json = nlohmann::json;

// ---------------------------------------------------------------------------
// Base64 encoder (for render response)
// ---------------------------------------------------------------------------

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static std::string base64_encode(const uint8_t* data, size_t len) {
    std::string out;
    out.reserve(((len + 2) / 3) * 4);
    for (size_t i = 0; i < len; i += 3) {
        uint32_t v = (uint32_t)data[i] << 16;
        if (i+1 < len) v |= (uint32_t)data[i+1] << 8;
        if (i+2 < len) v |= data[i+2];
        out += B64[(v >> 18) & 63];
        out += B64[(v >> 12) & 63];
        out += (i+1 < len) ? B64[(v >> 6) & 63] : '=';
        out += (i+2 < len) ? B64[v & 63]        : '=';
    }
    return out;
}

// ---------------------------------------------------------------------------
// ServerHandler
// ---------------------------------------------------------------------------

ServerHandler::ServerHandler(const AudioEngineConfig& cfg)
    : engine_(cfg) {}

std::string ServerHandler::handle(const std::string& req_str) {
    json resp;
    try {
        json req = json::parse(req_str);
        std::string cmd = req.value("cmd", "");
        resp = dispatch(cmd, req);
    } catch (const std::exception& e) {
        resp = {{"status", "error"}, {"message", e.what()}};
    }
    return resp.dump();
}

json ServerHandler::dispatch(const std::string& cmd, const json& req) {
    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_PING) {
        return {{"status", "ok"}, {"version", "0.1.0"},
                {"features", {
#ifdef AS_ENABLE_SF2
                    "fluidsynth",
#endif
#ifdef AS_ENABLE_LV2
                    "lv2",
#endif
                    "sine", "mixer", "control_source", "track_source",
                    "note_on", "note_off", "all_notes_off", "set_node_config"
                }}};
    }

    // -------------------------------------------------------------------
    // Note: CMD_SHUTDOWN is intentionally not handled here.
    // main.cpp handles it by setting g_shutdown; the binding path has no
    // process to shut down.  Unknown-command is the right response from
    // the binding, and main.cpp intercepts it before calling handle().

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_SET_GRAPH) {
        if (!engine_.is_open()) {
            std::string err = engine_.open();
            if (!err.empty())
                return {{"status", "error"}, {"message", "stream: " + err}};
            stream_open_ = true;
        }
        std::string err = engine_.set_graph(req.dump());
        if (!err.empty()) return {{"status", "error"}, {"message", err}};
        return {{"status", "ok"}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_SET_SCHEDULE) {
        std::string err = engine_.set_schedule(req.dump());
        if (!err.empty()) return {{"status", "error"}, {"message", err}};
        return {{"status", "ok"}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_PLAY) {
        engine_.play();
        return {{"status", "ok"}};
    }
    if (cmd == protocol::CMD_STOP) {
        engine_.stop();
        return {{"status", "ok"}};
    }
    if (cmd == protocol::CMD_SET_BPM) {
        engine_.set_bpm(req.value("bpm", 120.0f));
        return {{"status", "ok"}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_SEEK) {
        engine_.seek(req.value("beat", 0.0));
        return {{"status", "ok"}};
    }
    if (cmd == protocol::CMD_SET_LOOP) {
        if (req.value("enabled", true)) {
            engine_.set_loop(req.value("start", 0.0), req.value("end", 0.0));
        } else {
            engine_.disable_loop();
        }
        return {{"status", "ok"}};
    }
    if (cmd == protocol::CMD_GET_POSITION) {
        return {{"status", "ok"},
                {"beat",    engine_.current_beat()},
                {"playing", engine_.is_playing()}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_SET_PARAM) {
        engine_.set_param(
            req.value("node_id", ""),
            req.value("param_id", ""),
            req.value("value", 0.0f)
        );
        return {{"status", "ok"}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_RENDER) {
        std::string fmt       = req.value("format", "wav");
        double duration_beats = req.value("duration_beats", 0.0);
        if (fmt == "wav") {
            auto wav = engine_.render_offline_wav(1.0f, duration_beats);
            if (wav.empty()) return {{"status", "error"}, {"message", "nothing to render"}};
            std::string b64 = base64_encode(wav.data(), wav.size());
            return {{"status", "ok"}, {"format", "wav"},
                    {"data", b64},
                    {"sample_rate", (int)engine_.sample_rate()},
                    {"channels", 2}};
        }
        if (fmt == "raw_f32") {
            auto pcm = engine_.render_offline(1.0f, duration_beats);
            if (pcm.empty()) return {{"status", "error"}, {"message", "nothing to render"}};
            std::string b64 = base64_encode(
                reinterpret_cast<const uint8_t*>(pcm.data()),
                pcm.size() * sizeof(float));
            return {{"status", "ok"}, {"format", "raw_f32"},
                    {"data", b64},
                    {"sample_rate", (int)engine_.sample_rate()},
                    {"channels", 2},
                    {"frames", (int)(pcm.size() / 2)}};
        }
        return {{"status", "error"}, {"message", "unknown format: " + fmt}};
    }

    // -------------------------------------------------------------------
#ifdef AS_ENABLE_LV2
    if (cmd == protocol::CMD_LIST_PLUGINS) {
        std::string prefix = req.value("uri_prefix", "");
        std::string plugins_json = list_lv2_plugins(prefix);
        return {{"status", "ok"}, {"plugins", json::parse(plugins_json)}};
    }
#endif

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_NOTE_ON) {
        std::string node_id = req.value("node_id", "");
        int channel  = req.value("channel",  0);
        int pitch    = req.value("pitch",    60);
        int velocity = req.value("velocity", 100);
        if (!engine_.is_open()) {
            std::string err = engine_.open();
            if (!err.empty())
                return {{"status", "error"}, {"message", "stream: " + err}};
        }
        engine_.preview_note_on(node_id, channel, pitch, velocity);
        return {{"status", "ok"}};
    }

    if (cmd == protocol::CMD_NOTE_OFF) {
        std::string node_id = req.value("node_id", "");
        int channel = req.value("channel", 0);
        int pitch   = req.value("pitch",   60);
        engine_.preview_note_off(node_id, channel, pitch);
        return {{"status", "ok"}};
    }

    if (cmd == protocol::CMD_ALL_NOTES_OFF) {
        std::string node_id = req.value("node_id", "");
        engine_.preview_all_notes_off(node_id);
        return {{"status", "ok"}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_SET_NODE_CONFIG) {
        std::string node_id = req.value("node_id", "");
        if (node_id.empty())
            return {{"status", "error"}, {"message", "node_id required"}};
        json config = req.value("config", json::object());
        std::string err = engine_.set_node_config(node_id, config.dump());
        if (!err.empty()) return {{"status", "error"}, {"message", err}};
        return {{"status", "ok"}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_GET_NODE_DATA) {
        std::string node_id = req.value("node_id", "");
        std::string port_id = req.value("port_id", "history");
        if (node_id.empty())
            return {{"status", "error"}, {"message", "node_id required"}};
        std::string data = engine_.get_node_data(node_id, port_id);
        return {{"status", "ok"}, {"data", data}};
    }

    // -------------------------------------------------------------------
    if (cmd == protocol::CMD_LIST_REGISTERED_PLUGINS) {
        json plugins = json::array();
        for (auto* reg : PluginRegistry::all()) {
            auto desc = PluginRegistry::find_descriptor(reg->id);
            if (!desc) continue;

            json jp;
            jp["id"]           = desc->id;
            jp["display_name"] = desc->display_name;
            jp["category"]     = desc->category;
            jp["doc"]          = desc->doc;
            jp["author"]       = desc->author;
            jp["version"]      = desc->version;

            json ports = json::array();
            for (auto& p : desc->ports) {
                json jport;
                jport["id"]           = p.id;
                jport["display_name"] = p.display_name;
                jport["doc"]          = p.doc;

                switch (p.type) {
                    case PluginPortType::AudioMono:   jport["type"] = "audio_mono"; break;
                    case PluginPortType::AudioStereo: jport["type"] = "audio_stereo"; break;
                    case PluginPortType::Event:       jport["type"] = "event"; break;
                    case PluginPortType::Control:     jport["type"] = "control"; break;
                }
                switch (p.role) {
                    case PortRole::Input:    jport["role"] = "input"; break;
                    case PortRole::Output:   jport["role"] = "output"; break;
                    case PortRole::Sidechain:jport["role"] = "sidechain"; break;
                    case PortRole::Monitor:  jport["role"] = "monitor"; break;
                }

                if (p.type == PluginPortType::Control) {
                    switch (p.hint) {
                        case ControlHint::Continuous:  jport["hint"] = "continuous"; break;
                        case ControlHint::Toggle:      jport["hint"] = "toggle"; break;
                        case ControlHint::Integer:     jport["hint"] = "integer"; break;
                        case ControlHint::Categorical: jport["hint"] = "categorical"; break;
                        case ControlHint::Radio:       jport["hint"] = "radio"; break;
                        case ControlHint::Meter:       jport["hint"] = "meter"; break;
                        case ControlHint::GraphEditor: jport["hint"] = "graph_editor"; break;
                    }
                    jport["default"] = p.default_value;
                    jport["min"]     = p.min_value;
                    jport["max"]     = p.max_value;
                    jport["step"]    = p.step;
                    jport["show_port_default"] = p.show_port_default;
                    if (!p.choices.empty())
                        jport["choices"] = p.choices;
                    if (!p.graph_type.empty())
                        jport["graph_type"] = p.graph_type;
                }

                ports.push_back(jport);
            }
            jp["ports"] = ports;

            json config_params = json::array();
            for (auto& cp : desc->config_params) {
                json jcp;
                jcp["id"]           = cp.id;
                jcp["display_name"] = cp.display_name;
                jcp["doc"]          = cp.doc;
                switch (cp.type) {
                    case ConfigType::String:      jcp["type"] = "string"; break;
                    case ConfigType::FilePath:    jcp["type"] = "filepath"; break;
                    case ConfigType::Integer:     jcp["type"] = "integer"; break;
                    case ConfigType::Float:       jcp["type"] = "float"; break;
                    case ConfigType::Bool:        jcp["type"] = "bool"; break;
                    case ConfigType::Categorical: jcp["type"] = "categorical"; break;
                }
                jcp["default"] = cp.default_value;
                if (!cp.file_filter.empty())
                    jcp["file_filter"] = cp.file_filter;
                if (!cp.choices.empty())
                    jcp["choices"] = cp.choices;
                config_params.push_back(jcp);
            }
            jp["config_params"] = config_params;

            plugins.push_back(jp);
        }
        return {{"status", "ok"}, {"plugins", plugins}};
    }

    // -------------------------------------------------------------------
    return {{"status", "error"}, {"message", "unknown command: " + cmd}};
}
