# Standalone Music Arranger - Implementation Plan

## Goal

Re-implement the web-based Music Arranger (Flask + HTML5 Canvas) as a standalone
desktop Python application using tkinter. The app should provide the same core
functionality — pattern-based MIDI arrangement with a piano roll editor, beat
grid editor, and arrangement timeline — without requiring a web browser or Flask
server.

---

## 1. Architecture Overview

### Current Architecture (Web)

```
arranger.py (Flask)          template.html (JS + Canvas)
├── SF2 parser               ├── State management (S object)
├── MIDI writer              ├── Canvas rendering (3 canvases)
├── Audio rendering          ├── Mouse/keyboard interaction
└── 6 API routes             ├── Web Audio API playback
                             └── Fetch-based API calls
```

### Proposed Architecture (Standalone)

```
standalone/
├── main.py                  # Entry point, arg parsing, launches app
├── app.py                   # Application class, top-level window + layout
├── state.py                 # Central state model (replaces JS `S` object)
├── core/
│   ├── __init__.py
│   ├── sf2.py               # SF2 parser (extracted from arranger.py)
│   ├── midi.py              # MIDI writer (extracted from arranger.py)
│   └── audio.py             # Audio rendering + playback (fluidsynth, basic synth)
├── ui/
│   ├── __init__.py
│   ├── topbar.py            # BPM, time sig, snap, buttons
│   ├── pattern_list.py      # Left panel: pattern + beat pattern lists
│   ├── arrangement.py       # Center top: arrangement timeline canvas
│   ├── piano_roll.py        # Center bottom: piano roll canvas (melodic)
│   ├── beat_grid.py         # Center bottom: beat grid canvas (drums)
│   ├── track_panel.py       # Right panel: track settings, SF2 presets, beat kit
│   └── dialogs.py           # Modal dialogs (new pattern, load SF2, etc.)
└── requirements.txt         # numpy, scipy (no flask)
```

### Why tkinter

- **Zero additional GUI dependencies** — ships with Python
- `tkinter.Canvas` supports the drawing primitives we need (rectangles, lines,
  text) and is event-driven rather than immediate-mode, but for our use case
  of clearing-and-redrawing on each state change, it works fine
- The existing web UI is entirely custom-drawn on `<canvas>` — there are no
  complex form layouts or rich widgets that would demand Qt
- Cross-platform (Linux, macOS, Windows) with no compilation step
- Keeps the dependency footprint minimal, matching the project's ethos

### Alternative considered: PySide6 / PyQt

PySide6 would provide QPainter (more capable drawing API), QMediaPlayer, and
richer widgets. However it's a ~150 MB dependency and introduces licensing
considerations. If tkinter's Canvas performance proves insufficient (unlikely
for this UI complexity), migrating to PySide6 later would be straightforward
since the core logic is fully decoupled.

---

## 2. Core Logic (Reused from arranger.py)

These modules are extracted from `arranger.py` with minimal changes. No Flask
dependency.

### 2a. `core/sf2.py` — SF2 Parser

Extract the `SF2Info` class as-is. Only dependency: `struct`, `pathlib`.

```python
# Direct extraction from arranger.py lines 23-74
class SF2Info:
    ...
```

Additions for standalone use:
- Add a `scan_directory(path)` function that returns a list of SF2Info objects
  for all `.sf2` files in a directory (replaces the `/api/list_sf2` route).

### 2b. `core/midi.py` — MIDI Writer

Extract `_vlq()` and `create_midi()` as-is. Only dependency: `struct`.

```python
# Direct extraction from arranger.py lines 80-133
def create_midi(arr, tpb=480): ...
```

No changes needed. The function takes a plain dict and returns bytes.

### 2c. `core/audio.py` — Audio Rendering + Playback

Extract `render_fluidsynth()`, `render_basic()`, and `wav_to_mp3()`.
Dependencies: `numpy`, `subprocess`, `wave`, `io`.

New additions:
- **`play_wav(wav_bytes)`** — Play WAV audio using a cross-platform approach.
  Options in order of preference:
  1. `simpleaudio` (pip installable, clean API, plays bytes directly)
  2. `subprocess` calling `aplay` / `afplay` / `powershell` as fallback
  3. Write to temp file and use `os.startfile` / `xdg-open` as last resort

  Recommend `simpleaudio` as an optional dependency with subprocess fallback.

- **`stop_playback()`** — Stop any currently playing audio.

---

## 3. State Model (`state.py`)

Replace the JS global `S` object with a Python dataclass-based model.

