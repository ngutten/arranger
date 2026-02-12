"""Piano roll editor - note editing on a pitch/time grid."""

import time

from PySide6.QtWidgets import (QFrame, QWidget, QScrollArea, QLabel, QPushButton,
                                QComboBox, QSlider, QVBoxLayout, QHBoxLayout)
from PySide6.QtCore import Qt, QRect, QPoint, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QKeyEvent

from ..state import NOTE_NAMES, scale_set, vel_color, Note
from ..clipboard import NoteClipboard


class PianoRoll(QFrame):
    """Piano roll editor with piano keys, note grid, and velocity lane."""

    NH = 14    # note row height
    BW = 80    # pixels per beat
    LO = 24    # lowest pitch displayed
    HI = 96    # highest pitch displayed

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state

        # Interaction state
        self._drag_note = None
        self._drag_offset_x = 0
        self._drag_start_pos = None  # QPoint to track initial click position for deadzone
        self._resize_note = None
        self._selected = set()
        
        # New interaction states
        self._marquee_start = None  # QPoint for marquee selection
        self._ghost_notes = []  # List of Note objects in ghost/paste mode
        self._ghost_offset = None  # (dx, dy) offset from original positions
        self._note_clipboard = NoteClipboard()

        # Bend tool state
        self._bend_drag_note = None   # Note currently being edited in bend mode
        self._bend_drag_point_idx = None  # index of control point being dragged, or None

        # MIDI recording state
        self._rec_midi_in = None       # rtmidi.MidiIn instance while armed/recording
        self._rec_notes = {}           # {pitch: start_time} for open note-ons
        self._rec_events = []          # [(start_beat, duration, pitch, velocity)]
        self._rec_start_time = None    # time.monotonic() when first note landed
        self._rec_armed = False        # True = waiting for first note-on
        self._rec_recording = False    # True = recording in progress
        
        # TODO: Implement undo/redo stack
        # self._undo_stack = []
        # self._redo_stack = []

        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header bar
        hdr = QFrame()
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(8, 4, 8, 4)

        self.name_label = QLabel('No pattern')
        self.name_label.setFont(QFont('TkDefaultFont', 9))
        hdr_layout.addWidget(self.name_label)

        preview_btn = QPushButton('Preview')
        preview_btn.clicked.connect(self.app.preview_pattern)
        hdr_layout.addWidget(preview_btn)

        hdr_layout.addStretch()

        # Note length
        hdr_layout.addWidget(QLabel('Len'))
        self.note_len_cb = QComboBox()
        self.note_len_cb.addItems(['snap', 'last', '1/16', '1/8', '1/4',
                                    '1/2', '1', '2', '4'])
        self.note_len_cb.setCurrentText(self.state.note_len)
        self.note_len_cb.currentTextChanged.connect(self._on_note_len)
        hdr_layout.addWidget(self.note_len_cb)

        # Tool buttons - Edit, Slice, and Bend
        self.edit_btn = QPushButton('Edit')
        self.edit_btn.setCheckable(True)
        self.edit_btn.clicked.connect(lambda: self._set_tool('edit'))
        hdr_layout.addWidget(self.edit_btn)

        self.slice_btn = QPushButton('Slice')
        self.slice_btn.setCheckable(True)
        self.slice_btn.clicked.connect(lambda: self._set_tool('slice'))
        hdr_layout.addWidget(self.slice_btn)

        self.bend_btn = QPushButton('Bend')
        self.bend_btn.setCheckable(True)
        self.bend_btn.clicked.connect(lambda: self._set_tool('bend'))
        hdr_layout.addWidget(self.bend_btn)

        # Velocity slider
        hdr_layout.addWidget(QLabel('Vel'))
        self.vel_slider = QSlider(Qt.Horizontal)
        self.vel_slider.setRange(1, 127)
        self.vel_slider.setValue(self.state.default_vel)
        self.vel_slider.valueChanged.connect(self._on_vel_change)
        self.vel_slider.setMaximumWidth(100)
        hdr_layout.addWidget(self.vel_slider)

        self.vel_label = QLabel('100')
        self.vel_label.setMinimumWidth(30)
        hdr_layout.addWidget(self.vel_label)

        # MIDI record button
        self.rec_btn = QPushButton('Rec')
        self.rec_btn.setCheckable(True)
        self.rec_btn.setStyleSheet(
            'QPushButton { color: #aaa; }'
            'QPushButton:checked { background-color: #c0392b; color: #fff; }'
            'QPushButton:disabled { color: #444; }'
        )
        self.rec_btn.setToolTip('Record from MIDI input (arm — starts on first note)')
        self.rec_btn.clicked.connect(self._toggle_rec)
        hdr_layout.addWidget(self.rec_btn)
        self._update_rec_btn_enabled()

        layout.addWidget(hdr)

        # Main area: piano keys + canvas + velocity lane
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # Piano keys
        self.keys_scroll = QScrollArea()
        self.keys_scroll.setFixedWidth(44)
        self.keys_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.keys_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.keys_scroll.setWidgetResizable(False)
        
        self.keys_widget = PianoKeysWidget(self)
        self.keys_scroll.setWidget(self.keys_widget)
        body.addWidget(self.keys_scroll)

        # Right side: note grid + velocity lane
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        # Note canvas with scrollbars
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        
        self.grid_widget = PianoGridWidget(self)
        self.scroll_area.setWidget(self.grid_widget)
        right.addWidget(self.scroll_area, 1)

        # Sync scrolling
        self.scroll_area.verticalScrollBar().valueChanged.connect(
            self.keys_scroll.verticalScrollBar().setValue
        )

        # Velocity lane
        self.vel_widget = VelocityWidget(self)
        self.vel_widget.setFixedHeight(50)
        right.addWidget(self.vel_widget)

        body.addLayout(right)
        layout.addLayout(body)
        
        # Set focus policy to receive keyboard events
        self.setFocusPolicy(Qt.StrongFocus)

    def _on_note_len(self, text):
        self.state.note_len = text

    def _set_tool(self, tool):
        self.state.tool = tool
        self._update_tool_buttons()

    def _update_tool_buttons(self):
        self.edit_btn.setChecked(self.state.tool == 'edit')
        self.slice_btn.setChecked(self.state.tool == 'slice')
        self.bend_btn.setChecked(self.state.tool == 'bend')

    def _on_vel_change(self, value):
        self.vel_label.setText(str(value))
        self.state.default_vel = value
        
        # If notes are selected, update their velocities
        if self._selected:
            pat = self.state.find_pattern(self.state.sel_pat)
            if pat:
                for idx in self._selected:
                    if 0 <= idx < len(pat.notes):
                        pat.notes[idx].velocity = value
                self.refresh()

    def _snap(self, beat):
        return int(beat / self.state.snap) * self.state.snap

    def _hit_bend_point(self, x, y, radius=6):
        """Hit-test bend control points. Returns (note, point_index) or (None, -1)."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return None, -1
        for n in pat.notes:
            if not n.bend:
                continue
            note_y_top = (self.HI - n.pitch) * self.NH
            note_y_center = note_y_top + self.NH // 2
            for i, (beat_off, semitones) in enumerate(n.bend):
                px = (n.start + beat_off) * self.BW
                # semitones in [-2, 2] -> y offset within the note row, ±2 rows
                py = note_y_center - int(semitones / 2.0 * self.NH * 2)
                if abs(x - px) <= radius and abs(y - py) <= radius:
                    return n, i
        return None, -1

    def _hit_note(self, x, y):
        """Hit test for notes. Returns (note, index, is_resize_handle)."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return None, -1, False
        pitch = self.HI - int(y / self.NH)
        beat = x / self.BW
        for i in range(len(pat.notes) - 1, -1, -1):
            n = pat.notes[i]
            if n.pitch == pitch and n.start <= beat < n.start + n.duration:
                is_resize = beat > n.start + n.duration - 0.15
                return n, i, is_resize
        return None, -1, False
    
    def _coords_to_beat_pitch(self, x, y):
        """Convert pixel coordinates to (beat, pitch)."""
        pitch = self.HI - int(y / self.NH)
        beat = x / self.BW
        return beat, pitch

    def refresh(self):
        """Redraw the piano roll."""
        pat = self.state.find_pattern(self.state.sel_pat)

        # Update header
        if pat:
            self.name_label.setText(
                f'{pat.name} ({pat.length}b, {pat.key} {pat.scale})')
        else:
            self.name_label.setText('No pattern')

        self._update_tool_buttons()
        
        # Update widget sizes
        pitch_range = self.HI - self.LO + 1
        total_h = pitch_range * self.NH
        beats = pat.length if pat else 16
        total_w = int(beats * self.BW)
        
        self.keys_widget.setMinimumSize(44, total_h)
        self.grid_widget.setMinimumSize(total_w, total_h)
        
        self.keys_widget.update()
        self.grid_widget.update()
        self.vel_widget.update()

    def clear_selection(self):
        self._selected.clear()
        self.refresh()
    
    def _copy_to_clipboard(self):
        """Copy selected notes to clipboard."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not self._selected:
            return
        notes_to_copy = [pat.notes[i] for i in sorted(self._selected)
                         if 0 <= i < len(pat.notes)]
        self._note_clipboard.copy(notes_to_copy)
    
    def _cut_to_clipboard(self):
        """Cut selected notes (copy + delete), enter ghost mode."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not self._selected:
            return
        
        self._copy_to_clipboard()
        
        from ..ops.note_edit import delete_selected
        self._selected = delete_selected(pat, self._selected)
        
        # Enter ghost mode immediately with clipboard contents
        self._ghost_notes = self._note_clipboard.paste()
        self._ghost_offset = (0, 0)
        
        self.state.notify('note_edit')
        self.refresh()
    
    def _paste_from_clipboard(self):
        """Enter ghost mode with clipboard contents."""
        if not self._note_clipboard.has_data():
            return
        self._ghost_notes = self._note_clipboard.paste()
        self._ghost_offset = (0, 0)
        self.refresh()
    
    def _duplicate_selection(self):
        """Duplicate selected notes with smart offset."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not self._selected:
            return
        
        self._copy_to_clipboard()
        offset_beats = max(self.state.snap, 1.0)
        
        from ..ops.note_edit import duplicate_notes
        self._selected = duplicate_notes(
            pat, self._selected, self._note_clipboard.notes, offset_beats)
        
        self.state.notify('note_add')
        self.refresh()
    
    def _commit_ghost_notes(self, mouse_x, mouse_y):
        """Commit ghost notes to the pattern at current mouse position."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not self._ghost_notes:
            return
        
        beat, pitch = self._coords_to_beat_pitch(mouse_x, mouse_y)
        
        from ..ops.note_edit import commit_ghost_notes
        self._selected = commit_ghost_notes(
            pat, self._ghost_notes, beat, pitch,
            self._snap, self.LO, self.HI)
        
        self._ghost_notes = []
        self._ghost_offset = None
        
        self.state.notify('note_add')
        self.refresh()
    
    def _cancel_ghost_mode(self):
        """Cancel ghost mode without placing notes."""
        self._ghost_notes = []
        self._ghost_offset = None
        self.refresh()
    
    def _delete_selected(self):
        """Delete all selected notes."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not self._selected:
            return
        
        from ..ops.note_edit import delete_selected
        self._selected = delete_selected(pat, self._selected)
        self.state.notify('note_edit')
        self.refresh()
    
    def _merge_selected_notes(self):
        """Merge two selected adjacent notes at the same pitch."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or len(self._selected) != 2:
            return
        
        from ..ops.note_edit import merge_notes
        result = merge_notes(pat, self._selected)
        if result is not None:
            self._selected = result
            self.state.notify('note_edit')
            self.refresh()

    # ---- MIDI recording ----

    def _update_rec_btn_enabled(self):
        """Enable Rec only when a MIDI device is configured."""
        try:
            has_device = bool(
                hasattr(self.app, 'settings') and
                self.app.settings.midi_input_device
            )
        except Exception:
            has_device = False
        self.rec_btn.setEnabled(has_device)
        if not has_device:
            self.rec_btn.setToolTip('No MIDI input device configured (see Config)')

    def _toggle_rec(self, checked):
        if checked:
            self._arm_recording()
        else:
            self._stop_recording()

    def _arm_recording(self):
        """Open the MIDI port and wait for the first note-on."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            self.rec_btn.setChecked(False)
            return

        device_name = getattr(getattr(self.app, 'settings', None), 'midi_input_device', '')
        if not device_name:
            self.rec_btn.setChecked(False)
            return

        try:
            import rtmidi
            midi_in = rtmidi.MidiIn()
            ports = midi_in.get_ports()
            if device_name not in ports:
                self.rec_btn.setChecked(False)
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, 'MIDI Error',
                    f'Device "{device_name}" not found.\nCheck Config for available devices.')
                return
            midi_in.open_port(ports.index(device_name))
            midi_in.set_callback(self._midi_callback)
            midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
            self._rec_midi_in = midi_in
        except Exception as e:
            self.rec_btn.setChecked(False)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, 'MIDI Error', f'Could not open MIDI port:\n{e}')
            return

        self._rec_notes = {}
        self._rec_events = []
        self._rec_start_time = None
        self._rec_armed = True
        self._rec_recording = False
        self.rec_btn.setText('■ Stop')
        self.rec_btn.setToolTip('Recording — click to stop')

    def _midi_callback(self, event, data=None):
        """Called from rtmidi thread on each incoming MIDI message."""
        msg, _delta_t = event
        if not msg:
            return
        status = msg[0] & 0xF0
        now = time.monotonic()

        if status == 0x90 and len(msg) >= 3 and msg[2] > 0:   # note-on
            pitch, vel = msg[1], msg[2]
            if self._rec_armed and not self._rec_recording:
                self._rec_start_time = now
                self._rec_armed = False
                self._rec_recording = True
            if self._rec_recording and self._rec_start_time is not None:
                beat = (now - self._rec_start_time) / (60.0 / self.state.bpm)
                self._rec_notes[pitch] = (beat, vel)

        elif status in (0x80,) or (status == 0x90 and len(msg) >= 3 and msg[2] == 0):
            pitch = msg[1]
            if pitch in self._rec_notes and self._rec_recording:
                start_beat, vel = self._rec_notes.pop(pitch)
                if self._rec_start_time is not None:
                    end_beat = (now - self._rec_start_time) / (60.0 / self.state.bpm)
                    duration = max(0.0625, end_beat - start_beat)
                    self._rec_events.append((start_beat, duration, pitch, vel))

    def _stop_recording(self):
        """Close MIDI port and commit recorded notes to the pattern."""
        self._rec_armed = False
        self._rec_recording = False

        # Close still-open notes at current wall time
        if self._rec_start_time is not None:
            now = time.monotonic()
            for pitch, (start_beat, vel) in list(self._rec_notes.items()):
                end_beat = (now - self._rec_start_time) / (60.0 / self.state.bpm)
                duration = max(0.0625, end_beat - start_beat)
                self._rec_events.append((start_beat, duration, pitch, vel))
        self._rec_notes = {}

        if self._rec_midi_in:
            try:
                self._rec_midi_in.close_port()
            except Exception:
                pass
            self._rec_midi_in = None

        self.rec_btn.setText('Rec')
        self.rec_btn.setChecked(False)
        self.rec_btn.setToolTip('Record from MIDI input (arm — starts on first note)')

        if not self._rec_events:
            return

        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return

        from ..state import Note

        # Shift so first note lands at beat 0
        t0 = min(s for s, d, p, v in self._rec_events)
        notes = []
        for start, dur, pitch, vel in self._rec_events:
            notes.append(Note(pitch=pitch, start=round(start - t0, 6),
                               duration=round(dur, 6), velocity=vel))

        # Expand pattern if needed, rounding up to a whole bar
        max_end = max(n.start + n.duration for n in notes)
        if max_end > pat.length:
            bar = self.state.ts_num
            pat.length = bar * (int(max_end / bar) + 1)

        pat.notes.extend(notes)
        self._rec_events = []
        self.state.notify('note_add')
        self.refresh()

    def keyPressEvent(self, event: QKeyEvent):
        """Handle keyboard shortcuts."""
        modifiers = event.modifiers()
        key = event.key()
        
        # Escape - clear selection or cancel ghost mode
        if key == Qt.Key_Escape:
            if self._ghost_notes:
                self._cancel_ghost_mode()
            else:
                self.clear_selection()
            return
        
        # Ctrl/Cmd + C - Copy
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_C:
            self._copy_to_clipboard()
            return
        
        # Ctrl/Cmd + X - Cut
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_X:
            self._cut_to_clipboard()
            return
        
        # Ctrl/Cmd + V - Paste
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_V:
            self._paste_from_clipboard()
            return
        
        # Ctrl/Cmd + D - Duplicate
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_D:
            self._duplicate_selection()
            return
        
        # Ctrl/Cmd + A - Select all
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_A:
            pat = self.state.find_pattern(self.state.sel_pat)
            if pat:
                self._selected = set(range(len(pat.notes)))
                self.refresh()
            return
        
        # Delete or Backspace - Delete selected
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            self._delete_selected()
            return
        
        # M - Merge selected notes
        if key == Qt.Key_M:
            self._merge_selected_notes()
            return
        
        super().keyPressEvent(event)


class PianoKeysWidget(QWidget):
    """Piano keyboard on left side."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_roll = parent

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            y = event.pos().y()
            pitch = self.parent_roll.HI - int(y / self.parent_roll.NH)
            if self.parent_roll.LO <= pitch <= self.parent_roll.HI:
                self.parent_roll.app.play_note(pitch, 100, track_id=self.parent_roll.state.sel_trk)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pat = self.parent_roll.state.find_pattern(self.parent_roll.state.sel_pat)
        in_key = scale_set(pat.key, pat.scale) if pat else set()

        for p in range(self.parent_roll.LO, self.parent_roll.HI + 1):
            y = (self.parent_roll.HI - p) * self.parent_roll.NH
            nm = NOTE_NAMES[p % 12]
            is_black = '#' in nm
            is_c = p % 12 == 0
            ik = (p % 12) in in_key
            oct = p // 12 - 1

            if is_black:
                bg = QColor('#2a1a50') if ik else QColor('#111')
            else:
                bg = QColor('#2e2450') if ik else QColor('#16213e')

            painter.fillRect(0, y, 44, self.parent_roll.NH, bg)
            painter.setPen(QColor('#1a1a2e'))
            painter.drawRect(0, y, 44, self.parent_roll.NH)

            if is_c:
                painter.setPen(QColor('#eee'))
                painter.setFont(QFont('TkDefaultFont', 6))
                painter.drawText(QRect(0, y, 40, self.parent_roll.NH),
                                Qt.AlignRight | Qt.AlignVCenter, f'C{oct}')
                painter.setPen(QColor('#533483'))
                painter.drawLine(0, y + self.parent_roll.NH, 44, y + self.parent_roll.NH)
            elif not is_black:
                painter.setPen(QColor('#888'))
                painter.setFont(QFont('TkDefaultFont', 5))
                painter.drawText(QRect(0, y, 40, self.parent_roll.NH),
                                Qt.AlignRight | Qt.AlignVCenter, f'{nm}{oct}')


