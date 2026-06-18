"""Enhanced parameter widgets for node graph editor.

Provides smart parameter widgets based on value ranges:
- Small bounded ranges (≤10): Linear slider + value box
- Wide positive ranges (>100): Logarithmic slider + value box  
- Large/unbounded with negatives: Spinbox only

All widgets support animation when driven by Control streams.
"""

from __future__ import annotations
import math
from typing import Callable, Optional
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QDoubleSpinBox, QSlider, QLabel, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QRectF, QPoint
from PySide6.QtWidgets import QToolTip
from PySide6.QtGui import (QFontMetrics, QDoubleValidator, QPainter, QColor,
                           QPen, QBrush, QImage, QPolygonF)

def format_value(value, display_dec=10):
    if value == 0:
        return "0"
    
    # 1. Determine the magnitude to find the "100x smaller" threshold
    magnitude = math.floor(math.log10(abs(value)))
    
    # 2. Calculate required decimals (2 places below the leading digit)
    # We cap this at 'display_dec' so it doesn't exceed your spinbox limit
    keep_decimals = min(display_dec, max(0, 2 - magnitude))
    
    # 3. Format the string
    formatted = f"{value:.{keep_decimals}f}"
    
    # 4. Only strip zeros if there is a decimal point present
    if "." in formatted:
        # Strip trailing zeros, then strip a trailing decimal point if it's naked
        formatted = formatted.rstrip('0').rstrip('.')
        
    return formatted

