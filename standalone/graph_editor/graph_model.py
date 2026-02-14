"""Signal graph data model.

Pure Python — no Qt dependency.  Owns the graph topology that the node editor
UI edits and that ServerEngine serialises into set_graph payloads.

Port types:
  MIDI       – track_source fan-out (no buffer; drives downstream synths directly)
  AUDIO      – interleaved stereo pair (UI abstraction; expands to _L/_R on serialise)
  AUDIO_MONO – single-channel float buffer (used by split_stereo / merge_stereo and LV2)
  CONTROL    – single float, control rate

AUDIO vs AUDIO_MONO
-------------------
The server only knows mono buffers (audio_out_L, audio_out_R, audio_in_L_N, etc.).
In the UI we represent a matched L+R pair as a single AUDIO wire for clarity.
split_stereo and merge_stereo nodes convert between the two:

  split_stereo   AUDIO in  →  AUDIO_MONO L out,  AUDIO_MONO R out
  merge_stereo   AUDIO_MONO L in,  AUDIO_MONO R in  →  AUDIO out

On serialisation, every AUDIO connection from port "audio" on node A to port
"audio_in_N" on node B expands to two connections:
  A.audio_out_L → B.audio_in_L_N
  A.audio_out_R → B.audio_in_R_N

Node types:

  Sources:
    track_source   – one per sequencer track; MIDI output
    control_source – emits scheduled control values; CONTROL output

  Synthesizers (MIDI in → AUDIO out):
    fluidsynth     – SF2-backed
    sine           – built-in debug synth
    sampler        – sample player [future]

  Plugins:
    lv2            – LV2 plugin; ports are dynamic (AUDIO_MONO / CONTROL)

  Utilities:
    mixer          – N AUDIO inputs → one AUDIO output; channel_count editable
    split_stereo   – AUDIO → AUDIO_MONO L + AUDIO_MONO R
    merge_stereo   – AUDIO_MONO L + AUDIO_MONO R → AUDIO

  Output:
    output         – terminal sink; serialises as id="mixer", type="mixer".
                     Has only AUDIO inputs (no outputs — it's a sink).
                     channel_count is user-editable.

MIDI multi-input rule
---------------------
Synth MIDI input ports accept multiple incoming connections (many track_sources
→ one synth). All other input ports accept at most one connection.
"""

from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ---------------------------------------------------------------------------
# Port type
# ---------------------------------------------------------------------------

class PortType(Enum):
    MIDI       = "midi"
    AUDIO      = "audio"        # stereo pair (UI abstraction)
    AUDIO_MONO = "audio_mono"   # single channel (split/merge, LV2)
    CONTROL    = "control"


# ---------------------------------------------------------------------------
# Port definition
# ---------------------------------------------------------------------------

@dataclass
class PortDef:
    name: str
    port_id: str        # logical ID; AUDIO ports use base names like "audio" or "audio_in_0"
    ptype: PortType
    is_output: bool


# ---------------------------------------------------------------------------
# Per-node-type port tables
# ---------------------------------------------------------------------------

TRACK_SOURCE_PORTS = [
    PortDef("Events", "events_out", PortType.MIDI, True),
]

CONTROL_SOURCE_PORTS = [
    PortDef("Control", "control_out", PortType.CONTROL, True),
]

# MIDI input — multi-connection allowed
SYNTH_MIDI_IN = PortDef("Events", "events_in", PortType.MIDI, False)

FLUIDSYNTH_PORTS = [
    SYNTH_MIDI_IN,
    PortDef("Audio", "audio", PortType.AUDIO, True),
]

SINE_PORTS = [
    SYNTH_MIDI_IN,
    PortDef("Audio", "audio", PortType.AUDIO, True),
]

SAMPLER_PORTS = [
    SYNTH_MIDI_IN,
    PortDef("Audio", "audio", PortType.AUDIO, True),
]

SPLIT_STEREO_PORTS = [
    PortDef("Stereo", "audio",   PortType.AUDIO,      False),
    PortDef("L",      "mono_L",  PortType.AUDIO_MONO, True),
    PortDef("R",      "mono_R",  PortType.AUDIO_MONO, True),
]

