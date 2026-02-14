// ipc.cpp
#include "ipc.h"
#include "protocol.h"

#include <cstring>
#include <stdexcept>
#include <iostream>

#ifdef AS_PLATFORM_WINDOWS
#  include <windows.h>
#else
#  include <sys/socket.h>
#  include <sys/un.h>
#  include <unistd.h>
#  include <errno.h>
#  include <fcntl.h>
#endif

// ---------------------------------------------------------------------------
// Framing helpers  (shared between server and client, same platform)
// ---------------------------------------------------------------------------

static constexpr uint32_t MAX_MSG = protocol::MAX_MESSAGE_BYTES;

// Little-endian 4-byte length prefix.

#ifndef AS_PLATFORM_WINDOWS

bool IpcServer::send_all(int fd, const void* buf, size_t len) {
    const char* p = static_cast<const char*>(buf);
    while (len > 0) {
        ssize_t n = write(fd, p, len);
        if (n <= 0) return false;
        p   += n;
        len -= n;
    }
    return true;
}

bool IpcServer::recv_all(int fd, void* buf, size_t len) {
    char* p = static_cast<char*>(buf);
    while (len > 0) {
        ssize_t n = read(fd, p, len);
        if (n <= 0) return false;
        p   += n;
        len -= n;
    }
    return true;
}

// ---------------------------------------------------------------------------
// IpcServer — Unix
// ---------------------------------------------------------------------------

IpcServer::IpcServer(const std::string& address) : address_(address) {}

IpcServer::~IpcServer() { stop(); }

std::string IpcServer::start(RequestHandler handler) {
    server_fd_ = socket(AF_UNIX, SOCK_STREAM, 0);
    if (server_fd_ < 0) return "socket() failed";

    // Allow reuse of socket path
    unlink(address_.c_str());

    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, address_.c_str(), sizeof(addr.sun_path) - 1);

    if (bind(server_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
        return std::string("bind() failed: ") + strerror(errno);

    if (listen(server_fd_, 1) < 0)
        return std::string("listen() failed: ") + strerror(errno);

    running_.store(true);
    thread_ = std::thread([this, handler = std::move(handler)]() {
        run_unix(std::move(handler));
    });
    return {};
}

void IpcServer::run_unix(RequestHandler handler) {
    while (running_.load()) {
        // Set socket non-blocking for accept so we can check running_
        fcntl(server_fd_, F_SETFL, O_NONBLOCK);

        int client = -1;
        while (running_.load()) {
            client = accept(server_fd_, nullptr, nullptr);
            if (client >= 0) break;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                usleep(10000);  // 10ms poll
                continue;
            }
            break;  // real error
        }
        if (client < 0) continue;
        client_fd_ = client;

        // Restore blocking mode for the client socket
        int flags = fcntl(client_fd_, F_GETFL, 0);
        fcntl(client_fd_, F_SETFL, flags & ~O_NONBLOCK);

        // Serve this client until disconnect or stop
        while (running_.load()) {
            // Read 4-byte length prefix
            uint32_t len_le = 0;
            if (!recv_all(client_fd_, &len_le, 4)) break;

            uint32_t len = len_le;  // already LE on x86
            if (len == 0 || len > MAX_MSG) break;

            std::string msg(len, '\0');
            if (!recv_all(client_fd_, msg.data(), len)) break;

            std::string response = handler(msg);

            uint32_t resp_len = static_cast<uint32_t>(response.size());
            if (!send_all(client_fd_, &resp_len, 4)) break;
            if (!send_all(client_fd_, response.data(), resp_len)) break;
        }

        close(client_fd_);
        client_fd_ = -1;
    }
}

void IpcServer::stop() {
    running_.store(false);
    if (server_fd_ >= 0) {
        close(server_fd_);
        server_fd_ = -1;
        unlink(address_.c_str());
    }
    if (thread_.joinable()) thread_.join();
}

// ---------------------------------------------------------------------------
// IpcClient — Unix
// ---------------------------------------------------------------------------

IpcClient::IpcClient(const std::string& address) : address_(address) {}
IpcClient::~IpcClient() { disconnect(); }

bool IpcClient::send_all(const void* buf, size_t len) {
    const char* p = static_cast<const char*>(buf);
    while (len > 0) {
        ssize_t n = write(fd_, p, len);
        if (n <= 0) return false;
        p += n; len -= n;
    }
    return true;
}

bool IpcClient::recv_all(void* buf, size_t len) {
    char* p = static_cast<char*>(buf);
    while (len > 0) {
        ssize_t n = read(fd_, p, len);
        if (n <= 0) return false;
        p += n; len -= n;
    }
    return true;
}

std::string IpcClient::connect() {
    fd_ = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd_ < 0) return "socket() failed";

    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, address_.c_str(), sizeof(addr.sun_path) - 1);

    if (::connect(fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        close(fd_); fd_ = -1;
        return std::string("connect() failed: ") + strerror(errno);
    }
    return {};
}

void IpcClient::disconnect() {
    if (fd_ >= 0) { close(fd_); fd_ = -1; }
}

bool IpcClient::is_connected() const { return fd_ >= 0; }

