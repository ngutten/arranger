"""Playback helpers — note preview, pattern preview, loop management.

Functions here take engine/player/state as explicit arguments.
QTimer-based playhead animation stays in app.py since it's UI wiring.
"""

import threading

from ..state import Track
from ..core.midi import create_midi
from ..core.audio import (
    render_fluidsynth, render_basic,
    generate_preview_tone, render_sample,
)
from .export import _get_sf2_path


def play_note(state, engine, player, pitch, velocity, track_id=None):
    """Play a single note preview, using track instrument if available."""
    channel = 0
    if track_id:
        t = state.find_track(track_id)
        if t:
            channel = t.channel

    # Use engine if available
    if engine:
        engine.play_single_note(pitch, velocity, channel, duration=0.5)
        return

    # Legacy fallback
    bank, program = 0, 0
    if track_id:
        t = state.find_track(track_id)
        if t:
            bank, program = t.bank, t.program

    sf2_path = _get_sf2_path(state.sf2)
    if sf2_path:
        try:
            wav = render_sample(sf2_path, bank, program, pitch, velocity,
                                duration=0.5, channel=channel)
            if wav:
                player.play_async(wav)
                return
        except Exception:
            pass
    wav = generate_preview_tone(pitch, velocity, 0.3)
    player.play_async(wav)


def play_beat_hit(state, engine, player, inst_id):
    """Play a single beat instrument hit."""
    inst = state.find_beat_instrument(inst_id)
    if not inst:
        return

    # Use engine if available
    if engine:
        if inst.channel != 9:
            engine._send_cmd('_setup_program', inst.channel, inst.bank, inst.program)
        engine.play_single_note(inst.pitch, inst.velocity,
                                inst.channel, duration=0.5)
        return

    # Legacy fallback
    sf2_path = _get_sf2_path(state.sf2)
    if sf2_path:
        wav = render_sample(sf2_path, inst.bank, inst.program, inst.pitch,
                            inst.velocity, duration=0.5, channel=inst.channel)
        if wav:
            player.play_async(wav)
            return

    wav = generate_preview_tone(inst.pitch, inst.velocity, 0.3)
    player.play_async(wav)


def build_pattern_preview(state):
    """Build an arrangement dict for previewing the selected melodic pattern.
    
    Returns the arrangement dict, or None if nothing to preview.
    """
    pat = state.find_pattern(state.sel_pat)
    if not pat or not pat.notes:
        return None

    t = state.find_track(state.sel_trk)
    if not t:
        t = Track(id='preview', name='Preview', channel=0,
                  bank=0, program=0, volume=100)

    inst = {
        'name': t.name, 'channel': t.channel,
        'bank': t.bank, 'program': t.program,
        'volume': t.volume,
    }

    notes = [{'pitch': n.pitch, 'start': n.start, 'duration': n.duration,
              'velocity': n.velocity,
              **({'bend': n.bend} if n.bend else {})} for n in pat.notes]

    tracks = [{
        **inst,
        'placements': [{
            'pattern': {'notes': notes, 'length': pat.length},
            'time': 0, 'transpose': 0, 'repeats': 1,
        }]
    }]

    return {'bpm': state.bpm, 'tsNum': state.ts_num,
            'tsDen': state.ts_den, 'tracks': tracks}


def build_beat_pattern_preview(state):
    """Build an arrangement dict for previewing the selected beat pattern.
    
    Returns the arrangement dict, or None if nothing to preview.
    """
    pat = state.find_beat_pattern(state.sel_beat_pat)
    if not pat or not pat.grid:
        return None

    tracks = []
    for inst in state.beat_kit:
        grid = pat.grid.get(inst.id)
        if not grid:
            continue
        notes = []
        for step_idx, vel in enumerate(grid):
            if vel > 0:
                step_pos = step_idx / pat.subdivision
                notes.append({
                    'pitch': inst.pitch,
                    'start': step_pos,
                    'duration': 0.25,
                    'velocity': vel,
                })
        if notes:
            tracks.append({
                'name': inst.name, 'channel': inst.channel,
                'bank': inst.bank, 'program': inst.program,
                'volume': 100,
                'placements': [{
                    'pattern': {'notes': notes, 'length': pat.length},
                    'time': 0, 'transpose': 0, 'repeats': 1,
                }]
            })

    if not tracks:
        return None

    return {'bpm': state.bpm, 'tsNum': state.ts_num,
            'tsDen': state.ts_den, 'tracks': tracks}


def render_and_play_arr(arr, sf2_path, player):
    """Render an arrangement dict and play via player in a background thread.
    
    Used for pattern previews. Separate from export since this takes
    a pre-built arrangement dict rather than building from state.
    """
    def work():
        midi = create_midi(arr)
        wav = None
        if sf2_path:
            wav = render_fluidsynth(midi, sf2_path)
        if wav is None:
            wav = render_basic(arr)
        if wav:
            player.play_async(wav)

    threading.Thread(target=work, daemon=True).start()


def sync_loop_to_engine(state, engine):
    """Push current loop state to the engine."""
    if not engine:
        return
    if state.looping and state.loop_end is not None:
        ls = state.loop_start if state.loop_start is not None else 0.0
        engine.set_loop(ls, state.loop_end)
    else:
        engine.set_loop(None, None)


def compute_arrangement_length(state):
    """Compute the length in beats of the full arrangement.
    
    Wraps engine.compute_arrangement_length but falls back to manual
    calculation if engine module isn't available.
    """
    try:
        from ..core.engine import compute_arrangement_length as _cal
        return _cal(state)
    except ImportError:
        pass

    # Manual fallback
    max_beat = 0
    for pl in state.placements:
        pat = state.find_pattern(pl.pattern_id)
        if pat:
            max_beat = max(max_beat, pl.time + pat.length * (pl.repeats or 1))
    for bp in state.beat_placements:
        pat = state.find_beat_pattern(bp.pattern_id)
        if pat:
            max_beat = max(max_beat, bp.time + pat.length * (bp.repeats or 1))
    return max_beat