MERGE_STEREO_PORTS = [
    PortDef("L",      "mono_L",  PortType.AUDIO_MONO, False),
    PortDef("R",      "mono_R",  PortType.AUDIO_MONO, False),
    PortDef("Stereo", "audio",   PortType.AUDIO,      True),
]

NOTE_GATE_PORTS = [
    PortDef("Events",  "events_in",   PortType.MIDI,    False),
    PortDef("Control", "control_out", PortType.CONTROL, True),
]

NOTE_GATE_MODES = ["Gate", "Velocity", "Pitch", "Note Count"]


def midi_note_name(pitch: int) -> str:
    """Return display name for a MIDI pitch, e.g. 60 → 'C4'."""
    names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    octave = pitch // 12 - 1   # MIDI convention: C4 = 60
    return f"{names[pitch % 12]}{octave}"


def midi_pitch_from_name(name: str) -> Optional[int]:
    """Parse 'C4', 'F#3', etc. back to MIDI pitch. Returns None on failure."""
    names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    name = name.strip()
    # Split at the last digit run (handles negative octaves like C-1)
    i = len(name) - 1
    while i >= 0 and (name[i].isdigit() or name[i] == '-'):
        i -= 1
    note_part = name[:i+1].upper()
    oct_part  = name[i+1:]
    if note_part not in names or not oct_part:
        return None
    try:
        octave = int(oct_part)
        return names.index(note_part) + (octave + 1) * 12
    except ValueError:
        return None


def mixer_ports(channel_count: int) -> list[PortDef]:
    ports = [PortDef(f"In {i}", f"audio_in_{i}", PortType.AUDIO, False)
             for i in range(channel_count)]
    ports.append(PortDef("Audio", "audio", PortType.AUDIO, True))
    return ports


def output_ports(channel_count: int) -> list[PortDef]:
    """Output node has only inputs — it is a terminal sink."""
    return [PortDef(f"In {i}", f"audio_in_{i}", PortType.AUDIO, False)
            for i in range(channel_count)]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@dataclass
