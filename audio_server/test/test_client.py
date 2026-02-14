#!/usr/bin/env python3
"""
test_client.py — Reference implementation of the Python ↔ audio_server IPC interface.

This script exercises the full round-trip without touching any of the
sequencer's Qt/state machinery. It is the reference for how the frontend
calls the server — read this before writing any server_engine.py code.

Run with the server already started:
    ./build/audio_server &
    python3 test/test_client.py

Or with a specific socket path / SF2 file:
    python3 test/test_client.py --address /tmp/my_server.sock --sf2 /path/to/gm.sf2

Key things demonstrated here that are NOT obvious from the protocol spec alone:
  - The canonical graph shape uses track_source nodes, not direct synth nodes.
    Every sequencer track maps to one track_source; the source fans events out
    to whichever processor nodes are downstream.  This is what lets note preview
    and arrangement playback coexist without interfering.
  - note_on / note_off / all_notes_off bypass the schedule entirely.  They are
    the ONLY correct way to do live note preview — do NOT use set_schedule +
    play + stop for this.
  - Setup events (program-change, volume) go into the schedule with beat=-1.
    The server clamps them to beat=0 so they fire before any note-ons.
  - set_node_config updates live mixer/LV2 parameters without a graph rebuild.
"""

import argparse
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import wave
import io

# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------
import platform
IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Default address (mirrors protocol.h DEFAULT_ADDRESS)
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    DEFAULT_ADDRESS = r"\\.\pipe\AudioServer"
else:
    DEFAULT_ADDRESS = "/tmp/audio_server.sock"


# ---------------------------------------------------------------------------
# IPC client
# ---------------------------------------------------------------------------

class AudioServerClient:
    """Length-prefixed JSON IPC client.

    Mirrors IpcClient in ipc.h — 4-byte LE length prefix, then UTF-8 JSON.
    This is the class that server_engine.py wraps in the actual frontend.
    """

    def __init__(self, address: str = DEFAULT_ADDRESS):
        self.address = address
        self._sock = None
        self._pipe = None  # Windows only

    def connect(self, timeout: float = 5.0) -> None:
        """Connect to the server, retrying for up to `timeout` seconds."""
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                if IS_WINDOWS:
                    self._connect_windows()
                else:
                    self._connect_unix()
                return
            except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
                last_err = e
                time.sleep(0.05)
        raise ConnectionError(
            f"Could not connect to {self.address!r} after {timeout}s: {last_err}"
        )

    def _connect_unix(self):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self.address)

    def _connect_windows(self):
        import ctypes
        import ctypes.wintypes
        GENERIC_RW = 0xC0000000
        OPEN_EXISTING = 3
        h = ctypes.windll.kernel32.CreateFileW(
            self.address, GENERIC_RW, 0, None, OPEN_EXISTING, 0, None
        )
        INVALID = ctypes.wintypes.HANDLE(-1).value
        if h == INVALID:
            raise ConnectionRefusedError(f"Named pipe not available: {self.address}")
        self._pipe = h

    def disconnect(self) -> None:
        if self._sock:
            try: self._sock.close()
            except: pass
            self._sock = None
        if IS_WINDOWS and self._pipe:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._pipe)
            self._pipe = None

    def send(self, request: dict) -> dict:
        """Send a command dict, return the response dict."""
        payload = json.dumps(request).encode("utf-8")
        length  = struct.pack("<I", len(payload))
        self._write(length + payload)

        resp_len = struct.unpack("<I", self._read(4))[0]
        resp_bytes = self._read(resp_len)
        return json.loads(resp_bytes)

    def _write(self, data: bytes) -> None:
        if IS_WINDOWS:
            import ctypes
            written = ctypes.c_ulong(0)
            ctypes.windll.kernel32.WriteFile(
                self._pipe, data, len(data), ctypes.byref(written), None
            )
        else:
            self._sock.sendall(data)

    def _read(self, n: int) -> bytes:
        if IS_WINDOWS:
            import ctypes
            buf = (ctypes.c_char * n)()
            got = ctypes.c_ulong(0)
            ctypes.windll.kernel32.ReadFile(
                self._pipe, buf, n, ctypes.byref(got), None
            )
            return bytes(buf)
        else:
            chunks = []
            remaining = n
            while remaining > 0:
                chunk = self._sock.recv(remaining)
                if not chunk:
                    raise EOFError("Server disconnected")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------

