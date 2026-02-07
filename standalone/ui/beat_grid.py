"""Beat grid editor - drum pattern editing on a step sequencer grid."""

from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget
from PySide6.QtCore import Qt, QRect, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QFont

from ..state import PALETTE, vel_color


class BeatGrid(QFrame):
    """Beat grid editor displayed when a beat pattern is selected."""

    RH = 28    # row height
    CW = 24    # cell width

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        hdr = QFrame()
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(8, 4, 8, 4)

        self.name_label = QLabel('No beat pattern')
        font = QFont()
        font.setPointSize(9)
        self.name_label.setFont(font)
        hdr_layout.addWidget(self.name_label)

        preview_btn = QPushButton('Preview')
        preview_btn.clicked.connect(self.app.preview_beat_pattern)
        hdr_layout.addWidget(preview_btn)
        hdr_layout.addStretch()

        layout.addWidget(hdr)

        # Main area: lane labels + grid
        body = QFrame()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # Lane labels canvas
        self.lane_widget = LaneWidget(self)
        self.lane_widget.setFixedWidth(70)
        body_layout.addWidget(self.lane_widget)

        # Grid scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.grid_widget = GridWidget(self)
        self.scroll_area.setWidget(self.grid_widget)
        self.scroll_area.verticalScrollBar().valueChanged.connect(
            lambda v: self.lane_widget.scroll_to(v)
        )
        body_layout.addWidget(self.scroll_area)

        layout.addWidget(body)

    def refresh(self):
        """Redraw the beat grid."""
        pat = self.state.find_beat_pattern(self.state.sel_beat_pat)

        if pat:
            self.name_label.setText(f'{pat.name} ({pat.length}b, /{pat.subdivision})')
        else:
            self.name_label.setText('No beat pattern')

        self.lane_widget.update()
        self.grid_widget.update_size()
        self.grid_widget.update()


class LaneWidget(QWidget):
    """Widget for drawing lane labels."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_grid = parent
        self.scroll_offset = 0
        self.setMinimumHeight(200)

    def scroll_to(self, value):
        self.scroll_offset = value
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor('#16213e'))

        state = self.parent_grid.state
        RH = self.parent_grid.RH

        for i, inst in enumerate(state.beat_kit):
            y = i * RH - self.scroll_offset
            if y + RH < 0 or y > self.height():
                continue

            # Row background
            painter.setPen(QColor('#222244'))
            painter.setBrush(QColor('#16213e'))
            painter.drawRect(0, y, 70, RH)

            # Color dot
            color = QColor(PALETTE[i % len(PALETTE)])
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(6, y + RH // 2 - 4, 8, 8)

            # Name
            painter.setPen(QColor('#eee'))
            font = QFont()
            font.setPointSize(7)
            painter.setFont(font)
            painter.drawText(18, y + RH // 2 + 4, inst.name)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            y = event.pos().y() + self.scroll_offset
            row = int(y // self.parent_grid.RH)
            state = self.parent_grid.state
            if 0 <= row < len(state.beat_kit):
                self.parent_grid.app.play_beat_hit(state.beat_kit[row].id)


class GridWidget(QWidget):
    """Widget for drawing the beat grid."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_grid = parent
        self.setMouseTracking(False)
        self.update_size()

    def update_size(self):
        state = self.parent_grid.state
        pat = state.find_beat_pattern(state.sel_beat_pat)
        
        if not pat:
            self.setMinimumSize(400, 200)
            return

        num_rows = len(state.beat_kit)
        num_cols = int(pat.length * pat.subdivision)
        RH = self.parent_grid.RH
        CW = self.parent_grid.CW

        width = num_cols * CW
        height = num_rows * RH
        self.setMinimumSize(width, height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        state = self.parent_grid.state
        pat = state.find_beat_pattern(state.sel_beat_pat)

        # Background
        painter.fillRect(self.rect(), QColor('#1a1a30'))

        if not pat:
            return

        num_rows = len(state.beat_kit)
        num_cols = int(pat.length * pat.subdivision)
        RH = self.parent_grid.RH
        CW = self.parent_grid.CW
        bpm_beats = state.ts_num * (4 / state.ts_den)

        # Row backgrounds
        for i in range(num_rows):
            y = i * RH
            bg = QColor('#181828' if i % 2 else '#1a1a30')
            painter.fillRect(0, y, self.width(), RH, bg)
            painter.setPen(QColor('#222244'))
            painter.drawLine(0, y + RH, self.width(), y + RH)

        # Column lines
        for col in range(num_cols + 1):
            x = col * CW
            beat_num = col / pat.subdivision
            is_measure = abs(beat_num % bpm_beats) < 0.001 or beat_num == 0
            is_beat = col % pat.subdivision == 0

            if is_measure:
                color, width = QColor('#4a4a8a'), 1.5
            elif is_beat:
                color, width = QColor('#3a3a6a'), 1
            else:
                color, width = QColor('#2a2a4a'), 0.5
            
            pen = QPen(color)
            pen.setWidthF(width)
            painter.setPen(pen)
            painter.drawLine(x, 0, x, num_rows * RH)

        # Grid cells
        for row, inst in enumerate(state.beat_kit):
            grid = pat.grid.get(inst.id)
            if not grid:
                continue

            y = row * RH
            color = QColor(PALETTE[row % len(PALETTE)])

            for col, vel in enumerate(grid):
                if vel > 0:
                    x = col * CW
                    vc = QColor(vel_color(vel))
                    
                    painter.setPen(color)
                    painter.setBrush(vc)
                    painter.drawRect(x + 1, y + 2, CW - 2, RH - 4)
                    
                    # Show velocity if cell is wide enough
                    if CW >= 20 and vel >= 10:
                        painter.setPen(QColor('#fff'))
                        font = QFont()
                        font.setPointSize(6)
                        painter.setFont(font)
                        painter.drawText(x + 4, y + RH - 6, str(vel))

    def mousePressEvent(self, event):
        state = self.parent_grid.state
        pat = state.find_beat_pattern(state.sel_beat_pat)
        
        if not pat or not state.beat_kit:
            return

        RH = self.parent_grid.RH
        CW = self.parent_grid.CW
        
        x, y = event.pos().x(), event.pos().y()
        row = int(y // RH)
        col = int(x // CW)

        if row < 0 or row >= len(state.beat_kit):
            return

        num_cols = int(pat.length * pat.subdivision)
        if col < 0 or col >= num_cols:
            return

        inst = state.beat_kit[row]
        grid = pat.grid.get(inst.id)
        if grid is None:
            # Initialize grid for this instrument if it doesn't exist yet
            num_steps = int(pat.length * pat.subdivision)
            pat.grid[inst.id] = [0] * num_steps
            grid = pat.grid[inst.id]

        if event.button() == Qt.LeftButton:
            vel = state.default_vel
            if grid[col] > 0:
                grid[col] = 0
            else:
                grid[col] = vel
                self.parent_grid.app.play_beat_hit(inst.id)
        elif event.button() == Qt.RightButton:
            grid[col] = 0

        self.parent_grid.state.notify('beat_grid_edit')
        self.update()
