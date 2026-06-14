"""Signal graph data model.

Pure Python — no Qt dependency.  Owns the graph topology that the node editor
UI edits and that ServerEngine serialises into set_graph payloads.

Port types:
  MIDI       – track_source fan-out (no buffer; drives downstream synths directly)
  AUDIO      – interleaved stereo pair (UI abstraction; expands to _L/_R on serialise)
  AUDIO_MONO – single-channel float buffer (used by split_stereo / merge_stereo)
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
    PATTERN    = "pattern"      # PatternData snapshot (melodic or beat pattern)


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

MIDI_SOURCE_PORTS = [
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

PATTERN_SOURCE_PORTS = [
    PortDef("Pattern", "pattern_out", PortType.PATTERN, True),
]

BEAT_PATTERN_SOURCE_PORTS = [
    PortDef("Pattern", "pattern_out", PortType.PATTERN, True),
]


def serialize_pattern(pattern) -> dict:
    """Serialize a melodic Pattern to the inline dict expected by C++ graph.cpp.

    Notes carry channel 0 and program=-1 (no program override), matching
    the convention for melodic tracks that route through a single synth.
    """
    notes = []
    for n in pattern.notes:
        note_dict = {
            "beat":     n.start,
            "duration": n.duration,
            "channel":  0,
            "pitch":    n.pitch,
            "velocity": n.velocity,
            "program":  -1,
            "bank":     -1,
        }
        if n.lyric:
            note_dict["lyric"] = n.lyric
        notes.append(note_dict)
    return {
        "notes":           notes,
        "length_beats":    float(pattern.length),
        "subdivision":     0,
        "is_beat_pattern": False,
    }


def serialize_beat_pattern(beat_pattern, state) -> dict:
    """Serialize a BeatPattern + AppState beat_kit to the inline dict.

    Each active grid cell becomes a note carrying the instrument's channel,
    pitch, program, and bank so downstream synths can route correctly.
    """
    notes = []
    for inst in state.beat_kit:
        grid = beat_pattern.grid.get(inst.id)
        if not grid:
            continue
        n_steps = len(grid)
        step_dur = beat_pattern.length / n_steps
        for step_idx, vel in enumerate(grid):
            if vel <= 0:
                continue
            notes.append({
                "beat":     step_idx * step_dur,
                "duration": step_dur * 0.8,
                "channel":  inst.channel,
                "pitch":    inst.pitch,
                "velocity": vel,
                "program":  inst.program,
                "bank":     inst.bank,
            })
    notes.sort(key=lambda n: n["beat"])
    return {
        "notes":           notes,
        "length_beats":    float(beat_pattern.length),
        "subdivision":     beat_pattern.subdivision,
        "is_beat_pattern": True,
    }


def update_pattern_source_node(node, pattern_id: int, state) -> bool:
    """Populate node.params['_pattern_id'] and ['_pattern_data'] from state.

    Called by the UI when the user selects a pattern from the dropdown.
    Returns True if the pattern was found and the node updated.
    """
    if node.node_type == "pattern_source":
        pat = state.find_pattern(pattern_id)
        if pat is None:
            return False
        node.params["_pattern_id"]   = pattern_id
        node.params["_pattern_data"] = serialize_pattern(pat)
        node.display_name = f"Pattern: {pat.name}"
        return True
    elif node.node_type == "beat_pattern_source":
        pat = state.find_beat_pattern(pattern_id)
        if pat is None:
            return False
        node.params["_pattern_id"]   = pattern_id
        node.params["_pattern_data"] = serialize_beat_pattern(pat, state)
        node.display_name = f"Beat: {pat.name}"
        return True
    return False


# ---------------------------------------------------------------------------
# Plugin descriptor cache  (populated from list_registered_plugins response)
# ---------------------------------------------------------------------------

# Maps plugin ID ("builtin.sine", etc.) → descriptor dict from server.
# Populated by set_plugin_descriptors() at startup / reconnect.
_plugin_descriptors: dict[str, dict] = {}

# Maps legacy type name → plugin ID for backward compatibility.
# E.g. "sine" → "builtin.sine", "mixer" → "builtin.mixer", etc.
_legacy_type_to_plugin_id: dict[str, str] = {}


def set_plugin_descriptors(descriptors: list[dict]) -> None:
    """Cache plugin descriptors fetched from the server.

    Called once (or on reconnect) with the 'plugins' list from the
    list_registered_plugins response.
    """
    global _plugin_descriptors, _legacy_type_to_plugin_id
    _plugin_descriptors.clear()
    _legacy_type_to_plugin_id.clear()
    for desc in descriptors:
        pid = desc.get("id", "")
        if not pid:
            continue
        _plugin_descriptors[pid] = desc
        # Build legacy mapping: "builtin.sine" → register under "sine" too
        if pid.startswith("builtin."):
            short = pid[len("builtin."):]
            _legacy_type_to_plugin_id[short] = pid


def get_plugin_descriptor(type_or_id: str) -> Optional[dict]:
    """Look up a cached plugin descriptor by ID or legacy type name."""
    if type_or_id in _plugin_descriptors:
        return _plugin_descriptors[type_or_id]
    pid = _legacy_type_to_plugin_id.get(type_or_id)
    if pid:
        return _plugin_descriptors.get(pid)
    return None


def plugin_id_for_type(type_name: str) -> Optional[str]:
    """Return the plugin ID for a node type, or None if not plugin-backed."""
    if type_name in _plugin_descriptors:
        return type_name
    return _legacy_type_to_plugin_id.get(type_name)


def note_attrs_for_type(type_or_id: str) -> list[dict]:
    """Per-note attribute declarations advertised by a node type's plugin.

    Returns the list of {id, display_name, hint, default, min, max, choices?}
    dicts, or [] for non-synth / unknown nodes.
    """
    desc = get_plugin_descriptor(type_or_id)
    return list(desc.get("note_attrs", [])) if desc else []


def note_attrs_in_graph(model) -> list[dict]:
    """Union of note-attr declarations across every node in *model*'s graph.

    This is what the piano roll offers as editable lanes: an attribute appears
    if any synth in the current signal graph consumes it.  Deduplicated by id,
    preserving first-seen order.  Returns [] if model is None.
    """
    if model is None:
        return []
    seen: dict[str, dict] = {}
    for node in getattr(model, "nodes", []):
        for na in note_attrs_for_type(node.node_type):
            if na["id"] not in seen:
                seen[na["id"]] = na
    return list(seen.values())


def _plugin_ports_from_descriptor(desc: dict, params: dict) -> list["PortDef"]:
    """Derive PortDef list from a plugin descriptor dict.

    Handles the mapping from plugin port types to the UI's PortType enum,
    including the AudioStereo → single AUDIO wire abstraction and
    Event → MIDI equivalence.

    For mixer-like plugins with dynamic channel_count, uses params to
    determine the actual descriptor (falls back to the cached one).
    """
    ports_out = []
    for p in desc.get("ports", []):
        ptype_str = p.get("type", "")
        role = p.get("role", "input")
        is_out = role in ("output", "monitor")
        pid = p.get("id", "")
        display = p.get("display_name", pid)

        if ptype_str == "audio_stereo":
            ports_out.append(PortDef(display, pid, PortType.AUDIO, is_out))
        elif ptype_str == "audio_mono":
            ports_out.append(PortDef(display, pid, PortType.AUDIO_MONO, is_out))
        elif ptype_str == "event":
            ports_out.append(PortDef(display, pid, PortType.MIDI, is_out))
        elif ptype_str == "control":
            ports_out.append(PortDef(display, pid, PortType.CONTROL, is_out))
        elif ptype_str == "pattern":
            ports_out.append(PortDef(display, pid, PortType.PATTERN, is_out))
    return ports_out


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
    hidden_ports – set of port_ids explicitly hidden by the user.
    """
    node_type:    str
    node_id:      str = field(default_factory=lambda: str(uuid.uuid4()))
    display_name: str = ""
    x: float = 0.0
    y: float = 0.0
    params: dict = field(default_factory=dict)
    minimised: bool = False
    is_default_synth: bool = False
    hidden_ports: set = field(default_factory=set)

    def visible_ports(self) -> list[PortDef]:
        """Ports that are currently shown in the canvas (not hidden by user)."""
        return [p for p in self.ports() if p.port_id not in self.hidden_ports]

    def visible_inputs(self) -> list[PortDef]:
        return [p for p in self.visible_ports() if not p.is_output]

    def visible_outputs(self) -> list[PortDef]:
        return [p for p in self.visible_ports() if p.is_output]

    def ports(self) -> list[PortDef]:
        t = self.node_type
        if t == "track_source":    return TRACK_SOURCE_PORTS
        if t == "midi_source":     return MIDI_SOURCE_PORTS
        if t == "fluidsynth":      return FLUIDSYNTH_PORTS
        if t == "sine":            return SINE_PORTS
        if t == "pattern_source":      return PATTERN_SOURCE_PORTS
        if t == "beat_pattern_source": return BEAT_PATTERN_SOURCE_PORTS
        # "sampler" and "control_source" fall through to plugin descriptor
        if t == "sampler":
            desc = get_plugin_descriptor(t)
            if desc:
                return _plugin_ports_from_descriptor(desc, self.params)
            return SAMPLER_PORTS   # fallback: events_in + audio only
        if t == "control_source":
            # Now uses plugin descriptor from builtin.control_source
            desc = get_plugin_descriptor(t)
            if desc:
                return _plugin_ports_from_descriptor(desc, self.params)
            return CONTROL_SOURCE_PORTS   # fallback for backward compatibility
        if t == "split_stereo":    return SPLIT_STEREO_PORTS
        if t == "merge_stereo":    return MERGE_STEREO_PORTS
        if t == "note_gate":       return NOTE_GATE_PORTS
        if t == "mixer":           return mixer_ports(self.params.get("channel_count", 2))
        if t == "output":          return output_ports(self.params.get("channel_count", 1))
        # Plugin-backed node: look up descriptor from cache
        desc = get_plugin_descriptor(t)
        if desc:
            return _plugin_ports_from_descriptor(desc, self.params)
        return []

    def output_ports(self) -> list[PortDef]: return [p for p in self.ports() if p.is_output]
    def input_ports(self)  -> list[PortDef]: return [p for p in self.ports() if not p.is_output]

    # -- Serialisation helpers --

    def _server_id(self) -> str:
        return "mixer" if self.node_type == "output" else self.node_id

    def _server_type(self) -> str:
        if self.node_type in ("output", "mixer"):
            return "mixer"
        if self.node_type == "midi_source":
            return "track_source"   # server sees a plain track_source; Python drives it
        # If this is a known plugin, send the plugin ID so make_node()
        # resolves via the registry.  Legacy short names also work.
        pid = plugin_id_for_type(self.node_type)
        if pid:
            return pid
        return self.node_type

    def to_server_dict(self) -> Optional[dict]:
        """Serialise as a server NodeDesc.

        split_stereo and merge_stereo are pure UI abstractions — they don't
        correspond to any server node, so they return None.  The connection
        expansion in GraphModel.to_server_dict() handles them transparently.
        """
        if self.node_type in ("split_stereo", "merge_stereo"):
            return None

        # Pattern source nodes — emit type + inline pattern data for C++ graph.cpp.
        if self.node_type in ("pattern_source", "beat_pattern_source"):
            pattern_data = self.params.get("_pattern_data") or {
                "notes": [], "length_beats": 1.0, "subdivision": 0,
                "is_beat_pattern": self.node_type == "beat_pattern_source",
            }
            return {
                "id":      self.node_id,
                "type":    self.node_type,
                "pattern": pattern_data,
            }

        d: dict = {"id": self._server_id(), "type": self._server_type()}

        if self.node_type == "fluidsynth":
            d["sf2_path"] = self.params.get("sf2_path", "")
        if self.node_type == "sampler":
            d["sample_path"] = self.params.get("sample_path", "")
        if self.node_type in ("mixer", "output"):
            d["channel_count"] = self.params.get("channel_count", 2 if self.node_type == "mixer" else 1)
        if self.node_type == "note_gate":
            d["pitch_lo"]  = self.params.get("pitch_lo", 0)
            d["pitch_hi"]  = self.params.get("pitch_hi", 127)
            d["gate_mode"] = self.params.get("gate_mode", 0)

        # For plugin-backed nodes, pass config_params through the params dict
        # so make_node() can forward them via configure().
        # Config params must arrive as JSON strings so graph.cpp routes them
        # through Plugin::configure() (string_params path) rather than
        # set_param() (control port path), which doesn't handle config keys.
        desc = get_plugin_descriptor(self.node_type)
        config_param_ids: set = set()
        if desc:
            config_params_by_id = {cp["id"]: cp for cp in desc.get("config_params", [])}
            config_param_ids = set(config_params_by_id.keys())
            for cid in config_params_by_id:
                if cid in self.params:
                    val = self.params[cid]
                    if isinstance(val, bool):
                        val = "true" if val else "false"
                    elif not isinstance(val, str):
                        val = str(val)
                    d.setdefault("params", {})[cid] = val

        # Internal cache keys to exclude from server payload
        _internal_keys = {"sf2_path", "sample_path",
                          "channel_count", "_ports", "_stereo_map", "_dual_mono",
                          "_plugin_desc"} | config_param_ids
        param_keys = {k: v for k, v in self.params.items()
                      if k not in _internal_keys
                      and isinstance(v, (int, float))}
        if param_keys:
            d.setdefault("params", {}).update(param_keys)

        return d

    def to_dict(self) -> dict:
        # Exclude computed caches — rebuilt from _ports on load.
        # Also exclude _pattern_data — it's regenerated from _pattern_id + live state
        # on load via update_pattern_source_node(), so storing it would just be stale.
        clean_params = {k: v for k, v in self.params.items()
                        if k not in ("_stereo_map", "_dual_mono", "_plugin_desc",
                                     "_pattern_data")}
        return {
            "node_type":   self.node_type,
            "node_id":     self.node_id,
            "display_name": self.display_name,
            "x": self.x, "y": self.y,
            "params":      clean_params,
            "minimised":   self.minimised,
            "is_default_synth": self.is_default_synth,
            "hidden_ports": list(self.hidden_ports),
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
            hidden_ports=set(d.get("hidden_ports", [])),
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

import re as _re

def default_hidden_ports_for_node(node_type: str) -> set:
    """Return port_ids that should be hidden by default for a given node type.

    Ports are hidden by default when their ControlHint is Categorical, Radio, or
    Toggle (modes/selectors rarely need to be wired up), or when the plugin
    descriptor sets show_port_default=False explicitly.
    """
    hidden = set()
    desc = get_plugin_descriptor(node_type)
    if not desc:
        return hidden
    for p in desc.get("ports", []):
        if p.get("role") not in ("input", None):
            continue
        if p.get("type") != "control":
            continue
        hint = p.get("hint", "continuous")
        if hint in ("categorical", "radio", "toggle"):
            hidden.add(p.get("id", ""))
        if not p.get("show_port_default", True):
            hidden.add(p.get("id", ""))
    return hidden


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

    # -- MIDI source node helpers --

    def find_midi_source(self) -> Optional["GraphNode"]:
        """Return the midi_source node if one exists in the graph."""
        for n in self.nodes:
            if n.node_type == "midi_source":
                return n
        return None

    # -- Default synth --

    def default_synth(self) -> Optional[GraphNode]:
        for n in self.nodes:
            if n.is_default_synth:
                return n
        for n in self.nodes:
            if n.node_type in ("fluidsynth", "sine", "sampler"):
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
        # Include split-routed beat instrument nodes
        for inst in state.beat_kit:
            if inst.split_routing:
                current_ids.add(f"beat_inst_{inst.id}")

        existing_ids = {n.node_id for n in self.nodes if n.node_type == "track_source"}
        for nid in existing_ids - current_ids:
            self.remove_node(nid)
        for t in state.tracks:
            self.add_track_source(t.id, t.name, sf2_path)
        for bt in state.beat_tracks:
            self.add_track_source(bt.id, bt.name, sf2_path)
        # Add per-instrument source nodes
        for inst in state.beat_kit:
            if inst.split_routing:
                nid = f"beat_inst_{inst.id}"
                if not self.get_node(nid):
                    existing = [n for n in self.nodes if n.node_type == "track_source"]
                    node = GraphNode(
                        node_type="track_source",
                        node_id=nid,
                        display_name=inst.name,
                        x=40, y=40 + len(existing) * 70,
                    )
                    self.add_node(node)
                    target = self.default_synth()
                    if target:
                        self.add_connection(GraphConnection(
                            from_node=nid, from_port="events_out",
                            to_node=target.node_id, to_port="events_in",
                        ))

    def sync_pattern_sources(self, state) -> None:
        """Regenerate _pattern_data for all pattern source nodes from live state.

        Called before every graph push so edits to patterns are reflected
        immediately without requiring the user to reconnect nodes.
        """
        for node in self.nodes:
            if node.node_type in ("pattern_source", "beat_pattern_source"):
                pat_id = node.params.get("_pattern_id")
                if pat_id is not None:
                    update_pattern_source_node(node, pat_id, state)

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

        connections = []
        for c in self.connections:
            from_node = id_remap.get(c.from_node, c.from_node)
            to_node   = id_remap.get(c.to_node,   c.to_node)

            src_node = self.get_node(c.from_node)
            dst_node = self.get_node(c.to_node)
            if not src_node or not dst_node:
                continue

            src_type = self._port_type_for(c.from_node, c.from_port)

            # --- PATTERN connections — pass through verbatim ---
            # wire_pattern_ports() in C++ graph.cpp handles the actual injection;
            # the connection just needs to reach the server unchanged.
            if src_type == PortType.PATTERN:
                connections.append({
                    "from_node": c.from_node,
                    "from_port": c.from_port,
                    "to_node":   c.to_node,
                    "to_port":   c.to_port,
                })
                continue

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
                    return base_id

                def _port_for_side(node_obj, port_id, side):
                    """Return the server port symbol for one channel of a stereo wire."""
                    # Plugin-backed nodes go through PluginAdapterNode, which expands
                    # an AudioStereo port named "audio_out" to "audio_out_L"/"audio_out_R".
                    # The graph-model logical port "audio" maps to server "audio_out_{side}"
                    # via _audio_port_to_lr, NOT the naive f"{port_id}_{side}" = "audio_L".
                    # Both plugin-backed and legacy nodes use the same _audio_port_to_lr mapping.
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
