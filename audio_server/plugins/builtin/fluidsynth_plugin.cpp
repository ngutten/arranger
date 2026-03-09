// fluidsynth_plugin.cpp
// Port of FluidSynthNode to the Plugin API.
// SF2 soundfont-based MIDI synthesizer with:
//   - Scala/KBM microtonal tuning (Phase 2)
//   - Per-note pitch bends via fluid_synth_tune_notes (Phase 3)
//
// Only compiled when AS_ENABLE_SF2 is defined (same as original).

#ifdef AS_ENABLE_SF2

#include "plugin_api.h"
#include <fluidsynth.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <stdexcept>
#include <string>

// ---------------------------------------------------------------------------
// Scala file parsers (shared with microtonal_mapper_plugin)
// ---------------------------------------------------------------------------

struct ScalaScale {
    std::string description;
    std::vector<float> cents;  // scale degrees in cents from 1/1

    bool load(const std::string& path) {
        std::ifstream f(path);
        if (!f) return false;

        cents.clear();
        description.clear();

        std::string line;
        bool in_header = true;
        int expected_count = 0;

        while (std::getline(f, line)) {
            size_t comment = line.find('!');
            if (comment != std::string::npos)
                line = line.substr(0, comment);
            line.erase(0, line.find_first_not_of(" \t\r\n"));
            size_t last = line.find_last_not_of(" \t\r\n");
            if (last != std::string::npos) line.erase(last + 1);

            if (line.empty()) continue;

            if (in_header) {
                if (description.empty()) {
                    description = line;
                } else {
                    try { expected_count = std::stoi(line); }
                    catch (...) { return false; }
                    in_header = false;
                }
            } else {
                float cents_val;
                try {
                    if (line.find('/') != std::string::npos) {
                        size_t slash = line.find('/');
                        float num = std::stof(line.substr(0, slash));
                        float den = std::stof(line.substr(slash + 1));
                        if (den == 0.0f) return false;
                        cents_val = 1200.0f * std::log2f(num / den);
                    } else {
                        cents_val = std::stof(line);
                    }
                } catch (...) {
                    fprintf(stderr, "[FluidSynth] malformed scale line: %s\n", line.c_str());
                    return false;
                }
                cents.push_back(cents_val);
            }
        }

        if (static_cast<int>(cents.size()) != expected_count) {
            cents.clear();
            return false;
        }
        return !cents.empty();
    }
};

struct KeyboardMapping {
    int map_size = 0;
    int reference_note = 60;
    int reference_freq = 69;
    float reference_hz = 440.0f;
    int formal_octave = 0;
    std::vector<int> mapping;  // MIDI note offset -> scale degree (-1 = unmapped)

    bool load(const std::string& path) {
        std::ifstream f(path);
        if (!f) return false;

        std::string line;
        int line_num = 0;

        while (std::getline(f, line)) {
            size_t comment = line.find('!');
            if (comment != std::string::npos)
                line = line.substr(0, comment);
            line.erase(0, line.find_first_not_of(" \t\r\n"));
            size_t last = line.find_last_not_of(" \t\r\n");
            if (last != std::string::npos) line.erase(last + 1);

            if (line.empty()) continue;

            try {
                switch (line_num) {
                    case 0: map_size       = std::stoi(line); break;
                    case 1: reference_note = std::stoi(line); break;
                    case 2: reference_freq = std::stoi(line); break;
                    case 3: reference_hz   = std::stof(line); break;
                    case 4: formal_octave  = std::stoi(line); break;
                    case 5: break;  // first MIDI note of mapping
                    case 6: break;  // last MIDI note of mapping
                    default:
                        if (line == "x") mapping.push_back(-1);
                        else             mapping.push_back(std::stoi(line));
                        break;
                }
            } catch (...) {
                fprintf(stderr, "[FluidSynth] malformed .kbm line %d: %s\n",
                        line_num, line.c_str());
                return false;
            }
            line_num++;
        }
        return true;
    }
};

// ---------------------------------------------------------------------------
// FluidSynthPlugin
// ---------------------------------------------------------------------------

