"""Tests for the broadcast-band feature.

Covers:
  * :func:`is_broadcast_eligible` schema-based auto-detection.
  * :attr:`PluginManifest.broadcast_eligible` explicit override (both directions).
  * :class:`PluginHost` broadcaster swap / clear semantics with mock blocks.

The PluginHost tests use a mock block stand-in (no Qt), so the full
widget stack is not required.
"""

from __future__ import annotations

from standalone.song_plugins.api import PluginManifest
from standalone.song_plugins.registry import (
    BROADCAST_ELIGIBLE_SCHEMAS, is_broadcast_eligible,
)


# ---------------------------------------------------------------------------
# is_broadcast_eligible — schema-derived default
# ---------------------------------------------------------------------------

def _mk_manifest(schemas=(), *, broadcast_eligible=None, id_="x"):
    return PluginManifest(
        id=id_, name=id_, version="1", description="",
        capabilities=("analyze",),
        schemas=tuple(schemas),
        broadcast_eligible=broadcast_eligible,
    )


def test_eligible_scalar_curve():
    assert is_broadcast_eligible(_mk_manifest(("scalar_curve",)))


def test_eligible_multi_curve():
    assert is_broadcast_eligible(_mk_manifest(("multi_curve",)))


def test_eligible_events():
    assert is_broadcast_eligible(_mk_manifest(("events",)))


def test_eligible_grid2d():
    assert is_broadcast_eligible(_mk_manifest(("grid2d",)))


def test_not_eligible_stats():
    assert not is_broadcast_eligible(_mk_manifest(("stats",)))


def test_not_eligible_note_tags():
    assert not is_broadcast_eligible(_mk_manifest(("note_tags",)))


def test_not_eligible_placement_tags():
    assert not is_broadcast_eligible(_mk_manifest(("placement_tags",)))


def test_not_eligible_custom():
    assert not is_broadcast_eligible(_mk_manifest(("custom",)))


def test_not_eligible_empty_schemas():
    assert not is_broadcast_eligible(_mk_manifest(()))


def test_eligible_if_any_schema_is_time_axis():
    # Mixture: plugin may emit a stats *or* a scalar_curve. Eligible.
    assert is_broadcast_eligible(_mk_manifest(("stats", "scalar_curve")))


# ---------------------------------------------------------------------------
# Explicit manifest.broadcast_eligible override
# ---------------------------------------------------------------------------

def test_override_true_forces_eligibility():
    # A schema that would normally be ineligible, but manifest forces it on.
    m = _mk_manifest(("stats",), broadcast_eligible=True)
    assert is_broadcast_eligible(m)


def test_override_false_forces_ineligibility():
    # A schema that would normally be eligible, but manifest forces it off.
    m = _mk_manifest(("scalar_curve",), broadcast_eligible=False)
    assert not is_broadcast_eligible(m)


def test_override_none_falls_back_to_schema_derivation():
    # Explicit None (default) should derive from schemas.
    m = _mk_manifest(("scalar_curve",), broadcast_eligible=None)
    assert is_broadcast_eligible(m)
    m2 = _mk_manifest(("stats",), broadcast_eligible=None)
    assert not is_broadcast_eligible(m2)


def test_known_eligible_schema_set():
    # Lock the canonical set so a regression can be detected.
    assert BROADCAST_ELIGIBLE_SCHEMAS == frozenset(
        {"scalar_curve", "multi_curve", "events", "grid2d"}
    )


# ---------------------------------------------------------------------------
# PluginHost broadcaster swap / clear — with mock blocks (no Qt)
# ---------------------------------------------------------------------------

class _MockManifest:
    def __init__(self, name):
        self.name = name


class _MockBlock:
    """Stand-in for PluginBlock: exposes manifest, _last_annotation and
    the ``_on_broadcast_released`` hook the host calls."""

    def __init__(self, name, ann=None):
        self.manifest = _MockManifest(name)
        self._last_annotation = ann
        self.released_count = 0

    def _on_broadcast_released(self):
        self.released_count += 1


