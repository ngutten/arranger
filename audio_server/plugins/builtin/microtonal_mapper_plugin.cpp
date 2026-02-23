// microtonal_mapper_plugin.cpp
// Microtonal scale mapper with channel allocation for pitch-bent notes.
//
// Takes MIDI note events, maps them to microtonal pitches using Scala .scl
// format, then outputs quantized MIDI notes + pitch bends. When multiple
// notes require different pitch bends, allocates them across channels 11-16
// to avoid conflicts (MIDI's per-channel pitch bend limitation).
//
// File formats:
//   .scl — Scala scale format (pitch classes in cents)
//   .kbm — Scala keyboard mapping (maps MIDI keys to scale degrees)
//
// If no .kbm is provided, uses chromatic mapping starting from reference note.

#include "plugin_api.h"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// ---------------------------------------------------------------------------
// Scala file parsers
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
            // Strip comments
            size_t comment = line.find('!');
            if (comment != std::string::npos)
                line = line.substr(0, comment);
            
            // Trim whitespace
            line.erase(0, line.find_first_not_of(" \t\r\n"));
            line.erase(line.find_last_not_of(" \t\r\n") + 1);
            
            if (line.empty()) continue;
            
            if (in_header) {
                if (description.empty()) {
                    description = line;
                } else {
                    expected_count = std::stoi(line);
                    in_header = false;
                }
            } else {
                // Parse pitch: either cents (100.0) or ratio (3/2)
                float cents_val;
                if (line.find('/') != std::string::npos) {
                    // Ratio format
                    size_t slash = line.find('/');
                    float num = std::stof(line.substr(0, slash));
                    float den = std::stof(line.substr(slash + 1));
                    cents_val = 1200.0f * std::log2(num / den);
                } else {
                    // Cents format
                    cents_val = std::stof(line);
                }
                cents.push_back(cents_val);
            }
        }
        
        // Verify count matches
        if (static_cast<int>(cents.size()) != expected_count) {
            cents.clear();
            return false;
        }
        
        return !cents.empty();
    }
};

struct KeyboardMapping {
    int map_size = 0;              // Number of mapped keys (0 = use scale size)
    int reference_note = 60;       // MIDI note for scale degree 0 (middle C)
    int reference_freq = 69;       // MIDI note at reference frequency
    float reference_hz = 440.0f;   // Reference frequency
    int formal_octave = 0;         // Degree where octave repeats (0 = last in scale)
    std::vector<int> mapping;      // MIDI note -> scale degree (-1 = unmapped)
    
    bool load(const std::string& path) {
        std::ifstream f(path);
        if (!f) return false;
        
        std::string line;
        int line_num = 0;
        
        while (std::getline(f, line)) {
            // Strip comments
            size_t comment = line.find('!');
            if (comment != std::string::npos)
                line = line.substr(0, comment);
            
            line.erase(0, line.find_first_not_of(" \t\r\n"));
            line.erase(line.find_last_not_of(" \t\r\n") + 1);
            
            if (line.empty()) continue;
            
            switch (line_num) {
                case 0: map_size = std::stoi(line); break;
                case 1: reference_note = std::stoi(line); break;
                case 2: reference_freq = std::stoi(line); break;
                case 3: reference_hz = std::stof(line); break;
                case 4: formal_octave = std::stoi(line); break;
                case 5: /* first MIDI note of mapping - handled below */ break;
                case 6: /* last MIDI note - handled below */ break;
                default:
                    // Mapping entries
                    if (line == "x") {
                        mapping.push_back(-1);  // unmapped
                    } else {
                        mapping.push_back(std::stoi(line));
                    }
                    break;
            }
            line_num++;
        }
        
        return true;
    }
};

// ---------------------------------------------------------------------------
// Microtonal Mapper Plugin
// ---------------------------------------------------------------------------

