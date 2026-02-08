"""Central state model for the standalone arranger.

Replaces the JavaScript global `S` object with Python dataclasses.
Supports observer pattern for UI updates and JSON serialization
compatible with the web version's project format (v:3).
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Callable, Optional


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

    def to_dict(self):
        return {'pitch': self.pitch, 'start': self.start,
                'duration': self.duration, 'velocity': self.velocity}

    @staticmethod
    def from_dict(d):
        return Note(pitch=d['pitch'], start=d['start'],
                    duration=d['duration'], velocity=d.get('velocity', 100))


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

    def to_dict(self):
        return {
            'id': self.id, 'trackId': self.track_id,
            'patternId': self.pattern_id, 'time': self.time,
            'transpose': self.transpose, 'repeats': self.repeats,
            'targetKey': self.target_key, 'targetScale': self.target_scale,
        }

    @staticmethod
    def from_dict(d):
        return Placement(
            id=d['id'], track_id=d['trackId'], pattern_id=d['patternId'],
            time=d.get('time', 0), transpose=d.get('transpose', 0),
            repeats=d.get('repeats', 1), target_key=d.get('targetKey', 'C'),
            target_scale=d.get('targetScale', 'major'),
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

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'channel': self.channel,
            'bank': self.bank, 'program': self.program,
            'pitch': self.pitch, 'velocity': self.velocity,
        }

    @staticmethod
    def from_dict(d):
        return BeatInstrument(
            id=d['id'], name=d['name'], channel=d.get('channel', 9),
            bank=d.get('bank', 0), program=d.get('program', 0),
            pitch=d.get('pitch', 36), velocity=d.get('velocity', 100),
        )


class AppState:
    """Central application state with observer pattern for UI updates."""

    def __init__(self):
        self.bpm: int = 120
        self.snap: float = 0.5
        self.ts_num: int = 4
        self.ts_den: int = 4

        self.patterns: list[Pattern] = []
        self.tracks: list[Track] = []
        self.placements: list[Placement] = []

        self.beat_kit: list[BeatInstrument] = []
        self.beat_patterns: list[BeatPattern] = []
        self.beat_tracks: list[BeatTrack] = []
        self.beat_placements: list[BeatPlacement] = []

        self.sf2 = None  # SF2Info or dict with path/name/presets

        # Selection state
        self.sel_pat: Optional[int] = None
        self.sel_trk: Optional[int] = None
        self.sel_pl: Optional[int] = None
        self.sel_beat_pat: Optional[int] = None
        self.sel_beat_trk: Optional[int] = None
        self.sel_beat_pl: Optional[int] = None

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

        # Internal
        self._next_id: int = 1
        self._listeners: list[Callable] = []
        self._project_path: Optional[str] = None

    def new_id(self) -> int:
        nid = self._next_id
        self._next_id += 1
        return nid

    def on_change(self, callback: Callable):
        self._listeners.append(callback)

    def notify(self, source=None):
        for cb in self._listeners:
            cb(source)

    # Lookup helpers
    def find_pattern(self, pid) -> Optional[Pattern]:
        return next((p for p in self.patterns if p.id == pid), None)

    def find_track(self, tid) -> Optional[Track]:
        return next((t for t in self.tracks if t.id == tid), None)

    def find_placement(self, plid) -> Optional[Placement]:
        return next((p for p in self.placements if p.id == plid), None)

    def find_beat_pattern(self, bpid) -> Optional[BeatPattern]:
        return next((p for p in self.beat_patterns if p.id == bpid), None)

    def find_beat_track(self, btid) -> Optional[BeatTrack]:
        return next((t for t in self.beat_tracks if t.id == btid), None)

    def find_beat_placement(self, bplid) -> Optional[BeatPlacement]:
        return next((p for p in self.beat_placements if p.id == bplid), None)

    def find_beat_instrument(self, iid) -> Optional[BeatInstrument]:
        return next((i for i in self.beat_kit if i.id == iid), None)

    def compute_transpose(self, pl: Placement) -> int:
        """Compute total transposition for a placement (manual + key shift)."""
        pat = self.find_pattern(pl.pattern_id)
        pk = pat.key if pat else 'C'
        tk = pl.target_key or pk
        return (pl.transpose or 0) + key_shift(pk, tk)

    def build_arrangement(self) -> dict:
        """Build arrangement dict for MIDI export / audio rendering."""
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
                pat = self.find_pattern(p.pattern_id)
                if not pat:
                    continue
                trk['placements'].append({
                    'pattern': {
                        'notes': [n.to_dict() for n in pat.notes],
                        'length': pat.length,
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
            'sf2Path': self.sf2.path if self.sf2 else None,
            'nextId': self._next_id,
        }
        return json.dumps(data, indent=2)

    def load_json(self, text: str):
        d = json.loads(text)
        self.bpm = d.get('bpm', 120)
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
        self._next_id = d.get('nextId', 1)
        self.sel_pat = None
        self.sel_trk = self.tracks[0].id if self.tracks else None
        self.sel_pl = None
        self.sel_beat_pat = None
        self.sel_beat_trk = None
        self.sel_beat_pl = None
        # sf2Path is stored but the caller must reload the SF2 file
        self._sf2_path_hint = d.get('sf2Path')
        self.notify()
