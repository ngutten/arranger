"""Dialogs for automation pattern and track management."""

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                                QLineEdit, QPushButton, QDoubleSpinBox, QComboBox,
                                QColorDialog, QMessageBox)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from ..state import AutomationPattern, AutomationTrack, PALETTE


class AutomationPatternDialog(QDialog):
    """Dialog for creating/editing automation patterns."""

    def __init__(self, parent, state, pattern=None):
        super().__init__(parent)
        self.state = state
        self.pattern = pattern  # None = create new, otherwise edit existing
        self.result_pattern = None

        self.setWindowTitle('Edit Automation Pattern' if pattern else 'New Automation Pattern')
        self.setModal(True)
        self._build()
        
        if pattern:
            self._load_pattern(pattern)

    def _build(self):
        layout = QVBoxLayout(self)

        # Name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel('Name:'))
        self.name_edit = QLineEdit()
        self.name_edit.setText('Automation 1')
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)

        # Length
        length_layout = QHBoxLayout()
        length_layout.addWidget(QLabel('Length (beats):'))
        self.length_spin = QDoubleSpinBox()
        self.length_spin.setRange(0.25, 256)
        self.length_spin.setDecimals(2)
        self.length_spin.setSingleStep(0.25)
        self.length_spin.setValue(4.0)
        length_layout.addWidget(self.length_spin)
        layout.addLayout(length_layout)

        # Min value
        min_layout = QHBoxLayout()
        min_layout.addWidget(QLabel('Min Value:'))
        self.min_spin = QDoubleSpinBox()
        self.min_spin.setRange(-1000000, 1000000)
        self.min_spin.setDecimals(3)
        self.min_spin.setSingleStep(0.1)
        self.min_spin.setValue(0.0)
        min_layout.addWidget(self.min_spin)
        layout.addLayout(min_layout)

        # Max value
        max_layout = QHBoxLayout()
        max_layout.addWidget(QLabel('Max Value:'))
        self.max_spin = QDoubleSpinBox()
        self.max_spin.setRange(-1000000, 1000000)
        self.max_spin.setDecimals(3)
        self.max_spin.setSingleStep(0.1)
        self.max_spin.setValue(1.0)
        max_layout.addWidget(self.max_spin)
        layout.addLayout(max_layout)

        # Color
        color_layout = QHBoxLayout()
        color_layout.addWidget(QLabel('Color:'))
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(50, 25)
        self.color = '#4a90e2'
        self._update_color_button()
        self.color_btn.clicked.connect(self._pick_color)
        color_layout.addWidget(self.color_btn)
        color_layout.addStretch()
        layout.addLayout(color_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        ok_btn = QPushButton('OK')
        ok_btn.clicked.connect(self._on_ok)
        ok_btn.setDefault(True)
        btn_layout.addWidget(ok_btn)
        
        layout.addLayout(btn_layout)

    def _load_pattern(self, pattern):
        """Load existing pattern data into fields."""
        self.name_edit.setText(pattern.name)
        self.length_spin.setValue(pattern.length)
        self.min_spin.setValue(pattern.min_value)
        self.max_spin.setValue(pattern.max_value)
        self.color = pattern.color
        self._update_color_button()

    def _update_color_button(self):
        """Update color button background."""
        self.color_btn.setStyleSheet(f'background-color: {self.color};')

    def _pick_color(self):
        """Open color picker dialog."""
        color = QColorDialog.getColor(QColor(self.color), self, 'Pick Color')
        if color.isValid():
            self.color = color.name()
            self._update_color_button()

    def _on_ok(self):
        """Validate and accept."""
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, 'Invalid Name', 'Pattern name cannot be empty.')
            return

        length = self.length_spin.value()
        min_val = self.min_spin.value()
        max_val = self.max_spin.value()

        if max_val <= min_val:
            QMessageBox.warning(self, 'Invalid Range', 'Max value must be greater than min value.')
            return

        if self.pattern:
            # Editing existing
            self.pattern.name = name
            self.pattern.length = length
            self.pattern.min_value = min_val
            self.pattern.max_value = max_val
            self.pattern.color = self.color
            self.result_pattern = self.pattern
        else:
            # Creating new
            pat_id = self.state.new_id()
            self.result_pattern = AutomationPattern(
                id=pat_id,
                name=name,
                length=length,
                min_value=min_val,
                max_value=max_val,
                color=self.color,
                points=[]
            )

        self.accept()


class AutomationTrackDialog(QDialog):
    """Dialog for creating/editing automation tracks."""

    def __init__(self, parent, state, graph_model=None, track=None):
        super().__init__(parent)
        self.state = state
        self.track = track  # None = create new, otherwise edit existing
        self.result_track = None

        self.setWindowTitle('Edit Automation Track' if track else 'New Automation Track')
        self.setModal(True)
        self._build()
        
        if track:
            self._load_track(track)

    def _build(self):
        layout = QVBoxLayout(self)

        # Name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel('Name:'))
        self.name_edit = QLineEdit()
        self.name_edit.setText('Automation Track 1')
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)

        # Target
        target_layout = QHBoxLayout()
        target_layout.addWidget(QLabel('Target:'))
        self.target_combo = QComboBox()
        self.target_combo.addItems(['None', 'Tempo'])
        target_layout.addWidget(self.target_combo)
        layout.addLayout(target_layout)

        # Info label
        info_label = QLabel(
            'Create an automation track, then create a Control Source node\n'
            'in the Graph Editor and select this track from its settings.\n'
            'Set Target to "Tempo" to use this track as a BPM automation lane.'
        )
        info_label.setStyleSheet('color: #888; font-size: 10px;')
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton('OK')
        ok_btn.clicked.connect(self._on_ok)
        ok_btn.setDefault(True)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

    def _load_track(self, track):
        """Load existing track data into fields."""
        self.name_edit.setText(track.name)
        if track.target == 'tempo':
            self.target_combo.setCurrentText('Tempo')
        else:
            self.target_combo.setCurrentText('None')

    def _on_ok(self):
        """Validate and accept."""
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, 'Invalid Name', 'Track name cannot be empty.')
            return

        target_text = self.target_combo.currentText()
        target = 'tempo' if target_text == 'Tempo' else None

        # Validate: only one tempo track allowed
        if target == 'tempo':
            existing = self.state.find_tempo_track()
            if existing and (not self.track or existing.id != self.track.id):
                QMessageBox.warning(self, 'Duplicate Tempo Track',
                    'A tempo automation track already exists. '
                    'Only one track can target tempo.')
                return

        if self.track:
            # Editing existing
            self.track.name = name
            self.track.target = target
            self.result_track = self.track
        else:
            # Creating new
            track_id = self.state.new_id()
            self.result_track = AutomationTrack(
                id=track_id,
                name=name,
                target=target,
            )

        self.accept()
