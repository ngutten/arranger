"""Audio rendering and playback for the standalone arranger."""

import io
import os
import wave
import struct
import shutil
import tempfile
import subprocess
import threading

import numpy as np


def render_fluidsynth(midi_bytes, sf2_path, sr=44100):
    """Render MIDI to WAV using fluidsynth. Returns WAV bytes or None."""
    if not shutil.which('fluidsynth'):
        return None
    with tempfile.NamedTemporaryFile(suffix='.mid', delete=False) as mf:
        mf.write(midi_bytes)
        mid = mf.name
    wav_path = mid.replace('.mid', '.wav')
    try:
        r = subprocess.run(
            ['fluidsynth', '-ni', sf2_path, mid, '-F', wav_path, '-r', str(sr)],
            capture_output=True, timeout=120
        )
        if r.returncode == 0 and os.path.exists(wav_path):
            with open(wav_path, 'rb') as f:
                return f.read()
    except Exception:
        pass
    finally:
        for p in [mid, wav_path]:
            try:
                os.unlink(p)
            except Exception:
                pass
    return None


def render_basic(arr, sr=44100):
    """Render arrangement to WAV using basic sine/noise synthesis."""
    bpm = arr.get('bpm', 120)
    bd = 60.0 / bpm
    notes = []
    for t in arr.get('tracks', []):
        drum = t.get('channel', 0) == 9
        for pl in t.get('placements', []):
            pat = pl.get('pattern', {})
            ns = pat.get('notes', [])
            bt = pl.get('time', 0)
            tr = pl.get('transpose', 0)
            reps = pl.get('repeats', 1)
            plen = pat.get('length', 4)
            if ns:
                plen = max(plen, max(n['start'] + n['duration'] for n in ns))
            for rep in range(reps):
                off = bt + rep * plen
                for n in ns:
                    notes.append((
                        (off + n['start']) * bd,
                        n['duration'] * bd,
                        n['pitch'] + tr,
                        n.get('velocity', 100) / 127.0,
                        drum
                    ))
    if not notes:
        return None
    total = max(t + d for t, d, _, _, _ in notes) + 0.5
    nsamp = int(total * sr)
    audio = np.zeros(nsamp, dtype=np.float64)
    for t, dur, pitch, vel, drum in notes:
        freq = 440.0 * 2 ** ((pitch - 69) / 12.0)
        s = int(t * sr)
        l = min(int(dur * sr), nsamp - s)
        if l <= 0:
            continue
        tt = np.arange(l) / sr
        if drum:
            sig = np.random.randn(l) * np.exp(-tt * 20)
        else:
            env = np.ones(l)
            a = min(int(.01 * sr), l // 4)
            r = min(int(.05 * sr), l // 3)
            if a > 0:
                env[:a] = np.linspace(0, 1, a)
            if r > 0:
                env[-r:] = np.linspace(1, 0, r)
            sig = np.sin(2 * np.pi * freq * tt) * env
        audio[s:s + l] += sig * vel * 0.3
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio /= peak / 0.9
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def wav_to_mp3(wav_bytes):
    """Convert WAV bytes to MP3 using ffmpeg. Returns MP3 bytes or None."""
    if not shutil.which('ffmpeg'):
        return None
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
        wf.write(wav_bytes)
        wp = wf.name
    mp = wp.replace('.wav', '.mp3')
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-i', wp, '-b:a', '192k', mp],
            capture_output=True, timeout=120
        )
        if os.path.exists(mp):
            with open(mp, 'rb') as f:
                return f.read()
    finally:
        for p in [wp, mp]:
            try:
                os.unlink(p)
            except Exception:
                pass
    return None


def render_sample(sf2_path, bank, program, pitch, velocity=100, duration=0.5, channel=0):
    """Render a single note sample via fluidsynth. Returns WAV bytes or None."""
    from .midi import _vlq
    tpb = 480
    bpm = 120
    note_ticks = int(duration * tpb * (bpm / 60))
    ch = channel & 0x0F
    evs = [
        (0, bytes([0xB0 | ch, 0, (bank >> 7) & 0x7F])),
        (0, bytes([0xB0 | ch, 0x20, bank & 0x7F])),
        (0, bytes([0xC0 | ch, program & 0x7F])),
        (0, bytes([0x90 | ch, pitch & 0x7F, velocity & 0x7F])),
        (note_ticks, bytes([0x80 | ch, pitch & 0x7F, 0])),
        (note_ticks, bytes([0xFF, 0x2F, 0x00]))
    ]
    hdr = b'MThd' + struct.pack('>I', 6) + struct.pack('>HHH', 0, 1, tpb)
    tb = b''
    prev = 0
    for at, data in evs:
        tb += _vlq(max(0, at - prev)) + data
        prev = at
    midi_bytes = hdr + b'MTrk' + struct.pack('>I', len(tb)) + tb
    return render_fluidsynth(midi_bytes, sf2_path)


def generate_preview_tone(pitch, velocity=100, duration=0.15, sr=22050):
    """Generate a short preview tone as WAV bytes."""
    t = np.arange(int(sr * duration)) / sr
    freq = 440.0 * 2 ** ((pitch - 69) / 12.0)
    env = np.exp(-t * 15)
    sig = np.sin(2 * np.pi * freq * t) * env * (velocity / 127) * 0.3
    samples = (sig * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())
    return buf.getvalue()


class AudioPlayer:
    """Cross-platform audio playback using subprocess fallbacks."""

    def __init__(self):
        self._process = None
        self._lock = threading.Lock()

    def play_wav(self, wav_bytes):
        """Play WAV bytes. Stops any current playback first."""
        self.stop()
        with self._lock:
            # Write to temp file
            tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            tmp.write(wav_bytes)
            tmp.close()
            # Try platform-specific playback
            try:
                if shutil.which('aplay'):
                    self._process = subprocess.Popen(
                        ['aplay', '-q', tmp.name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                elif shutil.which('afplay'):
                    self._process = subprocess.Popen(
                        ['afplay', tmp.name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                elif shutil.which('powershell'):
                    self._process = subprocess.Popen(
                        ['powershell', '-c',
                         f'(New-Object Media.SoundPlayer "{tmp.name}").PlaySync()'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                else:
                    os.unlink(tmp.name)
                    return

                # Clean up temp file after playback finishes
                def cleanup():
                    if self._process:
                        self._process.wait()
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass

                threading.Thread(target=cleanup, daemon=True).start()
            except Exception:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass

    def stop(self):
        """Stop current playback."""
        with self._lock:
            if self._process:
                try:
                    self._process.terminate()
                except Exception:
                    pass
                self._process = None

    def play_async(self, wav_bytes):
        """Play WAV bytes in a background thread."""
        threading.Thread(target=self.play_wav, args=(wav_bytes,), daemon=True).start()
