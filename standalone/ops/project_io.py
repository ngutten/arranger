"""Project save/load operations, plus single-pattern import/export."""

import json


def save_project(state, path: str):
    """Save project state to JSON file."""
    with open(path, 'w') as f:
        f.write(state.to_json())
    state._project_path = path


def load_project(state, path: str, sf2_loader=None):
    """Load project state from JSON file.

    Args:
        state: AppState to load into
        path: Path to JSON file
        sf2_loader: Optional callable(path) to reload SF2 into engine.
                    Called with the sf2 path hint from the project file.

    Raises whatever json.loads or file I/O raises on bad input.
    """
    with open(path) as f:
        state.load_json(f.read())
    state._project_path = path

    if sf2_loader and hasattr(state, '_sf2_path_hint') and state._sf2_path_hint:
        try:
            sf2_loader(state._sf2_path_hint)
        except Exception:
            pass


# ---- Single-pattern import / export ----

def export_pattern(pat, path: str):
    """Write a single Pattern to a JSON file.

    Format is {'type': 'pattern', 'pattern': <Pattern.to_dict()>}.
    Raises on I/O error.
    """
    data = {'type': 'pattern', 'pattern': pat.to_dict()}
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def export_beat_pattern(pat, path: str):
    """Write a single BeatPattern to a JSON file."""
    data = {'type': 'beat_pattern', 'pattern': pat.to_dict()}
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def import_pattern(state, path: str):
    """Load a single Pattern from a JSON file into state.

    Assigns a fresh ID so it never collides with existing patterns.
    Returns the new Pattern, or raises ValueError if the file is the wrong type.
    Raises on I/O or JSON parse errors.
    """
    from ..state import Pattern, PALETTE
    with open(path) as f:
        data = json.load(f)
    if data.get('type') != 'pattern':
        raise ValueError(
            f"Expected a pattern file (type='pattern'), got type={data.get('type')!r}. "
            "Use 'Import Beat Pattern' for beat patterns."
        )
    d = data['pattern']
    d['id'] = state.new_id()
    # Pick a fresh color if the original one collides visually; simple approach:
    # keep original color, just assign new id.
    pat = Pattern.from_dict(d)
    state.patterns.append(pat)
    state.sel_pat = pat.id
    state.sel_beat_pat = None
    state.notify('pattern_dialog')
    return pat


def import_beat_pattern(state, path: str):
    """Load a single BeatPattern from a JSON file into state.

    Returns the new BeatPattern, raises ValueError for wrong type.
    """
    from ..state import BeatPattern
    with open(path) as f:
        data = json.load(f)
    if data.get('type') != 'beat_pattern':
        raise ValueError(
            f"Expected a beat pattern file (type='beat_pattern'), got type={data.get('type')!r}. "
            "Use 'Import Pattern' for melodic patterns."
        )
    d = data['pattern']
    d['id'] = state.new_id()
    pat = BeatPattern.from_dict(d)
    state.beat_patterns.append(pat)
    state.sel_beat_pat = pat.id
    state.sel_pat = None
    state.notify('beat_pattern_dialog')
    return pat
