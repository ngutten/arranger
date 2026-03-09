// ddsp_plugin.cpp
// ==========================================================================
// DDSP (Differentiable Digital Signal Processing) Neural Synthesizer
// ==========================================================================
//
// Realtime neural synthesis: a small ONNX network converts MIDI features
// (f0, loudness) into parameters for an additive oscillator bank + filtered
// noise.  A helper thread runs inference at frame rate (~100fps); the audio
// thread interpolates parameters and synthesises at sample rate.
//
// Architecture:
//   [Helper thread]                     [Audio thread]
//     Read voice state (f0, loudness)     Read DDSPFrames from ring buffers
//     Run ONNX decoder per voice          Interpolate between frames
//     Push DDSPFrames to SPSC rings       Additive oscillator bank (harmonics)
//     Sleep until next 10ms frame         FFT-based filtered noise
//                                         ADSR envelope, mix to output
//
// ==========================================================================

#include "plugin_api.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef AS_ENABLE_DDSP
#include <onnxruntime_cxx_api.h>
#endif

// ==========================================================================
// Logging
// ==========================================================================

#define DDSP_LOG(fmt, ...) \
    std::fprintf(stderr, "[DDSP] " fmt "\n", ##__VA_ARGS__)

// ==========================================================================
// Constants
// ==========================================================================

static constexpr int MAX_HARMONICS   = 100;
static constexpr int MAX_NOISE_BANDS = 65;
static constexpr int MAX_VOICES      = 8;
static constexpr int RING_CAPACITY   = 8;   // frames per voice (~80ms @ 100fps)
static constexpr int SIN_TABLE_SIZE  = 1024;
static constexpr int NOISE_FFT_SIZE  = 1024;
static constexpr int NOISE_HOP_SIZE  = NOISE_FFT_SIZE / 2;  // 50% overlap

// ==========================================================================
// Wavetable sine lookup
// ==========================================================================

static float g_sin_table[SIN_TABLE_SIZE + 1];  // +1 for interpolation guard

static void init_sin_table() {
    static bool done = false;
    if (done) return;
    for (int i = 0; i <= SIN_TABLE_SIZE; ++i)
        g_sin_table[i] = std::sin(2.0f * static_cast<float>(M_PI) * i / SIN_TABLE_SIZE);
    done = true;
}

static inline float fast_sin(float phase) {
    // phase in [0,1)
    float idx = phase * SIN_TABLE_SIZE;
    int i = static_cast<int>(idx);
    float frac = idx - i;
    i &= (SIN_TABLE_SIZE - 1);
    return g_sin_table[i] + frac * (g_sin_table[i + 1] - g_sin_table[i]);
}

// ==========================================================================
// Radix-2 FFT (reused from sampler_plugin pattern)
// ==========================================================================

static void fft_complex_inplace(float* data, int N, bool inverse) {
    // Bit-reversal permutation
    for (int i = 1, j = 0; i < N; ++i) {
        int bit = N >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            std::swap(data[2 * i],     data[2 * j]);
            std::swap(data[2 * i + 1], data[2 * j + 1]);
        }
    }
    float sign = inverse ? 1.0f : -1.0f;
    for (int len = 2; len <= N; len <<= 1) {
        float ang = sign * 2.0f * static_cast<float>(M_PI) / len;
        float wR = std::cos(ang), wI = std::sin(ang);
        for (int i = 0; i < N; i += len) {
            float curR = 1.0f, curI = 0.0f;
            for (int j = 0; j < len / 2; ++j) {
                int u = i + j, v = u + len / 2;
                float tR = curR * data[2 * v] - curI * data[2 * v + 1];
                float tI = curR * data[2 * v + 1] + curI * data[2 * v];
                data[2 * v]     = data[2 * u]     - tR;
                data[2 * v + 1] = data[2 * u + 1] - tI;
                data[2 * u]     += tR;
                data[2 * u + 1] += tI;
                float nR = curR * wR - curI * wI;
                curI = curR * wI + curI * wR;
                curR = nR;
            }
        }
    }
    if (inverse) {
        float inv = 1.0f / static_cast<float>(N);
        for (int i = 0; i < 2 * N; ++i) data[i] *= inv;
    }
}

// ==========================================================================
// Hann window (pre-computed)
// ==========================================================================

static float g_hann_window[NOISE_FFT_SIZE];