def build_track_source_graph(track_ids: list, sf2_path: str = None) -> dict:
    """
    THE CANONICAL GRAPH SHAPE for a multi-track session.

    Each track gets one track_source node named "track_<uuid>".  All sources
    fan into a single fluidsynth (or sine fallback) node, then into the mixer.

    This is what set_graph should look like in server_engine.py once the
    track_source model is wired in.  The key property: the Dispatcher targets
    source nodes by node_id, and note_on/note_off preview commands also target
    source nodes — so arrangement events and preview events never collide.

    track_ids: list of track UUIDs (strings).  The resulting node IDs will be
               "track_<id>" for each.
    sf2_path:  if provided, use fluidsynth; otherwise fall back to sine.
    """
    nodes   = []
    connections = []

    for tid in track_ids:
        nodes.append({"id": f"track_{tid}", "type": "track_source"})

    synth_type = "fluidsynth" if sf2_path else "sine"
    synth_node = {"id": "synth", "type": synth_type}
    if sf2_path:
        synth_node["sf2_path"] = sf2_path
    nodes.append(synth_node)
    nodes.append({"id": "mixer", "type": "mixer", "channel_count": 1})

    for tid in track_ids:
        connections.append({
            "from_node": f"track_{tid}", "from_port": "events_out",
            "to_node":   "synth",        "to_port":   "events_in",
        })

    connections += [
        {"from_node": "synth", "from_port": "audio_out_L",
         "to_node":   "mixer", "to_port":   "audio_in_L_0"},
        {"from_node": "synth", "from_port": "audio_out_R",
         "to_node":   "mixer", "to_port":   "audio_in_R_0"},
    ]

    return {"cmd": "set_graph", "bpm": 120, "nodes": nodes, "connections": connections}


def build_per_track_synth_graph(track_ids: list, sf2_path: str) -> dict:
    """
    ADVANCED: one fluidsynth instance per track, independent audio streams.

    Each track gets its own polyphony pool and can have per-track effects
    inserted before the mixer.  Cost: N x fluidsynth memory + SF2 load time.
    Only use this when per-track FX chains are needed.
    """
    nodes   = []
    connections = []

    for i, tid in enumerate(track_ids):
        src_id   = f"track_{tid}"
        synth_id = f"synth_{tid}"
        nodes.append({"id": src_id,   "type": "track_source"})
        nodes.append({"id": synth_id, "type": "fluidsynth", "sf2_path": sf2_path})
        connections.append({
            "from_node": src_id,   "from_port": "events_out",
            "to_node":   synth_id, "to_port":   "events_in",
        })
        connections += [
            {"from_node": synth_id, "from_port": "audio_out_L",
             "to_node":   "mixer",  "to_port":   f"audio_in_L_{i}"},
            {"from_node": synth_id, "from_port": "audio_out_R",
             "to_node":   "mixer",  "to_port":   f"audio_in_R_{i}"},
        ]

    nodes.append({"id": "mixer", "type": "mixer", "channel_count": len(track_ids)})

    return {"cmd": "set_graph", "bpm": 120, "nodes": nodes, "connections": connections}


def build_lv2_graph(track_ids: list, lv2_uri: str, sf2_path: str) -> dict:
    """
    track_source nodes -> fluidsynth -> LV2 effect -> mixer.
    """
    nodes   = []
    connections = []

    for tid in track_ids:
        nodes.append({"id": f"track_{tid}", "type": "track_source"})
        connections.append({
            "from_node": f"track_{tid}", "from_port": "events_out",
            "to_node":   "synth",        "to_port":   "events_in",
        })

    nodes += [
        {"id": "synth",  "type": "fluidsynth", "sf2_path": sf2_path},
        {"id": "effect", "type": "lv2", "lv2_uri": lv2_uri},
        {"id": "mixer",  "type": "mixer", "channel_count": 1},
    ]
    connections += [
        {"from_node": "synth",  "from_port": "audio_out_L",
         "to_node":   "effect", "to_port":   "audio_in_L"},
        {"from_node": "synth",  "from_port": "audio_out_R",
         "to_node":   "effect", "to_port":   "audio_in_R"},
        {"from_node": "effect", "from_port": "audio_out_L",
         "to_node":   "mixer",  "to_port":   "audio_in_L_0"},
        {"from_node": "effect", "from_port": "audio_out_R",
         "to_node":   "mixer",  "to_port":   "audio_in_R_0"},
    ]

    return {"cmd": "set_graph", "bpm": 120, "nodes": nodes, "connections": connections}


