# Song Plugin System

Python-side plugin interface for whole-song analysis, transformation, and generation.
Distinct from the C++ audio-plugin system in `plugins/` — song plugins operate on the
symbolic arrangement (notes, patterns, placements, automation, beat grids), not the
audio graph.

All public types live in `standalone/song_plugins/api.py`. Plugins should import
only from `song_plugins.api`.

---

## 1. Concepts

A **plugin** is a Python class deriving from `SongPlugin`, with a class-level
`manifest: PluginManifest` and a `run()` method. It has one or more
**capabilities**:

- `"analyze"` — produces an `Annotation` (a data layer: curve, heatmap, events, tags, stats).
- `"transform"` — returns a list of `Operation` dataclasses that modify the project.
- `"generate"` — returns operations that create new patterns/placements/tracks.

A plugin may combine capabilities: an analyzer that also suggests edits returns
both `annotation` and `operations` in its `PluginResult`.

Plugins run on a daemon thread. Progress is reported through a `Progress` protocol
object provided at run time. Signals marshal results back to the UI thread.

---

## 2. Minimal example

```python
# standalone/song_plugins/builtin/hello.py
from song_plugins.api import (
    Annotation, ParamSpec, PluginManifest, PluginResult, Progress,
    Scope, SongPlugin,
)

class HelloPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.hello",
        name="Hello",
        version="1.0.0",
        description="Reports total note count.",
        capabilities=("analyze",),
        schemas=("stats",),
        params=(),
        scopes=("whole",),
        deps=("midi",),
    )

    def run(self, view, params, progress: Progress) -> PluginResult:
        n = sum(1 for _ in view.notes_in(Scope(kind="whole")))
        ann = Annotation(
            id="builtin.hello/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Hello",
            schema="stats",
            data={"render": "text", "data": f"{n} notes in song"},
            declared_deps=self.manifest.deps,
            status="ok",
        )
        return PluginResult(annotation=ann)

PLUGIN = HelloPlugin
```

Drop this file in `standalone/song_plugins/builtin/` and it appears in the
Plugins dock's **+ Add Plugin** menu.

---

## 3. Plugin manifest

```python
@dataclass(frozen=True)
class PluginManifest:
    id: str                    # Reverse-DNS-ish unique ID, e.g. "builtin.note_density"
    name: str                  # Display name
    version: str               # Free-form
    description: str           # One-line summary, shown as tooltip
    capabilities: tuple[Capability, ...]      # ("analyze",), ("transform",), etc.
    schemas: tuple[Schema, ...]               # Annotation schemas the plugin may emit
    params: tuple[ParamSpec, ...]             # Declared user-configurable parameters
    scopes: tuple[ScopeKind, ...]             # Which input scopes the plugin accepts
    selection_kinds: tuple[SelectionKind, ...]  # Required when "selection" in scopes
    deps: tuple[MetaDep, ...]                 # What input the plugin reads (for staleness)
    live_supported: bool                      # True if auto-rerun on dep change is safe
    persistence_default: Persistence          # "transient" | "cached" | "authoritative"
    custom_widget: bool                       # Reserved; not yet honored in UI
    broadcast_eligible: bool | None           # Reserved; None auto-derives from schemas
    author: str | None
```

### Capabilities

Exactly one of: `"analyze"`, `"transform"`, `"generate"` (multiples allowed as a tuple).

### Schemas

Possible annotation shapes the plugin may emit. A plugin can declare multiple
and pick one at run time based on params (e.g., `scalar_curve` when
`per_track=False`, `multi_curve` otherwise).

| Schema | `Annotation.data` shape | Current renderer |
|---|---|---|
| `scalar_curve` | `{beats: [float], values: [float]}` | ✓ |
| `multi_curve` | `{beats: [float], series: {name: [float]}}` | placeholder |
| `grid2d` | `{beat_cols, axis_rows, values: ndarray, axis_label, axis_unit}` | placeholder |
| `events` | `[{start_beat, end_beat, label, color?, payload?}]` | placeholder |
| `note_tags` | `{note_id: {color?, label?, payload?}}` | placeholder |
| `placement_tags` | `{placement_id: {...}}` | placeholder |
| `stats` | `{render: "table"\|"bars"\|"text"\|"ranked_list", data: ...}` | placeholder |
| `custom` | plugin-defined; requires `custom_widget=True` | placeholder |