static void init_hann_window() {
    static bool done = false;
    if (done) return;
    for (int i = 0; i < NOISE_FFT_SIZE; ++i)
        g_hann_window[i] = 0.5f * (1.0f - std::cos(2.0f * static_cast<float>(M_PI) * i / NOISE_FFT_SIZE));
    done = true;
}

// ==========================================================================
// DDSPFrame — per-voice parameter snapshot from inference
// ==========================================================================

struct DDSPFrame {
    float f0          = 0.0f;                         // Hz
    float amplitude   = 0.0f;                         // linear
    float harmonic_amps[MAX_HARMONICS] = {};           // linear amplitudes
    float noise_mags[MAX_NOISE_BANDS]  = {};           // linear magnitudes
    int   num_harmonics   = 0;
    int   num_noise_bands = 0;
    bool  valid           = false;
};

// ==========================================================================
// SPSCRingBuffer — lock-free single-producer single-consumer ring
// ==========================================================================

template <typename T, int Cap>
class SPSCRingBuffer {
public:
    SPSCRingBuffer() = default;

    void reset() {
        write_pos_.store(0, std::memory_order_relaxed);
        read_pos_.store(0, std::memory_order_relaxed);
    }

    bool push(const T& item) {
        uint32_t w = write_pos_.load(std::memory_order_relaxed);
        uint32_t r = read_pos_.load(std::memory_order_acquire);
        if (w - r >= Cap) return false;  // full
        buf_[w % Cap] = item;
        write_pos_.store(w + 1, std::memory_order_release);
        return true;
    }

    bool pop(T& item) {
        uint32_t r = read_pos_.load(std::memory_order_relaxed);
        uint32_t w = write_pos_.load(std::memory_order_acquire);
        if (r == w) return false;  // empty
        item = buf_[r % Cap];
        read_pos_.store(r + 1, std::memory_order_release);
        return true;
    }

    int available() const {
        uint32_t w = write_pos_.load(std::memory_order_acquire);
        uint32_t r = read_pos_.load(std::memory_order_relaxed);
        return static_cast<int>(w - r);
    }

private:
    T buf_[Cap] = {};
    std::atomic<uint32_t> write_pos_{0};
    std::atomic<uint32_t> read_pos_{0};
};

// ==========================================================================
// ADSR envelope
// ==========================================================================

struct ADSREnvelope {
    enum class Stage { Attack, Decay, Sustain, Release, Off };
    Stage stage = Stage::Off;
    float level = 0.0f;

    float attack_rate  = 0.0f;   // per sample
    float decay_rate   = 0.0f;
    float sustain_level = 0.8f;
    float release_rate = 0.0f;

    void trigger(float sample_rate, float attack_s, float decay_s,
                 float sustain, float release_s) {
        sustain_level = sustain;
        attack_rate   = (attack_s  > 0.001f) ? 1.0f / (attack_s  * sample_rate) : 1.0f;
        decay_rate    = (decay_s   > 0.001f) ? 1.0f / (decay_s   * sample_rate) : 1.0f;
        release_rate  = (release_s > 0.001f) ? 1.0f / (release_s * sample_rate) : 1.0f;
        stage = Stage::Attack;
        // don't reset level — allows retrigger without click
    }

    void release() {
        if (stage != Stage::Off)
            stage = Stage::Release;
    }

    float next() {
        switch (stage) {
        case Stage::Attack:
            level += attack_rate;
            if (level >= 1.0f) { level = 1.0f; stage = Stage::Decay; }
            break;
        case Stage::Decay:
            level -= decay_rate * (level - sustain_level);
            if (level <= sustain_level + 0.001f) {
                level = sustain_level;
                stage = Stage::Sustain;
            }
            break;
        case Stage::Sustain:
            break;
        case Stage::Release:
            level -= release_rate * level;
            if (level < 0.001f) { level = 0.0f; stage = Stage::Off; }
            break;
        case Stage::Off:
            level = 0.0f;
            break;
        }
        return level;
    }

    bool is_off() const { return stage == Stage::Off; }
};

// ==========================================================================
// DDSPVoice — per-voice state
// ==========================================================================

struct DDSPVoice {
    // --- Written by audio thread, read by helper ---
    std::atomic<bool> active{false};
    std::atomic<bool> releasing{false};
    int      channel  = 0;
    int      pitch    = 0;
    int      velocity = 0;
    float    f0_hz    = 0.0f;
    float    tune_semitones = 0.0f;   // from note_tune