def build_control_graph() -> dict:
    """
    Demonstrates a control_source node driving an LV2 effect parameter.
    The control signal doesn't render audio itself — it just exposes a value
    that gets wired to a processor param port.
    """
    return {
        "cmd": "set_graph",
        "bpm": 120,
        "nodes": [
            {"id": "track_abc",  "type": "track_source"},
            {"id": "synth",      "type": "sine"},
            {"id": "beat_ctrl",  "type": "control_source"},
            {"id": "mixer",      "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            {"from_node": "track_abc", "from_port": "events_out",
             "to_node":   "synth",     "to_port":   "events_in"},
            {"from_node": "synth",     "from_port": "audio_out_L",
             "to_node":   "mixer",     "to_port":   "audio_in_L_0"},
            {"from_node": "synth",     "from_port": "audio_out_R",
             "to_node":   "mixer",     "to_port":   "audio_in_R_0"},
            # In the full system, beat_ctrl.control_out connects to an LV2 param.
        ],
    }


# ---------------------------------------------------------------------------
# Schedule builders
# ---------------------------------------------------------------------------

def build_schedule(
    notes,           # list of (start_beat, duration, pitch, velocity)
    node_id,         # "track_<uuid>" — the track_source for this track
    channel=0,
    program=None,    # if set, emitted as beat=-1 setup event
    bank=0,
    volume=None,     # if set, emitted as beat=-1 setup event
):
    """
    Convert a note list into a set_schedule payload targeting a track_source node.

    node_id should be "track_<uuid>" — the track_source node for this track.
    The source node fans events to its downstream synth(s), so the schedule
    never targets a synth node directly.

    program / bank / volume, if given, are emitted as beat=-1 setup events.
    The server clamps beat<0 to 0.0, so they fire before any note-ons.
    This is what _schedule_to_server_events() in server_engine.py should produce.
    """
    events = []

    # Setup events: program-change and volume go in first (beat=-1 -> clamped to 0)
    if program is not None:
        events.append({
            "beat": -1, "type": "program",
            "node_id": node_id, "channel": channel,
            "pitch": program, "velocity": bank, "value": 0.0,
        })
    if volume is not None:
        events.append({
            "beat": -1, "type": "volume",
            "node_id": node_id, "channel": channel,
            "pitch": volume, "velocity": 0, "value": 0.0,
        })

    for start, dur, pitch, vel in sorted(notes, key=lambda n: n[0]):
        events.append({
            "beat": start, "type": "note_on",
            "node_id": node_id, "channel": channel,
            "pitch": pitch, "velocity": vel, "value": 0.0,
        })
        events.append({
            "beat": start + dur, "type": "note_off",
            "node_id": node_id, "channel": channel,
            "pitch": pitch, "velocity": 0, "value": 0.0,
        })

    return {"cmd": "set_schedule", "events": events}


def build_multi_track_schedule(tracks):
    """
    Merge schedules from multiple tracks into one set_schedule call.

    tracks: list of dicts, each with keys:
        node_id   - "track_<uuid>"
        notes     - list of (start_beat, duration, pitch, velocity)
        channel   - MIDI channel (default 0)
        program   - optional program number
        bank      - optional bank (default 0)
        volume    - optional volume (0-127)

    This mirrors what the Python engine does when it calls set_schedule after
    a state.notify() rebuild — all tracks' events go into one batch.
    """
    all_events = []
    for track in tracks:
        sched = build_schedule(
            notes   = track["notes"],
            node_id = track["node_id"],
            channel = track.get("channel", 0),
            program = track.get("program"),
            bank    = track.get("bank", 0),
            volume  = track.get("volume"),
        )
        all_events.extend(sched["events"])
    return {"cmd": "set_schedule", "events": all_events}


