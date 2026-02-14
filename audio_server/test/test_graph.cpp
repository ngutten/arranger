// test/test_graph.cpp
// Tests graph construction: sine node → mixer, topo sort, buffer wiring,
// and one block of offline processing.  No PortAudio, no IPC.

#include "graph.h"
#include "scheduler.h"
#include "nlohmann/json.hpp"

#include <iostream>
#include <cassert>
#include <cmath>

using json = nlohmann::json;

static json make_test_graph() {
    return {
        {"bpm", 120},
        {"sample_rate", 44100},
        {"nodes", {
            {{"id","synth1"}, {"type","sine"}},
            {{"id","mixer"},  {"type","mixer"}, {"channel_count", 1}}
        }},
        {"connections", {
            {{"from_node","synth1"}, {"from_port","audio_out_L"},
             {"to_node","mixer"},    {"to_port","audio_in_L_0"}},
            {{"from_node","synth1"}, {"from_port","audio_out_R"},
             {"to_node","mixer"},    {"to_port","audio_in_R_0"}}
        }}
    };
}

static json make_test_schedule() {
    return {{"events", {
        // note_on at beat 0, note_off at beat 1
        {{"beat",0.0}, {"type","note_on"},  {"node_id","synth1"},
         {"channel",0},{"pitch",69},        {"velocity",100}},
        {{"beat",1.0}, {"type","note_off"}, {"node_id","synth1"},
         {"channel",0},{"pitch",69},        {"velocity",0}}
    }}};
}

int main() {
    std::cout << "=== test_graph ===\n";

    // --- Build graph ---
    std::string err;
    auto graph = Graph::from_json(make_test_graph().dump(), err);
    if (!graph) {
        std::cerr << "Graph construction failed: " << err << "\n";
        return 1;
    }
    std::cout << "PASS: graph constructed\n";

    // --- Activate ---
    bool ok = graph->activate(44100.0f, 512);
    assert(ok);
    std::cout << "PASS: graph activated, eval_order size="
              << graph->eval_order().size() << "\n";
    assert(graph->eval_order().size() == 2);

    // Eval order should be: synth1 before mixer
    assert(graph->eval_order()[0] == "synth1");
    assert(graph->eval_order()[1] == "mixer");

    // --- Build schedule ---
    auto sched = Schedule::from_json(make_test_schedule().dump(), err);
    if (!sched) {
        std::cerr << "Schedule construction failed: " << err << "\n";
        return 1;
    }
    assert(sched->events().size() == 2);
    std::cout << "PASS: schedule built with " << sched->events().size() << " events\n";

    // --- Dispatcher: trigger note_on ---
    Dispatcher disp;
    disp.swap_schedule(sched.release());
    disp.check_pending();

    // Dispatch beat 0..0.01 (a few samples worth at 120bpm)
    disp.dispatch(0.0, 0.01, graph.get());

    // --- Process one block ---
    ProcessContext ctx;
    ctx.block_size      = 512;
    ctx.sample_rate     = 44100.0f;
    ctx.bpm             = 120.0f;
    ctx.beat_position   = 0.0;
    ctx.beats_per_sample = 120.0 / 60.0 / 44100.0;

    graph->process(ctx);

    // Mixer output should be non-zero (sine was triggered)
    const float* L = graph->output_L();
    const float* R = graph->output_R();
    assert(L && R);

    float max_val = 0.0f;
    for (int i = 0; i < 512; ++i) max_val = std::max(max_val, std::abs(L[i]));
    std::cout << "PASS: output non-silent, max amplitude = " << max_val << "\n";
    assert(max_val > 1e-6f);

    // --- set_param ---
    graph->set_param("mixer", "master_gain", 0.5f);
    graph->process(ctx);
    float max_half = 0.0f;
    for (int i = 0; i < 512; ++i) max_half = std::max(max_half, std::abs(L[i]));
    // gain halved, so peak should be noticeably lower (not exact due to tanh)
    std::cout << "PASS: set_param master_gain=0.5, new max = " << max_half << "\n";
    assert(max_half < max_val * 0.75f);

    graph->deactivate();
    std::cout << "All graph tests passed.\n";
    return 0;
}
