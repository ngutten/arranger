// builtin_plugins.cpp
// Explicit registration of statically linked built-in plugins.
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
// that is called from main() and bindings.cpp before the first registry query.
// Because the linker must keep this TU to satisfy that reference, and because
// each plugin factory function is referenced here, the linker must also keep
// those TUs.
//
// STATICALLY LINKED PLUGINS (registered here):
//   sine, control_source, mixer
//
// DYNAMICALLY LOADED PLUGINS (loaded from plugins/ at startup, not here):
//   note_gate, control_monitor, reverb, arpeggiator, control_lfo, fluidsynth

#include "plugin_api.h"

// ---------------------------------------------------------------------------
// Forward declarations — defined at the bottom of each plugin's .cpp.
// ---------------------------------------------------------------------------

std::unique_ptr<Plugin> make_sine_plugin();
std::unique_ptr<Plugin> make_control_source_plugin();
std::unique_ptr<Plugin> make_mixer_plugin();

// ---------------------------------------------------------------------------
// Registration storage and helper
// ---------------------------------------------------------------------------
// The registry stores raw pointers to PluginRegistration objects, so those
// objects must have stable addresses for the lifetime of the program.
// We keep them in a static vector reserved to full capacity before any
// push_back — no reallocation, pointers remain valid.

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
// Public entry point — call once before any registry query.
// ---------------------------------------------------------------------------

void register_builtin_plugins() {
    registrations().reserve(3);

    register_one(make_sine_plugin);
    register_one(make_control_source_plugin);
    register_one(make_mixer_plugin);
}
