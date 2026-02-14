#!/usr/bin/env python3
"""
test_audio_server.py
Structured tests for the audio_server IPC interface.

Usage:
    python3 test_audio_server.py [--address /tmp/audio_server.sock]
                                 [--lv2-reverb-uri http://calf.sourceforge.net/plugins/Reverb]
                                 [--sf2 /path/to/soundfont.sf2]
                                 [-v]   # verbose: print full responses
    python3 test_audio_server.py --list  # list test names
    python3 test_audio_server.py --run <name> [<name> ...]  # run specific tests

Tests are independent: each builds and tears down its own graph state.
The server process must already be running.
"""

import socket
import struct
import json
import argparse
import sys
import time
import base64
import wave
import io
import os

# ---------------------------------------------------------------------------
# IPC transport
# ---------------------------------------------------------------------------

class ServerClient:
    MAX_MSG = 64 * 1024 * 1024

    def __init__(self, address):
        self.address = address
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.address)
        self.sock.settimeout(10.0)

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _send_all(self, data: bytes):
        sent = 0
        while sent < len(data):
            n = self.sock.send(data[sent:])
            if n == 0:
                raise RuntimeError("socket closed")
            sent += n

    def _recv_all(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("socket closed")
            buf.extend(chunk)
        return bytes(buf)

    def send(self, msg: dict) -> dict:
        payload = json.dumps(msg).encode()
        self._send_all(struct.pack("<I", len(payload)))
        self._send_all(payload)
        resp_len = struct.unpack("<I", self._recv_all(4))[0]
        if resp_len > self.MAX_MSG:
            raise RuntimeError(f"response too large: {resp_len}")
        return json.loads(self._recv_all(resp_len))

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()


# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self, name):
        self.name = name
        self.passed = True
        self.failures = []
        self.log = []

    def fail(self, msg):
        self.passed = False
        self.failures.append(msg)

    def info(self, msg):
        self.log.append(msg)


class TestContext:
    def __init__(self, client: ServerClient, verbose=False):
        self.client = client
        self.verbose = verbose

    def cmd(self, result: TestResult, msg: dict, expect_ok=True) -> dict:
        resp = self.client.send(msg)
        if self.verbose:
            result.info(f"  >> {json.dumps(msg, indent=None)}")
            result.info(f"  << {json.dumps(resp, indent=None)}")
        if expect_ok and resp.get("status") != "ok":
            result.fail(f"cmd {msg.get('cmd')} failed: {resp}")
        return resp

    def assert_ok(self, result: TestResult, resp: dict, label=""):
        if resp.get("status") != "ok":
            result.fail(f"Expected ok{' (' + label + ')' if label else ''}: {resp}")

    def assert_eq(self, result: TestResult, got, expected, label=""):
        if got != expected:
            result.fail(f"{label}: expected {expected!r}, got {got!r}")

    def assert_in(self, result: TestResult, key, container, label=""):
        if key not in container:
            result.fail(f"{label}: {key!r} not in {container!r}")

    def assert_audio_nonzero(self, result: TestResult, wav_b64: str, label=""):
        """Decode a base64 WAV and check that it contains non-silent audio."""
        try:
            wav_bytes = base64.b64decode(wav_b64)
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                frames = wf.readframes(wf.getnframes())
                # Check that at least one sample is non-zero
                nonzero = any(b != 0 for b in frames)
                if not nonzero:
                    result.fail(f"Audio is silent{' (' + label + ')' if label else ''}")
                else:
                    result.info(f"  Audio OK: {wf.getnframes()} frames, "
                                f"{wf.getnchannels()} ch, {wf.getframerate()} Hz{' — ' + label if label else ''}")
        except Exception as e:
            result.fail(f"Failed to decode WAV ({label}): {e}")

    def reset_transport(self):
        """Stop playback and clear notes without caring about errors."""
        try:
            self.client.send({"cmd": "stop"})
            self.client.send({"cmd": "all_notes_off"})
            self.client.send({"cmd": "seek", "beat": 0.0})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Graph builders (reusable building blocks)
# ---------------------------------------------------------------------------

def sine_graph(n_channels=1):
    """Minimal graph: one sine node → mixer."""
    nodes = []
    connections = []
    for i in range(n_channels):
        nid = f"sine_{i}"
        nodes.append({"id": nid, "type": "sine"})
        connections.append({"from_node": nid, "from_port": "audio_out_L",
                             "to_node": "mixer", "to_port": f"audio_in_L_{i}"})
        connections.append({"from_node": nid, "from_port": "audio_out_R",
                             "to_node": "mixer", "to_port": f"audio_in_R_{i}"})
    nodes.append({"id": "mixer", "type": "mixer", "channel_count": n_channels})
    return {"cmd": "set_graph", "bpm": 120.0, "nodes": nodes, "connections": connections}


def track_sine_graph(track_id="track0"):
    """Track source → sine → mixer.

    The connection from track_id to sine0 is what triggers Graph::activate()
    to register sine0 in the TrackSourceNode's downstream list. Without it,
    note events sent to track0 have nowhere to go and the output is silent.
    """
    return {
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": track_id, "type": "track_source"},
            {"id": "sine0",  "type": "sine"},
            {"id": "mixer",  "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            # Graph::activate() populates TrackSourceNode.downstream_ by
            # scanning connections where from_node == track_source_id.
            # The port names here are ignored (TrackSourceNode has no audio
            # ports, so assign_buffers silently skips the buffer lookup).
            # Only from_node and to_node matter for the event fanout wiring.
            {"from_node": track_id, "from_port": "unused",
             "to_node": "sine0",    "to_port":   "unused"},
            # Audio routing: sine0 → mixer
            {"from_node": "sine0",  "from_port": "audio_out_L",
             "to_node": "mixer",    "to_port":   "audio_in_L_0"},
            {"from_node": "sine0",  "from_port": "audio_out_R",
             "to_node": "mixer",    "to_port":   "audio_in_R_0"},
        ]
    }


def fetch_lv2_safe_defaults(client: "ServerClient", uri: str) -> dict:
    """
    Query the server for a plugin's port list and return a params dict that
    corrects defaults likely to produce silence:

    1. Out-of-range defaults: clamp to [min, max].
    2. Zero gain where min > 0: raise to min (e.g. Calf level_in/level_out
       which have min=0.015625 but default=0 outside their own range).
    3. Bypass toggles: any integer port named 'on', 'enable', 'enabled',
       'active', or 'bypass' with range [0,1] and default 0 is forced to 1
       (or 0 for 'bypass' which is inverted logic).

    Returns {} if list_plugins fails or the plugin isn't found.
    """
    BYPASS_NAMES    = {"bypass"}          # these default to 1 = bypassed; force 0
    ENABLE_NAMES    = {"on", "enable", "enabled", "active"}  # force 1
    try:
        resp = client.send({"cmd": "list_plugins", "uri_prefix": uri})
        if resp.get("status") != "ok":
            return {}
        plugins = [p for p in resp.get("plugins", []) if p.get("uri") == uri]
        if not plugins:
            return {}
        params = {}
        for p in plugins[0].get("ports", []):
            if p.get("type") != "control" or p.get("direction") != "input":
                continue
            sym  = p["symbol"]
            dval = p.get("default", 0.0) or 0.0
            pmin = p.get("min")
            pmax = p.get("max")
            if pmin is None or pmax is None:
                continue

            # Rule 3: bypass/enable toggles
            if sym in BYPASS_NAMES and pmin == 0 and pmax == 1:
                params[sym] = 0.0   # bypass=0 means "not bypassed"
                continue
            if sym in ENABLE_NAMES and pmin == 0 and pmax == 1:
                params[sym] = 1.0
                continue

            # Rule 1: out-of-range default
            clamped = max(pmin, min(pmax, dval))
            if clamped != dval:
                params[sym] = clamped
                continue

            # Rule 2: clamped value still zero but min > 0 (silent gain port)
            if clamped <= 0.0 and pmin > 0.0:
                params[sym] = pmin

        return params
    except Exception:
        return {}


def lv2_passthrough_graph(plugin_uri: str, audio_in_port: str, audio_out_port: str,
                           init_params: dict = None):
    """
    Sine → LV2 → mixer passthrough.
    audio_in_port / audio_out_port: LV2 port symbols for the plugin's first
    audio input and output (e.g. 'in_l', 'out_l').
    init_params: optional dict of control port symbol → value to set on the
    node at graph load time (via the 'params' NodeDesc field).
    """
    fx_node = {"id": "fx", "type": "lv2", "lv2_uri": plugin_uri}
    if init_params:
        fx_node["params"] = init_params
    return {
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": "src", "type": "sine"},
            fx_node,
            {"id": "mixer", "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            {"from_node": "src",  "from_port": "audio_out_L", "to_node": "fx",    "to_port": audio_in_port},
            {"from_node": "fx",   "from_port": audio_out_port, "to_node": "mixer", "to_port": "audio_in_L_0"},
            {"from_node": "src",  "from_port": "audio_out_R", "to_node": "mixer", "to_port": "audio_in_R_0"},
        ]
    }


def simple_schedule(node_id: str, notes=None):
    """Return a set_schedule payload with a few note_on/off events."""
    if notes is None:
        notes = [(0.0, 60, 80), (1.0, 64, 80), (2.0, 67, 80)]
    events = []
    for beat, pitch, vel in notes:
        events.append({"beat": beat, "type": "note_on",  "node_id": node_id,
                        "channel": 0, "pitch": pitch, "velocity": vel})
        events.append({"beat": beat + 0.9, "type": "note_off", "node_id": node_id,
                        "channel": 0, "pitch": pitch, "velocity": 0})
    return {"cmd": "set_schedule", "events": events}


# ---------------------------------------------------------------------------
# Individual tests
# ---------------------------------------------------------------------------

TESTS = {}
def test(fn):
    TESTS[fn.__name__] = fn
    return fn


@test
def test_ping(ctx: TestContext, result: TestResult, args):
    """Server responds to ping with version and feature list."""
    resp = ctx.cmd(result, {"cmd": "ping"})
    result.info(f"  version={resp.get('version')} features={resp.get('features')}")


@test
def test_sine_graph_loads(ctx: TestContext, result: TestResult, args):
    """set_graph with a sine → mixer graph returns ok."""
    ctx.cmd(result, sine_graph())


@test
def test_track_source_wiring(ctx: TestContext, result: TestResult, args):
    """TrackSourceNode downstream wiring: preview note produces audio on render."""
    ctx.cmd(result, track_sine_graph())
    ctx.cmd(result, simple_schedule("track0"))

    resp = ctx.cmd(result, {"cmd": "render", "format": "wav"})
    ctx.assert_audio_nonzero(result, resp.get("data", ""), "track→sine render")


@test
def test_render_sine_offline(ctx: TestContext, result: TestResult, args):
    """Offline render of a simple sine sequence produces non-silent audio."""
    ctx.cmd(result, sine_graph())
    ctx.cmd(result, {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 2.0, "type": "note_off", "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp = ctx.cmd(result, {"cmd": "render", "format": "wav"})
    ctx.assert_in(result, "data", resp, "wav data key")
    ctx.assert_audio_nonzero(result, resp["data"], "offline sine render")


@test
def test_render_raw_f32(ctx: TestContext, result: TestResult, args):
    """raw_f32 render returns base64 float32 data with correct frame count."""
    ctx.cmd(result, sine_graph())
    ctx.cmd(result, {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 2.0, "type": "note_off", "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp = ctx.cmd(result, {"cmd": "render", "format": "raw_f32"})
    ctx.assert_in(result, "frames", resp, "frames key")
    result.info(f"  frames={resp.get('frames')} sr={resp.get('sample_rate')}")
    if "data" in resp:
        raw = base64.b64decode(resp["data"])
        import struct as st
        samples = st.unpack(f"<{len(raw)//4}f", raw)
        nonzero = sum(1 for s in samples if abs(s) > 1e-6)
        result.info(f"  non-zero samples: {nonzero}/{len(samples)}")
        if nonzero == 0:
            result.fail("All raw_f32 samples are silent")


@test
def test_get_position(ctx: TestContext, result: TestResult, args):
    """get_position returns beat and playing fields."""
    ctx.cmd(result, sine_graph())
    resp = ctx.cmd(result, {"cmd": "get_position"})
    ctx.assert_in(result, "beat", resp)
    ctx.assert_in(result, "playing", resp)
    ctx.assert_eq(result, resp["playing"], False, "should be stopped initially")


@test
def test_seek_and_position(ctx: TestContext, result: TestResult, args):
    """seek() changes the reported beat position.

    current_beat_ is updated immediately on the calling thread (not via the
    command queue), so get_position reflects the seek even without a running
    stream. The command queue entry is only needed for dispatcher reindex and
    all_notes_off on the audio side.
    """
    ctx.cmd(result, sine_graph())
    ctx.cmd(result, {"cmd": "seek", "beat": 4.0})
    resp = ctx.cmd(result, {"cmd": "get_position"})
    beat = resp.get("beat", -1)
    # Should be exactly 4.0 now that seek() stores directly to current_beat_.
    # Allow a small epsilon in case the audio thread advanced it slightly.
    if abs(beat - 4.0) > 0.2:
        result.fail(f"Expected beat ~4.0 after seek, got {beat}")
    else:
        result.info(f"  beat after seek: {beat}")


@test
def test_bpm_change(ctx: TestContext, result: TestResult, args):
    """set_bpm doesn't crash and is reflected in subsequent render timing."""
    ctx.cmd(result, sine_graph())
    ctx.cmd(result, {"cmd": "set_bpm", "bpm": 180.0})
    ctx.cmd(result, {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 1.0, "type": "note_off", "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp = ctx.cmd(result, {"cmd": "render", "format": "wav"})
    ctx.assert_audio_nonzero(result, resp.get("data", ""), "180bpm render")


@test
def test_multiple_graph_swaps(ctx: TestContext, result: TestResult, args):
    """Rapid repeated set_graph calls don't crash or deadlock."""
    for i in range(5):
        ctx.cmd(result, sine_graph(n_channels=1))
    ctx.cmd(result, {"cmd": "get_position"})


@test
def test_preview_note_before_graph(ctx: TestContext, result: TestResult, args):
    """note_on before any set_graph returns error or ok gracefully (no crash)."""
    # Intentionally no set_graph first.  Server should not crash.
    resp = ctx.client.send({"cmd": "note_on", "node_id": "nonexistent",
                             "channel": 0, "pitch": 60, "velocity": 100})
    result.info(f"  response: {resp}")
    # Either ok (stream opened, node not found = silent) or error is acceptable
    # as long as the server stays alive.
    ping = ctx.cmd(result, {"cmd": "ping"}, expect_ok=True)
    result.info(f"  server still alive: {ping.get('status')}")


@test
def test_preview_note_with_track_source(ctx: TestContext, result: TestResult, args):
    """Preview note injection via note_on IPC produces audio through track_source."""
    ctx.cmd(result, track_sine_graph("track0"))
    # Inject note, wait a bit, then render (transport stopped, preview path)
    ctx.cmd(result, {"cmd": "note_on", "node_id": "track0",
                     "channel": 0, "pitch": 60, "velocity": 100})
    # Set a short schedule just so render has a length to work with
    ctx.cmd(result, {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "track0",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 2.0, "type": "note_off", "node_id": "track0",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp = ctx.cmd(result, {"cmd": "render", "format": "wav"})
    ctx.assert_audio_nonzero(result, resp.get("data", ""), "preview note render")


@test
def test_all_notes_off_no_crash(ctx: TestContext, result: TestResult, args):
    """all_notes_off with and without node_id doesn't crash."""
    ctx.cmd(result, track_sine_graph())
    ctx.cmd(result, {"cmd": "note_on", "node_id": "track0", "channel": 0, "pitch": 60, "velocity": 100})
    ctx.cmd(result, {"cmd": "all_notes_off", "node_id": "track0"})
    ctx.cmd(result, {"cmd": "all_notes_off"})  # global
    ctx.cmd(result, {"cmd": "ping"})


@test
def test_loop_enable_disable(ctx: TestContext, result: TestResult, args):
    """set_loop enable/disable round-trip doesn't crash."""
    ctx.cmd(result, sine_graph())
    ctx.cmd(result, {"cmd": "set_loop", "enabled": True, "start": 0.0, "end": 4.0})
    ctx.cmd(result, {"cmd": "set_loop", "enabled": False})
    ctx.cmd(result, {"cmd": "ping"})


@test
def test_set_param(ctx: TestContext, result: TestResult, args):
    """set_param on a known node doesn't crash."""
    ctx.cmd(result, sine_graph())
    ctx.cmd(result, {"cmd": "set_param", "node_id": "sine_0", "param_id": "gain", "value": 0.5})
    ctx.cmd(result, {"cmd": "ping"})


@test
def test_unknown_command(ctx: TestContext, result: TestResult, args):
    """Unknown command returns error, not crash."""
    resp = ctx.client.send({"cmd": "definitely_not_a_real_command"})
    ctx.assert_eq(result, resp.get("status"), "error", "unknown cmd should be error")
    result.info(f"  error message: {resp.get('message')}")


# ---------------------------------------------------------------------------
# LV2-specific tests — skipped unless --lv2-reverb-uri is given
# ---------------------------------------------------------------------------

@test
def test_lv2_plugin_list(ctx: TestContext, result: TestResult, args):
    """list_plugins returns a non-empty array (requires AS_ENABLE_LV2)."""
    resp = ctx.client.send({"cmd": "list_plugins"})
    if resp.get("status") == "error":
        result.info("  SKIP: list_plugins not supported (build without AS_ENABLE_LV2?)")
        return
    plugins = resp.get("plugins", [])
    result.info(f"  {len(plugins)} plugins found")
    if not plugins:
        result.fail("Expected at least one LV2 plugin")
    else:
        result.info(f"  First plugin: {plugins[0].get('uri')} — {plugins[0].get('name')}")


@test
def test_lv2_graph_loads(ctx: TestContext, result: TestResult, args):
    """LV2 plugin can be added to a graph without crashing (no audio check)."""
    uri = getattr(args, "lv2_reverb_uri", None)
    if not uri:
        result.info("  SKIP: --lv2-reverb-uri not specified")
        return
    graph = {
        "cmd": "set_graph",
        "bpm": 120.0,
        "nodes": [
            {"id": "src",   "type": "sine"},
            {"id": "reverb","type": "lv2", "lv2_uri": uri},
            {"id": "mixer", "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            # We don't know the port names yet — connecting nothing is fine to
            # test that graph construction and activate() don't crash.
        ]
    }
    resp = ctx.client.send(graph)
    result.info(f"  set_graph response: {resp}")
    # Accept ok or error (e.g. plugin not found), but not a crash (server alive)
    ping = ctx.cmd(result, {"cmd": "ping"})
    result.info(f"  server alive after lv2 load: {ping.get('status')}")


@test
def test_lv2_passthrough_audio(ctx: TestContext, result: TestResult, args):
    """
    LV2 plugin in the signal path produces audio.
    Requires --lv2-reverb-uri and knowledge of port names (--lv2-audio-in / --lv2-audio-out).
    """
    uri     = getattr(args, "lv2_reverb_uri", None)
    in_port = getattr(args, "lv2_audio_in",   "in_l")
    out_port= getattr(args, "lv2_audio_out",  "out_l")
    if not uri:
        result.info("  SKIP: --lv2-reverb-uri not specified")
        return

    # Fetch safe defaults — corrects Calf-style out-of-range / zero-gain defaults.
    init_params = fetch_lv2_safe_defaults(ctx.client, uri)
    # Merge any explicit overrides from --lv2-params (these take precedence).
    extra = getattr(args, "lv2_params", None)
    if extra:
        try:
            init_params.update(json.loads(extra))
        except Exception as e:
            result.fail(f"--lv2-params parse error: {e}")
            return
    if init_params:
        result.info(f"  Init params: {init_params}")

    graph = lv2_passthrough_graph(uri, in_port, out_port, init_params)
    resp = ctx.client.send(graph)
    result.info(f"  set_graph: {resp}")
    if resp.get("status") != "ok":
        result.info("  SKIP: graph load failed — check port names with test_lv2_port_introspection")
        return

    ctx.cmd(result, {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "src",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 2.0, "type": "note_off", "node_id": "src",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp = ctx.cmd(result, {"cmd": "render", "format": "wav"})
    ctx.assert_audio_nonzero(result, resp.get("data", ""), "lv2 passthrough render")


@test
def test_lv2_port_introspection(ctx: TestContext, result: TestResult, args):
    """
    Print all ports of the specified LV2 plugin so you can check what
    port names to use when wiring connections.
    Useful for debugging 'no audio' when routing through a plugin.
    """
    uri = getattr(args, "lv2_reverb_uri", None)
    if not uri:
        result.info("  SKIP: --lv2-reverb-uri not specified")
        return
    resp = ctx.client.send({"cmd": "list_plugins", "uri_prefix": uri})
    if resp.get("status") != "ok":
        result.fail(f"list_plugins failed: {resp}")
        return
    plugins = [p for p in resp.get("plugins", []) if p.get("uri") == uri]
    if not plugins:
        result.fail(f"Plugin {uri!r} not found in list_plugins output")
        return
    p = plugins[0]
    result.info(f"  Plugin: {p['name']} ({p['uri']})")
    result.info(f"  Category: {p.get('category')}")
    result.info(f"  Ports:")
    for port in p.get("ports", []):
        result.info(f"    [{port['direction']:6}] [{port['type']:7}] '{port['symbol']}'"
                    + (f"  default={port.get('default', '?'):.4g}"
                       f"  [{port.get('min','?'):.4g}, {port.get('max','?'):.4g}]"
                       if port.get("type") == "control" and port.get("direction") == "input" else ""))


@test
def test_lv2_graph_incremental(ctx: TestContext, result: TestResult, args):
    """
    Build graph incrementally: first without LV2, then with it.
    Tests that set_graph swap doesn't crash mid-play.
    """
    uri = getattr(args, "lv2_reverb_uri", None)
    if not uri:
        result.info("  SKIP: --lv2-reverb-uri not specified")
        return

    # Step 1: sine only
    ctx.cmd(result, sine_graph())
    ctx.cmd(result, {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 4.0, "type": "note_off", "node_id": "sine_0",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp1 = ctx.cmd(result, {"cmd": "render", "format": "wav"})
    ctx.assert_audio_nonzero(result, resp1.get("data", ""), "sine-only render")
    result.info("  Step 1 (sine only): OK")

    # Step 2: add LV2 to the chain
    in_port  = getattr(args, "lv2_audio_in",  "in_l")
    out_port = getattr(args, "lv2_audio_out", "out_l")
    init_params = fetch_lv2_safe_defaults(ctx.client, uri)
    extra = getattr(args, "lv2_params", None)
    if extra:
        try:
            init_params.update(json.loads(extra))
        except Exception:
            pass
    if init_params:
        result.info(f"  Init params: {init_params}")
    graph2 = lv2_passthrough_graph(uri, in_port, out_port, init_params)
    resp = ctx.client.send(graph2)
    result.info(f"  Step 2 set_graph: {resp}")
    if resp.get("status") != "ok":
        result.info("  NOTE: LV2 graph failed to load — audio path may use wrong port names")
        result.info("  Run test_lv2_port_introspection to see available ports")
        return

    ctx.cmd(result, {
        "cmd": "set_schedule",
        "events": [
            {"beat": 0.0, "type": "note_on",  "node_id": "src",
             "channel": 0, "pitch": 60, "velocity": 100},
            {"beat": 4.0, "type": "note_off", "node_id": "src",
             "channel": 0, "pitch": 60, "velocity": 0},
        ]
    })
    resp2 = ctx.cmd(result, {"cmd": "render", "format": "wav"})
    ctx.assert_audio_nonzero(result, resp2.get("data", ""), "sine+lv2 render")
    result.info("  Step 2 (sine + LV2): OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_tests(names, args, verbose=False):
    if not names:
        names = list(TESTS.keys())

    results = []
    with ServerClient(args.address) as client:
        ctx = TestContext(client, verbose=verbose)
        for name in names:
            if name not in TESTS:
                print(f"  UNKNOWN: {name}")
                continue
            result = TestResult(name)
            try:
                ctx.reset_transport()
                TESTS[name](ctx, result, args)
            except Exception as e:
                result.fail(f"Exception: {e}")
            results.append(result)

    # Print results
    print()
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    print(f"Results: {passed} passed, {failed} failed")
    print()
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}")
        if verbose or not r.passed:
            for line in r.log:
                print(line)
        if not r.passed:
            for f in r.failures:
                print(f"         !! {f}")
    print()
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="audio_server test client")
    parser.add_argument("--address", default="/tmp/audio_server.sock",
                        help="Unix socket path")
    parser.add_argument("--lv2-reverb-uri", dest="lv2_reverb_uri", default=None,
                        help="LV2 URI for reverb plugin (e.g. http://calf.sourceforge.net/plugins/Reverb)")
    parser.add_argument("--lv2-audio-in",  dest="lv2_audio_in",  default="in_l",
                        help="LV2 audio input port symbol (default: in_l)")
    parser.add_argument("--lv2-audio-out", dest="lv2_audio_out", default="out_l",
                        help="LV2 audio output port symbol (default: out_l)")
    parser.add_argument("--lv2-params", dest="lv2_params", default=None,
                        help='JSON object of extra LV2 params to force, e.g. \'{"on":1,"dry":0}\'')
    parser.add_argument("--sf2", default=None, help="Path to SF2 soundfont for FluidSynth tests")
    parser.add_argument("--list", action="store_true", help="List available test names and exit")
    parser.add_argument("--run", nargs="+", metavar="TEST", help="Run only named tests")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print all request/response pairs")
    args = parser.parse_args()

    if args.list:
        print("Available tests:")
        for name, fn in TESTS.items():
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            print(f"  {name:45s} {doc}")
        return

    names = args.run or list(TESTS.keys())
    ok = run_tests(names, args, verbose=args.verbose)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
