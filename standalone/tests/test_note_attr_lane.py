"""Phase 3 tests: piano-roll note-attr lane editing logic.

The VelocityWidget edit methods only touch `self.parent_roll` and the note
objects, so they can be exercised with a lightweight stub instead of a live
Qt widget/QApplication.
"""
import math
import types

from standalone.ui.piano_roll import (
    VelocityWidget, _attr_log_norm, _attr_value_from_norm,
)
from standalone.state import Note


ATTACK = {'id': 'attack', 'hint': 'continuous', 'default': 1.0,
          'min': 0.125, 'max': 8.0}
EXCITATION = {'id': 'excitation', 'hint': 'categorical', 'default': 0.0,
              'min': 0.0, 'max': 1.0, 'choices': ['Pluck', 'Strike']}


class _Event:
    def __init__(self, x, y, button='left'):
        self._p = types.SimpleNamespace(x=lambda: x, y=lambda: y)
        self._b = button

    def pos(self):
        return self._p


def _roll(notes, decl, lane=None, selected=None):
    """Minimal parent_roll stub satisfying the lane edit methods."""
    hbar = types.SimpleNamespace(value=lambda: 0)
    scroll = types.SimpleNamespace(horizontalScrollBar=lambda: hbar)
    return types.SimpleNamespace(
        _active_lane=lane or decl['id'],
        _active_attr_decl=decl,
        _get_edit_notes=lambda: notes,
        _selected=set(selected or []),
        scroll_area=scroll,
        BW=80,
        state=types.SimpleNamespace(snap=0.25),
        _is_variation_mode=lambda: False,
        refresh=lambda: None,
    )


def _fake_widget(roll):
    w = types.SimpleNamespace(parent_roll=roll)
    # Bind the real (parent_roll-only) helper so _set_attr_at can call it.
    w._target_indices = lambda notes, ev: VelocityWidget._target_indices(w, notes, ev)
    return w


def _y_for_value(decl, value):
    norm = _attr_log_norm(decl, value)
    return (1 - norm) * 48


# -- pure mapping -----------------------------------------------------------

def test_log_mapping_roundtrip_and_neutral():
    for v in (0.125, 0.5, 1.0, 2.0, 8.0):
        assert abs(_attr_value_from_norm(ATTACK, _attr_log_norm(ATTACK, v)) - v) < 1e-3
    assert abs(_attr_log_norm(ATTACK, 1.0) - 0.5) < 1e-6  # neutral mid-lane
    # clamps out-of-range
    assert _attr_log_norm(ATTACK, 100.0) == 1.0
    assert _attr_log_norm(ATTACK, 0.001) == 0.0


# -- continuous attr editing ------------------------------------------------

def test_continuous_set_value():
    notes = [Note(pitch=60, start=0.0, duration=1.0, note_id=1)]
    roll = _roll(notes, ATTACK, selected=[0])
    w = _fake_widget(roll)
    VelocityWidget._set_attr_at(w, _Event(2, _y_for_value(ATTACK, 2.0)), True)
    assert abs(notes[0].attrs['attack'] - 2.0) < 0.05


def test_continuous_neutral_drops_key():
    notes = [Note(pitch=60, start=0.0, duration=1.0, note_id=1,
                  attrs={'attack': 3.0})]
    roll = _roll(notes, ATTACK, selected=[0])
    w = _fake_widget(roll)
    # Drag to the default (1.0) → key removed (note reverts to synth default)
    VelocityWidget._set_attr_at(w, _Event(2, _y_for_value(ATTACK, 1.0)), True)
    assert 'attack' not in notes[0].attrs


# -- categorical attr editing -----------------------------------------------

def test_categorical_cycles_choices():
    notes = [Note(pitch=60, start=0.0, duration=1.0, note_id=1)]
    roll = _roll(notes, EXCITATION, selected=[0])
    w = _fake_widget(roll)
    # unset -> 0 (Pluck) -> 1 (Strike) -> 0 ...
    VelocityWidget._set_attr_at(w, _Event(2, 20), True)
    assert notes[0].attrs['excitation'] == 0.0
    VelocityWidget._set_attr_at(w, _Event(2, 20), True)
    assert notes[0].attrs['excitation'] == 1.0
    VelocityWidget._set_attr_at(w, _Event(2, 20), True)
    assert notes[0].attrs['excitation'] == 0.0
    # drag (is_press=False) must NOT advance a categorical choice
    VelocityWidget._set_attr_at(w, _Event(2, 20), False)
    assert notes[0].attrs['excitation'] == 0.0


def test_right_click_clears_attr():
    notes = [Note(pitch=60, start=0.0, duration=1.0, note_id=1,
                  attrs={'excitation': 1.0})]
    roll = _roll(notes, EXCITATION, selected=[0])
    w = _fake_widget(roll)
    VelocityWidget._clear_attr_at(w, _Event(2, 20))
    assert 'excitation' not in notes[0].attrs


# -- targeting --------------------------------------------------------------

def test_nearest_note_when_no_selection():
    notes = [Note(pitch=60, start=0.0, duration=1.0, note_id=1),
             Note(pitch=64, start=2.0, duration=1.0, note_id=2)]
    roll = _roll(notes, ATTACK)  # no selection
    w = _fake_widget(roll)
    # x near beat 2 (BW=80 → x=160) should hit the second note only
    VelocityWidget._set_attr_at(w, _Event(160, _y_for_value(ATTACK, 4.0)), True)
    assert 'attack' not in notes[0].attrs
    assert abs(notes[1].attrs['attack'] - 4.0) < 0.1
