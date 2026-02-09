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


def export_midi(state):
    """Build arrangement and return MIDI bytes."""
    arr = state.build_arrangement()
    return create_midi(arr)


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
