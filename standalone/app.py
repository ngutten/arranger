"""Main application class - creates the window, wires up UI components."""

import os
import threading
from pathlib import Path

from PySide6.QtWidgets import (QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
                                QSplitter, QFileDialog, QMessageBox)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QPalette, QColor

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
from .core.audio import render_fluidsynth, render_basic, AudioPlayer
from .core.settings import Settings

try:
    from .core.engine import AudioEngine
    _HAS_ENGINE = True
except ImportError:
    _HAS_ENGINE = False

try:
    from .core.server_engine import ServerEngine
    _HAS_SERVER_ENGINE = True
except ImportError:
    _HAS_SERVER_ENGINE = False

try:
    from .core.binding_engine import BindingEngine
    _HAS_BINDING_ENGINE = True
except ImportError:
    _HAS_BINDING_ENGINE = False

from .ui.topbar import TopBar
from .ui.pattern_list import PatternList
from .ui.arrangement import ArrangementView
from .ui.piano_roll import PianoRoll
from .ui.beat_grid import BeatGrid
from .ui.track_panel import TrackPanel
from .ui.dialogs import PatternDialog, BeatPatternDialog, SF2Dialog, ConfigDialog

#try:
from .graph_editor import GraphModel, GraphEditorWindow
_HAS_GRAPH_EDITOR = True
#except ImportError:
#    _HAS_GRAPH_EDITOR = False

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
        }

        # Realtime audio engine
        self.engine = None  # initialized in _init_engine()

        # Graph editor window (non-modal; lazily created)
        self._graph_editor_window = None

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

        # Arrangement view (top)
        self.arrangement = ArrangementView(self.splitter, self)
        self.splitter.addWidget(self.arrangement)

        # Editor area (bottom) - switches between piano roll and beat grid
        self.editor_container = QWidget()
        self.editor_layout = QVBoxLayout(self.editor_container)
        self.editor_layout.setContentsMargins(0, 0, 0, 0)
        
        self.piano_roll = PianoRoll(self.editor_container, self)
        self.beat_grid = BeatGrid(self.editor_container, self)

        # Start with piano roll visible
        self.editor_layout.addWidget(self.piano_roll)
        self.editor_layout.addWidget(self.beat_grid)
        self.piano_roll.show()
        self.beat_grid.hide()
        self._current_editor = 'piano_roll'

        self.splitter.addWidget(self.editor_container)
        self.splitter.setSizes([400, 280])

        main_layout.addWidget(self.splitter, 1)

        # Right panel (track settings)
        self.track_panel = TrackPanel(main, self)
        main_layout.addWidget(self.track_panel)

        layout.addWidget(main)

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
        
        redo_shortcut = QShortcut(QKeySequence.StandardKey.Redo, self)
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

        if backend in ('binding', 'server') and _HAS_SERVER_ENGINE:
            # 'binding' falls through here if the .so wasn't built
            try:
                from .core.server_engine import DEFAULT_ADDRESS
                addr = self.settings.server_address or DEFAULT_ADDRESS
                self.engine = ServerEngine(self.state, self.settings, address=addr)
                return
            except Exception as e:
                print(f"[App] ServerEngine init failed: {e}; falling back")

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

    def open_graph_editor(self) -> None:
        """Open (or raise) the signal graph editor window."""
        if not _HAS_GRAPH_EDITOR:
            print("Error: no graph editor")
            return
        # Requires an engine that supports _send (BindingEngine or ServerEngine)
        if not (self.engine and hasattr(self.engine, '_send')):
            return

        if self._graph_editor_window is not None:
            self._graph_editor_window.raise_()
            self._graph_editor_window.activateWindow()
            return

        self._graph_editor_window = GraphEditorWindow(
            model=self.state.signal_graph,
            server_engine=self.engine,
            state=self.state,
            on_graph_changed=self._on_graph_model_changed,
            parent=None,   # free-floating window
        )
        self._graph_editor_window.closed.connect(self._on_graph_editor_closed)
        self._graph_editor_window.show()

    def _on_graph_model_changed(self, model) -> None:
        """Called when the graph editor makes a live change."""
        # model is the same object as self.state.signal_graph (edited in-place)
        # Nothing extra needed; the editor has already pushed to the server.
        pass

    def _on_graph_editor_closed(self) -> None:
        self._graph_editor_window = None

    def _on_state_change(self, source=None):
        """Called whenever state changes. Refreshes relevant UI components."""
        # Mark engine dirty so schedule rebuilds on next audio callback
        if self.engine and self.state.playing:
            self.engine.mark_dirty()

        # Capture undo snapshot for certain actions (synchronous, reads AppState not widgets)
        if source in self._undo_triggers:
            self._push_undo(source)

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
        else:
            self.beat_grid.refresh()
        self.track_panel.refresh()
    
    def _push_undo(self, source=None):
        """Push current state onto undo stack."""
        if source in ('undo', 'redo'):
            return  # Don't capture during undo/redo
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
            if self.engine and self.state.playing:
                self.engine.mark_dirty()
            self._refresh_all()

    def _switch_editor(self):
        """Switch between piano roll and beat grid based on selection."""
        if self.state.sel_beat_pat and self._current_editor != 'beat_grid':
            self.piano_roll.hide()
            if self.beat_grid.parent() != self.editor_container:
                self.editor_layout.addWidget(self.beat_grid)
            self.beat_grid.show()
            self._current_editor = 'beat_grid'
        elif not self.state.sel_beat_pat and self._current_editor != 'piano_roll':
            self.beat_grid.hide()
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
        if self.arrangement.selected_placements or self.arrangement.selected_beat_placements:
            self.arrangement.copy_selection()
        # Otherwise try piano roll
        elif self._current_editor == 'piano_roll':
            self.piano_roll._copy_to_clipboard()
        # TODO: Add beat_grid copy support when implemented

    def _on_cut(self):
        # Check if arranger has a selection
        if self.arrangement.selected_placements or self.arrangement.selected_beat_placements:
            self.arrangement.cut_selection()
        # Otherwise try piano roll
        elif self._current_editor == 'piano_roll':
            self.piano_roll._cut_to_clipboard()
        # TODO: Add beat_grid cut support when implemented

    def _on_paste(self):
        # Smart paste: check which clipboard has data and prioritize current context
        piano_has_data = self.piano_roll._note_clipboard.has_data()
        arrangement_has_data = self.arrangement.clipboard.has_data()
        
        # If current editor is piano roll and it has clipboard data, paste there
        if self._current_editor == 'piano_roll' and piano_has_data:
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
        # TODO: Add beat_grid paste support when implemented

    def _on_duplicate(self):
        if self._current_editor == 'piano_roll':
            self.piano_roll._duplicate_selection()
        # TODO: Add beat_grid duplicate support when implemented

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
        # TODO: Add beat_grid select all support when implemented

    def _on_delete(self):
        # Check if arranger has a selection
        if self.arrangement.selected_placements or self.arrangement.selected_beat_placements:
            self.arrangement.delete_selection()
        # Otherwise try piano roll
        elif self._current_editor == 'piano_roll':
            self.piano_roll._delete_selected()
        # TODO: Add beat_grid delete support when implemented

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

    def _push_graph_to_engine(self) -> None:
        """Push the current graph model to the engine if it supports _send."""
        if self.engine and hasattr(self.engine, '_send') and self.state.signal_graph:
            payload = self.state.signal_graph.to_server_dict(bpm=self.state.bpm)
            self.engine._send(payload)
            # Refresh graph editor canvas if open
            if self._graph_editor_window is not None:
                self._graph_editor_window._canvas.update()

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

        # Tear down current engine
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

        return self.engine is not None

    def _on_config_changed(self):
        """Called by ConfigDialog after settings are saved; update dependent UI."""
        if hasattr(self, 'piano_roll'):
            self.piano_roll._update_rec_btn_enabled()

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
            arr, _get_sf2_path(self.state.sf2), self.player)

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

        self.engine.mark_dirty()
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
        self.topbar.refresh()
        self.arrangement.refresh()

    # ---- Export ----

    def do_export(self, fmt):
        """Export the arrangement as MIDI, WAV, or MP3."""
        if fmt == 'midi':
            path, _ = QFileDialog.getSaveFileName(
                self, 'Export MIDI', '', 'MIDI files (*.mid);;All files (*.*)')
            if path:
                midi = export_ops.export_midi(self.state)
                with open(path, 'wb') as f:
                    f.write(midi)
            return

        # Get file path BEFORE starting background thread
        if fmt == 'mp3':
            path, _ = QFileDialog.getSaveFileName(
                self, 'Export MP3', '', 'MP3 files (*.mp3);;All files (*.*)')
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, 'Export WAV', '', 'WAV files (*.wav);;All files (*.*)')

        if not path:
            return

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
        path = "defaults/initial.json"
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
                self._refresh_all()
            except Exception as e:
                QMessageBox.critical(self, 'Error', f'Failed to load initial state: {e}')

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save Project', '', 'JSON files (*.json);;All files (*.*)')
        if path:
            project_io.save_project(self.state, path)

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load Project', '', 'JSON files (*.json);;All files (*.*)')
        if path:
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
                    self._graph_editor_window._canvas.frame_all()
                self._refresh_all()
            except Exception as e:
                QMessageBox.critical(self, 'Error', f'Failed to load project: {e}')

    def closeEvent(self, event):
        """Clean up audio engine on window close."""
        self._stop_playhead_timer()
        if self.engine:
            self.engine.shutdown()
        self.player.stop()
        super().closeEvent(event)
