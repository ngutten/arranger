"""midi_source_router.py
Bridges an rtmidi input port to the audio server IPC.

Two modes, handled by the same class:

1. **Default preview** (no midi_source node in the graph):
   When the MIDI recording port is open (piano roll rec button armed), this is
   already handled by the existing piano_roll._midi_callback.  However, we
   also want preview-through-audio when NOT recording.  The App instantiates
   one MidiPreviewRouter that forwards every note_on/note_off coming from the
   configured MIDI device to the audio server via play_single_note /
   all_notes_off, targeting the currently selected track.

2. **midi_source node** (a midi_source node is present in the graph):
   The MidiSourceRouter opens the configured port and feeds raw note
   events directly to the node's server-side track_source via note_on /
   note_off IPC commands.  This overrides the default preview routing.

The router is started/stopped automatically by the App when:
  - The MIDI input device setting changes.
  - A midi_source node is added/removed from the graph.
  - The graph is pushed to the engine.

Thread safety:
  - The rtmidi callback runs on a background thread.
  - It only calls engine._send() (which holds its own lock) and reads
    self._node_id / self._channel_filter atomically (they're just Python
    objects; the GIL protects simple attribute reads).
"""

from __future__ import annotations

import threading
from typing import Optional


class MidiSourceRouter:
    """Manages a single rtmidi input port and routes events to the server.

    Parameters
    ----------
    engine : ServerEngine
        The currently active server engine (must support _send / note_on etc.).
    device_name : str
        rtmidi port name to open.
    node_id : str
        Server node ID to target (e.g. "midi_source_0" or a track_<id>).
    channel_filter : int
        0 = all channels, 1-16 = specific MIDI channel.
    get_track_id : callable -> Optional[int]
        Called when node_id is None (default preview mode) to get the
        currently selected track id.  Ignored in midi_source mode.
    """

    def __init__(self, engine, device_name: str, node_id: Optional[str] = None,
                 channel_filter: int = 0, get_track_id=None):
        self._engine        = engine
        self._device_name   = device_name
        self._node_id       = node_id         # None → use selected track
        self._channel_filter = channel_filter
        self._get_track_id  = get_track_id

        self._midi_in = None
        self._running = False
        self._lock    = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Open the MIDI port and start routing.  Returns True on success."""
        with self._lock:
            if self._running:
                return True
            try:
                import rtmidi
                mi = rtmidi.MidiIn()
                ports = mi.get_ports()
                if self._device_name not in ports:
                    print(f"[MidiSourceRouter] Device not found: {self._device_name!r}")
                    return False
                mi.open_port(ports.index(self._device_name))
                mi.set_callback(self._callback)
                mi.ignore_types(sysex=True, timing=True, active_sense=True)
                self._midi_in = mi
                self._running = True
                print(f"[MidiSourceRouter] Opened {self._device_name!r} → node {self._node_id!r}")
                return True
            except Exception as e:
                print(f"[MidiSourceRouter] Could not open {self._device_name!r}: {e}")
                return False

    def stop(self) -> None:
        """Close the MIDI port and stop routing."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            if self._midi_in:
                try:
                    self._midi_in.close_port()
                except Exception:
                    pass
                self._midi_in = None
            print(f"[MidiSourceRouter] Closed {self._device_name!r}")

    def update_node(self, node_id: Optional[str], channel_filter: int = 0) -> None:
        """Hot-swap the target node and channel filter without restarting."""
        self._node_id        = node_id
        self._channel_filter = channel_filter

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # rtmidi callback (background thread)
    # ------------------------------------------------------------------

    def _callback(self, event, _data=None):
        msg, _dt = event
        if not msg:
            return

        status  = msg[0] & 0xF0
        channel = msg[0] & 0x0F   # 0-15

        # Channel filter (0 = all)
        cf = self._channel_filter
        if cf != 0 and channel != (cf - 1):
            return

        node_id = self._node_id
        if node_id is None:
            # Default preview mode: target selected track
            if self._get_track_id is not None:
                tid = self._get_track_id()
                if tid is not None:
                    node_id = f"track_{tid}"
            if node_id is None:
                return

        engine = self._engine
        if engine is None:
            return

        if status == 0x90 and len(msg) >= 3 and msg[2] > 0:
            # Note On
            engine._send({"cmd": "note_on", "node_id": node_id,
                           "channel": channel, "pitch": msg[1], "velocity": msg[2]})
        elif status == 0x80 or (status == 0x90 and len(msg) >= 3 and msg[2] == 0):
            # Note Off
            engine._send({"cmd": "note_off", "node_id": node_id,
                           "channel": channel, "pitch": msg[1]})
        elif status == 0xE0 and len(msg) >= 3:
            # Pitch bend — forward as a bend value (-1..1)
            raw   = (msg[2] << 7) | msg[1]
            value = (raw - 8192) / 8191.0
            # The server doesn't have a pitch_bend IPC command yet; skip silently.
            # (Future: add CMD_PITCH_BEND to protocol.h)
            pass
        elif status == 0xB0 and len(msg) >= 3:
            # CC — skip for now
            pass
