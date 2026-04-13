"""Public surface for song plugins.

Plugins should only import from ``song_plugins.api``. Everything a plugin
needs — views, scope types, progress protocol, manifest dataclasses,
operation dataclasses — is re-exported here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import (
    Any, Callable, ClassVar, Iterator, Literal, Optional, Protocol, Tuple,
    TYPE_CHECKING,
)

from .ops import (
    AddNote, MoveNote, ResizeNote, DeleteNote, SetNoteVelocity,
    SetNoteLyric, SetNoteBend, SplitNote,
    CreatePattern, RenamePattern, ResizePattern, DeletePattern,
    DuplicatePattern, SetPatternKeyScale,
    CreatePlacement, MovePlacement, SetPlacementRepeats,
    SetPlacementTranspose, DeletePlacement,
    CreateVariation, DeleteVariation, FlattenVariation,
    VariationAddNote, VariationDeleteNote, VariationModifyNote,
    VariationSplitNote,
    CreateTrack, RenameTrack, DeleteTrack,
    SetTrackInstrument, SetTrackVolume,
    CreateAutomationTrack, DeleteAutomationTrack, RenameAutomationTrack,
    CreateAutomationPattern, DeleteAutomationPattern, ResizeAutomationPattern,
    SetAutomationPoints,
    CreateAutomationPlacement, MoveAutomationPlacement,
    DeleteAutomationPlacement,
    CreateBeatPattern, DeleteBeatPattern, ResizeBeatPattern,
    SetBeatStep, SetBeatRow,
    CreateBeatPlacement, MoveBeatPlacement, SetBeatPlacementRepeats,
    DeleteBeatPlacement,
)


# ---------------------------------------------------------------------------
# Scope + selection
# ---------------------------------------------------------------------------

ScopeKind = Literal["whole", "range", "tracks", "selection", "notes", "placements"]


@dataclass(frozen=True)
class Scope:
    kind: ScopeKind
    start_beat: Optional[float] = None
    end_beat: Optional[float] = None
    track_ids: Optional[Tuple[int, ...]] = None
    note_ids: Optional[Tuple[int, ...]] = None
    placement_ids: Optional[Tuple[int, ...]] = None


@dataclass(frozen=True)
class SelectionSnapshot:
    notes: frozenset                     # frozenset[int] of note_ids
    placements: frozenset                 # frozenset[int] mixed placement ids
    primary: Literal["notes", "placements", "beat_placements",
                     "automation_placements", "none"]
    current_pattern_id: Optional[int]
    current_variation_id: Optional[int]
    current_beat_pattern_id: Optional[int]
    current_auto_pattern_id: Optional[int]


class SelectionMismatch(Exception):
    """Raised when a plugin's declared selection_kinds don't match what's selected."""


class SelectionEmpty(Exception):
    """Raised when a plugin declares scope='selection' but nothing is selected."""


# ---------------------------------------------------------------------------
# Progress protocol
# ---------------------------------------------------------------------------

class Progress(Protocol):
    def phase(self, name: str) -> None: ...
    def update(self, fraction: float, message: Optional[str] = None) -> None: ...
    @property
    def cancelled(self) -> bool: ...


# ---------------------------------------------------------------------------
# Read-only views
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackView:
    id: int
    name: str
    channel: int
    bank: int
    program: int
    volume: int


@dataclass(frozen=True)
class PatternView:
    id: int
    name: str
    length: float
    color: str
    key: str
    scale: str
    note_ids: Tuple[int, ...]


@dataclass(frozen=True)
class PlacementView:
    id: int
    track_id: int
    pattern_id: int
    time: float
    transpose: int
    repeats: int
    target_key: str
    target_scale: str
    is_variation: bool
    duration_beats: float


@dataclass(frozen=True)
class VariationView:
    id: int
    name: str
    parent_id: int
    color: str
    modification_note_ids: Tuple[int, ...]
    deleted_note_ids: Tuple[int, ...]
    added_note_ids: Tuple[int, ...]
    split_note_ids: Tuple[int, ...]


@dataclass(frozen=True)
class BeatInstrumentView:
    id: int
    name: str
    channel: int
    bank: int
    program: int
    pitch: int
    velocity: int


@dataclass(frozen=True)
class BeatPatternView:
    id: int
    name: str
    length: float
    subdivision: int
    color: str
    grid: dict  # {inst_id: tuple of ints}


@dataclass(frozen=True)
class BeatTrackView:
    id: int
    name: str


@dataclass(frozen=True)
class BeatPlacementView:
    id: int
    track_id: int
    pattern_id: int
    time: float
    repeats: int
    duration_beats: float


@dataclass(frozen=True)
class AutomationTrackView:
    id: int
    name: str
    target: Optional[str]


