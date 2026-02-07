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
from .core.sf2 import SF2Info, scan_directory
from .core.midi import create_midi
from .core.audio import (
    render_fluidsynth, render_basic, wav_to_mp3,
    generate_preview_tone, render_sample, AudioPlayer,
)
from .ui.topbar import TopBar
from .ui.pattern_list import PatternList
from .ui.arrangement import ArrangementView
from .ui.piano_roll import PianoRoll
from .ui.beat_grid import BeatGrid
from .ui.track_panel import TrackPanel
from .ui.dialogs import PatternDialog, BeatPatternDialog, SF2Dialog


class App(QMainWindow):
    """Main application - owns the state, creates the window, coordinates UI."""
    
    # Signal to communicate from background thread to main thread
    start_playback_animation = Signal()

    def __init__(self, instruments_dir=None):
        super().__init__()
        self.state = AppState()
        self.player = AudioPlayer()
        self.instruments_dir = instruments_dir or str(
            Path(__file__).parent.parent / 'instruments')

        # Drag-and-drop state
        self._drag_type = None
        self._drag_pid = None

        # Playback state
        self._play_timer = None
        self._playback_max_beat = 0
        
        # Connect the playback signal
        self.start_playback_animation.connect(self._start_playhead_animation)

        self._setup_theme()
        self._build_ui()
        self._bind_keys()
        self._init_state()

        # Connect state observer
        self.state.on_change(self._on_state_change)

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
        QShortcut(QKeySequence.Paste, self, self._on_paste)
        QShortcut(QKeySequence.SelectAll, self, self._on_select_all)
        QShortcut(QKeySequence.Delete, self, self._on_delete)
        QShortcut(Qt.Key_Backspace, self, self._on_delete)

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

        # Auto-load first SF2
        self._auto_load_sf2()

        # Initial render
        self._refresh_all()

    def _auto_load_sf2(self):
        """Try to load the first SF2 file from the instruments directory."""
        sf2_list = scan_directory(self.instruments_dir)
        if sf2_list:
            self.state.sf2 = sf2_list[0]

    def _on_state_change(self, source=None):
        """Called whenever state changes. Refreshes relevant UI components."""
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
        # Copy functionality would go here
        pass

    def _on_paste(self):
        # Paste functionality would go here
        pass

    def _on_select_all(self):
        # Select all functionality would go here
        pass

    def _on_delete(self):
        # Delete functionality would go here
        pass

    # ---- Pattern management ----

    def add_pattern(self):
        """Create a new melodic pattern."""
        dlg = PatternDialog(self, self.state)
        if dlg.exec():
            pat = Pattern(
                id=self.state.new_id(),
                name=dlg.name,
                length=dlg.length,
                notes=[],
                color=dlg.color,
                key=dlg.key,
                scale=dlg.scale,
            )
            self.state.patterns.append(pat)
            self.state.sel_pat = pat.id
            self.state.notify('add_pattern')

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
        pat = self.state.find_pattern(pid)
        if not pat:
            return
        new_pat = Pattern(
            id=self.state.new_id(),
            name=f'{pat.name} (copy)',
            length=pat.length,
            notes=[n.copy() for n in pat.notes],
            color=pat.color,
            key=pat.key,
            scale=pat.scale,
        )
        self.state.patterns.append(new_pat)
        self.state.sel_pat = new_pat.id
        self.state.notify('duplicate_pattern')

    def delete_pattern(self, pid):
        """Delete a pattern and its placements."""
        self.state.patterns = [p for p in self.state.patterns if p.id != pid]
        self.state.placements = [p for p in self.state.placements if p.pattern_id != pid]
        if self.state.sel_pat == pid:
            self.state.sel_pat = self.state.patterns[0].id if self.state.patterns else None
        self.state.notify('delete_pattern')

    def add_beat_pattern(self):
        """Create a new beat pattern."""
        dlg = BeatPatternDialog(self, self.state)
        if dlg.exec():
            from .state import BeatPattern
            pat = BeatPattern(
                id=self.state.new_id(),
                name=dlg.name,
                length=dlg.length,
                steps={},
                color=dlg.color,
            )
            self.state.beat_patterns.append(pat)
            self.state.sel_beat_pat = pat.id
            self.state.notify('add_beat_pattern')

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
        from .state import BeatPattern
        pat = self.state.find_beat_pattern(pid)
        if not pat:
            return
        new_pat = BeatPattern(
            id=self.state.new_id(),
            name=f'{pat.name} (copy)',
            length=pat.length,
            steps={k: list(v) for k, v in pat.steps.items()},
            color=pat.color,
        )
        self.state.beat_patterns.append(new_pat)
        self.state.sel_beat_pat = new_pat.id
        self.state.notify('duplicate_beat_pattern')

    def delete_beat_pattern(self, pid):
        """Delete a beat pattern and its placements."""
        self.state.beat_patterns = [p for p in self.state.beat_patterns if p.id != pid]
        self.state.beat_placements = [p for p in self.state.beat_placements if p.pattern_id != pid]
        if self.state.sel_beat_pat == pid:
            self.state.sel_beat_pat = (self.state.beat_patterns[0].id
                                       if self.state.beat_patterns else None)
        self.state.notify('delete_beat_pattern')

    # ---- Track management ----

    def add_track(self):
        """Create a new track."""
        t = Track(id=self.state.new_id(), name=f'Track {len(self.state.tracks) + 1}',
                  channel=len(self.state.tracks) % 16)
        self.state.tracks.append(t)
        self.state.sel_trk = t.id
        self.state.notify('add_track')

    def delete_track(self, tid):
        """Delete a track and its placements."""
        self.state.tracks = [t for t in self.state.tracks if t.id != tid]
        self.state.placements = [p for p in self.state.placements if p.track_id != tid]
        if self.state.sel_trk == tid:
            self.state.sel_trk = self.state.tracks[0].id if self.state.tracks else None
        self.state.notify('delete_track')

    def add_beat_track(self):
        """Create a new beat track."""
        bt = BeatTrack(id=self.state.new_id(),
                       name=f'Beat {len(self.state.beat_tracks) + 1}')
        self.state.beat_tracks.append(bt)
        self.state.sel_beat_trk = bt.id
        self.state.notify('add_beat_track')

    def delete_beat_track(self, btid):
        """Delete a beat track and its placements."""
        self.state.beat_tracks = [t for t in self.state.beat_tracks if t.id != btid]
        self.state.beat_placements = [p for p in self.state.beat_placements
                                       if p.track_id != btid]
        if self.state.sel_beat_trk == btid:
            self.state.sel_beat_trk = (self.state.beat_tracks[0].id
                                       if self.state.beat_tracks else None)
        self.state.notify('delete_beat_track')

    def add_beat_instrument(self):
        """Add an instrument to the beat kit."""
        inst = BeatInstrument(
            id=self.state.new_id(),
            name=f'Inst {len(self.state.beat_kit) + 1}',
            channel=9,  # Drum channel
            pitch=36,   # Bass drum
            velocity=100,
        )
        self.state.beat_kit.append(inst)
        self.state.notify('beat_kit')

    def delete_beat_instrument(self, iid):
        """Remove an instrument from the beat kit."""
        self.state.beat_kit = [i for i in self.state.beat_kit if i.id != iid]
        # Remove steps using this instrument from all beat patterns
        for pat in self.state.beat_patterns:
            if iid in pat.steps:
                del pat.steps[iid]
        self.state.notify('beat_kit')

    # ---- Soundfont ----

    def load_sf2(self):
        """Open dialog to select and load a soundfont."""
        sf2_list = scan_directory(self.instruments_dir)
        dlg = SF2Dialog(self, self, sf2_list if sf2_list else [])
        if dlg.exec():
            self.state.sf2 = dlg.result
            self.state.notify('sf2_loaded')

    # ---- Playback helpers ----

    def play_note(self, pitch, velocity, track_id=None):
        """Play a single note preview, using track instrument if available."""
        # Get track instrument info if track_id provided
        bank, program, channel = 0, 0, 0
        if track_id:
            t = self.state.find_track(track_id)
            if t:
                bank, program, channel = t.bank, t.program, t.channel
        
        # Try SF2 rendering if available
        if self.state.sf2:
            sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                        else self.state.sf2.get('path'))
            if sf2_path:
                try:
                    wav = render_sample(sf2_path, bank, program, pitch, velocity, 
                                       duration=0.5, channel=channel)
                    if wav:
                        self.player.play_async(wav)
                        return
                except Exception:
                    pass  # Fall through to basic tone
        
        # Fallback to basic tone
        wav = generate_preview_tone(pitch, velocity, 0.3)
        self.player.play_async(wav)

    def play_beat_hit(self, inst_id):
        """Play a single beat instrument hit."""
        inst = next((i for i in self.state.beat_kit if i.id == inst_id), None)
        if not inst:
            return
        sf2_path = None
        if self.state.sf2:
            sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                        else self.state.sf2.get('path'))
        if sf2_path:
            # For beat instruments, use bank/program/pitch with channel (usually 9 for drums)
            wav = render_sample(sf2_path, inst.bank, inst.program, inst.pitch, 
                              inst.velocity, duration=0.5, channel=inst.channel)
            if wav:
                self.player.play_async(wav)
        else:
            wav = generate_preview_tone(inst.pitch, inst.velocity, 0.3)
            self.player.play_async(wav)

    def preview_pattern(self):
        """Preview the currently selected pattern."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not pat.notes:
            return

        t = self.state.find_track(self.state.sel_trk)
        if not t:
            t = Track(id='preview', name='Preview', channel=0,
                      bank=0, program=0, volume=100)

        inst = {
            'name': t.name, 'channel': t.channel,
            'bank': t.bank, 'program': t.program,
            'volume': t.volume,
        }

        notes = [{'pitch': n.pitch, 'start': n.start, 'duration': n.duration,
                  'velocity': n.velocity} for n in pat.notes]

        tracks = [{
            **inst,
            'placements': [{
                'pattern': {'notes': notes, 'length': pat.length},
                'time': 0, 'transpose': 0, 'repeats': 1,
            }]
        }]

        arr = {'bpm': self.state.bpm, 'tsNum': self.state.ts_num,
               'tsDen': self.state.ts_den, 'tracks': tracks}
        self._render_and_play(arr)

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

    def preview_beat_pattern(self):
        """Preview the currently selected beat pattern."""
        pat = self.state.find_beat_pattern(self.state.sel_beat_pat)
        if not pat or not pat.grid:
            return

        tracks = []
        for inst in self.state.beat_kit:
            grid = pat.grid.get(inst.id)
            if not grid:
                continue
            notes = []
            # Convert grid to notes
            for step_idx, vel in enumerate(grid):
                if vel > 0:
                    step_pos = step_idx / pat.subdivision
                    notes.append({
                        'pitch': inst.pitch,
                        'start': step_pos,
                        'duration': 0.25,
                        'velocity': vel,
                    })
            if notes:
                tracks.append({
                    'name': inst.name, 'channel': inst.channel,
                    'bank': inst.bank, 'program': inst.program,
                    'volume': 100,
                    'placements': [{
                        'pattern': {'notes': notes, 'length': pat.length},
                        'time': 0, 'transpose': 0, 'repeats': 1,
                    }]
                })

        if not tracks:
            return

        arr = {'bpm': self.state.bpm, 'tsNum': self.state.ts_num,
               'tsDen': self.state.ts_den, 'tracks': tracks}
        self._render_and_play(arr)

    def _render_and_play(self, arr):
        """Render an arrangement and play it in a background thread."""
        def work():
            midi = create_midi(arr)
            wav = None
            if self.state.sf2:
                sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                            else self.state.sf2.get('path'))
                if sf2_path:
                    wav = render_fluidsynth(midi, sf2_path)
            if wav is None:
                wav = render_basic(arr)
            if wav:
                self.player.play_async(wav)

        threading.Thread(target=work, daemon=True).start()

    # ---- Playback ----

    def toggle_play(self):
        if self.state.playing:
            self.stop_play()
        else:
            self.start_play()

    def toggle_loop(self):
        self.state.looping = not self.state.looping
        self.topbar.refresh()

    def start_play(self):
        """Start full arrangement playback."""
        arr = self.state.build_arrangement()
        # Check if there are any notes
        has_notes = any(
            any(n for p in t.get('placements', []) for n in p.get('pattern', {}).get('notes', []))
            for t in arr.get('tracks', [])
        )
        if not has_notes:
            return

        # Calculate total duration in beats
        max_beat = 0
        for pl in self.state.placements:
            pat = self.state.find_pattern(pl.pattern_id)
            if pat:
                max_beat = max(max_beat, pl.time + pat.length * (pl.repeats or 1))
        for bp in self.state.beat_placements:
            pat = self.state.find_beat_pattern(bp.pattern_id)
            if pat:
                max_beat = max(max_beat, bp.time + pat.length * (bp.repeats or 1))
        
        if max_beat == 0:
            return  # Nothing to play
        
        self.state.playing = True
        self.state.playhead = 0
        self.topbar.refresh()

        # Store max_beat as instance variable for the animation function
        self._playback_max_beat = max_beat

        def render_and_start():
            midi = create_midi(arr)
            wav = None
            if self.state.sf2:
                sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                            else self.state.sf2.get('path'))
                if sf2_path:
                    wav = render_fluidsynth(midi, sf2_path)
            if wav is None:
                wav = render_basic(arr)
            if wav:
                self.player.play_wav(wav)
                # Emit signal to start animation on main thread
                self.start_playback_animation.emit()

        threading.Thread(target=render_and_start, daemon=True).start()

    def _start_playhead_animation(self):
        """Start playhead animation on the main thread."""
        max_beat = self._playback_max_beat
        import time
        beat_duration = 60.0 / self.state.bpm
        start_time = time.time()
        
        def update_playhead():
            if not self.state.playing:
                return
            
            current_time = time.time() - start_time
            current_beat = current_time / beat_duration
            
            if self.state.looping:
                # Loop back to start
                self.state.playhead = current_beat % max_beat
                QTimer.singleShot(30, update_playhead)
            else:
                # Check if we've reached the end
                if current_beat >= max_beat:
                    self.stop_play()
                else:
                    self.state.playhead = current_beat
                    QTimer.singleShot(30, update_playhead)
            
            # Force refresh on main thread
            self.arrangement.refresh()
        
        # Start the update loop
        update_playhead()

    def stop_play(self):
        self.state.playing = False
        self.state.playhead = None
        self.player.stop()
        self.topbar.refresh()
        self.arrangement.refresh()

    # ---- Export ----

    def do_export(self, fmt):
        """Export the arrangement as MIDI, WAV, or MP3."""
        arr = self.state.build_arrangement()
        midi = create_midi(arr)

        if fmt == 'midi':
            path, _ = QFileDialog.getSaveFileName(
                self, 'Export MIDI', '', 'MIDI files (*.mid);;All files (*.*)')
            if path:
                with open(path, 'wb') as f:
                    f.write(midi)
                QMessageBox.information(self, 'Export', f'MIDI exported to {path}')
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

        def render_work():
            wav = None
            if self.state.sf2:
                sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                            else self.state.sf2.get('path'))
                if sf2_path:
                    wav = render_fluidsynth(midi, sf2_path)
            if wav is None:
                wav = render_basic(arr)
            if wav is None:
                QTimer.singleShot(0, lambda: QMessageBox.critical(
                    self, 'Error', 'No notes to render'))
                return

            if fmt == 'mp3':
                mp3 = wav_to_mp3(wav)
                if mp3:
                    with open(path, 'wb') as f:
                        f.write(mp3)
                    # Item 10: Don't show success message
                else:
                    QTimer.singleShot(0, lambda: QMessageBox.critical(
                        self, 'Error', 'ffmpeg not available for MP3 conversion'))
            else:
                with open(path, 'wb') as f:
                    f.write(wav)
                # Item 10: Don't show success message

        threading.Thread(target=render_work, daemon=True).start()

    # ---- Save/Load ----

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save Project', '', 'JSON files (*.json);;All files (*.*)')
        if path:
            with open(path, 'w') as f:
                f.write(self.state.to_json())
            self.state._project_path = path

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load Project', '', 'JSON files (*.json);;All files (*.*)')
        if path:
            try:
                with open(path) as f:
                    self.state.load_json(f.read())
                self.state._project_path = path
                # Try to reload SF2 if path hint exists
                if hasattr(self.state, '_sf2_path_hint') and self.state._sf2_path_hint:
                    try:
                        self.state.sf2 = SF2Info(self.state._sf2_path_hint)
                    except Exception:
                        pass
                self.piano_roll.clear_selection()
                self.topbar.refresh()
                self._refresh_all()
            except Exception as e:
                QMessageBox.critical(self, 'Error', f'Failed to load project: {e}')
