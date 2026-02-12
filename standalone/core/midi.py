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


def _bend_curve_events(note_start_beats, duration, control_points, tpb, resolution=32):
    """Return list of (tick, lsb, msb) pitch bend events for one note.

    Uses Catmull-Rom interpolation.  Implicit zero-anchors are only added
    where the user has NOT placed a point, avoiding degenerate zero-length
    segments that corrupt the curve.  Emits a center-reset at the note-off
    tick (same tick as note-off, not one tick after) so it fires before the
    next note-on at the same position.
    """
    BEND_CENTER = 8192

    pts = sorted(control_points, key=lambda p: p[0])
    pts = [[max(0.0, min(duration, p[0])), p[1]] for p in pts]

    # Build anchor list: add implicit zeros only where no user point exists
    full = []
    if pts[0][0] > 1e-9:
        full.append([0.0, 0.0])
    full.extend(pts)
    if pts[-1][0] < duration - 1e-9:
        full.append([duration, 0.0])

    def interp(tc):
        # Strict < on right boundary so tc==duration falls into the final segment
        seg = 0
        for k in range(len(full) - 1):
            if full[k][0] <= tc < full[k + 1][0]:
                seg = k
                break
        if tc >= full[-1][0]:
            seg = len(full) - 2
        t1, v1 = full[seg]
        t2, v2 = full[min(len(full) - 1, seg + 1)]
        v0 = full[max(0, seg - 1)][1]
        v3 = full[min(len(full) - 1, seg + 2)][1]
        seg_len = t2 - t1
        if seg_len > 1e-9:
            lt = max(0.0, min(1.0, (tc - t1) / seg_len))
            return 0.5 * ((2 * v1) + (-v0 + v2) * lt +
                          (2 * v0 - 5 * v1 + 4 * v2 - v3) * lt * lt +
                          (-v0 + 3 * v1 - 3 * v2 + v3) * lt * lt * lt)
        return v1  # degenerate segment — return left value

    step = 1.0 / resolution
    t = 0.0
    result = []
    prev_bv = -1
    while t <= duration + step * 0.5:
        tc = min(t, duration)
        sem = interp(tc)
        ratio = max(-1.0, min(1.0, sem / 2.0))
        bv = int(BEND_CENTER + ratio * (BEND_CENTER - 1 if ratio >= 0 else BEND_CENTER))
        if bv != prev_bv:
            tick = int((note_start_beats + tc) * tpb)
            result.append((tick, bv & 0x7F, (bv >> 7) & 0x7F))
            prev_bv = bv
        t += step

    # Reset to center at the note-off tick so it fires before the next
    # note-on that may start at the same position.
    off_tick = int((note_start_beats + duration) * tpb)
    if prev_bv != BEND_CENTER:
        result.append((off_tick, 0x00, 0x40))  # 0x4000 = 8192 center

    return result


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
                    bend_pts = n.get('bend', [])
                    if bend_pts:
                        for tick, lsb, msb in _bend_curve_events(
                                off + n['start'], n['duration'], bend_pts, tpb):
                            evs.append((tick, bytes([0xE0 | ch, lsb, msb])))

        # Sort: note-offs (pri 0) before bend/note-ons (pri 1) at same tick
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