class PianoGridWidget(QWidget):
    """Note grid for piano roll."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_roll = parent
        self._bg_note_fade = {}  # (pitch, pattern_id) -> fade_level (0.0-1.0)
        self._slice_hover_pos = None  # (note_idx, beat) for slice mode preview
        
        # Enable keyboard focus
        self.setFocusPolicy(Qt.StrongFocus)
        
        # Enable mouse tracking to receive move events without button press
        self.setMouseTracking(True)
    
    def keyPressEvent(self, event):
        """Forward keyboard events to parent roll."""
        self.parent_roll.keyPressEvent(event)

    def mousePressEvent(self, event):
        # Ensure grid widget has keyboard focus
        self.setFocus()
        
        if event.button() == Qt.LeftButton:
            self.parent_roll._on_click(event)
        elif event.button() == Qt.RightButton:
            self.parent_roll._on_right_click(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self.parent_roll._on_drag(event)
        else:
            # Update slice preview
            if self.parent_roll.state.tool == 'slice':
                x, y = event.pos().x(), event.pos().y()
                n, i, _ = self.parent_roll._hit_note(x, y)
                if n:
                    beat, _ = self.parent_roll._coords_to_beat_pitch(x, y)
                    self._slice_hover_pos = (i, beat)
                else:
                    self._slice_hover_pos = None
                self.update()
        
        # Always update ghost note position when ghost notes are active
        if self.parent_roll._ghost_notes:
            self.update()

    def mouseReleaseEvent(self, event):
        self.parent_roll._on_release(event)
    
    def leaveEvent(self, event):
        """Clear slice preview when mouse leaves widget."""
        if self._slice_hover_pos:
            self._slice_hover_pos = None
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pat = self.parent_roll.state.find_pattern(self.parent_roll.state.sel_pat)
        s = self.parent_roll.state
        
        pitch_range = self.parent_roll.HI - self.parent_roll.LO + 1
        total_h = pitch_range * self.parent_roll.NH
        beats = pat.length if pat else 16
        total_w = self.width()

        in_key = scale_set(pat.key, pat.scale) if pat else set()
        bpm_beats = s.ts_num * (4 / s.ts_den)

        # Row backgrounds
        for p in range(self.parent_roll.LO, self.parent_roll.HI + 1):
            y = (self.parent_roll.HI - p) * self.parent_roll.NH
            nm = NOTE_NAMES[p % 12]
            is_black = '#' in nm
            is_c = p % 12 == 0
            ik = (p % 12) in in_key
            
            if is_black:
                bg = QColor('#1e1a40') if ik else QColor('#15152a')
            else:
                bg = QColor('#252050') if ik else QColor('#1a1a30')
            
            painter.fillRect(0, y, total_w, self.parent_roll.NH, bg)
            
            line_color = QColor('#3a3a6a') if is_c else QColor('#222244')
            width = 1 if is_c else 0.5
            painter.setPen(QPen(line_color, width))
            painter.drawLine(0, y, total_w, y)

        # Beat lines
        total_subdivs = int(beats * 4)
        for b in range(total_subdivs + 1):
            x = b * self.parent_roll.BW / 4
            bn = b / 4
            is_measure = (abs(bn % bpm_beats) < 0.001) or (abs(bn % bpm_beats - bpm_beats) < 0.001)
            is_beat = b % 4 == 0
            
            if is_measure:
                color, width = QColor('#4a4a8a'), 1.5
            elif is_beat:
                color, width = QColor('#3a3a6a'), 1
            elif b % 2 == 0:
                color, width = QColor('#2a2a5a'), 0.5
            else:
                color, width = QColor('#222244'), 0.5
            
            painter.setPen(QPen(color, width))
            painter.drawLine(int(x), 0, int(x), total_h)

        if not pat:
            return

        # Background notes (other patterns) - overlay system
        bg_notes = []
        
        # Determine which other patterns are currently playing (playhead is
        # inside one of their placements on the arrangement timeline).
        playing_pattern_ids = set()
        if s.playing and s.playhead is not None:
            for pl in s.placements:
                other = s.find_pattern(pl.pattern_id)
                if other and other.id != pat.id:
                    pl_end = pl.time + other.length * (pl.repeats or 1)
                    if pl.time <= s.playhead < pl_end:
                        playing_pattern_ids.add(other.id)
        
        def find_smart_offset(current_pat_id, other_pat_id):
            """Find the offset to align first overlap in pattern-relative coordinates."""
            curr_pls = [pl for pl in s.placements if pl.pattern_id == current_pat_id]
            other_pls = [pl for pl in s.placements if pl.pattern_id == other_pat_id]
            
            if not curr_pls or not other_pls:
                return 0.0
            
            curr_pat = s.find_pattern(current_pat_id)
            other_pat = s.find_pattern(other_pat_id)
            if not curr_pat or not other_pat:
                return 0.0
            
            for curr_pl in sorted(curr_pls, key=lambda p: p.time):
                curr_reps = curr_pl.repeats or 1
                for curr_rep in range(curr_reps):
                    curr_arr_start = curr_pl.time + curr_rep * curr_pat.length
                    curr_arr_end = curr_arr_start + curr_pat.length
                    
                    for other_pl in sorted(other_pls, key=lambda p: p.time):
                        other_reps = other_pl.repeats or 1
                        for other_rep in range(other_reps):
                            other_arr_start = other_pl.time + other_rep * other_pat.length
                            other_arr_end = other_arr_start + other_pat.length
                            
                            if not (curr_arr_end <= other_arr_start or other_arr_end <= curr_arr_start):
                                return other_arr_start - curr_arr_start
            
            return 0.0
        
        for pl in s.placements:
            if pl.pattern_id == pat.id:
                continue
            other_pat = s.find_pattern(pl.pattern_id)
            if not other_pat:
                continue
            
            if other_pat.overlay_mode == 'off':
                continue
            
            # 'playing' mode only shows when pattern is actively playing
            if other_pat.overlay_mode == 'playing' and other_pat.id not in playing_pattern_ids:
                continue
            
            t = s.find_track(pl.track_id)
            if not t:
                continue
            
            transpose = s.compute_transpose(pl)
            pattern_offset = find_smart_offset(pat.id, other_pat.id)
            
            # 'always' draws at fixed alpha; 'playing' uses the fade system
            is_always = other_pat.overlay_mode == 'always'
            
            for n in other_pat.notes:
                bg_notes.append({
                    'pitch': n.pitch + transpose,
                    'start': n.start + pattern_offset,
                    'duration': n.duration,
                    'velocity': n.velocity,
                    'key': (n.pitch + transpose, other_pat.id),
                    'is_always': is_always,
                })
            
            # For 'playing' mode, mark these notes as active so they fade in
            if not is_always:
                for n in other_pat.notes:
                    key = (n.pitch + transpose, other_pat.id)
                    self._bg_note_fade[key] = 1.0
        
        # Decay fading notes
        keys_to_remove = []
        for key in self._bg_note_fade:
            self._bg_note_fade[key] = max(0.0, self._bg_note_fade[key] - 0.05)
            if self._bg_note_fade[key] <= 0:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self._bg_note_fade[key]
        
        for n in bg_notes:
            x = n['start'] * self.parent_roll.BW
            y = (self.parent_roll.HI - n['pitch']) * self.parent_roll.NH
            w = n['duration'] * self.parent_roll.BW
            
            if 0 <= n['pitch'] <= 127 and -w < x < total_w:
                if n['is_always']:
                    alpha = 40
                else:
                    fade = self._bg_note_fade.get(n['key'], 0.0)
                    if fade <= 0:
                        continue
                    alpha = int(40 * fade)
                
                painter.setPen(Qt.NoPen)
                color = QColor('#cccccc')
                color.setAlpha(alpha)
                painter.setBrush(color)
                painter.drawRect(int(x), y + 1, int(w - 1), self.parent_roll.NH - 2)
        
        if self._bg_note_fade:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(33, self.update)

        # Notes from current pattern
        for i, n in enumerate(pat.notes):
            x = n.start * self.parent_roll.BW
            y = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH
            w = n.duration * self.parent_roll.BW
            sel = i in self.parent_roll._selected
            color = QColor(vel_color(n.velocity))

            if sel:
                painter.setPen(QPen(QColor('#fff'), 2))
            else:
                painter.setPen(QPen(QColor(pat.color), 1))
            
            painter.setBrush(color)
            painter.drawRect(int(x), y + 1, int(w - 1), self.parent_roll.NH - 2)

            # Resize handle
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 51))
            painter.drawRect(int(x + w - 4), y + 1, 3, self.parent_roll.NH - 2)

            # Velocity text for selected notes
            if sel:
                painter.setPen(QColor('#fff'))
                painter.setFont(QFont('TkDefaultFont', 6))
                painter.drawText(int(x + 2), y + self.parent_roll.NH - 3, f'v{n.velocity}')

        # Bend curves — drawn on top of all notes
        _in_bend_mode = s.tool == 'bend'
        for i, n in enumerate(pat.notes):
            if not n.bend and not _in_bend_mode:
                continue

            note_x0 = n.start * self.parent_roll.BW
            note_y_center = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH + self.parent_roll.NH // 2

            if n.bend:
                # Build full point list with implicit zero at start/end
                pts = sorted(n.bend, key=lambda p: p[0])
                full = [[0.0, 0.0]] + [[p[0], p[1]] for p in pts] + [[n.duration, 0.0]]

                def _curve_y(beat_off):
                    tc = max(0.0, min(n.duration, beat_off))
                    seg = 0
                    for k in range(len(full) - 1):
                        if full[k][0] <= tc <= full[k+1][0]:
                            seg = k
                            break
                    t1, v1 = full[seg]
                    t2, v2 = full[min(len(full)-1, seg+1)]
                    v0 = full[max(0, seg-1)][1]
                    v3 = full[min(len(full)-1, seg+2)][1]
                    seg_len = t2 - t1
                    if seg_len > 1e-9:
                        lt = max(0.0, min(1.0, (tc - t1) / seg_len))
                        sem = 0.5 * ((2*v1) + (-v0+v2)*lt +
                                     (2*v0-5*v1+4*v2-v3)*lt*lt +
                                     (-v0+3*v1-3*v2+v3)*lt*lt*lt)
                    else:
                        sem = v2
                    return note_y_center - int(sem / 2.0 * self.parent_roll.NH * 2)

                # Draw smooth curve by sampling
                from PySide6.QtCore import QLineF
                curve_color = QColor('#00f5d4')
                curve_color.setAlpha(200)
                painter.setPen(QPen(curve_color, 1.5))
                steps = max(16, int(n.duration * 32))
                prev_cx = note_x0
                prev_cy = _curve_y(0.0)
                for s_idx in range(1, steps + 1):
                    t = s_idx / steps * n.duration
                    cx = note_x0 + t * self.parent_roll.BW
                    cy = _curve_y(t)
                    painter.drawLine(int(prev_cx), int(prev_cy), int(cx), int(cy))
                    prev_cx, prev_cy = cx, cy

                # Draw control point handles
                for pt_idx, (beat_off, semitones) in enumerate(n.bend):
                    px = int(note_x0 + beat_off * self.parent_roll.BW)
                    py = note_y_center - int(semitones / 2.0 * self.parent_roll.NH * 2)
                    is_dragging = (self.parent_roll._bend_drag_note is n and
                                   self.parent_roll._bend_drag_point_idx == pt_idx)
                    handle_color = QColor('#fee440') if is_dragging else QColor('#00f5d4')
                    painter.setPen(QPen(QColor('#000'), 1))
                    painter.setBrush(handle_color)
                    painter.drawEllipse(px - 4, py - 4, 8, 8)

            elif _in_bend_mode:
                # In bend mode, show a faint zero-line on notes with no bend yet
                painter.setPen(QPen(QColor(255, 255, 255, 40), 1, Qt.DashLine))
                nx0 = int(note_x0)
                nx1 = int(note_x0 + n.duration * self.parent_roll.BW)
                painter.drawLine(nx0, note_y_center, nx1, note_y_center)
        
        # Draw slice preview line
        if self._slice_hover_pos and s.tool == 'slice':
            note_idx, beat = self._slice_hover_pos
            if 0 <= note_idx < len(pat.notes):
                n = pat.notes[note_idx]
                x = beat * self.parent_roll.BW
                y = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH
                
                painter.setPen(QPen(QColor('#ff0000'), 2))
                painter.drawLine(int(x), y + 1, int(x), y + self.parent_roll.NH - 1)
        
        # Draw marquee selection rectangle
        if self.parent_roll._marquee_start:
            from PySide6.QtGui import QCursor
            cursor_pos = self.mapFromGlobal(QCursor.pos())
            start = self.parent_roll._marquee_start
            rect = QRectF(
                min(start.x(), cursor_pos.x()),
                min(start.y(), cursor_pos.y()),
                abs(cursor_pos.x() - start.x()),
                abs(cursor_pos.y() - start.y())
            )
            painter.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
            painter.setBrush(QColor(255, 255, 255, 30))
            painter.drawRect(rect)
        
        # Draw ghost notes (semi-transparent)
        if self.parent_roll._ghost_notes:
            from PySide6.QtGui import QCursor
            cursor_pos = self.mapFromGlobal(QCursor.pos())
            
            # Calculate offset from first note
            if self.parent_roll._ghost_notes:
                min_start = min(n.start for n in self.parent_roll._ghost_notes)
                min_pitch = min(n.pitch for n in self.parent_roll._ghost_notes)
                
                cursor_beat, cursor_pitch = self.parent_roll._coords_to_beat_pitch(
                    cursor_pos.x(), cursor_pos.y()
                )
                snapped_beat = self.parent_roll._snap(cursor_beat)
                
                beat_offset = snapped_beat - min_start
                pitch_offset = cursor_pitch - min_pitch
                
                for n in self.parent_roll._ghost_notes:
                    ghost_pitch = n.pitch + pitch_offset
                    ghost_start = n.start + beat_offset
                    
                    # Clamp to valid range
                    if ghost_pitch < self.parent_roll.LO or ghost_pitch > self.parent_roll.HI:
                        continue
                    
                    x = ghost_start * self.parent_roll.BW
                    y = (self.parent_roll.HI - ghost_pitch) * self.parent_roll.NH
                    w = n.duration * self.parent_roll.BW
                    
                    # Draw semi-transparent
                    color = QColor(vel_color(n.velocity))
                    color.setAlpha(128)
                    painter.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
                    painter.setBrush(color)
                    painter.drawRect(int(x), y + 1, int(w - 1), self.parent_roll.NH - 2)


class VelocityWidget(QWidget):
    """Velocity lane at bottom."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_roll = parent
        self._vel_dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._vel_dragging = True
            self._set_vel_at(event)

    def mouseMoveEvent(self, event):
        if self._vel_dragging:
            self._set_vel_at(event)

    def mouseReleaseEvent(self, event):
        self._vel_dragging = False

    def _set_vel_at(self, event):
        pat = self.parent_roll.state.find_pattern(self.parent_roll.state.sel_pat)
        if not pat:
            return
        
        x = event.pos().x()
        y = event.pos().y()
        vel = max(1, min(127, int((1 - y / 48) * 127)))
        
        # If notes are selected, update all of them
        if self.parent_roll._selected:
            for idx in self.parent_roll._selected:
                if 0 <= idx < len(pat.notes):
                    pat.notes[idx].velocity = vel
            self.parent_roll.vel_slider.setValue(vel)
            self.parent_roll.refresh()
            return
        
        # Otherwise, find nearest note
        beat = x / self.parent_roll.BW
        
        best = -1
        best_dist = float('inf')
        for i, n in enumerate(pat.notes):
            d = abs(beat - n.start)
            if d < best_dist and d < 0.5:
                best_dist = d
                best = i
        
        if best >= 0:
            pat.notes[best].velocity = vel
            self.parent_roll.refresh()

    def paintEvent(self, event):
        painter = QPainter(self)
        
        pat = self.parent_roll.state.find_pattern(self.parent_roll.state.sel_pat)
        beats = pat.length if pat else 16
        total_w = self.width()
        
        painter.fillRect(self.rect(), QColor('#12121f'))
        painter.setPen(QPen(QColor('#2a2a4a'), 0.5))
        painter.drawLine(0, 25, total_w, 25)

        if not pat:
            return

        bw = max(3, self.parent_roll.state.snap * self.parent_roll.BW * 0.6)
        for i, n in enumerate(pat.notes):
            x = n.start * self.parent_roll.BW + 2
            h = n.velocity / 127 * 46
            color = QColor('#fff') if i in self.parent_roll._selected else QColor(vel_color(n.velocity))
            
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRect(int(x), int(48 - h), int(bw), int(h))


