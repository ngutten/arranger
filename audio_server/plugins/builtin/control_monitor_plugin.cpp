// control_monitor_plugin.cpp
// Monitors an incoming Control stream and makes recent samples available to
// the UI via the Monitor readback path (read_monitor).
//
// The plugin keeps a circular buffer of HISTORY_SIZE control values.
// read_monitor("latest")  → most recent value
// read_monitor("min")     → rolling minimum over the buffer
// read_monitor("max")     → rolling maximum over the buffer
// read_monitor("mean")    → rolling mean over the buffer
//
// The UI polls these at display rate (e.g. 30 Hz) to render a sparkline.
// The full sample history is available via get_graph_data("history") as a
// JSON array of floats, which the Python side reads for the sparkline plot.

#include "plugin_api.h"
#include <atomic>
#include <array>
#include <algorithm>
#include <numeric>
#include <cstring>
#include <string>

// How many control values to keep in the circular history buffer.
// At 44100 Hz with a typical block_size of 128, this gives ~0.37 s of
// history at 8 blocks/second control rate.  Adjust as needed.
static constexpr int HISTORY_SIZE = 512;

class ControlMonitorPlugin final : public Plugin {
public:
    PluginDescriptor descriptor() const override {
        PluginDescriptor d;
        d.id           = "builtin.control_monitor";
        d.display_name = "Control Monitor";
        d.category     = "Utility";
        d.doc          = "Monitors a Control stream and displays a live scrolling plot in the UI.";
        d.author       = "builtin";
        d.version      = 1;

        d.ports = {
            { "control_in", "Control In", "Control stream to monitor",
              PluginPortType::Control, PortRole::Input,
              ControlHint::Continuous, 0.0f, 0.0f, 1.0f },

            // Monitor outputs for the UI
            { "latest", "Latest", "Most recent value",
              PluginPortType::Control, PortRole::Monitor,
              ControlHint::Meter, 0.0f, 0.0f, 1.0f },
            { "min", "Min", "Rolling minimum",
              PluginPortType::Control, PortRole::Monitor,
              ControlHint::Meter, 0.0f, 0.0f, 1.0f },
            { "max", "Max", "Rolling maximum",
              PluginPortType::Control, PortRole::Monitor,
              ControlHint::Meter, 0.0f, 0.0f, 1.0f },
        };

        return d;
    }

    void activate(float /*sample_rate*/, int /*max_block_size*/) override {
        _head.store(0);
        _count.store(0);
        std::fill(_buf.begin(), _buf.end(), 0.0f);
        _latest.store(0.0f);
        _min.store(0.0f);
        _max.store(0.0f);
    }

    void process(const PluginProcessContext& /*ctx*/, PluginBuffers& buffers) override {
        auto* in = buffers.control.get("control_in");
        float v = in ? in->value : 0.0f;

        // Write into circular buffer (audio thread — no allocation/lock)
        int h = _head.load(std::memory_order_relaxed);
        _buf[h] = v;
        _head.store((h + 1) % HISTORY_SIZE, std::memory_order_relaxed);

        int cnt = _count.load(std::memory_order_relaxed);
        if (cnt < HISTORY_SIZE)
            _count.store(cnt + 1, std::memory_order_relaxed);

        _latest.store(v, std::memory_order_relaxed);
    }

    float read_monitor(const std::string& port_id) override {
        // Compute stats lazily on the main thread (fine — this is non-realtime).
        if (port_id == "latest") return _latest.load();

        int cnt  = _count.load(std::memory_order_acquire);
        int head = _head.load(std::memory_order_acquire);
        if (cnt == 0) return 0.0f;

        // Snapshot the relevant portion of the ring buffer
        float mn = _buf[0], mx = _buf[0];
        for (int i = 0; i < cnt; ++i)
            mn = std::min(mn, _buf[i]), mx = std::max(mx, _buf[i]);

        _min.store(mn);
        _max.store(mx);

        if (port_id == "min")  return mn;
        if (port_id == "max")  return mx;
        return _latest.load();
    }

    std::string get_graph_data(const std::string& port_id) override {
        if (port_id != "history") return "[]";

        int cnt  = _count.load(std::memory_order_acquire);
        int head = _head.load(std::memory_order_acquire);
        if (cnt == 0) return "[]";

        // Build chronological JSON array from the ring buffer
        std::string json = "[";
        int start = (cnt < HISTORY_SIZE) ? 0 : head;
        for (int i = 0; i < cnt; ++i) {
            int idx = (start + i) % HISTORY_SIZE;
            if (i > 0) json += ',';
            float v = _buf[idx];
            // Simple float → string without printf (no I/O in hot path, but
            // this is the main thread so it's fine to use sprintf here)
            char tmp[32];
            std::snprintf(tmp, sizeof(tmp), "%.6g", (double)v);
            json += tmp;
        }
        json += ']';
        return json;
    }

private:
    std::array<float, HISTORY_SIZE> _buf{};
    std::atomic<int>   _head{0};
    std::atomic<int>   _count{0};
    std::atomic<float> _latest{0.0f};
    std::atomic<float> _min{0.0f};
    std::atomic<float> _max{0.0f};
};

REGISTER_PLUGIN(ControlMonitorPlugin);
REGISTER_PLUGIN_DYNAMIC(ControlMonitorPlugin);

std::unique_ptr<Plugin> make_control_monitor_plugin() {
    return std::make_unique<ControlMonitorPlugin>();
}
