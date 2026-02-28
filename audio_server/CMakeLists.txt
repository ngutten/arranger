cmake_minimum_required(VERSION 3.18)
project(AudioServer VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ---------------------------------------------------------------------------
# Build options
# ---------------------------------------------------------------------------
option(ENABLE_SF2      "Build with SF2/FluidSynth support"              ON)
option(ENABLE_SNDFILE  "Build Sampler plugin with libsndfile audio I/O" ON)
option(ENABLE_TESTS  "Build test programs"                            ON)
option(ENABLE_ESPEAK
    "Build singing plugin with espeak-ng (ON=auto-detect, OFF=disable)"
    ON)

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
if(WIN32 OR MINGW OR CMAKE_SYSTEM_NAME STREQUAL "Windows")
    set(PLATFORM_WINDOWS TRUE)
    add_compile_definitions(AS_PLATFORM_WINDOWS)
elseif(APPLE)
    set(PLATFORM_MAC TRUE)
    add_compile_definitions(AS_PLATFORM_LINUX)  # Keep existing define for compatibility
else()
    set(PLATFORM_LINUX TRUE)
    add_compile_definitions(AS_PLATFORM_LINUX)
endif()

# ---------------------------------------------------------------------------
# Mac-specific setup for MacPorts and Homebrew
# ---------------------------------------------------------------------------
if(APPLE)
    # Try MacPorts first (typically /opt/local)
    if(EXISTS "/opt/local/lib/pkgconfig")
        list(APPEND CMAKE_PREFIX_PATH "/opt/local")
        set(ENV{PKG_CONFIG_PATH} "/opt/local/lib/pkgconfig:$ENV{PKG_CONFIG_PATH}")
    endif()
    
    # Then try Homebrew (typically /usr/local or /opt/homebrew on ARM)
    execute_process(
        COMMAND brew --prefix
        OUTPUT_VARIABLE HOMEBREW_PREFIX
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(HOMEBREW_PREFIX)
        list(APPEND CMAKE_PREFIX_PATH "${HOMEBREW_PREFIX}")
        set(ENV{PKG_CONFIG_PATH} "${HOMEBREW_PREFIX}/lib/pkgconfig:$ENV{PKG_CONFIG_PATH}")
    endif()
endif()

# ---------------------------------------------------------------------------
# Find dependencies
# ---------------------------------------------------------------------------

find_package(PkgConfig REQUIRED)

# PortAudio - try pkg-config first, then fallback to find_library (optional)
pkg_check_modules(PORTAUDIO portaudio-2.0)
if(NOT PORTAUDIO_FOUND)
    message(STATUS "pkg-config didn't find portaudio, trying find_library...")
    find_library(PORTAUDIO_LIBRARY NAMES portaudio)
    find_path(PORTAUDIO_INCLUDE_DIR portaudio.h)
    if(PORTAUDIO_LIBRARY)
        set(PORTAUDIO_LIBRARIES ${PORTAUDIO_LIBRARY})
        set(PORTAUDIO_INCLUDE_DIRS ${PORTAUDIO_INCLUDE_DIR})
        set(PORTAUDIO_FOUND TRUE)
    else()
        message(STATUS "PortAudio not found — real-time audio output will be unavailable")
        set(PORTAUDIO_LIBRARIES "")
        set(PORTAUDIO_INCLUDE_DIRS "")
        set(PORTAUDIO_FOUND FALSE)
    endif()
endif()

# nlohmann/json — header-only
# Resolution order:
#   1. Bundled in include/nlohmann/json.hpp (always wins if present)
#   2. find_package — works with nlohmann-json3-dev on Ubuntu >= 22.04
#   3. find_path — works with nlohmann-json3-dev on Ubuntu 20.04 / Debian Buster
#      (those versions ship the header but no CMake config file)
#   4. FetchContent — downloads at configure time if none of the above work

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/include/nlohmann/json.hpp")
    message(STATUS "nlohmann/json: using bundled include/nlohmann/json.hpp")
    set(_JSON_EXTRA_INCLUDE "")
    set(_JSON_LINK_TARGET "")

else()
    find_package(nlohmann_json 3.2.0 QUIET)

    if(nlohmann_json_FOUND)
        message(STATUS "nlohmann/json: ${nlohmann_json_VERSION} via find_package")
        set(_JSON_EXTRA_INCLUDE "")
        set(_JSON_LINK_TARGET "nlohmann_json::nlohmann_json")

    else()
        # Older distros: header present but no CMake config. find_path returns
        # the directory that contains the nlohmann/ subdirectory.
        find_path(_JSON_HEADER_DIR nlohmann/json.hpp
            PATHS /usr/include /usr/local/include /opt/local/include
            NO_DEFAULT_PATH)

        if(_JSON_HEADER_DIR)
            message(STATUS "nlohmann/json: header found at ${_JSON_HEADER_DIR}/nlohmann/json.hpp")
            set(_JSON_EXTRA_INCLUDE "${_JSON_HEADER_DIR}")
            set(_JSON_LINK_TARGET "")

        else()
            message(STATUS "nlohmann/json: not installed - downloading via FetchContent")
            include(FetchContent)
            FetchContent_Declare(nlohmann_json
                URL      https://github.com/nlohmann/json/releases/download/v3.11.3/json.hpp
                URL_HASH SHA256=9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6
                DOWNLOAD_NO_EXTRACT TRUE
                DOWNLOAD_DIR "${CMAKE_CURRENT_SOURCE_DIR}/include/nlohmann"
            )
            FetchContent_MakeAvailable(nlohmann_json)
            set(_JSON_EXTRA_INCLUDE "")
            set(_JSON_LINK_TARGET "")
        endif()
    endif()
endif()

if(ENABLE_SF2)
    pkg_check_modules(FLUIDSYNTH fluidsynth)
    if(NOT FLUIDSYNTH_FOUND)
        message(STATUS "pkg-config didn't find fluidsynth, trying find_library...")
        find_library(FLUIDSYNTH_LIBRARY NAMES fluidsynth REQUIRED)
        find_path(FLUIDSYNTH_INCLUDE_DIR fluidsynth.h)
        set(FLUIDSYNTH_LIBRARIES ${FLUIDSYNTH_LIBRARY})
        set(FLUIDSYNTH_INCLUDE_DIRS ${FLUIDSYNTH_INCLUDE_DIR})
        set(FLUIDSYNTH_FOUND TRUE)
    endif()
    add_compile_definitions(AS_ENABLE_SF2)
endif()

if(ENABLE_SNDFILE)
    pkg_check_modules(SNDFILE sndfile)
    if(SNDFILE_FOUND)
        add_compile_definitions(AS_ENABLE_SNDFILE)
        message(STATUS "libsndfile: ${SNDFILE_VERSION} — Sampler will decode audio files")
    else()
        message(STATUS "libsndfile: not found — Sampler will compile but produce silence")
        set(ENABLE_SNDFILE OFF)
    endif()
endif()

# ---------------------------------------------------------------------------
# Main server library (shared between server binary and tests)
# ---------------------------------------------------------------------------
# On Windows, we make this SHARED so it creates an import library (.dll.a)
# that plugin DLLs can link against. On Linux, STATIC is fine because .so
# files can have undefined symbols resolved from the loading executable.
if(PLATFORM_WINDOWS)
    set(_AUDIO_SERVER_LIB_TYPE SHARED)
else()
    set(_AUDIO_SERVER_LIB_TYPE STATIC)
endif()

add_library(audio_server_lib ${_AUDIO_SERVER_LIB_TYPE}
    src/graph.cpp
    #src/ipc.cpp
    src/scheduler.cpp
    src/synth_node.cpp
    src/audio_engine.cpp
    src/plugin_registry.cpp
    src/plugin_adapter.cpp
    src/builtin_plugins.cpp
    src/server_handler.cpp
    # Statically linked built-in plugins
    plugins/builtin/sine_plugin.cpp
    plugins/builtin/control_source_plugin.cpp
    plugins/builtin/mixer_plugin.cpp
    # note_gate, control_monitor, reverb, arpeggiator, control_lfo are
    # dynamic-only: built as arranger_plugin_*.so via ENABLE_PLUGIN_LIBS.
)

target_include_directories(audio_server_lib PUBLIC
    include/
    ${PORTAUDIO_INCLUDE_DIRS}
)

if(_JSON_EXTRA_INCLUDE)
    target_include_directories(audio_server_lib PUBLIC "${_JSON_EXTRA_INCLUDE}")
endif()

target_link_libraries(audio_server_lib PUBLIC
    ${PORTAUDIO_LIBRARIES}
)

if(PORTAUDIO_LIBRARY_DIRS)
    target_link_directories(audio_server_lib PUBLIC ${PORTAUDIO_LIBRARY_DIRS})
endif()

if(_JSON_LINK_TARGET)
    target_link_libraries(audio_server_lib PUBLIC "${_JSON_LINK_TARGET}")
endif()

target_compile_options(audio_server_lib PRIVATE
    ${PORTAUDIO_CFLAGS_OTHER}
)

# Required when audio_server_lib is linked into the Python extension (.so).
# Has no effect on the standalone binary build.
set_target_properties(audio_server_lib PROPERTIES POSITION_INDEPENDENT_CODE ON)

# On Windows, audio_server_lib is a SHARED library. Put it in the plugins
# directory so plugin DLLs can find it at runtime.
if(PLATFORM_WINDOWS)
    # Export all symbols from the DLL
    target_link_options(audio_server_lib PRIVATE -Wl,--export-all-symbols)
    
    get_filename_component(_plugins_dir "${CMAKE_SOURCE_DIR}/../plugins" ABSOLUTE)
    set_target_properties(audio_server_lib PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY           "${_plugins_dir}"
        RUNTIME_OUTPUT_DIRECTORY_DEBUG     "${_plugins_dir}"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE   "${_plugins_dir}"
        RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${_plugins_dir}"
    )
    
    # Also need a copy in standalone/ for the Python extension
    # We'll use a post-build command to copy it when building arranger_engine
    if(ENABLE_PYTHON_BINDINGS)
        add_custom_command(TARGET audio_server_lib POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                $<TARGET_FILE:audio_server_lib>
                "${CMAKE_SOURCE_DIR}/../standalone/$<TARGET_FILE_NAME:audio_server_lib>"
            COMMENT "Copying audio_server_lib.dll to standalone/ for Python extension"
        )
    endif()
endif()

if(ENABLE_SF2)
    target_include_directories(audio_server_lib PUBLIC ${FLUIDSYNTH_INCLUDE_DIRS})
    target_link_libraries(audio_server_lib PUBLIC ${FLUIDSYNTH_LIBRARIES})
    if(FLUIDSYNTH_LIBRARY_DIRS)
        target_link_directories(audio_server_lib PUBLIC ${FLUIDSYNTH_LIBRARY_DIRS})
    endif()
    target_sources(audio_server_lib PRIVATE plugins/builtin/fluidsynth_plugin.cpp)
endif()

if(PLATFORM_WINDOWS)
    target_link_libraries(audio_server_lib PUBLIC ws2_32)
endif()

# ---------------------------------------------------------------------------
# Tests (optional)
# ---------------------------------------------------------------------------
if(ENABLE_TESTS)
    add_executable(test_render test/test_render.cpp)
    target_link_libraries(test_render PRIVATE audio_server_lib)
endif()

# ---------------------------------------------------------------------------
# Python bindings (optional)
# ---------------------------------------------------------------------------
option(ENABLE_PYTHON_BINDINGS "Build Python extension module (arranger_engine)" OFF)

if(ENABLE_PYTHON_BINDINGS)
    find_package(Python3 REQUIRED COMPONENTS Interpreter Development)

    include(FetchContent)
    FetchContent_Declare(pybind11
        GIT_REPOSITORY https://github.com/pybind/pybind11
        GIT_TAG        v2.13.6
    )
    FetchContent_MakeAvailable(pybind11)

    pybind11_add_module(arranger_engine bindings/bindings.cpp)
    target_link_libraries(arranger_engine PRIVATE audio_server_lib)

    # Export PluginRegistry symbols so dlopen'd plugin .so files can resolve them.
    set_target_properties(arranger_engine PROPERTIES ENABLE_EXPORTS ON)

    # On Windows, we need --export-all-symbols to actually export from the DLL
    if(PLATFORM_WINDOWS)
        target_link_options(arranger_engine PRIVATE -Wl,--export-all-symbols)
    endif()

    # Output into standalone/ so `from standalone.arranger_engine import ...` works.
    set_target_properties(arranger_engine PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY
        "${CMAKE_SOURCE_DIR}/../standalone")
endif()


# ---------------------------------------------------------------------------
# Dynamic plugin libraries
# ---------------------------------------------------------------------------
# Builds selected plugins as standalone MODULE shared libraries loadable at
# runtime via load_plugin_library() / the "load_plugin" command.
#
# Output goes to <project_root>/plugins/ so the Python binding and server
# binary can find them without an install step.
#
# plugin_library(<n> SOURCES <src> [LINK_LIBRARIES <libs>])
#   Creates target arranger_plugin_<n>.
#   Defines AS_PLUGIN_DYNAMIC (suppresses REGISTER_PLUGIN static init).

function(plugin_library name)
    cmake_parse_arguments(PL "" "" "SOURCES;LINK_LIBRARIES;INCLUDE_DIRECTORIES;LIBRARY_DIRECTORIES" ${ARGN})

    set(target "arranger_plugin_${name}")

    add_library(${target} MODULE ${PL_SOURCES})

    target_include_directories(${target} PRIVATE
        include/
        ${PORTAUDIO_INCLUDE_DIRS}
    )
    if(_JSON_EXTRA_INCLUDE)
        target_include_directories(${target} PRIVATE "${_JSON_EXTRA_INCLUDE}")
    endif()
    if(PL_INCLUDE_DIRECTORIES)
        target_include_directories(${target} PRIVATE ${PL_INCLUDE_DIRECTORIES})
    endif()

    # Do NOT link plugin_registry.cpp here.  PluginRegistry::add() and
    # PluginRegistry::all() are resolved from the loading process's symbol
    # table at dlopen time.
    #
    # On Linux: audio_server_lib is STATIC and plugins have undefined symbols
    # that are resolved from the loading executable/Python module at dlopen.
    #
    # On Mac: Use -undefined dynamic_lookup to allow symbols to be resolved
    # at runtime from the loading executable/Python module.
    #
    # On Windows: audio_server_lib is SHARED, creating an import library that
    # plugins link against. The symbols will be resolved from audio_server_lib.dll
    # at runtime. The audio_server_lib.dll must be present alongside the plugins.
    if(PLATFORM_WINDOWS)
        target_link_libraries(${target} PRIVATE audio_server_lib)
    endif()

    # Suppress REGISTER_PLUGIN static-init so only register_plugin() fires.
    target_compile_definitions(${target} PRIVATE AS_PLUGIN_DYNAMIC)

    if(PL_LINK_LIBRARIES)
        target_link_libraries(${target} PRIVATE ${PL_LINK_LIBRARIES})
    endif()
    
    if(PL_LIBRARY_DIRECTORIES)
        target_link_directories(${target} PRIVATE ${PL_LIBRARY_DIRECTORIES})
    endif()

    # Use an absolute path resolved at configure time to avoid CMake silently
    # placing MODULE targets in the build tree.  Also set the per-config
    # variants so Debug/Release builds don't add a subdirectory.
    get_filename_component(_plugins_dir "${CMAKE_SOURCE_DIR}/../plugins" ABSOLUTE)
    set_target_properties(${target} PROPERTIES
        PREFIX ""
        LIBRARY_OUTPUT_DIRECTORY           "${_plugins_dir}"
        LIBRARY_OUTPUT_DIRECTORY_DEBUG     "${_plugins_dir}"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE   "${_plugins_dir}"
        LIBRARY_OUTPUT_DIRECTORY_RELWITHDEBINFO "${_plugins_dir}"
    )
    
    # On Mac, allow undefined symbols to be resolved at runtime
    if(PLATFORM_MAC)
        target_link_options(${target} PRIVATE "-undefined" "dynamic_lookup")
    endif()
endfunction()

# Ensure the output directory exists at configure time.
file(MAKE_DIRECTORY "${CMAKE_SOURCE_DIR}/../plugins")

# sine / control_source / mixer remain statically linked.
plugin_library(note_gate       SOURCES plugins/builtin/note_gate_plugin.cpp)
plugin_library(control_monitor SOURCES plugins/builtin/control_monitor_plugin.cpp)
plugin_library(arpeggiator     SOURCES plugins/builtin/arpeggiator_plugin.cpp)
plugin_library(risset_rhythm SOURCES plugins/builtin/risset_rhythm_plugin.cpp)
plugin_library(shepard_tone SOURCES plugins/builtin/shepard_tone_plugin.cpp)

# --- Control utility plugins (no extra deps) ---
plugin_library(control_multiply SOURCES plugins/builtin/control_multiply_plugin.cpp)
plugin_library(control_add      SOURCES plugins/builtin/control_add_plugin.cpp)
plugin_library(control_to_note  SOURCES plugins/builtin/control_to_note_plugin.cpp)
plugin_library(control_lfo     SOURCES plugins/builtin/control_lfo_plugin.cpp)
plugin_library(control_resonator  SOURCES plugins/builtin/control_resonator_plugin.cpp)
plugin_library(control_delay_line  SOURCES plugins/builtin/control_delay_line_plugin.cpp)

# --- Audio effect plugins ---
plugin_library(amp              SOURCES plugins/builtin/amp_plugin.cpp)
plugin_library(filter           SOURCES plugins/builtin/filter_plugin.cpp)
plugin_library(pitch_shift      SOURCES plugins/builtin/pitch_shift_plugin.cpp)
plugin_library(reverb          SOURCES plugins/builtin/reverb_plugin.cpp)
plugin_library(audio_delay_line  SOURCES plugins/builtin/audio_delay_line_plugin.cpp)
plugin_library(tape_stop  SOURCES plugins/builtin/tape_stop_plugin.cpp)
plugin_library(flanger  SOURCES plugins/builtin/flanger_plugin.cpp)

plugin_library(pattern_stamper  SOURCES plugins/builtin/pattern_stamper_plugin.cpp)

plugin_library(midi_capture SOURCES plugins/builtin/midi_capture_plugin.cpp)

plugin_library(microtonal_mapper SOURCES plugins/builtin/microtonal_mapper_plugin.cpp)

# --- Sampler (libsndfile for audio I/O) ---
if(ENABLE_SNDFILE)
    plugin_library(sampler
        SOURCES plugins/builtin/sampler_plugin.cpp
        LINK_LIBRARIES ${SNDFILE_LIBRARIES}
        INCLUDE_DIRECTORIES ${SNDFILE_INCLUDE_DIRS}
        LIBRARY_DIRECTORIES ${SNDFILE_LIBRARY_DIRS}
    )
    target_compile_definitions(arranger_plugin_sampler PRIVATE AS_ENABLE_SNDFILE)
else()
    # Build without sndfile — still registers and compiles, just loads no audio
    plugin_library(sampler SOURCES plugins/builtin/sampler_plugin.cpp)
endif()


# MODULE libraries are excluded from ALL by default in CMake.
# Tie them to audio_server so `make` / `make audio_server` builds them too.
add_dependencies(audio_server_lib
    arranger_plugin_note_gate
    arranger_plugin_control_monitor
    arranger_plugin_reverb
    arranger_plugin_arpeggiator
    arranger_plugin_control_lfo
    arranger_plugin_control_multiply
    arranger_plugin_control_resonator
    arranger_plugin_control_add
    arranger_plugin_control_to_note
    arranger_plugin_control_delay_line
    arranger_plugin_audio_delay_line
    arranger_plugin_tape_stop
    arranger_plugin_flanger
    arranger_plugin_amp
    arranger_plugin_filter
    arranger_plugin_pitch_shift
    arranger_plugin_sampler
    arranger_plugin_risset_rhythm
    arranger_plugin_shepard_tone    
    arranger_plugin_midi_capture
    arranger_plugin_microtonal_mapper
    arranger_plugin_pattern_stamper
)

if(ENABLE_SF2)
    plugin_library(fluidsynth
        SOURCES plugins/builtin/fluidsynth_plugin.cpp
        LINK_LIBRARIES ${FLUIDSYNTH_LIBRARIES}
        INCLUDE_DIRECTORIES ${FLUIDSYNTH_INCLUDE_DIRS}
        LIBRARY_DIRECTORIES ${FLUIDSYNTH_LIBRARY_DIRS}
    )
    target_compile_definitions(arranger_plugin_fluidsynth PRIVATE AS_ENABLE_SF2)
    add_dependencies(audio_server_lib arranger_plugin_fluidsynth)
endif()

# ---------------------------------------------------------------------------
# Singing plugin (espeak-ng) — optional, auto-detected
# ---------------------------------------------------------------------------
if(ENABLE_ESPEAK)
    pkg_check_modules(ESPEAK espeak-ng)
    if(ESPEAK_FOUND)
        message(STATUS "espeak-ng: ${ESPEAK_VERSION} — singing plugin enabled")
        plugin_library(singing
            SOURCES plugins/builtin/singing_plugin.cpp
            LINK_LIBRARIES ${ESPEAK_LIBRARIES}
            INCLUDE_DIRECTORIES ${ESPEAK_INCLUDE_DIRS}
            LIBRARY_DIRECTORIES ${ESPEAK_LIBRARY_DIRS}
        )
        target_compile_definitions(arranger_plugin_singing PRIVATE AS_ENABLE_ESPEAK)
        add_dependencies(audio_server_lib arranger_plugin_singing)
    else()
        message(STATUS "espeak-ng: not found — singing plugin disabled"
                       " (install libespeak-ng-dev to enable, or set ENABLE_ESPEAK=OFF)")
    endif()
endif()

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
install(DIRECTORY "${CMAKE_SOURCE_DIR}/../plugins/"
        DESTINATION lib/arranger/plugins
        FILES_MATCHING PATTERN "*.so" PATTERN "*.dll" PATTERN "*.dylib")
