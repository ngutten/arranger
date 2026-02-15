#pragma once
// plugin_loader.h
// ==========================================================================
// Dynamic plugin library loader.
//
// Separated from plugin_api.h so that platform dlfcn / Windows headers don't
// leak into every plugin translation unit.
//
// Usage (C++):
//   #include "plugin_loader.h"
//   auto result = load_plugin_library("/path/to/my_plugin.so");
//   if (!result.ok) { /* result.error describes what went wrong */ }
//
// Usage (Python, via bindings):
//   import arranger_engine
//   ok = arranger_engine.load_plugin_library("/path/to/my_plugin.so")
//
// After a successful load, PluginRegistry::all() and handle("list_registered_plugins")
// immediately reflect the new plugin — no restart needed.
//
// Thread safety: call from the main thread only (same requirement as
// register_builtin_plugins). Do not call while the audio callback is running
// (i.e. call before AudioEngine::open_stream, or pause playback first).
// ==========================================================================

#include <string>

struct LoadPluginResult {
    bool        ok    = false;
    std::string error;          ///< Non-empty on failure.
    std::string plugin_id;      ///< The id reported by the loaded plugin (on success).
};

/// Load a plugin shared library and register its plugin(s) with PluginRegistry.
///
/// The library must export:
///   extern "C" void register_plugin(PluginRegistry* registry);
///
/// That function is called immediately after the library is loaded.
/// The library handle is intentionally never closed (plugins must remain live
/// for the duration of the process).
LoadPluginResult load_plugin_library(const std::string& path);

// ---------------------------------------------------------------------------
// Implementation (header-only to avoid a separate TU)
// ---------------------------------------------------------------------------

#include "plugin_api.h"
#include <vector>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <dlfcn.h>
#endif

inline LoadPluginResult load_plugin_library(const std::string& path) {
    LoadPluginResult result;

    // --- Load the shared library ---

#ifdef _WIN32
    HMODULE handle = LoadLibraryA(path.c_str());
    if (!handle) {
        DWORD err = GetLastError();
        char msg[256];
        FormatMessageA(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                       nullptr, err, 0, msg, sizeof(msg), nullptr);
        result.error = "LoadLibrary failed: " + std::string(msg);
        return result;
    }
    using RegisterFn = void(*)(PluginRegistry*);
    auto* fn = reinterpret_cast<RegisterFn>(GetProcAddress(handle, "register_plugin"));
    if (!fn) {
        result.error = path + ": symbol 'register_plugin' not found";
        // Don't FreeLibrary — partially loaded state is safer left open.
        return result;
    }
#else
    void* handle = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        result.error = std::string("dlopen failed: ") + dlerror();
        return result;
    }
    using RegisterFn = void(*)(PluginRegistry*);
    dlerror(); // clear any previous error
    auto* fn = reinterpret_cast<RegisterFn>(dlsym(handle, "register_plugin"));
    const char* sym_err = dlerror();
    if (sym_err) {
        result.error = path + ": " + sym_err;
        return result;
    }
#endif

    // --- Call the registration function ---

    // Snapshot registry size so we can report what was added.
    std::size_t before = PluginRegistry::all().size();

    // The plugin's register_plugin() receives a PluginRegistry* for
    // future-proofing, but since both the loader and the plugin are in the
    // same process they share the global singleton.  Pass nullptr — the
    // canonical implementation ignores this parameter and calls
    // PluginRegistry::add() directly (see REGISTER_PLUGIN_DYNAMIC macro).
    fn(nullptr);

    std::size_t after = PluginRegistry::all().size();
    if (after == before) {
        result.error = path + ": register_plugin() added no entries to the registry";
        return result;
    }

    // Return the id of the first newly registered plugin (covers the common
    // single-plugin-per-library case; callers that load multi-plugin libraries
    // can enumerate via PluginRegistry::all()).
    result.plugin_id = PluginRegistry::all()[before]->id;
    result.ok        = true;
    return result;
}