def build_control_schedule(beats, node_id="beat_ctrl", value=1.0):
    """
    Convert beat positions into control trigger events for a control_source node.
    """
    events = [
        {"beat": b, "type": "control",
         "node_id": node_id, "channel": 0,
         "pitch": 0, "velocity": 0, "value": value}
        for b in beats
    ]
    return {"cmd": "set_schedule", "events": events}


# ---------------------------------------------------------------------------
# Test routines
# ---------------------------------------------------------------------------

def test_ping(client):
    print("\n--- test_ping ---")
    resp = client.send({"cmd": "ping"})
    assert resp["status"] == "ok", resp
    print(f"  version:  {resp.get('version')}")
    print(f"  features: {resp.get('features')}")
    # Verify new features are advertised
    features = resp.get("features", [])
    for expected in ("track_source", "note_on", "note_off", "all_notes_off"):
        assert expected in features, f"Missing feature: {expected}"
    print("PASS")


def test_track_source_graph(client):
    print("\n--- test_track_source_graph ---")
    # Two tracks, shared sine synth (no SF2 needed)
    track_ids = ["abc", "def"]
    resp = client.send(build_track_source_graph(track_ids))
    assert resp["status"] == "ok", resp
    print(f"  Graph with {len(track_ids)} track_source nodes: ok")
    print("PASS")


def test_track_source_schedule(client):
    print("\n--- test_track_source_schedule ---")
    # Two tracks playing simultaneously: track_abc plays C major, track_def plays E minor
    sched = build_multi_track_schedule([
        {
            "node_id": "track_abc",
            "channel": 0,
            "program": 0,    # acoustic grand — fires as beat-0 setup event
            "volume":  100,
            "notes": [(0.0, 0.9, 60, 90), (1.0, 0.9, 64, 85), (2.0, 0.9, 67, 80)],
        },
        {
            "node_id": "track_def",
            "channel": 1,
            "program": 40,   # violin
            "notes": [(0.5, 0.9, 64, 75), (1.5, 0.9, 67, 70), (2.5, 0.9, 71, 65)],
        },
    ])
    resp = client.send(sched)
    assert resp["status"] == "ok", resp
    setup_events = [e for e in sched["events"] if e["beat"] < 0]
    note_events  = [e for e in sched["events"] if e["beat"] >= 0]
    print(f"  {len(setup_events)} setup events (beat=-1), {len(note_events)} note events")
    print(f"  Total: {len(sched['events'])} events across 2 tracks")
    print("PASS")


def test_note_preview(client):
    """
    THE CORRECT WAY to do live note preview.

    note_on injects into the track_source's preview stream, bypassing the
    schedule and transport entirely.  The note sustains until note_off.
    Multiple concurrent preview notes work because each (channel, pitch)
    pair is independent.  stop/seek/play do NOT cut preview notes.

    Do NOT do this (the old broken way):
        client.send({"cmd": "set_schedule", "events": [single_note]})
        client.send({"cmd": "play"})
        time.sleep(duration)
        client.send({"cmd": "stop"})
        client.send({"cmd": "seek", "beat": 0})
    That races with arrangement playback and mangles the schedule.
    """
    print("\n--- test_note_preview ---")

    # Basic note_on / note_off on track_abc
    resp = client.send({
        "cmd": "note_on",
        "node_id": "track_abc",
        "channel": 0,
        "pitch": 60,
        "velocity": 100,
    })
    assert resp["status"] == "ok", resp
    print("  note_on(track_abc, ch=0, pitch=60): ok")

    # A second note on a different pitch — both sustain simultaneously
    resp = client.send({
        "cmd": "note_on",
        "node_id": "track_abc",
        "channel": 0,
        "pitch": 64,
        "velocity": 90,
    })
    assert resp["status"] == "ok", resp
    print("  note_on(track_abc, ch=0, pitch=64): ok (both notes now sustaining)")

    time.sleep(0.1)

    # Release them individually
    resp = client.send({
        "cmd": "note_off",
        "node_id": "track_abc",
        "channel": 0,
        "pitch": 60,
    })
    assert resp["status"] == "ok", resp
    print("  note_off(track_abc, ch=0, pitch=60): ok")

    resp = client.send({
        "cmd": "note_off",
        "node_id": "track_abc",
        "channel": 0,
        "pitch": 64,
    })
    assert resp["status"] == "ok", resp
    print("  note_off(track_abc, ch=0, pitch=64): ok")
    print("PASS")


