"""MIDI file loader — converts MIDI bytes into AppState tracks/patterns/placements.

Two-pass approach:
  Pass 1 — `parse_midi()` + `midi_to_arrangement()`: decode SMF, then emit one
           arranger Track per (MIDI-track, channel) and one Pattern per Track
           containing every note from that channel.
  Pass 2 — `segment_track()`: scan the single big pattern for measure-aligned
           repeats. If found, replace it with a smaller Pattern + multiple
           Placements (each with `repeats` for runs of identical chunks).
"""

from __future__ import annotations

import struct
from typing import Optional

from ..state import (Note, Pattern, Placement, Track, PALETTE, GM_NAMES)


# ---------- Low-level SMF decode ----------

def _read_vlq(data, p):
    v = 0
    while True:
        b = data[p]
        p += 1
        v = (v << 7) | (b & 0x7F)
        if not (b & 0x80):
            return v, p


def _parse_track_events(data, start, length):
    """Parse one MTrk chunk. Returns a list of tuples; each tuple's shape
    depends on event kind:
        ('meta', abs_tick, mtype, payload_bytes)
        (status_high_nibble, abs_tick, ch, d1, d2)   # 0x80,0x90,0xA0,0xB0,0xE0
        (status_high_nibble, abs_tick, ch, d1, 0)    # 0xC0,0xD0
    """
    end = start + length
    p = start
    abs_tick = 0
    last_status = None
    out = []
    while p < end:
        delta, p = _read_vlq(data, p)
        abs_tick += delta
        b = data[p]
        if b & 0x80:
            status = b
            p += 1
        else:
            if last_status is None:
                raise ValueError("running status with no prior status byte")
            status = last_status
        if status == 0xFF:
            mtype = data[p]; p += 1
            mlen, p = _read_vlq(data, p)
            md = bytes(data[p:p + mlen])
            p += mlen
            out.append(('meta', abs_tick, mtype, md))
            # meta does not set running status
        elif status in (0xF0, 0xF7):
            slen, p = _read_vlq(data, p)
            p += slen
            # ignore sysex
        else:
            kind = status & 0xF0
            ch = status & 0x0F
            if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                d1 = data[p]; p += 1
                d2 = data[p]; p += 1
                out.append((kind, abs_tick, ch, d1, d2))
            elif kind in (0xC0, 0xD0):
                d1 = data[p]; p += 1
                out.append((kind, abs_tick, ch, d1, 0))
            last_status = status
    return out


def parse_midi(data: bytes) -> dict:
    """Parse MIDI file bytes into {tpb, format, tracks: [events]}."""
    if len(data) < 14 or data[:4] != b'MThd':
        raise ValueError('not a MIDI file (missing MThd)')
    hlen = struct.unpack('>I', data[4:8])[0]
    fmt, ntrks, div = struct.unpack('>HHH', data[8:14])
    if div & 0x8000:
        raise ValueError('SMPTE-coded division is not supported')
    tpb = div
    p = 8 + hlen
    tracks = []
    for _ in range(ntrks):
        if data[p:p + 4] != b'MTrk':
            raise ValueError(f'expected MTrk at offset {p}')
        tlen = struct.unpack('>I', data[p + 4:p + 8])[0]
        tracks.append(_parse_track_events(data, p + 8, tlen))
        p += 8 + tlen
    return {'tpb': tpb, 'format': fmt, 'tracks': tracks}


# ---------- Pass 1: build flat arranger tracks ----------

