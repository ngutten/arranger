// builtin_plugins.cpp
// Explicit registration of all built-in plugins.
//
// WHY THIS FILE EXISTS
// --------------------
// Each plugin .cpp uses REGISTER_PLUGIN(), which expands to two file-scope
// statics with internal linkage.  When those TUs are compiled into a static
// library, the linker dead-strips them — nothing in the server binary holds
// an unresolved reference into those TUs, so their constructors never run
// and PluginRegistry::all() returns an empty list.
//
// This file provides a real external-linkage symbol (register_builtin_plugins)
// that is called from main() before the first registry query.  Because the
// linker must keep this TU to satisfy that reference, and because each plugin
// factory function is referenced here, the linker must also keep those TUs.
//
// Each plugin .cpp exposes a make_<n>_plugin() factory defined *after*
// its class definition, so the class is fully visible at the call site.
//
// ADDING A NEW BUILT-IN PLUGIN
// ----------------------------
//   1. Add its .cpp to CMakeLists.txt as usual.
//   2. Add a make_<n>_plugin() factory at the bottom of that .cpp
//      (same pattern as the others).
//   3. Forward-declare it here, increment the reserve count, and add the
//      register_one() call below.

#include "plugin_api.h"

// ---------------------------------------------------------------------------
// Forward declarations of per-plugin factory functions.
// Defined at the bottom of each plugin's .cpp after the class definition.
// ---------------------------------------------------------------------------

std::unique_ptr<Plugin> make_sine_plugin();
std::unique_ptr<Plugin> make_note_gate_plugin();
std::unique_ptr<Plugin> make_control_source_plugin();
std::unique_ptr<Plugin> make_control_monitor_plugin();
std::unique_ptr<Plugin> make_mixer_plugin();
std::unique_ptr<Plugin> make_reverb_plugin();
std::unique_ptr<Plugin> make_arpeggiator_plugin();
std::unique_ptr<Plugin> make_control_lfo_plugin();

// ---------------------------------------------------------------------------
// Registration storage and helper
// ---------------------------------------------------------------------------
// The registry stores raw pointers to PluginRegistration objects, so those
// objects must have stable addresses for the lifetime of the program.
// We keep them in a static vector that is reserved to full capacity *before*
// any push_back — this guarantees no reallocation ever occurs, so the
// pointers handed to the registry remain valid.

static std::vector<PluginRegistration>& registrations() {
    static std::vector<PluginRegistration> r;
    return r;
}

static void register_one(std::unique_ptr<Plugin>(*factory)()) {
    auto id = factory()->descriptor().id;
    registrations().push_back({std::move(id), factory});
    PluginRegistry::add(&registrations().back());
}

// ---------------------------------------------------------------------------
// Public entry point — call once from main() before any registry query.
// ---------------------------------------------------------------------------

void register_builtin_plugins() {
    // Reserve the exact count BEFORE any push_back.  The registry holds raw
    // pointers into this vector; reallocation would invalidate them and
    // cause a segfault on the first registry query.
    int count = 8;
#ifdef AS_ENABLE_SF2
    count += 1;
#endif
    registrations().reserve(count);

    register_one(make_sine_plugin);
    register_one(make_note_gate_plugin);
    register_one(make_control_source_plugin);
    register_one(make_control_monitor_plugin);
    register_one(make_mixer_plugin);
    register_one(make_reverb_plugin);
    register_one(make_arpeggiator_plugin);
    register_one(make_control_lfo_plugin);

#ifdef AS_ENABLE_SF2
    extern std::unique_ptr<Plugin> make_fluidsynth_plugin();
    register_one(make_fluidsynth_plugin);
#endif
}
