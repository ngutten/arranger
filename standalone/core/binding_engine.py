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
from typing import Optional

from pathlib import Path

from ..arranger_engine import AudioServer, AudioEngineConfig, load_plugin_library
from .server_engine import _build_graph, _build_server_schedule
from .settings import Settings

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

        # Cache playing state so is_playing doesn't need a round-trip every call.
        self._is_playing   = False
        self._current_beat = 0.0

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
        try:
            return json.loads(self._server.handle(json.dumps(request)))
        except Exception as e:
            print(f"[BindingEngine] handle() error: {e}")
            return None

    # ------------------------------------------------------------------
    # Graph / soundfont  (mirrors ServerEngine exactly)
    # ------------------------------------------------------------------

    def _current_track_ids(self) -> frozenset:
        return frozenset(
            [t.id for t in self.state.tracks] +
            [bt.id for bt in self.state.beat_tracks]
        )

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
        self._send(self._graph_payload())
        self._graph_loaded    = True
        self._graph_track_ids = self._current_track_ids()
        self._send({"cmd": "set_bpm", "bpm": self.state.bpm})
        self._send({"cmd": "set_schedule",
                    "events": _build_server_schedule(self.state)})

    def play(self):
        self.mark_dirty()
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
        resp = self._send({"cmd": "render", "format": "wav"})
        if resp is None or resp.get("status") != "ok":
            return None
        try:
            return base64.b64decode(resp["data"])
        except Exception as e:
            print(f"[BindingEngine] render decode error: {e}")
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
