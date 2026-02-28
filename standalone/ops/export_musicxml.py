"""MusicXML export — converts an AppState arrangement to MusicXML 3.1."""

import xml.etree.ElementTree as ET
from xml.dom.minidom import parseString

# MusicXML DOCTYPE header prepended to the serialised output.
_DOCTYPE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE score-partwise PUBLIC\n'
    '  "-//Recordare//DTD MusicXML 3.1 Partwise//EN"\n'
    '  "http://www.musicxml.org/dtds/partwise.dtd">\n'
)

# Semitone → (step, alter) mapping (sharps).
_PC_TO_STEP = [
    ('C', 0), ('C', 1), ('D', 0), ('D', 1), ('E', 0),
    ('F', 0), ('F', 1), ('G', 0), ('G', 1), ('A', 0), ('A', 1), ('B', 0),
]

# Beat duration → MusicXML note-type string (longest match wins).
_DUR_TYPES = [
    (4.0, 'whole'),
    (2.0, 'half'),
    (1.0, 'quarter'),
    (0.5, 'eighth'),
    (0.25, '16th'),
    (0.125, '32nd'),
    (0.0625, '64th'),
]


def _dur_type(beats):
    for threshold, name in _DUR_TYPES:
        if beats >= threshold - 1e-6:
            return name
    return '64th'


def _sub(parent, tag, text=None, **attrib):
    """Create a sub-element, optionally with text content and attributes."""
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = str(text)
    return el


