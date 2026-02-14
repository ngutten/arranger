// main.cpp
// Audio server entry point.
//
// Usage:
//   audio_server [--address <socket_path_or_pipe_name>]
//                [--sample-rate 44100]
//                [--block-size 512]
//
// The server listens for JSON commands on a Unix socket (Linux) or
// named pipe (Windows), processes them, and returns JSON responses.

#include "audio_engine.h"
#include "ipc.h"
#include "protocol.h"
#include "nlohmann/json.hpp"

#ifdef AS_ENABLE_LV2
#include "synth_node.h"  // list_lv2_plugins
#endif

#include <iostream>
#include <string>
#include <csignal>
#include <atomic>

using json = nlohmann::json;

static std::atomic<bool> g_shutdown { false };

static void handle_signal(int) { g_shutdown.store(true); }

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
// Request handler
// ---------------------------------------------------------------------------

class ServerHandler {
public:
    explicit ServerHandler(AudioEngine& engine) : engine_(engine) {}

    std::string handle(const std::string& req_str) {
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

private:
    AudioEngine& engine_;
    bool         stream_open_ = false;

    json dispatch(const std::string& cmd, const json& req) {
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
        if (cmd == protocol::CMD_SHUTDOWN) {
            g_shutdown.store(true);
            return {{"status", "ok"}};
        }

        // -------------------------------------------------------------------
        if (cmd == protocol::CMD_SET_GRAPH) {
            // Lazily open stream on first set_graph
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
            std::string fmt = req.value("format", "wav");
            if (fmt == "wav") {
                auto wav = engine_.render_offline_wav();
                if (wav.empty()) return {{"status", "error"}, {"message", "nothing to render"}};
                std::string b64 = base64_encode(wav.data(), wav.size());
                return {{"status", "ok"}, {"format", "wav"},
                        {"data", b64},
                        {"sample_rate", (int)engine_.sample_rate()},
                        {"channels", 2}};
            }
            if (fmt == "raw_f32") {
                auto pcm = engine_.render_offline();
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
        // Preview note injection — bypasses schedule/transport entirely.
        // Routes to TrackSourceNode::preview_note_on/off so stop/seek won't
        // cut preview notes; only an explicit note_off or all_notes_off does.

        if (cmd == protocol::CMD_NOTE_ON) {
            std::string node_id = req.value("node_id", "");
            int channel  = req.value("channel",  0);
            int pitch    = req.value("pitch",    60);
            int velocity = req.value("velocity", 100);
            // Lazily open stream so preview works before any set_graph play
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
            // node_id omitted → silence all source nodes
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
        return {{"status", "error"}, {"message", "unknown command: " + cmd}};
    }
};

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
    std::string address   = protocol::DEFAULT_ADDRESS;
    float       sample_rate = 44100.0f;
    int         block_size  = 512;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--address"     && i+1 < argc) address      = argv[++i];
        if (arg == "--sample-rate" && i+1 < argc) sample_rate  = std::stof(argv[++i]);
        if (arg == "--block-size"  && i+1 < argc) block_size   = std::stoi(argv[++i]);
    }

    std::signal(SIGINT,  handle_signal);
    std::signal(SIGTERM, handle_signal);

    AudioEngineConfig cfg;
    cfg.sample_rate = sample_rate;
    cfg.block_size  = block_size;

    AudioEngine engine(cfg);
    ServerHandler handler(engine);

    IpcServer server(address);
    std::string err = server.start([&](const std::string& req) {
        return handler.handle(req);
    });
    if (!err.empty()) {
        std::cerr << "[audio_server] IPC start failed: " << err << "\n";
        return 1;
    }

    std::cerr << "[audio_server] Listening on: " << address << "\n";
    std::cerr << "[audio_server] Sample rate: " << sample_rate
              << "  Block size: " << block_size << "\n";

    while (!g_shutdown.load()) {
#ifdef AS_PLATFORM_WINDOWS
        Sleep(100);
#else
        usleep(100000);
#endif
    }

    std::cerr << "[audio_server] Shutting down.\n";
    server.stop();
    engine.close();
    return 0;
}
