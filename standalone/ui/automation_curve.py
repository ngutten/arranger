"""Automation curve editor - control automation on a time/value grid."""

from PySide6.QtWidgets import (QFrame, QWidget, QScrollArea, QLabel, QPushButton,
                                QComboBox, QDoubleSpinBox, QVBoxLayout, QHBoxLayout)
from PySide6.QtCore import Qt, QRect, QPoint, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont

from ..state import AutomationPoint
from ..core.curve_utils import interpolate_curve


class AutomationCurve(QFrame):
    """Automation curve editor displayed when an automation pattern is selected."""

    VH = 200   # vertical height for value axis
    BW = 80    # pixels per beat
    HANDLE_RADIUS = 5  # control point handle radius

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state

        # Interaction state
        self._drag_point_idx = None  # Index of point being dragged, or None
        self._drag_offset = (0, 0)   # Offset from point position to cursor

        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header bar
        hdr = QFrame()
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(8, 4, 8, 4)

        self.name_label = QLabel('No automation pattern')
        self.name_label.setFont(QFont('TkDefaultFont', 9))
        hdr_layout.addWidget(self.name_label)

        preview_btn = QPushButton('Preview')
        preview_btn.setToolTip('Preview automation pattern')
        preview_btn.clicked.connect(self._preview_pattern)
        hdr_layout.addWidget(preview_btn)

        hdr_layout.addStretch()

        # Min/Max value controls
        hdr_layout.addWidget(QLabel('Min'))
        self.min_spin = QDoubleSpinBox()
        self.min_spin.setRange(-1000000, 1000000)
        self.min_spin.setDecimals(3)
        self.min_spin.setSingleStep(0.1)
        self.min_spin.setValue(0.0)
        self.min_spin.setMaximumWidth(80)
        self.min_spin.valueChanged.connect(self._on_range_changed)
        hdr_layout.addWidget(self.min_spin)

        hdr_layout.addWidget(QLabel('Max'))
        self.max_spin = QDoubleSpinBox()
        self.max_spin.setRange(-1000000, 1000000)
        self.max_spin.setDecimals(3)
        self.max_spin.setSingleStep(0.1)
        self.max_spin.setValue(1.0)
        self.max_spin.setMaximumWidth(80)
        self.max_spin.valueChanged.connect(self._on_range_changed)
        hdr_layout.addWidget(self.max_spin)

        # Interpolation mode
        hdr_layout.addWidget(QLabel('Curve'))
        self.interp_combo = QComboBox()
        self.interp_combo.addItems(['Linear', 'Step', 'Smooth'])
        self.interp_combo.setMaximumWidth(100)
        self.interp_combo.currentTextChanged.connect(self._on_interp_changed)
        hdr_layout.addWidget(self.interp_combo)

        layout.addWidget(hdr)

        # Main area: value axis + curve canvas
        body = QFrame()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # Value axis labels
        self.axis_widget = ValueAxisWidget(self)
        self.axis_widget.setFixedWidth(50)
        body_layout.addWidget(self.axis_widget)

        # Curve scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.curve_widget = CurveWidget(self)
        self.scroll_area.setWidget(self.curve_widget)
        self.scroll_area.verticalScrollBar().valueChanged.connect(
            lambda v: self.axis_widget.update()
        )
        body_layout.addWidget(self.scroll_area)

        layout.addWidget(body)

    def refresh(self):
        """Redraw the automation curve editor."""
        pat = self.state.find_automation_pattern(self.state.sel_auto_pat)

        if pat:
            self.name_label.setText(f'{pat.name} ({pat.length}b)')
            self.min_spin.blockSignals(True)
            self.max_spin.blockSignals(True)
            self.min_spin.setValue(pat.min_value)
            self.max_spin.setValue(pat.max_value)
            self.min_spin.blockSignals(False)
            self.max_spin.blockSignals(False)
        else:
            self.name_label.setText('No automation pattern')

        self.axis_widget.update()
        self.curve_widget.update_size()
        self.curve_widget.update()

    def _on_range_changed(self):
        """User changed min/max value range.
        
        Points are stored normalized in [0,1], so changing the range just
        changes the output mapping and display labels - point positions stay fixed.
        
        The dispatcher will read these min/max values and apply the scaling
        before sending control values to the ControlSource plugin.
        """
        pat = self.state.find_automation_pattern(self.state.sel_auto_pat)
        if not pat:
            return

        min_val = self.min_spin.value()
        max_val = self.max_spin.value()

        # Ensure max > min
        if max_val <= min_val:
            max_val = min_val + 0.1
            self.max_spin.blockSignals(True)
            self.max_spin.setValue(max_val)
            self.max_spin.blockSignals(False)

        pat.min_value = min_val
        pat.max_value = max_val
        
        self.state.notify('automation_edit')
        self.refresh()

    def _on_interp_changed(self):
        """User changed interpolation mode - applies to all existing points."""
        pat = self.state.find_automation_pattern(self.state.sel_auto_pat)
        if not pat or not pat.points:
            return
        
        # Get the new interpolation mode
        interp_mode = self.interp_combo.currentText().lower()
        
        # Apply to all existing points
        for point in pat.points:
            point.curve = interp_mode
        
        self.state.notify('automation_edit')
        self.refresh()

    def _preview_pattern(self):
        """Preview the automation pattern by playing the full arrangement.

        Automation modulates graph node parameters and is only meaningful in
        the context of the full arrangement, so we trigger normal playback
        from the start of the pattern's first placement.
        """
        pat = self.state.find_automation_pattern(self.state.sel_auto_pat)
        if not pat:
            return

        # Find the earliest placement of this pattern and seek there
        earliest = None
        for ap in self.state.automation_placements:
            if ap.pattern_id == pat.id:
                if earliest is None or ap.time < earliest:
                    earliest = ap.time

        if earliest is not None and hasattr(self.app, 'engine') and self.app.engine:
            self.app.engine.seek(earliest)

        # Start playback so the user hears the automation in context
        if hasattr(self.app, '_on_play'):
            self.app._on_play()

    def _snap(self, beat):
        """Snap beat position to grid."""
        return round(beat / self.state.snap) * self.state.snap


