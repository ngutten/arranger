# Sequencer Architecture Guide

Where to find things, and where to put new things.

## File Map

```
app.py              Main window — UI wiring, keyboard routing, undo/redo
state.py            AppState, data classes (Pattern, Track, Note, ...), IndexedList
engine.py           Realtime audio engine (FluidSynth/Sine), schedule builder

ops/                Domain logic — pure functions, no Qt imports
  patterns.py       Pattern & beat pattern: add, duplicate, delete
  tracks.py         Track, beat track, beat instrument: add, delete
  playback.py       Note/pattern preview, loop sync, arrangement length
  export.py         MIDI/WAV/MP3 rendering with engine→fluidsynth→basic fallback
  project_io.py     Save/load project JSON
  note_edit.py      Piano roll note operations: delete, merge, duplicate,
                    ghost commit, marquee select

piano_roll.py       Piano roll widget — note grid, velocity lane, selection state
arrangement.py      Arrangement view — timeline, placement drag/resize, clipboard
beat_grid.py        Beat pattern step sequencer grid
pattern_list.py     Left panel — pattern/beat pattern list with overlay toggles
track_panel.py      Right panel — track settings, SF2 browser, beat kit editor
topbar.py           Transport bar — play/stop, BPM, time sig, tool selector
clipboard.py        Clipboard classes for arrangement (NoteClipboard, ArrangementClipboard)
dialogs.py          Pattern/beat pattern create/edit dialogs
undo.py             Undo stack — snapshot/restore of full AppState
```

## Data Flow

```
User action → Qt signal/event
  → app.py thin wrapper (1–3 lines)
    → ops/ function (mutates AppState, returns result)
  → app.py cleans up UI-owned state (selections, etc.)
  → state.notify() → listener callbacks → widget.refresh()
```

For note editing in the piano roll, the flow is similar but the wiring
lives in `piano_roll.py` rather than `app.py`:

```
Mouse/key event in PianoGridWidget
  → PianoRoll method (_cut_to_clipboard, _delete_selected, etc.)
    → ops/note_edit function (mutates pattern notes, returns new selection)
  → PianoRoll updates _selected, _ghost_notes
  → state.notify() → refresh
```

## State (state.py)

`AppState` is the single source of truth. All collections are `IndexedList`
instances — they behave like normal lists (iteration, indexing, slicing,
`append`, `remove`, `extend`, list comprehension reassignment) but maintain
a `{id: item}` shadow dict for O(1) lookup via `find_*` methods.

Key types and their relationships:

```
Pattern  ──< Note          (pattern.notes is a plain list of Note)
Track
Placement ─→ Pattern       (placement.pattern_id)
Placement ─→ Track         (placement.track_id)

BeatPattern ──< grid       (beat_pattern.grid: {instrument_id: [velocity, ...]})
BeatTrack
BeatPlacement ─→ BeatPattern
BeatPlacement ─→ BeatTrack
BeatInstrument             (beat_kit: list of instruments with pitch/bank/program)
```

Selection state lives on AppState: `sel_pat`, `sel_trk`, `sel_pl`, etc.
UI-only selection (e.g. which arrangement placements are marquee-selected)
lives on the widget.

## Ops Conventions

Every function in `ops/` follows these rules:

- Takes `state` as the first argument, plus any needed engine/player/sf2 refs.
- Returns data the caller needs (e.g. deleted IDs for selection cleanup).
- Never imports Qt. Never touches widget state directly.
- Calls `state.notify()` when it changes state that widgets should react to.

This means ops functions are testable without a running GUI.

## Where to Put New Features

**New note editing operation** (e.g. quantize, humanize, transpose selection):
1. Write the function in `ops/note_edit.py` — takes `(pat, selected, ...)`, returns new selection set.
2. Add a 3-line wrapper in `piano_roll.py` that calls it.
3. Wire the keyboard shortcut in `PianoRoll.keyPressEvent`.

**New pattern/track operation** (e.g. merge patterns, reorder tracks):
1. Write in `ops/patterns.py` or `ops/tracks.py`.
2. Wire from `app.py` (thin method that calls ops, cleans up selections).
3. If triggered from pattern_list or track_panel, wire from there instead.

**New export format** (e.g. OGG, FLAC):
1. Add render function in `ops/export.py`.
2. Add menu item / button in `topbar.py`, wire through `app.py.do_export()`.

**New playback feature** (e.g. metronome, count-in):
1. Logic in `ops/playback.py` if it's pure state/engine work.
2. QTimer/UI polling stays in `app.py`.
3. Engine-level changes go in `engine.py` (schedule builder or callback).

**New UI panel or widget**:
1. Create the widget file.
2. Wire it into `app.py._build_ui()`.
3. Domain logic it triggers should go through ops, not be inlined.

**New data type on AppState** (e.g. automation lanes):
1. Add the dataclass to `state.py`.
2. Add to AppState as an `IndexedList` with property getter/setter.
3. Add `find_*` one-liner.
4. Add to `to_json` / `load_json` / `build_arrangement` as needed.
5. Add to `undo.py` `capture_state` / `restore_state`.

## Engine Interface (engine.py)

The engine is a separate thread. Communication is via command queue (main→audio)
and atomic float reads (audio→main for current_beat).

Methods called from app.py / ops:
- `engine.play()`, `engine.stop()`, `engine.seek(beat)`
- `engine.set_loop(start, end)` — None to disable
- `engine.mark_dirty()` — rebuilds schedule from AppState
- `engine.load_sf2(path)` — loads soundfont
- `engine.play_single_note(pitch, vel, channel, duration)` — preview
- `engine._send_cmd(cmd, *args)` — low-level command queue
- `engine.current_beat` (property) — read from main thread
- `engine.is_playing` (property)
- `engine.render_offline_wav()` — offline render for export

Free functions:
- `build_schedule(state)` — converts AppState into sorted event list
- `compute_arrangement_length(state)` — total length in beats

## SF2 Path Extraction

SF2 info can be either an `SF2Info` object (with `.path`) or a dict
(with `['path']`). Use `ops.export._get_sf2_path(state.sf2)` to handle
both cases. Don't inline the `hasattr` / `.get` pattern.
