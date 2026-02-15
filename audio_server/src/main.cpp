// main.cpp
// Audio server entry point.
//
// Usage:
//   audio_server [--address <socket_path_or_pipe_name>]
//                [--sample-rate 44100]
//                [--block-size 512]

#include "server_handler.h"
#include "ipc.h"
#include "protocol.h"
#include "nlohmann/json.hpp"

void register_builtin_plugins();

#include <iostream>
#include <string>
#include <csignal>
#include <atomic>

static std::atomic<bool> g_shutdown { false };

static void handle_signal(int) { g_shutdown.store(true); }

int main(int argc, char** argv) {
    std::string address    = protocol::DEFAULT_ADDRESS;
    float       sample_rate = 44100.0f;
    int         block_size  = 512;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--address"     && i+1 < argc) address     = argv[++i];
        if (arg == "--sample-rate" && i+1 < argc) sample_rate = std::stof(argv[++i]);
        if (arg == "--block-size"  && i+1 < argc) block_size  = std::stoi(argv[++i]);
    }

    register_builtin_plugins();

    std::signal(SIGINT,  handle_signal);
    std::signal(SIGTERM, handle_signal);

    AudioEngineConfig cfg;
    cfg.sample_rate = sample_rate;
    cfg.block_size  = block_size;

    ServerHandler handler(cfg);

    // Intercept the shutdown command here so ServerHandler stays process-agnostic.
    IpcServer server(address);
    std::string err = server.start([&](const std::string& req) -> std::string {
        if (req.find("\"shutdown\"") != std::string::npos) {
            try {
                auto j = nlohmann::json::parse(req);
                if (j.value("cmd", "") == protocol::CMD_SHUTDOWN) {
                    g_shutdown.store(true);
                    return nlohmann::json({{"status", "ok"}}).dump();
                }
            } catch (...) {}
        }
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
    handler.engine().close();
    return 0;
}