class GraphConnection:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_node: str = ""
    from_port: str = ""
    to_node:   str = ""
    to_port:   str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_node": self.from_node, "from_port": self.from_port,
            "to_node":   self.to_node,   "to_port":   self.to_port,
        }

    @staticmethod
    def from_dict(d: dict) -> "GraphConnection":
        return GraphConnection(
            id=d.get("id", str(uuid.uuid4())),
            from_node=d["from_node"], from_port=d["from_port"],
            to_node=d["to_node"],     to_port=d["to_port"],
        )


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    """One node in the signal graph.

    node_type    – one of the type strings documented above.
    node_id      – unique within the graph; output node serialises as "mixer".
    display_name – shown in node header.
    x, y         – canvas position (scene coords).
    params       – type-specific config dict.
    minimised    – settings panel collapsed.
    is_default_synth – new tracks auto-route here.
    """
    node_type:    str
    node_id:      str = field(default_factory=lambda: str(uuid.uuid4()))
    display_name: str = ""
    x: float = 0.0
    y: float = 0.0
    params: dict = field(default_factory=dict)
    minimised: bool = False
    is_default_synth: bool = False

    def ports(self) -> list[PortDef]:
        t = self.node_type
        if t == "track_source":    return TRACK_SOURCE_PORTS
        if t == "control_source":  return CONTROL_SOURCE_PORTS
        if t == "fluidsynth":      return FLUIDSYNTH_PORTS
        if t == "sine":            return SINE_PORTS
        if t == "sampler":         return SAMPLER_PORTS
        if t == "split_stereo":    return SPLIT_STEREO_PORTS
        if t == "merge_stereo":    return MERGE_STEREO_PORTS
        if t == "note_gate":       return NOTE_GATE_PORTS
        if t == "mixer":           return mixer_ports(self.params.get("channel_count", 2))
        if t == "output":          return output_ports(self.params.get("channel_count", 1))
        if t == "lv2":
            ports, stereo_map, dual_mono = _lv2_build_ports(self.params.get("_ports", []))
            # Cache derived metadata so to_server_dict can use it.
            # Written each call but idempotent.
            self.params["_stereo_map"] = stereo_map
            self.params["_dual_mono"]  = dual_mono
            return ports
        return []

    def output_ports(self) -> list[PortDef]: return [p for p in self.ports() if p.is_output]
    def input_ports(self)  -> list[PortDef]: return [p for p in self.ports() if not p.is_output]

    # -- Serialisation helpers --

    def _server_id(self) -> str:
        return "mixer" if self.node_type == "output" else self.node_id

    def _server_type(self) -> str:
        return "mixer" if self.node_type in ("output", "mixer") else self.node_type

    def to_server_dict(self) -> Optional[dict]:
        """Serialise as a server NodeDesc.

        split_stereo and merge_stereo are pure UI abstractions — they don't
        correspond to any server node, so they return None.  The connection
        expansion in GraphModel.to_server_dict() handles them transparently.
        """
        if self.node_type in ("split_stereo", "merge_stereo"):
            return None
        # Dual-mono LV2 nodes are expanded to two server nodes by GraphModel.to_server_dict
        if self.node_type == "lv2" and self.params.get("_dual_mono"):
            return None

        d: dict = {"id": self._server_id(), "type": self._server_type()}

        if self.node_type == "fluidsynth":
            d["sf2_path"] = self.params.get("sf2_path", "")
        if self.node_type == "lv2":
            d["lv2_uri"] = self.params.get("lv2_uri", "")
        if self.node_type == "sampler":
            d["sample_path"] = self.params.get("sample_path", "")
        if self.node_type in ("mixer", "output"):
            d["channel_count"] = self.params.get("channel_count", 2 if self.node_type == "mixer" else 1)
        if self.node_type == "note_gate":
            d["pitch_lo"]  = self.params.get("pitch_lo", 0)
            d["pitch_hi"]  = self.params.get("pitch_hi", 127)
            d["gate_mode"] = self.params.get("gate_mode", 0)

        param_keys = {k: v for k, v in self.params.items()
                      if k not in ("sf2_path", "lv2_uri", "sample_path",
                                   "channel_count", "_ports", "_stereo_map", "_dual_mono")
                      and isinstance(v, (int, float))}
        if param_keys:
            d["params"] = param_keys

        return d

    def to_dict(self) -> dict:
        # Exclude computed caches — rebuilt from _ports on load
        clean_params = {k: v for k, v in self.params.items()
                        if k not in ("_stereo_map", "_dual_mono")}
        return {
            "node_type":   self.node_type,
            "node_id":     self.node_id,
            "display_name": self.display_name,
            "x": self.x, "y": self.y,
            "params":      clean_params,
            "minimised":   self.minimised,
            "is_default_synth": self.is_default_synth,
        }

    @staticmethod
    def from_dict(d: dict) -> "GraphNode":
        return GraphNode(
            node_type=d["node_type"],
            node_id=d["node_id"],
            display_name=d.get("display_name", ""),
            x=d.get("x", 0.0), y=d.get("y", 0.0),
            params=d.get("params", {}),
            minimised=d.get("minimised", False),
            is_default_synth=d.get("is_default_synth", False),
        )


# ---------------------------------------------------------------------------
# Serialisation helpers — AUDIO port expansion
# ---------------------------------------------------------------------------

def _audio_port_to_lr(port_id: str, side: str) -> str:
    """Map a logical AUDIO port_id to its physical _L / _R server name.

    Rules:
      "audio"        → "audio_out_L" / "audio_out_R"   (synth / merge output)
      "audio_in_N"   → "audio_in_L_N" / "audio_in_R_N" (mixer / output inputs)
    """
    if port_id == "audio":
        return f"audio_out_{side}"
    if port_id.startswith("audio_in_"):
        n = port_id[len("audio_in_"):]
        return f"audio_in_{side}_{n}"
    # Fallback: just append _L or _R
    return f"{port_id}_{side}"


