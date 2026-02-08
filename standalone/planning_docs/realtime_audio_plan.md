# Realtime Audio Engine Plan (Finalized)

## Current Architecture

The playback path is: `build_arrangement()` → `create_midi()` → `render_fluidsynth()` (subprocess) → `AudioPlayer.play_wav()` (subprocess). Everything is offline: render the whole thing to a WAV, then play the file. No seeking, no live editing, no loop points.

## Target Architecture

### Core Abstraction: `AudioEngine`

A new `engine.py` module that owns the realtime audio pipeline. Three concerns:

1. **Instrument backends** — things that turn (pitch, velocity, channel) into audio samples
2. **Sequencer** — walks the arrangement timeline, dispatches note-on/off events to instruments at the right time
3. **Audio output** — pulls mixed audio from the sequencer and sends it to the system audio device

```
┌─────────────┐     note events     ┌──────────────────┐
│  Sequencer   │ ──────────────────► │  InstrumentBus   │
│  (reads      │                     │  (one per track)  │
│   AppState)  │                     │                   │
└──────┬───────┘                     └────────┬─────────┘
       │ transport control                    │ audio samples
       │ (play/stop/seek/loop)                ▼
       │                              ┌──────────────┐
       └─────────────────────────────►│  AudioOutput  │
                                      │  (sounddevice │
                                      │   callback)   │
                                      └──────────────┘
```

### Instrument Abstraction

```python
class Instrument(Protocol):
    """Something that produces audio given MIDI-like events."""
    def note_on(self, pitch: int, velocity: int, channel: int = 0) -> None: ...
    def note_off(self, pitch: int, channel: int = 0) -> None: ...
    def render(self, num_frames: int) -> np.ndarray: ...  # stereo float32, shape (num_frames, 2)
    def set_program(self, bank: int, program: int) -> None: ...
```

For now, the only real implementation is `FluidSynthInstrument` wrapping `pyfluidsynth`. This protocol is the extension point for future synths (simple oscillator, DDSP, VST via `pedalboard` or `dawdreamer`, etc).

### Why `pyfluidsynth`

`pyfluidsynth` wraps `libfluidsynth` via ctypes. It gives us:
- `Synth.noteon(chan, key, vel)` / `Synth.noteoff(chan, key)`
- `Synth.get_samples(num_frames)` → numpy array of interleaved int16 stereo
- Program/bank selection, gain control, reverb/chorus
- Multiple channels on one synth instance

Synthesizes in realtime at whatever block size we ask for. Stateful — notes ring out naturally with proper release tails.

For identical WAV export, we use the *same* `FluidSynthInstrument` class driven offline (tight loop rather than audio callback). This replaces the current subprocess `fluidsynth` call and guarantees identical output.

### Audio Output: `sounddevice`

```python
import sounddevice as sd

stream = sd.OutputStream(
    samplerate=RATE,
    channels=2,
    dtype='float32',
    blocksize=config.block_size,  # default 512 (~12ms at 44100)
    callback=audio_callback
)
```

Callback runs on a high-priority audio thread. Must be lock-free and fast.

## Finalized Design Decisions

### Block size
Configurable via settings file, default 512 (~12ms at 44100). Not exposed in UI. Settings file is a simple JSON (e.g. `~/.arranger/settings.json` or `settings.json` next to the project) with an `audio_block_size` key.

### Schedule rebuild granularity
Per-callback rebuild when dirty. Expected event counts are in the low thousands (e.g. 5-6 min song, 8 beat instruments at 25% density ≈ 32 events/measure from beats alone, plus melody/chords). Rebuilding a schedule of this size is well under a millisecond — just sorting a few thousand tuples. If it ever becomes a problem, we can add a double-buffer prep thread, but the simpler approach should be fine and is easier to reason about.

### FluidSynth instances
Single FluidSynth instance, channels mapped to tracks. Sufficient for now (16 channel limit). Can add multi-instance support later if needed.

### Sample rate
44100 Hz. All code uses a `RATE` constant — no magic 44100 literals anywhere. Changing sample rate is a one-line edit.

### Channel layout
Stereo throughout. FluidSynth outputs stereo natively. All buffers are `(num_frames, 2)` float32. Each track has a `pan` setting (−1 to +1) alongside `volume`.

## Track Bus / Mixer

```python
@dataclass
class InstrumentBus:
    instrument: Instrument
    channel: int            # FluidSynth channel for this track
    volume: float = 1.0     # 0-1, from track.volume / 127
    pan: float = 0.0        # -1 (left) to +1 (right)
    mute: bool = False
    solo: bool = False
    # Future: effects: list[Effect] = []
```

Pan law: constant-power (`left = cos(θ), right = sin(θ)` where `θ = (pan + 1) * π/4`).