# Event handlers for PianoRoll
def _on_click(self, event):
    """Handle left mouse button press."""
    pat = self.state.find_pattern(self.state.sel_pat)
    if not pat:
        return
    
    x, y = event.pos().x(), event.pos().y()
    beat, pitch = self._coords_to_beat_pitch(x, y)
    modifiers = event.modifiers()
    
    # Store initial click position for deadzone check
    self._drag_start_pos = QPoint(x, y)
    
    # If in ghost mode, commit the paste
    if self._ghost_notes:
        self._commit_ghost_notes(x, y)
        return
    
    if self.state.tool == 'edit':
        n, i, is_resize = self._hit_note(x, y)
        
        # Clear arranger selection when interacting with piano roll
        self.app.arrangement.selected_placements = []
        self.app.arrangement.selected_beat_placements = []
        
        # Shift modifier - marquee select or multi-select
        if modifiers & Qt.ShiftModifier:
            if n:
                # Multi-select toggle
                if i in self._selected:
                    self._selected.discard(i)
                else:
                    self._selected.add(i)
                self.refresh()
            else:
                # Start marquee selection
                self._marquee_start = QPoint(x, y)
        else:
            # Regular click
            if n and is_resize:
                # Resize existing note (but will check deadzone in drag)
                self._resize_note = n
            elif n:
                # Select and prepare to drag
                if i not in self._selected:
                    self._selected = {i}
                self._drag_note = n
                self._drag_offset_x = beat - n.start
                self.refresh()
            else:
                # Create new note
                self._selected.clear()
                vel = self.vel_slider.value()
                dur = self.state.snap
                
                if self.state.note_len == 'snap':
                    dur = self.state.snap
                elif self.state.note_len == 'last':
                    dur = self.state.last_note_len
                else:
                    text = self.state.note_len
                    try:
                        if '/' in text:
                            parts = text.split('/')
                            dur = float(parts[0]) / float(parts[1])
                        else:
                            dur = float(text)
                    except ValueError:
                        dur = self.state.snap
                
                snap_value = min(self.state.snap, dur)
                snap_beat = int(beat / snap_value) * snap_value
                
                nn = Note(pitch=pitch, start=snap_beat, duration=dur, velocity=vel)
                pat.notes.append(nn)
                self.state.last_note_len = dur
                self.app.play_note(pitch, vel, track_id=self.state.sel_trk)
                self.state.notify('note_add')
                self.refresh()
    
    elif self.state.tool == 'slice':
        n, i, _ = self._hit_note(x, y)
        if n:
            # Split the note at the current beat position
            if n.start < beat < n.start + n.duration:
                # Create new note for the right portion
                right_note = Note(
                    pitch=n.pitch,
                    start=beat,
                    duration=(n.start + n.duration) - beat,
                    velocity=n.velocity
                )
                # Shorten the left portion, strip its bend (can't cleanly split a curve)
                n.duration = beat - n.start
                n.bend = []
                # Add the right portion
                pat.notes.append(right_note)

                self.state.notify('note_edit')
                self.refresh()

    elif self.state.tool == 'bend':
        # Hit-test existing control points first
        bn, bi = self._hit_bend_point(x, y)
        if bn is not None:
            # Start dragging this control point
            self._bend_drag_note = bn
            self._bend_drag_point_idx = bi
        else:
            # Click on a note body — add new control point
            n, i, _ = self._hit_note(x, y)
            if n:
                beat_off = max(0.0, min(n.duration, beat - n.start))
                note_y_center = (self.HI - n.pitch) * self.NH + self.NH // 2
                semitones = max(-2.0, min(2.0, -(y - note_y_center) / (self.NH * 2.0) * 2.0))
                n.bend.append([beat_off, round(semitones, 3)])
                n.bend.sort(key=lambda p: p[0])
                # Start dragging the new point
                self._bend_drag_note = n
                self._bend_drag_point_idx = next(
                    k for k, p in enumerate(n.bend) if abs(p[0] - beat_off) < 1e-6)
                self.app.engine.mark_dirty()
                self.state.notify('note_edit')
                self.refresh()

