// test/test_ipc.cpp
// Tests the IPC layer in isolation: starts a server thread, sends a few
// messages, verifies responses. No audio engine involved.

#include "ipc.h"
#include "nlohmann/json.hpp"

#include <iostream>
#include <thread>
#include <chrono>
#include <cassert>

using json = nlohmann::json;

#ifdef AS_PLATFORM_WINDOWS
static const std::string ADDR = "\\\\.\\pipe\\AudioServerTest";
#else
static const std::string ADDR = "/tmp/audio_server_test.sock";
#endif

int main() {
    std::cout << "=== test_ipc ===\n";

    // Start a simple echo/dispatch server
    IpcServer server(ADDR);
    std::string start_err = server.start([](const std::string& req) -> std::string {
        json j = json::parse(req);
        std::string cmd = j.value("cmd", "");
        if (cmd == "ping")     return json{{"status","ok"},{"pong",true}}.dump();
        if (cmd == "echo")     return json{{"status","ok"},{"data", j.value("data","")}}.dump();
        if (cmd == "shutdown") return json{{"status","ok"}}.dump();
        return json{{"status","error"},{"message","unknown"}}.dump();
    });
    if (!start_err.empty()) {
        std::cerr << "Server start failed: " << start_err << "\n";
        return 1;
    }

    // Give server a moment to bind
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    IpcClient client(ADDR);
    std::string conn_err = client.connect();
    if (!conn_err.empty()) {
        std::cerr << "Client connect failed: " << conn_err << "\n";
        return 1;
    }

    // Test 1: ping
    {
        std::string resp;
        std::string err = client.send(json{{"cmd","ping"}}.dump(), resp);
        assert(err.empty());
        auto j = json::parse(resp);
        assert(j["status"] == "ok");
        assert(j["pong"] == true);
        std::cout << "PASS: ping\n";
    }

    // Test 2: echo with payload
    {
        std::string payload(1024, 'x');  // 1KB of data
        std::string resp;
        std::string err = client.send(
            json{{"cmd","echo"},{"data",payload}}.dump(), resp);
        assert(err.empty());
        auto j = json::parse(resp);
        assert(j["status"] == "ok");
        assert(j["data"] == payload);
        std::cout << "PASS: echo 1KB\n";
    }

    // Test 3: large message (64KB)
    {
        std::string payload(65536, 'y');
        std::string resp;
        std::string err = client.send(
            json{{"cmd","echo"},{"data",payload}}.dump(), resp);
        assert(err.empty());
        auto j = json::parse(resp);
        assert(j["data"] == payload);
        std::cout << "PASS: echo 64KB\n";
    }

    // Test 4: unknown command
    {
        std::string resp;
        client.send(json{{"cmd","nope"}}.dump(), resp);
        auto j = json::parse(resp);
        assert(j["status"] == "error");
        std::cout << "PASS: unknown command → error\n";
    }

    client.disconnect();
    server.stop();

    std::cout << "All IPC tests passed.\n";
    return 0;
}
