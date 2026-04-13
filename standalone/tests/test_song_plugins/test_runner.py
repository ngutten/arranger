"""Tests for PluginRunner running note_density synchronously.

We use :py:meth:`PluginRunner.run_blocking` to avoid needing a live Qt
event loop. That exercise validates the progress protocol wiring, plugin
construction, and final PluginResult shape.
"""

import pytest

# QtProgress inherits QObject; we need a QApplication so Signal can exist.
@pytest.fixture(scope="module", autouse=True)
def _qt_app():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 not available")
    import sys
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


def _view_factory(state, empty_selection):
    from standalone.song_plugins.song_view import SongView
    return lambda: SongView(state, empty_selection)


def test_note_density_run_blocking_returns_scalar_curve(fixture_state, empty_selection):
    from standalone.song_plugins.builtin.note_density import NoteDensityPlugin
    from standalone.song_plugins.ui.runner import PluginRunner

    plugin = NoteDensityPlugin()
    vf = _view_factory(fixture_state, empty_selection)
    runner = PluginRunner(plugin, vf, params={"window_beats": 2.0,
                                               "smoothing": "none",
                                               "scope": "whole"})
    result = runner.run_blocking()

    assert result is not None
    ann = result.annotation
    assert ann is not None
    assert ann.schema == "scalar_curve"
    assert "beats" in ann.data
    assert "values" in ann.data
    assert len(ann.data["beats"]) == len(ann.data["values"])
    # fixture has notes on t1 (8 beats) + t2 (16 beats), so at least
    # a handful of bins.
    assert len(ann.data["beats"]) >= 1


def test_note_density_smoothing_ema(fixture_state, empty_selection):
    from standalone.song_plugins.builtin.note_density import NoteDensityPlugin
    from standalone.song_plugins.ui.runner import PluginRunner

    plugin = NoteDensityPlugin()
    vf = _view_factory(fixture_state, empty_selection)
    runner = PluginRunner(plugin, vf, {"window_beats": 1.0,
                                        "smoothing": "ema",
                                        "scope": "whole"})
    result = runner.run_blocking()
    assert result.annotation is not None
    values = result.annotation.data["values"]
    assert all(isinstance(v, float) for v in values)


def test_runner_cancel_flag_visible_to_plugin(fixture_state, empty_selection):
    """A pre-cancelled runner should cause note_density's loop to bail early."""
    from standalone.song_plugins.builtin.note_density import NoteDensityPlugin
    from standalone.song_plugins.ui.runner import PluginRunner

    plugin = NoteDensityPlugin()
    vf = _view_factory(fixture_state, empty_selection)
    runner = PluginRunner(plugin, vf, {"window_beats": 2.0,
                                        "smoothing": "none",
                                        "scope": "whole"})
    runner.cancel()
    # note_density still returns a PluginResult even when cancelled mid-loop;
    # this just verifies the cancel signal propagates without crashing.
    result = runner.run_blocking()
    assert result is not None