def _mono_port_to_server(port_id: str) -> str:
    """Map a logical AUDIO_MONO port_id to its server name.

    split_stereo outputs:  mono_L → audio_out_L,  mono_R → audio_out_R
    merge_stereo inputs:   mono_L → audio_out_L (of the upstream),
                           handled by the from-side of the connection.
    For AUDIO_MONO ports that are just plain LV2 symbols, pass through.
    """
    if port_id == "mono_L": return "audio_out_L"
    if port_id == "mono_R": return "audio_out_R"
    return port_id


# ---------------------------------------------------------------------------
# LV2 stereo port detection
# ---------------------------------------------------------------------------

import re as _re

def _lv2_stereo_key(sym: str):
    """If sym looks like one half of a stereo pair, return (base, side) where
    side is 'L' or 'R'.  Returns None if no stereo pattern is detected.

    Recognised patterns (case-insensitive), tried in order:
      explicit separator    in_l / in_r, out_left / out_right, audio_in_1 / audio_in_2
                            space/dash/dot variants: "In L", "in-r", "audio.1"
      no separator          AudioL / AudioR, inputLeft / inputRight
      bare name             "left" / "right" / "l" / "r"  (base = empty string)
    """
    s = sym.lower().rstrip()

    # Explicit separator (space / dash / dot / underscore before suffix)
    for pat, side_map in [
        (r'^(.+?)[_\-\. ]([lr])$',           {'l': 'L', 'r': 'R'}),
        (r'^(.+?)[_\-\. ](left|right)$',      {'left': 'L', 'right': 'R'}),
        (r'^(.+?)[_\-\. ]([12])$',            {'1': 'L', '2': 'R'}),
    ]:
        m = _re.match(pat, s)
        if m:
            suffix = m.group(m.lastindex)
            if suffix in side_map:
                base = m.group(1).rstrip('_-. ')
                if base:
                    return (base, side_map[suffix])

    # No separator (camelCase or concatenated): "AudioL", "inputRight"
    for pat, side_map in [
        (r'^(.+?)(left|right)$',  {'left': 'L', 'right': 'R'}),
        (r'^(.+?)([lr])$',        {'l': 'L', 'r': 'R'}),
    ]:
        m = _re.match(pat, s)
        if m:
            base, suffix = m.group(1), m.group(2)
            if base and suffix in side_map:
                return (base, side_map[suffix])

    # Bare name: the entire symbol is just "left"/"right"/"l"/"r"
    _bare = {'left': 'L', 'right': 'R', 'l': 'L', 'r': 'R'}
    if s in _bare:
        return ('', _bare[s])

    return None