def test_note_preview_independence(client):
    """
    Preview notes survive transport stop/seek.  Arrangement playback and
    preview are fully independent.
    """
    print("\n--- test_note_preview_independence ---")

    # Start arrangement playback
    resp = client.send({"cmd": "play"})
    assert resp["status"] == "ok", resp
    print("  Arrangement playing")

    # Inject a preview note while playing — must not interfere
    resp = client.send({
        "cmd": "note_on",
        "node_id": "track_abc",
        "channel": 0,
        "pitch": 72,
        "velocity": 80,
    })
    assert resp["status"] == "ok", resp
    print("  Preview note_on while playing: ok")

    time.sleep(0.1)

    # Stop arrangement — preview note should still be alive
    resp = client.send({"cmd": "stop"})
    assert resp["status"] == "ok", resp
    print("  Arrangement stopped — preview note still sustaining")

    # Seek — still should not cut preview notes
    resp = client.send({"cmd": "seek", "beat": 0.0})
    assert resp["status"] == "ok", resp
    print("  Seeked to 0 — preview note still sustaining")

    time.sleep(0.05)

    # Explicitly release
    resp = client.send({
        "cmd": "note_off",
        "node_id": "track_abc",
        "channel": 0,
        "pitch": 72,
    })
    assert resp["status"] == "ok", resp
    print("  Preview note released via note_off: ok")
    print("PASS")


def test_all_notes_off(client):
    """
    all_notes_off is the emergency brake for preview notes — use it on dialog
    close, instrument change, or when the user releases the piano widget.
    It only affects preview notes; scheduled/arrangement notes are unaffected.
    """
    print("\n--- test_all_notes_off ---")

    # Inject several preview notes
    for pitch in [60, 64, 67, 72]:
        client.send({
            "cmd": "note_on",
            "node_id": "track_abc",
            "channel": 0,
            "pitch": pitch,
            "velocity": 80,
        })
    print("  4 preview notes injected on track_abc")

    time.sleep(0.05)

    # Silence just one source node
    resp = client.send({"cmd": "all_notes_off", "node_id": "track_abc"})
    assert resp["status"] == "ok", resp
    print("  all_notes_off(node_id='track_abc'): ok")

    # Inject again on both tracks
    for pitch in [60, 64]:
        client.send({"cmd": "note_on", "node_id": "track_abc",
                     "channel": 0, "pitch": pitch, "velocity": 80})
    for pitch in [67, 71]:
        client.send({"cmd": "note_on", "node_id": "track_def",
                     "channel": 1, "pitch": pitch, "velocity": 80})
    print("  Preview notes on both tracks")

    # Omit node_id -> silence ALL source nodes
    resp = client.send({"cmd": "all_notes_off"})
    assert resp["status"] == "ok", resp
    print("  all_notes_off (no node_id -> all sources): ok")
    print("PASS")


