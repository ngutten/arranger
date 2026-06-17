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

from .graph_model import GraphModel, GraphNode, PortType, default_hidden_ports_for_node
from .node_canvas import NodeGraphCanvas


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class GraphEditorWindow(QWidget):
    """Top-level (non-modal) graph editor.

    Parameters
    ----------
    model        GraphModel owned by AppState; edited in-place.
    server_engine  BindingEngine instance (or None).
    state        AppState (for BPM, track names).
    on_graph_changed  Optional callback(GraphModel) fired after every live push.
    """

    closed = Signal()

    def __init__(self, model: GraphModel, server_engine,
                 state, on_graph_changed: Callable = None,
                 parent=None, embedded: bool = False):
        # embedded=True hosts the editor inside a workspace pane (no window
        # chrome); otherwise it is a free-floating top-level window.
        self._embedded = embedded
        if embedded:
            super().__init__(parent)
        else:
            # Qt.Window makes it a movable top-level window while keeping the
            # parent relationship so it closes with the main window.
            super().__init__(parent, Qt.Window)
            self.setWindowTitle("Signal Graph Editor")
            self.resize(1100, 700)

        self._initial_model = model
        self.server_engine = server_engine
        self.state         = state
        self.settings      = server_engine.settings if server_engine and hasattr(server_engine, 'settings') else None
        self._on_graph_changed = on_graph_changed

        # Debounce live push so rapid drag events don't hammer the IPC
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(120)   # ms
        self._push_timer.timeout.connect(self._do_live_push)

        self._build_ui()

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

    @property
    def model(self):
        """The model currently shown by the canvas.

        set_model() can replace the canvas's model on project load, so always
        read it from the canvas — otherwise node add/delete/save would operate
        on a stale, detached model (and added nodes would never appear).
        """
        canvas = getattr(self, '_canvas', None)
        return canvas.model if canvas is not None else self._initial_model

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
        self._canvas.param_changed.connect(self._on_param_changed_fast)
        self._canvas._server_engine = self.server_engine
        self._canvas._state = self.state
        self._canvas._settings = self.settings

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
#        src_menu.addAction("Note Gate").triggered.connect(
#            lambda: self._add_node("note_gate"))
        src_menu.addSeparator()
        src_menu.addAction("External MIDI Input").triggered.connect(
            lambda: self._add_node("midi_source"))
        src_menu.addSeparator()
        src_menu.addAction("Pattern Source").triggered.connect(
            lambda: self._add_node("pattern_source"))
        src_menu.addAction("Beat Pattern Source").triggered.connect(
            lambda: self._add_node("beat_pattern_source"))

        # Synthesizers
        synth_menu = menu.addMenu("Synthesizers")
        synth_menu.addAction("FluidSynth").triggered.connect(
            lambda: self._add_node("fluidsynth"))
        synth_menu.addAction("Sine (debug)").triggered.connect(
            lambda: self._add_node("sine"))
        synth_menu.addAction("Sampler").triggered.connect(
            lambda: self._add_node("sampler"))

        # Utilities
        util_menu = menu.addMenu("Utilities")
        util_menu.addAction("Mixer").triggered.connect(
            lambda: self._add_node("mixer"))
        util_menu.addAction("Split Stereo").triggered.connect(
            lambda: self._add_node("split_stereo"))
        util_menu.addAction("Merge Stereo").triggered.connect(
            lambda: self._add_node("merge_stereo"))

        # Output (only one)
        output_action = menu.addAction("Audio Out")
        output_action.triggered.connect(lambda: self._add_node("output"))

        # --- Registered plugins (new plugin API) ---
        # Add plugins from the descriptor cache that aren't already in the
        # hardcoded menus above.
        self._plugin_menu_placeholder = menu  # store ref for dynamic rebuild
        self._populate_plugin_menu(menu)

        return menu

    def _populate_plugin_menu(self, menu: QMenu) -> None:
        """Add registered plugins to the Add Node menu, grouped by category.

        Skips plugins whose short name is already in the hardcoded menus
        (sine, mixer, control_source, fluidsynth) to avoid
        duplicates during the transition period.
        """
        from .graph_model import _plugin_descriptors

        # Types already represented in the hardcoded menus
        _hardcoded = {"builtin.sine", "builtin.mixer", 
                      "builtin.control_source", "builtin.fluidsynth",
                      "builtin.sampler"}

        # Group remaining plugins by category
        by_cat: dict[str, list[dict]] = {}
        for pid, desc in _plugin_descriptors.items():
            if pid in _hardcoded:
                continue
            cat = desc.get("category", "Other")
            by_cat.setdefault(cat, []).append(desc)

        if not by_cat:
            return

        menu.addSeparator()
        plugins_menu = menu.addMenu("Plugins")

        for cat in sorted(by_cat.keys()):
            if len(by_cat) == 1:
                # Only one category — don't nest
                target = plugins_menu
            else:
                target = plugins_menu.addMenu(cat)

            for desc in sorted(by_cat[cat], key=lambda d: d.get("display_name", "")):
                pid = desc.get("id", "")
                name = desc.get("display_name", pid)
                act = target.addAction(name)
                act.triggered.connect(
                    lambda checked=False, p=pid, n=name, d=desc:
                    self._add_plugin_node(p, n, d))

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

        # Only one midi_source allowed (it owns the external MIDI port)
        if node_type == "midi_source":
            if any(n.node_type == "midi_source" for n in self.model.nodes):
                QMessageBox.information(self, "Graph Editor",
                    "There is already an External MIDI Input node in the graph.\n"
                    "Only one is supported at a time.")
                return

        # Choose a sensible default position: centre of current view + small offset
        cx = self._canvas.view_to_scene(
            QPointF(self._canvas.width() / 2, self._canvas.height() / 2)
        )

        import uuid
        nid = str(uuid.uuid4())

        display_names = {
            "fluidsynth":          "FluidSynth",
            "sine":                "Sine",
            "sampler":             "Sampler",
            "mixer":               "Mixer",
            "output":              "Output",
            "control_source":      "Control Source",
            "split_stereo":        "Split Stereo",
            "merge_stereo":        "Merge Stereo",
            "note_gate":           "Note Gate",
            "midi_source":         "External MIDI In",
            "pattern_source":      "Pattern Source",
            "beat_pattern_source": "Beat Pattern Source",
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
        if node_type == "midi_source":
            # Pre-fill with the current MIDI device from settings (if any)
            device = ""
            if self.server_engine and hasattr(self.server_engine, 'settings'):
                device = getattr(self.server_engine.settings, 'midi_input_device', '') or ''
            params["midi_device"]  = device
            params["midi_channel"] = 0
        if node_type == "pattern_source" and self.state.patterns:
            params["_pattern_id"] = self.state.patterns[0].id
        if node_type == "beat_pattern_source" and self.state.beat_patterns:
            params["_pattern_id"] = self.state.beat_patterns[0].id

        node = GraphNode(
            node_type=node_type,
            node_id=nid,
            display_name=display_names.get(node_type, node_type),
            x=cx.x() - 90, y=cx.y() - 50,
            params=params,
        )

        # Populate _pattern_data immediately so the first graph push includes it
        # (the settings widget also does this when the dropdown fires, but that
        # races with the first _on_graph_changed_canvas call below).
        if node_type in ("pattern_source", "beat_pattern_source"):
            from .graph_model import update_pattern_source_node
            pat_id = params.get("_pattern_id")
            if pat_id is not None:
                update_pattern_source_node(node, pat_id, self.state)
        self.model.add_node(node)
        self._canvas._create_settings_widget(node)
        self._canvas.selected_nodes = {nid}
        self._canvas.update()
        self._on_graph_changed_canvas()
    
    def _add_plugin_node(self, plugin_id: str, name: str, desc: dict) -> None:
        """Add a node backed by a registered plugin (new plugin API)."""
        import uuid
        nid = str(uuid.uuid4())

        cx = self._canvas.view_to_scene(
            QPointF(self._canvas.width() / 2, self._canvas.height() / 2)
        )

        # Pre-populate params with config param defaults (include empty-string
        # defaults so params like lyrics_sequence reach node.params and are
        # forwarded via configure() even before the user edits them).
        params = {}
        for cp in desc.get("config_params", []):
            cp_id = cp.get("id", "")
            cp_def = cp.get("default", "")
            if cp_def is not None:
                params[cp_id] = cp_def
        # Pre-populate control input defaults
        for port in desc.get("ports", []):
            if port.get("type") == "control" and port.get("role") == "input":
                params[port["id"]] = port.get("default", 0.0)

        node = GraphNode(
            node_type=plugin_id,
            node_id=nid,
            display_name=name,
            x=cx.x() - 90, y=cx.y() - 50,
            params=params,
            hidden_ports=default_hidden_ports_for_node(plugin_id),
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

        # A node is a "synth" (can be default target for new tracks) if it's
        # a known synth type OR a plugin with Event input + Audio output.
        is_synth = node.node_type in ("fluidsynth", "sine", "sampler")
        if not is_synth:
            ports = node.ports()
            has_event_in = any(p.ptype == PortType.MIDI and not p.is_output for p in ports)
            has_audio_out = any(p.ptype in (PortType.AUDIO, PortType.AUDIO_MONO)
                                and p.is_output for p in ports)
            is_synth = has_event_in and has_audio_out

        if is_synth:
            if node.is_default_synth:
                act = menu.addAction("✓ Default synth for new tracks")
                act.setEnabled(False)
            else:
                act = menu.addAction("Set as default synth for new tracks")
                act.triggered.connect(
                    lambda: self._set_default_synth(node))

        # Port visibility submenu
        all_ports = node.ports()
        visible   = node.visible_ports()
        hidden    = [p for p in all_ports if p.port_id in node.hidden_ports]

        menu.addSeparator()
        ports_menu = menu.addMenu("Ports")

        if hidden:
            for port in hidden:
                lbl = port.name or port.port_id
                act = ports_menu.addAction(f"↩ Reveal  \"{lbl}\"")
                def _reveal(checked=False, p=port, n=node):
                    self._canvas._reveal_port(n, p)
                    self._canvas.update()
                act.triggered.connect(_reveal)
            def _reveal_all(checked=False, n=node):
                self._canvas._reveal_all_ports(n)
                self._canvas.update()
            ports_menu.addAction("Reveal all hidden").triggered.connect(_reveal_all)
            ports_menu.addSeparator()

        # Offer to hide visible ports individually
        for port in visible:
            lbl = port.name or port.port_id
            act = ports_menu.addAction(f"Hide  \"{lbl}\"")
            act.triggered.connect(
                lambda checked=False, p=port:
                    self._canvas._hide_port(node, p))

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
        self._canvas.emit_selection()
        # Clean up the inline settings widget so it doesn't linger on screen
        w = self._canvas._settings_widgets.pop(node.node_id, None)
        if w:
            w.setParent(None)
            w.deleteLater()
        self._canvas.update()
        self._on_graph_changed_canvas()

    # -----------------------------------------------------------------------
    # Live push to server
    # -----------------------------------------------------------------------

    def _on_graph_changed_canvas(self) -> None:
        """Called when canvas reports any mutation; schedules a debounced push."""
        self._push_timer.start()

    def _on_param_changed_fast(self, node_id: str, param_id: str, value: float) -> None:
        """Low-latency path: send set_param directly to server without graph rebuild."""
        if self.server_engine and hasattr(self.server_engine, 'set_param'):
            # Resolve the server-side node ID (output node serialises as "mixer")
            node = self.model.get_node(node_id)
            server_id = node._server_id() if node else node_id
            self.server_engine.set_param(server_id, param_id, value)

    def _do_live_push(self) -> None:
        """Push the current graph to the server."""

        if not self.server_engine:
            self._status_lbl.setText("No server")
            return
            
        self.server_engine.mark_dirty()
        """
        payload = self.model.to_server_dict(bpm=self.state.bpm)
        resp = self.server_engine._send(payload)
        
        if resp and resp.get("status") == "ok":
            self._status_lbl.setText("● live")
            self._status_lbl.setStyleSheet("color: #6bcb77; font-size: 10px;")
        else:
            msg = resp.get("message", "error") if resp else "no response"
            self._status_lbl.setText(f"⚠ {msg}")
            self._status_lbl.setStyleSheet("color: #e94560; font-size: 10px;")
        """
        # Notify the app so it knows the model changed (for save) and so
        # it can restart the MIDI router if a midi_source node was added/removed.
        if self._on_graph_changed:
            self._on_graph_changed(self.model)
        # Restart MIDI router: target may have changed
        parent = self.parent()
        if parent and hasattr(parent, '_restart_midi_router'):
            parent._restart_midi_router()

    # -----------------------------------------------------------------------
    # Save / load graph
    # -----------------------------------------------------------------------

    def _save_graph(self) -> None:
        start_dir = self.settings.get_recent_dir('graph') if self.settings else ''
        path, _ = QFileDialog.getSaveFileName(
            None, "Save Signal Graph", start_dir, "Graph JSON (*.graph.json *.json)",
            options=QFileDialog.DontUseNativeDialog)
        if not path:
            return
        if self.settings:
            self.settings.set_recent_dir('graph', path)
        try:
            with open(path, "w") as f:
                json.dump(self.model.to_dict(), f, indent=2)
            self._status_lbl.setText(f"Saved")
            self._status_lbl.setStyleSheet("color: #6bcb77; font-size: 10px;")
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def _load_graph(self) -> None:
        start_dir = self.settings.get_recent_dir('graph') if self.settings else ''
        path, _ = QFileDialog.getOpenFileName(
            None, "Load Signal Graph", start_dir, "Graph JSON (*.graph.json *.json)",
            options=QFileDialog.DontUseNativeDialog)
        if not path:
            return
        if self.settings:
            self.settings.set_recent_dir('graph', path)
        try:
            with open(path) as f:
                d = json.load(f)
            new_model = GraphModel.from_dict(d)
            # Ensure track sources are in sync with current state
            new_model.sync_track_sources(self.state)
            # Regenerate _pattern_data for any pattern source nodes — it's not
            # persisted in the graph JSON, so rebuild from _pattern_id + live state.
            from .graph_model import update_pattern_source_node
            for node in new_model.nodes:
                if node.node_type in ("pattern_source", "beat_pattern_source"):
                    pat_id = node.params.get("_pattern_id")
                    if pat_id is not None:
                        update_pattern_source_node(node, pat_id, self.state)
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
