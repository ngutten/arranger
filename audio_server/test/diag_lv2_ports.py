#!/usr/bin/env python3
"""
diag_lv2_ports.py
Standalone diagnostic for LV2 plugin port layout and graph wiring.

Run this first when a plugin is producing no audio or crashing:

    python3 diag_lv2_ports.py --uri http://calf.sourceforge.net/plugins/Reverb

Output:
  - Full port listing from list_plugins (what the server's lilv sees)
  - A ready-to-paste graph JSON with auto-detected port connections
  - A render test that tells you whether audio is flowing

This script is intentionally separate from the main test suite so it can
be used interactively during debugging without needing the full test framework.
"""

import socket, struct, json, base64, wave, io, sys, argparse


# ---------------------------------------------------------------------------
# Minimal IPC client (same framing as ipc.cpp)
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, addr):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(addr)
        self.sock.settimeout(15.0)

    def _send(self, data):
        s = 0
        while s < len(data):
            n = self.sock.send(data[s:])
            if not n: raise RuntimeError("closed")
            s += n

    def _recv(self, n):
        b = bytearray()
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c: raise RuntimeError("closed")
            b.extend(c)
        return bytes(b)

    def send(self, msg):
        p = json.dumps(msg).encode()
        self._send(struct.pack("<I", len(p)) + p)
        l = struct.unpack("<I", self._recv(4))[0]
        return json.loads(self._recv(l))

    def close(self):
        self.sock.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wav_stats(b64):
    """Return (n_frames, n_channels, sample_rate, n_nonzero, max_abs) from base64 WAV."""
    raw = base64.b64decode(b64)
    with wave.open(io.BytesIO(raw)) as wf:
        frames = wf.readframes(wf.getnframes())
        import struct as st
        n_samples = len(frames) // 2
        samples = st.unpack(f"<{n_samples}h", frames)
        nonzero = sum(1 for s in samples if s != 0)
        max_abs = max(abs(s) for s in samples) if samples else 0
        return wf.getnframes(), wf.getnchannels(), wf.getframerate(), nonzero, max_abs


def separator(title=""):
    line = "─" * 60
    if title:
        print(f"\n{line}")
        print(f"  {title}")
        print(line)
    else:
        print(line)


# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------