Placeholder renderers show a "not implemented" widget — emitting a non-scalar
schema today is safe but not yet visualized. All schemas are validated at
annotation creation time.

### Parameters

```python
ParamSpec(
    key="window_beats",
    type="float",                  # int|float|bool|enum|string|beat_range|track_select
    label="Window (beats)",
    default=2.0,
    min=0.25, max=16.0,
    choices=None,                  # required for "enum"
    help="Width of each density bin.",
    visible_when=None,             # e.g. {"mode": "advanced"} — show only when mode==advanced
)
```

The plugin block auto-builds a form from `params`. The factory in
`song_plugins/ui/param_widgets.py` currently supports `int`, `float`, `bool`,
`enum`, `string`. Types `beat_range` and `track_select` render as placeholders.
Add a case to that factory if you need them.

### Scopes and selection kinds

```python
scopes=("whole", "selection")
selection_kinds=("placements",)
```

Accepted scopes are offered in the plugin's scope param (if declared). When
`"selection"` is in `scopes`, `selection_kinds` must be non-empty — it
declares what kind of selection the plugin consumes.

Selection resolution is preflight: if the user has only notes selected and
your plugin's `selection_kinds=("placements",)`, the run is refused with
`SelectionMismatch` before any work starts. See §8 for the full table.

### Meta-dependencies (staleness)

```python
MetaDep = Literal["midi", "structure", "tempo", "tracks", "automation", "beat"]
```

- `midi` — notes, patterns, variations.
- `structure` — placements (position, repeats, existence).
- `tempo` — BPM or the tempo automation track.
- `tracks` — track metadata (name, instrument, volume).
- `automation` — non-tempo automation tracks.
- `beat` — beat patterns, beat grid edits, beat placements.

Declare only what you read. State edits are classified into one or more
meta-deps; annotations whose `deps` intersect the edit's deps are marked
`stale`. If `live=True` and live mode is on, a 200ms debounced rerun is
scheduled. Unknown edit sources map to the empty set (no-op).

### Live mode

`live_supported=True` means the plugin is safe to auto-rerun on state change.
Criteria: deterministic, fast (sub-second on realistic inputs), stateless
across runs. Expensive or non-idempotent plugins should leave this `False`;
the user will re-run manually.

### Persistence (reserved)

`persistence_default` is persisted on the `Annotation` but is not yet acted
on — annotations are transient in memory. `"cached"` and `"authoritative"`
behaviors land in a later phase.

---

## 4. SongView — reading the song

`SongView` is a read-only, thread-safe snapshot of the project, built at
run-start. Plugins never touch `AppState` directly.

### Song-level

```python
view.bpm                   # float — state.bpm (BPM at beat 0 when no tempo track)
view.total_beats           # float — computed from last placement end
view.time_signature        # tuple[int, int]
view.tempo_map             # TempoMapView with bpm_at / beat_to_seconds / seconds_to_beat
```

### Entity iteration (frozen views, immutable)

```python
view.tracks()                      # list[TrackView]
view.track(id)                     # TrackView | raises KeyError
view.patterns(); view.pattern(id)
view.variations(); view.variation(id)
view.placements(); view.placement(id)

view.beat_kit()                    # list[BeatInstrumentView]
view.beat_patterns()               # list[BeatPatternView]
view.beat_tracks()                 # list[BeatTrackView]
view.beat_placements()             # list[BeatPlacementView]

view.automation_tracks()
view.automation_track(id)
view.tempo_track()                 # AutomationTrackView | None
```

### Resolved iteration (handles variations + repeats)