def _on_drag(self, event):
    """Handle mouse drag."""
    pat = self.state.find_pattern(self.state.sel_pat)
    if not pat:
        return
    
    x, y = event.pos().x(), event.pos().y()
    beat, pitch = self._coords_to_beat_pitch(x, y)
    
    # Update ghost note preview
    if self._ghost_notes:
        self.grid_widget.update()
        return
    
    # Marquee selection
    if self._marquee_start:
        # Update marquee rectangle (drawing happens in paintEvent)
        self.grid_widget.update()
        return
    
    if self.state.tool == 'edit':
        # Check deadzone for resize operations (10 pixels)
        if self._resize_note and self._drag_start_pos:
            dist = (QPoint(x, y) - self._drag_start_pos).manhattanLength()
            if dist < 10:
                # Still in deadzone, don't resize yet
                return
            else:
                # Past deadzone, clear the start position so we don't check again
                self._drag_start_pos = None
        
        if self._resize_note:
            self._resize_note.duration = max(self.state.snap,
                                              self._snap(beat - self._resize_note.start))
            self.refresh()
        elif self._drag_note:
            # Check deadzone for drag operations
            if self._drag_start_pos:
                dist = (QPoint(x, y) - self._drag_start_pos).manhattanLength()
                if dist < 10:
                    # Still in deadzone, don't move yet
                    return
                else:
                    # Past deadzone
                    self._drag_start_pos = None
            
            # Calculate delta from the note we're dragging
            new_start = max(0, self._snap(beat - self._drag_offset_x))
            new_pitch = max(self.LO, min(self.HI, pitch))
            
            delta_start = new_start - self._drag_note.start
            delta_pitch = new_pitch - self._drag_note.pitch
            
            # Apply delta to all selected notes
            for idx in self._selected:
                if 0 <= idx < len(pat.notes):
                    pat.notes[idx].start = max(0, pat.notes[idx].start + delta_start)
                    pat.notes[idx].pitch = max(self.LO, min(self.HI, 
                                                            pat.notes[idx].pitch + delta_pitch))
            
            # Update the drag note position for next delta calculation
            self._drag_note.start = new_start
            self._drag_note.pitch = new_pitch

            self.refresh()

    elif self.state.tool == 'bend':
        if self._bend_drag_note is not None and self._bend_drag_point_idx is not None:
            n = self._bend_drag_note
            idx = self._bend_drag_point_idx
            if 0 <= idx < len(n.bend):
                new_beat_off = max(0.0, min(n.duration, beat - n.start))
                note_y_center = (self.HI - n.pitch) * self.NH + self.NH // 2
                new_semitones = max(-2.0, min(2.0, -(y - note_y_center) / (self.NH * 2.0) * 2.0))
                n.bend[idx] = [new_beat_off, round(new_semitones, 3)]
                n.bend.sort(key=lambda p: p[0])
                # Point may have shifted index after sort — find it by proximity
                self._bend_drag_point_idx = min(
                    range(len(n.bend)),
                    key=lambda k: abs(n.bend[k][0] - new_beat_off))
                self.app.engine.mark_dirty()
                self.grid_widget.update()