class SmartFloatWidget(QWidget):
    """Float parameter widget with adaptive UI based on range.
    
    Chooses between:
    - Linear slider + spinbox (small bounded ranges)
    - Log slider + spinbox (wide positive ranges)
    - Spinbox only (unbounded or ranges including negatives outside log range)
    """
    
    valueChanged = Signal(float)
    
    def __init__(self, 
                 value: float,
                 min_val: float, 
                 max_val: float,
                 label: str = "",
                 parent: QWidget = None):
        super().__init__(parent)
        
        self.min_val = min_val
        self.max_val = max_val
        self._current_value = value
        self._is_driven = False  # Set to True when driven by Control stream
        
        # Determine widget type based on range
        span = max_val - min_val
        self._use_log_slider = False
        self._use_slider = False
        
        # Wide positive range → logarithmic slider
        if min_val > 0 and max_val > 50 and (max_val / min_val) > 100:
            self._use_log_slider = True
            self._use_slider = True
        # Small bounded range → linear slider
        elif span <= 10 and abs(min_val) <= 10 and abs(max_val) <= 10:
            self._use_slider = True
            
        self._build_ui(value, min_val, max_val, label)
        
    def _build_ui(self, value: float, min_val: float, max_val: float, label: str):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        
        # Adaptive spinbox decimals and step
        span = max_val - min_val if max_val != min_val else 1.0
        if span <= 0.1:
            step, dec = 0.001, 4
        elif span <= 2.0:
            step, dec = 0.01, 3
        elif span <= 20.0:
            step, dec = 0.1, 2
        elif span <= 200.0:
            step, dec = 1.0, 1
        else:
            step, dec = 10.0, 0
        
        # Store display decimals for formatting
        self._display_decimals = dec
            
        # Create spinbox
        self._spinbox = QDoubleSpinBox()
        self._spinbox.setRange(min_val, max_val)
        self._spinbox.setSingleStep(step)
        # Set a high decimal count to accept whatever user types
        # The actual display will be determined by what they enter
        self._spinbox.setDecimals(10)  # Allow up to 10 decimals internally
        self._spinbox.setValue(value)
        self._spinbox.setStyleSheet(
            "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;"
        )
        
        # Allow flexible text entry
        self._spinbox.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
        # Keep the spinbox compact so the adaptive slider gets the rest of the
        # row — otherwise in a narrow (two-column) node the slider is left with
        # almost no travel.
        self._spinbox.setMinimumWidth(54)
        self._spinbox.setMaximumWidth(74)
        self._spinbox.setKeyboardTracking(False)
        
        # Override textFromValue to limit display decimals while keeping full precision
        original_textFromValue = self._spinbox.textFromValue
        display_dec = self._display_decimals
            
        def custom_textFromValue(value):
            # Format with limited decimals for display, stripping trailing zeros
            formatted = format_value(value) #f"{value:.{display_dec}f}".rstrip('0').rstrip('.')
            return formatted
        
        self._spinbox.textFromValue = custom_textFromValue
        
        # The validator just checks range, not decimal precision
        line_edit = self._spinbox.lineEdit()
        if line_edit:
            validator = QDoubleValidator(min_val, max_val, 10, line_edit)
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            line_edit.setValidator(validator)
            line_edit.setMaxLength(32767)
        
        if self._use_slider:
            # Create slider with darker track
            self._slider = QSlider(Qt.Horizontal)
            self._slider.setStyleSheet("""
                QSlider::groove:horizontal {
                    background: #0d1117;
                    height: 4px;
                    border-radius: 2px;
                }
                QSlider::handle:horizontal {
                    background: #4d96ff;
                    width: 12px;
                    height: 12px;
                    margin: -4px 0;
                    border-radius: 6px;
                }
                QSlider::handle:horizontal:hover {
                    background: #6ab6ff;
                }
            """)
            
            # Set slider resolution (1000 steps for good precision)
            self._slider_steps = 1000
            self._slider.setRange(0, self._slider_steps)
            self._slider.setValue(self._value_to_slider(value))
            
            # Connect signals
            self._slider.valueChanged.connect(self._on_slider_changed)
            self._spinbox.valueChanged.connect(self._on_spinbox_changed)
            
            layout.addWidget(self._slider, 1)  # Slider gets stretch
            layout.addWidget(self._spinbox)
        else:
            # No slider - just spinbox
            self._slider = None
            self._spinbox.valueChanged.connect(self._on_spinbox_changed)
            layout.addWidget(self._spinbox)
            
    def _value_to_slider(self, value: float) -> int:
        """Convert real value to slider position (0 to _slider_steps)."""
        if self._use_log_slider:
            # Logarithmic mapping
            if value <= 0:
                return 0
            log_min = math.log(self.min_val)
            log_max = math.log(self.max_val)
            log_val = math.log(value)
            frac = (log_val - log_min) / (log_max - log_min)
            return int(frac * self._slider_steps)
        else:
            # Linear mapping
            frac = (value - self.min_val) / (self.max_val - self.min_val)
            return int(frac * self._slider_steps)
            
    def _slider_to_value(self, slider_pos: int) -> float:
        """Convert slider position to real value."""
        frac = slider_pos / self._slider_steps
        
        if self._use_log_slider:
            # Logarithmic mapping
            log_min = math.log(self.min_val)
            log_max = math.log(self.max_val)
            log_val = log_min + frac * (log_max - log_min)
            return math.exp(log_val)
        else:
            # Linear mapping
            return self.min_val + frac * (self.max_val - self.min_val)
            
    def _on_slider_changed(self, slider_pos: int):
        """Handle slider movement - update spinbox."""
        if self._is_driven:
            return  # Ignore user input when driven by control stream
            
        value = self._slider_to_value(slider_pos)
        self._spinbox.blockSignals(True)
        self._spinbox.setValue(value)
        self._spinbox.blockSignals(False)
        self._current_value = value
        self.valueChanged.emit(value)
        
    def _on_spinbox_changed(self, value: float):
        """Handle spinbox edit - update slider."""
        if self._is_driven:
            return  # Ignore user input when driven by control stream
            
        self._current_value = value
        if self._slider:
            self._slider.blockSignals(True)
            self._slider.setValue(self._value_to_slider(value))
            self._slider.blockSignals(False)
        self.valueChanged.emit(value)
        
    def value(self) -> float:
        """Get current value."""
        return self._current_value
        
    def setValue(self, value: float):
        """Set value programmatically.
        
        Args:
            value: New value to set
        """
        self._current_value = value
        
        # Update both widgets
        self._spinbox.blockSignals(True)
        self._spinbox.setValue(value)
        self._spinbox.blockSignals(False)
        
        if self._slider:
            self._slider.blockSignals(True)
            self._slider.setValue(self._value_to_slider(value))
            self._slider.blockSignals(False)
        
    def setDriven(self, driven: bool):
        """Mark widget as driven by external control stream.
        
        When driven:
        - User input is disabled
        - Visual styling changes to indicate driven state (green tint)
        """
        self._is_driven = driven
        
        # Update styling to indicate driven state
        if driven:
            self._spinbox.setStyleSheet(
                "background: #0d2a1a; color: #6bcb77; border: 1px solid #2a4a2a;"
            )
            if self._slider:
                self._slider.setStyleSheet("""
                    QSlider::groove:horizontal {
                        background: #0d1a0d;
                        height: 4px;
                        border-radius: 2px;
                    }
                    QSlider::handle:horizontal {
                        background: #6bcb77;
                        width: 12px;
                        height: 12px;
                        margin: -4px 0;
                        border-radius: 6px;
                    }
                """)
        else:
            self._spinbox.setStyleSheet(
                "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;"
            )
            if self._slider:
                self._slider.setStyleSheet("""
                    QSlider::groove:horizontal {
                        background: #0d1117;
                        height: 4px;
                        border-radius: 2px;
                    }
                    QSlider::handle:horizontal {
                        background: #4d96ff;
                        width: 12px;
                        height: 12px;
                        margin: -4px 0;
                        border-radius: 6px;
                    }
                    QSlider::handle:horizontal:hover {
                        background: #6ab6ff;
                    }
                """)
                
        # Enable/disable user interaction
        self._spinbox.setReadOnly(driven)
        if self._slider:
            self._slider.setEnabled(not driven)
            
    def setEnabled(self, enabled: bool):
        """Override setEnabled to handle both widgets."""
        super().setEnabled(enabled)
        self._spinbox.setEnabled(enabled)
        if self._slider:
            self._slider.setEnabled(enabled)
            
    def setStyleSheet(self, stylesheet: str):
        """Override setStyleSheet to apply to spinbox."""
        if hasattr(self, '_spinbox'):
            self._spinbox.setStyleSheet(stylesheet)