std::string IpcClient::send(const std::string& req, std::string& resp_out) {
    uint32_t len = static_cast<uint32_t>(req.size());
    if (!send_all(&len, 4)) return "send length failed";
    if (!send_all(req.data(), req.size())) return "send body failed";

    uint32_t resp_len = 0;
    if (!recv_all(&resp_len, 4)) return "recv length failed";
    if (resp_len > MAX_MSG) return "response too large";

    resp_out.resize(resp_len);
    if (!recv_all(resp_out.data(), resp_len)) return "recv body failed";
    return {};
}

#else  // AS_PLATFORM_WINDOWS

// ---------------------------------------------------------------------------
// IpcServer — Windows Named Pipe
// ---------------------------------------------------------------------------

IpcServer::IpcServer(const std::string& address) : address_(address) {}
IpcServer::~IpcServer() { stop(); }

std::string IpcServer::start(RequestHandler handler) {
    running_.store(true);
    thread_ = std::thread([this, handler = std::move(handler)]() {
        run_windows(std::move(handler));
    });
    return {};
}

void IpcServer::run_windows(RequestHandler handler) {
    while (running_.load()) {
        HANDLE pipe = CreateNamedPipeA(
            address_.c_str(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,        // max instances
            65536,    // out buffer
            65536,    // in buffer
            0,        // default timeout
            nullptr
        );
        if (pipe == INVALID_HANDLE_VALUE) {
            Sleep(100);
            continue;
        }

        // Wait for client connection
        if (!ConnectNamedPipe(pipe, nullptr)) {
            if (GetLastError() != ERROR_PIPE_CONNECTED) {
                CloseHandle(pipe);
                continue;
            }
        }
        pipe_handle_ = pipe;

        while (running_.load()) {
            std::string msg;
            std::string recv_err = read_message(pipe, msg);
            if (!recv_err.empty()) break;

            std::string response = handler(msg);
            std::string send_err = send_response(pipe, response);
            if (!send_err.empty()) break;
        }

        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        pipe_handle_ = nullptr;
    }
}

std::string IpcServer::read_message(void* handle, std::string& out) {
    HANDLE pipe = static_cast<HANDLE>(handle);
    uint32_t len = 0;
    DWORD read = 0;
    if (!ReadFile(pipe, &len, 4, &read, nullptr) || read != 4)
        return "read length failed";
    if (len == 0 || len > MAX_MSG) return "invalid length";
    out.resize(len);
    DWORD total = 0;
    while (total < len) {
        DWORD got = 0;
        if (!ReadFile(pipe, out.data() + total, len - total, &got, nullptr)) return "read body failed";
        total += got;
    }
    return {};
}

std::string IpcServer::send_response(void* handle, const std::string& data) {
    HANDLE pipe = static_cast<HANDLE>(handle);
    uint32_t len = static_cast<uint32_t>(data.size());
    DWORD written = 0;
    if (!WriteFile(pipe, &len, 4, &written, nullptr)) return "write length failed";
    if (!WriteFile(pipe, data.data(), len, &written, nullptr)) return "write body failed";
    return {};
}

void IpcServer::stop() {
    running_.store(false);
    // Connect to ourselves to unblock ConnectNamedPipe
    HANDLE h = CreateFileA(address_.c_str(), GENERIC_READ, 0, nullptr,
                           OPEN_EXISTING, 0, nullptr);
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    if (thread_.joinable()) thread_.join();
}

// ---------------------------------------------------------------------------
// IpcClient — Windows Named Pipe
// ---------------------------------------------------------------------------

IpcClient::IpcClient(const std::string& address) : address_(address) {}
IpcClient::~IpcClient() { disconnect(); }

std::string IpcClient::connect() {
    HANDLE h = CreateFileA(address_.c_str(),
                           GENERIC_READ | GENERIC_WRITE,
                           0, nullptr, OPEN_EXISTING, 0, nullptr);
    if (h == INVALID_HANDLE_VALUE)
        return "CreateFile failed: " + std::to_string(GetLastError());
    pipe_handle_ = h;
    return {};
}

void IpcClient::disconnect() {
    if (pipe_handle_ && pipe_handle_ != INVALID_HANDLE_VALUE) {
        CloseHandle(static_cast<HANDLE>(pipe_handle_));
        pipe_handle_ = nullptr;
    }
}

bool IpcClient::is_connected() const {
    return pipe_handle_ && pipe_handle_ != INVALID_HANDLE_VALUE;
}

std::string IpcClient::send(const std::string& req, std::string& resp_out) {
    HANDLE pipe = static_cast<HANDLE>(pipe_handle_);
    uint32_t len = static_cast<uint32_t>(req.size());
    DWORD written = 0;
    if (!WriteFile(pipe, &len, 4, &written, nullptr)) return "write length failed";
    if (!WriteFile(pipe, req.data(), len, &written, nullptr)) return "write body failed";

    uint32_t resp_len = 0;
    DWORD got = 0;
    if (!ReadFile(pipe, &resp_len, 4, &got, nullptr) || got != 4) return "read length failed";
    if (resp_len > MAX_MSG) return "response too large";
    resp_out.resize(resp_len);
    DWORD total = 0;
    while (total < resp_len) {
        if (!ReadFile(pipe, resp_out.data() + total, resp_len - total, &got, nullptr))
            return "read body failed";
        total += got;
    }
    return {};
}

#endif // AS_PLATFORM_WINDOWS