class FluidSynthPlugin final : public Plugin {
public:
    ~FluidSynthPlugin() override { teardown(); }

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.fluidsynth";
        d.display_name = "FluidSynth";
        d.category     = "Synth";
        d.doc          = "SF2 soundfont-based MIDI synthesizer with microtonal tuning support.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "events_in",  "Events",  "MIDI input (held notes)",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
        };

        d.config_params = {
            { "sf2_path", "Soundfont", "Path to .sf2 soundfont file",
              ConfigType::FilePath, "",
              "SF2 Files (*.sf2);;All Files (*)" },
            { "scl_path", "Scale File (.scl)",
              "Scala scale file defining microtonal pitch classes",
              ConfigType::FilePath, "",
              "Scala Scale (*.scl);;All Files (*)" },
            { "kbm_path", "Keyboard Mapping (.kbm)",
              "Optional keyboard mapping file. If empty, uses chromatic mapping.",
              ConfigType::FilePath, "",
              "Scala Keyboard Mapping (*.kbm);;All Files (*)" },
            { "reference_note", "Reference Note",
              "MIDI note number for scale degree 0 (1/1). Default: 60 (middle C)",
              ConfigType::Integer, "60" },
            { "force_octave", "Force 2/1 Octave",
              "Force octave repetition at 2/1 (1200 cents) even if the scale specifies otherwise.",
              ConfigType::Bool, "true" },
        };

        return d;
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "sf2_path") {
            sf2_path_ = value;
            if (fs_) reload_sf2();
        } else if (key == "scl_path") {
            scl_path_ = value;
            reload_scale();
            if (fs_) apply_tuning_to_channels();
        } else if (key == "kbm_path") {
            kbm_path_ = value;
            reload_mapping();
            if (fs_) apply_tuning_to_channels();
        } else if (key == "reference_note") {
            try { reference_note_ = std::max(0, std::min(127, std::stoi(value))); }
            catch (...) { fprintf(stderr, "[FluidSynth] invalid reference_note: %s\n", value.c_str()); return; }
            compute_base_pitches();
            if (fs_) apply_tuning_to_channels();
        } else if (key == "force_octave") {
            force_octave_ = (value == "true" || value == "1");
            compute_base_pitches();
            if (fs_) apply_tuning_to_channels();
        }
    }

    void activate(float sample_rate, int max_block_size) override {
        sample_rate_ = sample_rate;
        block_size_  = max_block_size;

        fset_ = new_fluid_settings();
        fluid_settings_setnum(fset_, "synth.sample-rate", sample_rate);
        fluid_settings_setnum(fset_, "synth.gain", 0.15);
        fluid_settings_setint(fset_, "synth.threadsafe-api", 0);

        fs_ = new_fluid_synth(fset_);

        // Initialize bend_cents_ to zero
        for (int ch = 0; ch < 16; ++ch)
            for (int n = 0; n < 128; ++n)
                bend_cents_[ch][n] = 0.0;

        reload_scale();
        reload_mapping();
        compute_base_pitches();

        if (!sf2_path_.empty()) reload_sf2();
    }

    void deactivate() override { teardown(); }

    void note_on(int ch, int pitch, int vel) override {
        if (fs_) fluid_synth_noteon(fs_, ch, pitch, vel);
    }

    void note_off(int ch, int pitch) override {
        if (!fs_) return;
        fluid_synth_noteoff(fs_, ch, pitch);
        // Reset this note's per-note bend so the next note on this
        // channel+pitch starts clean.
        if (ch >= 0 && ch < 16 && pitch >= 0 && pitch < 128) {
            bend_cents_[ch][pitch] = 0.0;
            double p = base_pitch_[pitch];
            int key = pitch;
            fluid_synth_tune_notes(fs_, ch, 0, 1, &key, &p, /*apply=*/false);
        }
    }

    void program_change(int ch, int bank, int prog) override {
        if (fs_ && sfid_ >= 0)
            fluid_synth_program_select(fs_, ch, sfid_, bank, prog);
    }

    void pitch_bend(int ch, int value) override {
        if (fs_) fluid_synth_pitch_bend(fs_, ch, value);
    }

    void channel_volume(int ch, int volume) override {
        if (fs_) fluid_synth_cc(fs_, ch, 7, std::max(0, std::min(127, volume)));
    }

    void all_notes_off(int channel) override {
        if (!fs_) return;
        if (channel == -1) {
            for (int ch = 0; ch < 16; ++ch) {
                fluid_synth_cc(fs_, ch, 123, 0);
                fluid_synth_cc(fs_, ch, 120, 0);
            }
        } else {
            fluid_synth_cc(fs_, channel, 123, 0);
            fluid_synth_cc(fs_, channel, 120, 0);
        }
    }

    void note_tune(int ch, int note, float semitones) override {
        if (!fs_ || ch < 0 || ch >= 16 || note < 0 || note >= 128) return;
        bend_cents_[ch][note] = semitones * 100.0;
        double pitch = base_pitch_[note] + bend_cents_[ch][note];
        int key = note;
        fluid_synth_tune_notes(fs_, ch, 0, 1, &key, &pitch, /*apply=*/true);
    }

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* audio = buffers.audio.get("audio_out");
        if (!audio || !fs_) return;

        fluid_synth_write_float(fs_, ctx.block_size,
                                audio->left, 0, 1,
                                audio->right, 0, 1);

        for (int i = 0; i < ctx.block_size; ++i) {
            if (audio->left[i]  >  0.95f || audio->left[i]  < -0.95f)
                audio->left[i]  = std::tanh(audio->left[i]);
            if (audio->right[i] >  0.95f || audio->right[i] < -0.95f)
                audio->right[i] = std::tanh(audio->right[i]);
        }
    }

