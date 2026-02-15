#pragma once
// server_handler.h
// JSON command dispatcher — wraps AudioEngine and translates between the
// wire protocol and engine calls.  Used by both main.cpp (IPC path) and
// the pybind11 extension module (in-process path).

#include "audio_engine.h"
#include "nlohmann/json.hpp"
#include <string>

class ServerHandler {
public:
    explicit ServerHandler(const AudioEngineConfig& cfg = {});

    // Handle a JSON command string; return a JSON response string.
    std::string handle(const std::string& request_json);

    // Direct access for callers that need it (e.g. main.cpp shutdown logic).
    AudioEngine& engine() { return engine_; }

private:
    AudioEngine engine_;
    bool        stream_open_ = false;

    nlohmann::json dispatch(const std::string& cmd, const nlohmann::json& req);
};