def create_musicxml(state):
    """Return MusicXML bytes for *state*.

    Only melodic tracks that have at least one placed note are exported.
    Lyrics attached to notes are written as ``<lyric number="1">`` elements
    with ``<syllabic>single</syllabic>`` (the most broadly compatible form).
    """
    DIVS = 480  # divisions per quarter note (matches MIDI tpb)

    root = ET.Element('score-partwise', version='3.1')

    # ------------------------------------------------------------------ #
    # 1. Collect per-track note lists (absolute beat positions)           #
    # ------------------------------------------------------------------ #
    parts_data = []   # [(part_id, track_name, [note_dict, ...]), ...]

    for t in state.tracks:
        flat_notes = []
        for pl in state.placements:
            if pl.track_id != t.id:
                continue
            pat = state.find_pattern(pl.pattern_id)
            if not pat:
                continue
            tr = state.compute_transpose(pl)
            reps = pl.repeats or 1
            for rep in range(reps):
                off = pl.time + rep * pat.length
                for n in pat.notes:
                    flat_notes.append({
                        'pitch':    max(0, min(127, n.pitch + tr)),
                        'start':    off + n.start,
                        'duration': n.duration,
                        'velocity': n.velocity,
                        'lyric':    n.lyric or '',
                    })

        if not flat_notes:
            continue

        flat_notes.sort(key=lambda n: (n['start'], n['pitch']))
        parts_data.append((f'P{t.id}', t.name, flat_notes))

    if not parts_data:
        # Nothing to export — return a minimal valid document.
        parts_data = [('P1', 'Empty', [])]

    # ------------------------------------------------------------------ #
    # 2. Part-list                                                        #
    # ------------------------------------------------------------------ #
    part_list_el = _sub(root, 'part-list')
    for part_id, track_name, _ in parts_data:
        sp = _sub(part_list_el, 'score-part', id=part_id)
        _sub(sp, 'part-name', track_name)

    # ------------------------------------------------------------------ #
    # 3. Parts                                                            #
    # ------------------------------------------------------------------ #
    bpm_beats = state.ts_num * (4.0 / state.ts_den)   # beats per measure

    for part_id, track_name, flat_notes in parts_data:
        part_el = _sub(root, 'part', id=part_id)

        if not flat_notes:
            # One empty measure
            m = _sub(part_el, 'measure', number='1')
            _attrs_block(m, DIVS, state.ts_num, state.ts_den)
            rest = _sub(m, 'note')
            _sub(rest, 'rest', **{'measure': 'yes'})
            _sub(rest, 'duration', int(bpm_beats * DIVS))
            _sub(rest, 'type', 'whole')
            continue

        total_beats = max(n['start'] + n['duration'] for n in flat_notes)
        num_measures = max(1, int(total_beats / bpm_beats) + 1)

        note_idx = 0   # pointer into flat_notes

        for meas_num in range(1, num_measures + 1):
            m_start = (meas_num - 1) * bpm_beats
            m_end   = meas_num * bpm_beats

            m = _sub(part_el, 'measure', number=str(meas_num))

            if meas_num == 1:
                _attrs_block(m, DIVS, state.ts_num, state.ts_den)

            # Gather notes whose onset falls within this measure
            meas_notes = []
            while note_idx < len(flat_notes) and flat_notes[note_idx]['start'] < m_end:
                n = flat_notes[note_idx]
                if n['start'] >= m_start:
                    meas_notes.append(n)
                note_idx += 1

            cursor = m_start  # absolute beat position within the measure

            if not meas_notes:
                # Write a whole-measure rest
                rest = _sub(m, 'note')
                _sub(rest, 'rest', **{'measure': 'yes'})
                _sub(rest, 'duration', int(round(bpm_beats * DIVS)))
                _sub(rest, 'type', _dur_type(bpm_beats))
                continue

            for n in meas_notes:
                # Fill gap before this note with a rest
                gap = n['start'] - cursor
                if gap > 1e-4:
                    gap = min(gap, m_end - cursor)
                    rest = _sub(m, 'note')
                    _sub(rest, 'rest')
                    _sub(rest, 'duration', max(1, int(round(gap * DIVS))))
                    _sub(rest, 'type', _dur_type(gap))
                    cursor += gap

                # Duration clamped so the note doesn't exceed the measure
                dur = min(n['duration'], m_end - n['start'])
                dur = max(dur, 1.0 / DIVS)

                note_el = _sub(m, 'note')
                _pitch_el(note_el, n['pitch'])
                _sub(note_el, 'duration', max(1, int(round(dur * DIVS))))
                _sub(note_el, 'type', _dur_type(dur))
                _dynamics_el(note_el, n['velocity'])

                if n['lyric']:
                    lyric_el = _sub(note_el, 'lyric', number='1')
                    _sub(lyric_el, 'syllabic', 'single')
                    _sub(lyric_el, 'text', n['lyric'])

                cursor = n['start'] + n['duration']

            # Fill any remaining space in the measure with a rest
            tail = m_end - cursor
            if tail > 1e-4:
                rest = _sub(m, 'note')
                _sub(rest, 'rest')
                _sub(rest, 'duration', max(1, int(round(tail * DIVS))))
                _sub(rest, 'type', _dur_type(tail))

    # ------------------------------------------------------------------ #
    # 4. Serialise with pretty-printing                                   #
    # ------------------------------------------------------------------ #
    raw_xml = ET.tostring(root, encoding='unicode')
    dom = parseString(raw_xml)
    pretty = dom.toprettyxml(indent='  ', encoding=None)
    # toprettyxml adds its own <?xml?> declaration; strip it and prepend ours.
    lines = pretty.split('\n')
    if lines and lines[0].startswith('<?xml'):
        lines = lines[1:]
    body = '\n'.join(lines)
    return (_DOCTYPE + body).encode('utf-8')


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _attrs_block(measure_el, divs, ts_num, ts_den):
    attrs = _sub(measure_el, 'attributes')
    _sub(attrs, 'divisions', divs)
    key_el = _sub(attrs, 'key')
    _sub(key_el, 'fifths', '0')
    time_el = _sub(attrs, 'time')
    _sub(time_el, 'beats', ts_num)
    _sub(time_el, 'beat-type', ts_den)
    clef_el = _sub(attrs, 'clef')
    _sub(clef_el, 'sign', 'G')
    _sub(clef_el, 'line', '2')


def _pitch_el(note_el, midi_pitch):
    pc = midi_pitch % 12
    octave = midi_pitch // 12 - 1
    step, alter = _PC_TO_STEP[pc]
    p = _sub(note_el, 'pitch')
    _sub(p, 'step', step)
    if alter:
        _sub(p, 'alter', alter)
    _sub(p, 'octave', octave)


def _dynamics_el(note_el, velocity):
    """Write a <dynamics> element inside <notations> derived from MIDI velocity."""
    # Map velocity 1-127 to MF/F/FF/P/PP etc.
    if velocity >= 112:
        dyn = 'fff'
    elif velocity >= 96:
        dyn = 'ff'
    elif velocity >= 80:
        dyn = 'f'
    elif velocity >= 64:
        dyn = 'mf'
    elif velocity >= 48:
        dyn = 'mp'
    elif velocity >= 32:
        dyn = 'p'
    elif velocity >= 16:
        dyn = 'pp'
    else:
        dyn = 'ppp'
    notations = _sub(note_el, 'notations')
    dyns = _sub(notations, 'dynamics')
    _sub(dyns, dyn)
