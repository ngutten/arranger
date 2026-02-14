// test/test_render.cpp
// End-to-end offline render test: builds a graph+schedule via AudioEngine,
// renders to WAV bytes, checks WAV header and that audio is non-silent.
// No PortAudio stream is opened (render_offline doesn't need one).

#include "audio_engine.h"
#include "nlohmann/json.hpp"

#include <iostream>
#include <cassert>
#include <cstring>
#include <cmath>
#include <algorithm>

using json = nlohmann::json;

// Read WAV header fields
struct WavHeader {
    uint32_t data_size;
    uint16_t channels;
    uint32_t sample_rate;
    uint16_t bit_depth;
};

static bool parse_wav_header(const std::vector<uint8_t>& wav, WavHeader& out) {
    if (wav.size() < 44) return false;
    if (std::memcmp(wav.data(),     "RIFF", 4) != 0) return false;
    if (std::memcmp(wav.data() + 8, "WAVE", 4) != 0) return false;

    auto read_u16 = [&](int off) -> uint16_t {
        return (uint16_t)wav[off] | ((uint16_t)wav[off+1] << 8);
    };
    auto read_u32 = [&](int off) -> uint32_t {
        return (uint32_t)wav[off] | ((uint32_t)wav[off+1]<<8) |
               ((uint32_t)wav[off+2]<<16) | ((uint32_t)wav[off+3]<<24);
    };

    out.channels    = read_u16(22);
    out.sample_rate = read_u32(24);
    out.bit_depth   = read_u16(34);
    out.data_size   = read_u32(40);
    return true;
}

int main() {
    std::cout << "=== test_render ===\n";

    AudioEngineConfig cfg;
    cfg.sample_rate = 44100.0f;
    cfg.block_size  = 512;
    AudioEngine engine(cfg);

    // Build a simple graph: sine synth → mixer
    json graph_desc = {
        {"bpm", 120},
        {"nodes", {
            {{"id","synth"}, {"type","sine"}},
            {{"id","mixer"}, {"type","mixer"}, {"channel_count",1}}
        }},
        {"connections", {
            {{"from_node","synth"},{"from_port","audio_out_L"},
             {"to_node","mixer"}, {"to_port","audio_in_L_0"}},
            {{"from_node","synth"},{"from_port","audio_out_R"},
             {"to_node","mixer"}, {"to_port","audio_in_R_0"}}
        }}
    };

    std::string err = engine.set_graph(graph_desc.dump());
    if (!err.empty()) {
        std::cerr << "set_graph failed: " << err << "\n";
        return 1;
    }
    std::cout << "PASS: graph set\n";

    // Schedule: A4 (69) for 2 beats, then C5 (72) for 2 beats
    json sched = {{"events", {
        {{"beat",0.0},{"type","note_on"}, {"node_id","synth"},{"channel",0},{"pitch",69},{"velocity",100}},
        {{"beat",2.0},{"type","note_off"},{"node_id","synth"},{"channel",0},{"pitch",69},{"velocity",0}},
        {{"beat",2.0},{"type","note_on"}, {"node_id","synth"},{"channel",0},{"pitch",72},{"velocity",80}},
        {{"beat",4.0},{"type","note_off"},{"node_id","synth"},{"channel",0},{"pitch",72},{"velocity",0}},
    }}};

    err = engine.set_schedule(sched.dump());
    if (!err.empty()) {
        std::cerr << "set_schedule failed: " << err << "\n";
        return 1;
    }
    std::cout << "PASS: schedule set (4 beats)\n";

    // Offline render
    auto wav = engine.render_offline_wav(0.5f);
    if (wav.empty()) {
        std::cerr << "render returned empty\n";
        return 1;
    }
    std::cout << "PASS: render returned " << wav.size() << " bytes\n";

    // Parse and verify WAV header
    WavHeader hdr;
    assert(parse_wav_header(wav, hdr));
    assert(hdr.channels    == 2);
    assert(hdr.sample_rate == 44100);
    assert(hdr.bit_depth   == 16);
    std::cout << "PASS: WAV header valid, data_size=" << hdr.data_size << "\n";

    // Check audio is non-silent: read s16 samples after 44-byte header
    size_t n_samples = hdr.data_size / 2;
    const int16_t* samples = reinterpret_cast<const int16_t*>(wav.data() + 44);
    int16_t peak = 0;
    for (size_t i = 0; i < n_samples; ++i)
        peak = std::max(peak, static_cast<int16_t>(std::abs(samples[i])));

    std::cout << "PASS: peak sample value = " << peak << "\n";
    assert(peak > 100);  // 440Hz sine at amp 0.15 should give ~4000+ peak

    std::cout << "All render tests passed.\n";
    return 0;
}