@dataclass(frozen=True)
class AutomationPointView:
    time: float
    value: float
    curve: str


@dataclass(frozen=True)
class AutomationPatternView:
    id: int
    name: str
    length: float
    color: str
    min_value: float
    max_value: float
    points: Tuple[AutomationPointView, ...]


@dataclass(frozen=True)
class AutomationPlacementView:
    id: int
    track_id: int
    pattern_id: int
    time: float
    repeats: int
    duration_beats: float


@dataclass(frozen=True)
class ResolvedNote:
    note_id: int
    pitch: int
    start_beat: float
    duration_beats: float
    velocity: int
    lyric: str
    bend: Tuple[Tuple[float, float], ...]
    track_id: int
    placement_id: int
    source_id: int
    source_kind: Literal["pattern", "variation"]
    repeat_index: int


@dataclass(frozen=True)
class BeatEvent:
    inst_id: int
    pitch: int
    start_beat: float
    velocity: int
    track_id: int
    placement_id: int
    pattern_id: int
    step: int
    repeat_index: int


# ---------------------------------------------------------------------------
# Tempo map
# ---------------------------------------------------------------------------

class TempoMapView:
    """Computed from state.bpm + optional tempo automation track."""

    def __init__(self, default_bpm: float,
                 segments):  # list[(start_beat, end_beat_or_inf, bpm_at_start, bpm_at_end, curve)]
        self._default_bpm = float(default_bpm)
        self._segments = segments  # may be empty

    def bpm_at(self, beat: float) -> float:
        if not self._segments:
            return self._default_bpm
        # Find segment containing beat.
        for s_start, s_end, bpm_a, bpm_b, curve in self._segments:
            if s_start <= beat < s_end:
                if curve == 'step' or s_end == s_start:
                    return bpm_a
                t = (beat - s_start) / (s_end - s_start)
                return bpm_a + (bpm_b - bpm_a) * t
        # Past the last segment — hold the final value.
        return self._segments[-1][3]

    def beat_to_seconds(self, beat: float) -> float:
        if beat <= 0:
            return 0.0
        if not self._segments:
            return beat * 60.0 / self._default_bpm
        seconds = 0.0
        cursor = 0.0
        for s_start, s_end, bpm_a, bpm_b, curve in self._segments:
            if beat <= cursor:
                break
            # Gap before this segment (if any) uses default bpm.
            if cursor < s_start:
                span_end = min(s_start, beat)
                seconds += (span_end - cursor) * 60.0 / self._default_bpm
                cursor = span_end
                if cursor >= beat:
                    break
            seg_end = min(s_end, beat)
            span = seg_end - cursor
            if span > 0:
                if curve == 'step' or s_end == s_start:
                    seconds += span * 60.0 / max(bpm_a, 1e-6)
                else:
                    t0 = (cursor - s_start) / (s_end - s_start)
                    t1 = (seg_end - s_start) / (s_end - s_start)
                    # Average of 60/bpm over [t0, t1] evaluated numerically
                    # with small-step trapezoidal integration.
                    steps = max(8, int(span * 16))
                    acc = 0.0
                    prev_inv = 60.0 / max(bpm_a + (bpm_b - bpm_a) * t0, 1e-6)
                    for i in range(1, steps + 1):
                        ti = t0 + (t1 - t0) * (i / steps)
                        inv = 60.0 / max(bpm_a + (bpm_b - bpm_a) * ti, 1e-6)
                        acc += 0.5 * (prev_inv + inv)
                        prev_inv = inv
                    seconds += acc * span / steps
                cursor = seg_end
            if cursor >= beat:
                break
        if cursor < beat:
            # Past last segment — hold last bpm.
            bpm = self._segments[-1][3] if self._segments else self._default_bpm
            seconds += (beat - cursor) * 60.0 / max(bpm, 1e-6)
        return seconds

    def seconds_to_beat(self, seconds: float) -> float:
        if seconds <= 0:
            return 0.0
        # Simple bisection on beat_to_seconds.
        lo, hi = 0.0, 1.0
        # Grow hi until we overshoot.
        while self.beat_to_seconds(hi) < seconds:
            hi *= 2.0
            if hi > 1e9:
                break
        for _ in range(64):
            mid = (lo + hi) * 0.5
            if self.beat_to_seconds(mid) < seconds:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# SongView — forward declared; real implementation in song_view.py
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from .song_view import SongView
else:
    SongView = "SongView"  # resolved lazily; plugins type-hint against it by name


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

Schema = Literal[
    "scalar_curve", "multi_curve", "grid2d", "events",
    "note_tags", "placement_tags", "stats", "custom",
]
MetaDep = Literal["midi", "structure", "tempo", "tracks", "automation", "beat"]


