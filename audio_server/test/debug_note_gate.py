#!/usr/bin/env python3
"""
debug_note_gate.py — Systematic probe of the NoteGate → ControlMonitor chain.

Connects directly to the audio server (no Qt, no app state) and steps through
the signal chain, printing what actually happens at each stage.

Usage:
    # Server must already be running
    python3 test/debug_note_gate.py

    # Or with a custom socket path
    python3 test/debug_note_gate.py --address /tmp/audio_server.sock

Checkpoints tested:
  1. Server alive (ping)
  2. Graph with NoteGate + ControlMonitor loads without error
  3. Control Monitor history is empty before any notes
  4. note_on injected directly into track_source node
  5. Control Monitor history after note_on — should be non-zero
  6. note_off injected
  7. Control Monitor history after note_off — should return to 0
  8. Same via the scheduler (offline render path)
  9. Direct note_on to the NoteGate node itself (bypassing track_source)
 10. Mode=1 (Velocity) — check proportional output
"""

import argparse
import base64
import json
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# Minimal IPC client (mirrors server_engine._IpcClient)
# ---------------------------------------------------------------------------

DEFAULT_ADDRESS = "/tmp/audio_server.sock"


class Client:
    def __init__(self, address=DEFAULT_ADDRESS):
        self.address = address
        self._sock = None

    def connect(self, timeout=5.0):
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.address)
                self._sock = s
                return
            except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
                last_err = e
                time.sleep(0.05)
        raise ConnectionError(f"Cannot connect to {self.address!r}: {last_err}")

    def send(self, req: dict) -> dict:
        payload = json.dumps(req).encode()
        self._sock.sendall(struct.pack("<I", len(payload)) + payload)
        resp_len = struct.unpack("<I", self._recv(4))[0]
        return json.loads(self._recv(resp_len))

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("Server disconnected")
            buf += chunk
        return buf

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS  = "\033[32mPASS\033[0m"
FAIL  = "\033[31mFAIL\033[0m"
INFO  = "\033[36mINFO\033[0m"
WARN  = "\033[33mWARN\033[0m"


def check(label, cond, detail=""):
    status = PASS if cond else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    return cond


def info(msg):
    print(f"  [{INFO}] {msg}")


def warn(msg):
    print(f"  [{WARN}] {msg}")


def header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def get_history(client, node_id):
    """Read the ControlMonitor's history buffer via get_node_data."""
    resp = client.send({"cmd": "get_node_data", "node_id": node_id, "port_id": "history"})
    if resp.get("status") != "ok":
        return None, resp.get("message", "?")
    try:
        history = json.loads(resp.get("data", "[]"))
        return history, None
    except Exception as e:
        return None, str(e)


def monitor_stats(history):
    """Return (min, max, mean, n_nonzero) for a history list."""
    if not history:
        return None, None, None, 0
    mn = min(history)
    mx = max(history)
    mean = sum(history) / len(history)
    nonzero = sum(1 for v in history if abs(v) > 1e-6)
    return mn, mx, mean, nonzero


def wait_for_blocks(n_blocks=10, block_size=512, sample_rate=44100):
    """Sleep long enough for n_blocks to have been processed."""
    secs = (n_blocks * block_size) / sample_rate
    time.sleep(max(secs, 0.05))


# ---------------------------------------------------------------------------
# Graph definitions
# ---------------------------------------------------------------------------

