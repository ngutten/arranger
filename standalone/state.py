"""Central state model for the standalone arranger.

Replaces the JavaScript global `S` object with Python dataclasses.
Supports observer pattern for UI updates and JSON serialization
compatible with the web version's project format (v:3).
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Callable, Optional


class IndexedList(list):
    """A list that maintains an {id: item} index for O(1) lookups.

    Items must have an ``id`` attribute. The index is rebuilt whenever
    the list is replaced wholesale (via the property setter on AppState)
    and kept in sync by overriding mutating methods.

    For iteration, indexing, slicing, len(), ``in``, and list
    comprehensions this behaves identically to a plain list.
    """

    def __init__(self, items=()):
        super().__init__(items)
        self._idx: dict = {item.id: item for item in self}

    def get(self, item_id):
        """O(1) lookup by id. Returns None if not found."""
        return self._idx.get(item_id)

    def _rebuild_index(self):
        self._idx = {item.id: item for item in self}

    # -- mutating overrides --

    def append(self, item):
        super().append(item)
        self._idx[item.id] = item

    def extend(self, items):
        items = list(items)  # consume generator once
        super().extend(items)
        for item in items:
            self._idx[item.id] = item

    def remove(self, item):
        super().remove(item)
        self._idx.pop(item.id, None)

    def pop(self, index=-1):
        item = super().pop(index)
        self._idx.pop(item.id, None)
        return item

    def insert(self, index, item):
        super().insert(index, item)
        self._idx[item.id] = item

    def clear(self):
        super().clear()
        self._idx.clear()

    def __delitem__(self, index):
        item = self[index]
        super().__delitem__(index)
        if hasattr(item, 'id'):
            self._idx.pop(item.id, None)
        elif isinstance(item, list):
            # slice deletion
            self._rebuild_index()

    def __setitem__(self, index, value):
        # Handle replacing an item at an index
        if isinstance(index, int):
            old = self[index]
            self._idx.pop(old.id, None)
            super().__setitem__(index, value)
            self._idx[value.id] = value
        else:
            super().__setitem__(index, value)
            self._rebuild_index()

    def __iadd__(self, other):
        items = list(other)
        super().__iadd__(items)
        for item in items:
            self._idx[item.id] = item
        return self


# Music constants
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

SCALES = {
    'major': [0, 2, 4, 5, 7, 9, 11],
    'minor': [0, 2, 3, 5, 7, 8, 10],
    'dorian': [0, 2, 3, 5, 7, 9, 10],
    'mixolydian': [0, 2, 4, 5, 7, 9, 10],
    'phrygian': [0, 1, 3, 5, 7, 8, 10],
    'lydian': [0, 2, 4, 6, 7, 9, 11],
    'pentatonic': [0, 2, 4, 7, 9],
    'blues': [0, 3, 5, 6, 7, 10],
    'chromatic': [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
}

PALETTE = [
    '#e94560', '#533483', '#0f3460', '#00b4d8', '#06d6a0', '#ffd166',
    '#ef476f', '#118ab2', '#9b5de5', '#f15bb5', '#00f5d4', '#fee440',
]

GM_NAMES = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano",
    "Honky-tonk Piano", "Electric Piano 1", "Electric Piano 2", "Harpsichord",
    "Clavinet", "Celesta", "Glockenspiel", "Music Box", "Vibraphone",
    "Marimba", "Xylophone", "Tubular Bells", "Dulcimer", "Drawbar Organ",
    "Percussive Organ", "Rock Organ", "Church Organ", "Reed Organ",
    "Accordion", "Harmonica", "Tango Accordion", "Acoustic Guitar (nylon)",
    "Acoustic Guitar (steel)", "Electric Guitar (jazz)", "Electric Guitar (clean)",
    "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar",
    "Guitar Harmonics", "Acoustic Bass", "Electric Bass (finger)",
    "Electric Bass (pick)", "Fretless Bass", "Slap Bass 1", "Slap Bass 2",
    "Synth Bass 1", "Synth Bass 2", "Violin", "Viola", "Cello", "Contrabass",
    "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "Synth Strings 1",
    "Synth Strings 2", "Choir Aahs", "Voice Oohs", "Synth Choir",
    "Orchestra Hit", "Trumpet", "Trombone", "Tuba", "Muted Trumpet",
    "French Horn", "Brass Section", "Synth Brass 1", "Synth Brass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe",
    "English Horn", "Bassoon", "Clarinet", "Piccolo", "Flute", "Recorder",
    "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)",
    "Lead 4 (chiff)", "Lead 5 (charang)", "Lead 6 (voice)",
    "Lead 7 (fifths)", "Lead 8 (bass+lead)", "Pad 1 (new age)",
    "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)",
    "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)", "FX 1 (rain)",
    "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
    "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle",
    "Shanai", "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock",
    "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
    "Telephone Ring", "Helicopter", "Applause", "Gunshot",
]


def note_pc(name):
    """Get pitch class index (0-11) for a note name."""
    return NOTE_NAMES.index(name) if name in NOTE_NAMES else 0


def scale_set(root, scale_name):
    """Get the set of pitch classes in a scale."""
    r = note_pc(root)
    intervals = SCALES.get(scale_name, SCALES['major'])
    return set((r + i) % 12 for i in intervals)


def key_shift(from_key, to_key):
    """Calculate semitone shift between two keys."""
    return ((note_pc(to_key) - note_pc(from_key)) % 12 + 12) % 12


def preset_name(bank, program, sf2_presets=None):
    """Get the name for a bank/program combination."""
    if sf2_presets:
        for p in sf2_presets:
            if p['bank'] == bank and p['program'] == program:
                return p['name']
    if 0 <= program < len(GM_NAMES):
        return GM_NAMES[program]
    return f'B{bank}/P{program}'


def vel_color(v):
    """Convert velocity (1-127) to an RGB hex color string."""
    t = v / 127
    if t < 0.33:
        u = t / 0.33
        r, g, b = 60, int(60 + u * 160), int(200 - u * 60)
    elif t < 0.66:
        u = (t - 0.33) / 0.33
        r, g, b = int(60 + u * 180), int(220 - u * 40), int(140 - u * 100)
    else:
        u = (t - 0.66) / 0.34
        r, g, b = 240, int(180 - u * 120), int(40 - u * 40)
    return f'#{max(0,min(255,r)):02x}{max(0,min(255,g)):02x}{max(0,min(255,b)):02x}'


@dataclass
class Note:
    pitch: int
    start: float
    duration: float
    velocity: int = 100
    # Pitch bend control points: list of [beat_offset, semitones] pairs.
    # beat_offset is relative to note start, clamped to [0, duration].
    # semitones is in [-2.0, 2.0]. Empty list = no bend.
    bend: list = field(default_factory=list)
    # Lyric syllable associated with this note (for singing synthesis / export).
    lyric: str = ''
    # Stable identity for variation diff tracking. 0 = unassigned.
    note_id: int = 0
    # Free-form metadata keyed by namespace. Values must be JSON-serialisable.
    # Descriptive only — never load-bearing for playback.
    tags: dict = field(default_factory=dict)
    # Per-note attributes (load-bearing): {attr_id: float}. Onset-latched values
    # delivered to the synth as NoteAttr events. Continuous attrs (e.g. 'attack')
    # multiply the synth param (neutral 1.0); categorical attrs (e.g. 'excitation')
    # override it. Absent attr = synth default. See note_attr_latch.h (C++ side).
    attrs: dict = field(default_factory=dict)

    def to_dict(self):
        d = {'pitch': self.pitch, 'start': self.start,
             'duration': self.duration, 'velocity': self.velocity}
        if self.bend:
            d['bend'] = self.bend
        if self.lyric:
            d['lyric'] = self.lyric
        if self.note_id:
            d['noteId'] = self.note_id
        if self.tags:
            d['tags'] = self.tags
        if self.attrs:
            d['attrs'] = self.attrs
        return d

    @staticmethod
    def from_dict(d):
        return Note(pitch=d['pitch'], start=d['start'],
                    duration=d['duration'], velocity=d.get('velocity', 100),
                    bend=d.get('bend', []),
                    lyric=d.get('lyric', ''),
                    note_id=d.get('noteId', 0),
                    tags=dict(d.get('tags', {})),
                    attrs=dict(d.get('attrs', {})))


@dataclass
class Pattern:
    id: int
    name: str
    length: float
    notes: list
    color: str
    key: str = 'C'
    scale: str = 'major'
    preview_mode: str = 'sine'
    overlay_mode: str = 'playing'  # 'off', 'playing', 'always'

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'length': self.length,
            'notes': [n.to_dict() for n in self.notes],
            'color': self.color, 'key': self.key, 'scale': self.scale,
            'previewMode': self.preview_mode,
            'overlayMode': self.overlay_mode,
        }

    def ensure_note_ids(self, id_fn):
        """Assign IDs to any note with note_id == 0."""
        for n in self.notes:
            if n.note_id == 0:
                n.note_id = id_fn()

    @staticmethod
    def from_dict(d):
        return Pattern(
            id=d['id'], name=d['name'], length=d['length'],
            notes=[Note.from_dict(n) for n in d.get('notes', [])],
            color=d.get('color', PALETTE[0]),
            key=d.get('key', 'C'), scale=d.get('scale', 'major'),
            preview_mode=d.get('previewMode', 'sine'),
            overlay_mode=d.get('overlayMode', 'playing'),
        )


@dataclass
class BeatPattern:
    id: int
    name: str
    length: float
    subdivision: int
    color: str
    grid: dict  # {instrument_id (str): [velocity per step]}

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'length': self.length,
            'subdivision': self.subdivision, 'color': self.color,
            'grid': {str(k): list(v) for k, v in self.grid.items()},
        }

    @staticmethod
    def from_dict(d):
        grid = {}
        for k, v in d.get('grid', {}).items():
            grid[int(k)] = list(v)
        return BeatPattern(
            id=d['id'], name=d['name'], length=d['length'],
            subdivision=d.get('subdivision', 4),
            color=d.get('color', PALETTE[0]), grid=grid,
        )


@dataclass
class Track:
    id: int
    name: str
    channel: int = 0
    bank: int = 0
    program: int = 0
    volume: int = 100

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'channel': self.channel,
            'bank': self.bank, 'program': self.program, 'volume': self.volume,
        }

    @staticmethod
    def from_dict(d):
        return Track(
            id=d['id'], name=d['name'], channel=d.get('channel', 0),
            bank=d.get('bank', 0), program=d.get('program', 0),
            volume=d.get('volume', 100),
        )


@dataclass
class BeatTrack:
    id: int
    name: str

    def to_dict(self):
        return {'id': self.id, 'name': self.name}

    @staticmethod
    def from_dict(d):
        return BeatTrack(id=d['id'], name=d['name'])


@dataclass
class Placement:
    id: int
    track_id: int
    pattern_id: int
    time: float = 0
    transpose: int = 0
    repeats: int = 1
    target_key: str = 'C'
    target_scale: str = 'major'
    is_variation: bool = False  # if True, pattern_id refers to a Variation

    def to_dict(self):
        d = {
            'id': self.id, 'trackId': self.track_id,
            'patternId': self.pattern_id, 'time': self.time,
            'transpose': self.transpose, 'repeats': self.repeats,
            'targetKey': self.target_key, 'targetScale': self.target_scale,
        }
        if self.is_variation:
            d['isVariation'] = True
        return d

    @staticmethod
    def from_dict(d):
        return Placement(
            id=d['id'], track_id=d['trackId'], pattern_id=d['patternId'],
            time=d.get('time', 0), transpose=d.get('transpose', 0),
            repeats=d.get('repeats', 1), target_key=d.get('targetKey', 'C'),
            target_scale=d.get('targetScale', 'major'),
            is_variation=d.get('isVariation', False),
        )


@dataclass
class BeatPlacement:
    id: int
    track_id: int
    pattern_id: int
    time: float = 0
    repeats: int = 1

    def to_dict(self):
        return {
            'id': self.id, 'trackId': self.track_id,
            'patternId': self.pattern_id, 'time': self.time,
            'repeats': self.repeats,
        }

    @staticmethod
    def from_dict(d):
        return BeatPlacement(
            id=d['id'], track_id=d['trackId'], pattern_id=d['patternId'],
            time=d.get('time', 0), repeats=d.get('repeats', 1),
        )


@dataclass
class BeatInstrument:
    id: int
    name: str
    channel: int = 9
    bank: int = 0
    program: int = 0
    pitch: int = 36
    velocity: int = 100
    split_routing: bool = False

    def to_dict(self):
        d = {
            'id': self.id, 'name': self.name, 'channel': self.channel,
            'bank': self.bank, 'program': self.program,
            'pitch': self.pitch, 'velocity': self.velocity,
        }
        if self.split_routing:
            d['split_routing'] = True
        return d

    @staticmethod
    def from_dict(d):
        return BeatInstrument(
            id=d['id'], name=d['name'], channel=d.get('channel', 9),
            bank=d.get('bank', 0), program=d.get('program', 0),
            pitch=d.get('pitch', 36), velocity=d.get('velocity', 100),
            split_routing=d.get('split_routing', False),
        )


@dataclass
class AutomationPoint:
    """Single point in an automation curve.
    
    Similar to pitch bend points in Note.bend, but for general automation.
    time is in beats relative to pattern start.
    value is the control value (typically 0.0-1.0, but user-configurable range).
    curve determines interpolation: 'linear', 'step', 'smooth' (cubic spline).
    """
    time: float
    value: float
    curve: str = 'linear'

    def to_dict(self):
        return {'time': self.time, 'value': self.value, 'curve': self.curve}

    @staticmethod
    def from_dict(d):
        return AutomationPoint(
            time=d['time'], value=d['value'], curve=d.get('curve', 'linear')
        )


@dataclass
class AutomationPattern:
    """Automation curve pattern - like Pattern/BeatPattern but for control signals.
    
    Points define the curve shape. Interpolation happens between points.
    min_value/max_value define the editing range (not clamped on output).
    """
    id: int
    name: str
    length: float = 4.0
    color: str = '#4a90e2'
    points: list = field(default_factory=list)  # list[AutomationPoint]
    min_value: float = 0.0
    max_value: float = 1.0

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'length': self.length,
            'color': self.color,
            'points': [p.to_dict() for p in self.points],
            'minValue': self.min_value, 'maxValue': self.max_value,
        }

    @staticmethod
    def from_dict(d):
        return AutomationPattern(
            id=d['id'], name=d['name'], length=d['length'],
            color=d.get('color', '#4a90e2'),
            points=[AutomationPoint.from_dict(p) for p in d.get('points', [])],
            min_value=d.get('minValue', 0.0),
            max_value=d.get('maxValue', 1.0),
        )


@dataclass
class AutomationTrack:
    """Track containing automation pattern placements.

    ControlSource nodes in the signal graph reference automation tracks by ID.
    This is the inverse of the old design where tracks referenced nodes.

    target: Optional special routing.
        "tempo"            — drives project BPM.
        "output_gain:<N>"  — drives gain_N on the output mixer (N = 0-based
                             audio input channel index).
        None (default)     — track feeds control_source nodes in the graph
                             that reference it by automation_track_id.
    """
    id: int
    name: str
    target: Optional[str] = None

    def to_dict(self):
        d = {
            'id': self.id,
            'name': self.name,
        }
        if self.target is not None:
            d['target'] = self.target
        return d

    @staticmethod
    def from_dict(d):
        return AutomationTrack(
            id=d['id'],
            name=d['name'],
            target=d.get('target'),
        )


@dataclass
class AutomationPlacement:
    """Instance of an automation pattern on an automation track."""
    id: int
    track_id: int
    pattern_id: int
    time: float = 0
    repeats: int = 1

    def to_dict(self):
        return {
            'id': self.id, 'trackId': self.track_id,
            'patternId': self.pattern_id, 'time': self.time,
            'repeats': self.repeats,
        }

    @staticmethod
    def from_dict(d):
        return AutomationPlacement(
            id=d['id'], track_id=d['trackId'], pattern_id=d['patternId'],
            time=d.get('time', 0), repeats=d.get('repeats', 1),
        )


@dataclass
class NoteDelta:
    """Modification to a parent note in a variation."""
    note_id: int           # references parent Note.note_id
    d_start: float = 0.0   # additive offset to start
    d_duration: float = 0.0
    d_pitch: int = 0       # semitones
    d_velocity: int = 0
    bend: list = None      # None = inherit parent; list = override
    lyric: str = None       # None = inherit parent; str = override
    tags: dict = None       # None = inherit parent; dict = full replacement
    attrs: dict = None      # None = inherit parent; dict = full replacement

    def to_dict(self):
        d = {'noteId': self.note_id}
        if self.d_start: d['dStart'] = self.d_start
        if self.d_duration: d['dDuration'] = self.d_duration
        if self.d_pitch: d['dPitch'] = self.d_pitch
        if self.d_velocity: d['dVelocity'] = self.d_velocity
        if self.bend is not None: d['bend'] = self.bend
        if self.lyric is not None: d['lyric'] = self.lyric
        if self.tags is not None: d['tags'] = self.tags
        if self.attrs is not None: d['attrs'] = self.attrs
        return d

    @staticmethod
    def from_dict(d):
        return NoteDelta(
            note_id=d['noteId'],
            d_start=d.get('dStart', 0.0),
            d_duration=d.get('dDuration', 0.0),
            d_pitch=d.get('dPitch', 0),
            d_velocity=d.get('dVelocity', 0),
            bend=d.get('bend'),
            lyric=d.get('lyric'),
            tags=d.get('tags'),
            attrs=d.get('attrs'),
        )


@dataclass
class AddedNote:
    """A new note added in a variation."""
    note_id: int           # own unique ID
    pitch: int
    start: float
    duration: float
    velocity: int = 100
    bend: list = field(default_factory=list)
    lyric: str = ''
    ref_note_id: int = 0   # parent note this is bound to (0 = unbound)
    ref_bind: str = 'pitch' # 'full' (transforms with parent) or 'pitch' (pitch-follows only)
    # Offsets from reference note at binding time. None = legacy (compute on the fly).
    ref_pitch_offset: int = None
    ref_start_offset: float = None
    ref_dur_offset: float = None
    tags: dict = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)

    def to_dict(self):
        d = {'noteId': self.note_id, 'pitch': self.pitch, 'start': self.start,
             'duration': self.duration, 'velocity': self.velocity}
        if self.bend: d['bend'] = self.bend
        if self.lyric: d['lyric'] = self.lyric
        if self.ref_note_id: d['refNoteId'] = self.ref_note_id
        if self.ref_bind != 'pitch': d['refBind'] = self.ref_bind
        if self.ref_pitch_offset is not None: d['refPitchOffset'] = self.ref_pitch_offset
        if self.ref_start_offset is not None: d['refStartOffset'] = self.ref_start_offset
        if self.ref_dur_offset is not None: d['refDurOffset'] = self.ref_dur_offset
        if self.tags: d['tags'] = self.tags
        if self.attrs: d['attrs'] = self.attrs
        return d

    @staticmethod
    def from_dict(d):
        return AddedNote(
            note_id=d['noteId'], pitch=d['pitch'], start=d['start'],
            duration=d['duration'], velocity=d.get('velocity', 100),
            bend=d.get('bend', []), lyric=d.get('lyric', ''),
            ref_note_id=d.get('refNoteId', 0),
            ref_bind=d.get('refBind', 'pitch'),
            ref_pitch_offset=d.get('refPitchOffset'),
            ref_start_offset=d.get('refStartOffset'),
            ref_dur_offset=d.get('refDurOffset'),
            tags=dict(d.get('tags', {})),
            attrs=dict(d.get('attrs', {})),
        )


@dataclass
class SplitOp:
    """Records a split of a parent note in a variation."""
    note_id: int           # parent note that was split
    split_offset: float    # beat offset within the note where split occurs
    left_delta: NoteDelta = None
    right_delta: NoteDelta = None
    right_note_id: int = 0

    def to_dict(self):
        d = {'noteId': self.note_id, 'splitOffset': self.split_offset}
        if self.left_delta: d['leftDelta'] = self.left_delta.to_dict()
        if self.right_delta: d['rightDelta'] = self.right_delta.to_dict()
        if self.right_note_id: d['rightNoteId'] = self.right_note_id
        return d

    @staticmethod
    def from_dict(d):
        ld = NoteDelta.from_dict(d['leftDelta']) if d.get('leftDelta') else None
        rd = NoteDelta.from_dict(d['rightDelta']) if d.get('rightDelta') else None
        return SplitOp(
            note_id=d['noteId'], split_offset=d['splitOffset'],
            left_delta=ld, right_delta=rd,
            right_note_id=d.get('rightNoteId', 0),
        )


@dataclass
class Variation:
    """A derived pattern that stores diffs against a parent pattern."""
    id: int
    name: str
    parent_id: int         # references Pattern.id
    color: str
    modifications: list = field(default_factory=list)  # list[NoteDelta]
    deletions: list = field(default_factory=list)       # list[int] — note_ids suppressed
    additions: list = field(default_factory=list)       # list[AddedNote]
    splits: list = field(default_factory=list)          # list[SplitOp]

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'parentId': self.parent_id,
            'color': self.color,
            'modifications': [m.to_dict() for m in self.modifications],
            'deletions': list(self.deletions),
            'additions': [a.to_dict() for a in self.additions],
            'splits': [s.to_dict() for s in self.splits],
        }

    @staticmethod
    def from_dict(d):
        return Variation(
            id=d['id'], name=d['name'], parent_id=d['parentId'],
            color=d.get('color', PALETTE[0]),
            modifications=[NoteDelta.from_dict(m) for m in d.get('modifications', [])],
            deletions=d.get('deletions', []),
            additions=[AddedNote.from_dict(a) for a in d.get('additions', [])],
            splits=[SplitOp.from_dict(s) for s in d.get('splits', [])],
        )


class AppState:
    """Central application state with observer pattern for UI updates.

    Collections (patterns, tracks, placements, etc.) are stored as
    ``IndexedList`` instances, giving O(1) lookup by id via the
    ``find_*`` helpers while preserving ordered iteration.

    Reassigning a collection (``state.patterns = [...]``) transparently
    wraps the new list in an IndexedList, so all existing code that
    filters via comprehension continues to work unchanged.
    """

    # Names of collections that should be IndexedList-wrapped.
    _COLLECTIONS = (
        'patterns', 'tracks', 'placements',
        'beat_kit', 'beat_patterns', 'beat_tracks', 'beat_placements',
        'automation_patterns', 'automation_tracks', 'automation_placements',
        'variations',
    )

    def __init__(self):
        self.bpm: float = 120.0
        self.snap: float = 0.5
        self.ts_num: int = 4
        self.ts_den: int = 4

        self._patterns = IndexedList()
        self._tracks = IndexedList()
        self._placements = IndexedList()

        self._beat_kit = IndexedList()
        self._beat_patterns = IndexedList()
        self._beat_tracks = IndexedList()
        self._beat_placements = IndexedList()

        self._automation_patterns = IndexedList()
        self._automation_tracks = IndexedList()
        self._automation_placements = IndexedList()

        self._variations = IndexedList()

        self.sf2 = None  # SF2Info or dict with path/name/presets

        # Selection state
        self.sel_pat: Optional[int] = None
        self.sel_variation: Optional[int] = None
        self.sel_trk: Optional[int] = None
        self.sel_pl: Optional[int] = None
        self.sel_beat_pat: Optional[int] = None
        self.sel_beat_trk: Optional[int] = None
        self.sel_beat_pl: Optional[int] = None
        self.sel_auto_pat: Optional[int] = None
        self.sel_auto_trk: Optional[int] = None
        self.sel_auto_pl: Optional[int] = None

        # Editing state
        self.tool: str = 'edit'
        self.note_len: str = '0.25'
        self.last_note_len: float = 0.25
        self.default_vel: int = 100

        # Playback state
        self.playing: bool = False
        self.looping: bool = False
        self.playhead: Optional[float] = None
        self.loop_start: Optional[float] = None   # beat position, None = start of arrangement
        self.loop_end: Optional[float] = None     # beat position, None = end of arrangement

        # Signal graph — owned by GraphModel; None means use the automatic default
        # built from current tracks in ServerEngine._ensure_graph().
        self.signal_graph = None   # type: Optional[object]  (GraphModel)

        # Internal
        self._next_id: int = 1
        self._listeners: list[Callable] = []
        self._project_path: Optional[str] = None

    # -- Collection properties (auto-wrap in IndexedList on assignment) --

    @property
    def patterns(self) -> IndexedList:
        return self._patterns

    @patterns.setter
    def patterns(self, value):
        self._patterns = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def tracks(self) -> IndexedList:
        return self._tracks

    @tracks.setter
    def tracks(self, value):
        self._tracks = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def placements(self) -> IndexedList:
        return self._placements

    @placements.setter
    def placements(self, value):
        self._placements = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def beat_kit(self) -> IndexedList:
        return self._beat_kit

    @beat_kit.setter
    def beat_kit(self, value):
        self._beat_kit = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def beat_patterns(self) -> IndexedList:
        return self._beat_patterns

    @beat_patterns.setter
    def beat_patterns(self, value):
        self._beat_patterns = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def beat_tracks(self) -> IndexedList:
        return self._beat_tracks

    @beat_tracks.setter
    def beat_tracks(self, value):
        self._beat_tracks = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def beat_placements(self) -> IndexedList:
        return self._beat_placements

    @beat_placements.setter
    def beat_placements(self, value):
        self._beat_placements = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def automation_patterns(self) -> IndexedList:
        return self._automation_patterns

    @automation_patterns.setter
    def automation_patterns(self, value):
        self._automation_patterns = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def automation_tracks(self) -> IndexedList:
        return self._automation_tracks

    @automation_tracks.setter
    def automation_tracks(self, value):
        self._automation_tracks = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def automation_placements(self) -> IndexedList:
        return self._automation_placements

    @automation_placements.setter
    def automation_placements(self, value):
        self._automation_placements = value if isinstance(value, IndexedList) else IndexedList(value)

    @property
    def variations(self) -> IndexedList:
        return self._variations

    @variations.setter
    def variations(self, value):
        self._variations = value if isinstance(value, IndexedList) else IndexedList(value)

    def new_id(self) -> int:
        nid = self._next_id
        self._next_id += 1
        return nid

    def on_change(self, callback: Callable):
        self._listeners.append(callback)

    def notify(self, source=None):
        for cb in self._listeners:
            cb(source)

    # Lookup helpers — O(1) via IndexedList.get()
    def find_pattern(self, pid) -> Optional[Pattern]:
        return self._patterns.get(pid)

    def find_track(self, tid) -> Optional[Track]:
        return self._tracks.get(tid)

    def find_placement(self, plid) -> Optional[Placement]:
        return self._placements.get(plid)

    def find_beat_pattern(self, bpid) -> Optional[BeatPattern]:
        return self._beat_patterns.get(bpid)

    def find_beat_track(self, btid) -> Optional[BeatTrack]:
        return self._beat_tracks.get(btid)

    def find_beat_placement(self, bplid) -> Optional[BeatPlacement]:
        return self._beat_placements.get(bplid)

    def find_beat_instrument(self, iid) -> Optional[BeatInstrument]:
        return self._beat_kit.get(iid)

    def find_automation_pattern(self, apid) -> Optional[AutomationPattern]:
        return self._automation_patterns.get(apid)

    def find_automation_track(self, atid) -> Optional[AutomationTrack]:
        return self._automation_tracks.get(atid)

    def find_automation_placement(self, aplid) -> Optional[AutomationPlacement]:
        return self._automation_placements.get(aplid)

    def find_tempo_track(self) -> Optional[AutomationTrack]:
        """Return the first automation track with target=='tempo', or None."""
        for t in self._automation_tracks:
            if t.target == 'tempo':
                return t
        return None

    def find_variation(self, vid) -> Optional[Variation]:
        return self._variations.get(vid)

    def variations_of(self, pattern_id) -> list:
        """Return all variations whose parent_id matches pattern_id."""
        return [v for v in self._variations if v.parent_id == pattern_id]

    def compute_transpose(self, pl: Placement) -> int:
        """Compute total transposition for a placement (manual + key shift)."""
        if pl.is_variation:
            var = self.find_variation(pl.pattern_id)
            pat = self.find_pattern(var.parent_id) if var else None
        else:
            pat = self.find_pattern(pl.pattern_id)
        pk = pat.key if pat else 'C'
        tk = pl.target_key or pk
        return (pl.transpose or 0) + key_shift(pk, tk)

    def build_arrangement(self) -> dict:
        """Build arrangement dict for MIDI export / audio rendering."""
        from .ops.variations import resolve_placement_notes
        melodic_tracks = []
        for t in self.tracks:
            trk = {
                'name': t.name, 'channel': t.channel, 'bank': t.bank,
                'program': t.program, 'volume': t.volume,
                'placements': [],
            }
            for p in self.placements:
                if p.track_id != t.id:
                    continue
                notes, pat_length, pat_key, pat_scale = resolve_placement_notes(self, p)
                if notes is None:
                    continue
                trk['placements'].append({
                    'pattern': {
                        'notes': [n.to_dict() for n in notes],
                        'length': pat_length,
                    },
                    'time': p.time,
                    'transpose': self.compute_transpose(p),
                    'repeats': p.repeats or 1,
                })
            melodic_tracks.append(trk)

        beat_tracks = []
        for inst in self.beat_kit:
            placements = []
            for bp in self.beat_placements:
                bt = self.find_beat_track(bp.track_id)
                if not bt:
                    continue
                pat = self.find_beat_pattern(bp.pattern_id)
                if not pat:
                    continue
                grid = pat.grid.get(inst.id)
                if not grid or not any(v > 0 for v in grid):
                    continue
                step_dur = pat.length / len(grid)
                notes = []
                for i, v in enumerate(grid):
                    if v > 0:
                        notes.append({
                            'pitch': inst.pitch,
                            'velocity': v,
                            'start': i * step_dur,
                            'duration': step_dur * 0.8,
                        })
                placements.append({
                    'pattern': {'notes': notes, 'length': pat.length},
                    'time': bp.time,
                    'transpose': 0,
                    'repeats': bp.repeats or 1,
                })
            if placements:
                beat_tracks.append({
                    'name': inst.name, 'channel': inst.channel,
                    'bank': inst.bank, 'program': inst.program,
                    'volume': 100, 'placements': placements,
                })

        return {
            'bpm': self.bpm, 'tsNum': self.ts_num, 'tsDen': self.ts_den,
            'tracks': melodic_tracks + beat_tracks,
        }

    # Serialization
    def to_json(self) -> str:
        data = {
            'v': 3,
            'bpm': self.bpm, 'snap': self.snap,
            'tsNum': self.ts_num, 'tsDen': self.ts_den,
            'patterns': [p.to_dict() for p in self.patterns],
            'tracks': [t.to_dict() for t in self.tracks],
            'placements': [p.to_dict() for p in self.placements],
            'beatKit': [i.to_dict() for i in self.beat_kit],
            'beatPatterns': [p.to_dict() for p in self.beat_patterns],
            'beatTracks': [t.to_dict() for t in self.beat_tracks],
            'beatPlacements': [p.to_dict() for p in self.beat_placements],
            'automationPatterns': [p.to_dict() for p in self.automation_patterns],
            'automationTracks': [t.to_dict() for t in self.automation_tracks],
            'automationPlacements': [p.to_dict() for p in self.automation_placements],
            'variations': [v.to_dict() for v in self.variations],
            'sf2Path': self.sf2.path if self.sf2 else None,
            'nextId': self._next_id,
            'signalGraph': (self.signal_graph.to_dict()
                            if self.signal_graph is not None else None),
        }
        return json.dumps(data, indent=2)

    def load_json(self, text: str):
        d = json.loads(text)
        self.bpm = float(d.get('bpm', 120))
        self.snap = d.get('snap', 0.5)
        self.ts_num = d.get('tsNum', 4)
        self.ts_den = d.get('tsDen', 4)
        self.patterns = [Pattern.from_dict(p) for p in d.get('patterns', [])]
        self.tracks = [Track.from_dict(t) for t in d.get('tracks', [])]
        self.placements = [Placement.from_dict(p) for p in d.get('placements', [])]
        self.beat_kit = [BeatInstrument.from_dict(i) for i in d.get('beatKit', [])]
        self.beat_patterns = [BeatPattern.from_dict(p) for p in d.get('beatPatterns', [])]
        self.beat_tracks = [BeatTrack.from_dict(t) for t in d.get('beatTracks', [])]
        self.beat_placements = [BeatPlacement.from_dict(p) for p in d.get('beatPlacements', [])]
        self.automation_patterns = [AutomationPattern.from_dict(p) for p in d.get('automationPatterns', [])]
        self.automation_tracks = [AutomationTrack.from_dict(t) for t in d.get('automationTracks', [])]
        self.automation_placements = [AutomationPlacement.from_dict(p) for p in d.get('automationPlacements', [])]
        self.variations = [Variation.from_dict(v) for v in d.get('variations', [])]
        self._next_id = d.get('nextId', 1)
        self.sel_pat = None
        self.sel_variation = None
        self.sel_trk = self.tracks[0].id if self.tracks else None
        self.sel_pl = None
        self.sel_beat_pat = None
        self.sel_beat_trk = None
        self.sel_beat_pl = None
        self.sel_auto_pat = None
        self.sel_auto_trk = None
        self.sel_auto_pl = None
        # sf2Path is stored but the caller must reload the SF2 file
        self._sf2_path_hint = d.get('sf2Path')
        # Signal graph
        sg_data = d.get('signalGraph')
        if sg_data is not None:
            try:
                from .graph_editor import GraphModel
                self.signal_graph = GraphModel.from_dict(sg_data)
            except Exception:
                self.signal_graph = None
        else:
            self.signal_graph = None
        self.notify()