@dataclass
class Annotation:
    id: str
    plugin_id: str
    instance_id: str
    title: str
    schema: Schema
    data: Any
    render_hint: dict = field(default_factory=dict)
    persistence: Literal["transient", "cached", "authoritative"] = "transient"
    declared_deps: Tuple[MetaDep, ...] = ()
    live: bool = False
    stale: bool = False
    status: Literal["idle", "running", "ok", "error"] = "idle"
    last_run_ms: Optional[int] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Plugin manifest + base class
# ---------------------------------------------------------------------------

ParamType = Literal[
    "int", "float", "bool", "enum", "string", "beat_range", "track_select",
]
Capability = Literal["analyze", "transform", "generate"]
Persistence = Literal["transient", "cached", "authoritative"]
ScopeSpec = Literal["whole", "range", "tracks", "selection"]
SelectionKind = Literal["notes", "placements"]


@dataclass(frozen=True)
class ParamSpec:
    key: str
    type: ParamType
    label: str
    default: Any
    min: Any = None
    max: Any = None
    choices: Optional[Tuple] = None
    help: Optional[str] = None
    visible_when: Optional[dict] = None


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    description: str
    capabilities: Tuple[Capability, ...]
    schemas: Tuple[Schema, ...] = ()
    params: Tuple[ParamSpec, ...] = ()
    scopes: Tuple[ScopeSpec, ...] = ("whole",)
    selection_kinds: Tuple[SelectionKind, ...] = ()
    deps: Tuple[MetaDep, ...] = ()
    live_supported: bool = False
    persistence_default: Persistence = "transient"
    custom_widget: bool = False
    broadcast_eligible: Optional[bool] = None
    author: Optional[str] = None


@dataclass
class PluginResult:
    annotation: Optional[Annotation] = None
    operations: Optional[Tuple] = None
    message: Optional[str] = None


class SongPlugin(ABC):
    manifest: ClassVar[PluginManifest]

    @abstractmethod
    def run(self, view, params: dict, progress: Progress) -> PluginResult:
        ...


# Re-export broadcast-band helpers from registry. Registry imports
# PluginManifest from here, so we defer this import until after the
# dataclass definitions above are in place.
from .registry import (  # noqa: E402  (intentional late import)
    BROADCAST_ELIGIBLE_SCHEMAS, is_broadcast_eligible,
)


__all__ = [
    # Scope / selection
    "Scope", "ScopeKind", "SelectionSnapshot",
    "SelectionMismatch", "SelectionEmpty",
    # Progress
    "Progress",
    # Views
    "TrackView", "PatternView", "PlacementView", "VariationView",
    "BeatInstrumentView", "BeatPatternView", "BeatTrackView",
    "BeatPlacementView", "AutomationTrackView", "AutomationPatternView",
    "AutomationPlacementView", "AutomationPointView",
    "ResolvedNote", "BeatEvent", "TempoMapView", "SongView",
    # Annotations
    "Annotation", "Schema", "MetaDep",
    # Plugin
    "ParamSpec", "ParamType", "Capability", "Persistence",
    "ScopeSpec", "SelectionKind",
    "PluginManifest", "PluginResult", "SongPlugin",
    # Broadcast-band eligibility
    "BROADCAST_ELIGIBLE_SCHEMAS", "is_broadcast_eligible",
    # Ops
    "AddNote", "MoveNote", "ResizeNote", "DeleteNote", "SetNoteVelocity",
    "SetNoteLyric", "SetNoteBend", "SplitNote",
    "CreatePattern", "RenamePattern", "ResizePattern", "DeletePattern",
    "DuplicatePattern", "SetPatternKeyScale",
    "CreatePlacement", "MovePlacement", "SetPlacementRepeats",
    "SetPlacementTranspose", "DeletePlacement",
    "CreateVariation", "DeleteVariation", "FlattenVariation",
    "VariationAddNote", "VariationDeleteNote", "VariationModifyNote",
    "VariationSplitNote",
    "CreateTrack", "RenameTrack", "DeleteTrack",
    "SetTrackInstrument", "SetTrackVolume",
    "CreateAutomationTrack", "DeleteAutomationTrack",
    "RenameAutomationTrack",
    "CreateAutomationPattern", "DeleteAutomationPattern",
    "ResizeAutomationPattern", "SetAutomationPoints",
    "CreateAutomationPlacement", "MoveAutomationPlacement",
    "DeleteAutomationPlacement",
    "CreateBeatPattern", "DeleteBeatPattern", "ResizeBeatPattern",
    "SetBeatStep", "SetBeatRow",
    "CreateBeatPlacement", "MoveBeatPlacement",
    "SetBeatPlacementRepeats", "DeleteBeatPlacement",
]