def run(args):
    c = Client(args.address)

    separator("Server info")
    ping = c.send({"cmd": "ping"})
    print(f"  version : {ping.get('version')}")
    print(f"  features: {ping.get('features')}")

    if "lv2" not in ping.get("features", []):
        print("\n  !! Server was not built with AS_ENABLE_LV2. Exiting.")
        c.close()
        sys.exit(1)

    # -----------------------------------------------------------------------
    separator(f"Port listing for: {args.uri}")
    resp = c.send({"cmd": "list_plugins", "uri_prefix": args.uri})
    if resp.get("status") != "ok":
        print(f"  !! list_plugins failed: {resp}")
        c.close()
        sys.exit(1)

    plugins = [p for p in resp["plugins"] if p["uri"] == args.uri]
    if not plugins:
        all_uris = [p["uri"] for p in resp["plugins"]]
        print(f"  !! Plugin not found: {args.uri}")
        print(f"  Installed plugins matching prefix:")
        for u in all_uris:
            print(f"    {u}")
        c.close()
        sys.exit(1)

    plugin = plugins[0]
    print(f"  Name    : {plugin['name']}")
    print(f"  URI     : {plugin['uri']}")
    print(f"  Category: {plugin['category']}")
    print()

    audio_ins  = []
    audio_outs = []
    ctrl_ins   = []
    ctrl_outs  = []

    for p in plugin["ports"]:
        sym  = p["symbol"]
        typ  = p["type"]
        dirn = p["direction"]
        if typ == "audio" and dirn == "input":
            audio_ins.append(sym)
        elif typ == "audio" and dirn == "output":
            audio_outs.append(sym)
        elif typ == "control" and dirn == "input":
            ctrl_ins.append(sym)
        elif typ == "control" and dirn == "output":
            ctrl_outs.append(sym)
        else:
            print(f"  [OTHER  ] {dirn:6} '{sym}'  (atom/MIDI/CV — will get dummy buffer)")
            continue
        extras = ""
        if typ == "control" and dirn == "input":
            extras = (f"  default={p.get('default', '?'):.4g}"
                      f"  [{p.get('min','?'):.4g}, {p.get('max','?'):.4g}]")
        print(f"  [{typ:7}] {dirn:6} '{sym}'{extras}")

    print()
    print(f"  Audio inputs : {audio_ins}")
    print(f"  Audio outputs: {audio_outs}")
    print(f"  Control inputs ({len(ctrl_ins)} total): {ctrl_ins[:8]}{'...' if len(ctrl_ins)>8 else ''}")

    # -----------------------------------------------------------------------
    # Check declare_ports() consistency:
    # The graph system iterates LV2 ports in index order and includes all
    # audio and control ports (skipping unsupported types).  Audio inputs get
    # audio buffers; control inputs get control-rate values.
    # The graph wiring needs the exact port symbol strings.
    # -----------------------------------------------------------------------

    if not audio_ins or not audio_outs:
        print("\n  !! Plugin has no audio I/O — cannot build passthrough graph.")
        c.close()
        sys.exit(1)

    BYPASS_NAMES = {"bypass"}
    ENABLE_NAMES = {"on", "enable", "enabled", "active"}
    init_params = {}
    silent_risk_params = []
    for p in plugin["ports"]:
        if p["type"] != "control" or p["direction"] != "input":
            continue
        sym  = p["symbol"]
        dval = p.get("default", 0.0) or 0.0
        pmin = p.get("min")
        pmax = p.get("max")
        if pmin is None or pmax is None:
            continue

        # Bypass/enable toggles
        if sym in BYPASS_NAMES and pmin == 0 and pmax == 1:
            init_params[sym] = 0.0
            silent_risk_params.append((sym, dval, pmin, pmax, 0.0, "bypass toggle — forced to 0 (active)"))
            continue
        if sym in ENABLE_NAMES and pmin == 0 and pmax == 1:
            init_params[sym] = 1.0
            silent_risk_params.append((sym, dval, pmin, pmax, 1.0, "enable toggle — forced to 1"))
            continue

        clamped = max(pmin, min(pmax, dval)) if pmin is not None and pmax is not None else dval
        if clamped != dval:
            silent_risk_params.append((sym, dval, pmin, pmax, clamped, "default outside [min,max]"))
            init_params[sym] = clamped
        elif clamped <= 0.0 and pmin is not None and pmin > 0.0:
            silent_risk_params.append((sym, dval, pmin, pmax, pmin, "zero gain (min>0)"))
            init_params[sym] = pmin

    if silent_risk_params:
        print("\n  !! Silence-risk defaults detected (auto-corrected in graph params):")
        for sym, dval, pmin, pmax, fixed, reason in silent_risk_params:
            print(f"     '{sym}': default={dval}  range=[{pmin}, {pmax}]  → {fixed}  ({reason})")

    # -----------------------------------------------------------------------
    separator("Auto-generated graph (sine → plugin → mixer)")

    in0  = audio_ins[0]
    out0 = audio_outs[0]
    # If stereo, wire both channels; otherwise duplicate L to R
    in1  = audio_ins[1]  if len(audio_ins)  > 1 else in0
    out1 = audio_outs[1] if len(audio_outs) > 1 else out0

    graph = {
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": "src",    "type": "sine"},
            {"id": "fx",     "type": "lv2", "lv2_uri": args.uri,
             **({"params": init_params} if init_params else {})},
            {"id": "mixer",  "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            {"from_node": "src",  "from_port": "audio_out_L", "to_node": "fx",    "to_port": in0},
            {"from_node": "src",  "from_port": "audio_out_R", "to_node": "fx",    "to_port": in1},
            {"from_node": "fx",   "from_port": out0,          "to_node": "mixer", "to_port": "audio_in_L_0"},
            {"from_node": "fx",   "from_port": out1,          "to_node": "mixer", "to_port": "audio_in_R_0"},
        ]
    }
    print(json.dumps(graph, indent=2))

    # -----------------------------------------------------------------------
    separator("Loading graph")
    resp = c.send(graph)
    print(f"  set_graph → {resp}")
    if resp.get("status") != "ok":
        print("\n  !! Graph load failed. Check port symbols above.")
        c.close()
        sys.exit(1)

    # -----------------------------------------------------------------------
    separator("Render test (sine note through plugin)")
    sched = {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "src",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 2.0, "type": "note_off", "node_id": "src",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    }
    resp = c.send(sched)
    print(f"  set_schedule → {resp}")

    resp = c.send({"cmd": "render", "format": "wav"})
    if resp.get("status") != "ok":
        print(f"  !! render failed: {resp}")
        c.close()
        sys.exit(1)

    n_frames, n_ch, sr, nonzero, max_amp = wav_stats(resp["data"])
    print(f"  Rendered: {n_frames} frames, {n_ch} ch, {sr} Hz")
    print(f"  Non-zero samples: {nonzero}  max amplitude (s16): {max_amp}/32767")

    if nonzero == 0:
        print()
        print("  !! AUDIO IS SILENT after plugin. Possible causes:")
        print("     1. Wrong input port name  — connection from src not reaching plugin")
        print("        Expected: to_port = one of:", audio_ins)
        print("     2. Wrong output port name — plugin output not reaching mixer")
        print("        Expected: from_port = one of:", audio_outs)
        print("     3. Plugin is bypassed or has wet=0 by default — check control params:")
        for p in plugin["ports"]:
            if p["type"] == "control" and p["direction"] == "input":
                name = p["symbol"]
                if any(kw in name.lower() for kw in ["wet", "dry", "mix", "bypass", "enable", "level", "gain"]):
                    print(f"        '{name}' = {p.get('default','?')}  "
                          f"[{p.get('min','?')}, {p.get('max','?')}]")
        print("     4. Unsupported port type (atom/MIDI) not connected → run server with -DAS_DEBUG")
        print("     5. declare_ports() vs activate() port order mismatch → run with -DAS_DEBUG")
    else:
        print()
        print("  Audio is non-silent — plugin is working correctly.")

    # -----------------------------------------------------------------------
    # Optional: also test dry (sine only) and compare levels
    separator("Control: sine only (no plugin)")
    c.send({
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": "sine_0", "type": "sine"},
            {"id": "mixer",  "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            {"from_node": "sine_0", "from_port": "audio_out_L", "to_node": "mixer", "to_port": "audio_in_L_0"},
            {"from_node": "sine_0", "from_port": "audio_out_R", "to_node": "mixer", "to_port": "audio_in_R_0"},
        ]
    })
    c.send({
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 2.0, "type": "note_off", "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp2 = c.send({"cmd": "render", "format": "wav"})
    _, _, _, nz2, amp2 = wav_stats(resp2["data"])
    print(f"  Non-zero samples: {nz2}  max amplitude: {amp2}/32767")
    if nz2 > 0 and nonzero == 0:
        print("  → Dry signal is fine but plugin output is silent. Problem is in LV2 routing.")
    elif nz2 > 0 and nonzero > 0:
        print(f"  → Both paths produce audio. Ratio (plugin/dry): {max_amp}/{amp2} = "
              f"{max_amp/amp2:.2f}" if amp2 else "  (dry amp is 0 — unexpected)")

    c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LV2 port diagnostic")
    parser.add_argument("--address", default="/tmp/audio_server.sock")
    parser.add_argument("--uri", required=True, help="Full LV2 plugin URI")
    args = parser.parse_args()
    run(args)
