// test/test_graph.cpp
// Tests graph construction: sine node → mixer, topo sort, buffer wiring,
// and one block of offline processing.  No PortAudio, no IPC.

#include "graph.h"
#include "scheduler.h"
#include "nlohmann/json.hpp"
#include "../plugins/builtin/synth_common.h"

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

// Regression: muting a track (or any mid-playback edit) rebuilds the schedule
// and drops the muted track's note_off.  A note already sounding must be
// reported as orphaned by the dispatcher so the engine can release it, instead
// of hanging forever (audible on sustained instruments like DDSP/wind).
static void test_orphaned_notes_on_swap() {
    std::cout << "--- orphaned-note-on-swap ---\n";
    std::string err;

    // A long held note: on at beat 0, off at beat 4.
    json held = {{"events", {
        {{"beat",0.0}, {"type","note_on"},  {"node_id","synth1"},
         {"channel",0},{"pitch",60}, {"velocity",100}},
        {{"beat",4.0}, {"type","note_off"}, {"node_id","synth1"},
         {"channel",0},{"pitch",60}, {"velocity",0}}
    }}};

    std::vector<Dispatcher::ActiveNote> orphans;

    // Case A: new schedule still releases the note (note_off survives at beat 4).
    // The note must NOT be reported as orphaned — its note_off will fire normally.
    {
        Dispatcher d;
        d.swap_schedule(Schedule::from_json(held.dump(), err).release());
        d.check_pending();
        // Need a graph to dispatch note_on; reuse the shared test graph.
        auto g = Graph::from_json(make_test_graph().dump(), err);
        g->activate(44100.0f, 512);
        d.dispatch(0.0, 1.0, g.get());            // fires note_on, note becomes active
        d.swap_schedule(Schedule::from_json(held.dump(), err).release());
        d.check_pending();                         // re-apply an equivalent schedule
        orphans.clear();
        d.collect_orphaned_notes(1.0, orphans);
        assert(orphans.empty());
        std::cout << "PASS: held note with surviving note_off is not orphaned\n";
        g->deactivate();
    }

    // Case B: new schedule drops the note_off (track muted → events removed).
    // The sounding note has no future release and MUST be reported as orphaned.
    {
        Dispatcher d;
        d.swap_schedule(Schedule::from_json(held.dump(), err).release());
        d.check_pending();
        auto g = Graph::from_json(make_test_graph().dump(), err);
        g->activate(44100.0f, 512);
        d.dispatch(0.0, 1.0, g.get());            // fires note_on, note becomes active
        json muted = {{"events", json::array()}}; // muted track contributes nothing
        d.swap_schedule(Schedule::from_json(muted.dump(), err).release());
        d.check_pending();
        orphans.clear();
        d.collect_orphaned_notes(1.0, orphans);
        assert(orphans.size() == 1);
        assert(orphans[0].node_id == "synth1");
        assert(orphans[0].channel == 0);
        assert(orphans[0].pitch   == 60);
        std::cout << "PASS: held note whose note_off was dropped is orphaned\n";
        // Once collected, it is removed from the active set — a second sweep is empty.
        orphans.clear();
        d.collect_orphaned_notes(1.0, orphans);
        assert(orphans.empty());
        std::cout << "PASS: orphan removed from active set after collection\n";
        g->deactivate();
    }

    // Case C: a note_off that lies in the past (before the playhead) does not
    // count as a release — reindex() skips it — so the note is still orphaned.
    {
        json past_off = {{"events", {
            {{"beat",4.0}, {"type","note_off"}, {"node_id","synth1"},
             {"channel",0},{"pitch",60}, {"velocity",0}}
        }}};
        Dispatcher d;
        d.swap_schedule(Schedule::from_json(held.dump(), err).release());
        d.check_pending();
        auto g = Graph::from_json(make_test_graph().dump(), err);
        g->activate(44100.0f, 512);
        d.dispatch(0.0, 1.0, g.get());
        d.swap_schedule(Schedule::from_json(past_off.dump(), err).release());
        d.check_pending();
        orphans.clear();
        d.collect_orphaned_notes(5.0, orphans);   // playhead past the only note_off
        assert(orphans.size() == 1);
        std::cout << "PASS: note_off in the past does not save the note from orphaning\n";
        g->deactivate();
    }
}