def make_graph_note_gate_to_monitor():
    """
    track_source  → (MIDI fan-out) → NoteGate plugin → ControlMonitor plugin
                                                              ↓
                                                         (Monitor output)
    Also has a sine synth → mixer so the graph has a valid audio output.
    """
    return {
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": "track_1",    "type": "track_source"},
            {"id": "note_gate",  "type": "builtin.note_gate"},
            {"id": "ctrl_mon",   "type": "builtin.control_monitor"},
            {"id": "synth",      "type": "sine"},
            {"id": "mixer",      "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            # MIDI fan-out: track_source → note_gate (event input)
            {"from_node": "track_1",   "from_port": "events_out",
             "to_node":   "note_gate", "to_port":   "event_in"},
            # MIDI fan-out: track_source → sine synth (so audio path works)
            {"from_node": "track_1",   "from_port": "events_out",
             "to_node":   "synth",     "to_port":   "events_in"},
            # Control: note_gate output → control_monitor input
            {"from_node": "note_gate", "from_port": "control_out",
             "to_node":   "ctrl_mon",  "to_port":   "control_in"},
            # Audio: sine → mixer
            {"from_node": "synth",     "from_port": "audio_out_L",
             "to_node":   "mixer",     "to_port":   "audio_in_L_0"},
            {"from_node": "synth",     "from_port": "audio_out_R",
             "to_node":   "mixer",     "to_port":   "audio_in_R_0"},
        ],
    }


def make_graph_direct_gate():
    """
    Same but note_on goes directly to note_gate node (no track_source),
    to test whether the issue is in the MIDI fan-out path or the control path.
    """
    return {
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": "note_gate", "type": "builtin.note_gate"},
            {"id": "ctrl_mon",  "type": "builtin.control_monitor"},
            # Need a mixer for a valid audio output
            {"id": "mixer",     "type": "mixer", "channel_count": 0},
        ],
        "connections": [
            {"from_node": "note_gate", "from_port": "control_out",
             "to_node":   "ctrl_mon",  "to_port":   "control_in"},
        ],
    }


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------

def test_ping(client):
    header("Checkpoint 1: Server alive")
    resp = client.send({"cmd": "ping"})
    ok = check("ping responds ok", resp.get("status") == "ok", str(resp))
    if not ok:
        print("Server not responding — abort.")
        sys.exit(1)
    info(f"features: {resp.get('features', [])}")
    # Check builtin.note_gate is registered
    resp2 = client.send({"cmd": "list_registered_plugins"})
    if resp2.get("status") == "ok":
        ids = [p["id"] for p in resp2.get("plugins", [])]
        check("builtin.note_gate registered", "builtin.note_gate" in ids, str(ids))
        check("builtin.control_monitor registered", "builtin.control_monitor" in ids)
    else:
        warn(f"list_registered_plugins failed: {resp2}")


def test_graph_load(client):
    header("Checkpoint 2: Graph load")
    graph = make_graph_note_gate_to_monitor()
    resp = client.send(graph)
    ok = check("set_graph succeeds", resp.get("status") == "ok", resp.get("message", ""))
    if not ok:
        print("Graph load failed — abort.")
        sys.exit(1)
    info("Graph: track_source → note_gate → ctrl_mon + sine → mixer")
    return graph


def test_baseline(client):
    header("Checkpoint 3: Baseline (no notes)")
    # Wait for a few blocks to process
    wait_for_blocks(20)
    history, err = get_history(client, "ctrl_mon")
    if history is None:
        check("get_node_data succeeds", False, err)
        return
    check("get_node_data succeeds", True)
    info(f"History length: {len(history)}")
    mn, mx, mean, nonzero = monitor_stats(history)
    if len(history) == 0:
        warn("History is empty — monitor may not be processing blocks yet")
        warn("This could mean the graph is not activating or not running")
    else:
        check("All values are 0 before any notes", nonzero == 0,
              f"min={mn:.4f} max={mx:.4f} mean={mean:.4f} nonzero={nonzero}/{len(history)}")