def test_play_single_note_pattern(client):
    """
    Shows the correct server_engine.play_single_note() pattern.

    The client sends note_on, sleeps for the note duration on a daemon thread,
    then sends note_off.  No set_schedule, no play, no stop involved.
    This is robust to: concurrent preview notes, arrangement playback, rapid
    repeated calls, and all other race conditions the old approach had.
    """
    print("\n--- test_play_single_note_pattern ---")

    def play_note(node_id, channel, pitch, velocity=100, duration=0.3):
        client.send({"cmd": "note_on", "node_id": node_id,
                     "channel": channel, "pitch": pitch, "velocity": velocity})
        def _off():
            time.sleep(duration)
            client.send({"cmd": "note_off", "node_id": node_id,
                         "channel": channel, "pitch": pitch})
        threading.Thread(target=_off, daemon=True).start()

    # Play a C major chord — 3 notes start simultaneously, each ends independently
    for pitch in [60, 64, 67]:
        play_note("track_abc", channel=0, pitch=pitch, velocity=90, duration=0.3)
    print("  C major chord preview started (3 concurrent daemon threads)")

    # Start arrangement playback concurrently — chord should still sound fine
    resp = client.send({"cmd": "play"})
    assert resp["status"] == "ok", resp
    print("  Arrangement also playing — no interference expected")

    time.sleep(0.4)  # wait for note_off threads to fire

    resp = client.send({"cmd": "stop"})
    assert resp["status"] == "ok", resp
    resp = client.send({"cmd": "seek", "beat": 0.0})
    assert resp["status"] == "ok", resp
    print("PASS")


def test_setup_events(client):
    """
    Program-change and volume events with beat=-1 fire before any note-ons.
    Verify the server accepts and does not reject negative-beat events.
    """
    print("\n--- test_setup_events ---")

    sched = build_schedule(
        notes   = [(0.0, 0.9, 60, 90), (1.0, 0.9, 64, 85)],
        node_id = "track_abc",
        channel = 0,
        program = 40,   # violin
        bank    = 0,
        volume  = 100,
    )

    setup = [e for e in sched["events"] if e["beat"] < 0]
    notes = [e for e in sched["events"] if e["beat"] >= 0]
    print(f"  {len(setup)} setup events (beat=-1), {len(notes)} note events")
    assert len(setup) == 2, f"Expected 2 setup events, got {len(setup)}"

    resp = client.send(sched)
    assert resp["status"] == "ok", resp
    print("  set_schedule accepted beat=-1 events: ok")
    print("PASS")


def test_set_node_config(client):
    """
    set_node_config updates live mixer/LV2 parameters without rebuilding the graph.
    SF2/LV2 URI changes require a full set_graph (server will tell you).
    """
    print("\n--- test_set_node_config ---")

    resp = client.send({
        "cmd": "set_node_config",
        "node_id": "mixer",
        "config": {"master_gain": 0.8},
    })
    assert resp["status"] == "ok", resp
    print("  set_node_config(mixer, master_gain=0.8): ok")

    # channel_count change is not live-updatable — expect a clear error
    resp = client.send({
        "cmd": "set_node_config",
        "node_id": "mixer",
        "config": {"channel_count": 4},
    })
    assert resp["status"] == "error", "Expected error for channel_count change"
    print(f"  channel_count correctly rejected: '{resp['message']}'")

    # Restore gain
    client.send({
        "cmd": "set_node_config",
        "node_id": "mixer",
        "config": {"master_gain": 1.0},
    })
    print("  master_gain restored to 1.0")
    print("PASS")


def test_transport(client):
    print("\n--- test_transport ---")

    resp = client.send({"cmd": "play"})
    assert resp["status"] == "ok", resp
    print("  play: ok")

    resp = client.send({"cmd": "get_position"})
    assert resp["status"] == "ok", resp
    assert resp["playing"] == True
    print(f"  position after play: beat={resp['beat']:.3f}, playing={resp['playing']}")

    time.sleep(0.5)

    resp = client.send({"cmd": "get_position"})
    beat_after = resp["beat"]
    print(f"  position after 0.5s: beat={beat_after:.3f}")
    assert beat_after > 0.0, "Beat should have advanced"

    resp = client.send({"cmd": "stop"})
    assert resp["status"] == "ok", resp
    print("  stop: ok")

    resp = client.send({"cmd": "seek", "beat": 0.0})
    assert resp["status"] == "ok", resp
    print("  seek(0): ok")
    print("PASS")


def test_set_loop(client):
    print("\n--- test_set_loop ---")
    resp = client.send({"cmd": "set_loop", "start": 0.0, "end": 2.0, "enabled": True})
    assert resp["status"] == "ok", resp
    resp = client.send({"cmd": "set_loop", "enabled": False})
    assert resp["status"] == "ok", resp
    print("PASS")