```python
for note in view.notes_in(Scope(kind="whole")):
    note.note_id          # int (0 = unassigned, for unnumbered legacy notes)
    note.pitch            # int, 0-127
    note.start_beat       # float — ABSOLUTE in song time (placement + repeat + note.start)
    note.duration_beats   # float
    note.velocity         # int 0-127
    note.lyric            # str
    note.bend             # tuple[(beat_offset, semis), ...]
    note.track_id
    note.placement_id
    note.source_id        # pattern_id OR variation_id
    note.source_kind      # "pattern" | "variation"
    note.repeat_index     # 0..repeats-1
```

`notes_in` handles variation placements transparently (applies modifications,
deletions, splits, and added notes) and expands `placement.repeats`.

```python
for ev in view.beat_events_in(Scope(kind="whole")):
    ev.inst_id            # BeatInstrument.id
    ev.pitch              # resolved from kit
    ev.start_beat         # absolute
    ev.velocity           # grid cell value (0 cells are skipped)
    ev.step               # grid column index
    ev.repeat_index
```

### Selection

```python
sel = view.selection()
sel.notes                        # frozenset[int] — note_ids in the currently-edited pattern
sel.placements                   # frozenset[int] — union of selected placements
sel.primary                      # "notes"|"placements"|"beat_placements"|"automation_placements"|"none"
sel.current_pattern_id           # int | None
sel.current_variation_id
sel.current_beat_pattern_id
sel.current_auto_pattern_id
```

`primary` reflects the last editor the user interacted with. If the user has
selections in both piano roll and arrangement, `primary` is the tiebreaker.

### Helpers

```python
view.bin_notes_by_beat(scope, window)    # list[(bin_center_beat, [ResolvedNote, ...])]
view.sample_automation(track_id, beat)   # float — interpolated value at beat
```

Add common primitives here rather than reimplementing in each plugin.

### Scope

```python
Scope(kind="whole")
Scope(kind="range", start_beat=0.0, end_beat=16.0)
Scope(kind="tracks", track_ids=(1, 3))
Scope(kind="selection")                        # resolved against view.selection()
Scope(kind="notes", note_ids=(12, 13, 17))
Scope(kind="placements", placement_ids=(5, 6))
```

`kind="selection"` is converted to `"notes"` or `"placements"` by the host
before the plugin runs — a plugin may also construct `"notes"` / `"placements"`
scopes directly.

---

## 5. Annotations

```python
@dataclass
class Annotation:
    id: str                       # plugin-chosen; typically "{plugin_id}/{instance}"
    plugin_id: str                # match manifest.id
    instance_id: str              # distinguishes multiple blocks of the same plugin
    title: str                    # shown in the block header
    schema: Schema                # from §3 table
    data: Any                     # shape determined by schema (validated)
    render_hint: dict             # axis labels, colormap, etc. (schema-specific)
    persistence: Persistence
    declared_deps: tuple[MetaDep, ...]
    live: bool                    # set by host when the user toggles live
    stale: bool                   # set by host when deps change
    status: "idle"|"running"|"ok"|"error"
    last_run_ms: int | None
    error: str | None
```

### Render hints (per schema)

- `scalar_curve`, `multi_curve`: `{"x_label": "beat", "y_label": "notes/beat"}`
- `grid2d`: `{"colormap": "magma", "db_range": 80, "log_frequency": True}`
- `events`: `{"lane": "structure"}` (advisory)
- `stats`: `{"render": ...}` lives inside `data`, not `render_hint`

Render hints are advisory — renderers use defaults when keys are missing.

---

## 6. Operations — mutating the project

Transform/generate plugins return a tuple of `Operation` dataclass instances
from `PluginResult.operations`. The host applies them via
`apply_ops(ops, app, label)`:

- **Validated all-or-nothing**: every op is validated (IDs exist, values in
  range, no invariants broken) before any mutation. If any op fails
  validation, `OperationError` is raised with the offending index and reason,
  and state is unchanged.
- **Single undo step**: the whole batch is wrapped in `app.undo_group(label)`
  and becomes one entry in the undo stack, labeled with the plugin name.
- **Ordered**: ops apply in the order provided. The return value is a list,
  parallel to the input, where each slot is either `None` or the new ID
  created by that op (for `Create*` ops).

