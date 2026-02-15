cmake_minimum_required(VERSION 3.18)
project(AudioServer VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ---------------------------------------------------------------------------
# Build options
# ---------------------------------------------------------------------------
option(ENABLE_LV2    "Build with LV2 plugin hosting (requires lilv)"  OFF)
option(ENABLE_SF2    "Build with SF2/FluidSynth support"              ON)
option(ENABLE_TESTS  "Build test programs"                            ON)

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
if(WIN32 OR MINGW OR CMAKE_SYSTEM_NAME STREQUAL "Windows")
    set(PLATFORM_WINDOWS TRUE)
    add_compile_definitions(AS_PLATFORM_WINDOWS)
else()
    set(PLATFORM_LINUX TRUE)
    add_compile_definitions(AS_PLATFORM_LINUX)
endif()

# ---------------------------------------------------------------------------
# Find dependencies
# ---------------------------------------------------------------------------

find_package(PkgConfig REQUIRED)
pkg_check_modules(PORTAUDIO REQUIRED portaudio-2.0)

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
            PATHS /usr/include /usr/local/include
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

if(ENABLE_LV2)
    pkg_check_modules(LILV REQUIRED lilv-0)
    pkg_check_modules(LV2 REQUIRED lv2)
    add_compile_definitions(AS_ENABLE_LV2)
endif()

if(ENABLE_SF2)
    pkg_check_modules(FLUIDSYNTH REQUIRED fluidsynth)
    add_compile_definitions(AS_ENABLE_SF2)
endif()

# ---------------------------------------------------------------------------
# Main server library (shared between server binary and tests)
# ---------------------------------------------------------------------------
add_library(audio_server_lib STATIC
    src/graph.cpp
    src/ipc.cpp
    src/scheduler.cpp
    src/synth_node.cpp
    src/audio_engine.cpp
    src/plugin_registry.cpp
    src/plugin_adapter.cpp
    src/builtin_plugins.cpp
    # Built-in plugins (Phase 2)
    plugins/builtin/sine_plugin.cpp
    plugins/builtin/note_gate_plugin.cpp
    plugins/builtin/control_source_plugin.cpp
    plugins/builtin/mixer_plugin.cpp
    # Phase 4 plugins
    plugins/builtin/reverb_plugin.cpp
    plugins/builtin/arpeggiator_plugin.cpp
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

if(_JSON_LINK_TARGET)
    target_link_libraries(audio_server_lib PUBLIC "${_JSON_LINK_TARGET}")
endif()

target_compile_options(audio_server_lib PRIVATE
    ${PORTAUDIO_CFLAGS_OTHER}
)

if(ENABLE_LV2)
    target_include_directories(audio_server_lib PUBLIC ${LILV_INCLUDE_DIRS} ${LV2_INCLUDE_DIRS})
    target_link_libraries(audio_server_lib PUBLIC ${LILV_LIBRARIES})
    target_sources(audio_server_lib PRIVATE src/lv2_host.cpp)
endif()

if(ENABLE_SF2)
    target_include_directories(audio_server_lib PUBLIC ${FLUIDSYNTH_INCLUDE_DIRS})
    target_link_libraries(audio_server_lib PUBLIC ${FLUIDSYNTH_LIBRARIES})
    target_sources(audio_server_lib PRIVATE plugins/builtin/fluidsynth_plugin.cpp)
endif()

if(PLATFORM_WINDOWS)
    target_link_libraries(audio_server_lib PUBLIC ws2_32)
endif()

   
# ---------------------------------------------------------------------------
# Server executable
# ---------------------------------------------------------------------------
add_executable(audio_server src/main.cpp)
target_link_libraries(audio_server PRIVATE audio_server_lib)

# Debugging
#target_compile_options(audio_server PRIVATE
#    -fsanitize=address,undefined -fno-omit-frame-pointer -g -O1)
#target_link_options(audio_server PRIVATE
#    -fsanitize=address,undefined)

add_compile_definitions(AS_DEBUG)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
if(ENABLE_TESTS)
    add_executable(test_ipc test/test_ipc.cpp)
    target_link_libraries(test_ipc PRIVATE audio_server_lib)

    add_executable(test_graph test/test_graph.cpp)
    target_link_libraries(test_graph PRIVATE audio_server_lib)

    add_executable(test_render test/test_render.cpp)
    target_link_libraries(test_render PRIVATE audio_server_lib)
endif()

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
install(TARGETS audio_server DESTINATION bin)