def test_note_on_via_track_source(client):
    header("Checkpoint 4–6: note_on via track_source fan-out")

    # Clear history by reloading graph
    resp = client.send(make_graph_note_gate_to_monitor())
    check("Graph reload ok", resp.get("status") == "ok")
    wait_for_blocks(5)

    # Inject note_on into track_source — this goes through the fan-out
    info("Injecting note_on(ch=0, pitch=60, vel=100) → track_1")
    resp = client.send({
        "cmd": "note_on", "node_id": "track_1",
        "channel": 0, "pitch": 60, "velocity": 100,
    })
    check("note_on command accepted", resp.get("status") == "ok", str(resp))

    # Wait for several blocks to accumulate
    wait_for_blocks(30)

    history, err = get_history(client, "ctrl_mon")
    if history is None:
        check("get_node_data after note_on", False, err)
        return

    mn, mx, mean, nonzero = monitor_stats(history)
    info(f"After note_on: {len(history)} samples, min={mn:.4f} max={mx:.4f} "
         f"mean={mean:.4f} nonzero={nonzero}")

    check("History has entries after note_on", len(history) > 0,
          f"got {len(history)} entries")
    check("Control output is non-zero (gate mode = 1.0)", mx > 0.5,
          f"max={mx:.4f} (expected ~1.0 in gate mode)")

    # note_off
    info("Injecting note_off(ch=0, pitch=60) → track_1")
    resp = client.send({
        "cmd": "note_off", "node_id": "track_1",
        "channel": 0, "pitch": 60,
    })
    check("note_off command accepted", resp.get("status") == "ok", str(resp))

    wait_for_blocks(30)

    history2, _ = get_history(client, "ctrl_mon")
    if history2:
        mn2, mx2, mean2, nonzero2 = monitor_stats(history2[-20:])
        info(f"After note_off (last 20 samples): min={mn2:.4f} max={mx2:.4f} nonzero={nonzero2}")
        check("Control output returns to 0 after note_off", mx2 < 0.01,
              f"last 20 samples max={mx2:.4f}")


def test_note_on_direct_to_gate(client):
    header("Checkpoint 9: note_on via scheduler targeting note_gate directly")
    info("Scheduler calls node->note_on() directly, bypassing track_source fan-out")
    info("(preview_note_on only works for TrackSourceNodes, so we use set_schedule here)")

    resp = client.send(make_graph_direct_gate())
    check("Direct graph load ok", resp.get("status") == "ok", resp.get("message", ""))

    resp = client.send({
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "note_gate",
             "channel": 0, "pitch": 60, "velocity": 127, "value": 0.0},
            {"beat": 4.0, "type": "note_off", "node_id": "note_gate",
             "channel": 0, "pitch": 60, "velocity": 0,   "value": 0.0},
        ],
    })
    check("set_schedule ok", resp.get("status") == "ok")

    resp = client.send({"cmd": "render", "format": "raw_f32"})
    check("render ok", resp.get("status") == "ok", resp.get("message", ""))
    if resp.get("status") != "ok":
        return

    history, err = get_history(client, "ctrl_mon")
    if history is None:
        check("get_node_data", False, err)
        return

    mn, mx, mean, nonzero = monitor_stats(history)
    info(f"Direct path: {len(history)} samples, min={mn:.4f} max={mx:.4f} nonzero={nonzero}")
    check("Non-zero output with direct scheduler path", mx > 0.5, f"max={mx:.4f}")

    print()
    info("Checkpoint 9 vs Checkpoint 8 comparison:")
    info("  Both use offline render; the difference is whether events go")
    info("  through track_source fan-out (8) or directly to note_gate (9).")
    info("  If 9 passes but 8 fails → set_downstream not including note_gate")
    info("    (check MIDI connection port names in graph JSON).")
    info("  If both fail → control signal isn't flowing from note_gate → ctrl_mon")


def test_velocity_mode(client):
    header("Checkpoint 10: Velocity mode (mode=1)")

    resp = client.send(make_graph_note_gate_to_monitor())
    check("Graph reload ok", resp.get("status") == "ok")

    # Set mode=1 (Velocity) via set_param
    resp = client.send({
        "cmd": "set_param", "node_id": "note_gate",
        "param_id": "mode", "value": 1.0,
    })
    check("set_param mode=1 accepted", resp.get("status") == "ok", str(resp))
    wait_for_blocks(5)

    for velocity, expected in [(64, 64/127), (127, 1.0), (32, 32/127)]:
        # Reset by all_notes_off
        client.send({"cmd": "all_notes_off", "node_id": "track_1"})
        wait_for_blocks(5)

        resp = client.send({
            "cmd": "note_on", "node_id": "track_1",
            "channel": 0, "pitch": 60, "velocity": velocity,
        })
        wait_for_blocks(20)

        history, _ = get_history(client, "ctrl_mon")
        recent = history[-10:] if history and len(history) >= 10 else history or []
        _, mx, _, _ = monitor_stats(recent)
        tol = 0.02
        ok = mx is not None and abs(mx - expected) < tol
        check(f"velocity={velocity} → output≈{expected:.3f}",
              ok, f"got max={mx:.4f} in last 10 samples")

        client.send({"cmd": "all_notes_off", "node_id": "track_1"})


