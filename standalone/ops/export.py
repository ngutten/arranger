"""Export operations — MIDI, WAV, MP3."""

import threading

from ..core.midi import create_midi
from ..core.audio import render_fluidsynth, render_basic, wav_to_mp3


def _get_sf2_path(sf2):
    """Extract path from SF2Info or dict."""
    if not sf2:
        return None
    if hasattr(sf2, 'path'):
        return sf2.path
    if isinstance(sf2, dict):
        return sf2.get('path')
    return None


def _build_tempo_map_for_midi(state):
    """Build a list of (beat, bpm) tuples from the tempo automation track.

    Returns None if no tempo track exists.
    """
    tempo_track = state.find_tempo_track()
    if not tempo_track:
        return None

    from ..core.curve_utils import interpolate_curve

    placements = sorted(
        [ap for ap in state.automation_placements if ap.track_id == tempo_track.id],
        key=lambda ap: ap.time
    )
    if not placements:
        return None

    points = [(0.0, float(state.bpm))]
    for ap in placements:
        pattern = state.find_automation_pattern(ap.pattern_id)
        if not pattern or not pattern.points:
            continue
        curve_points = [(p.time, p.value, p.curve) for p in pattern.points]
        repeats = ap.repeats or 1
        for rep in range(repeats):
            offset = ap.time + rep * pattern.length
            num_samples = max(16, int(pattern.length * 16))
            for i in range(num_samples + 1):
                t = (i / num_samples) * pattern.length if num_samples > 0 else 0.0
                norm = interpolate_curve(curve_points, t, pattern.length, 0.0)
                norm = max(0.0, min(1.0, norm))
                bpm = pattern.min_value + norm * (pattern.max_value - pattern.min_value)
                bpm = max(20.0, min(300.0, bpm))
                points.append((offset + t, round(bpm, 2)))
    return points


def export_midi(state):
    """Build arrangement and return MIDI bytes."""
    arr = state.build_arrangement()
    tempo_map = _build_tempo_map_for_midi(state)
    return create_midi(arr, tempo_map=tempo_map)


def export_musicxml(state):
    """Return MusicXML bytes for the arrangement."""
    from .export_musicxml import create_musicxml
    return create_musicxml(state)


def render_wav(state, engine=None):
    """Render arrangement to WAV bytes.
    
    Tries engine offline rendering first, then fluidsynth, then basic.
    Returns WAV bytes or None.
    """
    arr = state.build_arrangement()
    midi = create_midi(arr)

    # Engine offline render (guarantees preview == export)
    if engine:
        wav = engine.render_offline_wav()
        if wav:
            return wav

    # Fluidsynth fallback
    sf2_path = _get_sf2_path(state.sf2)
    if sf2_path:
        wav = render_fluidsynth(midi, sf2_path)
        if wav:
            return wav

    # Basic synth fallback
    return render_basic(arr)


def render_mp3(state, engine=None):
    """Render arrangement to MP3 bytes, or None if ffmpeg unavailable."""
    wav = render_wav(state, engine)
    if wav is None:
        return None
    return wav_to_mp3(wav)


def render_and_play_async(state, player):
    """Render an arrangement dict and play it in a background thread.
    
    Used for pattern/beat previews. `player` is an AudioPlayer instance.
    """
    arr = state.build_arrangement()
    sf2_path = _get_sf2_path(state.sf2)

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