```python
@dataclass
class Note:
    pitch: int          # 0-127
    start: float        # beat offset
    duration: float     # beat length
    velocity: int       # 1-127

@dataclass
class Pattern:
    id: int
    name: str
    length: float       # beats
    notes: list[Note]
    color: str          # hex color
    key: str            # e.g. "C", "F#"
    scale: str          # e.g. "major", "minor"

@dataclass
class BeatPattern:
    id: int
    name: str
    length: float
    subdivision: int
    color: str
    grid: dict[int, list[int]]  # instrument_id -> [velocity per step]

@dataclass
class Track:
    id: int
    name: str
    channel: int        # 0-15
    bank: int
    program: int
    volume: int

@dataclass
class BeatTrack:
    id: int
    name: str

@dataclass
class Placement:
    id: int
    track_id: int
    pattern_id: int
    time: float         # beat start
    transpose: int
    repeats: int
    target_key: str

@dataclass
class BeatPlacement:
    id: int
    track_id: int
    pattern_id: int
    time: float
    repeats: int

@dataclass
class BeatInstrument:
    id: int
    name: str
    channel: int
    bank: int
    program: int
    pitch: int
    velocity: int

@dataclass
class AppState:
    bpm: int = 120
    snap: float = 0.5
    ts_num: int = 4
    ts_den: int = 4
    patterns: list[Pattern] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    beat_kit: list[BeatInstrument] = field(default_factory=list)
    beat_patterns: list[BeatPattern] = field(default_factory=list)
    beat_tracks: list[BeatTrack] = field(default_factory=list)
    beat_placements: list[BeatPlacement] = field(default_factory=list)
    sf2: SF2Info | None = None
    # Selection state
    sel_pat: int | None = None
    sel_trk: int | None = None
    sel_pl: int | None = None
    sel_beat_pat: int | None = None
    sel_beat_trk: int | None = None
    sel_beat_pl: int | None = None
    # Playback state
    playing: bool = False
    looping: bool = False
    playhead: float | None = None
    # Editing state
    tool: str = 'draw'          # draw | select | erase
    note_len: str = '0.25'
    default_vel: int = 100
    _next_id: int = 1

    def new_id(self) -> int:
        id = self._next_id
        self._next_id += 1
        return id
```

### Observer pattern for UI updates

The state object supports a simple callback system so UI components can
subscribe to changes:

```python
class AppState:
    def __init__(self):
        self._listeners: list[Callable] = []

    def on_change(self, callback):
        self._listeners.append(callback)

    def notify(self):
        for cb in self._listeners:
            cb()
```

Each UI component registers a callback. When state changes, the relevant
component redraws. This replaces the web app's `renderAll()` pattern.

### Save/Load

The state serializes to/from JSON, matching the existing `.json` project format
(version `v:3`). This ensures saved projects are interchangeable between the web
and standalone versions.

---

## 4. UI Components

All UI is built with `tkinter`. The layout mirrors the web version:

```
┌─────────────────────────────────────────────────────┐
│  TopBar (Frame): BPM, TS, Snap, buttons             │
├────────┬───────────────────────────────┬────────────┤
│ Left   │  Arrangement (Canvas)         │  Right     │
│ Panel  │  ─────────────────────────    │  Panel     │
│        │  Piano Roll / Beat Grid       │            │
│(Lists) │  (Canvas)                     │ (Settings) │
└────────┴───────────────────────────────┴────────────┘
```

### 4a. `ui/topbar.py` — Top Controls

A `ttk.Frame` containing:
- Play/Stop button
- Loop toggle button
- BPM spinbox
- Time signature dropdowns
- Snap dropdown
- Action buttons: Load SF2, +Track, +Beat Track, Export MIDI/WAV/MP3, Save, Load

All controls bind to `AppState` and call `state.notify()` on change.

### 4b. `ui/pattern_list.py` — Left Panel

Two `Listbox`-like sections (or custom-drawn via Canvas for colored dots):
- **Melodic Patterns** — click to select, right-click for context menu
  (rename, duplicate, delete). Drag to arrangement canvas.
- **Beat Patterns** — same interactions.

Drag-and-drop: tkinter supports intra-application DnD via `event_generate` and
binding to `<B1-Motion>` / `<ButtonRelease-1>`. We track the dragged item and
drop target ourselves (the arrangement canvas checks cursor position on drop).

### 4c. `ui/arrangement.py` — Arrangement Timeline