def test_scheduler_path(client):
    header("Checkpoint 8: Offline render via scheduler")
    info("Uses set_schedule + render to check control output in a deterministic way")

    resp = client.send(make_graph_note_gate_to_monitor())
    check("Graph load ok", resp.get("status") == "ok")

    # Schedule: one note held for 2 beats at bpm=120 (= 1 second)
    resp = client.send({
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "track_1",
             "channel": 0, "pitch": 60, "velocity": 100, "value": 0.0},
            {"beat": 2.0, "type": "note_off", "node_id": "track_1",
             "channel": 0, "pitch": 60, "velocity": 0,   "value": 0.0},
        ],
    })
    check("set_schedule ok", resp.get("status") == "ok")

    resp = client.send({"cmd": "render", "format": "raw_f32"})
    check("render ok", resp.get("status") == "ok", resp.get("message", ""))
    if resp.get("status") != "ok":
        return

    # Decode the raw f32 PCM (stereo interleaved) — but this is audio, not control.
    # The control monitor's history is what we actually want to check after render.
    history, err = get_history(client, "ctrl_mon")
    if history is None:
        check("get history after render", False, err)
        return

    mn, mx, mean, nonzero = monitor_stats(history)
    info(f"Post-render history: {len(history)} samples, min={mn:.4f} max={mx:.4f} "
         f"mean={mean:.4f} nonzero={nonzero}")
    check("Control output was non-zero during render", nonzero > 0,
          f"{nonzero}/{len(history)} non-zero samples")
    check("Peak was ~1.0 (gate mode)", mx > 0.9, f"max={mx:.4f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="NoteGate signal chain diagnostics")
    parser.add_argument("--address", default=DEFAULT_ADDRESS,
                        help="Server socket path")
    args = parser.parse_args()

    print(f"\nConnecting to {args.address!r} ...")
    client = Client(args.address)
    try:
        client.connect()
    except ConnectionError as e:
        print(f"FATAL: {e}")
        print("Is the audio_server running?")
        sys.exit(1)
    print("Connected.\n")

    try:
        test_ping(client)
        test_graph_load(client)
        test_baseline(client)
        test_note_on_via_track_source(client)
        test_note_on_direct_to_gate(client)
        test_velocity_mode(client)
        test_scheduler_path(client)
    finally:
        client.close()

    print("\n" + "="*60)
    print("  Diagnostics complete.")
    print("="*60)
    print("""
Interpretation guide:
  Checkpoint 3 fails (history empty):
    → Graph may not be processing. Check server logs.

  Checkpoint 4-5 fails (no non-zero after note_on via track_source):
    → Either (a) TrackSourceNode not forwarding to NoteGate,
      or (b) NoteGate internal state updating but control not reaching monitor.

  Checkpoint 9 passes but 4-5 fails:
    → Bug is in set_downstream / MIDI fan-out. TrackSourceNode isn't
      including the NoteGate in its downstream list.
      Check connections: the MIDI wire must be to_port="event_in" not
      something the graph ignores.

  Checkpoint 9 also fails:
    → Control value isn't making it from NoteGate to ControlMonitor.
      Check graph.cpp control port routing (PortBuffer.control vs .audio).

  Checkpoint 8 (render) passes but real-time (4-5) fails:
    → Timing issue — note events arrive but graph hasn't processed them
      before we poll. Try increasing wait_for_blocks(n).
""")


if __name__ == "__main__":
    main()
