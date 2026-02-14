// plugin_registry.cpp
// Implements the global plugin registry singleton.

#include "plugin_api.h"
#include <unordered_map>

// ---------------------------------------------------------------------------
// PluginBuffers map accessors
// ---------------------------------------------------------------------------
// Linear scan over small vectors — these are populated by the adapter once per
// block with typically 1-4 entries. No hash overhead, cache-friendly.

AudioPortBuffer* PluginBuffers::AudioMap::get(const std::string& id) {
    for (auto& [k, v] : entries) if (k == id) return &v;
    return nullptr;
}
const AudioPortBuffer* PluginBuffers::AudioMap::get(const std::string& id) const {
    for (auto& [k, v] : entries) if (k == id) return &v;
    return nullptr;
}

ControlPortBuffer* PluginBuffers::ControlMap::get(const std::string& id) {
    for (auto& [k, v] : entries) if (k == id) return &v;
    return nullptr;
}
const ControlPortBuffer* PluginBuffers::ControlMap::get(const std::string& id) const {
    for (auto& [k, v] : entries) if (k == id) return &v;
    return nullptr;
}

EventPortBuffer* PluginBuffers::EventMap::get(const std::string& id) {
    for (auto& [k, v] : entries) if (k == id) return &v;
    return nullptr;
}
const EventPortBuffer* PluginBuffers::EventMap::get(const std::string& id) const {
    for (auto& [k, v] : entries) if (k == id) return &v;
    return nullptr;
}

// ---------------------------------------------------------------------------
// Registry singleton
// ---------------------------------------------------------------------------

static std::vector<PluginRegistration*>& registry_entries() {
    static std::vector<PluginRegistration*> entries;
    return entries;
}

// Cache of descriptors, built lazily.
static std::unordered_map<std::string, PluginDescriptor>& descriptor_cache() {
    static std::unordered_map<std::string, PluginDescriptor> cache;
    return cache;
}

void PluginRegistry::add(PluginRegistration* reg) {
    registry_entries().push_back(reg);
}

const std::vector<PluginRegistration*>& PluginRegistry::all() {
    return registry_entries();
}

std::unique_ptr<Plugin> PluginRegistry::create(const std::string& id) {
    for (auto* reg : registry_entries()) {
        if (reg->id == id) return reg->factory();
    }
    return nullptr;
}

const PluginDescriptor* PluginRegistry::find_descriptor(const std::string& id) {
    auto& cache = descriptor_cache();
    auto it = cache.find(id);
    if (it != cache.end()) return &it->second;

    // Build on first access
    auto plugin = create(id);
    if (!plugin) return nullptr;
    auto [inserted, _] = cache.emplace(id, plugin->descriptor());
    return &inserted->second;
}