private:
    std::string       sf2_path_;
    std::string       scl_path_;
    std::string       kbm_path_;
    int               reference_note_ = 60;
    bool              force_octave_   = true;

    fluid_synth_t*    fs_   = nullptr;
    fluid_settings_t* fset_ = nullptr;
    int               sfid_ = -1;
    float             sample_rate_ = 44100.0f;
    int               block_size_  = 0;

    ScalaScale      scale_;
    KeyboardMapping kbm_;
    bool            has_kbm_ = false;

    // base_pitch_[n] = absolute pitch in cents for MIDI note n (MIDI 0 = 0, A4 = 6900)
    // (equal temperament when no scale is loaded)
    double base_pitch_[128] = {};

    // Per-note, per-channel bend offset in cents (from note_tune events)
    double bend_cents_[16][128] = {};

    // ---------------------------------------------------------------------------
    // Scale / tuning helpers
    // ---------------------------------------------------------------------------

    void reload_scale() {
        if (scl_path_.empty()) {
            scale_.cents.clear();
        } else if (!scale_.load(scl_path_)) {
            scale_.cents.clear();
        }
        compute_base_pitches();
    }

    void reload_mapping() {
        if (kbm_path_.empty()) {
            has_kbm_ = false;
        } else {
            has_kbm_ = kbm_.load(kbm_path_);
        }
        compute_base_pitches();
    }

    // Map MIDI note to scale degree
    int midi_to_scale_degree(int midi_note) const {
        if (has_kbm_ && !kbm_.mapping.empty()) {
            int offset = midi_note - reference_note_;
            if (offset >= 0 && offset < static_cast<int>(kbm_.mapping.size()))
                return kbm_.mapping[offset];
        }
        return midi_note - reference_note_;
    }

    // Convert scale degree to cents — absolute pitch where MIDI note N = N*100 cents
    // (i.e. A4 = MIDI 69 = 6900 cents, as required by fluid_synth_activate_key_tuning).
    double scale_degree_to_cents(int degree) const {
        if (scale_.cents.empty())
            return (degree + reference_note_) * 100.0;

        int scale_size = static_cast<int>(scale_.cents.size());
        double octave_cents = force_octave_ ? 1200.0 : scale_.cents.back();

        int octave = degree / scale_size;
        int degree_in_octave = degree % scale_size;
        if (degree_in_octave < 0) {
            degree_in_octave += scale_size;
            --octave;
        }

        double degree_cents = (degree_in_octave > 0)
            ? scale_.cents[degree_in_octave - 1]
            : 0.0;

        double total_cents = octave * octave_cents + degree_cents;
        double reference_abs = reference_note_ * 100.0;
        return reference_abs + total_cents;
    }

    void compute_base_pitches() {
        for (int n = 0; n < 128; ++n) {
            int degree = midi_to_scale_degree(n);
            base_pitch_[n] = scale_degree_to_cents(degree);
        }
    }

    // Apply tuning tables to all 16 channels using the current base_pitch_
    // and any accumulated bend_cents_.
    void apply_tuning_to_channels() {
        if (!fs_) return;
        double pitches[128];
        for (int ch = 0; ch < 16; ++ch) {
            for (int n = 0; n < 128; ++n)
                pitches[n] = base_pitch_[n] + bend_cents_[ch][n];
            char name[32];
            std::snprintf(name, sizeof(name), "ch%d", ch);
            fluid_synth_activate_key_tuning(fs_, ch, 0, name, pitches, /*apply=*/true);
            fluid_synth_activate_tuning(fs_, ch, ch, 0, /*apply=*/true);
        }
    }

    // ---------------------------------------------------------------------------
    // SF2 helpers
    // ---------------------------------------------------------------------------

    void reload_sf2() {
        if (!fs_ || sf2_path_.empty()) return;
        if (sfid_ >= 0) {
            fluid_synth_sfunload(fs_, sfid_, 1);
            sfid_ = -1;
        }
        sfid_ = fluid_synth_sfload(fs_, sf2_path_.c_str(), 1);
        if (sfid_ == FLUID_FAILED) {
            sfid_ = -1;
            return;
        }
        for (int ch = 0; ch < 16; ++ch)
            if (ch != 9)
                fluid_synth_program_select(fs_, ch, sfid_, 0, 0);

        // Apply tuning tables after soundfont is loaded
        apply_tuning_to_channels();
    }

    void teardown() {
        if (fs_)   { delete_fluid_synth(fs_);      fs_   = nullptr; }
        if (fset_) { delete_fluid_settings(fset_); fset_ = nullptr; }
        sfid_ = -1;
    }
};

REGISTER_PLUGIN(FluidSynthPlugin);
REGISTER_PLUGIN_DYNAMIC(FluidSynthPlugin);

std::unique_ptr<Plugin> make_fluidsynth_plugin() { return std::make_unique<FluidSynthPlugin>(); }

#endif // AS_ENABLE_SF2
