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
    QWidget, QHBoxLayout, QDoubleSpinBox, QSlider, QLabel
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QDoubleValidator

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
        self._spinbox.setMinimumWidth(60)
        self._spinbox.setMaximumWidth(100)
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