def create_float_widget(value: float, 
                        min_val: float, 
                        max_val: float,
                        label: str = "",
                        parent: QWidget = None) -> SmartFloatWidget:
    """Factory function to create appropriate float parameter widget.
    
    Args:
        value: Current value
        min_val: Minimum allowed value
        max_val: Maximum allowed value
        label: Optional label text
        parent: Parent widget
        
    Returns:
        SmartFloatWidget instance with appropriate slider configuration
    """
    return SmartFloatWidget(value, min_val, max_val, label, parent)


class AutomationTrackSelectorWidget(QWidget):
    """Widget for selecting an automation track from available tracks.
    
    Used in ControlSource node settings to choose which automation track
    to read control values from.
    """
    
    valueChanged = Signal(int)  # Emits automation track ID
    
    def __init__(self, current_track_id: int, state, parent: QWidget = None):
        super().__init__(parent)
        self.state = state
        self._current_track_id = current_track_id
        
        from PySide6.QtWidgets import QComboBox
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        
        # Dropdown for automation tracks
        self.combo = QComboBox()
        self.combo.setStyleSheet(
            "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;"
        )
        self.combo.setMinimumWidth(150)
        
        # Populate with automation tracks
        self._populate_tracks()
        
        # Connect signal
        self.combo.currentIndexChanged.connect(self._on_selection_changed)
        
        layout.addWidget(self.combo)
        layout.addStretch()
    
    def _populate_tracks(self):
        """Populate combo box with available automation tracks."""
        self.combo.blockSignals(True)
        self.combo.clear()
        
        # Add "None" option
        self.combo.addItem('(No automation track)', 0)
        
        # Add all automation tracks
        for track in self.state.automation_tracks:
            self.combo.addItem(track.name, track.id)
        
        # Select current track
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == self._current_track_id:
                self.combo.setCurrentIndex(i)
                break
        
        self.combo.blockSignals(False)
    
    def _on_selection_changed(self, index):
        """Handle track selection change."""
        track_id = self.combo.itemData(index)
        if track_id is not None:
            self._current_track_id = track_id
            self.valueChanged.emit(track_id)
    
    def value(self) -> int:
        """Get current automation track ID."""
        return self._current_track_id
    
    def setValue(self, track_id: int):
        """Set current automation track ID."""
        self._current_track_id = track_id
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == track_id:
                self.combo.blockSignals(True)
                self.combo.setCurrentIndex(i)
                self.combo.blockSignals(False)
                break
    
    def refresh(self):
        """Refresh the list of automation tracks (call when tracks are added/removed)."""
        self._populate_tracks()


