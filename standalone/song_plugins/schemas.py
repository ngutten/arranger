"""Validators for annotation ``data`` payloads.

Each schema literal has a matching ``validate_<schema>`` function that
returns ``(ok: bool, reason: str)``. ``validate(schema, data)`` dispatches.
"""

from __future__ import annotations
from typing import Any, Tuple


def validate_scalar_curve(data: Any) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "scalar_curve must be a dict"
    if 'beats' not in data or 'values' not in data:
        return False, "scalar_curve requires 'beats' and 'values'"
    b = data['beats']
    v = data['values']
    try:
        if len(b) != len(v):
            return False, "scalar_curve 'beats' and 'values' must have equal length"
    except TypeError:
        return False, "scalar_curve 'beats' / 'values' must be sequences"
    return True, ""


def validate_multi_curve(data: Any) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "multi_curve must be a dict"
    if 'beats' not in data or 'series' not in data:
        return False, "multi_curve requires 'beats' and 'series'"
    if not isinstance(data['series'], dict):
        return False, "multi_curve 'series' must be dict[name -> values]"
    try:
        blen = len(data['beats'])
    except TypeError:
        return False, "multi_curve 'beats' must be a sequence"
    for name, vs in data['series'].items():
        try:
            if len(vs) != blen:
                return False, f"series '{name}' length mismatches beats"
        except TypeError:
            return False, f"series '{name}' must be a sequence"
    return True, ""


def validate_grid2d(data: Any) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "grid2d must be a dict"
    if 'rows' not in data or 'cols' not in data or 'cells' not in data:
        return False, "grid2d requires 'rows', 'cols', 'cells'"
    try:
        rows = int(data['rows']); cols = int(data['cols'])
    except (TypeError, ValueError):
        return False, "grid2d 'rows'/'cols' must be ints"
    cells = data['cells']
    try:
        if len(cells) != rows * cols:
            return False, "grid2d 'cells' length must equal rows*cols"
    except TypeError:
        return False, "grid2d 'cells' must be a sequence"
    return True, ""


def validate_events(data: Any) -> Tuple[bool, str]:
    if not isinstance(data, list):
        return False, "events must be a list"
    for i, e in enumerate(data):
        if not isinstance(e, dict):
            return False, f"event[{i}] must be a dict"
        if 'beat' not in e:
            return False, f"event[{i}] missing 'beat'"
    return True, ""


def _validate_tag_value(v: Any) -> Tuple[bool, str]:
    """A tag value is either a str (short label) or a dict with at least
    ``label``. See the note_tags / placement_tags contract in api.py.
    """
    if isinstance(v, str):
        return True, ""
    if isinstance(v, dict):
        if 'label' not in v:
            return False, "dict tag values must include 'label'"
        if not isinstance(v['label'], str):
            return False, "'label' must be a string"
        color = v.get('color')
        if color is not None and not isinstance(color, str):
            return False, "'color' must be a string if present"
        priority = v.get('priority')
        if priority is not None and not isinstance(priority, int):
            return False, "'priority' must be an int if present"
        return True, ""
    return False, "tag value must be a str or dict"


def validate_note_tags(data: Any) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "note_tags must be a dict[note_id -> tag]"
    for k, v in data.items():
        if not isinstance(k, int):
            return False, "note_tags keys must be int (note_id)"
        ok, why = _validate_tag_value(v)
        if not ok:
            return False, f"note_tags[{k}]: {why}"
    return True, ""


def validate_placement_tags(data: Any) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "placement_tags must be a dict[placement_id -> tag]"
    for k, v in data.items():
        if not isinstance(k, int):
            return False, "placement_tags keys must be int (placement_id)"
        ok, why = _validate_tag_value(v)
        if not ok:
            return False, f"placement_tags[{k}]: {why}"
    return True, ""


def validate_regions(data: Any) -> Tuple[bool, str]:
    """Regions: a list of box dicts to paint behind notes.

    Each region has start_beat/end_beat (required), optional min_pitch/
    max_pitch (int MIDI note; full-height if absent), optional note_ids
    (sequence of ints; renderer may auto-fit extent), label (str),
    color ("#rrggbb"), payload (any), and optional pattern_id /
    variation_id — when set, coordinates are pattern-local and the
    region only applies within that pattern.
    """
    if not isinstance(data, list):
        return False, "regions must be a list"
    for i, r in enumerate(data):
        if not isinstance(r, dict):
            return False, f"regions[{i}] must be a dict"
        for key in ("start_beat", "end_beat"):
            if key not in r:
                return False, f"regions[{i}] missing '{key}'"
            try:
                float(r[key])
            except (TypeError, ValueError):
                return False, f"regions[{i}].{key} must be a number"
        if float(r["end_beat"]) < float(r["start_beat"]):
            return False, f"regions[{i}] has end_beat < start_beat"
        for key in ("min_pitch", "max_pitch"):
            if key in r and r[key] is not None:
                if not isinstance(r[key], int):
                    return False, f"regions[{i}].{key} must be int or None"
        if "note_ids" in r and r["note_ids"] is not None:
            try:
                for nid in r["note_ids"]:
                    if not isinstance(nid, int):
                        return False, (f"regions[{i}].note_ids "
                                       f"must be a sequence of ints")
            except TypeError:
                return False, f"regions[{i}].note_ids must be iterable"
        for key in ("pattern_id", "variation_id"):
            if key in r and r[key] is not None:
                if not isinstance(r[key], int):
                    return False, f"regions[{i}].{key} must be int or None"
        color = r.get("color")
        if color is not None and not isinstance(color, str):
            return False, f"regions[{i}].color must be a string"
        label = r.get("label")
        if label is not None and not isinstance(label, str):
            return False, f"regions[{i}].label must be a string"
    return True, ""


def validate_stats(data: Any) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "stats must be a dict of scalar values"
    return True, ""


def validate_custom(data: Any) -> Tuple[bool, str]:
    # Custom payloads are plugin-defined. Only require that they're JSON-shaped.
    return True, ""


_VALIDATORS = {
    'scalar_curve': validate_scalar_curve,
    'multi_curve': validate_multi_curve,
    'grid2d': validate_grid2d,
    'events': validate_events,
    'note_tags': validate_note_tags,
    'placement_tags': validate_placement_tags,
    'regions': validate_regions,
    'stats': validate_stats,
    'custom': validate_custom,
}


def validate(schema: str, data: Any) -> Tuple[bool, str]:
    if schema not in _VALIDATORS:
        return False, f"unknown schema '{schema}'"
    return _VALIDATORS[schema](data)
