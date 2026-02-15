"""C++ audio server backend.

Implements the same external API as AudioEngine (play, stop, seek, set_loop,
mark_dirty, current_beat, is_playing, load_sf2, play_single_note,
render_offline_wav) but delegates all audio work to an external audio_server
process via length-prefixed JSON IPC.

Graph model:
  Each sequencer track maps to one track_source node ("track_<id>").
  Source nodes emit event streams; processor nodes (fluidsynth, sine, lv2,
  mixer) consume them.  In the default graph all sources fan into one shared
  synth node.  This wiring is transparent to the schedule and preview APIs —
  both target source node IDs.

Note preview:
  play_single_note() uses the server's note_on / note_off commands, which
  inject directly into a source node's preview stream.  They are completely
  independent of the transport and set_schedule — arrangement playback and
  preview notes coexist without interference.

IPC wire format (mirrors protocol.h / ipc.h in the C++ server):
  4-byte LE uint32 length prefix, then UTF-8 JSON.  Same framing on replies.
"""

from __future__ import annotations

import base64
import json
import platform
import socket
import struct
import threading
import time
from typing import Optional

from .engine import (
    _emit_bend_events, SchedEvent,
)


# ---------------------------------------------------------------------------
# Platform defaults
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"

DEFAULT_ADDRESS = r"\\.\pipe\AudioServer" if IS_WINDOWS else "/tmp/audio_server.sock"


# ---------------------------------------------------------------------------
# Low-level IPC client
# ---------------------------------------------------------------------------

class _IpcClient:
    """Length-prefixed JSON IPC connection to the audio server.

    One instance = one persistent connection.  Not thread-safe on its own;
    ServerEngine serialises all calls through _send().
    """

    def __init__(self, address: str):
        self.address = address
        self._sock = None
        self._pipe = None  # Windows named pipe handle

    def connect(self, timeout: float = 5.0) -> None:
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
            f"audio_server not reachable at {self.address!r} after {timeout:.1f}s: {last_err}"
        )

    def _connect_unix(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.address)
        self._sock = s

    def _connect_windows(self):
        import ctypes, ctypes.wintypes
        GENERIC_RW = 0xC0000000
        OPEN_EXISTING = 3
        h = ctypes.windll.kernel32.CreateFileW(
            self.address, GENERIC_RW, 0, None, OPEN_EXISTING, 0, None
        )
        if h == ctypes.wintypes.HANDLE(-1).value:
            raise ConnectionRefusedError(f"Named pipe not available: {self.address}")
        self._pipe = h

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if IS_WINDOWS and self._pipe:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._pipe)
            self._pipe = None

    @property
    def connected(self) -> bool:
        return self._sock is not None or (IS_WINDOWS and self._pipe is not None)

    def send(self, request: dict) -> dict:
        payload = json.dumps(request).encode("utf-8")
        self._write(struct.pack("<I", len(payload)) + payload)
        resp_len = struct.unpack("<I", self._read(4))[0]
        return json.loads(self._read(resp_len))

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
            ctypes.windll.kernel32.ReadFile(self._pipe, buf, n, ctypes.byref(got), None)
            return bytes(buf)
        chunks, remaining = [], n
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise EOFError("audio_server disconnected")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_graph(state, sf2_path: Optional[str]) -> dict:
    """Build the default track_source graph for the current session state.

    All melodic tracks and beat tracks each get a track_source node.
    All sources fan into one shared synth (fluidsynth if sf2_path, else sine),
    which feeds the mixer.
    """
    nodes = []
    connections = []

    all_track_ids = (
        [t.id for t in state.tracks] +
        [bt.id for bt in state.beat_tracks]
    )

    for tid in all_track_ids:
        nodes.append({"id": f"track_{tid}", "type": "track_source"})
        connections.append({
            "from_node": f"track_{tid}", "from_port": "events_out",
            "to_node":   "synth",        "to_port":   "events_in",
        })

    synth_node = {"id": "synth", "type": "fluidsynth" if sf2_path else "sine"}
    if sf2_path:
        synth_node["sf2_path"] = sf2_path
    nodes.append(synth_node)
    nodes.append({"id": "mixer", "type": "mixer", "channel_count": 1})

    connections += [
        {"from_node": "synth", "from_port": "audio_out_L",
         "to_node":   "mixer", "to_port":   "audio_in_L_0"},
        {"from_node": "synth", "from_port": "audio_out_R",
         "to_node":   "mixer", "to_port":   "audio_in_R_0"},
    ]

    return {"cmd": "set_graph", "bpm": state.bpm, "nodes": nodes, "connections": connections}


