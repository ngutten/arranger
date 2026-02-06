#!/usr/bin/env python3
"""
Music Arranger - A web-based pattern sequencer and arrangement tool.

Dependencies:
    pip install flask numpy scipy
    # Optional: apt install fluidsynth   (for SF2 audio rendering)
    # ffmpeg for MP3 conversion

Usage:
    python arranger.py [--port 5000]
"""

import struct, json, os, sys, tempfile, subprocess, shutil, wave, io
from pathlib import Path
from flask import Flask, request, jsonify, send_file
import numpy as np

# =============================================================================
# SF2 Parser (preset metadata only)
# =============================================================================

class SF2Info:
    def __init__(self, path):
        self.path, self.presets, self.info = path, [], {}
        self._parse()

    def _parse(self):
        with open(self.path, 'rb') as f:
            if f.read(4) != b'RIFF': raise ValueError("Not RIFF")
            size = struct.unpack('<I', f.read(4))[0]
            if f.read(4) != b'sfbk': raise ValueError("Not SF2")
            self._chunks(f, f.tell(), f.tell() + size - 4)

    def _chunks(self, f, s, e):
        f.seek(s)
        while f.tell() < e:
            cid = f.read(4)
            if len(cid) < 4: break
            csz = struct.unpack('<I', f.read(4))[0]; cs = f.tell()
            if cid == b'LIST':
                lt = f.read(4)
                if lt == b'INFO': self._info(f, f.tell(), cs+csz)
                elif lt == b'pdta': self._pdta(f, f.tell(), cs+csz)
            f.seek(cs + csz + (csz % 2))

    def _info(self, f, s, e):
        f.seek(s)
        while f.tell() < e:
            sid = f.read(4)
            if len(sid) < 4: break
            ssz = struct.unpack('<I', f.read(4))[0]; data = f.read(ssz)
            if ssz % 2: f.read(1)
            try: self.info[sid.decode()] = data.rstrip(b'\x00').decode('ascii', errors='replace')
            except: pass

    def _pdta(self, f, s, e):
        f.seek(s)
        while f.tell() < e:
            sid = f.read(4)
            if len(sid) < 4: break
            ssz = struct.unpack('<I', f.read(4))[0]; ss = f.tell()
            if sid == b'phdr':
                for i in range(ssz // 38 - 1):
                    f.seek(ss + i * 38)
                    name = f.read(20).split(b'\x00')[0].decode('ascii', errors='replace')
                    prog = struct.unpack('<H', f.read(2))[0]
                    bank = struct.unpack('<H', f.read(2))[0]
                    self.presets.append({'name': name.strip(), 'bank': bank, 'program': prog})
            f.seek(ss + ssz + (ssz % 2))

    def to_dict(self):
        return {'path': str(self.path), 'name': self.info.get('INAM', Path(self.path).stem),
                'info': self.info, 'presets': sorted(self.presets, key=lambda p: (p['bank'], p['program']))}

# =============================================================================
# MIDI Writer
# =============================================================================

def _vlq(v):
    r = [v & 0x7F]; v >>= 7
    while v: r.append((v & 0x7F) | 0x80); v >>= 7
    return bytes(reversed(r))

def create_midi(arr, tpb=480):
    bpm = arr.get('bpm', 120)
    tsn, tsd = arr.get('tsNum', 4), arr.get('tsDen', 4)
    tracks = []

    # Track 0: tempo + time sig
    t0 = [(0, bytes([0xFF,0x51,0x03]) + struct.pack('>I', int(60e6/bpm))[1:]),
           (0, bytes([0xFF,0x58,0x04, tsn, {1:0,2:1,4:2,8:3,16:4,32:5}.get(tsd,2), 24, 8])),
           (0, bytes([0xFF,0x2F,0x00]))]
    tracks.append(t0)

    for trk in arr.get('tracks', []):
        evs = []
        ch = trk.get('channel', 0) & 0xF
        bank, prog = trk.get('bank', 0), trk.get('program', 0)
        evs.append((0, bytes([0xB0|ch, 0, (bank>>7)&0x7F])))
        evs.append((0, bytes([0xB0|ch, 0x20, bank&0x7F])))
        evs.append((0, bytes([0xC0|ch, prog&0x7F])))
        nm = trk.get('name','').encode('ascii', errors='replace')[:127]
        evs.append((0, bytes([0xFF,0x03,len(nm)]) + nm))

        for pl in trk.get('placements', []):
            pat = pl.get('pattern', {})
            notes = pat.get('notes', [])
            bt, tr, reps = pl.get('time', 0), pl.get('transpose', 0), pl.get('repeats', 1)
            plen = pat.get('length', 4)
            if notes: plen = max(plen, max(n['start']+n['duration'] for n in notes))
            for rep in range(reps):
                off = bt + rep * plen
                for n in notes:
                    p = max(0, min(127, n['pitch'] + tr))
                    v = max(1, min(127, n.get('velocity', 100)))
                    on = int((off + n['start']) * tpb)
                    of = int((off + n['start'] + n['duration']) * tpb)
                    evs.append((on, bytes([0x90|ch, p, v])))
                    evs.append((of, bytes([0x80|ch, p, 0])))

        evs.sort(key=lambda e: (e[0], 0 if e[1][0]&0xF0==0x80 else 1))
        evs.append((evs[-1][0] if evs else 0, bytes([0xFF,0x2F,0x00])))
        tracks.append(evs)

    hdr = b'MThd' + struct.pack('>I',6) + struct.pack('>HHH', 1, len(tracks), tpb)
    out = hdr
    for tevs in tracks:
        tb, prev = b'', 0
        for at, data in tevs:
            tb += _vlq(max(0, at-prev)) + data; prev = at
        out += b'MTrk' + struct.pack('>I', len(tb)) + tb
    return out

# =============================================================================
# Audio Rendering
# =============================================================================

def render_fluidsynth(midi_bytes, sf2_path, sr=44100):
    if not shutil.which('fluidsynth'): return None
    with tempfile.NamedTemporaryFile(suffix='.mid', delete=False) as mf:
        mf.write(midi_bytes); mid = mf.name
    wav = mid.replace('.mid', '.wav')
    try:
        r = subprocess.run(['fluidsynth','-ni',sf2_path,mid,'-F',wav,'-r',str(sr)],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.exists(wav):
            with open(wav,'rb') as f: return f.read()
    except: pass
    finally:
        for p in [mid, wav]:
            try: os.unlink(p)
            except: pass
    return None

def render_basic(arr, sr=44100):
    bpm = arr.get('bpm', 120); bd = 60.0 / bpm
    notes = []
    for t in arr.get('tracks', []):
        drum = t.get('channel',0) == 9
        for pl in t.get('placements', []):
            pat = pl.get('pattern', {}); ns = pat.get('notes', [])
            bt, tr, reps = pl.get('time',0), pl.get('transpose',0), pl.get('repeats',1)
            plen = pat.get('length', 4)
            if ns: plen = max(plen, max(n['start']+n['duration'] for n in ns))
            for rep in range(reps):
                off = bt + rep * plen
                for n in ns:
                    notes.append(((off+n['start'])*bd, n['duration']*bd, n['pitch']+tr,
                                  n.get('velocity',100)/127.0, drum))
    if not notes: return None
    total = max(t+d for t,d,_,_,_ in notes) + 0.5
    nsamp = int(total * sr)
    audio = np.zeros(nsamp, dtype=np.float64)
    for t, dur, pitch, vel, drum in notes:
        freq = 440.0 * 2**((pitch-69)/12.0)
        s, l = int(t*sr), min(int(dur*sr), nsamp - int(t*sr))
        if l <= 0: continue
        tt = np.arange(l) / sr
        if drum:
            sig = np.random.randn(l) * np.exp(-tt * 20)
        else:
            env = np.ones(l)
            a, r = min(int(.01*sr), l//4), min(int(.05*sr), l//3)
            if a > 0: env[:a] = np.linspace(0,1,a)
            if r > 0: env[-r:] = np.linspace(1,0,r)
            sig = np.sin(2*np.pi*freq*tt) * env
        audio[s:s+l] += sig * vel * 0.3
    peak = np.max(np.abs(audio))
    if peak > 0: audio /= peak / 0.9
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    return buf.getvalue()

def wav_to_mp3(wav_bytes):
    if not shutil.which('ffmpeg'): return None
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
        wf.write(wav_bytes); wp = wf.name
    mp = wp.replace('.wav', '.mp3')
    try:
        subprocess.run(['ffmpeg','-y','-i',wp,'-b:a','192k',mp], capture_output=True, timeout=120)
        if os.path.exists(mp):
            with open(mp,'rb') as f: return f.read()
    finally:
        for p in [wp, mp]:
            try: os.unlink(p)
            except: pass
    return None

# =============================================================================
# Flask App
# =============================================================================

app = Flask(__name__)

TEMPLATE_DIR = Path(__file__).parent
INSTRUMENTS_DIR = (TEMPLATE_DIR / 'instruments').resolve()

@app.route('/')
def index():
    html_path = TEMPLATE_DIR / 'template.html'
    return html_path.read_text()

@app.route('/api/list_sf2', methods=['GET'])
def api_list_sf2():
    """List available SF2 files in the instruments directory"""
    if not INSTRUMENTS_DIR.exists():
        INSTRUMENTS_DIR.mkdir(parents=True, exist_ok=True)
        return jsonify({'files': []})
    
    files = []
    for f in INSTRUMENTS_DIR.glob('*.sf2'):
        try:
            info = SF2Info(f)
            files.append({
                'name': f.name,
                'path': str(f),
                'displayName': info.info.get('INAM', f.stem)
            })
        except:
            # If can't parse, still show the file
            files.append({
                'name': f.name,
                'path': str(f),
                'displayName': f.stem
            })
    return jsonify({'files': sorted(files, key=lambda x: x['name'])})

@app.route('/api/load_sf2', methods=['POST'])
def api_load_sf2():
    filename = request.get_json().get('filename', '')
    if not filename:
        return jsonify({'error': 'No filename provided'})
    
    # Security: restrict to instruments directory only
    # Check for directory traversal in the filename itself
    if '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({'error': 'Invalid filename'})
    
    path = INSTRUMENTS_DIR / filename
    
    # Verify the path (before resolving symlinks) is in instruments dir
    try:
        # Use absolute() instead of resolve() to avoid following symlinks
        abs_path = path.absolute()
        if abs_path.parent != INSTRUMENTS_DIR:
            return jsonify({'error': 'Access denied: path outside instruments directory'})
    except:
        return jsonify({'error': 'Invalid path'})
    
    if not path.exists():
        return jsonify({'error': f'File not found: {filename}'})
    
    if not path.is_file():
        return jsonify({'error': f'Not a file: {filename}'})
    
    try:
        # resolve() here is fine - we just need the real path to read the file
        return jsonify(SF2Info(path.resolve()).to_dict())
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/export_midi', methods=['POST'])
def api_export_midi():
    try:
        return send_file(io.BytesIO(create_midi(request.get_json())),
                         mimetype='audio/midi', as_attachment=True, download_name='arrangement.mid')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def validate_sf2_path(sf2_path):
    """Validate that sf2_path is within allowed directories"""
    if not sf2_path:
        return False
    
    try:
        path = Path(sf2_path).resolve()
        # Must be within instruments directory (after following symlinks with whitelist)
        # For now, simple check:
        allowed_dirs = [
            INSTRUMENTS_DIR.resolve(),
            Path.home() / 'lmms',  # Your soundfonts dir
        ]
        
        for allowed_dir in allowed_dirs:
            try:
                path.relative_to(allowed_dir)
                return path.is_file() and path.suffix == '.sf2'
            except ValueError:
                continue
        return False
    except:
        return False

@app.route('/api/render', methods=['POST'])
def api_render():
    data = request.get_json()
    arr, sf2, fmt = data.get('arrangement', {}), data.get('sf2_path'), data.get('format', 'wav')
    wav = None
    if sf2 and validate_sf2_path(sf2):  # Add validation
        wav = render_fluidsynth(create_midi(arr), sf2)
    if wav is None:
        wav = render_basic(arr)
    if wav is None:
        return jsonify({'error': 'No notes to render'}), 400
    if fmt == 'mp3':
        mp3 = wav_to_mp3(wav)
        if mp3:
            return send_file(io.BytesIO(mp3), mimetype='audio/mpeg', as_attachment=True, download_name='arrangement.mp3')
        return jsonify({'error': 'ffmpeg not available for MP3'}), 500
    return send_file(io.BytesIO(wav), mimetype='audio/wav', as_attachment=True, download_name='arrangement.wav')

@app.route('/api/render_sample', methods=['POST'])
def api_render_sample():
    data = request.get_json()
    sf2_path = data.get('sf2_path')
    bank = data.get('bank', 0)
    program = data.get('program', 0)
    pitch = data.get('pitch', 60)
    velocity = data.get('velocity', 100)
    duration = data.get('duration', 0.5)  # duration in seconds
    channel = data.get('channel', 0)  # MIDI channel (0-15, where 9 is drums)
    
    if not validate_sf2_path(sf2_path):
        return jsonify({'error': 'Invalid SF2 path'}), 400
    
    # Create a simple MIDI with one note
    tpb = 480
    bpm = 120
    note_ticks = int(duration * tpb * (bpm / 60))
    
    # Track with single note on the specified channel
    ch = channel & 0x0F  # Ensure channel is 0-15
    evs = [
        (0, bytes([0xB0 | ch, 0, (bank >> 7) & 0x7F])),  # Bank MSB
        (0, bytes([0xB0 | ch, 0x20, bank & 0x7F])),       # Bank LSB
        (0, bytes([0xC0 | ch, program & 0x7F])),          # Program change
        (0, bytes([0x90 | ch, pitch & 0x7F, velocity & 0x7F])),  # Note on
        (note_ticks, bytes([0x80 | ch, pitch & 0x7F, 0])),       # Note off
        (note_ticks, bytes([0xFF, 0x2F, 0x00]))              # End of track
    ]
    
    # Build MIDI
    hdr = b'MThd' + struct.pack('>I', 6) + struct.pack('>HHH', 0, 1, tpb)
    tb = b''
    prev = 0
    for at, data in evs:
        tb += _vlq(max(0, at - prev)) + data
        prev = at
    midi_bytes = hdr + b'MTrk' + struct.pack('>I', len(tb)) + tb
    
    # Render with fluidsynth
    wav = render_fluidsynth(midi_bytes, sf2_path)
    if wav is None:
        return jsonify({'error': 'Rendering failed'}), 500
    
    return send_file(io.BytesIO(wav), mimetype='audio/wav')

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Music Arranger')
    p.add_argument('--port', type=int, default=5000)
    p.add_argument('--debug', action='store_true')
    p.add_argument('--host', type=str, default='127.0.0.1', 
                   help='Host to bind to (default: 127.0.0.1, use 0.0.0.0 for network access)')
    a = p.parse_args()
    print(f"\n  ♪ Music Arranger → http://{a.host}:{a.port}\n")
    app.run(host=a.host, port=a.port, debug=a.debug)
