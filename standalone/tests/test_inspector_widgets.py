"""Inspector settings-widget regression tests.

The "context-aware inspector" (commit f7db670) builds a node's settings panel
*outside* the graph canvas: `TrackPanel._render_node_params` calls the shared
builder with a non-canvas parent, then reparents the result into a layout. Any
settings widget that needs the host's `AppState` or live `server_engine` used to
find it by walking the widget parent chain up to the canvas — which no longer
reaches it from the inspector. That degraded *silently* (no exception):

  * Control Source: track-selector dropdown fell back to a bare spinbox.
  * Control Monitor: live sparkline stuck on "no data".

The fix threads `state`/`canvas` explicitly from `track_panel` into the builder
(see node_canvas `_resolve_state`/`_resolve_canvas`). These tests drive the real
`_render_node_params` call site through a stub host so they fail if that
forwarding is ever dropped again, and sweep every registered node type to catch
a builder that crashes on a new plugin descriptor.

Run: QT_QPA_PLATFORM=offscreen python -m pytest standalone/tests/test_inspector_widgets.py
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QGroupBox, QVBoxLayout, QComboBox, QSpinBox,
)

from standalone.arranger_engine import AudioServer, AudioEngineConfig  # noqa: E402
import standalone.core.binding_engine  # noqa: F401,E402  (side effect: load plugins)
from standalone.graph_editor import graph_model as gm  # noqa: E402
from standalone.graph_editor.node_canvas import (  # noqa: E402
    _make_default_settings_widget,
)
from standalone.graph_editor.graph_model import GraphNode  # noqa: E402
from standalone.ui.track_panel import TrackPanel  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures: a single QApplication + the real engine plugin descriptors
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def plugins(qapp):
    """Populate graph_model's descriptor cache from the live engine, exactly
    as BindingEngine does at startup, and return the raw plugin list."""
    srv = AudioServer(AudioEngineConfig())
    resp = json.loads(srv.handle(json.dumps({"cmd": "list_registered_plugins"})))
    pl = resp.get("plugins", [])
    gm.set_plugin_descriptors(pl)
    return pl


# --------------------------------------------------------------------------
# Stub host mirroring what TrackPanel._render_node_params actually touches
# --------------------------------------------------------------------------

class _Track:
    def __init__(self, tid, name):
        self.id, self.name = tid, name


class _Pattern:
    def __init__(self, pid, name):
        self.id, self.name = pid, name


class _FakeState:
    def __init__(self):
        self.automation_tracks = [_Track(1, "Cutoff"), _Track(2, "Vibrato")]
        self.patterns = [_Pattern(10, "Verse"), _Pattern(11, "Chorus")]
        self.beat_patterns = [_Pattern(20, "Beat A")]


class _FakeEngine:
    def get_node_data(self, node_id, what):
        assert what == "history"
        return [0.1, 0.4, 0.42, 0.5, 0.48]


class _FakeModel:
    connections = []


class _FakeCanvas:
    """Stands in for NodeGraphCanvas. `_resolve_canvas` honours an explicitly
    passed canvas, so this need not be a real NodeGraphCanvas instance."""
    def __init__(self):
        self._state = _FakeState()
        self._server_engine = _FakeEngine()
        self._settings = None
        self.model = _FakeModel()

    def _on_node_param_changed(self, *a):
        pass


class _StubPanel:
    """Minimal `self` for the unbound TrackPanel._render_node_params."""
    def __init__(self, canvas):
        class _App:
            pass
        self.app = _App()
        self.app._sel_graph_canvas = canvas
        self.node_frame = QGroupBox()
        QVBoxLayout(self.node_frame)

    def _clear_frame(self, frame):
        lay = frame.layout()
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()


def _render(node):
    """Drive the real inspector call site; return the node_frame to inspect."""
    panel = _StubPanel(_FakeCanvas())
    TrackPanel._render_node_params(panel, node)
    return panel.node_frame


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_control_source_renders_track_dropdown(plugins):
    """The automation-track selector must be a dropdown of track names — not the
    bare spinbox it silently degraded to when state wasn't forwarded."""
    node = GraphNode(node_id="cs", node_type="control_source",
                     display_name="Control Source")
    node.params["automation_track_id"] = 2
    frame = _render(node)

    combos = frame.findChildren(QComboBox)
    track_combo = next(
        (c for c in combos
         if [c.itemText(i) for i in range(c.count())][:1] == ["(No automation track)"]),
        None)
    assert track_combo is not None, "no automation-track dropdown rendered"
    items = [track_combo.itemText(i) for i in range(track_combo.count())]
    assert "Cutoff" in items and "Vibrato" in items, items
    # Stored id (2) must be the selected entry.
    assert track_combo.itemData(track_combo.currentIndex()) == 2


def test_pattern_source_renders_picker(plugins):
    node = GraphNode(node_id="ps", node_type="pattern_source",
                     display_name="Pattern Source")
    frame = _render(node)
    combos = frame.findChildren(QComboBox)
    names = [c.itemText(i) for c in combos for i in range(c.count())]
    assert "Verse" in names and "Chorus" in names, names


def test_beat_pattern_source_renders_picker(plugins):
    node = GraphNode(node_id="bps", node_type="beat_pattern_source",
                     display_name="Beat Pattern Source")
    frame = _render(node)
    names = [c.itemText(i) for c in frame.findChildren(QComboBox)
             for i in range(c.count())]
    assert "Beat A" in names, names


def test_control_monitor_polls_server(plugins):
    """The sparkline must reach the live server_engine and pull data — the
    regression left it permanently on its 'no data' placeholder."""
    node = GraphNode(node_id="cm", node_type="control_monitor",
                     display_name="Control Monitor")
    frame = _render(node)
    spark = next((w for w in frame.findChildren(object)
                  if hasattr(w, "_poll") and hasattr(w, "_data")), None)
    assert spark is not None, "no sparkline widget rendered"
    assert spark._data == [], "expected empty before polling"
    spark._poll()
    assert spark._data, "sparkline did not pull data from the server_engine"


def test_all_node_types_build_without_error(plugins):
    """Every registered node type (plus the hardcoded specials) must build a
    settings panel through the inspector path without raising — a guard against
    a new plugin descriptor that the builder can't handle."""
    specials = ["midi_source", "note_gate", "mixer", "output", "sine",
                "pattern_source", "beat_pattern_source", "control_source",
                "split_stereo", "merge_stereo", "track_source"]
    types = set(specials)
    for p in plugins:
        types.add(p["id"])
        if p["id"].startswith("builtin."):
            types.add(p["id"].split(".", 1)[1])

    canvas = _FakeCanvas()
    held = []
    failures = []
    for t in sorted(types):
        node = GraphNode(node_id=f"n_{t}", node_type=t, display_name=t)
        parent = QGroupBox()
        held.append(parent)  # keep alive so children aren't GC'd mid-build
        try:
            _make_default_settings_widget(
                node, parent, lambda *a: None,
                state=canvas._state, canvas=canvas)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{t}: {type(e).__name__}: {e}")
    assert not failures, "settings builder raised for:\n  " + "\n  ".join(failures)
