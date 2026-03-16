// test_pitch_delta.cpp — Simulate pitch delta tracking for a note sequence.
//
// Plays quarter notes at 120 BPM: C4 C4 C4 E4 G4 C5 A4 E4
// Each note is a separate note_on (no note_tune glide — these are discrete
// note changes). Then tests a portamento slide using note_tune events.
//
// Prints pitch_delta over time for delta_smooth = 0.02 and 0.2.

#include "../plugins/builtin/synth_common.h"
#include <cstdio>

struct EmptyExt {};

static const char* note_name(int pitch) {
    static const char* names[] = {
        "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"
    };
    static char buf[8];
    std::snprintf(buf, sizeof(buf), "%s%d", names[pitch % 12], pitch / 12 - 1);
    return buf;
}

static void run_scenario(const char* label, float delta_smooth) {
    std::printf("\n=== %s (delta_smooth=%.3f) ===\n", label, delta_smooth);

    VoiceManager<EmptyExt> vm;
    vm.init(44100.0f, 512);
    vm.delta_smooth = delta_smooth;

    float sr = 44100.0f;
    int bs = 512;
    float block_dur = bs / sr;  // ~11.6ms
    float bpm = 120.0f;
    float beat_dur = 60.0f / bpm;  // 0.5s per beat
    int blocks_per_beat = static_cast<int>(beat_dur / block_dur);  // ~43 blocks

    // Quarter notes at 120 BPM: C4 C4 C4 E4 G4 C5 A4 E4
    int notes[] = { 60, 60, 60, 64, 67, 72, 69, 64 };
    int n_notes = 8;
    using Voice = VoiceManager<EmptyExt>::Voice;
    Voice* current_voice = nullptr;

    std::printf("block_dur=%.4f  blocks_per_beat=%d\n", block_dur, blocks_per_beat);
    std::printf("\ntime_ms  block  event           pitch_delta  tanh(0.4*d)  tanh(1.0*d)\n");
    std::printf("-------  -----  --------------  -----------  -----------  -----------\n");

    int total_blocks = blocks_per_beat * n_notes + 20;
    int current_note = -1;

    for (int b = 0; b < total_blocks; ++b) {
        float time_s = b * block_dur;
        int beat_idx = static_cast<int>(time_s / beat_dur);

        // Trigger new note at beat boundaries
        if (beat_idx < n_notes && beat_idx != current_note) {
            if (current_note >= 0 && current_note < n_notes)
                vm.release_note(0, notes[current_note]);
            current_note = beat_idx;
            current_voice = vm.trigger(0, notes[current_note], 100,
                                       0.01f, 0.1f, 0.8f, 0.2f);
        }

        vm.begin_block(bs);

        bool at_note_start = (beat_idx < n_notes && beat_idx == current_note &&
                              b == static_cast<int>(beat_idx * beat_dur / block_dur));
        bool periodic = (b % 5 == 0);

        if ((at_note_start || periodic) && current_voice && current_voice->active) {
            float td = current_voice->pitch_delta;
            char evt_buf[16] = "              ";
            if (at_note_start && beat_idx < n_notes)
                std::snprintf(evt_buf, sizeof(evt_buf), "note_on %-3s   ", note_name(notes[beat_idx]));
            std::printf("%7.1f  %5d  %s  %11.4f  %11.4f  %11.4f\n",
                        time_s * 1000.0f, b, evt_buf, td,
                        std::tanh(0.4f * td), std::tanh(1.0f * td));
        }
    }
}

static void run_portamento(const char* label, float delta_smooth) {
    std::printf("\n=== %s (delta_smooth=%.3f) ===\n", label, delta_smooth);

    VoiceManager<EmptyExt> vm;
    vm.init(44100.0f, 512);
    vm.delta_smooth = delta_smooth;

    float sr = 44100.0f;
    int bs = 512;
    float block_dur = bs / sr;

    // Play C4, then slide to E4 over 250ms via note_tune events
    vm.trigger(0, 60, 100, 0.01f, 0.1f, 0.8f, 0.2f);

    float slide_start = 0.05f;   // start slide at 50ms
    float slide_dur   = 0.25f;   // 250ms slide
    float slide_end   = slide_start + slide_dur;
    float slide_semitones = 4.0f;  // C4 -> E4

    // Tune events arrive every ~15ms (like _BEND_RESOLUTION=32 at 120bpm)
    float tune_interval = 0.015f;

    std::printf("Slide C4->E4 over 250ms, tune events every 15ms\n");
    std::printf("\ntime_ms  block  tune_val  pitch_delta  tanh(0.4*d)  tanh(1.0*d)\n");
    std::printf("-------  -----  --------  -----------  -----------  -----------\n");

    int total_blocks = static_cast<int>(0.8f / block_dur);
    float next_tune = slide_start;

    for (int b = 0; b < total_blocks; ++b) {
        float time_s = b * block_dur;

        // Send tune events
        while (next_tune <= time_s + block_dur * 0.5f && next_tune <= slide_end + 0.001f) {
            float t = (next_tune - slide_start) / slide_dur;
            t = std::max(0.0f, std::min(1.0f, t));
            float semitones = t * slide_semitones;
            if (next_tune > slide_end) semitones = slide_semitones;
            vm.tune(0, 60, semitones);
            next_tune += tune_interval;
        }

        vm.begin_block(bs);

        auto& v = vm.voices[0];
        if (v.active && b % 2 == 0) {
            float td = v.pitch_delta;
            float tune_val = v.pitch_semitones - 60.0f;
            std::printf("%7.1f  %5d  %8.3f  %11.4f  %11.4f  %11.4f\n",
                        time_s * 1000.0f, b, tune_val, td,
                        std::tanh(0.4f * td), std::tanh(1.0f * td));
        }
    }
}

int main() {
    // Test 1: Discrete note changes (no portamento)
    run_scenario("Discrete notes", 0.02f);
    run_scenario("Discrete notes", 0.2f);

    // Test 2: Portamento slide via note_tune
    run_portamento("Portamento slide", 0.02f);
    run_portamento("Portamento slide", 0.2f);

    return 0;
}
