"""Graph editor popup window.

Opens as a non-modal top-level window (show(), not exec()).  Changes are
pushed to the server live whenever the graph mutates.

Layout:
  ┌──────────────────────────────────────────────────────┐
  │ [Add Node ▼]  [Frame All]  [Save Graph] [Load Graph] │  ← toolbar
  ├──────────────────────────────────────────────────────┤
  │                                                      │
  │              NodeGraphCanvas                         │
  │                                                      │
  └──────────────────────────────────────────────────────┘

Add Node dropdown is hierarchical:
  Sources
    → Track Source  (auto-managed; greyed out — tracks come from sequencer)
    → Control Source
  Synthesizers
    → FluidSynth
    → Sine (debug)
    → Sampler  [future]
  Plugins
    → LV2: <name>  (populated from server at open time)
  Utilities
    → Mixer
  Output
    → Output  (only one allowed)
"""

from __future__ import annotations

import json
import os
from typing import Optional, Callable

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QMenu, QToolButton, QLabel, QFrame, QFileDialog,
    QMessageBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QPoint, QPointF, QTimer, Signal
from PySide6.QtGui import QFont, QAction

from .graph_model import GraphModel, GraphNode, PortType
from .node_canvas import NodeGraphCanvas


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class GraphEditorWindow(QWidget):
    """Top-level (non-modal) graph editor.

    Parameters
    ----------
    model        GraphModel owned by AppState; edited in-place.
    server_engine  ServerEngine instance (or None).
    state        AppState (for BPM, track names).
    on_graph_changed  Optional callback(GraphModel) fired after every live push.
    """

    closed = Signal()

    def __init__(self, model: GraphModel, server_engine,
                 state, on_graph_changed: Callable = None,
                 parent=None):
        super().__init__(parent,
                         Qt.Window | Qt.WindowCloseButtonHint |
                         Qt.WindowMinimizeButtonHint)
        self.setWindowTitle("Signal Graph Editor")
        self.resize(1100, 700)

        self.model         = model
        self.server_engine = server_engine
        self.state         = state
        self._on_graph_changed = on_graph_changed

        # Debounce live push so rapid drag events don't hammer the IPC
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(120)   # ms
        self._push_timer.timeout.connect(self._do_live_push)

        self._lv2_plugins: list[dict] = []   # fetched once on open

        self._build_ui()
        self._fetch_lv2_plugins()

        # Apply dark palette matching the main window
        self.setStyleSheet("""
            QWidget { background-color: #16213e; color: #eeeeee; }
            QPushButton {
                background-color: #1a1a2e; color: #eeeeee;
                border: 1px solid #2a3a5c; border-radius: 4px;
                padding: 3px 8px;
            }
            QPushButton:hover { background-color: #2a3a5c; }
            QPushButton:disabled { color: #555; border-color: #333; }
            QToolButton {
                background-color: #1a1a2e; color: #eeeeee;
                border: 1px solid #2a3a5c; border-radius: 4px;
                padding: 3px 8px;
            }
            QToolButton:hover { background-color: #2a3a5c; }
            QMenu { background: #1a2236; color: #eee; border: 1px solid #2a3a5c; }
            QMenu::item:selected { background: #3a7bd5; }
            QMenu::item:disabled { color: #555; }
            QLabel { background: transparent; }
        """)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # Canvas — created first so toolbar buttons can connect to it
        self._canvas = NodeGraphCanvas(self.model, self)
        self._canvas.graph_changed.connect(self._on_graph_changed_canvas)
        self._canvas.node_right_clicked.connect(self._on_node_right_click)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        # Add node button (dropdown)
        self._add_btn = QToolButton()
        self._add_btn.setText("＋ Add Node  ▾")
        self._add_btn.setPopupMode(QToolButton.InstantPopup)
        self._add_menu = self._build_add_menu()
        self._add_btn.setMenu(self._add_menu)
        toolbar.addWidget(self._add_btn)

        toolbar.addSpacing(8)

        frame_btn = QPushButton("Frame All")
        frame_btn.setToolTip("Zoom to fit all nodes  [F]")
        frame_btn.clicked.connect(self._canvas.frame_all)
        toolbar.addWidget(frame_btn)

        toolbar.addStretch()

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #888; font-size: 10px;")
        toolbar.addWidget(self._status_lbl)

        toolbar.addSpacing(12)

        save_btn = QPushButton("Save Graph")
        save_btn.clicked.connect(self._save_graph)
        toolbar.addWidget(save_btn)

        load_btn = QPushButton("Load Graph")
        load_btn.clicked.connect(self._load_graph)
        toolbar.addWidget(load_btn)

        outer.addLayout(toolbar)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2a3a5c;")
        outer.addWidget(sep)

        # Canvas (created above; add to layout here)
        outer.addWidget(self._canvas, 1)

        QTimer.singleShot(50, self._canvas.frame_all)

    def _build_add_menu(self) -> QMenu:
        menu = QMenu(self)

        # Sources
        src_menu = menu.addMenu("Sources")
        ts_action = src_menu.addAction("Track Source")
        ts_action.setEnabled(False)
        ts_action.setToolTip("Track sources are managed automatically by the sequencer")
        src_menu.addAction("Control Source").triggered.connect(
            lambda: self._add_node("control_source"))
        src_menu.addAction("Note Gate").triggered.connect(
            lambda: self._add_node("note_gate"))

        # Synthesizers
        synth_menu = menu.addMenu("Synthesizers")
        synth_menu.addAction("FluidSynth").triggered.connect(
            lambda: self._add_node("fluidsynth"))
        synth_menu.addAction("Sine (debug)").triggered.connect(
            lambda: self._add_node("sine"))
        smp_action = synth_menu.addAction("Sampler  [future]")
        smp_action.triggered.connect(lambda: self._add_node("sampler"))

        # Plugins — populated later in _fetch_lv2_plugins
        self._plugins_menu = menu.addMenu("Plugins  (LV2)")
        self._lv2_loading_action = self._plugins_menu.addAction("Loading…")
        self._lv2_loading_action.setEnabled(False)

        # Utilities
        util_menu = menu.addMenu("Utilities")
        util_menu.addAction("Mixer").triggered.connect(
            lambda: self._add_node("mixer"))
        util_menu.addAction("Split Stereo").triggered.connect(
            lambda: self._add_node("split_stereo"))
        util_menu.addAction("Merge Stereo").triggered.connect(
            lambda: self._add_node("merge_stereo"))

        # Output (only one)
        output_action = menu.addAction("Output (final mix)")
        output_action.triggered.connect(lambda: self._add_node("output"))

        return menu

    # -----------------------------------------------------------------------
    # LV2 plugin fetch
    # -----------------------------------------------------------------------

    def _fetch_lv2_plugins(self) -> None:
        """Query the server for available LV2 plugins and populate the menu."""
        if not self.server_engine:
            self._lv2_loading_action.setText("Server not available")
            return

        def _fetch():
            try:
                resp = self.server_engine._send({"cmd": "list_plugins"})
                return resp
            except Exception:
                return None

        # Run in a background thread so the UI doesn't stall
        import threading
        def _worker():
            resp = _fetch()
            # Schedule UI update back on main thread
            QTimer.singleShot(0, lambda: self._populate_lv2_menu(resp))

        threading.Thread(target=_worker, daemon=True).start()

    def _populate_lv2_menu(self, resp) -> None:
        self._plugins_menu.clear()
        if not resp or resp.get("status") != "ok":
            act = self._plugins_menu.addAction("No LV2 plugins found")
            act.setEnabled(False)
            return

        plugins = resp.get("plugins", [])
        self._lv2_plugins = plugins

        if not plugins:
            act = self._plugins_menu.addAction("No LV2 plugins installed")
            act.setEnabled(False)
            return

        for p in plugins:
            name = p.get("name", p.get("uri", "?"))
            uri  = p.get("uri", "")
            ports = p.get("ports", [])
            act = self._plugins_menu.addAction(name)
            # capture
            act.triggered.connect(
                lambda checked=False, u=uri, n=name, ps=ports:
                self._add_lv2_node(u, n, ps))

    # -----------------------------------------------------------------------
    # Node add helpers
    # -----------------------------------------------------------------------

    def _add_node(self, node_type: str) -> None:
        # Only one output allowed
        if node_type == "output":
            if any(n.node_type == "output" for n in self.model.nodes):
                QMessageBox.information(self, "Graph Editor",
                    "There is already an Output node in the graph.")
                return

        # Choose a sensible default position: centre of current view + small offset
        cx = self._canvas.view_to_scene(
            QPointF(self._canvas.width() / 2, self._canvas.height() / 2)
        )

        import uuid
        nid = str(uuid.uuid4())

        display_names = {
            "fluidsynth":     "FluidSynth",
            "sine":           "Sine",
            "sampler":        "Sampler",
            "lv2":            "LV2",
            "mixer":          "Mixer",
            "output":         "Output",
            "control_source": "Control Source",
            "split_stereo":   "Split Stereo",
            "merge_stereo":   "Merge Stereo",
            "note_gate":      "Note Gate",
        }

        params = {}
        if node_type == "fluidsynth":
            # Default to the currently loaded SF2
            sf2 = getattr(self.server_engine, '_sf2_path', None) or ""
            params["sf2_path"] = sf2
        if node_type in ("mixer", "output"):
            params["channel_count"] = 2
        if node_type == "note_gate":
            params["pitch_lo"] = 0
            params["pitch_hi"] = 127
            params["gate_mode"] = 0

        node = GraphNode(
            node_type=node_type,
            node_id=nid,
            display_name=display_names.get(node_type, node_type),
            x=cx.x() - 90, y=cx.y() - 50,
            params=params,
        )
        self.model.add_node(node)
        self._canvas._create_settings_widget(node)
        self._canvas.selected_nodes = {nid}
        self._canvas.update()
        self._on_graph_changed_canvas()

    def _add_lv2_node(self, uri: str, name: str, ports: list) -> None:
        import uuid
        nid = str(uuid.uuid4())

        cx = self._canvas.view_to_scene(
            QPointF(self._canvas.width() / 2, self._canvas.height() / 2)
        )

        node = GraphNode(
            node_type="lv2",
            node_id=nid,
            display_name=name,
            x=cx.x() - 90, y=cx.y() - 50,
            params={"lv2_uri": uri, "_ports": ports},
        )
        self.model.add_node(node)
        self._canvas._create_settings_widget(node)
        self._canvas.selected_nodes = {nid}
        self._canvas.update()
        self._on_graph_changed_canvas()

    # -----------------------------------------------------------------------
    # Context menu on node right-click
    # -----------------------------------------------------------------------

    def _on_node_right_click(self, node: GraphNode, global_pos: QPoint) -> None:
        menu = QMenu(self)

        if node.node_type in ("fluidsynth", "sine", "sampler", "lv2"):
            if node.is_default_synth:
                act = menu.addAction("✓ Default synth for new tracks")
                act.setEnabled(False)
            else:
                act = menu.addAction("Set as default synth for new tracks")
                act.triggered.connect(
                    lambda: self._set_default_synth(node))

        if node.node_type not in ("track_source",):
            menu.addSeparator()
            del_act = menu.addAction("Delete node")
            del_act.triggered.connect(lambda: self._delete_node(node))

        if not menu.isEmpty():
            menu.exec(global_pos)

    def _set_default_synth(self, node: GraphNode) -> None:
        self.model.set_default_synth(node.node_id)
        self._canvas.update()
        self._on_graph_changed_canvas()

    def _delete_node(self, node: GraphNode) -> None:
        self.model.remove_node(node.node_id)
        self._canvas.selected_nodes.discard(node.node_id)
        self._canvas.update()
        self._on_graph_changed_canvas()

    # -----------------------------------------------------------------------
    # Live push to server
    # -----------------------------------------------------------------------

    def _on_graph_changed_canvas(self) -> None:
        """Called when canvas reports any mutation; schedules a debounced push."""
        self._push_timer.start()

    def _do_live_push(self) -> None:
        """Push the current graph to the server."""
        if not self.server_engine:
            self._status_lbl.setText("No server")
            return
        payload = self.model.to_server_dict(bpm=self.state.bpm)
        resp = self.server_engine._send(payload)
        if resp and resp.get("status") == "ok":
            self._status_lbl.setText("● live")
            self._status_lbl.setStyleSheet("color: #6bcb77; font-size: 10px;")
        else:
            msg = resp.get("message", "error") if resp else "no response"
            self._status_lbl.setText(f"⚠ {msg}")
            self._status_lbl.setStyleSheet("color: #e94560; font-size: 10px;")

        # Also notify the app so it knows the model changed (for save)
        if self._on_graph_changed:
            self._on_graph_changed(self.model)

    # -----------------------------------------------------------------------
    # Save / load graph
    # -----------------------------------------------------------------------

    def _save_graph(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Signal Graph", "", "Graph JSON (*.graph.json *.json)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump(self.model.to_dict(), f, indent=2)
            self._status_lbl.setText(f"Saved")
            self._status_lbl.setStyleSheet("color: #6bcb77; font-size: 10px;")
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def _load_graph(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Signal Graph", "", "Graph JSON (*.graph.json *.json)")
        if not path:
            return
        try:
            with open(path) as f:
                d = json.load(f)
            new_model = GraphModel.from_dict(d)
            # Ensure track sources are in sync with current state
            new_model.sync_track_sources(self.state)
            self.model.nodes       = new_model.nodes
            self.model.connections = new_model.connections
            self._canvas.set_model(self.model)
            self._canvas.frame_all()
            self._on_graph_changed_canvas()
        except Exception as e:
            QMessageBox.warning(self, "Load failed", str(e))

    # -----------------------------------------------------------------------
    # Window lifecycle
    # -----------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # Flush any pending push before closing
        if self._push_timer.isActive():
            self._push_timer.stop()
            self._do_live_push()
        self.closed.emit()
        super().closeEvent(event)