### Operation catalogue

```
Notes         AddNote, MoveNote, ResizeNote, DeleteNote,
              SetNoteVelocity, SetNoteLyric, SetNoteBend, SplitNote
Patterns      CreatePattern, RenamePattern, ResizePattern, DeletePattern,
              DuplicatePattern, SetPatternKeyScale
Placements    CreatePlacement, MovePlacement, SetPlacementRepeats,
              SetPlacementTranspose, DeletePlacement
Variations    CreateVariation, DeleteVariation, FlattenVariation,
              VariationAddNote, VariationDeleteNote, VariationModifyNote,
              VariationSplitNote
Tracks        CreateTrack, RenameTrack, DeleteTrack,
              SetTrackInstrument, SetTrackVolume
Automation    CreateAutomationTrack, DeleteAutomationTrack,
              RenameAutomationTrack, CreateAutomationPattern,
              DeleteAutomationPattern, ResizeAutomationPattern,
              SetAutomationPoints, CreateAutomationPlacement,
              MoveAutomationPlacement, DeleteAutomationPlacement
Beat          CreateBeatPattern, DeleteBeatPattern, ResizeBeatPattern,
              SetBeatStep, SetBeatRow, CreateBeatPlacement,
              MoveBeatPlacement, SetBeatPlacementRepeats, DeleteBeatPlacement
```

Each op is a frozen dataclass whose fields match the relevant state
dataclass. Units are consistent with the project model: all time is `float`
beats, all IDs are `int`, pitch and velocity are MIDI 0–127, bend is a list
of `(beat_offset, semitones)` pairs.

### UI apply flow

The plugin block wires the buttons based on declared capabilities:

- **Transform-only** (`"transform"` or `"generate"`, without `"analyze"`):
  a single **Apply** button runs the plugin and immediately commits the
  resulting operations via `apply_ops`. No preview, no two-step caching.
- **Analyze + transform** (both capabilities): the classic two-step flow —
  **Run** renders the annotation and caches the returned ops, then
  **Apply** commits them. Param changes / state edits invalidate the
  cache.
- **Analyze-only**: **Run** button only.

---

## 7. Implementation notes (gotchas)

### 7.1 Double-apply: using IDs created in the same batch

`apply_ops` does **not** support referencing an ID that was created by an
earlier op in the same batch. `CreatePattern` returns its new ID in the
result list, but a `CreatePlacement(pattern_id=<that id>)` in the same batch
has no way to refer to it.

**Workaround: two `apply_ops` calls.** The first creates the entity and
returns the ID. The second uses the ID:

```python
from song_plugins.apply_ops import apply_ops

def run(self, view, params, progress):
    # Phase 1: create a pattern. One undo step.
    ids1 = apply_ops(
        [CreatePattern(name="Generated", length=4.0)],
        app, "Generate (create)",
    )
    new_pat_id = ids1[0]
    # Phase 2: fill and place it. Second undo step.
    apply_ops(
        [
            AddNote(pattern_id=new_pat_id, pitch=60, start=0.0,
                    duration=1.0, velocity=100),
            CreatePlacement(track_id=1, pattern_id=new_pat_id, time=0.0),
        ],
        app, "Generate (populate)",
    )
```

**Cost**: two undo entries instead of one.

**Why**: `apply_ops` validates the entire batch against current state before
mutating — it can't know an ID that doesn't exist yet. A placeholder-ID
mechanism is possible but hasn't been built.

**Mitigation**: return both undo steps as a "Generate: ..." group so the
user sees one logical action in the history. Not implemented yet; workaround
is to accept the two entries.

### 7.2 Note IDs — when they are and aren't stable

`Note.note_id: int` is the stable identity used for variation bookkeeping.
Legacy notes in old projects may have `note_id == 0` (unassigned). Variations
reference `note_id`, so plugins that emit `note_tags` or `VariationModifyNote`
must handle the `0` case:

- `note_tags` with `note_id=0` are ambiguous and should be skipped or
  coalesced by the plugin.