def midi_to_arrangement(parsed: dict) -> dict:
    """Walk parsed MIDI events and build flat arranger tracks.

    Returns dict:
        {'bpm': float, 'ts_num': int, 'ts_den': int,
         'tracks': [
             {'name': str, 'channel': int, 'bank': int, 'program': int,
              'notes': [Note], 'length_beats': float}, ...
         ]}

    Each (MIDI-track, channel) pair becomes one arranger track.  Track names
    come from the SMF track-name meta event (0x03) when present, otherwise
    from the GM program name (or "Drums" for channel 9).
    """
    tpb = max(1, parsed['tpb'])
    bpm = 120.0
    ts_num, ts_den = 4, 4
    saw_tempo = False
    saw_ts = False

    out_tracks = []

    for evs in parsed['tracks']:
        track_name = ''
        ch_program = [0] * 16
        ch_bank_msb = [0] * 16
        ch_bank_lsb = [0] * 16
        per_ch = {}        # ch -> arranger track dict
        active = {}        # (ch, pitch) -> (start_tick, vel)

        def get_arr(ch):
            if ch in per_ch:
                return per_ch[ch]
            prog = ch_program[ch]
            bank = (ch_bank_msb[ch] << 7) | ch_bank_lsb[ch]
            if track_name:
                name = track_name
            elif ch == 9:
                name = 'Drums'
            elif 0 <= prog < len(GM_NAMES):
                name = GM_NAMES[prog]
            else:
                name = f'Track ch{ch + 1}'
            per_ch[ch] = {
                'name': name, 'channel': ch, 'bank': bank, 'program': prog,
                'notes': [], '_last_tick': 0,
            }
            return per_ch[ch]

        for ev in evs:
            tag = ev[0]
            if tag == 'meta':
                _, _, mtype, md = ev
                if mtype == 0x03 and md:
                    try:
                        track_name = md.decode('utf-8', errors='replace').strip()
                    except Exception:
                        pass
                elif mtype == 0x51 and len(md) == 3 and not saw_tempo:
                    uspb = (md[0] << 16) | (md[1] << 8) | md[2]
                    if uspb > 0:
                        bpm = round(60_000_000.0 / uspb, 3)
                    saw_tempo = True
                elif mtype == 0x58 and len(md) >= 4 and not saw_ts:
                    ts_num = md[0] or 4
                    ts_den = 1 << md[1]
                    saw_ts = True
                continue

            kind, tick, ch, d1, d2 = ev
            if kind == 0x90 and d2 > 0:
                key = (ch, d1)
                arr = get_arr(ch)
                # If a duplicate note-on arrives without a note-off, close it.
                prev = active.pop(key, None)
                if prev is not None:
                    on_tick, on_vel = prev
                    if tick > on_tick:
                        arr['notes'].append(Note(
                            pitch=d1,
                            start=on_tick / tpb,
                            duration=(tick - on_tick) / tpb,
                            velocity=on_vel,
                        ))
                        arr['_last_tick'] = max(arr['_last_tick'], tick)
                active[key] = (tick, d2)
                # Stamp the arr track so it's created even if note has zero length.
                arr['_last_tick'] = max(arr['_last_tick'], tick)
            elif kind == 0x80 or (kind == 0x90 and d2 == 0):
                key = (ch, d1)
                prev = active.pop(key, None)
                if prev is None:
                    continue
                on_tick, on_vel = prev
                arr = get_arr(ch)
                if tick > on_tick:
                    arr['notes'].append(Note(
                        pitch=d1,
                        start=on_tick / tpb,
                        duration=(tick - on_tick) / tpb,
                        velocity=on_vel,
                    ))
                    arr['_last_tick'] = max(arr['_last_tick'], tick)
            elif kind == 0xC0:
                ch_program[ch] = d1
                # Update name/program for an empty arranger track on this channel
                # so a program-change before any note still wins.
                if ch in per_ch and not per_ch[ch]['notes']:
                    per_ch[ch]['program'] = d1
                    if not track_name:
                        per_ch[ch]['name'] = (
                            'Drums' if ch == 9
                            else GM_NAMES[d1] if 0 <= d1 < len(GM_NAMES)
                            else f'Track ch{ch + 1}'
                        )
            elif kind == 0xB0:
                if d1 == 0:
                    ch_bank_msb[ch] = d2
                elif d1 == 32:
                    ch_bank_lsb[ch] = d2
                # Refresh bank on existing-but-empty arranger track
                if ch in per_ch and not per_ch[ch]['notes']:
                    per_ch[ch]['bank'] = (
                        (ch_bank_msb[ch] << 7) | ch_bank_lsb[ch]
                    )

        # Close any notes still on at end of track.
        for (ch, pitch), (on_tick, on_vel) in active.items():
            arr = per_ch.get(ch)
            if arr is None:
                continue
            end_tick = max(arr['_last_tick'], on_tick + 1)
            arr['notes'].append(Note(
                pitch=pitch,
                start=on_tick / tpb,
                duration=(end_tick - on_tick) / tpb,
                velocity=on_vel,
            ))

        for arr in per_ch.values():
            length = arr.pop('_last_tick') / tpb
            arr['length_beats'] = length
            out_tracks.append(arr)

    out_tracks = [t for t in out_tracks if t['notes']]
    return {
        'bpm': bpm, 'ts_num': ts_num, 'ts_den': ts_den,
        'tracks': out_tracks,
    }


# ---------- Pass 2: pattern segmentation ----------

def _round_up_to_measure(beats: float, measure_beats: float) -> float:
    if beats <= 0:
        return measure_beats
    n = int(beats / measure_beats - 1e-9) + 1
    return n * measure_beats


