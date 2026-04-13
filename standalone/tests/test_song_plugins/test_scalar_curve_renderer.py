"""Unit tests for :class:`ScalarCurveRenderer`.

Covers the fixed-range mode toggled via :meth:`set_beat_range`. Does
not invoke ``paintEvent`` — only verifies the renderer's internal
``_beat_range`` state after various calls.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module", autouse=True)
def _qt_app():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 not available")
    import sys
    inst = QApplication.instance() or QApplication(sys.argv[:1])
    yield inst


def _mk():
    from standalone.song_plugins.ui.renderers.scalar_curve import (
        ScalarCurveRenderer,
    )
    return ScalarCurveRenderer()


def test_default_range_is_auto():
    r = _mk()
    assert r._beat_range is None


def test_set_beat_range_stores_tuple():
    r = _mk()
    r.set_beat_range(4.0, 20.0)
    assert r._beat_range == (4.0, 20.0)


def test_set_beat_range_none_reverts_to_auto():
    r = _mk()
    r.set_beat_range(4.0, 20.0)
    r.set_beat_range(None, None)
    assert r._beat_range is None


def test_set_beat_range_partial_none_reverts():
    r = _mk()
    r.set_beat_range(4.0, 20.0)
    r.set_beat_range(None, 10.0)
    assert r._beat_range is None
    r.set_beat_range(4.0, 20.0)
    r.set_beat_range(3.0, None)
    assert r._beat_range is None


def test_set_beat_range_invalid_reverts_to_auto():
    r = _mk()
    # right <= left must revert to auto rather than setting a
    # degenerate range.
    r.set_beat_range(10.0, 10.0)
    assert r._beat_range is None
    r.set_beat_range(10.0, 5.0)
    assert r._beat_range is None


def test_set_beat_range_coerces_to_float():
    r = _mk()
    r.set_beat_range(4, 20)
    lo, hi = r._beat_range
    assert isinstance(lo, float) and isinstance(hi, float)
    assert (lo, hi) == (4.0, 20.0)
