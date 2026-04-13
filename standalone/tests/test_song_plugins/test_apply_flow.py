"""Integration test: run metric_velocity then apply_ops, verify mutation + undo."""

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


def _make_host_shim(app_obj, fixture_state, empty_selection):
    """Minimal host stand-in compatible with PluginBlock."""
    class _HostShim:
        def __init__(self):
            self.app = app_obj
            self.blocks = []
        def register_block(self, b): self.blocks.append(b)
        def unregister_block(self, b):
            if b in self.blocks:
                self.blocks.remove(b)
        def build_song_view(self):
            from standalone.song_plugins.song_view import SongView
            return SongView(fixture_state, empty_selection)
        def selection_snapshot(self):
            return empty_selection
    return _HostShim()


def test_transform_only_apply_runs_and_commits(
        fixture_state, empty_selection, app):
    """Transform-only plugins (euclidean_syncopate) collapse Run+Apply
    into a single Apply click. Verify the PluginBlock wiring:
      * no Run button in header
      * Apply click → runs plugin → commits ops → single undo entry
    """
    from standalone.song_plugins.ui.plugin_block import PluginBlock
    from standalone.song_plugins.builtin.euclidean_syncopate import (
        EuclideanSyncopatePlugin,
    )

    host = _make_host_shim(app, fixture_state, empty_selection)
    block = PluginBlock(host, EuclideanSyncopatePlugin)
    try:
        # Capability wiring: transform-only.
        assert block._transform_only is True
        assert block._has_analyze is False
        # Run button exists internally (for uniform enable/disable) but
        # is hidden — the header only shows Apply.
        assert block._run_btn.isHidden()
        assert block._apply_btn is not None
        assert block._apply_btn.isEnabled()

        # Simulate the Apply click synchronously by reproducing the flow
        # without a daemon thread: set the flag and run the plugin blocking.
        from standalone.song_plugins.ui.runner import PluginRunner
        block._apply_after_run = True
        runner = PluginRunner(
            block.plugin, host.build_song_view,
            block._current_params(),
        )
        result = runner.run_blocking()

        undo_before = len(app.undo_stack.stack)
        # Feed the result through the same finished-callback path that
        # the real runner would call; verify single-click auto-apply.
        block._on_runner_finished(result)

        # Ops were committed; undo stack grew by 1.
        assert len(app.undo_stack.stack) == undo_before + 1
        assert block._apply_after_run is False
        # Apply stays enabled after a successful run-and-commit.
        assert block._apply_btn.isEnabled()
        # Status is "ok".
        assert block._current_status == "ok"
    finally:
        block.deleteLater()


def test_transform_only_apply_failure_reenables_button(
        fixture_state, empty_selection, app):
    """On plugin failure, the Apply button must re-enable so the user
    can retry."""
    from standalone.song_plugins.ui.plugin_block import PluginBlock
    from standalone.song_plugins.builtin.euclidean_syncopate import (
        EuclideanSyncopatePlugin,
    )

    host = _make_host_shim(app, fixture_state, empty_selection)
    block = PluginBlock(host, EuclideanSyncopatePlugin)
    try:
        # Simulate a failure via the runner-failed callback.
        block._apply_after_run = True
        block._apply_btn.setEnabled(False)  # simulate "running" state
        block._on_runner_failed(RuntimeError("boom"))

        assert block._apply_btn.isEnabled()
        assert block._apply_after_run is False
        assert block._current_status == "error"
    finally:
        block.deleteLater()


def test_metric_velocity_runner_then_apply_ops(fixture_state, empty_selection, app):
    from standalone.song_plugins.song_view import SongView
    from standalone.song_plugins.builtin.metric_velocity import MetricVelocityPlugin
    from standalone.song_plugins.ui.runner import PluginRunner
    from standalone.song_plugins.apply_ops import apply_ops

    plugin = MetricVelocityPlugin()
    vf = lambda: SongView(fixture_state, empty_selection)
    runner = PluginRunner(plugin, vf, {
        "subdivision": "4", "pattern": "5 1 3 1",
        "mode": "absolute", "scope": "whole",
    })
    result = runner.run_blocking()

    assert result is not None
    ops = tuple(result.operations or ())
    assert len(ops) > 0  # the fixture should have notes that change

    # Snapshot selected velocities before apply.
    before = {}
    for op in ops:
        pat = fixture_state.find_pattern(op.pattern_id)
        assert pat is not None
        for n in pat.notes:
            if n.note_id == op.note_id:
                before[op.note_id] = n.velocity
                break

    # Snapshot undo-stack length before we apply.
    undo_before = len(app.undo_stack.stack)

    apply_ops(ops, app, label="Metric Velocity Pattern")

    # Each targeted note now matches op.velocity, and differs from before.
    for op in ops:
        pat = fixture_state.find_pattern(op.pattern_id)
        found = next(n for n in pat.notes if n.note_id == op.note_id)
        assert found.velocity == op.velocity
        assert found.velocity != before[op.note_id]

    # Exactly one new undo entry was pushed.
    undo_after = len(app.undo_stack.stack)
    assert undo_after == undo_before + 1