A `tkinter.Canvas` implementing:
- Track lanes (horizontal rows, 56px each, alternating background)
- Beat/measure grid lines
- Placement blocks (colored rectangles with pattern name text)
- Playhead line (animated during playback)
- Timeline header (measure numbers) as a separate canvas above, scroll-synced

Interactions (bound via `canvas.bind()`):
- **Click** placement to select; **right-click** to delete
- **Drag** placement to move (track + time)
- **Drag right edge** to change repeat count
- **Drop** pattern from left panel to create new placement

### 4d. `ui/piano_roll.py` — Piano Roll Editor

A `tkinter.Canvas` with:
- Pitch rows (MIDI 24–96, 14px each, colored by key/scale)
- Beat grid columns
- Note rectangles (colored by velocity)
- Resize handle on right edge of each note

Left side: a separate small canvas or frame showing piano key labels (C3, D3,
etc.), scroll-synced vertically with the main canvas.

Bottom: velocity lane canvas, scroll-synced horizontally.

Tools (draw/select/erase) controlled by topbar buttons:
- **Draw**: click to place note, drag right edge to resize
- **Select**: click to select, Shift+click for multi-select, drag to move
- **Erase**: click to delete

Keyboard: Ctrl+C/V for copy/paste, Delete to remove selected, Ctrl+A select all.

### 4e. `ui/beat_grid.py` — Beat Grid Editor

Displayed instead of piano roll when a beat pattern is selected.

A `tkinter.Canvas` with:
- Instrument rows (from beat kit)
- Step columns (based on pattern length * subdivision)
- Cells colored by velocity (click to toggle, right-click to clear)

Left side: instrument labels.

### 4f. `ui/track_panel.py` — Right Panel

A scrollable `Frame` with sections:
- **Track Settings** — name, channel, bank, program, volume (for selected track)
- **Soundfont** — name, bank filter, preset list (clickable)
- **Placement** — time, transpose, target key, repeats (for selected placement)
- **Beat Kit** — collapsible instrument list with channel, bank, program, pitch,
  velocity controls

### 4g. `ui/dialogs.py` — Modal Dialogs

tkinter `Toplevel` windows for:
- **New/Edit Pattern** — name, length, key, scale
- **New/Edit Beat Pattern** — name, length, subdivision
- **Load SF2** — file list from instruments/ directory (or file browser)
- **About / Help** (optional)

---

## 5. Audio Playback

### Note preview (click piano key / beat grid cell)

For immediate low-latency feedback, generate a short sine wave in Python and
play it:

```python
def play_preview(pitch, velocity=100, duration=0.15):
    sr = 22050
    t = np.arange(int(sr * duration)) / sr
    freq = 440.0 * 2 ** ((pitch - 69) / 12.0)
    env = np.exp(-t * 15)  # quick decay
    sig = np.sin(2 * np.pi * freq * t) * env * (velocity / 127) * 0.3
    wav = (sig * 32767).astype(np.int16).tobytes()
    # Play via simpleaudio or subprocess
```

For SF2-based preview: render a single note via `render_fluidsynth()` with a
minimal MIDI, cache the result, play it back. This is what the web version does
via `/api/render_sample`.

### Full arrangement playback

1. Call `build_arrangement()` (equivalent of JS `buildArr()`) to assemble the
   arrangement dict from state
2. Call `create_midi(arr)` to generate MIDI bytes
3. Call `render_fluidsynth()` or `render_basic()` to get WAV bytes
4. Play the WAV bytes using `simpleaudio` (or fallback)
5. Animate the playhead on the arrangement canvas using `root.after()` timer

Rendering happens in a background thread to avoid blocking the UI. A
`threading.Thread` or `concurrent.futures.ThreadPoolExecutor` handles this,
with the result posted back to the main thread via `root.after()`.

---

## 6. Implementation Phases

### Phase 1: Core extraction + minimal window

- Extract `core/sf2.py`, `core/midi.py`, `core/audio.py` from `arranger.py`
- Create `state.py` with the data model
- Create `main.py` and `app.py` with a basic tkinter window
- Implement `ui/topbar.py` with BPM/TS controls and export buttons
- Wire up MIDI export (state -> `build_arrangement()` -> `create_midi()` -> file save dialog)
- **Milestone**: Can set BPM/TS/tracks and export a MIDI file (no visual editor yet)

### Phase 2: Arrangement view

- Implement `ui/arrangement.py` canvas with track lanes and grid
- Implement placement rendering and hit-testing
- Implement drag-and-drop from a temporary pattern list
- Implement `ui/pattern_list.py` left panel
- **Milestone**: Can create patterns/tracks, place them on the timeline, export MIDI