class MicrotonalMapperPlugin final : public Plugin {
public:
    
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.microtonal_mapper";
        d.display_name = "Microtonal Mapper";
        d.category     = "Event";
        d.doc = 
            "Maps MIDI notes to microtonal pitches using Scala .scl files. "
            "Outputs quantized notes + pitch bends, allocating overlapping "
            "pitch-bent notes across channels 11-16 to avoid conflicts. "
            "Optionally load .kbm for custom keyboard mappings.";
        d.author  = "builtin";
        d.version = 1;
        
        d.ports = {
            { "events_in", "Events In", 
              "MIDI note events to map",
              PluginPortType::Event, PortRole::Input },
            { "events_out", "Events Out",
              "Mapped notes with pitch bends",
              PluginPortType::Event, PortRole::Output },
        };
        
        d.config_params = {
            { "scl_path", "Scale File (.scl)",
              "Scala scale file defining pitch classes in cents",
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
              "Force octave repetition at 2/1 (1200 cents) even if scale file specifies a different interval. "
              "Useful for tetrachords and other scale fragments.",
              ConfigType::Bool, "true" },
        };
        
        return d;
    }
    
    void configure(const std::string& key, const std::string& value) override {
        if (key == "scl_path") {
            scl_path_ = value;
            reload_scale();
        } else if (key == "kbm_path") {
            kbm_path_ = value;
            reload_mapping();
        } else if (key == "reference_note") {
            reference_note_ = std::max(0, std::min(127, std::stoi(value)));
        } else if (key == "force_octave") {
            force_octave_ = (value == "true" || value == "1");
        }
    }
    
    void activate(float sample_rate, int max_block_size) override {
        sample_rate_ = sample_rate;
        reload_scale();
        reload_mapping();
    }
    
    void deactivate() override {
        active_notes_.clear();
        channel_state_.clear();
    }
    
    void note_on(int channel, int pitch, int velocity) override {
        // Store incoming note for processing in process()
        pending_note_ons_.push_back({channel, pitch, velocity});
    }
    
    void note_off(int channel, int pitch) override {
        pending_note_offs_.push_back({channel, pitch});
    }
    
    void all_notes_off(int channel) override {
        // Forward all notes off to all allocated channels
        pending_all_notes_off_ = true;
        all_notes_off_channel_ = channel;
    }
    
    void program_change(int channel, int bank, int program) override {
        // Store for forwarding to allocated channels
        source_program_[channel] = program;
        source_bank_[channel] = bank;
    }
    
    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* ev_out = buffers.events.get("events_out");
        if (!ev_out || !ev_out->output_events) return;
        
        auto& out = *ev_out->output_events;
        out.clear();
        
        // Handle all notes off
        if (pending_all_notes_off_) {
            if (all_notes_off_channel_ == -1) {
                for (int ch = 11; ch <= 16; ++ch) {
                    out.push_back({0, static_cast<uint8_t>(0xB0 | (ch - 1)), 123, 0, 
                                   static_cast<uint8_t>(ch - 1)});
                    out.push_back({0, static_cast<uint8_t>(0xB0 | (ch - 1)), 120, 0,
                                   static_cast<uint8_t>(ch - 1)});
                }
            } else {
                out.push_back({0, static_cast<uint8_t>(0xB0 | (all_notes_off_channel_ - 1)), 
                              123, 0, static_cast<uint8_t>(all_notes_off_channel_ - 1)});
                out.push_back({0, static_cast<uint8_t>(0xB0 | (all_notes_off_channel_ - 1)),
                              120, 0, static_cast<uint8_t>(all_notes_off_channel_ - 1)});
            }
            active_notes_.clear();
            channel_state_.clear();
            pending_all_notes_off_ = false;
        }
        
        // Process note offs
        for (const auto& [src_ch, src_pitch] : pending_note_offs_) {
            int key = src_ch * 128 + src_pitch;
            auto it = active_notes_.find(key);
            if (it == active_notes_.end()) continue;
            
            const auto& note = it->second;
            
            // Send note off
            out.push_back({0, static_cast<uint8_t>(0x80 | (note.output_channel - 1)),
                          static_cast<uint8_t>(note.output_pitch), 0,
                          static_cast<uint8_t>(note.output_channel - 1)});
            
            // Free the channel slot
            auto ch_it = channel_state_.find(note.output_channel);
            if (ch_it != channel_state_.end()) {
                ch_it->second.active_pitches.erase(note.output_pitch);
            }
            
            active_notes_.erase(it);
        }
        pending_note_offs_.clear();
        
        // Process note ons
        for (const auto& [src_ch, src_pitch, velocity] : pending_note_ons_) {
            if (scale_.cents.empty()) {
                // No scale loaded - pass through unmodified
                out.push_back({0, static_cast<uint8_t>(0x90 | (src_ch - 1)),
                              static_cast<uint8_t>(src_pitch), 
                              static_cast<uint8_t>(velocity),
                              static_cast<uint8_t>(src_ch - 1)});
                continue;
            }
            
            // Map MIDI note to scale degree
            int scale_degree = midi_to_scale_degree(src_pitch);
            
            // Calculate target pitch in cents from A440
            float target_cents = scale_degree_to_cents(scale_degree);
            
            // Find nearest MIDI note and pitch bend offset
            int nearest_midi = 69 + static_cast<int>(std::round(target_cents / 100.0f));
            float bend_cents = target_cents - (nearest_midi * 100.0f);
            int bend_value = cents_to_pitch_bend(bend_cents);
            
            // Allocate channel for this note
            int out_channel = allocate_channel(nearest_midi, bend_value, src_ch);
            
            // Send program change if needed (first note on this channel)
            auto& ch_state = channel_state_[out_channel];
            if (ch_state.active_pitches.empty() && 
                source_program_.count(src_ch) > 0) {
                int prog = source_program_[src_ch];
                int bank = source_bank_[src_ch];
                // Bank select MSB (CC 0)
                out.push_back({0, static_cast<uint8_t>(0xB0 | (out_channel - 1)),
                              0, static_cast<uint8_t>(bank), 
                              static_cast<uint8_t>(out_channel - 1)});
                // Program change
                out.push_back({0, static_cast<uint8_t>(0xC0 | (out_channel - 1)),
                              static_cast<uint8_t>(prog), 0,
                              static_cast<uint8_t>(out_channel - 1)});
            }
            
            // Send pitch bend
            out.push_back({0, static_cast<uint8_t>(0xE0 | (out_channel - 1)),
                          static_cast<uint8_t>(bend_value & 0x7F),
                          static_cast<uint8_t>((bend_value >> 7) & 0x7F),
                          static_cast<uint8_t>(out_channel - 1)});
            
            // Send note on
            out.push_back({0, static_cast<uint8_t>(0x90 | (out_channel - 1)),
                          static_cast<uint8_t>(nearest_midi),
                          static_cast<uint8_t>(velocity),
                          static_cast<uint8_t>(out_channel - 1)});
            
            // Track active note
            int key = src_ch * 128 + src_pitch;
            active_notes_[key] = {out_channel, nearest_midi, bend_value};
            ch_state.active_pitches.insert(nearest_midi);
            ch_state.current_bend = bend_value;
        }
        pending_note_ons_.clear();
    }