class _MockBand:
    """Stand-in for BroadcastBand with just the methods the host touches.

    We don't need the Qt Signal machinery here — we only verify that
    the host calls ``set_broadcaster`` / ``update_annotation`` / ``clear``
    in the right sequence.
    """

    class _CleanSig:
        def connect(self, *a, **k): pass
        def disconnect(self, *a, **k): pass
        def emit(self, *a, **k): pass

    def __init__(self):
        self.cleared = _MockBand._CleanSig()
        self.calls = []  # list[(op_name, *args)]

    def set_broadcaster(self, block, ann):
        self.calls.append(("set", block, ann))

    def update_annotation(self, ann):
        self.calls.append(("update", ann))

    def clear(self):
        self.calls.append(("clear",))


def _make_host():
    """Build a PluginHost bypassing ``__init__`` so we don't need a real App."""
    from standalone.song_plugins.ui.host import PluginHost
    host = PluginHost.__new__(PluginHost)
    host.app = None
    host.selection_provider = None
    host.plugins = {}
    host._blocks = []
    host._broadcaster = None
    host._broadcast_band = None
    return host


def test_set_broadcaster_replaces_previous():
    host = _make_host()
    band = _MockBand()
    host.set_broadcast_band(band)
    a = _MockBlock("A")
    b = _MockBlock("B")
    host.set_broadcaster(a)
    assert host.current_broadcaster() is a
    assert a.released_count == 0
    # Now B takes over — A must be released exactly once.
    host.set_broadcaster(b)
    assert host.current_broadcaster() is b
    assert a.released_count == 1
    assert b.released_count == 0


def test_set_broadcaster_same_block_is_noop():
    host = _make_host()
    band = _MockBand()
    host.set_broadcast_band(band)
    a = _MockBlock("A")
    host.set_broadcaster(a)
    before_calls = len(band.calls)
    host.set_broadcaster(a)
    # No extra band activity; release hook not fired.
    assert host.current_broadcaster() is a
    assert a.released_count == 0
    assert len(band.calls) == before_calls


def test_clear_broadcaster_hides_and_releases():
    host = _make_host()
    band = _MockBand()
    host.set_broadcast_band(band)
    a = _MockBlock("A")
    host.set_broadcaster(a)
    host.clear_broadcaster()
    assert host.current_broadcaster() is None
    assert a.released_count == 1
    assert ("clear",) in band.calls


def test_clear_broadcaster_without_broadcaster_is_idempotent():
    host = _make_host()
    band = _MockBand()
    host.set_broadcast_band(band)
    # No current broadcaster — clear still tells the band to clear,
    # but does not raise.
    host.clear_broadcaster()
    assert host.current_broadcaster() is None


def test_broadcaster_annotation_updated_only_for_current():
    host = _make_host()
    band = _MockBand()
    host.set_broadcast_band(band)
    a = _MockBlock("A")
    b = _MockBlock("B")
    host.set_broadcaster(a)
    # Reset band call log for clarity.
    band.calls.clear()
    # Non-broadcaster reports a new annotation — ignored.
    host.broadcaster_annotation_updated(b, object())
    assert band.calls == []
    # Broadcaster reports — forwarded.
    ann = object()
    host.broadcaster_annotation_updated(a, ann)
    assert band.calls == [("update", ann)]


def test_unregister_block_clears_if_broadcaster():
    host = _make_host()
    band = _MockBand()
    host.set_broadcast_band(band)
    a = _MockBlock("A")
    host._blocks.append(a)
    host.set_broadcaster(a)
    host.unregister_block(a)
    assert host.current_broadcaster() is None
    assert ("clear",) in band.calls


def test_unregister_other_block_does_not_clear():
    host = _make_host()
    band = _MockBand()
    host.set_broadcast_band(band)
    a = _MockBlock("A")
    b = _MockBlock("B")
    host._blocks.extend([a, b])
    host.set_broadcaster(a)
    band.calls.clear()
    host.unregister_block(b)
    assert host.current_broadcaster() is a
    # Only no-op on the band.
    assert ("clear",) not in band.calls