def _on_release(self, event):
    """Handle mouse button release."""
    # Finalize marquee selection
    if self._marquee_start:
        pat = self.state.find_pattern(self.state.sel_pat)
        if pat:
            from PySide6.QtGui import QCursor
            cursor_pos = self.grid_widget.mapFromGlobal(QCursor.pos())

            from ..ops.note_edit import marquee_select
            new_sel = marquee_select(
                pat,
                (self._marquee_start.x(), self._marquee_start.y()),
                (cursor_pos.x(), cursor_pos.y()),
                self.BW, self.NH, self.HI)
            self._selected |= new_sel

        self._marquee_start = None
        self.refresh()
        return

    if self._resize_note:
        self.state.last_note_len = self._resize_note.duration
    if self._resize_note or self._drag_note:
        self.state.notify('note_edit')

    self._drag_note = None
    self._resize_note = None
    self._drag_start_pos = None

    # Finalise bend drag
    if self._bend_drag_note is not None:
        self.app.engine.mark_dirty()
        self.state.notify('note_edit')
        self._bend_drag_note = None
        self._bend_drag_point_idx = None


def _on_right_click(self, event):
    """Handle right-click."""
    pat = self.state.find_pattern(self.state.sel_pat)
    if not pat:
        return

    x, y = event.pos().x(), event.pos().y()

    if self.state.tool == 'bend':
        # Delete bend control point under cursor
        bn, bi = self._hit_bend_point(x, y)
        if bn is not None and bi >= 0:
            bn.bend.pop(bi)
            self.app.engine.mark_dirty()
            self.state.notify('note_edit')
            self.refresh()
        return

    n, i, _ = self._hit_note(x, y)

    if n:
        from ..ops.note_edit import delete_note_at
        self._selected = delete_note_at(pat, i, self._selected)
        self.refresh()
        self.state.notify('note_edit')

# Attach methods
PianoRoll._on_click = _on_click
PianoRoll._on_drag = _on_drag
PianoRoll._on_release = _on_release
PianoRoll._on_right_click = _on_right_click
