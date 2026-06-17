"""Main application class - creates the window, wires up UI components."""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtWidgets import (QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
                                QSplitter, QFileDialog, QMessageBox, QMenuBar,
                                QDockWidget, QStackedWidget, QLabel)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QPalette, QColor, QAction

from .state import (
    AppState, Pattern, Track, BeatTrack, BeatInstrument, BeatPlacement,
    Placement, PALETTE, NOTE_NAMES,
)
from .undo import UndoStack, capture_state, restore_state
from .ops import patterns as pat_ops
from .ops import tracks as trk_ops
from .ops import export as export_ops
from .ops import playback as play_ops
from .ops import project_io
from .core.sf2 import SF2Info, scan_directory
from .core.midi import create_midi
from .core.midi_load import import_midi as midi_import
from .core.audio import render_fluidsynth, render_basic, AudioPlayer
from .core.settings import Settings

try:
    from .core.engine import AudioEngine
    _HAS_ENGINE = True
except ImportError:
    _HAS_ENGINE = False

try:
    from .core.binding_engine import BindingEngine
    _HAS_BINDING_ENGINE = True
except ImportError:
    _HAS_BINDING_ENGINE = False

try:
    from .core.midi_source_router import MidiSourceRouter
    _HAS_MIDI_ROUTER = True
except ImportError:
    _HAS_MIDI_ROUTER = False

from .ui.topbar import TopBar
from .ui.pattern_list import PatternList
from .ui.arrangement import ArrangementView
from .ui.piano_roll import PianoRoll
from .ui.beat_grid import BeatGrid
from .ui.automation_curve import AutomationCurve
from .ui.track_panel import TrackPanel
from .ui.dialogs import PatternDialog, BeatPatternDialog, SF2Dialog, ConfigDialog

#try:
from .graph_editor import GraphModel, GraphEditorWindow
_HAS_GRAPH_EDITOR = True
#except ImportError:
#    _HAS_GRAPH_EDITOR = False

try:
    from .song_plugins.ui import PluginHost, PluginsDock, BroadcastBand
    _HAS_SONG_PLUGINS = True
except Exception:
    _HAS_SONG_PLUGINS = False

