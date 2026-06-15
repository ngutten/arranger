"""In-process audio engine via pybind11 bindings.

Drop-in replacement for ServerEngine. Identical external API so app.py
needs no changes beyond backend selection.

The _send() method accepts the same command dicts that ServerEngine sends
over IPC and routes them through ServerHandler::handle() in-process.
JSON serialisation still occurs (memory-to-memory), which is negligible
for the payload sizes involved and keeps a single dispatch table in C++.

The position poll thread is gone: current_beat and is_playing are direct
reads via handle("get_position"), called from the existing QTimer path in
app.py rather than a background thread.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional, List

from pathlib import Path

from ..arranger_engine import AudioServer, AudioEngineConfig, load_plugin_library
from .settings import Settings
from .engine import _emit_note_tune_events, SchedEvent, EVT_NOTE_TUNE

# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_schedule_from_arr(arr: dict, fallback_node_id: str) -> List[dict]:
    """Convert an arrangement dict (from build_pattern_preview) to server events.

    Mirrors _build_server_schedule but operates on the arrangement dict format
    rather than AppState, so it can be used for preview renders without touching
    live state.  Each track may carry a ``node_id`` key that overrides the
    *fallback_node_id* so that preview events target the correct source node
    in the user's real signal graph.
    """
    events: List[dict] = []
    for track in arr.get("tracks", []):
        node_id = track.get("node_id", fallback_node_id)
        ch = track.get("channel", 0) & 0x0F
        # GM convention: channel 9 drum kits live at bank 128 in most SF2
        # files, matching FluidSynthInstrument.set_program's remap logic.
        bank = track.get("bank", 0)
        prog_bank = 128 if (ch == 9 and bank == 0) else bank
        events.append({
            "beat": -1, "type": "program", "node_id": node_id,
            "channel": ch, "pitch": track.get("program", 0),
            "velocity": prog_bank, "value": 0.0,
        })
        events.append({
            "beat": -1, "type": "volume", "node_id": node_id,
            "channel": ch, "pitch": track.get("volume", 100),
            "velocity": 0, "value": 0.0,
        })

        for pl in track.get("placements", []):
            pat        = pl.get("pattern", {})
            offset     = pl.get("time", 0)
            transpose  = pl.get("transpose", 0)
            reps       = pl.get("repeats", 1) or 1
            pat_length = pat.get("length", 0)

            for rep in range(reps):
                rep_offset = offset + rep * pat_length
                for n in pat.get("notes", []):
                    p        = max(0, min(127, n["pitch"] + transpose))
                    v        = max(1, min(127, n["velocity"]))
                    on_beat  = rep_offset + n["start"]
                    off_beat = on_beat + n["duration"]

                    events.append({
                        "beat": on_beat, "type": "note_on", "node_id": node_id,
                        "channel": ch, "pitch": p, "velocity": v, "value": 0.0,
                    })
                    events.append({
                        "beat": off_beat, "type": "note_off", "node_id": node_id,
                        "channel": ch, "pitch": p, "velocity": 0, "value": 0.0,
                    })

                    for attr_id, attr_val in (n.get("attrs") or {}).items():
                        events.append({
                            "beat": on_beat, "type": "note_attr", "node_id": node_id,
                            "channel": ch, "pitch": p, "velocity": 0,
                            "value": float(attr_val), "port_id": attr_id,
                        })

                    bend = n.get("bend")
                    if bend:
                        tune_sched: list[SchedEvent] = []
                        _emit_note_tune_events(tune_sched, ch, p, on_beat,
                                               n["duration"], bend)
                        for te in tune_sched:
                            events.append({
                                "beat": te.beat, "type": "note_tune",
                                "node_id": node_id, "channel": ch,
                                "pitch": p, "velocity": 0, "value": te.value,
                            })
    return events


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

    # Per-instrument source nodes for split-routed beat instruments
    for inst in state.beat_kit:
        if inst.split_routing:
            nid = f"beat_inst_{inst.id}"
            nodes.append({"id": nid, "type": "track_source"})
            connections.append({
                "from_node": nid, "from_port": "events_out",
                "to_node":   "synth", "to_port": "events_in",
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
# Bend curves are emitted as "note_tune" events on the same (node_id, channel)
# as the note.  The FluidSynth plugin applies them per-note via
# fluid_synth_tune_notes, so no channel pool is needed.

def _build_server_schedule(state) -> list[dict]:
    """Convert AppState to a flat list of server event dicts."""
    events = []

    # --- Master section setup (terminal "mixer" node) ---
    # Emitted as beat=-1 control events so they fire before any note-on and
    # apply identically in live playback and offline export (same dispatcher
    # path as per-track volume).  master_gain is linear makeup; the ceiling is
    # stored in dBFS and converted to the limiter's linear threshold.
    ceiling_db = float(getattr(state, 'master_ceiling_db', -1.0))
    for port_id, value in (
        ("master_gain",      float(getattr(state, 'master_gain', 1.0))),
        ("limiter_enabled",  1.0 if getattr(state, 'master_limiter', True) else 0.0),
        ("limiter_threshold", 10.0 ** (ceiling_db / 20.0)),
    ):
        events.append({
            "beat": -1, "type": "control", "node_id": "mixer",
            "port_id": port_id, "channel": 0, "pitch": 0,
            "velocity": 0, "value": value,
        })

    from ..ops.variations import resolve_placement_notes

    # --- Melodic tracks ---
    # Solo overrides mute: if any track (melodic OR beat) is soloed, only
    # soloed tracks sound. Audibility is shared across both track types.
    any_solo = (any(getattr(t, 'solo', False) for t in state.tracks) or
                any(getattr(bt, 'solo', False) for bt in state.beat_tracks))
    for pl in state.placements:
        t = state.find_track(pl.track_id)
        if not t:
            continue
        notes, pat_length, pat_key, pat_scale = resolve_placement_notes(state, pl)
        if notes is None:
            continue

        node_id = f"track_{t.id}"
        ch = t.channel & 0x0F

        # Setup events: program, volume, pan fire before any note-ons (beat=-1).
        # These are emitted even for muted tracks so live un-muting is instant.
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
        pan_cc = max(0, min(127, int(round((getattr(t, 'pan', 0.0) + 1.0) * 0.5 * 127))))
        events.append({
            "beat": -1, "type": "pan",
            "node_id": node_id, "channel": ch,
            "pitch": pan_cc, "velocity": 0, "value": 0.0,
        })

        # Mute / solo: skip note events for inaudible tracks (export-correct).
        audible = (not getattr(t, 'mute', False)) and \
                  (getattr(t, 'solo', False) or not any_solo)
        if not audible:
            continue

        transpose = state.compute_transpose(pl)
        reps = pl.repeats or 1
        for rep in range(reps):
            offset = pl.time + rep * pat_length
            for n in notes:
                p = max(0, min(127, n.pitch + transpose))
                v = max(1, min(127, n.velocity))
                on_beat  = offset + n.start
                off_beat = on_beat + n.duration

                note_on_evt = {
                    "beat": on_beat, "type": "note_on",
                    "node_id": node_id, "channel": ch,
                    "pitch": p, "velocity": v, "value": 0.0,
                }
                if n.lyric:
                    note_on_evt["lyric"] = n.lyric
                events.append(note_on_evt)
                events.append({
                    "beat": off_beat, "type": "note_off",
                    "node_id": node_id, "channel": ch,
                    "pitch": p, "velocity": 0, "value": 0.0,
                })

                # Per-note attributes — onset-latched; the scheduler sorts these
                # ahead of the note_on at the same beat so the synth latches them
                # before triggering the voice. port_id carries the attr id.
                for attr_id, attr_val in (n.attrs or {}).items():
                    events.append({
                        "beat": on_beat, "type": "note_attr",
                        "node_id": node_id, "channel": ch,
                        "pitch": p, "velocity": 0, "value": float(attr_val),
                        "port_id": attr_id,
                    })

                if n.bend:
                    # Emit per-note tune events — FluidSynth applies them
                    # per-note via fluid_synth_tune_notes (no channel pool needed).
                    tune_sched: list[SchedEvent] = []
                    _emit_note_tune_events(tune_sched, ch, p, on_beat, n.duration, n.bend)
                    for te in tune_sched:
                        events.append({
                            "beat": te.beat, "type": "note_tune",
                            "node_id": node_id, "channel": ch,
                            "pitch": p, "velocity": 0, "value": te.value,
                        })

    # --- Beat tracks ---
    for bp in state.beat_placements:
        bt   = state.find_beat_track(bp.track_id)
        bpat = state.find_beat_pattern(bp.pattern_id)
        if not bt or not bpat:
            continue

        # Mute/solo: a beat track spans multiple (often shared) instrument
        # channels, so volume is a per-hit velocity scale rather than CC7.
        bt_audible = (not getattr(bt, 'mute', False)) and \
                     (getattr(bt, 'solo', False) or not any_solo)
        if not bt_audible:
            continue
        bt_vol = getattr(bt, 'volume', 100) / 100.0

        reps = bp.repeats or 1

        for inst in state.beat_kit:
            grid = bpat.grid.get(inst.id)
            if not grid:
                continue
            ch = inst.channel & 0x0F
            node_id = f"beat_inst_{inst.id}" if inst.split_routing else f"track_{bt.id}"

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
                        scaled_vel = max(1, min(127, int(round(vel * bt_vol))))
                        events.append({
                            "beat": on_beat, "type": "note_on",
                            "node_id": node_id, "channel": ch,
                            "pitch": inst.pitch, "velocity": scaled_vel, "value": 0.0,
                        })
                        events.append({
                            "beat": off_beat, "type": "note_off",
                            "node_id": node_id, "channel": ch,
                            "pitch": inst.pitch, "velocity": 0, "value": 0.0,
                        })

    # --- Output-mixer gain automation ---
    # Automation tracks with target "output_gain:<N>" drive the output
    # mixer's gain_N parameter directly, bypassing the control_source
    # routing. Events carry a port_id so the C++ dispatcher delivers them
    # via set_param(port_id, value) on the "mixer" node.
    from ..core.curve_utils import interpolate_curve as _interp
    for auto_track in state.automation_tracks:
        target = (auto_track.target or '')
        if not target.startswith('output_gain:'):
            continue
        try:
            channel = int(target.split(':', 1)[1])
        except (ValueError, IndexError):
            continue
        port_id = f'gain_{channel}'
        for ap in state.automation_placements:
            if ap.track_id != auto_track.id:
                continue
            pattern = state.find_automation_pattern(ap.pattern_id)
            if not pattern or not pattern.points:
                continue
            curve_points = [(p.time, p.value, p.curve) for p in pattern.points]
            repeats = ap.repeats or 1
            for rep in range(repeats):
                offset = ap.time + rep * pattern.length
                num_samples = max(16, int(pattern.length * 16))
                for i in range(num_samples + 1):
                    t = (i / num_samples) * pattern.length if num_samples > 0 else 0.0
                    norm = _interp(curve_points, t, pattern.length, 0.0)
                    norm = max(0.0, min(1.0, norm))
                    scaled = pattern.min_value + norm * (pattern.max_value - pattern.min_value)
                    events.append({
                        "beat": offset + t, "type": "control",
                        "node_id": "mixer", "port_id": port_id,
                        "channel": 0, "pitch": 0, "velocity": 0,
                        "value": scaled,
                    })

    # --- Automation tracks ---
    # Iterate ControlSource nodes in the signal graph to find which automation
    # tracks are being used, then generate control events from their placements.
    if state.signal_graph:
        from ..core.curve_utils import interpolate_curve
        
        for node in state.signal_graph.nodes:
            if node.node_type != 'control_source':
                continue
            
            # Get automation track ID from node params
            track_id = node.params.get('automation_track_id', 0)
            if track_id == 0:
                continue  # No track selected
            
            # Find the automation track
            auto_track = state.find_automation_track(track_id)
            if not auto_track:
                continue  # Track was deleted
            
            # Find all placements on this track
            for ap in state.automation_placements:
                if ap.track_id != track_id:
                    continue
                
                # Find the pattern
                pattern = state.find_automation_pattern(ap.pattern_id)
                if not pattern or not pattern.points:
                    continue
                
                # Generate control events from pattern
                repeats = ap.repeats or 1
                for rep in range(repeats):
                    offset = ap.time + rep * pattern.length
                    
                    # Convert points to tuples for interpolation
                    # Points are now stored normalized in [0, 1]
                    curve_points = [(p.time, p.value, p.curve) for p in pattern.points]
                    
                    # Dense sampling for smooth curves (16 samples per beat)
                    num_samples = max(16, int(pattern.length * 16))
                    for i in range(num_samples + 1):  # +1 to include endpoint
                        t = (i / num_samples) * pattern.length if num_samples > 0 else 0.0
                        
                        # Interpolate normalized value [0, 1]
                        # Default to 0.0 for normalized range
                        norm_value = interpolate_curve(curve_points, t, pattern.length, 0.0)
                        
                        # Clamp to [0, 1] just in case
                        norm_value = max(0.0, min(1.0, norm_value))
                        
                        # Scale to pattern's output range [min_value, max_value]
                        scaled_value = pattern.min_value + norm_value * (pattern.max_value - pattern.min_value)
                        
                        # Create control event with scaled value
                        events.append({
                            "beat": offset + t,
                            "type": "control",
                            "node_id": node.node_id,
                            "channel": 0,   # unused for control events
                            "pitch": 0,     # unused
                            "velocity": 0,  # unused
                            "value": scaled_value,  # scaled to [min_value, max_value]
                        })

    return events

def _build_tempo_map(state) -> list:
    """Build a tempo map from the tempo automation track.

    Returns a list of {"beat": float, "bpm": float} dicts for the C++ engine.
    Empty list if no tempo track exists.
    """
    tempo_track = state.find_tempo_track()
    if not tempo_track:
        return []

    from ..core.curve_utils import interpolate_curve

    # Collect all placements on the tempo track, sorted by time
    placements = sorted(
        [ap for ap in state.automation_placements if ap.track_id == tempo_track.id],
        key=lambda ap: ap.time
    )
    if not placements:
        return []

    tempo_points = []
    # Emit initial BPM at beat 0 (before first placement)
    tempo_points.append({"beat": 0.0, "bpm": float(state.bpm)})

    for ap in placements:
        pattern = state.find_automation_pattern(ap.pattern_id)
        if not pattern or not pattern.points:
            continue

        curve_points = [(p.time, p.value, p.curve) for p in pattern.points]
        repeats = ap.repeats or 1

        for rep in range(repeats):
            offset = ap.time + rep * pattern.length
            # Sample at 16 points per beat
            num_samples = max(16, int(pattern.length * 16))
            for i in range(num_samples + 1):
                t = (i / num_samples) * pattern.length if num_samples > 0 else 0.0
                norm_value = interpolate_curve(curve_points, t, pattern.length, 0.0)
                norm_value = max(0.0, min(1.0, norm_value))
                bpm_value = pattern.min_value + norm_value * (pattern.max_value - pattern.min_value)
                # Clamp BPM to reasonable range
                bpm_value = max(20.0, min(300.0, bpm_value))
                tempo_points.append({"beat": offset + t, "bpm": round(bpm_value, 2)})

    return tempo_points


# ---------------------------------------------------------------------------
# Dynamic plugin loader
# ---------------------------------------------------------------------------
# Loads arranger_plugin_*.so from the plugins/ directory adjacent to the
# project root (i.e. sibling of standalone/ and audio_server/).
# Called once at module import so all plugins are registered before any
# AudioServer is constructed.

def _promote_engine_symbols() -> None:
    # Re-open arranger_engine.so with RTLD_GLOBAL so its symbols (PluginRegistry,
    # PluginBuffers map methods, etc.) are visible to subsequently dlopen'd plugins.
    # Python imports extension modules with RTLD_LOCAL by default, which hides them.
    # RTLD_NOLOAD|RTLD_GLOBAL promotes an already-loaded library without reloading it.
    import ctypes
    RTLD_GLOBAL = getattr(ctypes, 'RTLD_GLOBAL', None)
    if RTLD_GLOBAL is None:
        return  # Windows — not needed, symbol visibility works differently
    RTLD_NOLOAD = 0x4  # Linux value; not exposed in ctypes constants
    import importlib.util
    spec = (importlib.util.find_spec("standalone.arranger_engine") or
            importlib.util.find_spec("arranger_engine"))
    if spec and spec.origin:
        ctypes.CDLL(spec.origin, RTLD_NOLOAD | RTLD_GLOBAL)


def _load_plugins_dir() -> None:
    plugins_dir = Path(__file__).resolve().parent.parent.parent / "plugins"
    if not plugins_dir.is_dir():
        return

    # Promote arranger_engine.so symbols to global table before loading plugins,
    # so PluginRegistry::add(), PluginBuffers::*Map::get() etc. resolve correctly.
    _promote_engine_symbols()

    patterns = ["arranger_plugin_*.so", "arranger_plugin_*.dll", "arranger_plugin_*.dylib"]
    for pattern in patterns:
        for path in sorted(plugins_dir.glob(pattern)):
            ok, plugin_id, error = load_plugin_library(str(path))
            if ok:
                print(f"[BindingEngine] loaded plugin: {plugin_id}")
            else:
                print(f"[BindingEngine] failed to load {path.name}: {error}")

_load_plugins_dir()


class BindingEngine:

    def __init__(self, state, settings: Optional[Settings] = None):
        self.state    = state
        self.settings = settings or Settings()

        cfg = AudioEngineConfig()
        cfg.sample_rate = self.settings.sample_rate
        cfg.block_size  = self.settings.block_size

        self._server = AudioServer(cfg)
        self._sf2_path: Optional[str] = None
        self._graph_loaded             = False
        self._graph_track_ids          = frozenset()
        self._last_graph_hash          = ""
        self._send_lock                = threading.Lock()

        # Cache playing state so is_playing doesn't need a round-trip every call.
        self._is_playing   = False
        self._current_beat = 0.0
        self._current_bpm  = 120.0
        self._master_meter = {"peak_l": 0.0, "peak_r": 0.0, "rms_l": 0.0,
                              "rms_r": 0.0, "gr": 1.0, "valid": False,
                              "channels": []}

        # Populate graph editor plugin descriptors
        resp = self._send({"cmd": "list_registered_plugins"})
        if resp and resp.get("status") == "ok":
            try:
                from ..graph_editor.graph_model import set_plugin_descriptors
                set_plugin_descriptors(resp.get("plugins", []))
            except ImportError:
                pass

    # ------------------------------------------------------------------
    # Core IPC-compatible dispatch
    # ------------------------------------------------------------------

    def _send(self, request: dict) -> Optional[dict]:
        with self._send_lock:
            try:
                return json.loads(self._server.handle(json.dumps(request)))
            except Exception as e:
                print(f"[BindingEngine] handle() error: {e}")
                return None

    # ------------------------------------------------------------------
    # Graph / soundfont  (mirrors ServerEngine exactly)
    # ------------------------------------------------------------------

    def _current_track_ids(self) -> frozenset:
        ids = ([t.id for t in self.state.tracks] +
               [bt.id for bt in self.state.beat_tracks])
        for inst in self.state.beat_kit:
            if inst.split_routing:
                ids.append(f"beat_inst_{inst.id}")
        return frozenset(ids)

    def _graph_payload(self) -> dict:
        if self.state.signal_graph is not None:
            return self.state.signal_graph.to_server_dict(bpm=self.state.bpm)
        return _build_graph(self.state, self._sf2_path)

    def load_sf2(self, sf2_path: str) -> bool:
        self._sf2_path = sf2_path
        if self.state.signal_graph is not None:
            for node in self.state.signal_graph.nodes:
                if node.node_type == "fluidsynth" and node.is_default_synth:
                    node.params["sf2_path"] = sf2_path
        resp = self._send(self._graph_payload())
        ok = resp is not None and resp.get("status") == "ok"
        if ok:
            self._graph_loaded    = True
            self._graph_track_ids = self._current_track_ids()
        return ok

    def _ensure_graph(self):
        current = self._current_track_ids()
        if not self._graph_loaded or current != self._graph_track_ids:
            if self.state.signal_graph is not None:
                self.state.signal_graph.sync_track_sources(self.state, self._sf2_path)
            resp = self._send(self._graph_payload())
            if resp and resp.get("status") == "ok":
                self._graph_loaded    = True
                self._graph_track_ids = current

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def mark_dirty(self):
        if self.state.signal_graph is not None:
            self.state.signal_graph.sync_track_sources(self.state, self._sf2_path)
            self.state.signal_graph.sync_pattern_sources(self.state)

        payload = self._graph_payload()
        # Hash the graph payload to detect structural changes.
        # Only send set_graph if the structure actually changed —
        # avoids destroying/recreating plugin instances (expensive for
        # plugins like DiffSinger that load ONNX models on activate).
        import json as _json
        payload_str = _json.dumps(payload, sort_keys=True)
        import hashlib
        graph_hash = hashlib.md5(payload_str.encode()).hexdigest()

        if graph_hash != self._last_graph_hash:
            resp = self._send(payload)
            if resp is None or resp.get("status") != "ok":
                print(f"[BindingEngine] set_graph failed: {resp}")
                return
            self._graph_loaded    = True
            self._graph_track_ids = self._current_track_ids()
            self._last_graph_hash = graph_hash

        self._send({"cmd": "set_bpm", "bpm": self.state.bpm})

        # Build and send tempo map from tempo automation track
        tempo_map = _build_tempo_map(self.state)
        self._send({"cmd": "set_tempo_map", "map": tempo_map})

        # Adjust all programming beats to just after the current beat to make sure they re-fire
        events = _build_server_schedule(self.state)

        self._send({"cmd": "set_schedule",
                    "events": events})

    def play(self):
        self.mark_dirty()
        self._send({"cmd": "prerender"})
        self._send({"cmd": "play"})
        self._is_playing = True

    def stop(self):
        self._send({"cmd": "stop"})
        self._is_playing = False

    def seek(self, beat: float):
        self._send({"cmd": "seek", "beat": beat})
        self._current_beat = beat

    def set_loop(self, start: Optional[float], end: Optional[float]):
        if start is not None and end is not None:
            self._send({"cmd": "set_loop", "start": start, "end": end,
                        "enabled": True})
        else:
            self._send({"cmd": "set_loop", "enabled": False})

    @property
    def current_beat(self) -> float:
        # Poll on demand rather than in a background thread.
        # Called from app.py's QTimer (~30fps) so the overhead is negligible.
        resp = self._send({"cmd": "get_position"})
        if resp and resp.get("status") == "ok":
            self._current_beat = resp.get("beat", self._current_beat)
            self._is_playing   = resp.get("playing", self._is_playing)
            self._current_bpm  = resp.get("bpm", self._current_bpm)
            m = resp.get("meter")
            if m:
                self._master_meter = m
        return self._current_beat

    @property
    def master_meter(self) -> dict:
        """Latest master-bus meter from the last get_position poll.

        Keys: peak_l, peak_r, rms_l, rms_r (linear), gr (gain reduction),
        valid (bool).  Polled lazily via current_beat by app.py's QTimer.
        """
        return self._master_meter

    # ------------------------------------------------------------------
    # Master section controls (terminal "mixer" node)
    # ------------------------------------------------------------------

    def set_master_gain(self, gain: float):
        """Linear makeup gain. Applied live now and persisted for export."""
        self.state.master_gain = max(0.0, float(gain))
        self.set_param("mixer", "master_gain", self.state.master_gain)

    def set_master_limiter(self, enabled: bool):
        self.state.master_limiter = bool(enabled)
        self.set_param("mixer", "limiter_enabled", 1.0 if enabled else 0.0)

    def set_master_ceiling_db(self, ceiling_db: float):
        self.state.master_ceiling_db = float(ceiling_db)
        self.set_param("mixer", "limiter_threshold", 10.0 ** (float(ceiling_db) / 20.0))

    @property
    def current_bpm(self) -> float:
        return self._current_bpm

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    # ------------------------------------------------------------------
    # Note preview
    # ------------------------------------------------------------------

    def play_single_note(self, pitch: int, velocity: int = 100,
                         channel: int = 0, duration: float = 0.5,
                         track_id=None):
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
        if track_id is not None:
            self._send({"cmd": "all_notes_off", "node_id": f"track_{track_id}"})
        else:
            self._send({"cmd": "all_notes_off"})

    def set_channel_program(self, channel: int, bank: int, program: int):
        pass  # handled by beat=-1 setup events in the schedule

    def _source_node_for(self, track_id, channel: int) -> str:
        if track_id is not None:
            if isinstance(track_id, str) and track_id.startswith("beat_inst_"):
                return track_id
            return f"track_{track_id}"
        for t in self.state.tracks:
            if (t.channel & 0x0F) == channel:
                return f"track_{t.id}"
        for bt in self.state.beat_tracks:
            return f"track_{bt.id}"
        return "track_default"

    # ------------------------------------------------------------------
    # Parameters and node data
    # ------------------------------------------------------------------

    def set_param(self, node_id: str, param_id: str, value: float):
        self._send({"cmd": "set_param", "node_id": node_id,
                    "param_id": param_id, "value": value})

    def get_node_data(self, node_id: str, port_id: str = "history") -> list:
        resp = self._send({"cmd": "get_node_data", "node_id": node_id,
                           "port_id": port_id})
        if not resp or resp.get("status") != "ok":
            return []
        try:
            return json.loads(resp.get("data", "[]"))
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Offline render
    # ------------------------------------------------------------------

    def render_offline_wav(self) -> Optional[bytes]:
        import base64
        self.mark_dirty()
        self._send({"cmd": "prerender"})
        resp = self._send({"cmd": "render", "format": "wav"})
        if resp is None or resp.get("status") != "ok":
            return None
        try:
            return base64.b64decode(resp["data"])
        except Exception as e:
            print(f"[BindingEngine] render decode error: {e}")
            return None

    def render_arr_wav(self, arr: dict) -> Optional[bytes]:
        """Render an arrangement dict for preview via the server engine.

        Unlike render_offline_wav(), this takes an arbitrary arrangement dict
        (as produced by build_pattern_preview) rather than rebuilding from
        AppState.  This lets pattern previews use the server's per-note tuning
        (note_tune events / fluid_synth_tune_notes) instead of the MIDI
        pitch-bend approximation used by the offline FluidSynth subprocess.

        Uses the real signal graph so previews route through the same synth
        chain the user has configured (samplers, effects, etc.) rather than
        falling back to a hardcoded FluidSynth-only graph.

        Thread-safe: uses _send_lock.  Marks graph dirty after rendering so
        the next play() or note preview restores the real graph and schedule.
        """
        import base64

        bpm = float(arr.get("bpm", 120.0))

        # Ensure the real graph is loaded so previews use the user's synth
        # chain (sampler plugins, effects, etc.).
        self._ensure_graph()

        fallback_node_id = "preview_track"
        events = _build_schedule_from_arr(arr, fallback_node_id)

        self._send({"cmd": "set_bpm", "bpm": bpm})
        self._send({"cmd": "set_schedule", "events": events})
        resp = self._send({"cmd": "render", "format": "wav"})

        # Mark graph dirty so the next play() / ensure_graph() rebuilds the
        # schedule from AppState.
        self._last_graph_hash = ""

        if resp is None or resp.get("status") != "ok":
            return None
        try:
            return base64.b64decode(resp["data"])
        except Exception as e:
            print(f"[BindingEngine] preview render decode error: {e}")
            return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        self.all_notes_off()
        # AudioServer destructor closes the PortAudio stream.
        self._server = None

    def ensure_instrument(self):
        self._ensure_graph()

    @property
    def is_connected(self) -> bool:
        return self._server is not None