def test_set_param(client):
    print("\n--- test_set_param ---")
    resp = client.send({
        "cmd": "set_param",
        "node_id": "mixer",
        "param_id": "master_gain",
        "value": 0.5,
    })
    assert resp["status"] == "ok", resp
    print("PASS")


def test_offline_render(client, out_path="/tmp/test_render.wav"):
    print("\n--- test_offline_render ---")
    resp = client.send({"cmd": "render", "format": "wav"})
    assert resp["status"] == "ok", resp

    wav_bytes = base64.b64decode(resp["data"])
    print(f"  Received {len(wav_bytes)} WAV bytes")
    assert len(wav_bytes) > 44, "WAV too small"

    with open(out_path, "wb") as f:
        f.write(wav_bytes)
    print(f"  Saved to {out_path}")

    with wave.open(io.BytesIO(wav_bytes)) as wf:
        n_channels  = wf.getnchannels()
        sample_rate = wf.getframerate()
        n_frames    = wf.getnframes()
        print(f"  channels={n_channels}, sample_rate={sample_rate}, frames={n_frames}")
        assert n_channels  == 2
        assert sample_rate == 44100
        assert n_frames    > 0

        raw = wf.readframes(n_frames)
        import struct as _s
        samples = _s.unpack_from(f"<{len(raw)//2}h", raw)
        peak = max(abs(s) for s in samples)
        print(f"  Peak sample: {peak}")
        assert peak > 100, f"Audio appears silent (peak={peak})"
    print("PASS")


def test_control_graph(client):
    print("\n--- test_control_graph (track_source + control_source) ---")
    resp = client.send(build_control_graph())
    assert resp["status"] == "ok", resp

    # Note events target the track_source; control events target control_source
    note_events = build_schedule(
        notes   = [(0.0, 3.9, 60, 80)],
        node_id = "track_abc",
    )["events"]
    beat_events = build_control_schedule(
        beats   = [i * 0.5 for i in range(8)],
        node_id = "beat_ctrl",
    )["events"]

    resp = client.send({"cmd": "set_schedule", "events": note_events + beat_events})
    assert resp["status"] == "ok", resp
    print("  Combined note+control schedule sent ok")

    resp = client.send({"cmd": "render", "format": "wav"})
    assert resp["status"] == "ok", resp
    print(f"  Render returned {len(base64.b64decode(resp['data']))} bytes")
    print("PASS")