private:
    std::string scl_path_;
    std::string kbm_path_;
    int reference_note_ = 60;  // Middle C
    bool force_octave_ = true;  // Force 2/1 octave by default
    float sample_rate_ = 44100.0f;
    
    ScalaScale scale_;
    KeyboardMapping kbm_;
    bool has_kbm_ = false;
    
    // Active note tracking
    struct ActiveNote {
        int output_channel;
        int output_pitch;
        int pitch_bend;
    };
    std::unordered_map<int, ActiveNote> active_notes_;  // key = src_ch*128 + src_pitch
    
    // Channel allocation (channels 11-16, one-indexed)
    struct ChannelState {
        std::unordered_set<int> active_pitches;  // set of active pitch values
        int current_bend = 8192;  // center
    };
    std::unordered_map<int, ChannelState> channel_state_;
    
    // Program/bank tracking
    std::unordered_map<int, int> source_program_;
    std::unordered_map<int, int> source_bank_;
    
    // Pending events (from note_on/off methods)
    std::vector<std::tuple<int,int,int>> pending_note_ons_;  // ch, pitch, vel
    std::vector<std::pair<int,int>> pending_note_offs_;      // ch, pitch
    bool pending_all_notes_off_ = false;
    int all_notes_off_channel_ = -1;
    
    void reload_scale() {
        if (scl_path_.empty()) {
            scale_.cents.clear();
            return;
        }
        if (!scale_.load(scl_path_)) {
            scale_.cents.clear();
        }
    }
    
    void reload_mapping() {
        if (kbm_path_.empty()) {
            has_kbm_ = false;
            return;
        }
        has_kbm_ = kbm_.load(kbm_path_);
    }
    
    // Map MIDI note to scale degree using keyboard mapping or chromatic default
    int midi_to_scale_degree(int midi_note) const {
        if (has_kbm_ && !kbm_.mapping.empty()) {
            // Use .kbm mapping
            int offset = midi_note - reference_note_;
            if (offset >= 0 && offset < static_cast<int>(kbm_.mapping.size())) {
                return kbm_.mapping[offset];
            }
            // Outside mapping range - use chromatic fallback
        }
        
        // Chromatic mapping: each semitone = one scale degree
        return midi_note - reference_note_;
    }
    
    // Convert scale degree to cents from A440
    float scale_degree_to_cents(int degree) const {
        if (scale_.cents.empty()) return 0.0f;
        
        int scale_size = static_cast<int>(scale_.cents.size());
        
        // Determine octave size: force 1200 cents or use last scale entry
        float octave_cents = force_octave_ ? 1200.0f : scale_.cents.back();
        
        int octave = degree / scale_size;
        int degree_in_octave = degree % scale_size;
        if (degree_in_octave < 0) {
            degree_in_octave += scale_size;
            octave--;
        }
        
        float degree_cents = (degree_in_octave > 0) ? 
            scale_.cents[degree_in_octave - 1] : 0.0f;
        
        // Cents from reference note
        float total_cents = octave * octave_cents + degree_cents;
        
        // Convert to cents from A440 (MIDI note 69)
        // reference_note_ is at 0 cents in our scale
        float reference_from_a440 = (reference_note_ - 69) * 100.0f;
        return reference_from_a440 + total_cents;
    }
    
    // Convert cents deviation to MIDI pitch bend value (14-bit, 8192 = center)
    int cents_to_pitch_bend(float cents) const {
        // Assume ±200 cents range (standard MIDI pitch bend)
        float semitones = cents / 100.0f;
        float bend_range = 2.0f;  // ±2 semitones
        float normalized = semitones / bend_range;  // -1 to +1
        return static_cast<int>(8192 + normalized * 8191);
    }
    
    // Allocate a channel for this note
    int allocate_channel(int pitch, int bend_value, int source_channel) {
        // First, check if any channel already has this exact pitch+bend
        for (int ch = 11; ch <= 16; ++ch) {
            auto it = channel_state_.find(ch);
            if (it != channel_state_.end()) {
                if (it->second.current_bend == bend_value &&
                    it->second.active_pitches.count(pitch) > 0) {
                    // Already playing this exact pitch+bend on this channel
                    return ch;
                }
            }
        }
        
        // Find first available channel (no active notes or matching bend)
        for (int ch = 11; ch <= 16; ++ch) {
            auto it = channel_state_.find(ch);
            if (it == channel_state_.end() || it->second.active_pitches.empty()) {
                return ch;
            }
            // If channel has notes but same bend value, can reuse
            if (it->second.current_bend == bend_value) {
                return ch;
            }
        }
        
        // All channels busy with different bends - steal least recently used
        // For now, just wrap around (simple round-robin)
        static int next_channel = 11;
        int result = next_channel;
        next_channel = (next_channel - 11 + 1) % 6 + 11;
        return result;
    }
};

REGISTER_PLUGIN(MicrotonalMapperPlugin);
REGISTER_PLUGIN_DYNAMIC(MicrotonalMapperPlugin);

std::unique_ptr<Plugin> make_microtonal_mapper_plugin() {
    return std::make_unique<MicrotonalMapperPlugin>();
}