// The track fader (CC7) and pan (CC10) were silently ignored by every synth
// except FluidSynth.  VoiceManager now owns the per-channel gain/pan that the
// whole synth_common family rides, so any voice-based plugin honors the fader.
static void test_channel_volume_pan() {
    std::cout << "--- voice-manager channel volume/pan ---\n";
    auto approx = [](float a, float b) { return std::fabs(a - b) < 1e-4f; };

    VoiceManager<int> vm;
    vm.init(44100.0f, 512);
    VoiceManager<int>::Voice* v = vm.trigger(/*channel=*/0, /*pitch=*/60,
                                             /*velocity=*/127,
                                             0.01f, 0.1f, 0.8f, 0.1f);
    assert(v != nullptr);

    float gl, gr;

    // Default: unity gain, centre pan.
    vm.voice_amp(*v, gl, gr);
    assert(approx(gl, 1.0f) && approx(gr, 1.0f));
    std::cout << "PASS: default channel state is unity / centre\n";

    // CC7 uses the GM curve gain=(v/127)^2: full stays unity, half ~= 0.254.
    vm.set_channel_volume(0, 127);
    vm.voice_amp(*v, gl, gr);
    assert(approx(gl, 1.0f) && approx(gr, 1.0f));
    vm.set_channel_volume(0, 64);
    vm.voice_amp(*v, gl, gr);
    float expect_half = (64.0f / 127.0f) * (64.0f / 127.0f);
    assert(approx(gl, expect_half) && approx(gr, expect_half));
    std::cout << "PASS: CC7 follows GM (v/127)^2 curve\n";

    // CC7 = 0 fully mutes (this is what the mixer "M" / a pulled fader does).
    vm.set_channel_volume(0, 0);
    vm.voice_amp(*v, gl, gr);
    assert(approx(gl, 0.0f) && approx(gr, 0.0f));
    std::cout << "PASS: CC7=0 mutes the voice\n";

    // Pan: unity-at-centre balance, no boost. Restore full volume first.
    vm.set_channel_volume(0, 127);
    vm.set_channel_pan(0, 0);     // hard left
    vm.voice_amp(*v, gl, gr);
    assert(approx(gl, 1.0f) && approx(gr, 0.0f));
    vm.set_channel_pan(0, 127);   // hard right
    vm.voice_amp(*v, gl, gr);
    assert(approx(gl, 0.0f) && approx(gr, 1.0f));
    vm.set_channel_pan(0, 64);    // centre — no attenuation either side
    vm.voice_amp(*v, gl, gr);
    assert(approx(gl, 1.0f) && approx(gr, 1.0f));
    std::cout << "PASS: pan is unity-at-centre linear balance\n";

    // Per-channel isolation: a voice on channel 1 is untouched by channel 0.
    VoiceManager<int>::Voice* v1 = vm.trigger(1, 67, 127, 0.01f, 0.1f, 0.8f, 0.1f);
    assert(v1 != nullptr);
    vm.set_channel_volume(0, 0);
    vm.voice_amp(*v1, gl, gr);
    assert(approx(gl, 1.0f) && approx(gr, 1.0f));
    std::cout << "PASS: channel volume is per-channel (no cross-talk)\n";
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
    // The master section runs a look-ahead limiter with a ~5 ms delay line, so
    // the block right after a gain change still emits pre-change samples.
    // Process a couple of blocks to flush the delay before measuring.
    graph->process(ctx);
    graph->process(ctx);
    graph->process(ctx);
    float max_half = 0.0f;
    for (int i = 0; i < 512; ++i) max_half = std::max(max_half, std::abs(L[i]));
    // Signal is well below the limiter threshold, so halving master_gain halves
    // the peak (no gain reduction in play).
    std::cout << "PASS: set_param master_gain=0.5, new max = " << max_half << "\n";
    assert(max_half < max_val * 0.75f);

    graph->deactivate();

    // --- Schedule-swap note hygiene ---
    test_orphaned_notes_on_swap();

    // --- Track fader / pan honored by the voice family ---
    test_channel_volume_pan();

    std::cout << "All graph tests passed.\n";
    return 0;
}