class ValueAxisWidget(QWidget):
    """Widget for drawing value axis labels."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_curve = parent
        self.setMinimumHeight(200)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor('#16213e'))

        state = self.parent_curve.state
        pat = state.find_automation_pattern(state.sel_auto_pat)
        if not pat:
            return

        VH = self.parent_curve.VH
        min_val = pat.min_value
        max_val = pat.max_value
        value_range = max_val - min_val if max_val != min_val else 1.0

        # Draw value labels (5 labels from min to max)
        # These show the OUTPUT range - points are stored normalized [0,1]
        painter.setPen(QColor('#ccc'))
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)

        for i in range(5):
            frac = i / 4
            # Display value is in user's [min, max] range
            value = min_val + frac * value_range
            y = VH - int(frac * VH)

            # Grid line
            painter.setPen(QColor('#2a2a4a'))
            painter.drawLine(35, y, 50, y)

            # Label
            painter.setPen(QColor('#ccc'))
            label = f'{value:.2f}'
            painter.drawText(2, y + 4, label)


class CurveWidget(QWidget):
    """Widget for drawing and editing the automation curve."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_curve = parent
        self.setMouseTracking(True)
        self._hover_point_idx = None  # Point under cursor
        self.update_size()

    def update_size(self):
        state = self.parent_curve.state
        pat = state.find_automation_pattern(state.sel_auto_pat)

        if not pat:
            self.setMinimumSize(400, 200)
            return

        VH = self.parent_curve.VH
        BW = self.parent_curve.BW

        width = int(pat.length * BW)
        height = VH
        self.setMinimumSize(width, height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        state = self.parent_curve.state
        pat = state.find_automation_pattern(state.sel_auto_pat)

        # Background
        painter.fillRect(self.rect(), QColor('#1a1a30'))

        if not pat:
            return

        VH = self.parent_curve.VH
        BW = self.parent_curve.BW
        min_val = pat.min_value
        max_val = pat.max_value
        value_range = max_val - min_val if max_val != min_val else 1.0

        # Grid lines - vertical (beat lines)
        total_beats = pat.length
        bpm_beats = state.ts_num * (4 / state.ts_den)
        total_subdivs = int(total_beats * 4)

        for b in range(total_subdivs + 1):
            x = b * BW / 4
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
            painter.drawLine(int(x), 0, int(x), VH)

        # Grid lines - horizontal (value lines)
        for i in range(5):
            y = int(VH * i / 4)
            painter.setPen(QPen(QColor('#2a2a4a'), 0.5))
            painter.drawLine(0, y, self.width(), y)

        # Draw the automation curve
        if pat.points:
            # Points are stored normalized in [0,1]
            # We display them scaled to [min_value, max_value]
            curve_points = [(p.time, p.value, p.curve) for p in pat.points]
            
            # Helper to convert normalized value [0,1] to y coordinate
            def value_to_y(norm_value):
                # norm_value is in [0,1], clamp just in case
                clamped = max(0.0, min(1.0, norm_value))
                return int(VH - clamped * VH)

            # Draw smooth curve by sampling
            curve_color = QColor('#00f5d4')
            painter.setPen(QPen(curve_color, 2))
            
            steps = max(32, int(pat.length * 16))  # Higher density for smooth curves
            prev_x = 0
            # interpolate_curve returns normalized [0,1] value
            prev_y = value_to_y(interpolate_curve(curve_points, 0, pat.length, 0.0))
            
            for i in range(1, steps + 1):
                t = i / steps * pat.length
                # Get normalized value [0,1]
                norm_value = interpolate_curve(curve_points, t, pat.length, 0.0)
                x = int(t * BW)
                y = value_to_y(norm_value)
                painter.drawLine(prev_x, prev_y, x, y)
                prev_x, prev_y = x, y

            # Draw control point handles
            for idx, pt in enumerate(pat.points):
                px = int(pt.time * BW)
                py = value_to_y(pt.value)  # pt.value is already normalized [0,1]
                
                is_dragging = self.parent_curve._drag_point_idx == idx
                is_hover = self._hover_point_idx == idx
                
                if is_dragging:
                    handle_color = QColor('#fee440')
                    handle_size = 7
                elif is_hover:
                    handle_color = QColor('#6ab6ff')
                    handle_size = 6
                else:
                    handle_color = QColor('#00f5d4')
                    handle_size = 5
                
                painter.setPen(QPen(QColor('#000'), 1))
                painter.setBrush(handle_color)
                painter.drawEllipse(px - handle_size, py - handle_size, 
                                   handle_size * 2, handle_size * 2)

    def mousePressEvent(self, event):
        """Handle mouse press - start dragging or add point."""
        pat = self.parent_curve.state.find_automation_pattern(
            self.parent_curve.state.sel_auto_pat)
        if not pat:
            return

        x, y = event.pos().x(), event.pos().y()
        
        # Check if clicking on existing point
        clicked_idx = self._find_point_at(x, y)
        
        if event.button() == Qt.LeftButton:
            if clicked_idx is not None:
                # Start dragging existing point
                self.parent_curve._drag_point_idx = clicked_idx
                pt = pat.points[clicked_idx]
                BW = self.parent_curve.BW
                px = int(pt.time * BW)
                py = self._value_to_y(pt.value, pat)
                self.parent_curve._drag_offset = (x - px, y - py)
            else:
                # Add new point
                self._add_point_at(x, y)
        elif event.button() == Qt.RightButton:
            if clicked_idx is not None:
                # Delete point
                pat.points.pop(clicked_idx)
                self.parent_curve.state.notify('automation_edit')
                self.update()

    def mouseMoveEvent(self, event):
        """Handle mouse move - drag point or update hover."""
        pat = self.parent_curve.state.find_automation_pattern(
            self.parent_curve.state.sel_auto_pat)
        if not pat:
            return

        x, y = event.pos().x(), event.pos().y()

        if self.parent_curve._drag_point_idx is not None:
            # Dragging a point
            idx = self.parent_curve._drag_point_idx
            if 0 <= idx < len(pat.points):
                pt = pat.points[idx]
                
                # Calculate new position accounting for drag offset
                dx, dy = self.parent_curve._drag_offset
                new_x = x - dx
                new_y = y - dy
                
                # Convert to beat and normalized value
                BW = self.parent_curve.BW
                new_time = new_x / BW
                new_value = self._y_to_value(new_y, pat)
                
                # Snap time to grid
                new_time = self.parent_curve._snap(new_time)
                
                # Clamp to pattern bounds
                new_time = max(0, min(pat.length, new_time))
                new_value = max(0.0, min(1.0, new_value))  # Normalized range
                
                pt.time = new_time
                pt.value = new_value
                
                self.update()
        else:
            # Update hover state
            old_hover = self._hover_point_idx
            self._hover_point_idx = self._find_point_at(x, y)
            if old_hover != self._hover_point_idx:
                self.update()

    def mouseReleaseEvent(self, event):
        """Handle mouse release - end dragging."""
        if self.parent_curve._drag_point_idx is not None:
            self.parent_curve.state.notify('automation_edit')
            self.parent_curve._drag_point_idx = None
            self.update()

    def _find_point_at(self, x, y):
        """Find point index at screen coordinates, or None."""
        pat = self.parent_curve.state.find_automation_pattern(
            self.parent_curve.state.sel_auto_pat)
        if not pat:
            return None

        BW = self.parent_curve.BW
        threshold = 10  # pixels

        for idx, pt in enumerate(pat.points):
            px = int(pt.time * BW)
            py = self._value_to_y(pt.value, pat)
            dist = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
            if dist <= threshold:
                return idx

        return None

    def _add_point_at(self, x, y):
        """Add new automation point at screen coordinates."""
        pat = self.parent_curve.state.find_automation_pattern(
            self.parent_curve.state.sel_auto_pat)
        if not pat:
            return

        BW = self.parent_curve.BW
        new_time = x / BW
        new_value = self._y_to_value(y, pat)

        # Snap time to grid
        new_time = self.parent_curve._snap(new_time)

        # Clamp to bounds
        new_time = max(0, min(pat.length, new_time))
        new_value = max(0.0, min(1.0, new_value))  # Normalized range

        # Get interpolation mode from combo box
        interp_mode = self.parent_curve.interp_combo.currentText().lower()

        # Create new point with normalized value
        new_point = AutomationPoint(new_time, new_value, interp_mode)
        pat.points.append(new_point)

        self.parent_curve.state.notify('automation_edit')
        self.update()

    def _value_to_y(self, value, pat):
        """Convert normalized value [0,1] to y screen coordinate."""
        VH = self.parent_curve.VH
        # Value is already normalized in [0,1]
        clamped = max(0.0, min(1.0, value))
        return int(VH - clamped * VH)

    def _y_to_value(self, y, pat):
        """Convert y screen coordinate to normalized value [0,1]."""
        VH = self.parent_curve.VH
        frac = 1.0 - (y / VH)  # Invert y axis
        frac = max(0.0, min(1.0, frac))
        return frac  # Return normalized [0,1]