def create_config_param_widget(param_id: str, param_value, state, parent: QWidget = None):
    """Factory function to create the appropriate widget for a config parameter.
    
    Special handling for known parameter types:
    - automation_track_id: AutomationTrackSelectorWidget (dropdown of tracks by name)
    - _pattern_id: Pattern selector (if implemented)
    
    Args:
        param_id: Parameter identifier
        param_value: Current value (as float or int)
        state: AppState (for looking up tracks, patterns, etc.)
        parent: Parent widget
        
    Returns:
        Widget with valueChanged signal that emits the new value
    """
    from PySide6.QtWidgets import QSpinBox
    
    # Special case: automation track selection
    if param_id == 'automation_track_id':
        track_id = int(param_value) if param_value else 0
        return AutomationTrackSelectorWidget(track_id, state, parent)
    
    # Default: integer spinbox for integer config params
    # (You can extend this for other types as needed)
    widget = QSpinBox(parent)
    widget.setRange(0, 999999)
    widget.setValue(int(param_value) if param_value else 0)
    widget.setStyleSheet(
        "background: #0d1117; color: #ccc; border: 1px solid #2a3a5c;"
    )
    return widget


def _density_color(v: float) -> QColor:
    """Map a 0..1 density value onto a dark→teal→amber heatmap ramp."""
    v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
    # two-segment ramp: navy → teal (0..0.5) → amber (0.5..1)
    if v < 0.5:
        t = v / 0.5
        r = int(13 + t * (45 - 13))
        g = int(17 + t * (160 - 17))
        b = int(40 + t * (150 - 40))
    else:
        t = (v - 0.5) / 0.5
        r = int(45 + t * (240 - 45))
        g = int(160 + t * (200 - 160))
        b = int(150 + t * (70 - 150))
    return QColor(r, g, b)


def _clip_color(clip: str) -> QColor:
    """Stable hue per clip name, for colour-coding scatter points."""
    h = 0
    for ch in clip:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return QColor.fromHsv(h % 360, 150, 235)


