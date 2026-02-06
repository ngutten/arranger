"""SF2 SoundFont parser - extracts preset metadata from .sf2 files."""

import struct
from pathlib import Path


class SF2Info:
    def __init__(self, path):
        self.path = str(path)
        self.presets = []
        self.info = {}
        self._parse()

    def _parse(self):
        with open(self.path, 'rb') as f:
            if f.read(4) != b'RIFF':
                raise ValueError("Not RIFF")
            size = struct.unpack('<I', f.read(4))[0]
            if f.read(4) != b'sfbk':
                raise ValueError("Not SF2")
            self._chunks(f, f.tell(), f.tell() + size - 4)

    def _chunks(self, f, s, e):
        f.seek(s)
        while f.tell() < e:
            cid = f.read(4)
            if len(cid) < 4:
                break
            csz = struct.unpack('<I', f.read(4))[0]
            cs = f.tell()
            if cid == b'LIST':
                lt = f.read(4)
                if lt == b'INFO':
                    self._info(f, f.tell(), cs + csz)
                elif lt == b'pdta':
                    self._pdta(f, f.tell(), cs + csz)
            f.seek(cs + csz + (csz % 2))

    def _info(self, f, s, e):
        f.seek(s)
        while f.tell() < e:
            sid = f.read(4)
            if len(sid) < 4:
                break
            ssz = struct.unpack('<I', f.read(4))[0]
            data = f.read(ssz)
            if ssz % 2:
                f.read(1)
            try:
                self.info[sid.decode()] = data.rstrip(b'\x00').decode('ascii', errors='replace')
            except Exception:
                pass

    def _pdta(self, f, s, e):
        f.seek(s)
        while f.tell() < e:
            sid = f.read(4)
            if len(sid) < 4:
                break
            ssz = struct.unpack('<I', f.read(4))[0]
            ss = f.tell()
            if sid == b'phdr':
                for i in range(ssz // 38 - 1):
                    f.seek(ss + i * 38)
                    name = f.read(20).split(b'\x00')[0].decode('ascii', errors='replace')
                    prog = struct.unpack('<H', f.read(2))[0]
                    bank = struct.unpack('<H', f.read(2))[0]
                    self.presets.append({'name': name.strip(), 'bank': bank, 'program': prog})
            f.seek(ss + ssz + (ssz % 2))

    @property
    def name(self):
        return self.info.get('INAM', Path(self.path).stem)

    def to_dict(self):
        return {
            'path': self.path,
            'name': self.name,
            'info': self.info,
            'presets': sorted(self.presets, key=lambda p: (p['bank'], p['program']))
        }


def scan_directory(directory):
    """Scan a directory for .sf2 files and return SF2Info objects."""
    results = []
    d = Path(directory)
    if not d.exists():
        return results
    for f in sorted(d.glob('*.sf2')):
        try:
            results.append(SF2Info(f))
        except Exception:
            pass
    return results
