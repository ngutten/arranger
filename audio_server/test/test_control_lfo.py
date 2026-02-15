#!/usr/bin/env python3
"""
test_control_lfo.py — Smoke-test for builtin.control_lfo via the control monitor.

If this passes but debug_note_gate.py fails:
  → control port routing is fine; the bug is specifically in note_gate
     (event delivery or note_on() not being called on the plugin).

If this also fails:
  → control port routing is broken at the graph level (assign_buffers /
     pool writeback in graph.cpp), not specific to note_gate.

Usage (server must already be running):
    python3 test/test_control_lfo.py
"""

import json, socket, struct, sys, time

DEFAULT_ADDRESS = "/tmp/audio_server.sock"

class Client:
    def __init__(self, address=DEFAULT_ADDRESS):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(address)

    def send(self, req):
        payload = json.dumps(req).encode()
        self._sock.sendall(struct.pack("<I", len(payload)) + payload)
        n = struct.unpack("<I", self._recv_exact(4))[0]
        return json.loads(self._recv_exact(n))

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk: raise EOFError("disconnected")
            buf += chunk
        return buf


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

def check(label, cond, detail=""):
    tag = PASS if cond else FAIL
    print(f"  [{tag}] {label}" + (f"  ({detail})" if detail else ""))
    return cond

def get_history(client, node_id):
    r = client.send({"cmd": "get_node_data", "node_id": node_id, "port_id": "history"})
    if r.get("status") != "ok":
        return None
    return json.loads(r.get("data", "[]"))


# ---------------------------------------------------------------------------
# Graph: lfo → ctrl_mon (+ empty mixer for valid audio output)
# ---------------------------------------------------------------------------
def make_lfo_graph(shape=0, freq=5.0, sync=0):
    return {
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": "lfo",     "type": "builtin.control_lfo",
             "params": {"shape": shape, "frequency": freq,
                        "amplitude": 0.5, "offset": 0.5, "sync": sync}},
            {"id": "ctrl_mon","type": "builtin.control_monitor"},
            {"id": "mixer",   "type": "mixer", "channel_count": 0},
        ],
        "connections": [
            {"from_node": "lfo",  "from_port": "control_out",
             "to_node": "ctrl_mon", "to_port": "control_in"},
        ],
    }


def main():
    print(f"Connecting to {DEFAULT_ADDRESS!r} ...")
    c = Client(DEFAULT_ADDRESS)
    print("Connected.\n")

    print("=" * 60)
    print("  LFO → ControlMonitor smoke test")
    print("=" * 60)

    shape_names = ["Sine", "Square", "Triangle", "Sawtooth"]

    for shape_idx, name in enumerate(shape_names):
        print(f"\n  Shape {shape_idx}: {name}")
        r = c.send(make_lfo_graph(shape=shape_idx, freq=4.0))
        if not check("graph load", r.get("status") == "ok", r.get("message", "")):
            continue

        # Use offline render — deterministic and doesn't need timing guesses
        r = c.send({
            "cmd": "set_schedule",
            "events": [],   # LFO needs no events
        })
        r = c.send({"cmd": "render", "format": "raw_f32", "duration_beats": 8.0})
        if not check("render", r.get("status") == "ok", r.get("message", "")):
            continue

        hist = get_history(c, "ctrl_mon")
        if hist is None or len(hist) == 0:
            check("history non-empty", False, "got None or []")
            continue

        mn, mx = min(hist), max(hist)
        nonzero = sum(1 for v in hist if abs(v - 0.5) > 0.01)

        check("history non-empty",        len(hist) > 0,  f"{len(hist)} samples")
        check("output varies (not stuck)", nonzero > 0,    f"{nonzero}/{len(hist)} non-0.5 samples")
        check("output within [0,1]",       mn >= -0.001 and mx <= 1.001,
              f"min={mn:.4f} max={mx:.4f}")

        if shape_idx == 1:  # Square: should be near 0 or 1
            near_extremes = sum(1 for v in hist if v < 0.1 or v > 0.9)
            check("square: near 0 or 1",  near_extremes > 0,
                  f"{near_extremes}/{len(hist)} near extremes")

    # Beat-sync mode sanity: two different beat positions should give different values
    print("\n  Beat-sync mode")
    r = c.send(make_lfo_graph(shape=0, sync=1))
    check("sync graph load", r.get("status") == "ok")
    r = c.send({"cmd": "render", "format": "raw_f32", "duration_beats": 8.0})
    check("sync render", r.get("status") == "ok")
    hist = get_history(c, "ctrl_mon")
    if hist:
        spread = max(hist) - min(hist)
        check("beat-sync shows variation", spread > 0.01, f"spread={spread:.4f}")

    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)
    print("""
If all checks pass but debug_note_gate.py still fails:
  → Control routing works. The bug is in note_gate specifically:
     either note_on() isn't being called on the plugin instance,
     or the pitch range check is silently filtering the note.

If checks fail here too:
  → graph.cpp control port writeback (pool[buf_idx][0]) is broken.
     Check assign_buffers() / process() in graph.cpp.
""")


if __name__ == "__main__":
    main()
