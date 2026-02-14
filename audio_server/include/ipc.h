#pragma once
// ipc.h
// Length-prefixed JSON IPC over Unix domain socket (Linux) or named pipe (Windows).
// Single-client: accepts one connection at a time; previous connection is closed
// when a new one arrives. This is appropriate for a local per-user server.

#include <string>
#include <functional>
#include <thread>
#include <atomic>

// Handler: receives a request JSON string, returns a response JSON string.
// Called on the IPC thread (not the audio thread, not the main thread).
using RequestHandler = std::function<std::string(const std::string& request_json)>;

class IpcServer {
public:
    explicit IpcServer(const std::string& address);
    ~IpcServer();

    // Not copyable.
    IpcServer(const IpcServer&) = delete;
    IpcServer& operator=(const IpcServer&) = delete;

    // Start listening. handler is called for each incoming message.
    // Returns error string on failure, empty on success.
    std::string start(RequestHandler handler);

    // Stop the server and close the socket/pipe.
    void stop();

    bool is_running() const { return running_.load(); }

private:
    std::string     address_;
    std::atomic<bool> running_ { false };
    std::thread     thread_;

#ifdef AS_PLATFORM_WINDOWS
    void* pipe_handle_ = nullptr;  // HANDLE — opaque
    void run_windows(RequestHandler handler);
    std::string send_response(void* handle, const std::string& data);
    std::string read_message(void* handle, std::string& out);
#else
    int  server_fd_ = -1;
    int  client_fd_ = -1;
    void run_unix(RequestHandler handler);
    bool send_all(int fd, const void* buf, size_t len);
    bool recv_all(int fd, void* buf, size_t len);
#endif
};

// ---------------------------------------------------------------------------
// IPC client — used by the test script and for health checks
// ---------------------------------------------------------------------------

class IpcClient {
public:
    explicit IpcClient(const std::string& address);
    ~IpcClient();

    // Connect to a running server. Returns error string on failure.
    std::string connect();
    void disconnect();
    bool is_connected() const;

    // Send a JSON request, receive a JSON response.
    // Returns error string on failure; response_out is set on success.
    std::string send(const std::string& request_json, std::string& response_out);

private:
    std::string address_;

#ifdef AS_PLATFORM_WINDOWS
    void* pipe_handle_ = nullptr;
#else
    int fd_ = -1;
    bool send_all(const void* buf, size_t len);
    bool recv_all(void* buf, size_t len);
#endif
};