- Before `VariationModifyNote(note_id=0)`, the referenced parent note must
  first have a real ID. No op exists to assign one retroactively — in
  practice, new edits in the piano roll assign IDs automatically, so most
  live projects have fully-numbered notes.

### 7.3 Piano-roll `_selected` holds **indices**, not note_ids

`PianoRoll._selected` stores list indices into the currently-edited
pattern's `notes` list. The `SelectionProvider` translates these to
`note_id` before building a `SelectionSnapshot`. Don't read `_selected`
directly from a plugin — always go through `view.selection()`.

### 7.4 Absolute beats in `ResolvedNote.start_beat`

`notes_in()` yields `ResolvedNote` objects whose `start_beat` is
**absolute in song time** — placement time plus repeat offset plus
pattern-local note start. If you need the note's position within its
pattern, compute `start_beat - (placement.time + repeat_index * pat.length)`,
or look up the source pattern and iterate `pat.notes` directly.

### 7.5 Placement duration is derived

There is no `placement.length` field — duration is
`placement.repeats × pattern.length`. The op for lengthening a placement is
`SetPlacementRepeats(placement_id, repeats)`. A non-integer multiple of the
pattern length is not expressible.

### 7.6 Selection ambiguity resolution

When a plugin's `scopes` include `"selection"`, the host applies this table
*before* calling `run()`:

| Plugin `selection_kinds` | Snapshot has notes | Snapshot has placements | Resolution |
|---|---|---|---|
| `("notes",)` | yes | (any) | Scope of notes |
| `("notes",)` | no | yes | `SelectionMismatch` |
| `("placements",)` | (any) | yes | Scope of placements |
| `("placements",)` | yes | no | `SelectionMismatch` |
| `("notes", "placements")` | yes | no | notes |
| `("notes", "placements")` | no | yes | placements |
| `("notes", "placements")` | yes | yes | `primary` wins |
| any | no | no | `SelectionEmpty` |

The plugin itself sees a concrete scope and never has to handle ambiguity.
`SelectionMismatch` / `SelectionEmpty` surface as an error on the plugin
block; no thread is spawned.

### 7.7 Live mode interaction with params

Changing a param in live mode marks the block stale and triggers a debounced
rerun *without* the user clicking Run. The debounce (200 ms) coalesces rapid
edits. If a plugin is still running when the debounce fires, the new run
is skipped (the in-flight run's result will still be delivered, then the
next edit re-arms the timer). This is conservative; overlapping runs are
not supported.

### 7.8 Threading

`plugin.run()` executes on a daemon `threading.Thread`. The `SongView`
handed in is a frozen snapshot — thread-safe to read, but mutating anything
reachable through it is undefined behavior. Do not call `apply_ops` from
inside `run()`; return operations in `PluginResult.operations` and the host
applies them on the UI thread.

`progress.update()` may be called from the worker thread. It emits Qt
signals; receivers use queued connections automatically.

`progress.cancelled` is a thread-safe flag. Long loops should poll it and
return early:

```python
for i, n in enumerate(view.notes_in(scope)):
    if progress.cancelled:
        break
    ...
```

Returning with `progress.cancelled` set is allowed; the host discards the
partial result.

### 7.9 `progress.update()` must not be called after `run()` returns

Once `run()` returns, the plugin is torn down. Calling `progress.update()`
from a side thread you started inside `run()` after the main `run()`
returns is a use-after-free on the progress object. Don't spawn background
work that outlives `run()`.

### 7.10 Unknown schemas render as placeholders

If your plugin emits a schema other than `scalar_curve`, the block output
shows a "not implemented" message rather than crashing. This is intentional —
new schemas are additive and safe to prototype. Add a renderer under
`song_plugins/ui/renderers/` and register it in `renderers/__init__.py` to
remove the placeholder.

### 7.11 What dep-watcher sees

Edits in the app call `state.notify(source)` where `source` is a short
string (e.g., `"note_edit"`, `"add_placement"`, `"beat_grid_edit"`). The
`dep_watcher.SOURCE_TO_DEPS` table maps each known source to its meta-dep
set. Unknown sources produce an empty set, so a live plugin is *safe* (no
false reruns) but potentially *missed* (no rerun when it should have).
Adding a new state-change source requires adding it to
`dep_watcher.SOURCE_TO_DEPS` if plugins should react to it.