def segment_track(notes: list, length_beats: float, ts_num: int,
                  *, min_savings: int = 1) -> Optional[list]:
    """Try to break a single track's notes into repeating measure-aligned chunks.

    Returns a list of (sub_notes, sub_len_beats, start_beat, repeats) tuples,
    or None if no good split was found (caller should keep one big pattern).

    A chunk size in measures is considered if:
      * it divides the total length evenly
      * every note fits inside its starting chunk (start + duration <= chunk_end)
      * deduplicating produces at least `min_savings` redundant chunks
    Among candidates, picks the one with the highest savings ratio, preferring
    longer chunks (more musical phrasing) on ties.
    """
    measure = float(ts_num)
    total_measures = int(round(length_beats / measure))
    if total_measures < 2:
        return None

    best = None
    for m in (1, 2, 4, 8, 16):
        if m * 2 > total_measures or total_measures % m != 0:
            continue
        chunk_beats = m * measure
        n_chunks = total_measures // m

        # Place each note into its starting chunk; bail if any note overruns.
        chunk_notes = [[] for _ in range(n_chunks)]
        overrun = False
        for n in notes:
            ci = int(n.start // chunk_beats + 1e-9)
            if ci < 0 or ci >= n_chunks:
                # note starts past arrangement end; ignore for this candidate
                overrun = True
                break
            rel_start = n.start - ci * chunk_beats
            if rel_start + n.duration > chunk_beats + 1e-6:
                overrun = True
                break
            chunk_notes[ci].append(n)
        if overrun:
            continue

        # Build a hashable signature per chunk.
        sigs = []
        for cn in chunk_notes:
            sig = tuple(sorted(
                (round(n.start - (n.start // chunk_beats) * chunk_beats, 6),
                 n.pitch,
                 round(n.duration, 6),
                 n.velocity)
                for n in cn
            ))
            sigs.append(sig)

        distinct = len(set(s for s in sigs if s))
        non_empty = sum(1 for s in sigs if s)
        savings = non_empty - distinct
        if savings < min_savings:
            continue
        ratio = savings / max(1, non_empty)
        score = (ratio, m)  # higher ratio first; longer chunks break ties
        if best is None or score > best[0]:
            best = (score, m, chunk_beats, sigs, chunk_notes)

    if best is None:
        return None

    _, _m, chunk_beats, sigs, chunk_notes = best

    # Build sub-patterns: one per distinct non-empty signature, using the
    # first chunk that has each signature.
    sig_to_notes = {}
    for ci, sig in enumerate(sigs):
        if not sig or sig in sig_to_notes:
            continue
        lo = ci * chunk_beats
        sub = []
        for n in chunk_notes[ci]:
            sub.append(Note(
                pitch=n.pitch,
                start=n.start - lo,
                duration=n.duration,
                velocity=n.velocity,
            ))
        sig_to_notes[sig] = sub

    # Group consecutive identical chunks into runs (so we can use `repeats`).
    out = []
    i = 0
    while i < len(sigs):
        s = sigs[i]
        if not s:
            i += 1
            continue
        j = i
        while j + 1 < len(sigs) and sigs[j + 1] == s:
            j += 1
        out.append((sig_to_notes[s], chunk_beats, i * chunk_beats, j - i + 1))
        i = j + 1
    return out


# ---------- High-level: ingest a MIDI file into AppState ----------

def import_midi(state, path: str, *, segment: bool = True) -> dict:
    """Load a MIDI file into `state`, replacing all tracks/patterns/placements.

    Leaves signal_graph, beat_*, automation_* and SF2 alone.

    Returns a small stats dict for the caller (track count, pattern count,
    whether segmentation found repeats).
    """
    with open(path, 'rb') as f:
        data = f.read()
    parsed = parse_midi(data)
    arr = midi_to_arrangement(parsed)

    # Wipe melodic content; preserve the rest.
    state.patterns.clear()
    state.tracks.clear()
    state.placements.clear()
    state.variations.clear()
    state.bpm = float(arr['bpm'])
    state.ts_num = int(arr['ts_num'])
    state.ts_den = int(arr['ts_den'])
    state.sel_pat = None
    state.sel_trk = None
    state.sel_pl = None
    state.sel_variation = None

    measure = float(state.ts_num)
    n_segmented = 0

    for ti, t in enumerate(arr['tracks']):
        track = Track(
            id=state.new_id(),
            name=t['name'],
            channel=t['channel'],
            bank=t['bank'],
            program=t['program'],
            volume=100,
        )
        state.tracks.append(track)

        notes = t['notes']
        length = max(_round_up_to_measure(t['length_beats'], measure), measure)

        segments = None
        if segment:
            segments = segment_track(notes, length, state.ts_num)

        color = PALETTE[ti % len(PALETTE)]

        if segments:
            n_segmented += 1
            # One Pattern per distinct sub-pattern, one Placement per run.
            cache = {}  # id(notes_list) -> Pattern
            for sub_notes, sub_len, start_beat, reps in segments:
                key = id(sub_notes)
                pat = cache.get(key)
                if pat is None:
                    pat = Pattern(
                        id=state.new_id(),
                        name=f'{track.name} {len(cache) + 1}',
                        length=sub_len,
                        notes=sub_notes,
                        color=color,
                    )
                    state.patterns.append(pat)
                    cache[key] = pat
                state.placements.append(Placement(
                    id=state.new_id(),
                    track_id=track.id,
                    pattern_id=pat.id,
                    time=start_beat,
                    repeats=reps,
                ))
        else:
            pat = Pattern(
                id=state.new_id(),
                name=track.name,
                length=length,
                notes=notes,
                color=color,
            )
            state.patterns.append(pat)
            state.placements.append(Placement(
                id=state.new_id(),
                track_id=track.id,
                pattern_id=pat.id,
                time=0.0,
                repeats=1,
            ))

    if state.tracks:
        state.sel_trk = state.tracks[0].id
    if state.patterns:
        state.sel_pat = state.patterns[0].id

    state.notify()
    return {
        'tracks': len(state.tracks),
        'patterns': len(state.patterns),
        'placements': len(state.placements),
        'segmented_tracks': n_segmented,
    }
