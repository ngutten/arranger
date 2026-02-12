"""Realtime audio engine for the arranger.

Key design constraints:
- All FluidSynth calls happen on the audio thread (in the callback).
  fluidsynth.Synth is NOT thread-safe.
- Main thread communicates via atomic reference swaps (schedule, commands).
- No locks in the audio path.
- Schedule is an immutable sorted list; rebuilt from AppState when dirty.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol, Optional

import numpy as np

try:
    from .settings import Settings
except ImportError:
    from settings import Settings


# ---------------------------------------------------------------------------
# Instrument protocol
# ---------------------------------------------------------------------------

class Instrument(Protocol):
    """Something that produces audio given MIDI-like events."""

    def note_on(self, pitch: int, velocity: int, channel: int = 0) -> None: ...
    def note_off(self, pitch: int, channel: int = 0) -> None: ...
    def render(self, num_frames: int) -> np.ndarray: ...  # (num_frames, 2) float32
    def set_program(self, channel: int, bank: int, program: int) -> None: ...
    def set_channel_volume(self, channel: int, volume: int) -> None: ...
    def all_notes_off(self, channel: int = -1) -> None: ...
    def pitchbend(self, channel: int, value: int) -> None: ...


# ---------------------------------------------------------------------------
# FluidSynth instrument
# ---------------------------------------------------------------------------

class FluidSynthInstrument:
    """Wraps pyfluidsynth for realtime synthesis.

    IMPORTANT: all methods must be called from the same thread (audio thread).
    The only exception is the constructor, which runs on the main thread before
    the audio stream starts.
    """

    def __init__(self, sf2_path: str, settings: Settings):
        import fluidsynth
        self.fs = fluidsynth.Synth(samplerate=float(settings.sample_rate))
        # Lower gain to prevent clipping with polyphonic material.
        # Default is 0.2; we use 0.15 which gives ~3dB headroom for dense passages.
        self.fs.setting('synth.gain', 0.15)
        self.sfid = self.fs.sfload(sf2_path)
        self._sample_rate = settings.sample_rate
        # Pre-select program 0 on melodic channels only.
        # Channel 9 is GM drums — leave it for explicit setup via set_program.
        for ch in range(16):
            if ch != 9:
                self.fs.program_select(ch, self.sfid, 0, 0)

    def note_on(self, pitch: int, velocity: int, channel: int = 0) -> None:
        self.fs.noteon(channel, pitch, velocity)

    def note_off(self, pitch: int, channel: int = 0) -> None:
        self.fs.noteoff(channel, pitch)

    def set_program(self, channel: int, bank: int, program: int) -> None:
        # GM convention: channel 9 is drums. Most SF2 files put drum kits
        # at bank 128. If the caller says bank 0 on channel 9, try bank 128
        # first (standard GM SF2 layout), fall back to bank 0.
        if channel == 9 and bank == 0:
            try:
                self.fs.program_select(channel, self.sfid, 128, program)
                return
            except Exception:
                pass
        self.fs.program_select(channel, self.sfid, bank, program)

    def set_channel_volume(self, channel: int, volume: int) -> None:
        # CC7 = channel volume
        self.fs.cc(channel, 7, max(0, min(127, volume)))

    def pitchbend(self, channel: int, value: int) -> None:
        """Send pitch bend. value is 14-bit (0-16383), center=8192.

        pyfluidsynth's pitch_bend() takes a signed value (-8192..+8191, 0=center)
        and adds 8192 internally before calling fluid_synth_pitch_bend.
        We store/compute bend values in the MIDI-spec unsigned convention
        (0-16383, 8192=center), so subtract 8192 here to convert.
        """
        self.fs.pitch_bend(channel, value - 8192)

    def all_notes_off(self, channel: int = -1) -> None:
        """Send all-notes-off. channel=-1 means all channels."""
        if channel == -1:
            for ch in range(16):
                self.fs.cc(ch, 123, 0)   # CC 123 = all notes off
                self.fs.cc(ch, 120, 0)   # CC 120 = all sound off (kills tails)
                self.fs.pitch_bend(ch, 0)  # reset bend (pyfluidsynth signed: 0=center)
        else:
            self.fs.cc(channel, 123, 0)
            self.fs.cc(channel, 120, 0)
            self.fs.pitch_bend(channel, 0)  # reset bend (pyfluidsynth signed: 0=center)

    def render(self, num_frames: int) -> np.ndarray:
        """Render num_frames of stereo audio.

        fluidsynth.Synth.get_samples(num_frames) returns a numpy array of
        shape (2 * num_frames,) dtype int16 — interleaved stereo.
        We reshape to (num_frames, 2) float32 normalized to [-1, 1].
        """
        raw = self.fs.get_samples(num_frames)
        # raw is np.ndarray of int16, length 2*num_frames, interleaved [L,R,L,R,...]
        audio = raw.astype(np.float32) / 32768.0
        audio = audio.reshape(num_frames, 2)
        # Soft-clip via tanh to avoid hard clipping artifacts.
        # Only kicks in when samples exceed ~±0.95; transparent below that.
        peak = np.max(np.abs(audio))
        if peak > 0.95:
            audio = np.tanh(audio)
        return audio

    def delete(self):
        """Clean up FluidSynth resources."""
        try:
            self.fs.delete()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Sine fallback instrument
# ---------------------------------------------------------------------------

class SineInstrument:
    """Minimal sine-wave synth for when FluidSynth is unavailable."""

    def __init__(self, settings: Settings):
        self._sr = settings.sample_rate
        self._voices: dict[tuple[int, int], _SineVoice] = {}  # (channel, pitch) -> voice
        self._phase = 0.0

    def note_on(self, pitch: int, velocity: int, channel: int = 0) -> None:
        freq = 440.0 * 2 ** ((pitch - 69) / 12.0)
        self._voices[(channel, pitch)] = _SineVoice(freq, velocity / 127.0, self._sr)

    def note_off(self, pitch: int, channel: int = 0) -> None:
        v = self._voices.get((channel, pitch))
        if v:
            v.releasing = True

    def set_program(self, channel: int, bank: int, program: int) -> None:
        pass  # no-op for sine

    def set_channel_volume(self, channel: int, volume: int) -> None:
        pass

    def pitchbend(self, channel: int, value: int) -> None:
        pass  # no-op for sine

    def all_notes_off(self, channel: int = -1) -> None:
        self._voices.clear()

    def render(self, num_frames: int) -> np.ndarray:
        out = np.zeros((num_frames, 2), dtype=np.float32)
        dead = []
        for key, v in self._voices.items():
            buf = v.render(num_frames)
            out += buf
            if v.done:
                dead.append(key)
        for k in dead:
            del self._voices[k]
        # Soft-clip via tanh — transparent below ~0.8, smooth saturation above
        peak = np.max(np.abs(out))
        if peak > 0.8:
            out = np.tanh(out)
        return out

    def delete(self):
        pass


@dataclass
class _SineVoice:
    freq: float
    amp: float
    sr: int
    phase: float = 0.0
    releasing: bool = False
    release_gain: float = 1.0
    done: bool = False

    def render(self, n: int) -> np.ndarray:
        t = np.arange(n) / self.sr
        sig = np.sin(2 * np.pi * self.freq * t + self.phase) * self.amp * 0.2
        self.phase += 2 * np.pi * self.freq * n / self.sr
        self.phase %= 2 * np.pi

        if self.releasing:
            # Fast exponential release
            decay = np.exp(-np.arange(n) * 30.0 / self.sr) * self.release_gain
            sig *= decay
            self.release_gain = decay[-1] if n > 0 else 0
            if self.release_gain < 0.001:
                self.done = True

        # Mono to stereo
        return np.column_stack([sig, sig]).astype(np.float32)


# ---------------------------------------------------------------------------
# Schedule events
# ---------------------------------------------------------------------------

# Event types
EVT_NOTE_ON = 0
EVT_NOTE_OFF = 1
EVT_PROGRAM = 2
EVT_VOLUME = 3
EVT_BEND = 4  # pitch bend; pitch=14-bit value (0-16383, center=8192)

@dataclass(slots=True)
class SchedEvent:
    """A single scheduled event, sorted by beat position."""
    beat: float
    event_type: int
    channel: int
    # Overloaded fields depending on event_type:
    #   NOTE_ON:  pitch, velocity
    #   NOTE_OFF: pitch, velocity=0
    #   PROGRAM:  pitch=program, velocity=bank
    #   VOLUME:   pitch=volume, velocity=0
    pitch: int = 0
    velocity: int = 0


_BEND_CENTER = 8192
_BEND_RANGE_SEMITONES = 2.0   # matches FluidSynth default RPN 0 bend range
_BEND_POOL = list(range(10, 16))  # channels reserved for auto-routed bent notes
_BEND_RESOLUTION = 32  # bend events per beat (≈ 1 event per ~2ms at 120bpm)


def _semitones_to_bend(semitones: float) -> int:
    """Convert semitones to 14-bit MIDI pitch bend value."""
    ratio = max(-1.0, min(1.0, semitones / _BEND_RANGE_SEMITONES))
    return int(_BEND_CENTER + ratio * (_BEND_CENTER - 1 if ratio >= 0 else _BEND_CENTER))


def _cubic_interp(t, p0, p1, p2, p3):
    """Catmull-Rom cubic interpolation between p1 and p2, with p0/p3 as neighbours."""
    return 0.5 * ((2 * p1) +
                  (-p0 + p2) * t +
                  (2*p0 - 5*p1 + 4*p2 - p3) * t*t +
                  (-p0 + 3*p1 - 3*p2 + p3) * t*t*t)


def _emit_bend_events(events, channel, note_start, note_duration, control_points):
    """Emit a densely sampled bend curve for one note.

    control_points: list of [beat_offset, semitones] sorted by beat_offset.
    Pads with implicit zero-bend at start and end so curves return to neutral.
    """
    if not control_points:
        return

    # Sort and clamp offsets to note duration
    pts = sorted(control_points, key=lambda p: p[0])
    pts = [[max(0.0, min(note_duration, p[0])), p[1]] for p in pts]

    # Build anchor list: implicit zero at start and end, but don't duplicate
    # if the user already placed a point at t=0 or t=duration.
    full = []
    if pts[0][0] > 1e-9:
        full.append([0.0, 0.0])
    full.extend(pts)
    if pts[-1][0] < note_duration - 1e-9:
        full.append([note_duration, 0.0])

    if len(full) < 2:
        # Single point — constant bend, return to center at note end
        if full:
            bv = _semitones_to_bend(full[0][1])
            events.append(SchedEvent(beat=note_start, event_type=EVT_BEND,
                                     channel=channel, pitch=bv))
        events.append(SchedEvent(beat=note_start + note_duration,
                                 event_type=EVT_BEND, channel=channel,
                                 pitch=_BEND_CENTER))
        return

    # Sample the curve at _BEND_RESOLUTION events/beat
    step = 1.0 / _BEND_RESOLUTION
    t = 0.0
    prev_bend_val = -1  # force first event

    while t <= note_duration + step * 0.5:
        t_clamped = min(t, note_duration)

        # Find surrounding control points for Catmull-Rom
        seg = 0
        for k in range(len(full) - 1):
            if full[k][0] <= t_clamped < full[k+1][0]:
                seg = k
                break
        # At exactly note_duration, use the last segment
        if t_clamped >= full[-1][0]:
            seg = len(full) - 2

        t1, v1 = full[seg]
        t2, v2 = full[min(len(full)-1, seg+1)]
        v0 = full[max(0, seg-1)][1]
        v3 = full[min(len(full)-1, seg+2)][1]

        seg_len = t2 - t1
        if seg_len > 1e-9:
            local_t = max(0.0, min(1.0, (t_clamped - t1) / seg_len))
            semitones = _cubic_interp(local_t, v0, v1, v2, v3)
        else:
            semitones = v1

        bend_val = _semitones_to_bend(semitones)
        if bend_val != prev_bend_val:
            events.append(SchedEvent(
                beat=note_start + t_clamped,
                event_type=EVT_BEND,
                channel=channel,
                pitch=bend_val,
            ))
            prev_bend_val = bend_val

        t += step

    # Always reset bend to centre at note-off (same tick, sorts before next note-on)
    if prev_bend_val != _BEND_CENTER:
        events.append(SchedEvent(
            beat=note_start + note_duration,
            event_type=EVT_BEND,
            channel=channel,
            pitch=_BEND_CENTER,
        ))


def build_schedule(state) -> list[SchedEvent]:
    """Build a sorted event schedule from the current AppState.

    Notes with pitch bend data are auto-routed to dedicated channels from
    _BEND_POOL so that their per-note bend events don't interfere with other
    notes on the same track channel. Program/volume setup events are emitted
    for each routed channel so FluidSynth uses the right instrument.

    This runs on the main thread. The result is an immutable list that
    gets swapped atomically into the engine.
    """
    events: list[SchedEvent] = []

    # Track channel assignments and programs
    for t in state.tracks:
        ch = t.channel & 0x0F
        events.append(SchedEvent(beat=-1, event_type=EVT_PROGRAM, channel=ch,
                                 pitch=t.program, velocity=t.bank))
        events.append(SchedEvent(beat=-1, event_type=EVT_VOLUME, channel=ch,
                                 pitch=t.volume))

    # Beat instrument programs (typically channel 9)
    for inst in state.beat_kit:
        ch = inst.channel & 0x0F
        events.append(SchedEvent(beat=-1, event_type=EVT_PROGRAM, channel=ch,
                                 pitch=inst.program, velocity=inst.bank))

    # Melodic placements — with bend auto-routing
    # We allocate bend channels per (track_channel, time_window) to handle
    # polyphony: a pool channel is "in use" from note_on to note_off.
    bend_pool_state: dict[int, float] = {}  # pool_ch -> note_off_beat (when it's free)
    # Track the last (bank, program, volume) configured on each pool channel so we
    # can skip redundant in-sequence program/volume events when the same track reuses
    # the same channel back-to-back.
    bend_channel_last_config: dict[int, tuple] = {}  # pool_ch -> (bank, program, volume)

    def alloc_bend_channel(on_beat, note_off_beat, bank, program, volume):
        """Return a free pool channel, configuring it if needed.

        Program and volume events are emitted at on_beat (just before the note-on)
        rather than as static beat=-2 setup events.  A pool channel can be reused
        by notes from different tracks at different times, so the correct instrument
        must be set at playback time, not once at startup.
        """
        for pool_ch in _BEND_POOL:
            if bend_pool_state.get(pool_ch, -1.0) <= on_beat + 1e-9:
                bend_pool_state[pool_ch] = note_off_beat
                last = bend_channel_last_config.get(pool_ch)
                if last != (bank, program, volume):
                    # Emit program/volume at on_beat so they fire right before the
                    # note-on.  Using on_beat - 1e-9 would be cleaner in principle,
                    # but the sort key already orders EVT_PROGRAM before EVT_NOTE_ON
                    # at the same beat, so on_beat is fine.
                    events.append(SchedEvent(beat=on_beat, event_type=EVT_PROGRAM,
                                             channel=pool_ch, pitch=program, velocity=bank))
                    events.append(SchedEvent(beat=on_beat, event_type=EVT_VOLUME,
                                             channel=pool_ch, pitch=volume))
                    bend_channel_last_config[pool_ch] = (bank, program, volume)
                return pool_ch
        # Pool exhausted — fall back to track channel (already configured)
        return track_ch

    for pl in state.placements:
        t = state.find_track(pl.track_id)
        pat = state.find_pattern(pl.pattern_id)
        if not t or not pat:
            continue
        ch = t.channel & 0x0F
        transpose = state.compute_transpose(pl)
        reps = pl.repeats or 1
        for rep in range(reps):
            offset = pl.time + rep * pat.length
            for n in pat.notes:
                p = max(0, min(127, n.pitch + transpose))
                v = max(1, min(127, n.velocity))
                on_beat = offset + n.start
                off_beat = on_beat + n.duration

                if n.bend:
                    note_ch = alloc_bend_channel(on_beat, off_beat, t.bank, t.program, t.volume)
                    _emit_bend_events(events, note_ch, on_beat, n.duration, n.bend)
                else:
                    note_ch = ch

                events.append(SchedEvent(beat=on_beat, event_type=EVT_NOTE_ON,
                                         channel=note_ch, pitch=p, velocity=v))
                events.append(SchedEvent(beat=off_beat, event_type=EVT_NOTE_OFF,
                                         channel=note_ch, pitch=p))

    # Beat placements (no bend support — drums don't bend)
    for bp in state.beat_placements:
        bt = state.find_beat_track(bp.track_id)
        bpat = state.find_beat_pattern(bp.pattern_id)
        if not bt or not bpat:
            continue
        reps = bp.repeats or 1
        for inst in state.beat_kit:
            grid = bpat.grid.get(inst.id)
            if not grid:
                continue
            ch = inst.channel & 0x0F
            step_dur = bpat.length / len(grid)
            for rep in range(reps):
                offset = bp.time + rep * bpat.length
                for step_idx, vel in enumerate(grid):
                    if vel > 0:
                        on_beat = offset + step_idx * step_dur
                        off_beat = on_beat + step_dur * 0.8
                        events.append(SchedEvent(beat=on_beat, event_type=EVT_NOTE_ON,
                                                 channel=ch, pitch=inst.pitch,
                                                 velocity=vel))
                        events.append(SchedEvent(beat=off_beat, event_type=EVT_NOTE_OFF,
                                                 channel=ch, pitch=inst.pitch))

    # Sort: by beat, then: note-offs, bend, note-ons (avoids re-triggering and ensures
    # bend is applied before new notes fire at the same beat position)
    _order = {EVT_NOTE_OFF: 0, EVT_BEND: 1, EVT_PROGRAM: 1, EVT_VOLUME: 1, EVT_NOTE_ON: 2}
    events.sort(key=lambda e: (e.beat, _order.get(e.event_type, 1)))
    return events


def compute_arrangement_length(state) -> float:
    """Compute total arrangement length in beats."""
    max_beat = 0.0
    for pl in state.placements:
        pat = state.find_pattern(pl.pattern_id)
        if pat:
            max_beat = max(max_beat, pl.time + pat.length * (pl.repeats or 1))
    for bp in state.beat_placements:
        pat = state.find_beat_pattern(bp.pattern_id)
        if pat:
            max_beat = max(max_beat, bp.time + pat.length * (bp.repeats or 1))
    return max_beat


# ---------------------------------------------------------------------------
# Audio Engine
# ---------------------------------------------------------------------------

# Commands sent from main thread to audio thread
CMD_PLAY = 'play'
CMD_STOP = 'stop'
CMD_SEEK = 'seek'
CMD_SET_LOOP = 'set_loop'
CMD_ALL_NOTES_OFF = 'all_notes_off'


class AudioEngine:
    """Owns the realtime audio pipeline.

    Threading model:
    - Main thread: calls play/stop/seek, sets schedule via mark_dirty()
    - Audio thread: runs the callback, reads schedule, drives FluidSynth

    Communication is via:
    - _pending_schedule: atomic reference swap (list or None)
    - _commands: list of (cmd, args) tuples consumed in callback
    - _current_beat: float written by audio thread, read by main thread
    """

    def __init__(self, state, settings: Optional[Settings] = None):
        self.state = state
        self.settings = settings or Settings()
        self._sr = self.settings.sample_rate
        self._block_size = self.settings.block_size

        # Instrument
        self._instrument: Optional[Instrument] = None
        self._sf2_path: Optional[str] = None

        # Sequencer state (audio thread only)
        self._schedule: list[SchedEvent] = []
        self._sched_idx: int = 0  # next event to dispatch
        self._beat_pos: float = 0.0
        self._playing: bool = False
        self._arrangement_length: float = 0.0

        # Loop
        self._loop_start: float = 0.0
        self._loop_end: float = 0.0
        self._looping: bool = False

        # Cross-thread communication
        self._pending_schedule: Optional[list[SchedEvent]] = None  # atomic swap
        self._pending_length: float = 0.0
        self._commands: list[tuple] = []  # consumed in callback
        self._cmd_lock = threading.Lock()  # only protects command list append
        self._current_beat: float = 0.0  # written by audio thread, read by main

        # Audio stream
        self._stream = None
        self._stream_active = False

    # -------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------

    def load_sf2(self, sf2_path: str) -> bool:
        """Load a SoundFont. Call before play(). Returns True on success."""
        if self._instrument:
            self._send_cmd(CMD_ALL_NOTES_OFF)
            # Wait briefly for audio thread to process
            import time
            time.sleep(0.05)
            self._instrument.delete()
            self._instrument = None

        try:
            self._instrument = FluidSynthInstrument(sf2_path, self.settings)
            self._sf2_path = sf2_path
            return True
        except Exception as e:
            print(f"[AudioEngine] Failed to load SF2: {e}")
            self._instrument = SineInstrument(self.settings)
            self._sf2_path = None
            return False

    def ensure_instrument(self):
        """Ensure we have some instrument (sine fallback if no SF2)."""
        if self._instrument is None:
            self._instrument = SineInstrument(self.settings)

    # -------------------------------------------------------------------
    # Transport (called from main thread)
    # -------------------------------------------------------------------

    def play(self):
        """Start playback from current position."""
        self.mark_dirty()  # rebuild schedule
        self._ensure_stream()
        self._send_cmd(CMD_PLAY)

    def stop(self):
        """Stop playback."""
        self._send_cmd(CMD_STOP)

    def seek(self, beat: float):
        """Seek to a beat position."""
        self._send_cmd(CMD_SEEK, beat)

    def set_loop(self, start: Optional[float], end: Optional[float]):
        """Set loop points. None to disable."""
        if start is not None and end is not None:
            self._send_cmd(CMD_SET_LOOP, start, end, True)
        else:
            self._send_cmd(CMD_SET_LOOP, 0.0, 0.0, False)

    @property
    def current_beat(self) -> float:
        """Current playback position in beats. Safe to read from main thread."""
        return self._current_beat

    @property
    def is_playing(self) -> bool:
        return self._playing

    # -------------------------------------------------------------------
    # Dirty flag / schedule rebuild (called from main thread)
    # -------------------------------------------------------------------

    def mark_dirty(self):
        """Rebuild schedule from current state and queue it for the audio thread."""
        schedule = build_schedule(self.state)
        length = compute_arrangement_length(self.state)
        self._pending_length = length
        self._pending_schedule = schedule

    # -------------------------------------------------------------------
    # Audio callback (runs on audio thread)
    # -------------------------------------------------------------------

    def _audio_callback(self, outdata, frames, time_info, status):
        """sounddevice OutputStream callback. Must be fast and lock-free."""
        try:
            self._process_commands()
            self._check_pending_schedule()

            if self._instrument is None:
                outdata[:] = 0
                return

            if not self._playing:
                # Not playing the arrangement, but still render the instrument
                # so that note previews (play_single_note) are heard.
                audio = self._instrument.render(frames)
                outdata[:] = audio
                return

            bpm = self.state.bpm
            beats_per_frame = bpm / 60.0 / self._sr

            # Process audio in this block, dispatching events at correct times
            block_start_beat = self._beat_pos
            block_end_beat = block_start_beat + frames * beats_per_frame

            # Dispatch events that fall within this block
            self._dispatch_events(block_start_beat, block_end_beat)

            # Render audio
            audio = self._instrument.render(frames)
            outdata[:] = audio

            # Advance beat position
            self._beat_pos = block_end_beat
            self._current_beat = self._beat_pos

            # Check end of arrangement / loop
            if self._looping and self._loop_end > self._loop_start:
                if self._beat_pos >= self._loop_end:
                    self._seek_internal(self._loop_start)
            elif self._arrangement_length > 0 and self._beat_pos >= self._arrangement_length:
                if self._looping:
                    self._seek_internal(0.0)
                else:
                    self._playing = False
                    self._instrument.all_notes_off()
                    self._current_beat = 0.0

        except Exception as e:
            # NEVER let exceptions escape the callback — that kills PortAudio
            outdata[:] = 0
            import traceback
            traceback.print_exc()

    def _process_commands(self):
        """Process pending commands from the main thread."""
        if not self._commands:
            return
        with self._cmd_lock:
            cmds = self._commands[:]
            self._commands.clear()

        for cmd_tuple in cmds:
            cmd = cmd_tuple[0]
            if cmd == CMD_PLAY:
                self._apply_setup_events()
                self._playing = True
            elif cmd == CMD_STOP:
                self._playing = False
                if self._instrument:
                    self._instrument.all_notes_off()
                self._current_beat = self._beat_pos
            elif cmd == CMD_SEEK:
                beat = cmd_tuple[1]
                self._seek_internal(beat)
            elif cmd == CMD_SET_LOOP:
                self._loop_start = cmd_tuple[1]
                self._loop_end = cmd_tuple[2]
                self._looping = cmd_tuple[3]
            elif cmd == CMD_ALL_NOTES_OFF:
                if self._instrument:
                    self._instrument.all_notes_off()
            elif cmd == '_note_preview_on':
                if self._instrument:
                    _, pitch, vel, ch, dur = cmd_tuple
                    self._handle_note_preview(pitch, vel, ch, dur)
            elif cmd == '_note_preview_off':
                if self._instrument:
                    _, pitch, ch = cmd_tuple
                    self._instrument.note_off(pitch, ch)
            elif cmd == '_setup_program':
                if self._instrument:
                    _, ch, bank, prog = cmd_tuple
                    self._instrument.set_program(ch, bank, prog)

    def _check_pending_schedule(self):
        """Swap in a new schedule if one is pending."""
        pending = self._pending_schedule
        if pending is not None:
            old_schedule = self._schedule
            self._schedule = pending
            self._arrangement_length = self._pending_length
            self._pending_schedule = None
            self._reindex_schedule()
            if self._playing:
                self._apply_setup_events()
                self._retrigger_active_notes(old_schedule)

    def _apply_setup_events(self):
        """Apply all setup events (beat < 0) — programs, volumes, bend resets.

        Always re-applies programs (no caching) because the user may have
        changed track instruments since last play. Also resets pitch bend on
        all pool channels so residual state from a prior session doesn't bleed.
        """
        if not self._instrument:
            return
        # Reset bend on all pool channels unconditionally
        for pool_ch in _BEND_POOL:
            self._instrument.pitchbend(pool_ch, _BEND_CENTER)
        for evt in self._schedule:
            if evt.beat >= 0:
                break
            if evt.event_type == EVT_PROGRAM:
                self._instrument.set_program(evt.channel, evt.velocity, evt.pitch)
            elif evt.event_type == EVT_VOLUME:
                self._instrument.set_channel_volume(evt.channel, evt.pitch)

    def _retrigger_active_notes(self, old_schedule):
        """After a schedule swap, re-trigger notes that should be sounding now.

        We figure out which (channel, pitch) pairs should be active at the
        current beat position by walking the new schedule up to _beat_pos,
        tracking note-on/off state. Then we compare to what the old schedule
        had active, send note-offs for notes that are no longer active, and
        note-ons for notes that are newly active.
        """
        if not self._instrument:
            return
        pos = self._beat_pos

        def active_notes(schedule):
            """Return set of (channel, pitch) that are sounding at `pos`."""
            active = set()
            for evt in schedule:
                if evt.beat < 0:
                    continue
                if evt.beat > pos:
                    break
                key = (evt.channel, evt.pitch)
                if evt.event_type == EVT_NOTE_ON:
                    active.add(key)
                elif evt.event_type == EVT_NOTE_OFF:
                    active.discard(key)
            return active

        old_active = active_notes(old_schedule)
        new_active = active_notes(self._schedule)

        # Notes that should stop
        for ch, pitch in old_active - new_active:
            self._instrument.note_off(pitch, ch)

        # Notes that should start (or were modified — re-trigger)
        # For re-triggered notes, we need velocity. Build a map from the new schedule.
        vel_map = {}
        for evt in self._schedule:
            if evt.beat < 0:
                continue
            if evt.beat > pos:
                break
            key = (evt.channel, evt.pitch)
            if evt.event_type == EVT_NOTE_ON:
                vel_map[key] = evt.velocity
            elif evt.event_type == EVT_NOTE_OFF:
                vel_map.pop(key, None)

        for key in new_active - old_active:
            vel = vel_map.get(key, 100)
            self._instrument.note_on(key[1], vel, key[0])

    def _dispatch_events(self, start_beat: float, end_beat: float):
        """Dispatch note events in [start_beat, end_beat)."""
        schedule = self._schedule
        idx = self._sched_idx

        while idx < len(schedule):
            evt = schedule[idx]
            if evt.beat < 0:
                idx += 1
                continue
            if evt.beat >= end_beat:
                break
            if evt.beat >= start_beat:
                if evt.event_type == EVT_NOTE_ON:
                    self._instrument.note_on(evt.pitch, evt.velocity, evt.channel)
                elif evt.event_type == EVT_NOTE_OFF:
                    self._instrument.note_off(evt.pitch, evt.channel)
                elif evt.event_type == EVT_BEND:
                    self._instrument.pitchbend(evt.channel, evt.pitch)
            idx += 1

        self._sched_idx = idx

    def _seek_internal(self, beat: float):
        """Seek to a beat position (audio thread)."""
        if self._instrument:
            self._instrument.all_notes_off()
        self._beat_pos = beat
        self._current_beat = beat
        self._reindex_schedule()

    def _reindex_schedule(self):
        """Find the schedule index for the current beat position."""
        pos = self._beat_pos
        # Binary search would be fine but linear is plenty fast for ~thousands of events
        self._sched_idx = 0
        for i, evt in enumerate(self._schedule):
            if evt.beat >= pos:
                self._sched_idx = i
                return
        self._sched_idx = len(self._schedule)

    # -------------------------------------------------------------------
    # Stream management
    # -------------------------------------------------------------------

    def _ensure_stream(self):
        """Create the sounddevice output stream if not already running."""
        if self._stream_active:
            return
        self.ensure_instrument()

        import sounddevice as sd
        self._stream = sd.OutputStream(
            samplerate=self._sr,
            channels=2,
            dtype='float32',
            blocksize=self._block_size,
            callback=self._audio_callback,
        )
        self._stream.start()
        self._stream_active = True

    def shutdown(self):
        """Stop and clean up everything."""
        self._playing = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._stream_active = False
        if self._instrument:
            self._instrument.delete()
            self._instrument = None

    # -------------------------------------------------------------------
    # Offline rendering
    # -------------------------------------------------------------------

    def render_offline(self) -> Optional[np.ndarray]:
        """Render the entire arrangement offline. Returns (N, 2) float32 array.

        Uses the same instrument and schedule as realtime, just driven in a
        tight loop instead of from the audio callback.
        """
        schedule = build_schedule(self.state)
        length = compute_arrangement_length(self.state)
        if length <= 0:
            return None

        bpm = self.state.bpm
        total_seconds = length * 60.0 / bpm + 1.0  # +1s for release tails
        total_frames = int(total_seconds * self._sr)
        block = self._block_size
        beats_per_frame = bpm / 60.0 / self._sr

        # Use a fresh instrument instance for offline rendering
        if self._sf2_path:
            inst = FluidSynthInstrument(self._sf2_path, self.settings)
        else:
            inst = SineInstrument(self.settings)

        # Apply setup events (programs, volumes) and reset all pool bend channels
        for pool_ch in _BEND_POOL:
            inst.pitchbend(pool_ch, _BEND_CENTER)
        for evt in schedule:
            if evt.beat >= 0:
                break
            if evt.event_type == EVT_PROGRAM:
                inst.set_program(evt.channel, evt.velocity, evt.pitch)
            elif evt.event_type == EVT_VOLUME:
                inst.set_channel_volume(evt.channel, evt.pitch)

        # Render
        output = np.zeros((total_frames, 2), dtype=np.float32)
        beat_pos = 0.0
        sched_idx = 0
        frame_pos = 0

        # Skip setup events
        while sched_idx < len(schedule) and schedule[sched_idx].beat < 0:
            sched_idx += 1

        while frame_pos < total_frames:
            n = min(block, total_frames - frame_pos)
            end_beat = beat_pos + n * beats_per_frame

            # Dispatch events
            while sched_idx < len(schedule):
                evt = schedule[sched_idx]
                if evt.beat >= end_beat:
                    break
                if evt.event_type == EVT_NOTE_ON:
                    inst.note_on(evt.pitch, evt.velocity, evt.channel)
                elif evt.event_type == EVT_NOTE_OFF:
                    inst.note_off(evt.pitch, evt.channel)
                elif evt.event_type == EVT_BEND:
                    inst.pitchbend(evt.channel, evt.pitch)
                sched_idx += 1

            audio = inst.render(n)
            output[frame_pos:frame_pos + n] = audio

            beat_pos = end_beat
            frame_pos += n

        inst.delete()
        return output

    def render_offline_wav(self) -> Optional[bytes]:
        """Render offline and return WAV bytes."""
        import io
        import wave

        audio = self.render_offline()
        if audio is None:
            return None

        # Convert to int16
        audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self._sr)
            wf.writeframes(audio_int16.tobytes())
        return buf.getvalue()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _send_cmd(self, cmd, *args):
        """Queue a command for the audio thread."""
        with self._cmd_lock:
            self._commands.append((cmd, *args))

    def play_single_note(self, pitch: int, velocity: int = 100,
                         channel: int = 0, duration: float = 0.5):
        """Play a single note preview. Manages its own note-off via timer."""
        self._ensure_stream()
        if self._instrument is None:
            return
        # For channel 9 (drums), ensure drum program is selected
        if channel == 9:
            self._send_cmd('_setup_program', channel, 0, 0)
        # Send note-on via command queue (processed in audio thread)
        self._send_cmd('_note_preview_on', pitch, velocity, channel, duration)

    def _handle_note_preview(self, pitch, velocity, channel, duration):
        """Handle note preview — called from _process_commands on audio thread."""
        self._instrument.note_on(pitch, velocity, channel)
        # Schedule note-off after duration using a timer thread
        # (We can't use audio-thread timing easily for one-shots)
        def off():
            import time
            time.sleep(duration)
            self._send_cmd('_note_preview_off', pitch, channel)
        threading.Thread(target=off, daemon=True).start()
