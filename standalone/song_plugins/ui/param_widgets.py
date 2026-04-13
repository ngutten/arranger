"""Auto-build a param widget from a ParamSpec.

Returns ``(widget, getter)``. The getter returns the current value in the
widget; changed signals are exposed via the widget as usual so callers can
wire up change notifications.
"""

from __future__ import annotations

import logging
from typing import Callable, Tuple

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QLabel, QLineEdit, QSpinBox,
    QWidget,
)

from ..api import ParamSpec

logger = logging.getLogger(__name__)


def _apply_tooltip(w: QWidget, spec: ParamSpec) -> None:
    if spec.help:
        w.setToolTip(spec.help)


def build_param_widget(spec: ParamSpec) -> Tuple[QWidget, Callable[[], object]]:
    """Return ``(widget, getter)`` for the given param spec."""
    t = spec.type

    if t == "int":
        sb = QSpinBox()
        lo = int(spec.min) if spec.min is not None else -2**31
        hi = int(spec.max) if spec.max is not None else 2**31 - 1
        sb.setRange(lo, hi)
        sb.setValue(int(spec.default if spec.default is not None else 0))
        _apply_tooltip(sb, spec)
        return sb, sb.value

    if t == "float":
        dsb = QDoubleSpinBox()
        lo = float(spec.min) if spec.min is not None else -1e12
        hi = float(spec.max) if spec.max is not None else 1e12
        dsb.setRange(lo, hi)
        dsb.setDecimals(3)
        dsb.setValue(float(spec.default if spec.default is not None else 0.0))
        _apply_tooltip(dsb, spec)
        return dsb, dsb.value

    if t == "bool":
        cb = QCheckBox()
        cb.setChecked(bool(spec.default))
        _apply_tooltip(cb, spec)
        return cb, cb.isChecked

    if t == "enum":
        combo = QComboBox()
        choices = tuple(spec.choices or ())
        for c in choices:
            combo.addItem(str(c))
        if spec.default is not None and str(spec.default) in [str(c) for c in choices]:
            combo.setCurrentText(str(spec.default))
        _apply_tooltip(combo, spec)
        return combo, combo.currentText

    if t == "string":
        le = QLineEdit()
        le.setText(str(spec.default) if spec.default is not None else "")
        _apply_tooltip(le, spec)
        return le, le.text

    # Deferred types: render a labelled placeholder.
    if t in ("beat_range", "track_select"):
        label = QLabel(f"[{t} not yet supported]")
        label.setStyleSheet("color: #888;")
        _apply_tooltip(label, spec)
        logger.info("param %r uses deferred type %r", spec.key, t)
        # Return the default value (whatever it is) as a static getter.
        default = spec.default
        return label, (lambda v=default: v)

    # Unknown type — static label.
    label = QLabel(f"[unknown param type: {t}]")
    label.setStyleSheet("color: #c55;")
    return label, (lambda: spec.default)


def evaluate_visible_when(visible_when: dict | None, values: dict) -> bool:
    """Return True if the param should be visible given current values.

    Simple contract: ``visible_when = {"other_key": expected_value}``; the
    param is visible iff ``values[other_key] == expected_value``. If multiple
    keys are given, all must match. Missing keys => hidden.
    """
    if not visible_when:
        return True
    for k, v in visible_when.items():
        if values.get(k) != v:
            return False
    return True