### Phase 3: Piano roll

- Implement `ui/piano_roll.py` with note drawing, selection, and editing
- Implement velocity lane
- Implement piano key labels with scroll sync
- Connect keyboard shortcuts (copy/paste, delete)
- **Milestone**: Full melodic pattern editing

### Phase 4: Beat system

- Implement `ui/beat_grid.py` canvas
- Implement beat kit section in `ui/track_panel.py`
- Wire beat patterns into arrangement and MIDI export
- **Milestone**: Full drum pattern editing and export

### Phase 5: Audio playback

- Integrate `simpleaudio` or fallback audio playback
- Implement note preview on piano key click
- Implement full arrangement playback with animated playhead
- Implement loop mode
- **Milestone**: Full playback support

### Phase 6: Polish

- SF2 loading dialog and preset browser
- Save/load project files (JSON, compatible with web version)
- Track settings panel (channel, bank, program, volume)
- Placement settings panel (transpose, target key, repeats)
- Window resize handling and paned layout
- **Milestone**: Feature parity with web version

---

## 7. Dependencies

### Required
```
numpy>=1.24.0        # Audio synthesis (render_basic)
scipy>=1.10.0        # Signal processing (if needed)
```

### Optional
```
simpleaudio>=1.0.4   # Audio playback (fallback: subprocess aplay/afplay)
```

### System (same as web version)
```
fluidsynth           # SF2 audio rendering
ffmpeg               # MP3 conversion
```

### Not needed (removed from web version)
```
flask                # No longer needed
```

---

## 8. Project File Compatibility

The standalone app reads and writes the same JSON project format as the web
version (`v:3`). Field name mapping:

| JSON field        | Python state field      |
|-------------------|-------------------------|
| `bpm`             | `state.bpm`             |
| `snap`            | `state.snap`            |
| `tsNum`           | `state.ts_num`          |
| `tsDen`           | `state.ts_den`          |
| `patterns`        | `state.patterns`        |
| `tracks`          | `state.tracks`          |
| `placements`      | `state.placements`      |
| `beatKit`         | `state.beat_kit`        |
| `beatPatterns`    | `state.beat_patterns`   |
| `beatTracks`      | `state.beat_tracks`     |
| `beatPlacements`  | `state.beat_placements` |
| `sf2Path`         | `state.sf2.path`        |
| `nextId`          | `state._next_id`        |

The serialization layer handles camelCase <-> snake_case conversion so both
versions can open each other's files.

---

## 9. Key Differences from Web Version

| Aspect              | Web version                | Standalone                      |
|---------------------|----------------------------|---------------------------------|
| GUI rendering       | HTML5 Canvas (JS)          | tkinter Canvas (Python)         |
| Audio playback      | Web Audio API              | simpleaudio / subprocess        |
| Note preview        | OscillatorNode / fetch     | numpy synthesis / fluidsynth    |
| SF2 browsing        | Server-side directory list | Direct filesystem access        |
| File export         | Fetch + blob download      | `filedialog.asksaveasfilename`  |
| State management    | Global JS object           | Python dataclass + callbacks    |
| Networking          | HTTP API (Flask)           | None — all in-process           |
| Layout              | CSS Grid/Flexbox           | tkinter grid/pack + PanedWindow |
| Code style          | Compact/minified JS        | Standard readable Python        |

---

## 10. Risk Areas & Mitigations

**tkinter Canvas performance**: The web version redraws 3 canvases on every
state change. tkinter Canvas uses retained-mode graphics (object IDs), so the
equivalent is `canvas.delete("all")` followed by redrawing. For our scale
(< 1000 items per canvas), this is fine. If jank appears during playhead
animation, we can optimize by only updating the playhead line rather than
redrawing everything.

**Audio latency**: Note preview via `simpleaudio` has slightly higher latency
than Web Audio API's `OscillatorNode`. Acceptable for a composition tool.
If needed, `sounddevice` (PortAudio wrapper) can provide lower-latency
streaming.

**Drag-and-drop**: tkinter doesn't have built-in cross-widget DnD like HTML5.
We implement it manually by tracking mouse state across widgets — click in
pattern list sets a drag payload, release over arrangement canvas creates the
placement. This is simple to implement.

**Scroll sync**: The piano roll has 3 synced scroll regions (keys, grid,
velocity). In tkinter, we bind `<Configure>` and scroll events, calling
`canvas.yview_moveto()` / `canvas.xview_moveto()` to keep them in sync.