---

## 8. Plugin discovery

Built-in plugins live in `standalone/song_plugins/builtin/`. Every `.py`
file there is imported at app startup by `registry.load_builtin_plugins()`.
A plugin module must expose a module-level `PLUGIN` attribute bound to a
`SongPlugin` subclass:

```python
class MyPlugin(SongPlugin):
    manifest = PluginManifest(...)
    def run(self, view, params, progress): ...

PLUGIN = MyPlugin
```

Duplicate plugin IDs: the first wins, the second is logged and skipped.
Modules that raise on import are logged and skipped without aborting the
scan.

External plugins can be loaded from an arbitrary directory via
`registry.load_plugins_from_dir(path)` (uses `spec_from_file_location`).
External plugin modules cannot use relative imports into `song_plugins`.

### Manifest validation

`validate_manifest()` runs at load time and rejects:

- empty `capabilities`
- `"selection"` in `scopes` with empty `selection_kinds`
- unknown `Schema` / `MetaDep` / `Capability` literals
- `custom_widget=True` without `schemas` containing `"custom"`

Errors surface as log warnings; the plugin is skipped.

---

## 9. Testing plugins

Tests live under `standalone/tests/test_song_plugins/`. The existing test
suite provides fixtures you can borrow (see `conftest.py` for a small
deterministic AppState).

### Testing `run()` directly

No Qt needed. Construct a `SongView` from the fixture state plus a hand-built
`SelectionSnapshot`, a dummy progress that records calls, and call
`plugin.run(view, params, progress)`. Assert on `PluginResult`.

### Testing operations

Use `apply_ops(ops, app, label)` against a fixture `App` (or the fallback
undo_group path for headless tests). Assert post-state and confirm
validation rejects bad inputs without mutating state.

### Testing through the runner

`PluginRunner.run_blocking()` runs synchronously on the calling thread —
useful in tests. The daemon-thread `start()` entry point is for the app.

---

## 10. What's not yet implemented

These appear in the API or design but are deferred to later phases. Writing
a plugin that relies on them will work against the data model but may
render as a placeholder or be silently ignored.

- **Broadcast band.** No shared visualization strip near the arranger.
  `broadcast_eligible` is accepted but unused.
- **Non-scalar renderers.** Only `scalar_curve` is drawn. Others fall back
  to a placeholder widget.
- **Per-object overlays.** `note_tags` / `placement_tags` data is accepted
  but not painted on piano roll / arranger.
- **Custom widgets.** `custom_widget=True` is accepted but the UI doesn't
  swap a plugin-provided widget into the output region yet.
- **Annotation persistence.** `persistence_default` is stored on the
  annotation but not acted on. All annotations are transient for now.
- **Promotion to authoritative.** No UI path from annotation to first-class
  project data.
- **Inter-plugin data flow.** Intentionally out of scope — plugins are
  self-contained. Share helpers via `SongView` primitives or plain Python
  modules imported by both plugins.
- **In-batch ID dependencies.** See §7.1. Use two `apply_ops` calls.

---

## 11. File map

```
standalone/song_plugins/
  api.py              Public surface — plugins import only from here
  ops.py              Operation dataclasses
  apply_ops.py        Executor with validation + undo_group
  song_view.py        SongView implementation
  schemas.py          Schema data-shape validators
  registry.py         Discovery + loading + selection resolver
  builtin/
    note_density.py   Reference analyze plugin
  ui/
    plugins_dock.py   Dock widget
    plugin_block.py   Per-plugin block
    flow_layout.py    Multi-column flow layout
    param_widgets.py  Auto-generated param widget factory
    selection_provider.py
    dep_watcher.py    State-change source → MetaDep classification
    runner.py         Threaded runner + QtProgress
    host.py           PluginHost controller
    renderers/
      scalar_curve.py
      placeholder.py
```