    // --- Audio thread only ---
    float    harmonic_phase[MAX_HARMONICS] = {};  // phase accumulators [0,1)
    DDSPFrame last_frame;
    DDSPFrame cur_frame;
    float    frame_phase   = 0.0f;    // interpolation position [0,1)
    float    frame_advance = 0.0f;    // per-sample advance rate
    ADSREnvelope env;
    float    vel_gain = 1.0f;

    // Noise OLA state
    float    noise_ola_buf[NOISE_FFT_SIZE] = {};
    float    noise_fft_scratch[2 * NOISE_FFT_SIZE] = {};
    float    noise_out_buf[NOISE_FFT_SIZE] = {};
    int      noise_ola_pos = 0;  // position within current OLA window
    bool     noise_buf_ready = false;

    // --- Shared: helper writes, audio reads ---
    SPSCRingBuffer<DDSPFrame, RING_CAPACITY> ring;

    void reset() {
        active.store(false, std::memory_order_relaxed);
        releasing.store(false, std::memory_order_relaxed);
        channel = pitch = velocity = 0;
        f0_hz = 0.0f;
        tune_semitones = 0.0f;
        std::memset(harmonic_phase, 0, sizeof(harmonic_phase));
        last_frame = DDSPFrame{};
        cur_frame  = DDSPFrame{};
        frame_phase = 0.0f;
        frame_advance = 0.0f;
        env = ADSREnvelope{};
        vel_gain = 1.0f;
        std::memset(noise_ola_buf, 0, sizeof(noise_ola_buf));
        std::memset(noise_fft_scratch, 0, sizeof(noise_fft_scratch));
        std::memset(noise_out_buf, 0, sizeof(noise_out_buf));
        noise_ola_pos = 0;
        noise_buf_ready = false;
        ring.reset();
    }
};

// ==========================================================================
// RNG — simple xorshift for noise generation on audio thread
// ==========================================================================

struct XorShift32 {
    uint32_t state = 12345;
    float next() {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        // Map to [-1, 1)
        return static_cast<float>(static_cast<int32_t>(state)) / 2147483648.0f;
    }
};

// ==========================================================================
// DDSPPlugin
// ==========================================================================

class DDSPPlugin : public Plugin {
public:
    DDSPPlugin() {
        init_sin_table();
        init_hann_window();
    }

    ~DDSPPlugin() override {
        stop_helper_thread();
    }