class XYPadWidget(QWidget):
    """2D pad for a pair of bounded params (e.g. a 2D latent style space).

    Drag the puck to set both axes at once — far better than two sliders for
    sweeping a latent live. Emits xChanged/yChanged with mapped values. The Y
    axis is drawn with its maximum at the top (screen-natural).

    For DDSP latent pads, an optional ``latent`` config block overlays the
    distribution of the underlying training data so the user can aim at the
    populated timbre hotspots rather than the empty regions the (nonlinear)
    map extrapolates into:

      * ``density`` — precomputed smoothed heatmap, drawn directly;
      * ``cloud``   — raw per-chunk points, drawn as a scatter + hover tooltip;
      * ``anchors`` — named cluster presets, drawn as labelled dots you can
        click to snap the puck onto;
      * ``hull``    — cloud boundary; the cursor is clamped/snapped to it
        (the map produces garbage outside the populated region).

    All ``latent`` coordinates are in *pad-coordinate units* (the ``extent``
    range). The widget's own value space is the port range (normalised
    ``[-1,1]`` for DDSP), and the C++ side maps that onto ``extent`` with a
    piecewise-linear ``map_pad`` (``0`` stays at the mean). We invert exactly
    that map here so the overlay and the puck stay registered.
    """

    xChanged = Signal(float)
    yChanged = Signal(float)

    def __init__(self, x_min, x_max, y_min, y_max, x_val, y_val,
                 x_label="X", y_label="Y", parent=None, latent=None):
        super().__init__(parent)
        self._xmin, self._xmax = float(x_min), float(x_max)
        self._ymin, self._ymax = float(y_min), float(y_max)
        if self._xmax == self._xmin:
            self._xmax = self._xmin + 1.0
        if self._ymax == self._ymin:
            self._ymax = self._ymin + 1.0
        self._x, self._y = float(x_val), float(y_val)
        self._x_label, self._y_label = x_label, y_label
        self._driven = False
        self._pad = 6
        # Latent overlay (all in widget value units after normalisation).
        self._anchors = []     # [(name, vx, vy)]
        self._cloud = []       # [(vx, vy, clip, t0, t1)]
        self._hull = []        # [(vx, vy)] polygon
        self._density = None   # (QImage, vx0, vy0, vx1, vy1)
        self._hover_idx = -1
        self._ingest_latent(latent)
        self.setMinimumSize(96, 96)
        self.setMaximumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.CrossCursor)
        if self._cloud:
            self.setMouseTracking(True)

    # -- latent ingest / coordinate mapping -------------------------------
    def _ingest_latent(self, latent):
        """Normalise a config ``latent`` block into widget value units."""
        if not isinstance(latent, dict):
            return
        ext = latent.get("extent") or {}
        ex = ext.get("x") if isinstance(ext, dict) else None
        ey = ext.get("y") if isinstance(ext, dict) else None
        # Per-axis (lo,hi) in pad coords; fall back to the widget range so a
        # block lacking extent is treated as already-normalised.
        xlo, xhi = (float(ex[0]), float(ex[1])) if ex else (self._xmin, self._xmax)
        ylo, yhi = (float(ey[0]), float(ey[1])) if ey else (self._ymin, self._ymax)

        def nx(c):
            return self._norm(float(c), xlo, xhi)

        def ny(c):
            return self._norm(float(c), ylo, yhi)

        anchors = latent.get("anchors")
        if isinstance(anchors, dict):
            for name, xy in anchors.items():
                if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                    self._anchors.append((str(name), nx(xy[0]), ny(xy[1])))

        cloud = latent.get("cloud")
        if isinstance(cloud, list):
            for pt in cloud:
                if not isinstance(pt, dict):
                    continue
                xy = pt.get("xy")
                if not (isinstance(xy, (list, tuple)) and len(xy) >= 2):
                    continue
                t = pt.get("t") or [0.0, 0.0]
                t0 = float(t[0]) if len(t) > 0 else 0.0
                t1 = float(t[1]) if len(t) > 1 else t0
                self._cloud.append((nx(xy[0]), ny(xy[1]),
                                    str(pt.get("clip", "")), t0, t1))

        hull = latent.get("hull")
        if isinstance(hull, list):
            for xy in hull:
                if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                    self._hull.append((nx(xy[0]), ny(xy[1])))

        dens = latent.get("density")
        if isinstance(dens, dict):
            self._build_density(dens, nx, ny)

    def _build_density(self, dens, nx, ny):
        bbox = dens.get("bbox")
        w = int(dens.get("w", 0))
        h = int(dens.get("h", 0))
        vals = dens.get("values")
        if not (bbox and w > 0 and h > 0 and isinstance(vals, list)
                and len(vals) >= w * h):
            return
        img = QImage(w, h, QImage.Format_ARGB32)
        for j in range(h):
            row = j * w
            for i in range(w):
                v = float(vals[row + i])
                c = _density_color(v)
                a = int(220 * (0.0 if v < 0.0 else (1.0 if v > 1.0 else v)))
                img.setPixelColor(i, j, QColor(c.red(), c.green(), c.blue(), a))
        # bbox is [xmin,ymin,xmax,ymax] in pad coords, row-major from ymin up.
        vx0, vx1 = nx(bbox[0]), nx(bbox[2])
        vy0, vy1 = ny(bbox[1]), ny(bbox[3])
        self._density = (img, vx0, vy0, vx1, vy1)

    @staticmethod
    def _norm(c, lo, hi):
        """Inverse of the C++ ``map_pad``: pad coord → normalised value.

        ``map_pad`` is piecewise-linear with ``0→0``: ``n≥0 → n*hi``,
        ``n<0 → n*(-lo)``. Inverting keeps the overlay registered with the puck.
        """
        if c >= 0.0:
            return c / hi if hi > 1e-9 else 0.0
        return c / (-lo) if lo < -1e-9 else 0.0

    # -- value <-> pixel mapping ------------------------------------------
    def _inner(self):
        return self.rect().adjusted(self._pad, self._pad, -self._pad, -self._pad)

    def _vxy_to_px(self, vx, vy):
        r = self._inner()
        fx = (vx - self._xmin) / (self._xmax - self._xmin)
        fy = (vy - self._ymin) / (self._ymax - self._ymin)
        return (r.left() + fx * r.width(), r.bottom() - fy * r.height())

    def _val_to_px(self):
        return self._vxy_to_px(self._x, self._y)

    def _px_to_val(self, px, py):
        r = self._inner()
        fx = min(1.0, max(0.0, (px - r.left()) / max(1, r.width())))
        fy = min(1.0, max(0.0, (r.bottom() - py) / max(1, r.height())))
        return (self._xmin + fx * (self._xmax - self._xmin),
                self._ymin + fy * (self._ymax - self._ymin))

    # -- public API -------------------------------------------------------
    def set_values(self, x, y):
        self._x, self._y = float(x), float(y)
        self.update()

    def setDriven(self, driven: bool):
        self._driven = bool(driven)
        self.setEnabled(not self._driven)
        self.update()

    # -- interaction ------------------------------------------------------
    def mousePressEvent(self, e):
        if not self._driven:
            self._apply(e)

    def mouseMoveEvent(self, e):
        if self._driven:
            return
        if e.buttons() & Qt.LeftButton:
            self._apply(e)
        elif self._cloud:
            self._update_hover(e.position().x(), e.position().y())

    def leaveEvent(self, e):
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.update()
        super().leaveEvent(e)

    def _apply(self, e):
        x, y = self._px_to_val(e.position().x(), e.position().y())
        # Snap to a named anchor preset when clicking near its dot.
        snap = self._anchor_near(e.position().x(), e.position().y())
        if snap is not None:
            x, y = snap
        elif self._hull:
            x, y = self._clamp_to_hull(x, y)
        cx, cy = (x != self._x), (y != self._y)
        self._x, self._y = x, y
        self.update()
        if cx:
            self.xChanged.emit(x)
        if cy:
            self.yChanged.emit(y)

    def _anchor_near(self, px, py, radius=7.0):
        best, bd = None, radius * radius
        for _name, vx, vy in self._anchors:
            ax, ay = self._vxy_to_px(vx, vy)
            d = (ax - px) ** 2 + (ay - py) ** 2
            if d <= bd:
                best, bd = (vx, vy), d
        return best

    def _update_hover(self, px, py, radius=6.0):
        best, bd = -1, radius * radius
        for i, (vx, vy, _clip, _t0, _t1) in enumerate(self._cloud):
            cx, cy = self._vxy_to_px(vx, vy)
            d = (cx - px) ** 2 + (cy - py) ** 2
            if d <= bd:
                best, bd = i, d
        if best != self._hover_idx:
            self._hover_idx = best
            if best >= 0:
                _vx, _vy, clip, t0, t1 = self._cloud[best]
                label = clip or "clip"
                QToolTip.showText(self.mapToGlobal(QPoint(int(px), int(py))),
                                  f"{label}  {t0:.1f}–{t1:.1f}s", self)
            else:
                QToolTip.hideText()
            self.update()

    # -- hull clamp -------------------------------------------------------
    def _clamp_to_hull(self, x, y):
        pts = self._hull
        if len(pts) < 3 or self._point_in_poly(x, y, pts):
            return (x, y)
        # Project onto the nearest hull edge.
        best, bd = (x, y), None
        n = len(pts)
        for i in range(n):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            qx, qy = self._closest_on_seg(x, y, ax, ay, bx, by)
            d = (qx - x) ** 2 + (qy - y) ** 2
            if bd is None or d < bd:
                best, bd = (qx, qy), d
        return best

    @staticmethod
    def _point_in_poly(x, y, pts):
        inside = False
        n = len(pts)
        j = n - 1
        for i in range(n):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if ((yi > y) != (yj > y)) and \
               (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _closest_on_seg(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        if denom < 1e-12:
            return (ax, ay)
        t = ((px - ax) * dx + (py - ay) * dy) / denom
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        return (ax + t * dx, ay + t * dy)

    # -- paint ------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._inner()
        p.fillRect(self.rect(), QColor("#0d1117"))

        # density heatmap (drawn first, under everything)
        if self._density is not None:
            img, vx0, vy0, vx1, vy1 = self._density
            x0, y0 = self._vxy_to_px(vx0, vy0)
            x1, y1 = self._vxy_to_px(vx1, vy1)
            p.setClipRect(r)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(QRectF(min(x0, x1), min(y0, y1),
                               abs(x1 - x0), abs(y1 - y0)), img)
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.setClipping(False)

        # cloud scatter
        if self._cloud:
            for i, (vx, vy, clip, _t0, _t1) in enumerate(self._cloud):
                cx, cy = self._vxy_to_px(vx, vy)
                c = _clip_color(clip)
                hot = (i == self._hover_idx)
                c.setAlpha(235 if hot else 90)
                p.setBrush(QBrush(c))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(cx, cy), 2.4 if hot else 1.6,
                              2.4 if hot else 1.6)

        p.setPen(QPen(QColor("#2a3a5c"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r)

        # hull outline
        if len(self._hull) >= 3:
            poly = QPolygonF([QPointF(*self._vxy_to_px(vx, vy))
                              for vx, vy in self._hull])
            p.setPen(QPen(QColor("#3d5a80"), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(poly)

        # centre crosshair (the mean / (0,0))
        p.setPen(QPen(QColor("#1d2942"), 1))
        cx, cy = r.left() + r.width() / 2, r.top() + r.height() / 2
        p.drawLine(int(cx), r.top(), int(cx), r.bottom())
        p.drawLine(r.left(), int(cy), r.right(), int(cy))

        # axis labels
        f = p.font()
        f.setPointSize(7)
        p.setFont(f)
        p.setPen(QColor("#6b7790"))
        p.drawText(r.adjusted(3, 0, 0, -1), Qt.AlignBottom | Qt.AlignLeft,
                   self._x_label)
        p.drawText(r.adjusted(0, 1, -3, 0), Qt.AlignTop | Qt.AlignRight,
                   self._y_label)

        # anchor presets. A small named set (new models) is labelled; a large
        # legacy per-clip set is left as bare snap dots to avoid label clutter.
        label_anchors = len(self._anchors) <= 8
        for name, vx, vy in self._anchors:
            ax, ay = self._vxy_to_px(vx, vy)
            p.setBrush(QBrush(QColor("#f0a050")))
            p.setPen(QPen(QColor("#1a1f29"), 1))
            p.drawEllipse(QPointF(ax, ay), 3, 3)
            if label_anchors:
                p.setPen(QColor("#d7c4a0"))
                p.drawText(QPointF(ax + 4, ay - 3), name)

        # puck
        px, py = self._val_to_px()
        puck = QColor("#5a6680") if self._driven else QColor("#4db6ac")
        p.setBrush(QBrush(puck))
        p.setPen(QPen(QColor("#e6edf6"), 1.5))
        p.drawEllipse(QPointF(px, py), 6, 6)
