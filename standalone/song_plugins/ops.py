"""Operation dataclasses for the song plugin system.

Every user-visible mutation a plugin can perform is one of these.
Frozen, so plugins can construct and return them without the executor
worrying about aliasing.

New-entity ops (Create*) carry no ID of their own; the executor's
``apply_ops`` returns a parallel list whose entry for each Create*
is the newly allocated integer id. Non-Create ops return None.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple


# ---------------------------------------------------------------------------
# Notes (within a Pattern)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AddNote:
    pattern_id: int
    pitch: int
    start: float
    duration: float
    velocity: int = 100
    lyric: str = ''
    bend: Tuple[Tuple[float, float], ...] = ()
    tags: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MoveNote:
    pattern_id: int
    note_id: int
    new_start: float
    new_pitch: int


@dataclass(frozen=True)
class ResizeNote:
    pattern_id: int
    note_id: int
    new_duration: float


@dataclass(frozen=True)
class DeleteNote:
    pattern_id: int
    note_id: int


@dataclass(frozen=True)
class SetNoteVelocity:
    pattern_id: int
    note_id: int
    velocity: int


@dataclass(frozen=True)
class SetNoteLyric:
    pattern_id: int
    note_id: int
    lyric: str


@dataclass(frozen=True)
class SetNoteBend:
    pattern_id: int
    note_id: int
    bend: Tuple[Tuple[float, float], ...]


@dataclass(frozen=True)
class SplitNote:
    pattern_id: int
    note_id: int
    split_offset: float


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreatePattern:
    name: str
    length: float
    color: Optional[str] = None
    key: str = 'C'
    scale: str = 'major'


@dataclass(frozen=True)
class RenamePattern:
    pattern_id: int
    name: str


@dataclass(frozen=True)
class ResizePattern:
    pattern_id: int
    new_length: float


@dataclass(frozen=True)
class DeletePattern:
    pattern_id: int


@dataclass(frozen=True)
class DuplicatePattern:
    pattern_id: int


@dataclass(frozen=True)
class SetPatternKeyScale:
    pattern_id: int
    key: str
    scale: str


# ---------------------------------------------------------------------------
# Placements
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreatePlacement:
    track_id: int
    pattern_id: int
    time: float
    repeats: int = 1
    transpose: int = 0
    target_key: str = 'C'
    target_scale: str = 'major'
    is_variation: bool = False


@dataclass(frozen=True)
class MovePlacement:
    placement_id: int
    new_time: float
    new_track_id: Optional[int] = None


@dataclass(frozen=True)
class SetPlacementRepeats:
    placement_id: int
    repeats: int


@dataclass(frozen=True)
class SetPlacementTranspose:
    placement_id: int
    transpose: int


@dataclass(frozen=True)
class DeletePlacement:
    placement_id: int


# ---------------------------------------------------------------------------
# Variations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateVariation:
    parent_pattern_id: int
    name: Optional[str] = None


@dataclass(frozen=True)
class DeleteVariation:
    variation_id: int


@dataclass(frozen=True)
class FlattenVariation:
    variation_id: int


@dataclass(frozen=True)
class VariationAddNote:
    variation_id: int
    pitch: int
    start: float
    duration: float
    velocity: int = 100
    lyric: str = ''
    tags: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VariationDeleteNote:
    variation_id: int
    note_id: int


@dataclass(frozen=True)
class VariationModifyNote:
    variation_id: int
    note_id: int
    d_start: float = 0.0
    d_duration: float = 0.0
    d_pitch: int = 0
    d_velocity: int = 0


@dataclass(frozen=True)
class VariationSplitNote:
    variation_id: int
    note_id: int
    split_offset: float


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateTrack:
    name: str
    channel: int = 0
    bank: int = 0
    program: int = 0
    volume: int = 100


@dataclass(frozen=True)
class RenameTrack:
    track_id: int
    name: str


@dataclass(frozen=True)
class DeleteTrack:
    track_id: int


@dataclass(frozen=True)
class SetTrackInstrument:
    track_id: int
    bank: int
    program: int


@dataclass(frozen=True)
class SetTrackVolume:
    track_id: int
    volume: int


# ---------------------------------------------------------------------------
# Automation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateAutomationTrack:
    name: str
    target: Optional[str] = None


@dataclass(frozen=True)
class DeleteAutomationTrack:
    track_id: int


@dataclass(frozen=True)
class RenameAutomationTrack:
    track_id: int
    name: str


@dataclass(frozen=True)
class CreateAutomationPattern:
    name: str
    length: float = 4.0
    min_value: float = 0.0
    max_value: float = 1.0
    color: str = '#4a90e2'


@dataclass(frozen=True)
class DeleteAutomationPattern:
    pattern_id: int


@dataclass(frozen=True)
class ResizeAutomationPattern:
    pattern_id: int
    new_length: float


@dataclass(frozen=True)
class SetAutomationPoints:
    pattern_id: int
    # Each point: (time, value, curve)
    points: Tuple[Tuple[float, float, str], ...]


@dataclass(frozen=True)
class CreateAutomationPlacement:
    track_id: int
    pattern_id: int
    time: float
    repeats: int = 1


@dataclass(frozen=True)
class MoveAutomationPlacement:
    placement_id: int
    new_time: float
    new_track_id: Optional[int] = None


@dataclass(frozen=True)
class DeleteAutomationPlacement:
    placement_id: int


# ---------------------------------------------------------------------------
# Beat
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateBeatPattern:
    name: str
    length: float
    subdivision: int = 4
    color: Optional[str] = None


@dataclass(frozen=True)
class DeleteBeatPattern:
    pattern_id: int


@dataclass(frozen=True)
class ResizeBeatPattern:
    pattern_id: int
    new_length: float


@dataclass(frozen=True)
class SetBeatStep:
    pattern_id: int
    inst_id: int
    step: int
    velocity: int


@dataclass(frozen=True)
class SetBeatRow:
    pattern_id: int
    inst_id: int
    velocities: Tuple[int, ...]


@dataclass(frozen=True)
class CreateBeatPlacement:
    track_id: int
    pattern_id: int
    time: float
    repeats: int = 1


@dataclass(frozen=True)
class MoveBeatPlacement:
    placement_id: int
    new_time: float
    new_track_id: Optional[int] = None


@dataclass(frozen=True)
class SetBeatPlacementRepeats:
    placement_id: int
    repeats: int


@dataclass(frozen=True)
class DeleteBeatPlacement:
    placement_id: int


# The set of all op types — used by executor for dispatch.
ALL_OP_TYPES = (
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
    CreateAutomationPlacement, MoveAutomationPlacement, DeleteAutomationPlacement,
    CreateBeatPattern, DeleteBeatPattern, ResizeBeatPattern,
    SetBeatStep, SetBeatRow,
    CreateBeatPlacement, MoveBeatPlacement, SetBeatPlacementRepeats,
    DeleteBeatPlacement,
)