    // ------------------------------------------------------------------
    // Descriptor
    // ------------------------------------------------------------------

    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.ddsp";
        d.display_name = "DDSP Synth";
        d.category     = "Synthesizer";
        d.doc          = "Neural synthesizer using DDSP (Differentiable Digital Signal "
                         "Processing). A small ONNX neural network converts MIDI input "
                         "into parameters for an additive oscillator bank and filtered "
                         "noise, producing expressive timbres in realtime.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "events_in", "Events In", "MIDI event input.",
              PluginPortType::Event, PortRole::Input },
            { "audio_out", "Audio Out", "Stereo audio output.",
              PluginPortType::AudioStereo, PortRole::Output },
            { "gain", "Gain", "Output gain multiplier. 1.0 = unity.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 2.0f },
            { "expression", "Expression", "Loudness expression control (0-1).",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 1.0f, 0.0f, 1.0f },
            { "brightness", "Brightness",
              "Harmonic brightness control. Higher values emphasize upper harmonics.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.5f, 0.0f, 1.0f },
            { "attack", "Attack",
              "Envelope attack time in seconds.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.01f, 0.001f, 2.0f },
            { "release", "Release",
              "Envelope release time in seconds.",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.1f, 0.001f, 5.0f },
        };

        d.config_params = {
            { "model_dir", "Model Directory",
              "Path to DDSP model directory containing decoder.onnx and config.json.",
              ConfigType::DirPath, "" },
        };

        return d;
    }

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    void configure(const std::string& key, const std::string& value) override {
        if (key == "model_dir") {
            pending_model_dir_ = value;
            model_dirty_ = true;
        }
    }

    void activate(float sample_rate, int /*max_block_size*/) override {
        sample_rate_ = sample_rate;

        for (auto& v : voices_) v.reset();

        if (model_dirty_) {
            load_model(pending_model_dir_);
            model_dirty_ = false;
        }

        // Compute frame advance: how fast to interpolate between inference frames
        if (model_frame_rate_ > 0)
            frame_advance_per_sample_ = static_cast<float>(model_frame_rate_) / sample_rate_;
        else
            frame_advance_per_sample_ = 100.0f / sample_rate_;  // default 100fps

        start_helper_thread();
    }

    void deactivate() override {
        stop_helper_thread();
        for (auto& v : voices_) v.reset();
    }

    // ------------------------------------------------------------------
    // MIDI events (audio thread)
    // ------------------------------------------------------------------

    void note_on(int channel, int pitch, int velocity) override {
        if (velocity == 0) { note_off(channel, pitch); return; }

        // Steal oldest voice if all active
        int slot = -1;
        for (int i = 0; i < MAX_VOICES; ++i) {
            if (!voices_[i].active.load(std::memory_order_relaxed)) {
                slot = i;
                break;
            }
        }
        if (slot < 0) slot = 0;  // steal voice 0

        auto& v = voices_[slot];
        v.reset();
        v.channel  = channel;
        v.pitch    = pitch;
        v.velocity = velocity;
        v.vel_gain = velocity / 127.0f;
        v.f0_hz    = 440.0f * std::pow(2.0f, (pitch - 69) / 12.0f);
        v.frame_advance = frame_advance_per_sample_;

        // Set up initial hardcoded frame (used before ONNX frames arrive)
        make_default_frame(v.cur_frame, v.f0_hz, num_harmonics_, num_noise_bands_);
        v.last_frame = v.cur_frame;
        v.frame_phase = 0.0f;

        // Envelope
        v.env.trigger(sample_rate_, attack_time_, 0.05f, 0.8f, release_time_);

        v.active.store(true, std::memory_order_release);

        // Wake helper thread
        wake_helper();
    }

    void note_off(int channel, int pitch) override {
        for (auto& v : voices_) {
            if (v.active.load(std::memory_order_relaxed) &&
                v.channel == channel && v.pitch == pitch &&
                !v.releasing.load(std::memory_order_relaxed)) {
                v.releasing.store(true, std::memory_order_release);
                v.env.release();
                break;
            }
        }
    }

    void all_notes_off(int /*channel*/) override {
        for (auto& v : voices_) {
            if (v.active.load(std::memory_order_relaxed)) {
                v.releasing.store(true, std::memory_order_release);
                v.env.release();
            }
        }
    }

    void note_tune(int /*channel*/, int note, float semitones) override {
        for (auto& v : voices_) {
            if (v.active.load(std::memory_order_relaxed) && v.pitch == note) {
                v.tune_semitones = semitones;
                v.f0_hz = 440.0f * std::pow(2.0f, (v.pitch + semitones - 69) / 12.0f);
            }
        }
    }

    // ------------------------------------------------------------------
    // Process (audio thread)
    // ------------------------------------------------------------------

    void process(const PluginProcessContext& ctx, PluginBuffers& buffers) override {
        auto* out = buffers.audio.get("audio_out");
        if (!out) return;

        float* L = out->left;
        float* R = out->right ? out->right : out->left;
        const int N = ctx.block_size;

        std::memset(L, 0, N * sizeof(float));
        if (out->right) std::memset(R, 0, N * sizeof(float));

        // Read control ports
        auto ctrl = [&](const char* id, float fallback) -> float {
            auto* p = buffers.control.get(id);
            return p ? p->value : fallback;
        };
        float gain       = ctrl("gain", 1.0f);
        float expression = ctrl("expression", 1.0f);
        float brightness = ctrl("brightness", 0.5f);
        attack_time_     = ctrl("attack", 0.01f);
        release_time_    = ctrl("release", 0.1f);

        for (auto& v : voices_) {
            if (!v.active.load(std::memory_order_relaxed)) continue;

            // Consume new frames from ring buffer
            DDSPFrame new_frame;
            while (v.ring.pop(new_frame)) {
                v.last_frame = v.cur_frame;
                v.cur_frame  = new_frame;
                v.frame_phase = 0.0f;
            }

            // Determine effective f0 (may have been updated by note_tune)
            float eff_f0 = v.f0_hz;
            int n_harm = v.cur_frame.valid ? v.cur_frame.num_harmonics :
                         std::min(num_harmonics_, MAX_HARMONICS);
            int n_noise = v.cur_frame.valid ? v.cur_frame.num_noise_bands :
                          std::min(num_noise_bands_, MAX_NOISE_BANDS);

            // --- Additive synthesis ---
            for (int i = 0; i < N; ++i) {
                float t = std::min(v.frame_phase, 1.0f);
                float env_val = v.env.next();

                if (v.env.is_off()) {
                    v.active.store(false, std::memory_order_relaxed);
                    break;
                }

                // Interpolate amplitude
                float amp = v.last_frame.amplitude * (1.0f - t) +
                            v.cur_frame.amplitude * t;

                // Accumulate harmonics
                float sample = 0.0f;
                float phase_inc_base = eff_f0 / sample_rate_;

                for (int h = 0; h < n_harm; ++h) {
                    // Interpolate per-harmonic amplitude
                    float h_amp = v.last_frame.harmonic_amps[h] * (1.0f - t) +
                                  v.cur_frame.harmonic_amps[h] * t;

                    // Apply brightness: attenuate higher harmonics when brightness < 0.5,
                    // boost when > 0.5
                    float h_frac = static_cast<float>(h) / std::max(1, n_harm - 1);
                    float bright_scale = std::pow(h_frac + 0.01f, -(brightness - 0.5f) * 2.0f);
                    h_amp *= bright_scale;

                    float phase = v.harmonic_phase[h];
                    sample += h_amp * fast_sin(phase);

                    // Advance phase for harmonic (h+1) * f0
                    phase += phase_inc_base * (h + 1);
                    phase -= std::floor(phase);
                    v.harmonic_phase[h] = phase;
                }

                sample *= amp * env_val * expression * v.vel_gain * gain;
                L[i] += sample;
                R[i] += sample;

                v.frame_phase += v.frame_advance;
            }

            // --- Filtered noise (per block) ---
            if (v.active.load(std::memory_order_relaxed) && n_noise > 0) {
                render_noise_block(v, N, n_noise, brightness, expression, gain);
                for (int i = 0; i < N; ++i) {
                    float env_approx = v.env.level;  // use current level for noise
                    float ns = v.noise_out_buf[i] * env_approx * v.vel_gain;
                    L[i] += ns;
                    R[i] += ns;
                }
            }
        }

        // Soft clip output
        for (int i = 0; i < N; ++i) {
            auto sc = [](float x) { return (x > 0.95f || x < -0.95f) ? std::tanh(x) : x; };
            L[i] = sc(L[i]);
            if (out->right) R[i] = sc(R[i]);
        }
    }

private:
    // ------------------------------------------------------------------
    // Noise synthesis (audio thread, per block)
    // ------------------------------------------------------------------

    void render_noise_block(DDSPVoice& v, int block_size, int n_noise,
                            float brightness, float expression, float gain) {
        // Generate noise and filter via FFT + magnitude shaping + IFFT
        // with 50% overlap-add for smooth transitions

        int remaining = block_size;
        int out_pos = 0;

        while (remaining > 0) {
            if (v.noise_ola_pos >= NOISE_HOP_SIZE || !v.noise_buf_ready) {
                // Need to synthesise a new noise frame
                synthesise_noise_frame(v, n_noise, brightness, expression, gain);
                v.noise_ola_pos = 0;
                v.noise_buf_ready = true;
            }

            int avail = NOISE_HOP_SIZE - v.noise_ola_pos;
            int to_copy = std::min(remaining, avail);

            // Read from OLA buffer at current position
            std::memcpy(v.noise_out_buf + out_pos,
                        v.noise_ola_buf + v.noise_ola_pos,
                        to_copy * sizeof(float));

            v.noise_ola_pos += to_copy;
            out_pos += to_copy;
            remaining -= to_copy;
        }
    }

    void synthesise_noise_frame(DDSPVoice& v, int n_noise,
                                float brightness, float expression, float gain) {
        float* scratch = v.noise_fft_scratch;

        // Generate windowed white noise in complex format
        for (int i = 0; i < NOISE_FFT_SIZE; ++i) {
            float noise = rng_.next() * g_hann_window[i];
            scratch[2 * i]     = noise;
            scratch[2 * i + 1] = 0.0f;
        }

        // Forward FFT
        fft_complex_inplace(scratch, NOISE_FFT_SIZE, false);

        // Apply noise magnitude filter
        // Map n_noise bands to NOISE_FFT_SIZE/2+1 bins
        int n_bins = NOISE_FFT_SIZE / 2 + 1;
        float t = std::min(v.frame_phase, 1.0f);

        for (int k = 0; k < n_bins; ++k) {
            // Map bin to noise band index
            int band = (n_noise > 0) ? (k * n_noise / n_bins) : 0;
            if (band >= n_noise) band = n_noise - 1;

            float mag = v.last_frame.noise_mags[band] * (1.0f - t) +
                        v.cur_frame.noise_mags[band] * t;

            // Apply brightness: boost/attenuate high-frequency noise
            float k_frac = static_cast<float>(k) / std::max(1, n_bins - 1);
            float bright_scale = std::pow(k_frac + 0.01f, -(brightness - 0.5f) * 2.0f);
            mag *= bright_scale * expression * gain;

            scratch[2 * k]     *= mag;
            scratch[2 * k + 1] *= mag;
        }

        // Restore Hermitian symmetry
        for (int k = 1; k < NOISE_FFT_SIZE / 2; ++k) {
            scratch[2 * (NOISE_FFT_SIZE - k)]     =  scratch[2 * k];
            scratch[2 * (NOISE_FFT_SIZE - k) + 1] = -scratch[2 * k + 1];
        }

        // Inverse FFT
        fft_complex_inplace(scratch, NOISE_FFT_SIZE, true);

        // Overlap-add into OLA buffer
        // Shift old buffer left by NOISE_HOP_SIZE, add new frame
        std::memmove(v.noise_ola_buf, v.noise_ola_buf + NOISE_HOP_SIZE,
                     NOISE_HOP_SIZE * sizeof(float));
        // Zero the second half (will be filled by current frame)
        std::memset(v.noise_ola_buf + NOISE_HOP_SIZE, 0, NOISE_HOP_SIZE * sizeof(float));

        // Add windowed IFFT output
        for (int i = 0; i < NOISE_FFT_SIZE; ++i) {
            int ola_idx = i - NOISE_HOP_SIZE;  // map into OLA buffer
            if (ola_idx >= 0 && ola_idx < NOISE_FFT_SIZE) {
                v.noise_ola_buf[ola_idx] += scratch[2 * i] * g_hann_window[i];
            }
        }
    }

    // ------------------------------------------------------------------
    // Default frame (hardcoded harmonic series for use before ONNX loads)
    // ------------------------------------------------------------------

    static void make_default_frame(DDSPFrame& f, float f0, int n_harm, int n_noise) {
        f.f0 = f0;
        f.amplitude = 0.3f;
        f.num_harmonics = std::min(n_harm, MAX_HARMONICS);
        f.num_noise_bands = std::min(n_noise, MAX_NOISE_BANDS);
        f.valid = true;

        // Decaying harmonic series: amplitude ∝ 1/(h+1)
        for (int h = 0; h < f.num_harmonics; ++h)
            f.harmonic_amps[h] = 1.0f / (h + 1);

        // Normalize harmonic amplitudes
        float sum = 0.0f;
        for (int h = 0; h < f.num_harmonics; ++h) sum += f.harmonic_amps[h];
        if (sum > 0.0f) {
            for (int h = 0; h < f.num_harmonics; ++h)
                f.harmonic_amps[h] /= sum;
        }

        // Gentle noise floor
        for (int b = 0; b < f.num_noise_bands; ++b)
            f.noise_mags[b] = 0.01f;
    }

    // ------------------------------------------------------------------
    // Model loading (main thread)
    // ------------------------------------------------------------------

    void load_model(const std::string& dir) {
        if (dir.empty()) {
            DDSP_LOG("No model directory specified");
            model_loaded_ = false;
            return;
        }

        // Parse config.json
        std::string config_path = dir + "/config.json";
        std::ifstream cfg_file(config_path);
        if (!cfg_file.is_open()) {
            DDSP_LOG("Cannot open %s", config_path.c_str());
            model_loaded_ = false;
            return;
        }

        try {
            nlohmann::json cfg;
            cfg_file >> cfg;

            if (cfg.contains("num_harmonics"))
                num_harmonics_ = cfg["num_harmonics"].get<int>();
            if (cfg.contains("num_noise_bands"))
                num_noise_bands_ = cfg["num_noise_bands"].get<int>();
            if (cfg.contains("frame_rate"))
                model_frame_rate_ = cfg["frame_rate"].get<int>();
            if (cfg.contains("sample_rate"))
                model_sample_rate_ = cfg["sample_rate"].get<int>();
            if (cfg.contains("z_dim"))
                z_dim_ = cfg["z_dim"].get<int>();

            num_harmonics_   = std::min(num_harmonics_, MAX_HARMONICS);
            num_noise_bands_ = std::min(num_noise_bands_, MAX_NOISE_BANDS);

            DDSP_LOG("Config: harmonics=%d noise_bands=%d frame_rate=%d sr=%d z_dim=%d",
                     num_harmonics_, num_noise_bands_, model_frame_rate_,
                     model_sample_rate_, z_dim_);
        } catch (const std::exception& e) {
            DDSP_LOG("Error parsing config.json: %s", e.what());
            model_loaded_ = false;
            return;
        }

#ifdef AS_ENABLE_DDSP
        // Load ONNX model
        std::string onnx_path = dir + "/decoder.onnx";
        try {
            if (!ort_env_)
                ort_env_ = std::make_unique<Ort::Env>(ORT_LOGGING_LEVEL_WARNING, "ddsp");

            Ort::SessionOptions opts;
            opts.SetIntraOpNumThreads(2);
            opts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);

            ort_session_ = std::make_unique<Ort::Session>(*ort_env_, onnx_path.c_str(), opts);
            model_loaded_ = true;
            DDSP_LOG("Loaded decoder: %s", onnx_path.c_str());
        } catch (const Ort::Exception& e) {
            DDSP_LOG("ONNX error loading %s: %s", onnx_path.c_str(), e.what());
            model_loaded_ = false;
        }
#else
        DDSP_LOG("ONNX Runtime not available — using hardcoded frames");
        model_loaded_ = false;
#endif

        // Update frame advance rate
        if (sample_rate_ > 0 && model_frame_rate_ > 0)
            frame_advance_per_sample_ = static_cast<float>(model_frame_rate_) / sample_rate_;
    }

    // ------------------------------------------------------------------
    // Helper thread — runs ONNX inference at frame rate
    // ------------------------------------------------------------------

    void start_helper_thread() {
        if (helper_running_.load()) return;
        helper_stop_.store(false);
        helper_running_.store(true);
        helper_thread_ = std::thread(&DDSPPlugin::helper_loop, this);
    }

    void stop_helper_thread() {
        if (!helper_running_.load()) return;
        helper_stop_.store(true);
        wake_helper();
        if (helper_thread_.joinable())
            helper_thread_.join();
        helper_running_.store(false);
    }

    void wake_helper() {
        std::lock_guard<std::mutex> lk(helper_mtx_);
        helper_wake_ = true;
        helper_cv_.notify_one();
    }

    void helper_loop() {
        DDSP_LOG("Helper thread started");
        while (!helper_stop_.load(std::memory_order_relaxed)) {
            {
                std::unique_lock<std::mutex> lk(helper_mtx_);
                helper_cv_.wait_for(lk, std::chrono::milliseconds(10),
                                    [this] { return helper_wake_ || helper_stop_.load(); });
                helper_wake_ = false;
            }
            if (helper_stop_.load()) break;

            // Run inference for each active voice
            for (auto& v : voices_) {
                if (!v.active.load(std::memory_order_acquire)) continue;

                DDSPFrame frame;
                if (model_loaded_) {
                    if (!run_inference(v, frame)) {
                        // Inference failed — use default frame
                        make_default_frame(frame, v.f0_hz, num_harmonics_, num_noise_bands_);
                    }
                } else {
                    // No model — generate default harmonic frame
                    make_default_frame(frame, v.f0_hz, num_harmonics_, num_noise_bands_);
                }

                v.ring.push(frame);
            }
        }
        DDSP_LOG("Helper thread stopped");
    }

    bool run_inference([[maybe_unused]] DDSPVoice& v,
                       [[maybe_unused]] DDSPFrame& frame) {
#ifdef AS_ENABLE_DDSP
        if (!ort_session_) return false;

        try {
            // Prepare inputs: f0 [1,1], loudness [1,1], optionally z [1,Z_DIM]
            float f0_val = v.f0_hz;
            float loudness_val = v.velocity / 127.0f;

            std::array<int64_t, 2> shape_1x1 = {1, 1};
            auto f0_tensor = Ort::Value::CreateTensor<float>(
                mem_info_, &f0_val, 1, shape_1x1.data(), 2);
            auto loud_tensor = Ort::Value::CreateTensor<float>(
                mem_info_, &loudness_val, 1, shape_1x1.data(), 2);

            std::vector<const char*> in_names;
            std::vector<Ort::Value> in_vals;

            in_names.push_back("f0");
            in_vals.push_back(std::move(f0_tensor));
            in_names.push_back("loudness");
            in_vals.push_back(std::move(loud_tensor));

            // Optional z input (latent code)
            std::vector<float> z_data;
            if (z_dim_ > 0) {
                z_data.resize(z_dim_, 0.0f);
                std::array<int64_t, 2> z_shape = {1, static_cast<int64_t>(z_dim_)};
                auto z_tensor = Ort::Value::CreateTensor<float>(
                    mem_info_, z_data.data(), z_data.size(), z_shape.data(), 2);
                in_names.push_back("z");
                in_vals.push_back(std::move(z_tensor));
            }

            // Output names
            std::vector<const char*> out_names = {
                "harmonic_amplitudes", "noise_magnitudes", "amplitude"
            };

            auto outs = ort_session_->Run(
                Ort::RunOptions{nullptr},
                in_names.data(), in_vals.data(), in_vals.size(),
                out_names.data(), out_names.size());

            if (outs.size() < 3) return false;

            // Extract outputs
            auto harm_info = outs[0].GetTensorTypeAndShapeInfo();
            auto noise_info = outs[1].GetTensorTypeAndShapeInfo();

            const float* harm_data = outs[0].GetTensorData<float>();
            const float* noise_data = outs[1].GetTensorData<float>();
            const float* amp_data = outs[2].GetTensorData<float>();

            int n_h = static_cast<int>(harm_info.GetElementCount());
            int n_n = static_cast<int>(noise_info.GetElementCount());

            frame.f0 = f0_val;
            frame.amplitude = amp_data[0];
            frame.num_harmonics = std::min(n_h, MAX_HARMONICS);
            frame.num_noise_bands = std::min(n_n, MAX_NOISE_BANDS);

            for (int i = 0; i < frame.num_harmonics; ++i)
                frame.harmonic_amps[i] = harm_data[i];
            for (int i = 0; i < frame.num_noise_bands; ++i)
                frame.noise_mags[i] = noise_data[i];

            frame.valid = true;
            return true;

        } catch (const Ort::Exception& e) {
            DDSP_LOG("Inference error: %s", e.what());
            return false;
        }
#else
        return false;
#endif
    }

    // ------------------------------------------------------------------
    // Member data
    // ------------------------------------------------------------------

    float sample_rate_ = 44100.0f;

    // Model config
    std::string pending_model_dir_;
    bool model_dirty_  = false;
    bool model_loaded_ = false;
    int  num_harmonics_     = 60;
    int  num_noise_bands_   = 65;
    int  model_frame_rate_  = 100;
    int  model_sample_rate_ = 16000;
    int  z_dim_             = 0;
    float frame_advance_per_sample_ = 100.0f / 44100.0f;

    // ADSR params (updated from control ports each block)
    float attack_time_  = 0.01f;
    float release_time_ = 0.1f;

    // Voices
    DDSPVoice voices_[MAX_VOICES];

    // Noise RNG
    XorShift32 rng_;

    // Helper thread
    std::thread helper_thread_;
    std::atomic<bool> helper_running_{false};
    std::atomic<bool> helper_stop_{false};
    std::mutex helper_mtx_;
    std::condition_variable helper_cv_;
    bool helper_wake_ = false;

    // ONNX Runtime
#ifdef AS_ENABLE_DDSP
    std::unique_ptr<Ort::Env>     ort_env_;
    std::unique_ptr<Ort::Session> ort_session_;
    Ort::MemoryInfo mem_info_ = Ort::MemoryInfo::CreateCpu(
        OrtArenaAllocator, OrtMemTypeDefault);
#endif
};

// ==========================================================================
// Registration
// ==========================================================================

REGISTER_PLUGIN(DDSPPlugin);
REGISTER_PLUGIN_DYNAMIC(DDSPPlugin);
