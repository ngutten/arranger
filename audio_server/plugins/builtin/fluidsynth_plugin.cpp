// fluidsynth_plugin.cpp
// Port of FluidSynthNode to the Plugin API.
// SF2 soundfont-based MIDI synthesizer.
//
// Only compiled when AS_ENABLE_SF2 is defined (same as original).

#ifdef AS_ENABLE_SF2

#include "plugin_api.h"
#include <fluidsynth.h>
#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>

class FluidSynthPlugin final : public Plugin {
public:
    ~FluidSynthPlugin() override { teardown(); }

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.fluidsynth";
        d.display_name = "FluidSynth";
        d.category     = "Synth";
        d.doc          = "SF2 soundfont-based MIDI synthesizer.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "audio_out", "Audio Out", "Stereo audio output",
              PluginPortType::AudioStereo, PortRole::Output },
        };

        d.config_params = {
            { "sf2_path", "Soundfont", "Path to .sf2 soundfont file",
              ConfigType::FilePath, "",
              "SF2 Files (*.sf2);;All Files (*)" },
        };

        return d;
    }

    void configure(const std::string& key, const std::string& value) override {
        if (key == "sf2_path") {
            sf2_path_ = value;
            // If already activated, reload the soundfont
            if (fs_) {
                reload_sf2();
            }
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

        if (!sf2_path_.empty()) {
            reload_sf2();
        }
    }

    void deactivate() override {
        teardown();
    }

    void note_on(int ch, int pitch, int vel) override {
        if (fs_) fluid_synth_noteon(fs_, ch, pitch, vel);
    }

    void note_off(int ch, int pitch) override {
        if (fs_) fluid_synth_noteoff(fs_, ch, pitch);
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

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* audio = buffers.audio.get("audio_out");
        if (!audio || !fs_) return;

        fluid_synth_write_float(fs_, ctx.block_size,
                                audio->left, 0, 1,
                                audio->right, 0, 1);

        // Conditional soft clip (only when approaching clipping)
        for (int i = 0; i < ctx.block_size; ++i) {
            if (audio->left[i]  >  0.95f || audio->left[i]  < -0.95f)
                audio->left[i]  = std::tanh(audio->left[i]);
            if (audio->right[i] >  0.95f || audio->right[i] < -0.95f)
                audio->right[i] = std::tanh(audio->right[i]);
        }
    }

private:
    std::string       sf2_path_;
    fluid_synth_t*    fs_   = nullptr;
    fluid_settings_t* fset_ = nullptr;
    int               sfid_ = -1;
    float             sample_rate_ = 44100.0f;
    int               block_size_  = 0;

    void reload_sf2() {
        if (!fs_ || sf2_path_.empty()) return;
        // Unload previous if any
        if (sfid_ >= 0) {
            fluid_synth_sfunload(fs_, sfid_, 1);
            sfid_ = -1;
        }
        sfid_ = fluid_synth_sfload(fs_, sf2_path_.c_str(), 1);
        if (sfid_ == FLUID_FAILED) {
            sfid_ = -1;
            return;  // Silently fail — no audio output until valid sf2 loaded
        }
        for (int ch = 0; ch < 16; ++ch)
            if (ch != 9)
                fluid_synth_program_select(fs_, ch, sfid_, 0, 0);
    }

    void teardown() {
        if (fs_)   { delete_fluid_synth(fs_);     fs_   = nullptr; }
        if (fset_) { delete_fluid_settings(fset_); fset_ = nullptr; }
        sfid_ = -1;
    }
};

REGISTER_PLUGIN(FluidSynthPlugin);

#endif // AS_ENABLE_SF2