def _lv2_build_ports(raw_ports: list) -> tuple:
    """Convert a raw LV2 port list (from list_plugins JSON) to PortDef objects.

    Returns (ports, stereo_map, dual_mono) where:
      ports       - list of PortDef for the UI graph
      stereo_map  - {port_id: {"L": sym_L, "R": sym_R}} for native-stereo plugins
      dual_mono   - True if the plugin is genuinely mono (1 audio in, 1 audio out)
                    and should be instantiated twice (L and R) on the server.

    Native-stereo plugins (L/R suffix pairs) are collapsed into single AUDIO ports.

    Dual-mono plugins (exactly one unpaired audio input + one unpaired audio output,
    no other audio ports) get their lone ports promoted to AUDIO so they wire
    directly with the rest of the stereo graph.  On serialisation, two server-side
    LV2 nodes are emitted, one for each channel.

    Anything else with unmatched audio ports stays as AUDIO_MONO.
    """
    from collections import defaultdict

    audio_ports = [p for p in raw_ports if p.get("type") == "audio"]
    other_ports = [p for p in raw_ports if p.get("type") != "audio"]

    # Pass 1: match native L/R stereo pairs
    groups: dict = defaultdict(list)
    ungrouped = []
    for p in audio_ports:
        sym = p.get("symbol", "")
        key = _lv2_stereo_key(sym)
        if key:
            groups[(key[0], p.get("direction", ""))].append((key[1], p))
        else:
            ungrouped.append(p)

    result: list[PortDef] = []
    stereo_map: dict = {}

    for (base, direction), members in groups.items():
        sides = {side: p for side, p in members}
        if "L" in sides and "R" in sides:
            sym_L = sides["L"].get("symbol", "l")
            sym_R = sides["R"].get("symbol", "r")
            port_id = base if base else sym_L
            display_name = base if base else f"{sym_L}/{sym_R}"
            result.append(PortDef(
                name=display_name,
                port_id=port_id,
                ptype=PortType.AUDIO,
                is_output=(direction == "output"),
            ))
            stereo_map[port_id] = {"L": sym_L, "R": sym_R}
        else:
            for side, p in members:
                result.append(PortDef(
                    name=p.get("symbol", "?"),
                    port_id=p.get("symbol", "?"),
                    ptype=PortType.AUDIO_MONO,
                    is_output=(p.get("direction") == "output"),
                ))

    # Pass 2: dual-mono detection
    # If no native stereo pairs were found and the plugin has exactly one unpaired
    # audio input and one unpaired audio output, treat it as dual-mono: promote
    # those ports to AUDIO and flag for dual instantiation on the server.
    dual_mono = False
    if not stereo_map:
        mono_ins  = [p for p in ungrouped if p.get("direction") == "input"]
        mono_outs = [p for p in ungrouped if p.get("direction") == "output"]
        if len(mono_ins) == 1 and len(mono_outs) == 1:
            dual_mono = True
            for p, is_out in ((mono_ins[0], False), (mono_outs[0], True)):
                sym = p.get("symbol", "?")
                result.append(PortDef(
                    name=p.get("name", sym),
                    port_id=sym,
                    ptype=PortType.AUDIO,
                    is_output=is_out,
                ))
            ungrouped = []

    for p in ungrouped:
        result.append(PortDef(
            name=p.get("symbol", "?"),
            port_id=p.get("symbol", "?"),
            ptype=PortType.AUDIO_MONO,
            is_output=(p.get("direction") == "output"),
        ))

    for p in other_ports:
        result.append(PortDef(
            name=p.get("symbol", "?"),
            port_id=p.get("symbol", "?"),
            ptype=PortType.CONTROL,
            is_output=(p.get("direction") == "output"),
        ))

    return result, stereo_map, dual_mono


# ---------------------------------------------------------------------------
# Graph model
# ---------------------------------------------------------------------------