class App(QMainWindow):
    """Main application - owns the state, creates the window, coordinates UI."""

    def __init__(self, instruments_dir=None):
        super().__init__()
        self.state = AppState()
        self.player = AudioPlayer()  # kept for legacy preview fallback
        self.instruments_dir = instruments_dir or str(
            Path(__file__).parent.parent / 'instruments')

        # Load user settings (MIDI device, SF2 path, audio params, etc.)
        self.settings = Settings()

        # Undo/redo system
        self.undo_stack = UndoStack(max_size=100)
        self._undo_triggers = {
            'pattern_dialog', 'beat_pattern_dialog',
            'placement_edit', 'beat_placement_edit', 
            'del_pl', 'del_beat_pl',
            'placement_added', 'beat_placement_added',
            'note_edit', 'note_add',  # Piano roll edits
            'piano_roll_edit', 'beat_grid_edit',
            'track_deleted', 'beat_track_deleted',
            'ts', 'cut_placements', 'paste_placements', 'delete_placements',
            # Automation undo triggers
            'automation_edit', 'automation_pattern_dialog',
            'automation_track_dialog', 'del_auto_pat', 'dup_auto_pat',
            'automation_placement_edit', 'automation_placement_added',
        }

        # Realtime audio engine
        self.engine = None  # initialized in _init_engine()

        # MIDI live-preview router (bridges rtmidi → server)
        self._midi_router = None

        # Graph editor window (non-modal; lazily created)
        self._graph_editor_window = None

        # Context-aware inspector: the graph node currently selected in the
        # graph editor (drives the inspector's node-params panel). Cleared by
        # any arrangement-side selection so the two views don't fight.
        self.sel_graph_node = None
        self._sel_graph_canvas = None
        # Frame the graph to fit the first time it's shown at real size (the
        # construction-time frame runs while the page is hidden/zero-size).
        self._graph_framed = False

        # Drag-and-drop state
        self._drag_type = None
        self._drag_pid = None

        # Playback state
        self._play_timer = None
        self._playback_max_beat = 0

        # Coalesced refresh state
        self._refresh_pending = False

        self._setup_theme()
        self._build_ui()
        self._bind_keys()
        self._init_state()
        
        self.new_project()
        
        # Connect state observer — must be after _init_state so engine exists
        self.state.on_change(self._on_state_change)
        
        # Capture initial state for undo
        self._push_undo("init")
        
        # Timer for auto-save functionality
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self._auto_save)
        self.autosave_timer.start(60000)

        # Always-on master meter refresh (~20fps). During playback the playhead
        # timer already refreshes the meter snapshot, so we only poll here when
        # stopped (catches live note/pattern previews).
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(50)
        self._meter_timer.timeout.connect(self._meter_tick)
        self._meter_timer.start()

    def _meter_tick(self):
        eng = self.engine
        if not eng or not hasattr(eng, 'master_meter'):
            return
        if not self.state.playing:
            _ = eng.current_beat   # refresh meter snapshot via get_position
        self.topbar.update_meter()
        if self.mixer_dock.isVisible():
            self.mixer_view.update_meter()
    
    # Autosave functionality
    def _auto_save(self):
        project_io.save_project(self.state, "autosave.json")
        
    def _setup_theme(self):
        """Configure Qt stylesheet for dark mode."""
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #16213e;
                color: #eeeeee;
            }
            QFrame {
                background-color: #16213e;
            }
            QPushButton {
                background-color: #1a1a2e;
                color: #eeeeee;
                border: 1px solid #2a2a4a;
                padding: 4px 8px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background-color: #e94560;
                color: #ffffff;
            }
            QPushButton:pressed {
                background-color: #d63850;
            }
            QPushButton:checked {
                background-color: #e94560;
                color: #ffffff;
            }
            QPushButton:disabled {
                background-color: #1a1a2e;
                color: #555577;
                border-color: #222240;
            }
            QLineEdit, QSpinBox, QComboBox {
                background-color: #1a1a2e;
                color: #eeeeee;
                border: 1px solid #2a2a4a;
                padding: 2px 4px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 4px solid #eeeeee;
                width: 0;
                height: 0;
            }
            QGroupBox {
                border: 1px solid #2a2a4a;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title {
                color: #e94560;
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            QScrollBar:vertical {
                background: #16213e;
                width: 12px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background: #2a2a4a;
                min-height: 20px;
                border-radius: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #3a3a6a;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                background: #16213e;
                height: 12px;
                border: none;
            }
            QScrollBar::handle:horizontal {
                background: #2a2a4a;
                min-width: 20px;
                border-radius: 2px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #3a3a6a;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QSlider::groove:horizontal {
                background: #1a1a2e;
                height: 4px;
            }
            QSlider::handle:horizontal {
                background: #e94560;
                width: 12px;
                margin: -4px 0;
                border-radius: 6px;
            }
            QListWidget {
                background-color: #1a1a30;
                color: #eeeeee;
                border: 1px solid #2a2a4a;
            }
            QListWidget::item:selected {
                background-color: #e94560;
            }
            QMenuBar {
                background-color: #1a1a2e;
                color: #eeeeee;
                border-bottom: 1px solid #2a2a4a;
            }
            QMenuBar::item {
                background-color: transparent;
                padding: 4px 10px;
            }
            QMenuBar::item:selected {
                background-color: #e94560;
                color: #ffffff;
            }
            QMenuBar::item:pressed {
                background-color: #d63850;
                color: #ffffff;
            }
            QMenu {
                background-color: #1a1a2e;
                color: #eeeeee;
                border: 1px solid #2a2a4a;
                padding: 4px 0;
            }
            QMenu::item {
                padding: 4px 24px 4px 20px;
                background-color: transparent;
            }
            QMenu::item:selected {
                background-color: #e94560;
                color: #ffffff;
            }
            QMenu::item:disabled {
                color: #555577;
            }
            QMenu::separator {
                height: 1px;
                background-color: #2a2a4a;
                margin: 4px 8px;
            }
            QMenu::indicator {
                width: 14px;
                height: 14px;
                left: 4px;
            }
        """)

    def _build_ui(self):
        """Build the main UI layout."""
        self.setWindowTitle('Music Arranger')
        self.resize(1200, 750)
        self.setMinimumSize(800, 500)
        self.showMaximized()

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Top bar
        self.topbar = TopBar(central, self)
        layout.addWidget(self.topbar)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background-color: #2a2a4a;")
        sep.setFixedHeight(1)
        layout.addWidget(sep)

        # Main area
        main = QWidget()
        main_layout = QHBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Left panel (pattern list)
        self.pattern_list = PatternList(main, self)
        main_layout.addWidget(self.pattern_list)

        # Center area (arrangement + piano roll / beat grid)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setHandleWidth(4)
        self.splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #e94560;
            }
        """)

        # Arrangement view (top) with an optional BroadcastBand slot
        # stacked directly beneath it. The band spans the same width as
        # the arranger (not the whole window) because it lives in the
        # same splitter pane. It starts hidden and only reveals itself
        # when a plugin block claims the broadcast slot.
        self._arrangement_container = QWidget(self.splitter)
        _arr_layout = QVBoxLayout(self._arrangement_container)
        _arr_layout.setContentsMargins(0, 0, 0, 0)
        _arr_layout.setSpacing(0)
        self.arrangement = ArrangementView(self._arrangement_container, self)
        _arr_layout.addWidget(self.arrangement, stretch=1)
        self.broadcast_band = None
        if _HAS_SONG_PLUGINS:
            try:
                self.broadcast_band = BroadcastBand(self._arrangement_container)
                _arr_layout.addWidget(self.broadcast_band)
            except Exception as exc:
                print(f"[App] BroadcastBand init failed: {exc}")
                self.broadcast_band = None
        self.splitter.addWidget(self._arrangement_container)

        # Editor area (bottom) - switches between piano roll, beat grid, and automation curve
        self.editor_container = QWidget()
        self.editor_layout = QVBoxLayout(self.editor_container)
        self.editor_layout.setContentsMargins(0, 0, 0, 0)
        
        self.piano_roll = PianoRoll(self.editor_container, self)
        self.beat_grid = BeatGrid(self.editor_container, self)
        self.automation_curve = AutomationCurve(self.editor_container, self)

        # Start with piano roll visible
        self.editor_layout.addWidget(self.piano_roll)
        self.editor_layout.addWidget(self.beat_grid)
        self.editor_layout.addWidget(self.automation_curve)
        self.piano_roll.show()
        self.beat_grid.hide()
        self.automation_curve.hide()
        self._current_editor = 'piano_roll'

        self.splitter.addWidget(self.editor_container)
        self.splitter.setSizes([400, 280])

        main_layout.addWidget(self.splitter, 1)

        # ---- Workspaces -------------------------------------------------
        # The centre area is a stack of three task layouts switched from the
        # topbar: Arrange (pattern list + arrangement + bottom editor), Graph
        # (the signal-graph editor, given near-full width), and Mix (the full
        # mixer). The topbar transport + the inspector dock persist across all
        # three, so a loop keeps playing while you switch.
        self.workspace = 'arrange'
        self.workspace_stack = QStackedWidget()

        # Page 0 — Arrange (the `main` widget built above)
        self.workspace_stack.addWidget(main)

        # Page 1 — Graph (populated lazily by _ensure_graph_panel once an
        # engine that supports the graph protocol is up)
        self.graph_page = QWidget()
        self.graph_page_layout = QVBoxLayout(self.graph_page)
        self.graph_page_layout.setContentsMargins(0, 0, 0, 0)
        self._graph_placeholder = QLabel(
            'The signal-graph editor requires the C++ built-in or server backend.\n'
            'Switch the audio backend in Settings to use it.')
        self._graph_placeholder.setAlignment(Qt.AlignCenter)
        self._graph_placeholder.setStyleSheet('color: #888;')
        self.graph_page_layout.addWidget(self._graph_placeholder)
        self.workspace_stack.addWidget(self.graph_page)

        self._workspace_index = {'arrange': 0, 'graph': 1}

        layout.addWidget(self.workspace_stack)

        # Mixer lives in a dock (like the inspector/plugins), not a workspace —
        # it's wide by nature so it defaults to the bottom, but being a
        # QDockWidget the user can drag it into a side column. Available under
        # any workspace. Built into its dock further below with the others.
        from .ui.mixer_view import MixerView
        self.mixer_view = MixerView(self, None)

        # Right panel (track settings) lives in a dock so it can tab together
        # with the song-plugins dock instead of both competing for the right
        # edge. The center area (splitter) reclaims the freed horizontal space.
        self.track_panel = TrackPanel(self, self)
        self.inspector_dock = QDockWidget("Inspector", self)
        self.inspector_dock.setObjectName("inspector_dock")
        self.inspector_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.inspector_dock.setWidget(self.track_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.inspector_dock)

        # Mixer dock — bottom by default (mixers are wide), but movable to a
        # left/right column if preferred. Hidden until toggled.
        self.mixer_dock = QDockWidget("Mixer", self)
        self.mixer_dock.setObjectName("mixer_dock")
        self.mixer_dock.setAllowedAreas(
            Qt.BottomDockWidgetArea | Qt.LeftDockWidgetArea
            | Qt.RightDockWidgetArea)
        self.mixer_dock.setWidget(self.mixer_view)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.mixer_dock)
        self.mixer_dock.hide()
        self.mixer_dock.visibilityChanged.connect(self._on_mixer_visibility)

        # Song-plugins host + dock (optional)
        self.plugin_host = None
        self.plugins_dock = None
        if _HAS_SONG_PLUGINS:
            try:
                self.plugin_host = PluginHost(self)
                self.plugins_dock = PluginsDock(self.plugin_host, self)
                self.addDockWidget(Qt.RightDockWidgetArea, self.plugins_dock)
                # Tab the plugins dock together with the inspector so they
                # share the right edge (one visible at a time) rather than
                # stacking side-by-side and crowding the layout.
                self.tabifyDockWidget(self.inspector_dock, self.plugins_dock)
                self.inspector_dock.raise_()   # inspector is the default tab
                self.plugins_dock.hide()  # default hidden
                self.plugin_host.selection_provider.install_focus_tracker()
                if self.broadcast_band is not None:
                    self.plugin_host.set_broadcast_band(self.broadcast_band)
                    # Sync the band's horizontal view with the arranger's
                    # viewport so beats line up and scroll in lockstep.
                    try:
                        self.broadcast_band.set_arrangement_view(self.arrangement)
                    except Exception as exc:
                        print(f"[App] broadcast_band arrangement sync failed: {exc}")
                # Piano-roll overlay — the sibling target for tag/region
                # schemas. The piano roll reads it during paintEvent.
                try:
                    from .song_plugins.ui.piano_roll_overlay import (
                        PianoRollOverlay,
                    )
                    self.piano_roll_overlay = PianoRollOverlay(self)
                    self.plugin_host.set_piano_roll_overlay(
                        self.piano_roll_overlay
                    )
                    if self.piano_roll is not None:
                        self.piano_roll_overlay.changed.connect(
                            self.piano_roll.grid_widget.update
                        )
                except Exception as exc:
                    print(f"[App] piano-roll overlay init failed: {exc}")
            except Exception as exc:
                print(f"[App] Song-plugins UI init failed: {exc}")
                self.plugin_host = None
                self.plugins_dock = None

        self._build_menu_bar()

    def _build_menu_bar(self):
        """Build the main window menu bar."""
        mb = self.menuBar()
        self._build_file_menu(mb)
        self._build_edit_menu(mb)
        self._build_view_menu(mb)

    def _build_file_menu(self, mb):
        file_menu = mb.addMenu("&File")

        new_act = QAction("&New", self)
        new_act.setShortcut(QKeySequence.New)  # Ctrl+N
        new_act.triggered.connect(self.new_project)
        file_menu.addAction(new_act)

        open_act = QAction("&Open...", self)
        open_act.setShortcut(QKeySequence.Open)  # Ctrl+O
        open_act.triggered.connect(self.load_project)
        file_menu.addAction(open_act)

        imp_midi = QAction("&Import MIDI...", self)
        imp_midi.triggered.connect(self.import_midi)
        file_menu.addAction(imp_midi)

        save_act = QAction("&Save", self)
        save_act.setShortcut(QKeySequence.Save)  # Ctrl+S
        save_act.triggered.connect(self.save_project_current)
        file_menu.addAction(save_act)

        save_as_act = QAction("Save &As...", self)
        save_as_act.setShortcut(QKeySequence.SaveAs)  # Ctrl+Shift+S
        save_as_act.triggered.connect(self.save_project)
        file_menu.addAction(save_as_act)

        file_menu.addSeparator()

        exp_midi = QAction("Export &MIDI...", self)
        exp_midi.triggered.connect(lambda: self.do_export('midi'))
        file_menu.addAction(exp_midi)

        exp_xml = QAction("Export Music&XML...", self)
        exp_xml.triggered.connect(lambda: self.do_export('musicxml'))
        file_menu.addAction(exp_xml)

        exp_mp3 = QAction("Export M&P3...", self)
        exp_mp3.triggered.connect(lambda: self.do_export('mp3'))
        file_menu.addAction(exp_mp3)

        exp_wav = QAction("Export &WAV...", self)
        exp_wav.triggered.connect(lambda: self.do_export('wav'))
        file_menu.addAction(exp_wav)

        file_menu.addSeparator()

        cfg_act = QAction("Settin&gs...", self)
        cfg_act.triggered.connect(self.open_config)
        file_menu.addAction(cfg_act)

        file_menu.addSeparator()

        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence("Ctrl+Q"))
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    def _build_edit_menu(self, mb):
        edit_menu = mb.addMenu("&Edit")

        # Undo / Redo — existing QShortcut bindings in _bind_keys() already
        # own Ctrl+Z and Ctrl+Y. Don't re-bind on the QAction (would cause
        # "ambiguous shortcut overload"). Just show the key hint in the label.
        undo_act = QAction("&Undo\tCtrl+Z", self)
        undo_act.triggered.connect(self.do_undo)
        edit_menu.addAction(undo_act)

        redo_act = QAction("&Redo\tCtrl+Y", self)
        redo_act.triggered.connect(self.do_redo)
        edit_menu.addAction(redo_act)

        edit_menu.addSeparator()

        # Cut/Copy/Paste — the app already has QShortcut handlers wired to
        # _on_cut/_on_copy/_on_paste which do smart context-aware dispatch
        # (arrangement vs piano roll vs beat grid). Reuse those same handlers.
        # Don't set shortcuts on the QAction (existing QShortcut owns them).
        cut_act = QAction("Cu&t\tCtrl+X", self)
        cut_act.triggered.connect(self._menu_cut)
        edit_menu.addAction(cut_act)

        copy_act = QAction("&Copy\tCtrl+C", self)
        copy_act.triggered.connect(self._menu_copy)
        edit_menu.addAction(copy_act)

        paste_act = QAction("&Paste\tCtrl+V", self)
        paste_act.triggered.connect(self._menu_paste)
        edit_menu.addAction(paste_act)

        edit_menu.addSeparator()

        add_trk = QAction("Add &Track", self)
        add_trk.triggered.connect(self.add_track)
        edit_menu.addAction(add_trk)

        add_beat = QAction("Add &Beat Track", self)
        add_beat.triggered.connect(self.add_beat_track)
        edit_menu.addAction(add_beat)

        add_auto = QAction("Add &Automation Track", self)
        add_auto.triggered.connect(self.add_automation_track)
        edit_menu.addAction(add_auto)

    def _build_view_menu(self, mb):
        view_menu = mb.addMenu("&View")

        # Plugins dock (kept — added in earlier PR)
        if self.plugins_dock is not None:
            self._plugins_action = QAction("&Plugins", self, checkable=True)
            self._plugins_action.setChecked(self.plugins_dock.isVisible())
            self._plugins_action.toggled.connect(self._toggle_plugins_dock)
            # Mirror state if the dock is closed via its own X button.
            self.plugins_dock.visibilityChanged.connect(
                lambda visible: self._plugins_action.setChecked(bool(visible)))
            view_menu.addAction(self._plugins_action)
            view_menu.addSeparator()

        # Left sidebar (Pattern List)
        pl_act = QAction("Pattern &List", self, checkable=True)
        pl_act.setChecked(not self.pattern_list.isHidden())
        pl_act.toggled.connect(self.pattern_list.setVisible)
        view_menu.addAction(pl_act)

        # Right sidebar (Inspector dock)
        tp_act = QAction("&Inspector", self, checkable=True)
        tp_act.setChecked(self.inspector_dock.isVisible())
        tp_act.toggled.connect(self.inspector_dock.setVisible)
        self.inspector_dock.visibilityChanged.connect(
            lambda visible: tp_act.setChecked(bool(visible)))
        view_menu.addAction(tp_act)

        view_menu.addSeparator()

        # Right-sidebar sub-panels (GroupBoxes inside TrackPanel)
        panel_frames = [
            ("&Track Settings", 'trk_frame'),
            ("&Soundfont", 'sf2_frame'),
            ("P&lacement", 'pl_frame'),
            ("Beat &Kit", 'kit_frame'),
            ("&Automation Tracks", 'auto_frame'),
        ]
        self._panel_actions = {}
        for label, attr in panel_frames:
            frame = getattr(self.track_panel, attr, None)
            if frame is None:
                continue
            act = QAction(label, self, checkable=True)
            act.setChecked(not frame.isHidden())
            # Bind frame via default-arg to avoid late-binding closure bug
            act.toggled.connect(lambda checked, f=frame: f.setVisible(checked))
            view_menu.addAction(act)
            self._panel_actions[attr] = act

        view_menu.addSeparator()

        # Graph editor — popup window, not a panel
        graph_act = QAction("&Graph Editor...", self)
        graph_act.triggered.connect(self.open_graph_editor)
        view_menu.addAction(graph_act)

    def _toggle_plugins_dock(self, checked: bool):
        if self.plugins_dock is None:
            return
        self.plugins_dock.setVisible(checked)
        if checked:
            self.plugins_dock.raise_()

    # ---- Menu dispatchers for Cut/Copy/Paste ----
    # These mirror the QShortcut handlers so the menu and keyboard route to the
    # same smart-context logic (arrangement vs piano roll vs beat grid).

    def _menu_cut(self):
        self._on_cut()

    def _menu_copy(self):
        self._on_copy()

    def _menu_paste(self):
        self._on_paste()

    def _bind_keys(self):
        """Bind keyboard shortcuts."""
        QShortcut(Qt.Key_Space, self, self._on_space)
        QShortcut(QKeySequence.Copy, self, self._on_copy)
        QShortcut(QKeySequence.Cut, self, self._on_cut)
        QShortcut(QKeySequence.Paste, self, self._on_paste)
        QShortcut(QKeySequence.SelectAll, self, self._on_select_all)
        QShortcut(QKeySequence.Delete, self, self._on_delete)
        QShortcut(Qt.Key_Backspace, self, self._on_delete)
        QShortcut(QKeySequence('Ctrl+D'), self, self._on_duplicate)
        
        # Undo/Redo shortcuts
        undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        undo_shortcut.activated.connect(self.do_undo)
        
        redo_shortcut = QShortcut(QKeySequence('Ctrl+Y'), self)
        redo_shortcut.activated.connect(self.do_redo)

    def _init_state(self):
        """Set up initial state with one pattern and one track."""
        # Create default pattern
        pat = Pattern(
            id=self.state.new_id(), name='Pattern 1', length=4,
            notes=[], color=PALETTE[0], key='C', scale='major',
        )
        self.state.patterns.append(pat)
        self.state.sel_pat = pat.id

        # Create default track
        trk = Track(id=self.state.new_id(), name='Track 1', channel=0)
        self.state.tracks.append(trk)
        self.state.sel_trk = trk.id

        # Initialize realtime audio engine
        self._init_engine()

        # Auto-load first SF2
        self._auto_load_sf2()

        # Build default graph model (done after SF2 load so sf2_path is known)
        self._ensure_graph_model()
        self._ensure_graph_panel()

        # Start MIDI live-preview router (if a device is configured)
        self._restart_midi_router()

        # Initial render
        self._refresh_all()

    def _init_engine(self):
        """Initialize the audio engine according to settings.audio_backend."""
        backend = self.settings.audio_backend  # 'binding', 'server', or 'fluidsynth'

        if backend == 'binding' and _HAS_BINDING_ENGINE:
            try:
                self.engine = BindingEngine(self.state, self.settings)
                return
            except Exception as e:
                print(f"[App] BindingEngine init failed: {e}; falling back")

        if _HAS_ENGINE:
            try:
                self.engine = AudioEngine(self.state, self.settings)
                return
            except Exception as e:
                print(f"[App] AudioEngine init failed: {e}")

        self.engine = None

    def _auto_load_sf2(self):
        """Load SF2 on startup: prefer settings path, fall back to first in instruments dir."""
        from .core.sf2 import SF2Info
        from .ops.export import _get_sf2_path

        # Prefer the user's saved default SF2
        if self.settings.sf2_path:
            try:
                sf2 = SF2Info(self.settings.sf2_path)
                self.state.sf2 = sf2
                if self.engine:
                    self.engine.load_sf2(self.settings.sf2_path)
                return
            except Exception:
                pass  # fall through to directory scan

        sf2_list = scan_directory(self.instruments_dir)
        if sf2_list:
            self.state.sf2 = sf2_list[0]
            if self.engine:
                sf2_path = _get_sf2_path(sf2_list[0])
                if sf2_path:
                    self.engine.load_sf2(sf2_path)

    def _ensure_graph_model(self) -> None:
        """Build a default GraphModel if one doesn't exist yet.

        Called after engine init and SF2 load so the default synth can be
        populated with the correct SF2 path.
        """
        if not _HAS_GRAPH_EDITOR:
            return
        if self.state.signal_graph is not None:
            # Already loaded (e.g. from project file); just sync track sources
            sf2_path = self._current_sf2_path()
            self.state.signal_graph.sync_track_sources(self.state, sf2_path)
            return
        sf2_path = self._current_sf2_path()
        self.state.signal_graph = GraphModel.make_default(self.state, sf2_path)

    def _current_sf2_path(self) -> str:
        """Return the currently loaded SF2 path, or ''."""
        if self.state.sf2 and hasattr(self.state.sf2, 'path'):
            return self.state.sf2.path
        if self.engine and hasattr(self.engine, '_sf2_path'):
            return self.engine._sf2_path or ''
        return ''

    def _graph_supported(self) -> bool:
        """True if the current engine can host the live signal graph."""
        return bool(_HAS_GRAPH_EDITOR and self.engine
                    and hasattr(self.engine, '_send'))

    def _ensure_graph_panel(self) -> None:
        """Build the embedded graph editor into the Graph workspace page.

        Idempotent. Does nothing (placeholder stays) until an engine that
        supports the graph protocol is available.
        """
        if self._graph_editor_window is not None:
            return
        if not self._graph_supported():
            return
        self._graph_editor_window = GraphEditorWindow(
            model=self.state.signal_graph,
            server_engine=self.engine,
            state=self.state,
            on_graph_changed=self._on_graph_model_changed,
            parent=self.graph_page,
            embedded=True,
        )
        # Drive the context-aware inspector from node selection in the canvas;
        # with the inspector co-located, node params live there (pure layout).
        self._sel_graph_canvas = self._graph_editor_window._canvas
        self._sel_graph_canvas.selection_changed.connect(
            self._on_graph_node_selected)
        self._sel_graph_canvas.params_in_inspector = True
        self._graph_placeholder.hide()
        self.graph_page_layout.addWidget(self._graph_editor_window)

    def _teardown_graph_panel(self) -> None:
        """Drop the embedded graph editor (e.g. on backend switch)."""
        if self._graph_editor_window is not None:
            self.graph_page_layout.removeWidget(self._graph_editor_window)
            self._graph_editor_window.setParent(None)
            self._graph_editor_window.deleteLater()
        self._graph_editor_window = None
        self._sel_graph_canvas = None
        self.sel_graph_node = None
        self._graph_placeholder.show()

    def set_workspace(self, name: str) -> None:
        """Switch the centre area between 'arrange' and 'graph'."""
        if name not in self._workspace_index:
            return
        if name == 'graph':
            self._ensure_graph_panel()
        self.workspace = name
        self.workspace_stack.setCurrentIndex(self._workspace_index[name])

        if name == 'graph':
            # Sync the inspector to whatever is selected on the canvas.
            if self._sel_graph_canvas is not None:
                self._sel_graph_canvas.emit_selection()
            # Frame-to-fit once the page is laid out at real size.
            if self._sel_graph_canvas is not None and not self._graph_framed:
                QTimer.singleShot(0, self._frame_graph_once)
        else:
            # Leaving the graph hands the inspector back to the arrangement.
            self.sel_graph_node = None
        self.track_panel.refresh()
        if hasattr(self.topbar, 'sync_workspace'):
            self.topbar.sync_workspace(name)

    def _frame_graph_once(self) -> None:
        if self._sel_graph_canvas is not None:
            self._sel_graph_canvas.frame_all()
            self._graph_framed = True

    def _regraph_view(self) -> None:
        """Re-fit the graph after its model changed (project load/new/import).

        Frames now if the Graph workspace is showing, else on next entry.
        """
        self._graph_framed = False
        if self.workspace == 'graph':
            QTimer.singleShot(0, self._frame_graph_once)

    def open_graph_editor(self) -> None:
        """Switch to the Graph workspace (was: open floating window)."""
        self.set_workspace('graph')

    def _on_graph_node_selected(self, node) -> None:
        """A node was (de)selected in the graph editor → update inspector."""
        # Only reflect node selection while the Graph workspace is active.
        if self.workspace != 'graph':
            return
        self.sel_graph_node = node
        self.track_panel.refresh()

    def _on_graph_model_changed(self, model) -> None:
        """Called when the graph editor makes a live change."""
        # model is the same object as self.state.signal_graph (edited in-place)
        pass
        self.track_panel.refresh()

    def _on_state_change(self, source=None):
        """Called whenever state changes. Refreshes relevant UI components."""
        # An arrangement/track/pattern change reclaims the inspector from the
        # graph-node view. Node param edits use the canvas fast-path and never
        # call state.notify(), so they don't trip this.
        self.sel_graph_node = None

        # Mark engine dirty so schedule rebuilds on next audio callback
        if self.engine and self.state.playing:
            self.engine.mark_dirty()

        # Capture undo snapshot for certain actions (synchronous, reads AppState not widgets)
        if (source in self._undo_triggers
                and not getattr(self, '_suppress_undo', False)):
            self._push_undo(source)

        # Notify plugin host so live-mode blocks can mark-stale / rerun.
        if self.plugin_host is not None:
            try:
                self.plugin_host.on_state_change(source)
            except Exception as exc:
                print(f"[App] plugin host state-change hook failed: {exc}")

        # Coalesce UI refresh — schedule once, skip if already pending
        self._schedule_refresh()

    def _schedule_refresh(self):
        """Schedule a UI refresh for the end of the current event batch.

        Multiple calls within the same event loop iteration coalesce into
        a single refresh, which prevents tearing down and rebuilding widgets
        while user input events are still being delivered.
        """
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(0, self._do_deferred_refresh)

    def _do_deferred_refresh(self):
        """Execute the coalesced refresh."""
        self._refresh_pending = False
        self._refresh_all()

    def _refresh_all(self):
        """Refresh all UI components from current state."""
        self._switch_editor()
        self.topbar.refresh()
        self.pattern_list.refresh()
        self.arrangement.refresh()
        if self._current_editor == 'piano_roll':
            self.piano_roll.refresh()
        elif self._current_editor == 'beat_grid':
            self.beat_grid.refresh()
        elif self._current_editor == 'automation_curve':
            self.automation_curve.refresh()
        if self.mixer_dock.isVisible():
            self.mixer_view.refresh()
        self.track_panel.refresh()
    
    def _push_undo(self, source=None):
        """Push current state onto undo stack."""
        if source in ('undo', 'redo'):
            return  # Don't capture during undo/redo
        snapshot = capture_state(self.state)
        self.undo_stack.push(snapshot)

    @contextmanager
    def undo_group(self, label: str):
        """Batch multiple notifies into a single undo entry.

        While active, per-notify undo captures are suppressed. On exit
        (outermost call only) one combined snapshot is pushed onto the
        undo stack.
        """
        prev_suppress = getattr(self, '_suppress_undo', False)
        self._suppress_undo = True
        try:
            yield
        finally:
            self._suppress_undo = prev_suppress
            if not prev_suppress:
                snapshot = capture_state(self.state)
                self.undo_stack.push(snapshot)
    
    def do_undo(self):
        """Undo the last action."""
        if not self.undo_stack.can_undo():
            return
        snapshot = self.undo_stack.undo()
        if snapshot:
            restore_state(self.state, snapshot)
            # Clear all selections to avoid ghost selections
            self.piano_roll.clear_selection()
            self.arrangement.selected_placements = []
            self.arrangement.selected_beat_placements = []
            self.arrangement.selected_automation_placements = []
            # Mark engine dirty directly, skip the notify→schedule path
            if self.engine and self.state.playing:
                self.engine.mark_dirty()
            self._refresh_all()
    
    def do_redo(self):
        """Redo the last undone action."""
        if not self.undo_stack.can_redo():
            return
        
        snapshot = self.undo_stack.redo()
        if snapshot:
            restore_state(self.state, snapshot)
            # Clear all selections to avoid ghost selections
            self.piano_roll.clear_selection()
            self.arrangement.selected_placements = []
            self.arrangement.selected_beat_placements = []
            self.arrangement.selected_automation_placements = []
            if self.engine and self.state.playing:
                self.engine.mark_dirty()
            self._refresh_all()

    def toggle_mixer(self):
        """Show/hide the mixer dock (independent of the active workspace)."""
        self.mixer_dock.setVisible(not self.mixer_dock.isVisible())

    def _on_mixer_visibility(self, visible):
        if visible:
            self.mixer_view.refresh()
        if hasattr(self.topbar, 'sync_mixer_button'):
            self.topbar.sync_mixer_button(visible)

    def _switch_editor(self):
        """Switch between piano roll, beat grid, and automation curve based on selection."""
        # Selection-driven editor switching only applies in the Arrange page.
        if self.workspace != 'arrange':
            return
        if self.state.sel_auto_pat and self._current_editor != 'automation_curve':
            # Switch to automation curve
            self.piano_roll.hide()
            self.beat_grid.hide()
            if self.automation_curve.parent() != self.editor_container:
                self.editor_layout.addWidget(self.automation_curve)
            self.automation_curve.show()
            self._current_editor = 'automation_curve'
        elif self.state.sel_beat_pat and self._current_editor != 'beat_grid':
            # Switch to beat grid
            self.piano_roll.hide()
            self.automation_curve.hide()
            if self.beat_grid.parent() != self.editor_container:
                self.editor_layout.addWidget(self.beat_grid)
            self.beat_grid.show()
            self._current_editor = 'beat_grid'
        elif not self.state.sel_beat_pat and not self.state.sel_auto_pat and self._current_editor != 'piano_roll':
            # Switch to piano roll
            self.beat_grid.hide()
            self.automation_curve.hide()
            if self.piano_roll.parent() != self.editor_container:
                self.editor_layout.addWidget(self.piano_roll)
            self.piano_roll.show()
            self._current_editor = 'piano_roll'

    # ---- Keyboard handlers ----

    def _on_space(self):
        focused = self.focusWidget()
        if focused and focused.__class__.__name__ in ('QLineEdit', 'QSpinBox', 'QComboBox'):
            return
        self.toggle_play()

    def _on_copy(self):
        # Check if arranger has a selection
        if self.arrangement.selected_placements or self.arrangement.selected_beat_placements or self.arrangement.selected_automation_placements:
            self.arrangement.copy_selection()
        # Otherwise try piano roll
        elif self._current_editor == 'piano_roll':
            self.piano_roll._copy_to_clipboard()
        elif self._current_editor == 'beat_grid':
            self.beat_grid._copy_to_clipboard()

    def _on_cut(self):
        # Check if arranger has a selection
        if self.arrangement.selected_placements or self.arrangement.selected_beat_placements or self.arrangement.selected_automation_placements:
            self.arrangement.cut_selection()
        # Otherwise try piano roll
        elif self._current_editor == 'piano_roll':
            self.piano_roll._cut_to_clipboard()
        elif self._current_editor == 'beat_grid':
            self.beat_grid._cut_to_clipboard()

    def _on_paste(self):
        # Smart paste: check which clipboard has data and prioritize current context
        piano_has_data = self.piano_roll._note_clipboard.has_data()
        arrangement_has_data = self.arrangement.clipboard.has_data()
        beat_grid_has_data = self.beat_grid._clipboard.has_data()

        # Beat grid paste takes priority when in beat grid editor
        if self._current_editor == 'beat_grid' and beat_grid_has_data:
            self.beat_grid._paste_from_clipboard()
        # If current editor is piano roll and it has clipboard data, paste there
        elif self._current_editor == 'piano_roll' and piano_has_data:
            self.piano_roll._paste_from_clipboard()
        # If current editor is piano roll but only arrangement has data, paste arrangement
        elif self._current_editor == 'piano_roll' and arrangement_has_data and not piano_has_data:
            self.arrangement.paste_at_playhead()
        # If arrangement has data (and we're not in piano roll with data), paste arrangement
        elif arrangement_has_data:
            self.arrangement.paste_at_playhead()
        # Fallback to piano roll if it has data
        elif piano_has_data:
            self.piano_roll._paste_from_clipboard()

    def _on_duplicate(self):
        if self._current_editor == 'piano_roll':
            self.piano_roll._duplicate_selection()
        elif self._current_editor == 'beat_grid':
            self.beat_grid._duplicate_pattern()

    def _on_select_all(self):
        # Check which widget has focus or mouse position
        focused = self.focusWidget()
        
        # If arrangement canvas or no clear focus, select arrangement
        if focused == self.arrangement.canvas_widget or \
           isinstance(focused, type(self.arrangement.canvas_widget)) or \
           (self.arrangement.selected_placements or self.arrangement.selected_beat_placements):
            self.arrangement.select_all()
        # Otherwise piano roll
        elif self._current_editor == 'piano_roll':
            pat = self.state.find_pattern(self.state.sel_pat)
            if pat:
                self.piano_roll._selected = set(range(len(pat.notes)))
                self.piano_roll.refresh()
        elif self._current_editor == 'beat_grid':
            self.beat_grid._select_all()

    def _on_delete(self):
        # Check if arranger has a selection
        if self.arrangement.selected_placements or self.arrangement.selected_beat_placements:
            self.arrangement.delete_selection()
        # Otherwise try piano roll
        elif self._current_editor == 'piano_roll':
            self.piano_roll._delete_selected()
        elif self._current_editor == 'beat_grid':
            self.beat_grid._delete_selected()

    # ---- Pattern management ----

    def add_pattern(self):
        """Create a new melodic pattern."""
        pat_ops.add_pattern(self.state)

    def edit_pattern(self, pid):
        """Edit an existing pattern's metadata."""
        pat = self.state.find_pattern(pid)
        if not pat:
            return
        dlg = PatternDialog(self, self.state, pat)
        if dlg.exec():
            pat.name = dlg.name
            pat.length = dlg.length
            pat.color = dlg.color
            pat.key = dlg.key
            pat.scale = dlg.scale
            self.state.notify('edit_pattern')

    def duplicate_pattern(self, pid):
        """Duplicate a pattern."""
        pat_ops.duplicate_pattern(self.state, pid)

    def delete_pattern(self, pid):
        """Delete a pattern and its placements."""
        deleted_ids = pat_ops.delete_pattern(self.state, pid)
        self.arrangement.selected_placements = [
            p for p in self.arrangement.selected_placements
            if p.id not in deleted_ids
        ]

    def add_beat_pattern(self):
        """Create a new beat pattern."""
        pat_ops.add_beat_pattern(self.state)

    def edit_beat_pattern(self, pid):
        """Edit an existing beat pattern's metadata."""
        pat = self.state.find_beat_pattern(pid)
        if not pat:
            return
        dlg = BeatPatternDialog(self, self.state, pat)
        if dlg.exec():
            pat.name = dlg.name
            pat.length = dlg.length
            pat.color = dlg.color
            self.state.notify('edit_beat_pattern')

    def duplicate_beat_pattern(self, pid):
        """Duplicate a beat pattern."""
        pat_ops.duplicate_beat_pattern(self.state, pid)

    def delete_beat_pattern(self, pid):
        """Delete a beat pattern and its placements."""
        deleted_ids = pat_ops.delete_beat_pattern(self.state, pid)
        self.arrangement.selected_beat_placements = [
            p for p in self.arrangement.selected_beat_placements
            if p.id not in deleted_ids
        ]

    # ---- Track management ----

    def add_track(self):
        """Create a new track."""
        t = trk_ops.add_track(self.state)
        if _HAS_GRAPH_EDITOR and self.state.signal_graph is not None:
            self.state.signal_graph.add_track_source(
                t.id, t.name, self._current_sf2_path())
            self._push_graph_to_engine()

    def delete_track(self, tid):
        """Delete a track and its placements."""
        deleted_ids = trk_ops.delete_track(self.state, tid)
        self.arrangement.selected_placements = [
            p for p in self.arrangement.selected_placements
            if p.id not in deleted_ids
        ]
        if _HAS_GRAPH_EDITOR and self.state.signal_graph is not None:
            self.state.signal_graph.remove_track_source(tid)
            self._push_graph_to_engine()

    def add_beat_track(self):
        """Create a new beat track."""
        bt = trk_ops.add_beat_track(self.state)
        if _HAS_GRAPH_EDITOR and self.state.signal_graph is not None:
            self.state.signal_graph.add_track_source(
                bt.id, bt.name, self._current_sf2_path())
            self._push_graph_to_engine()

    def delete_beat_track(self, btid):
        """Delete a beat track and its placements."""
        deleted_ids = trk_ops.delete_beat_track(self.state, btid)
        self.arrangement.selected_beat_placements = [
            p for p in self.arrangement.selected_beat_placements
            if p.id not in deleted_ids
        ]
        if _HAS_GRAPH_EDITOR and self.state.signal_graph is not None:
            self.state.signal_graph.remove_track_source(btid)
            self._push_graph_to_engine()

    def add_automation_track(self):
        """Create a new automation track via dialog."""
        from .ui.automation_dialogs import AutomationTrackDialog
        dlg = AutomationTrackDialog(self, self.state, self.state.signal_graph)
        if dlg.exec() and dlg.result_track:
            self.state.automation_tracks.append(dlg.result_track)
            self.state.sel_auto_trk = dlg.result_track.id
            self.state.notify('automation_track_dialog')

    def _push_graph_to_engine(self) -> None:
        """Push the current graph model to the engine if it supports _send."""
        if self.engine and hasattr(self.engine, '_send') and self.state.signal_graph:
            payload = self.state.signal_graph.to_server_dict(bpm=self.state.bpm)
            self.engine._send(payload)
            # Refresh graph editor canvas if open
            if self._graph_editor_window is not None:
                self._graph_editor_window._canvas.update()
        # Restart MIDI router after graph changes (new node_id may be present)
        self._restart_midi_router()

    # ---- MIDI live-preview routing ----

    def _restart_midi_router(self) -> None:
        """Start (or restart) the MIDI live-preview router.

        If a midi_source node is present in the graph, route events to that
        node's server ID.  Otherwise fall back to the selected track.
        """
        if not _HAS_MIDI_ROUTER:
            return
        if not (self.engine and hasattr(self.engine, '_send')):
            return

        device = getattr(self.settings, 'midi_input_device', '') or ''

        # Stop existing router
        if self._midi_router is not None:
            self._midi_router.stop()
            self._midi_router = None

        if not device:
            return

        # Determine target node
        node_id = None
        channel_filter = 0
        if self.state.signal_graph is not None:
            ms_node = self.state.signal_graph.find_midi_source()
            if ms_node is not None:
                node_id       = ms_node.node_id
                channel_filter = ms_node.params.get('midi_channel', 0)

        def _get_sel_track():
            return self.state.sel_trk

        self._midi_router = MidiSourceRouter(
            engine=self.engine,
            device_name=device,
            node_id=node_id,
            channel_filter=channel_filter,
            get_track_id=_get_sel_track,
        )
        self._midi_router.start()

    def add_beat_instrument(self):
        """Add an instrument to the beat kit."""
        trk_ops.add_beat_instrument(self.state)

    def delete_beat_instrument(self, iid):
        """Remove an instrument from the beat kit."""
        trk_ops.delete_beat_instrument(self.state, iid)

    # ---- Soundfont ----

    def open_config(self):
        """Open the configuration dialog."""
        dlg = ConfigDialog(self, self)
        dlg.exec()

    def switch_backend(self, backend: str, server_address: str = '') -> bool:
        """Switch the audio backend at runtime.  Returns True if successful.

        Called by ConfigDialog when the user changes the backend selector.
        Tears down the running engine, reinitialises with the new backend, and
        reloads the SF2 if one is set.  Safe to call while not playing.

        Valid backend values: 'binding', 'server', 'fluidsynth'.
        """
        if self.state.playing:
            self.stop_play()

        # Tear down current engine (and the graph panel bound to it)
        self._teardown_graph_panel()
        if self.engine:
            try:
                self.engine.shutdown()
            except Exception:
                pass
            self.engine = None

        # Persist the choice
        self.settings.audio_backend = backend
        self.settings.server_address = server_address
        self.settings.save()

        # Reinitialise
        self._init_engine()

        # Re-apply SF2 if one is loaded
        if self.engine and self.state.sf2:
            from .ops.export import _get_sf2_path
            sf2_path = _get_sf2_path(self.state.sf2)
            if sf2_path:
                self.engine.load_sf2(sf2_path)

        # Rebuild the graph panel against the new engine; if the Graph
        # workspace is showing, drop back to Arrange when unsupported.
        self._ensure_graph_panel()
        if self.workspace == 'graph' and not self._graph_supported():
            self.set_workspace('arrange')

        return self.engine is not None

    def _on_config_changed(self):
        """Called by ConfigDialog after settings are saved; update dependent UI."""
        if hasattr(self, 'piano_roll'):
            self.piano_roll._update_rec_btn_enabled()
        # Restart MIDI router with the (possibly changed) device
        self._restart_midi_router()

    def load_sf2(self):
        """Open dialog to select and load a soundfont."""
        sf2_list = scan_directory(self.instruments_dir)
        dlg = SF2Dialog(self, self, sf2_list if sf2_list else [])
        if dlg.exec():
            self.state.sf2 = dlg.result
            if self.engine and dlg.result:
                from .ops.export import _get_sf2_path
                sf2_path = _get_sf2_path(dlg.result)
                if sf2_path:
                    self.engine.load_sf2(sf2_path)
            self.state.notify('sf2_loaded')

    # ---- Playback helpers ----

    def play_note(self, pitch, velocity, track_id=None):
        """Play a single note preview."""
        play_ops.play_note(self.state, self.engine, self.player,
                           pitch, velocity, track_id)

    def play_beat_hit(self, inst_id):
        """Play a single beat instrument hit."""
        play_ops.play_beat_hit(self.state, self.engine, self.player, inst_id)

    def preview_pattern(self):
        """Preview the currently selected pattern."""
        arr = play_ops.build_pattern_preview(self.state)
        if arr:
            self._render_and_play(arr)

    def preview_beat_pattern(self):
        """Preview the currently selected beat pattern."""
        arr = play_ops.build_beat_pattern_preview(self.state)
        if arr:
            self._render_and_play(arr)

    def _render_and_play(self, arr):
        """Render an arrangement and play it in a background thread."""
        from .ops.export import _get_sf2_path
        play_ops.render_and_play_arr(
            arr, _get_sf2_path(self.state.sf2), self.player,
            engine=self.engine)

    # ---- Pattern/Beat Pattern Dialogs ----
    
    def show_pattern_dialog(self, pattern_id=None):
        """Show pattern creation/edit dialog."""
        dialog = PatternDialog(self, self, pattern_id)
        dialog.exec()
        self._refresh_all()
    
    def show_beat_pattern_dialog(self, pattern_id=None):
        """Show beat pattern creation/edit dialog."""
        dialog = BeatPatternDialog(self, self, pattern_id)
        dialog.exec()
        self._refresh_all()

    # ---- Playback ----

    def toggle_play(self):
        if self.state.playing:
            self.stop_play()
        else:
            self.start_play()

    def toggle_loop(self):
        self.state.looping = not self.state.looping
        if self.state.looping:
            if self.state.loop_end is None:
                length = play_ops.compute_arrangement_length(self.state)
                if length > 0:
                    self.state.loop_start = 0.0
                    self.state.loop_end = length
                else:
                    self.state.loop_start = 0.0
                    self.state.loop_end = float(self.state.ts_num)
        play_ops.sync_loop_to_engine(self.state, self.engine)
        self.topbar.refresh()
        self.arrangement.refresh()

    def _sync_loop_to_engine(self):
        """Push current loop state to the engine."""
        play_ops.sync_loop_to_engine(self.state, self.engine)

    def start_play(self):
        """Start full arrangement playback."""
        if self.engine:
            self._start_play_engine()
            return
        self._start_play_legacy()

    def _start_play_engine(self):
        """Start playback via the realtime audio engine."""
        max_beat = play_ops.compute_arrangement_length(self.state)
        if max_beat == 0:
            return

        self.state.playing = True
        self.state.playhead = 0
        self._playback_max_beat = max_beat
        self.topbar.refresh()

        play_ops.sync_loop_to_engine(self.state, self.engine)

        #self.engine.mark_dirty() # This is redundant - self.engine.play() already does mark_dirty()
        self.engine.seek(0.0)
        self.engine.play()

        self._start_playhead_timer()

    def _start_playhead_timer(self):
        """Start a QTimer to poll engine.current_beat and update the UI playhead."""
        if self._play_timer:
            self._play_timer.stop()

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(30)  # ~33fps
        self._play_timer.timeout.connect(self._update_playhead)
        self._play_timer.start()

    def _update_playhead(self):
        """Poll engine beat position and update UI."""
        if not self.engine or not self.state.playing:
            self._stop_playhead_timer()
            return

        beat = self.engine.current_beat

        # Check if engine stopped itself (reached end of arrangement)
        if not self.engine.is_playing:
            self.stop_play()
            return

        self.state.playhead = beat

        # Animate BPM display if tempo automation is active
        if hasattr(self.engine, 'current_bpm') and self.state.find_tempo_track():
            self.topbar.update_bpm_display(self.engine.current_bpm)

        self.topbar.update_meter()
        self.arrangement.refresh()
        self.piano_roll.grid_widget.update()  # Update piano roll for background notes

    def _stop_playhead_timer(self):
        if self._play_timer:
            self._play_timer.stop()
            self._play_timer = None

    def _start_play_legacy(self):
        """Legacy offline-render playback (fallback when engine unavailable)."""
        arr = self.state.build_arrangement()
        has_notes = any(
            any(n for p in t.get('placements', []) for n in p.get('pattern', {}).get('notes', []))
            for t in arr.get('tracks', [])
        )
        if not has_notes:
            return

        max_beat = play_ops.compute_arrangement_length(self.state)
        if max_beat == 0:
            return

        self.state.playing = True
        self.state.playhead = 0
        self._playback_max_beat = max_beat
        self.topbar.refresh()

        from .ops.export import _get_sf2_path
        sf2_path = _get_sf2_path(self.state.sf2)

        def render_and_start():
            midi = create_midi(arr)
            wav = None
            if sf2_path:
                wav = render_fluidsynth(midi, sf2_path)
            if wav is None:
                wav = render_basic(arr)
            if wav:
                self.player.play_wav(wav)
                QTimer.singleShot(0, self._start_legacy_playhead)

        threading.Thread(target=render_and_start, daemon=True).start()

    def _start_legacy_playhead(self):
        """Wall-clock playhead animation for legacy playback."""
        import time as _time
        beat_duration = 60.0 / self.state.bpm
        max_beat = self._playback_max_beat
        start_time = _time.time()

        def update():
            if not self.state.playing:
                return
            elapsed = _time.time() - start_time
            current_beat = elapsed / beat_duration
            if self.state.looping:
                self.state.playhead = current_beat % max_beat
                QTimer.singleShot(30, update)
            elif current_beat >= max_beat:
                self.stop_play()
            else:
                self.state.playhead = current_beat
                QTimer.singleShot(30, update)
            self.arrangement.refresh()

        update()

    def stop_play(self):
        self.state.playing = False
        self.state.playhead = None
        self._stop_playhead_timer()
        if self.engine:
            self.engine.stop()
        self.player.stop()
        # Reset BPM display to base value
        self.topbar.update_bpm_display(self.state.bpm)
        self.topbar.refresh()
        self.arrangement.refresh()

    # ---- Export ----

    def do_export(self, fmt):
        """Export the arrangement as MIDI, WAV, MP3, or MusicXML."""
        _proj_dir = self.settings.get_recent_dir('project')
        if fmt == 'midi':
            path, _ = QFileDialog.getSaveFileName(
                None, 'Export MIDI', _proj_dir, 'MIDI files (*.mid);;All files (*.*)',
                options=QFileDialog.DontUseNativeDialog)
            if path:
                self.settings.set_recent_dir('project', path)
                midi = export_ops.export_midi(self.state)
                with open(path, 'wb') as f:
                    f.write(midi)
            return

        if fmt == 'musicxml':
            path, _ = QFileDialog.getSaveFileName(
                None, 'Export MusicXML', _proj_dir,
                'MusicXML files (*.musicxml *.xml);;All files (*.*)',
                options=QFileDialog.DontUseNativeDialog)
            if path:
                self.settings.set_recent_dir('project', path)
                data = export_ops.export_musicxml(self.state)
                with open(path, 'wb') as f:
                    f.write(data)
            return

        # Get file path BEFORE starting background thread
        if fmt == 'mp3':
            path, _ = QFileDialog.getSaveFileName(
                None, 'Export MP3', _proj_dir, 'MP3 files (*.mp3);;All files (*.*)',
                options=QFileDialog.DontUseNativeDialog)
        else:
            path, _ = QFileDialog.getSaveFileName(
                None, 'Export WAV', _proj_dir, 'WAV files (*.wav);;All files (*.*)',
                options=QFileDialog.DontUseNativeDialog)

        if not path:
            return
        self.settings.set_recent_dir('project', path)

        engine = self.engine

        def render_work():
            if fmt == 'mp3':
                data = export_ops.render_mp3(self.state, engine)
                if data is None:
                    QTimer.singleShot(0, lambda: QMessageBox.critical(
                        self, 'Error', 'ffmpeg not available for MP3 conversion'))
                    return
            else:
                data = export_ops.render_wav(self.state, engine)
                if data is None:
                    QTimer.singleShot(0, lambda: QMessageBox.critical(
                        self, 'Error', 'No notes to render'))
                    return

            with open(path, 'wb') as f:
                f.write(data)

        threading.Thread(target=render_work, daemon=True).start()

    # ---- New/Save/Load ----

    def new_project(self):
        # Resolve relative to this file so the path is correct whether running
        # from source or frozen inside a PyInstaller bundle.
        path = str(Path(__file__).resolve().parent.parent / 'defaults' / 'initial.json')
        if path:
            try:
                def sf2_loader(sf2_path):
                    self.state.sf2 = SF2Info(sf2_path)
                    if self.engine:
                        self.engine.load_sf2(sf2_path)

                project_io.load_project(self.state, path, sf2_loader)
                self.state.sel_pat = self.state.patterns[0].id
                self.piano_roll.clear_selection()
                self.topbar.refresh()
                # Build/sync graph model after loading
                self._ensure_graph_model()
                if self._graph_editor_window is not None:
                    self._graph_editor_window._canvas.set_model(self.state.signal_graph)
                self._regraph_view()
                self._refresh_all()
            except Exception as e:
                QMessageBox.critical(self, 'Error', f'Failed to load initial state: {e}')

    def save_project(self):
        """Save As — always prompt for a file path."""
        path, _ = QFileDialog.getSaveFileName(
            None, 'Save Project', self.settings.get_recent_dir('project'),
            'JSON files (*.json);;All files (*.*)',
            options=QFileDialog.DontUseNativeDialog)
        if path:
            self.settings.set_recent_dir('project', path)
            project_io.save_project(self.state, path)

    def save_project_current(self):
        """Save to the current project path if known, otherwise prompt."""
        path = getattr(self.state, '_project_path', None)
        if path and os.path.isdir(os.path.dirname(path)):
            project_io.save_project(self.state, path)
        else:
            self.save_project()

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            None, 'Load Project', self.settings.get_recent_dir('project'),
            'JSON files (*.json);;All files (*.*)',
            options=QFileDialog.DontUseNativeDialog)
        if path:
            self.settings.set_recent_dir('project', path)
            try:
                def sf2_loader(sf2_path):
                    self.state.sf2 = SF2Info(sf2_path)
                    if self.engine:
                        self.engine.load_sf2(sf2_path)

                project_io.load_project(self.state, path, sf2_loader)
                self.piano_roll.clear_selection()
                self.topbar.refresh()
                # Sync/rebuild graph model
                self._ensure_graph_model()
                if self._graph_editor_window is not None:
                    self._graph_editor_window._canvas.set_model(self.state.signal_graph)
                self._regraph_view()
                self._refresh_all()
            except Exception as e:
                QMessageBox.critical(self, 'Error', f'Failed to load project: {e}')

    def import_midi(self):
        path, _ = QFileDialog.getOpenFileName(
            None, 'Import MIDI', self.settings.get_recent_dir('project'),
            'MIDI files (*.mid *.midi);;All files (*.*)',
            options=QFileDialog.DontUseNativeDialog)
        if not path:
            return
        self.settings.set_recent_dir('project', path)
        try:
            stats = midi_import(self.state, path, segment=True)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f'Failed to import MIDI: {e}')
            return
        # Drop the loaded project path so the next Save prompts for a name.
        self.state._project_path = None
        self.piano_roll.clear_selection()
        self.topbar.refresh()
        self._ensure_graph_model()
        if self._graph_editor_window is not None:
            self._graph_editor_window._canvas.set_model(self.state.signal_graph)
        self._regraph_view()
        self._refresh_all()
        QMessageBox.information(
            self, 'MIDI imported',
            f"Loaded {stats['tracks']} track(s), "
            f"{stats['patterns']} pattern(s), "
            f"{stats['placements']} placement(s). "
            f"Segmentation found repeats in {stats['segmented_tracks']} track(s).",
        )

    def closeEvent(self, event):
        """Clean up audio engine on window close."""
        self._stop_playhead_timer()
        if self._midi_router is not None:
            self._midi_router.stop()
            self._midi_router = None
        if self.engine:
            self.engine.shutdown()
        self.player.stop()
        super().closeEvent(event)
