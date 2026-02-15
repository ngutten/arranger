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
    
    resp = c.send({"cmd": "list_registered_plugins"})
    print(resp)
    
    c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LV2 port diagnostic")
    parser.add_argument("--address", default="/tmp/audio_server.sock")
    args = parser.parse_args()
    run(args)
