"""Piano roll editor - note editing on a pitch/time grid."""

from PySide6.QtWidgets import (QFrame, QWidget, QScrollArea, QLabel, QPushButton,
                                QComboBox, QSlider, QVBoxLayout, QHBoxLayout)
from PySide6.QtCore import Qt, QRect, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont

from ..state import NOTE_NAMES, scale_set, vel_color, Note


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
        self._resize_note = None
        self._selected = set()

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

        # Tool buttons
        self.draw_btn = QPushButton('Draw')
        self.draw_btn.setCheckable(True)
        self.draw_btn.clicked.connect(lambda: self._set_tool('draw'))
        hdr_layout.addWidget(self.draw_btn)

        self.sel_btn = QPushButton('Sel')
        self.sel_btn.setCheckable(True)
        self.sel_btn.clicked.connect(lambda: self._set_tool('select'))
        hdr_layout.addWidget(self.sel_btn)

        self.del_btn = QPushButton('Del')
        self.del_btn.setCheckable(True)
        self.del_btn.clicked.connect(lambda: self._set_tool('erase'))
        hdr_layout.addWidget(self.del_btn)

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

    def _on_note_len(self, text):
        self.state.note_len = text

    def _set_tool(self, tool):
        self.state.tool = tool
        self._update_tool_buttons()

    def _update_tool_buttons(self):
        self.draw_btn.setChecked(self.state.tool == 'draw')
        self.sel_btn.setChecked(self.state.tool == 'select')
        self.del_btn.setChecked(self.state.tool == 'erase')

    def _on_vel_change(self, value):
        self.vel_label.setText(str(value))
        self.state.default_vel = value

    def _snap(self, beat):
        return int(beat / self.state.snap) * self.state.snap

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
                bg = QColor('#1a1530') if ik else QColor('#111')
            else:
                bg = QColor('#1e1a35') if ik else QColor('#16213e')

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

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.parent_roll._on_click(event)
        elif event.button() == Qt.RightButton:
            self.parent_roll._on_right_click(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self.parent_roll._on_drag(event)

    def mouseReleaseEvent(self, event):
        self.parent_roll._on_release(event)

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
                bg = QColor('#1a1530') if ik else QColor('#15152a')
            else:
                bg = QColor('#1e1a35') if ik else QColor('#1a1a30')
            
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

        # Notes
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


# Add event handlers to PianoRoll
def _on_click(self, event):
    pat = self.state.find_pattern(self.state.sel_pat)
    if not pat:
        return
    
    x, y = event.pos().x(), event.pos().y()
    pitch = self.HI - int(y / self.NH)
    beat = x / self.BW

    if self.state.tool == 'draw':
        n, i, is_resize = self._hit_note(x, y)
        if n and is_resize:
            self._resize_note = n
        elif n:
            self._drag_note = n
            self._drag_offset_x = beat - n.start
        else:
            # Create new note
            vel = self.vel_slider.value()
            dur = self.state.snap
            if self.state.note_len == 'snap':
                dur = self.state.snap
            elif self.state.note_len == 'last':
                dur = self.state.last_note_len
            else:
                # Parse fraction format
                text = self.state.note_len
                try:
                    if '/' in text:
                        parts = text.split('/')
                        dur = float(parts[0]) / float(parts[1])
                    else:
                        dur = float(text)
                except ValueError:
                    dur = self.state.snap
            
            # Use the smaller of snap and note duration for snapping (item 7)
            snap_value = min(self.state.snap, dur)
            snap_beat = int(beat / snap_value) * snap_value
            
            nn = Note(pitch=pitch, start=snap_beat, duration=dur, velocity=vel)
            pat.notes.append(nn)
            # Don't set _resize_note - prevents changing length on initial click (item 8)
            self.state.last_note_len = dur
            self.app.play_note(pitch, vel, track_id=self.state.sel_trk)
            self.refresh()
    elif self.state.tool == 'erase':
        n, i, _ = self._hit_note(x, y)
        if n:
            pat.notes.pop(i)
            self._selected.discard(i)
            self.refresh()
    elif self.state.tool == 'select':
        n, i, is_resize = self._hit_note(x, y)
        if n:
            if event.modifiers() & Qt.ShiftModifier:
                if i in self._selected:
                    self._selected.discard(i)
                else:
                    self._selected.add(i)
            else:
                self._selected = {i}
            if is_resize:
                self._resize_note = n
            else:
                self._drag_note = n
                self._drag_offset_x = beat - n.start
        else:
            self._selected.clear()
        self.refresh()

def _on_drag(self, event):
    pat = self.state.find_pattern(self.state.sel_pat)
    if not pat:
        return
    
    x, y = event.pos().x(), event.pos().y()
    pitch = self.HI - int(y / self.NH)
    beat = x / self.BW

    if self._resize_note:
        self._resize_note.duration = max(self.state.snap,
                                          self._snap(beat - self._resize_note.start))
        self.refresh()
    elif self._drag_note:
        self._drag_note.start = max(0, self._snap(beat - self._drag_offset_x))
        self._drag_note.pitch = max(self.LO, min(self.HI, pitch))
        self.refresh()

def _on_release(self, event):
    if self._resize_note:
        self.state.last_note_len = self._resize_note.duration
    if self._resize_note or self._drag_note:
        self.state.notify('note_edit')
    self._drag_note = None
    self._resize_note = None

def _on_right_click(self, event):
    """Handle right-click to delete notes."""
    pat = self.state.find_pattern(self.state.sel_pat)
    if not pat:
        return
    
    x, y = event.pos().x(), event.pos().y()
    n, i, _ = self._hit_note(x, y)
    if n:
        pat.notes.pop(i)
        self._selected.discard(i)
        self.refresh()
        self.state.notify('note_edit')

# Attach methods
PianoRoll._on_click = _on_click
PianoRoll._on_drag = _on_drag
PianoRoll._on_release = _on_release
PianoRoll._on_right_click = _on_right_click
