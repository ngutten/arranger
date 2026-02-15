// bindings/bindings.cpp
// pybind11 extension module: exposes ServerHandler::handle() to Python.
//
// Build with -DENABLE_PYTHON_BINDINGS=ON.
// Output: standalone/arranger_engine.cpython-3xx-<platform>.so

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "server_handler.h"
#include "audio_engine.h"
#include "plugin_api.h"
#include "plugin_loader.h"

namespace py = pybind11;

void register_builtin_plugins();

// Return all registered plugin descriptors as a Python list of dicts.
// Mirrors the list_registered_plugins command so Python can call it at
// import time without going through handle().
static py::list _list_plugins() {
    py::list result;
    for (auto* reg : PluginRegistry::all()) {
        auto desc = PluginRegistry::find_descriptor(reg->id);
        if (!desc) continue;
        py::dict p;
        p["id"]           = desc->id;
        p["display_name"] = desc->display_name;
        p["category"]     = desc->category;
        p["doc"]          = desc->doc;
        p["author"]       = desc->author;
        p["version"]      = desc->version;
        // Full descriptor (ports, config_params) via handle("list_registered_plugins").
        result.append(p);
    }
    return result;
}

PYBIND11_MODULE(arranger_engine, m) {
    m.doc() = "Arranger audio engine — in-process Python bindings";

    // Register built-in plugins once at module import.
    // Safe to call multiple times (guarded internally).
    register_builtin_plugins();

    py::class_<AudioEngineConfig>(m, "AudioEngineConfig")
        .def(py::init<>())
        .def_readwrite("sample_rate",   &AudioEngineConfig::sample_rate)
        .def_readwrite("block_size",    &AudioEngineConfig::block_size)
        .def_readwrite("output_device", &AudioEngineConfig::output_device);

    py::class_<ServerHandler>(m, "AudioServer")
        .def(py::init<const AudioEngineConfig&>(),
             py::arg("cfg") = AudioEngineConfig{})
        // Release GIL so the audio callback thread can never accidentally
        // try to acquire it while we're inside C++.
        .def("handle", &ServerHandler::handle,
             py::call_guard<py::gil_scoped_release>());

    m.def("list_plugins", &_list_plugins,
          "Return brief descriptors for all registered plugins.");

    // Load a plugin shared library and register its plugin(s).
    // Returns (ok: bool, plugin_id: str, error: str).
    // Call before AudioServer() or while no graph is running.
    m.def("load_plugin_library",
          [](const std::string& path) -> py::tuple {
              auto r = load_plugin_library(path);
              return py::make_tuple(r.ok, r.plugin_id, r.error);
          },
          py::arg("path"),
          "Load a plugin .so/.dll and register it. Returns (ok, plugin_id, error).");
}
