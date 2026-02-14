#pragma once
// debug.h
// Lightweight debug logging for audio_server.
//
// Define AS_DEBUG at compile time to activate; no-ops otherwise.
// All macros are safe to leave in production builds (zero overhead when disabled).
//
// Usage:
//   AS_LOG("graph", "node %s has %zu ports", id.c_str(), ports.size());
//   AS_LOG_LV2("activate", "port %u type=%s dir=%s", i, type, dir);
//   AS_ASSERT_AUDIO(ptr != nullptr, "output buffer is null for node %s", id.c_str());
//
// Thread safety: each AS_LOG call is a single fprintf (atomic on Linux for
// short writes to stderr). No mutex needed for diagnostics.

#pragma once
#include <cstdio>
#include <cstdarg>

#ifdef AS_DEBUG

// Internal helper — single formatted write to stderr with prefix.
static inline void as_log_impl(const char* subsystem, const char* fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf) - 2, fmt, ap);
    va_end(ap);
    if (n < 0) n = 0;
    buf[n] = '\n'; buf[n+1] = '\0';
    fprintf(stderr, "[as/%s] %s", subsystem, buf);
}

#define AS_LOG(subsystem, ...) as_log_impl(subsystem, __VA_ARGS__)

// Abort with diagnostic if condition is false — only in debug builds.
// Using fprintf+abort rather than assert() so the message appears even
// when NDEBUG is set.
#define AS_ASSERT(cond, ...) \
    do { if (!(cond)) { \
        fprintf(stderr, "[as/ASSERT] %s:%d: ", __FILE__, __LINE__); \
        fprintf(stderr, __VA_ARGS__); \
        fprintf(stderr, "\n"); \
        fflush(stderr); \
        __builtin_trap(); \
    } } while (0)

// Softer version — logs but doesn't abort. Useful in the audio callback
// where aborting would hang the audio system.
#define AS_WARN(cond, subsystem, ...) \
    do { if (!(cond)) { as_log_impl(subsystem, "WARN " __VA_ARGS__); } } while (0)

#else // !AS_DEBUG

#define AS_LOG(subsystem, ...)     do {} while (0)
#define AS_ASSERT(cond, ...)       do {} while (0)
#define AS_WARN(cond, sub, ...)    do {} while (0)

#endif // AS_DEBUG