The engine mixes all buses in the callback. Natural place to later add per-track effects chains, sends, master bus processing.

## New Dependencies

```
pyfluidsynth>=1.3.0    # ctypes wrapper for libfluidsynth
sounddevice>=0.4.0     # PortAudio wrapper for audio I/O
```

System dependency: `libfluidsynth` must be installed. Ubuntu: `apt install libfluidsynth3`. macOS: `brew install fluid-synth`. Windows: DLL ships with some distributions, or manual install.

## Settings File

`settings.json` (next to project or in `~/.arranger/`):
```json
{
    "audio_block_size": 512,
    "sample_rate": 44100
}
```

Loaded at startup, not hot-reloaded. Simple `json.load` with defaults for missing keys.

## Sequencer Design

Reads AppState and maintains:
- Current playback position (in beats), stored as atomic float
- Sample-accurate beat counter: incremented by `block_size / RATE * bpm / 60` each callback
- Loop start/end points (in beats)
- A "schedule" — sorted list of `(beat, event_type, params)` tuples derived from the arrangement

Re-derive schedule from AppState when dirty flag is set. The dirty flag is set by `state.notify()`. At the top of the next audio callback, the sequencer rebuilds its event list. Cost: sorting ~thousands of tuples, well under 1ms.

Seeking: set beat position atomically, send all-notes-off to FluidSynth, resume dispatching from new position.

## Threading Model

```
Main thread (Qt)              Audio thread (PortAudio)
─────────────────             ──────────────────────────
UI events                     audio_callback():
state.notify() ──────►          if dirty: rebuild schedule
  sets dirty flag               advance beat counter
                                dispatch note events
seek(beat) ───────────►         instrument.render(frames)
  atomic write                  mix buses → outdata

read engine.current_beat ◄─── atomic float
  for playhead animation
```

No locks in the audio path. Dirty flag and beat position are atomic. Schedule rebuild is the only non-trivial work in the callback, and it's fast for expected arrangement sizes.

## Implementation Phases

### Phase 1: Engine core (`engine.py`, `settings.py`)
- `Settings` class: loads/defaults from settings.json
- `FluidSynthInstrument` class wrapping pyfluidsynth
- `SineInstrument` fallback (no-fluidsynth graceful degradation)
- `InstrumentBus` with volume + pan
- `Sequencer` class: reads AppState, builds sorted event schedule, dispatches events
- `AudioEngine` class: owns sounddevice stream, instruments, sequencer
- Transport: `play()`, `stop()`, `seek(beat)`, `set_loop(start_beat, end_beat)`
- `RATE` constant used everywhere

### Phase 2: Wire into App
- Replace `AudioPlayer` usage in `app.py` with `AudioEngine`
- `toggle_play()` → `engine.play()` / `engine.stop()`
- Playhead: engine exposes `current_beat` property; existing QTimer reads it instead of wall clock
- Remove `_render_and_play` pattern for preview
- Add `pan` field to `Track` and `BeatInstrument` dataclasses in state.py

### Phase 3: Live editing
- `state.notify()` sets dirty flag on engine
- Engine re-derives event schedule on next callback
- Pattern/beat grid edits take effect immediately during playback

### Phase 4: Loop points UI
- Add loop start/end markers to arrangement view (draggable)
- Store in AppState: `loop_start: Optional[float]`, `loop_end: Optional[float]`
- Engine: when playhead reaches `loop_end`, jump to `loop_start`, send all-notes-off

### Phase 5: Offline rendering (identical to realtime)
- `AudioEngine.render_offline(arrangement) -> np.ndarray`
- Same `FluidSynthInstrument` + `Sequencer`, driven in tight loop
- `do_export()` uses this instead of subprocess fluidsynth
- Guarantees preview == export

### Phase 6: Fallback
- `SineInstrument` as fallback `Instrument` implementation
- If libfluidsynth unavailable, engine uses SineInstrument
- Degrades gracefully — realtime preview with basic tones

## Future Extension Points

- **Mixer UI**: `InstrumentBus` already has volume/pan/mute/solo. Just need UI.
- **Effects**: Add `effects: list[Effect]` to `InstrumentBus`, where `Effect.process(audio) -> audio`.
- **VST plugins**: via `pedalboard` or `dawdreamer`, implementing `Effect` protocol.
- **Alternative synths**: Implement `Instrument` protocol. Subtractive synth, wavetable, DDSP — all fit.
- **MIDI input**: Feed external MIDI events into instrument buses. Orthogonal to sequencer.
- **Per-track SF2**: Give each `FluidSynthInstrument` its own synth instance with different SF2.
- **Multi-instance FluidSynth**: If we hit the 16-channel limit, spawn additional instances.