class GraphModel:
    """Mutable signal graph: nodes + connections."""

    def __init__(self):
        self.nodes: list[GraphNode] = []
        self.connections: list[GraphConnection] = []

    # -- Node accessors --

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    def add_node(self, node: GraphNode) -> None:
        self.nodes.append(node)

    def remove_node(self, node_id: str) -> None:
        self.nodes = [n for n in self.nodes if n.node_id != node_id]
        self.connections = [
            c for c in self.connections
            if c.from_node != node_id and c.to_node != node_id
        ]

    # -- Connection accessors --

    def _port_type_for(self, node_id: str, port_id: str) -> Optional[PortType]:
        node = self.get_node(node_id)
        if not node:
            return None
        return next((p.ptype for p in node.ports() if p.port_id == port_id), None)

    def _is_midi_input(self, node_id: str, port_id: str) -> bool:
        node = self.get_node(node_id)
        if not node:
            return False
        p = next((p for p in node.ports()
                  if p.port_id == port_id and not p.is_output), None)
        return p is not None and p.ptype == PortType.MIDI

    def add_connection(self, conn: GraphConnection) -> bool:
        """Add connection. Returns True if accepted.

        Rules:
          - No duplicate connections.
          - No self-loops.
          - from_port must be an output, to_port must be an input.
          - Port types must match.
          - At most one incoming connection per input port, EXCEPT MIDI inputs
            which accept any number (many track_sources → one synth).
        """
        if conn.from_node == conn.to_node:
            return False

        # Exact duplicate
        for c in self.connections:
            if (c.from_node == conn.from_node and c.from_port == conn.from_port and
                    c.to_node == conn.to_node and c.to_port == conn.to_port):
                return False

        # Type match
        src_type = self._port_type_for(conn.from_node, conn.from_port)
        dst_type = self._port_type_for(conn.to_node,   conn.to_port)
        if src_type is None or dst_type is None or src_type != dst_type:
            return False

        # One-per-input, except MIDI
        if not self._is_midi_input(conn.to_node, conn.to_port):
            for c in self.connections:
                if c.to_node == conn.to_node and c.to_port == conn.to_port:
                    return False

        self.connections.append(conn)
        return True

    def remove_connection(self, conn_id: str) -> None:
        self.connections = [c for c in self.connections if c.id != conn_id]

    def connections_for_node(self, node_id: str) -> list[GraphConnection]:
        return [c for c in self.connections
                if c.from_node == node_id or c.to_node == node_id]

    # -- Default synth --

    def default_synth(self) -> Optional[GraphNode]:
        for n in self.nodes:
            if n.is_default_synth:
                return n
        for n in self.nodes:
            if n.node_type in ("fluidsynth", "sine", "sampler", "lv2"):
                return n
        return None

    def set_default_synth(self, node_id: str) -> None:
        for n in self.nodes:
            n.is_default_synth = (n.node_id == node_id)

    # -- Track source management --

    def add_track_source(self, track_id, track_name: str, sf2_path: str = "") -> None:
        nid = f"track_{track_id}"
        if self.get_node(nid):
            return
        existing = [n for n in self.nodes if n.node_type == "track_source"]
        node = GraphNode(
            node_type="track_source",
            node_id=nid,
            display_name=track_name,
            x=40, y=40 + len(existing) * 70,
        )
        self.add_node(node)
        target = self.default_synth()
        if target:
            self.add_connection(GraphConnection(
                from_node=nid, from_port="events_out",
                to_node=target.node_id, to_port="events_in",
            ))

    def remove_track_source(self, track_id) -> None:
        self.remove_node(f"track_{track_id}")

    def sync_track_sources(self, state, sf2_path: str = "") -> None:
        current_ids = set(
            [f"track_{t.id}" for t in state.tracks] +
            [f"track_{bt.id}" for bt in state.beat_tracks]
        )
        existing_ids = {n.node_id for n in self.nodes if n.node_type == "track_source"}
        for nid in existing_ids - current_ids:
            self.remove_node(nid)
        for t in state.tracks:
            self.add_track_source(t.id, t.name, sf2_path)
        for bt in state.beat_tracks:
            self.add_track_source(bt.id, bt.name, sf2_path)

    # -- Serialisation --

    def to_server_dict(self, bpm: float = 120.0) -> dict:
        """Build the set_graph payload, expanding AUDIO wires and eliding
        split_stereo / merge_stereo pass-through nodes."""

        # Node ID remapping: output → "mixer", split/merge → elided
        id_remap = {}
        for n in self.nodes:
            if n.node_type == "output":
                id_remap[n.node_id] = "mixer"

        # Collect normal nodes (dual-mono LV2 nodes return None here; we add
        # their two server-side instances below).
        nodes = [d for n in self.nodes
                 if (d := n.to_server_dict()) is not None]

        # Emit a pair of LV2 nodes (id__L, id__R) for every dual-mono plugin
        for n in self.nodes:
            if n.node_type == "lv2" and n.params.get("_dual_mono"):
                # Force port metadata to be populated
                n.ports()
                base_params = {k: v for k, v in n.params.items()
                               if k not in ("_ports", "_stereo_map", "_dual_mono")
                               and isinstance(v, (int, float))}
                for side in ("L", "R"):
                    d = {
                        "id":      f"{n.node_id}__{side}",
                        "type":    "lv2",
                        "lv2_uri": n.params.get("lv2_uri", ""),
                    }
                    if base_params:
                        d["params"] = base_params
                    nodes.append(d)

        connections = []
        for c in self.connections:
            from_node = id_remap.get(c.from_node, c.from_node)
            to_node   = id_remap.get(c.to_node,   c.to_node)

            src_node = self.get_node(c.from_node)
            dst_node = self.get_node(c.to_node)
            if not src_node or not dst_node:
                continue

            src_type = self._port_type_for(c.from_node, c.from_port)

            # --- Elide split_stereo ---
            # Connection INTO a split_stereo: record the mapping so that
            # connections OUT of split_stereo can skip straight to the real dest.
            # We handle this by tracing the full path at serialisation time.
            if dst_node.node_type == "split_stereo":
                # The other side of the split will be handled when we process
                # connections FROM the split_stereo node — skip here.
                continue
            if src_node.node_type == "split_stereo":
                # Trace back to what feeds the split_stereo's input
                feed = next(
                    (fc for fc in self.connections
                     if fc.to_node == c.from_node and fc.to_port == "audio"),
                    None
                )
                if feed is None:
                    continue
                real_src_node = self.get_node(feed.from_node)
                if real_src_node is None:
                    continue
                real_from_node = id_remap.get(feed.from_node, feed.from_node)
                # c.from_port is "mono_L" or "mono_R"
                side = "L" if c.from_port == "mono_L" else "R"
                from_port_server = _audio_port_to_lr(feed.from_port, side)
                to_port_server   = _mono_port_to_server(c.to_port) if src_type == PortType.AUDIO_MONO else c.to_port
                connections.append({
                    "from_node": real_from_node, "from_port": from_port_server,
                    "to_node":   to_node,        "to_port":   to_port_server,
                })
                continue

            # --- Elide merge_stereo ---
            if dst_node.node_type == "merge_stereo":
                continue
            if src_node.node_type == "merge_stereo":
                # Find both connections feeding the merge's mono_L / mono_R inputs.
                feed_L = next(
                    (fc for fc in self.connections
                     if fc.to_node == c.from_node and fc.to_port == "mono_L"), None)
                feed_R = next(
                    (fc for fc in self.connections
                     if fc.to_node == c.from_node and fc.to_port == "mono_R"), None)
                if feed_L is None or feed_R is None:
                    continue

                def _resolve_mono_feed(feed, side_char):
                    """Return (real_from_node_id, from_port_server) for a
                    connection feeding a merge_stereo input, tracing through any
                    intervening split_stereo node transparently."""
                    upstream = self.get_node(feed.from_node)
                    if upstream and upstream.node_type == "split_stereo":
                        # Trace back to what feeds the split's AUDIO input
                        split_feed = next(
                            (fc for fc in self.connections
                             if fc.to_node == feed.from_node and fc.to_port == "audio"),
                            None)
                        if split_feed is None:
                            return None, None
                        real_src = self.get_node(split_feed.from_node)
                        real_from_id = id_remap.get(split_feed.from_node, split_feed.from_node)
                        sm = (real_src.params.get("_stereo_map", {})
                              if real_src and real_src.node_type == "lv2" else {})
                        pair = sm.get(split_feed.from_port)
                        from_port_sv = pair[side_char] if pair else _audio_port_to_lr(split_feed.from_port, side_char)
                        return real_from_id, from_port_sv
                    else:
                        # feed.from_port is a plain AUDIO_MONO symbol
                        real_from_id = id_remap.get(feed.from_node, feed.from_node)
                        return real_from_id, _mono_port_to_server(feed.from_port)

                dst_sm = (dst_node.params.get("_stereo_map", {})
                          if dst_node.node_type == "lv2" else {})
                for feed, side_char in ((feed_L, "L"), (feed_R, "R")):
                    real_from_id, from_port_sv = _resolve_mono_feed(feed, side_char)
                    if real_from_id is None:
                        continue
                    pair = dst_sm.get(c.to_port)
                    to_port_sv = pair[side_char] if pair else _audio_port_to_lr(c.to_port, side_char)
                    connections.append({
                        "from_node": real_from_id, "from_port": from_port_sv,
                        "to_node":   to_node,      "to_port":   to_port_sv,
                    })
                continue

            # --- Normal connection ---
            if src_type == PortType.AUDIO:
                # Expand stereo pair.  Three cases per side:
                #
                #  dual-mono node: the server has two instances (id__L, id__R),
                #    each with one audio port.  Route side X to instance __X,
                #    using the plugin's own port symbol (not the _L/_R convention).
                #
                #  native-stereo LV2: look up actual L/R symbols in _stereo_map.
                #
                #  everything else (FluidSynth, Mixer, etc.): standard audio_out_L
                #    / audio_in_L_N naming via _audio_port_to_lr.

                def _node_id_for_side(node_obj, base_id, side):
                    """Return the server node id for one channel of a stereo wire."""
                    if node_obj and node_obj.node_type == "lv2" and node_obj.params.get("_dual_mono"):
                        return f"{base_id}__{side}"
                    return base_id

                def _port_for_side(node_obj, port_id, side):
                    """Return the server port symbol for one channel of a stereo wire."""
                    if node_obj and node_obj.node_type == "lv2":
                        if node_obj.params.get("_dual_mono"):
                            # The server node has only one audio port; use its symbol directly
                            return port_id
                        sm = node_obj.params.get("_stereo_map", {})
                        pair = sm.get(port_id)
                        if pair:
                            return pair[side]
                    return _audio_port_to_lr(port_id, side)

                for side in ("L", "R"):
                    connections.append({
                        "from_node": _node_id_for_side(src_node, from_node, side),
                        "from_port": _port_for_side(src_node, c.from_port, side),
                        "to_node":   _node_id_for_side(dst_node, to_node,   side),
                        "to_port":   _port_for_side(dst_node, c.to_port,   side),
                    })
            elif src_type == PortType.AUDIO_MONO:
                connections.append({
                    "from_node": from_node,
                    "from_port": _mono_port_to_server(c.from_port),
                    "to_node":   to_node,
                    "to_port":   _mono_port_to_server(c.to_port),
                })
            else:
                # MIDI or CONTROL — mostly pass through as-is.
                # Exception: if the destination is a dual-mono LV2 node, the
                # control value needs to reach both __L and __R instances.
                if (dst_node and dst_node.node_type == "lv2"
                        and dst_node.params.get("_dual_mono")):
                    for side in ("L", "R"):
                        connections.append({
                            "from_node": from_node,
                            "from_port": c.from_port,
                            "to_node":   f"{to_node}__{side}",
                            "to_port":   c.to_port,
                        })
                else:
                    connections.append({
                        "from_node": from_node, "from_port": c.from_port,
                        "to_node":   to_node,   "to_port":   c.to_port,
                    })

        return {"cmd": "set_graph", "bpm": bpm, "nodes": nodes, "connections": connections}

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "connections": [c.to_dict() for c in self.connections],
        }

    @staticmethod
    def from_dict(d: dict) -> "GraphModel":
        g = GraphModel()
        g.nodes = [GraphNode.from_dict(n) for n in d.get("nodes", [])]
        g.connections = [GraphConnection.from_dict(c) for c in d.get("connections", [])]
        return g

    # -- Factory --

    @staticmethod
    def make_default(state, sf2_path: str = "") -> "GraphModel":
        """Build the standard default graph: all tracks → synth → output."""
        g = GraphModel()

        synth_type = "fluidsynth" if sf2_path else "sine"
        synth = GraphNode(
            node_type=synth_type,
            node_id="synth_default",
            display_name="FluidSynth" if sf2_path else "Sine",
            x=320, y=200,
            params={"sf2_path": sf2_path} if sf2_path else {},
            is_default_synth=True,
        )
        g.add_node(synth)

        output = GraphNode(
            node_type="output",
            node_id="output_main",
            display_name="Output",
            x=600, y=200,
            params={"channel_count": 1},
        )
        g.add_node(output)

        # Single AUDIO wire: synth → output
        g.add_connection(GraphConnection(
            from_node=synth.node_id, from_port="audio",
            to_node=output.node_id,  to_port="audio_in_0",
        ))

        all_tracks = list(state.tracks) + list(state.beat_tracks)
        for i, t in enumerate(all_tracks):
            nid = f"track_{t.id}"
            g.add_node(GraphNode(
                node_type="track_source",
                node_id=nid,
                display_name=getattr(t, 'name', f'Track {t.id}'),
                x=40, y=40 + i * 70,
            ))
            g.add_connection(GraphConnection(
                from_node=nid,         from_port="events_out",
                to_node=synth.node_id, to_port="events_in",
            ))

        return g