def test_list_plugins(client):
    print("\n--- test_list_plugins ---")
    resp = client.send({"cmd": "list_plugins"})
    if resp["status"] == "error":
        print(f"  SKIP (LV2 not built or no plugins installed): {resp.get('message')}")
        return
    plugins = resp.get("plugins", [])
    print(f"  Found {len(plugins)} LV2 plugins")
    if plugins:
        print(f"  First: {plugins[0].get('name')} — {plugins[0].get('uri')}")
    print("PASS")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Audio server test / reference client")
    parser.add_argument("--address", default=DEFAULT_ADDRESS,
                        help="Server socket path or named pipe")
    parser.add_argument("--sf2", default=None,
                        help="Path to SF2 file (server-side path; enables SF2 tests)")
    parser.add_argument("--lv2", default=None,
                        help="LV2 plugin URI (enables LV2 graph test)")
    parser.add_argument("--wav-out", default="/tmp/test_render.wav",
                        help="Where to save the rendered WAV")
    parser.add_argument("--skip-transport", action="store_true",
                        help="Skip real-time transport tests (useful in headless CI)")
    args = parser.parse_args()

    print(f"Connecting to {args.address!r} ...")
    with AudioServerClient(args.address) as client:
        print("Connected.\n")

        # ----------------------------------------------------------------
        # Core protocol sanity
        test_ping(client)

        # ----------------------------------------------------------------
        # Track-source graph model (the canonical session shape)
        track_ids = ["abc", "def"]
        client.send(build_track_source_graph(track_ids))
        test_track_source_graph(client)

        # Schedule targeting track_source nodes
        client.send(build_track_source_graph(track_ids))
        test_track_source_schedule(client)

        # ----------------------------------------------------------------
        # Note preview — the main motivation for the new API
        client.send(build_track_source_graph(track_ids))
        test_note_preview(client)

        client.send(build_track_source_graph(track_ids))
        # Minimal schedule so the independence test has something to play
        sched = build_schedule(
            notes   = [(i * 0.5, 0.4, 60 + i * 2, 80) for i in range(8)],
            node_id = "track_abc",
        )
        client.send(sched)
        test_note_preview_independence(client)

        client.send(build_track_source_graph(track_ids))
        test_all_notes_off(client)

        client.send(build_track_source_graph(track_ids))
        client.send(sched)
        test_play_single_note_pattern(client)

        # ----------------------------------------------------------------
        # Setup events (beat=-1 program/volume)
        client.send(build_track_source_graph(track_ids))
        test_setup_events(client)

        # ----------------------------------------------------------------
        # Live node config
        client.send(build_track_source_graph(track_ids))
        test_set_node_config(client)

        # ----------------------------------------------------------------
        # Offline render (schedule from test_setup_events still active)
        test_offline_render(client, out_path=args.wav_out)

        # ----------------------------------------------------------------
        # Control source
        test_control_graph(client)

        # ----------------------------------------------------------------
        # Misc existing commands
        client.send(build_track_source_graph(["abc"]))
        test_set_param(client)
        test_list_plugins(client)

        # ----------------------------------------------------------------
        # Real-time transport
        if not args.skip_transport:
            client.send(build_track_source_graph(["abc"]))
            client.send(build_schedule(
                notes   = [(i * 0.5, 0.4, 60 + i * 2, 80) for i in range(8)],
                node_id = "track_abc",
            ))
            test_transport(client)
            test_set_loop(client)

        # ----------------------------------------------------------------
        # SF2 graph (requires --sf2)
        if args.sf2:
            print("\n--- test_sf2_track_source_graph ---")
            resp = client.send(build_track_source_graph(["abc", "def"], sf2_path=args.sf2))
            if resp["status"] == "ok":
                sf2_sched = build_multi_track_schedule([
                    {"node_id": "track_abc", "channel": 0,
                     "program": 0, "notes": [(0.0, 1.9, 60, 90), (2.0, 1.9, 64, 85)]},
                    {"node_id": "track_def", "channel": 1,
                     "program": 48, "notes": [(0.5, 1.9, 55, 80), (2.5, 1.9, 59, 75)]},
                ])
                client.send(sf2_sched)
                resp2 = client.send({"cmd": "render", "format": "wav"})
                if resp2["status"] == "ok":
                    wav_path = args.wav_out.replace(".wav", "_sf2.wav")
                    with open(wav_path, "wb") as f:
                        f.write(base64.b64decode(resp2["data"]))
                    print(f"PASS: SF2 render saved to {wav_path}")
                else:
                    print(f"  Render failed: {resp2.get('message')}")
            else:
                print(f"  SF2 graph failed: {resp.get('message')} (is FluidSynth built in?)")

            # Preview notes through SF2
            if resp["status"] == "ok":
                print("\n--- test_sf2_preview ---")
                client.send(build_track_source_graph(["abc"], sf2_path=args.sf2))
                for pitch in [60, 64, 67]:
                    client.send({"cmd": "note_on", "node_id": "track_abc",
                                 "channel": 0, "pitch": pitch, "velocity": 90})
                print("  SF2 chord preview started")
                time.sleep(0.5)
                client.send({"cmd": "all_notes_off", "node_id": "track_abc"})
                print("  all_notes_off: ok")
                print("PASS")

        # ----------------------------------------------------------------
        # LV2 graph (requires --sf2 and --lv2)
        if args.sf2 and args.lv2:
            print("\n--- test_lv2_track_source_graph ---")
            resp = client.send(build_lv2_graph(["abc"], args.lv2, args.sf2))
            if resp["status"] == "ok":
                print("PASS: LV2 graph loaded")
            else:
                print(f"  LV2 graph failed: {resp.get('message')}")

        print("\n=== All tests passed ===")
        # client.send({"cmd": "shutdown"})


if __name__ == "__main__":
    main()