# ---------------------------------------------------------------------------
# Schedule builder  (AppState  →  server set_schedule payload)
# ---------------------------------------------------------------------------
#
# We build the event list directly from AppState rather than converting the
# SchedEvent list from build_schedule(), because SchedEvent has no track_id
# field — the channel mapping is already collapsed by the time we get it back.
#
# Setup events (program, volume) are emitted with beat=-1.  The server clamps
# these to fire before any note-ons, matching build_schedule() semantics.
#
# Bend auto-routing (the _BEND_POOL logic in engine.py) is not replicated here.
# The server's fluidsynth node handles polyphony internally; bend curves are
# emitted as "control" events on the same (node_id, channel) as the note.

def _build_server_schedule(state) -> list[dict]:
    """Convert AppState to a flat list of server event dicts."""
    events = []

    # --- Melodic tracks ---
    for pl in state.placements:
        t   = state.find_track(pl.track_id)
        pat = state.find_pattern(pl.pattern_id)
        if not t or not pat:
            continue

        node_id = f"track_{t.id}"
        ch = t.channel & 0x0F

        # Setup events: program and volume fire before any note-ons (beat=-1)
        events.append({
            "beat": -1, "type": "program",
            "node_id": node_id, "channel": ch,
            "pitch": t.program, "velocity": t.bank, "value": 0.0,
        })
        events.append({
            "beat": -1, "type": "volume",
            "node_id": node_id, "channel": ch,
            "pitch": t.volume, "velocity": 0, "value": 0.0,
        })

        transpose = state.compute_transpose(pl)
        reps = pl.repeats or 1
        for rep in range(reps):
            offset = pl.time + rep * pat.length
            for n in pat.notes:
                p = max(0, min(127, n.pitch + transpose))
                v = max(1, min(127, n.velocity))
                on_beat  = offset + n.start
                off_beat = on_beat + n.duration

                events.append({
                    "beat": on_beat, "type": "note_on",
                    "node_id": node_id, "channel": ch,
                    "pitch": p, "velocity": v, "value": 0.0,
                })
                events.append({
                    "beat": off_beat, "type": "note_off",
                    "node_id": node_id, "channel": ch,
                    "pitch": p, "velocity": 0, "value": 0.0,
                })

                if n.bend:
                    # Collect bend SchedEvents from engine helper, then convert.
                    bend_sched: list[SchedEvent] = []
                    _emit_bend_events(bend_sched, ch, on_beat, n.duration, n.bend)
                    for be in bend_sched:
                        norm = (be.pitch - 8192) / 8191.0
                        events.append({
                            "beat": be.beat, "type": "control",
                            "node_id": node_id, "channel": ch,
                            "pitch": 0, "velocity": 0, "value": norm,
                        })

    # --- Beat tracks ---
    for bp in state.beat_placements:
        bt   = state.find_beat_track(bp.track_id)
        bpat = state.find_beat_pattern(bp.pattern_id)
        if not bt or not bpat:
            continue

        node_id = f"track_{bt.id}"
        reps = bp.repeats or 1

        for inst in state.beat_kit:
            grid = bpat.grid.get(inst.id)
            if not grid:
                continue
            ch = inst.channel & 0x0F

            # GM convention: channel 9 drum kits live at bank 128 in most SF2
            # files, matching FluidSynthInstrument.set_program's remap logic.
            prog_bank = 128 if (ch == 9 and inst.bank == 0) else inst.bank
            events.append({
                "beat": -1, "type": "program",
                "node_id": node_id, "channel": ch,
                "pitch": inst.program, "velocity": prog_bank, "value": 0.0,
            })

            step_dur = bpat.length / len(grid)
            for rep in range(reps):
                offset = bp.time + rep * bpat.length
                for step_idx, vel in enumerate(grid):
                    if vel > 0:
                        on_beat  = offset + step_idx * step_dur
                        off_beat = on_beat + step_dur * 0.8
                        events.append({
                            "beat": on_beat, "type": "note_on",
                            "node_id": node_id, "channel": ch,
                            "pitch": inst.pitch, "velocity": vel, "value": 0.0,
                        })
                        events.append({
                            "beat": off_beat, "type": "note_off",
                            "node_id": node_id, "channel": ch,
                            "pitch": inst.pitch, "velocity": 0, "value": 0.0,
                        })

    return events


# ---------------------------------------------------------------------------
# ServerEngine  —  drop-in replacement for AudioEngine
# ---------------------------------------------------------------------------

