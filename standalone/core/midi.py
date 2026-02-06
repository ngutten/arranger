"""MIDI file writer - converts arrangement data to MIDI bytes."""

import struct


def _vlq(v):
    """Encode a value as MIDI variable-length quantity."""
    r = [v & 0x7F]
    v >>= 7
    while v:
        r.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(r))


def create_midi(arr, tpb=480):
    """Create MIDI file bytes from an arrangement dict.

    arr should have: bpm, tsNum, tsDen, tracks[]
    Each track: name, channel, bank, program, volume, placements[]
    Each placement: pattern{notes[], length}, time, transpose, repeats
    """
    bpm = arr.get('bpm', 120)
    tsn = arr.get('tsNum', 4)
    tsd = arr.get('tsDen', 4)
    tracks = []

    # Track 0: tempo + time sig
    t0 = [
        (0, bytes([0xFF, 0x51, 0x03]) + struct.pack('>I', int(60e6 / bpm))[1:]),
        (0, bytes([0xFF, 0x58, 0x04, tsn,
                   {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5}.get(tsd, 2), 24, 8])),
        (0, bytes([0xFF, 0x2F, 0x00]))
    ]
    tracks.append(t0)

    for trk in arr.get('tracks', []):
        evs = []
        ch = trk.get('channel', 0) & 0xF
        bank = trk.get('bank', 0)
        prog = trk.get('program', 0)
        evs.append((0, bytes([0xB0 | ch, 0, (bank >> 7) & 0x7F])))
        evs.append((0, bytes([0xB0 | ch, 0x20, bank & 0x7F])))
        evs.append((0, bytes([0xC0 | ch, prog & 0x7F])))
        nm = trk.get('name', '').encode('ascii', errors='replace')[:127]
        evs.append((0, bytes([0xFF, 0x03, len(nm)]) + nm))

        for pl in trk.get('placements', []):
            pat = pl.get('pattern', {})
            notes = pat.get('notes', [])
            bt = pl.get('time', 0)
            tr = pl.get('transpose', 0)
            reps = pl.get('repeats', 1)
            plen = pat.get('length', 4)
            if notes:
                plen = max(plen, max(n['start'] + n['duration'] for n in notes))
            for rep in range(reps):
                off = bt + rep * plen
                for n in notes:
                    p = max(0, min(127, n['pitch'] + tr))
                    v = max(1, min(127, n.get('velocity', 100)))
                    on = int((off + n['start']) * tpb)
                    of = int((off + n['start'] + n['duration']) * tpb)
                    evs.append((on, bytes([0x90 | ch, p, v])))
                    evs.append((of, bytes([0x80 | ch, p, 0])))

        evs.sort(key=lambda e: (e[0], 0 if e[1][0] & 0xF0 == 0x80 else 1))
        evs.append((evs[-1][0] if evs else 0, bytes([0xFF, 0x2F, 0x00])))
        tracks.append(evs)

    hdr = b'MThd' + struct.pack('>I', 6) + struct.pack('>HHH', 1, len(tracks), tpb)
    out = hdr
    for tevs in tracks:
        tb = b''
        prev = 0
        for at, data in tevs:
            tb += _vlq(max(0, at - prev)) + data
            prev = at
        out += b'MTrk' + struct.pack('>I', len(tb)) + tb
    return out