class ServerEngine:
    """Audio engine that delegates to the C++ audio_server process.

    External API mirrors AudioEngine so app.py needs no changes beyond the
    constructor call.

    Thread safety: all IPC calls go through _send(), which holds _lock.
    _current_beat and _is_playing are written by a background poll thread and
    read by app.py's QTimer, matching AudioEngine's threading model.
    """

    def __init__(self, state, settings=None, address: str = DEFAULT_ADDRESS):
        self.state = state
        self.address = address

        from .settings import Settings
        self.settings = settings or Settings()

        self._sf2_path: Optional[str] = None
        self._graph_loaded: bool = False
        self._graph_track_ids: frozenset = frozenset()  # track IDs in last-built graph

        self._lock = threading.Lock()
        self._client: Optional[_IpcClient] = None

        # Written by poll thread, read by app.py QTimer
        self._current_beat: float = 0.0
        self._is_playing: bool = False

        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()

        self._connect()
        # Fetch plugin descriptors after connection is established
        if self._client is not None:
            self._fetch_plugin_descriptors()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> bool:
        try:
            client = _IpcClient(self.address)
            client.connect(timeout=2.0)
            self._client = client
            print(f"[ServerEngine] Connected to audio_server at {self.address!r}")
            return True
        except Exception as e:
            print(f"[ServerEngine] Could not connect: {e}")
            self._client = None
            return False

    def _fetch_plugin_descriptors(self) -> None:
        """Fetch registered plugin descriptors and cache them in graph_model.

        Must be called outside _lock (it calls _send which acquires _lock).
        """
        try:
            resp = self._send({"cmd": "list_registered_plugins"})
            if resp and resp.get("status") == "ok":
                plugins = resp.get("plugins", [])
                from ..graph_editor.graph_model import set_plugin_descriptors
                set_plugin_descriptors(plugins)
                print(f"[ServerEngine] Cached {len(plugins)} plugin descriptors")
        except Exception as e:
            print(f"[ServerEngine] Failed to fetch plugin descriptors: {e}")
            import traceback
            traceback.print_exc()

    def _send(self, request: dict) -> Optional[dict]:
        """Send a command and return the response, reconnecting once on failure."""
        with self._lock:
            for attempt in range(2):
                if self._client is None or not self._client.connected:
                    if not self._connect():
                        return None
                try:
                    resp = self._client.send(request)
                    if resp.get("status") != "ok":
                        print(f"[ServerEngine] Server error: {resp.get('message', resp)}")
                    return resp
                except Exception as e:
                    print(f"[ServerEngine] IPC error (attempt {attempt+1}): {e}")
                    try:
                        self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None
            return None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.connected

    # ------------------------------------------------------------------
    # Low-latency parameter updates
    # ------------------------------------------------------------------

    def set_param(self, node_id: str, param_id: str, value: float) -> None:
        """Send a set_param command for immediate audio-thread update.

        This bypasses the full graph rebuild path — the audio engine queues
        the value change and applies it at the start of the next block.
        """
        self._send({
            "cmd": "set_param",
            "node_id": node_id,
            "param_id": param_id,
            "value": value,
        })

    def get_node_data(self, node_id: str, port_id: str = "history") -> list:
        """Retrieve graph/monitor data from a plugin node.

        Returns a Python list (parsed from the JSON the plugin returns), or
        an empty list if the node is not found / not connected.
        """
        import json as _json
        resp = self._send({
            "cmd": "get_node_data",
            "node_id": node_id,
            "port_id": port_id,
        })
        if not resp or resp.get("status") != "ok":
            return []
        try:
            return _json.loads(resp.get("data", "[]"))
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Graph / soundfont
    # ------------------------------------------------------------------

    def _current_track_ids(self) -> frozenset:
        return frozenset(
            [t.id for t in self.state.tracks] +
            [bt.id for bt in self.state.beat_tracks]
        )

    def _graph_payload(self) -> dict:
        """Build the set_graph payload, using the custom graph model if present."""
        if self.state.signal_graph is not None:
            return self.state.signal_graph.to_server_dict(bpm=self.state.bpm)
        # Fall back to the auto-generated default
        return _build_graph(self.state, self._sf2_path)

    def load_sf2(self, sf2_path: str) -> bool:
        """Rebuild the graph with a fluidsynth node.  Returns True on success."""
        self._sf2_path = sf2_path
        # Update the default synth's sf2_path in the custom graph if one exists
        if self.state.signal_graph is not None:
            for node in self.state.signal_graph.nodes:
                if node.node_type == "fluidsynth" and node.is_default_synth:
                    node.params["sf2_path"] = sf2_path
        resp = self._send(self._graph_payload())
        ok = resp is not None and resp.get("status") == "ok"
        if ok:
            self._graph_loaded = True
            self._graph_track_ids = self._current_track_ids()
        else:
            self._ensure_graph()
        return ok

    def _ensure_graph(self):
        """Make sure the server has a current graph loaded.

        Rebuilds if no graph has been loaded yet, or if the track set has
        changed since the last build (new track added, track deleted).
        When a custom graph model is active, syncs its track_source nodes
        to match the current tracks before sending.
        """
        current = self._current_track_ids()
        if not self._graph_loaded or current != self._graph_track_ids:
            if self.state.signal_graph is not None:
                self.state.signal_graph.sync_track_sources(self.state, self._sf2_path)
            resp = self._send(self._graph_payload())
            if resp and resp.get("status") == "ok":
                self._graph_loaded = True
                self._graph_track_ids = current

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def mark_dirty(self):
        """Rebuild the graph (in case tracks changed) and push a fresh schedule."""
        if self.state.signal_graph is not None:
            self.state.signal_graph.sync_track_sources(self.state, self._sf2_path)
        self._send(self._graph_payload())
        self._graph_loaded = True
        self._graph_track_ids = self._current_track_ids()
        self._send({"cmd": "set_bpm", "bpm": self.state.bpm})
        self._send({"cmd": "set_schedule", "events": _build_server_schedule(self.state)})

    def play(self):
        self.mark_dirty()
        self._send({"cmd": "play"})
        self._start_poll()

    def stop(self):
        self._send({"cmd": "stop"})
        self._is_playing = False
        self._stop_poll()

    def seek(self, beat: float):
        self._send({"cmd": "seek", "beat": beat})
        self._current_beat = beat

    def set_loop(self, start: Optional[float], end: Optional[float]):
        if start is not None and end is not None:
            self._send({"cmd": "set_loop", "start": start, "end": end, "enabled": True})
        else:
            self._send({"cmd": "set_loop", "enabled": False})

    @property
    def current_beat(self) -> float:
        return self._current_beat

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    # ------------------------------------------------------------------
    # Note preview
    # ------------------------------------------------------------------

    def play_single_note(self, pitch: int, velocity: int = 100,
                         channel: int = 0, duration: float = 0.5,
                         track_id=None):
        """Preview a single note via the server's note_on / note_off commands.

        Targets the track_source node for track_id if given, otherwise
        reverse-maps channel to a source node.  Does NOT interact with
        set_schedule, play, or stop — safe during arrangement playback and
        during rapid repeated calls.
        """
        self._ensure_graph()
        node_id = self._source_node_for(track_id, channel)
        self._send({"cmd": "note_on", "node_id": node_id,
                    "channel": channel, "pitch": pitch, "velocity": velocity})
        def _off():
            time.sleep(duration)
            self._send({"cmd": "note_off", "node_id": node_id,
                        "channel": channel, "pitch": pitch})
        threading.Thread(target=_off, daemon=True).start()

    def all_notes_off(self, track_id=None):
        """Silence all active preview notes on a source node (or all nodes)."""
        if track_id is not None:
            self._send({"cmd": "all_notes_off", "node_id": f"track_{track_id}"})
        else:
            self._send({"cmd": "all_notes_off"})

    def set_channel_program(self, channel: int, bank: int, program: int):
        """Public API for ops/playback — no-op on the server path.

        Program state is carried by beat=-1 setup events in the schedule,
        so there is no need to set it out-of-band here.
        """
        pass

    def _source_node_for(self, track_id, channel: int) -> str:
        """Return the track_source node ID to use for a preview note."""
        if track_id is not None:
            return f"track_{track_id}"
        for t in self.state.tracks:
            if (t.channel & 0x0F) == channel:
                return f"track_{t.id}"
        for bt in self.state.beat_tracks:
            return f"track_{bt.id}"
        return "track_default"  # empty session; server will error gracefully

    # ------------------------------------------------------------------
    # Offline render
    # ------------------------------------------------------------------

    def render_offline_wav(self) -> Optional[bytes]:
        """Ask the server to render the current schedule and return WAV bytes."""
        self.mark_dirty()
        resp = self._send({"cmd": "render", "format": "wav"})
        if resp is None or resp.get("status") != "ok":
            return None
        try:
            return base64.b64decode(resp["data"])
        except Exception as e:
            print(f"[ServerEngine] render decode error: {e}")
            return None

    # ------------------------------------------------------------------
    # Position polling thread
    # ------------------------------------------------------------------

    def _start_poll(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _stop_poll(self):
        self._poll_stop.set()

    def _poll_loop(self):
        """Poll server position at ~30fps.  Exits when server reports not playing."""
        while not self._poll_stop.is_set():
            resp = self._send({"cmd": "get_position"})
            if resp:
                self._current_beat = resp.get("beat", self._current_beat)
                self._is_playing   = resp.get("playing", False)
                if not self._is_playing:
                    break
            time.sleep(0.033)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        self._stop_poll()
        if self._poll_thread:
            self._poll_thread.join(timeout=1.0)
        self.all_notes_off()
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def ensure_instrument(self):
        """No-op — instrument is owned by the server process."""
        self._ensure_graph()
