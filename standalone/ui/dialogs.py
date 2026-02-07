"""Modal dialogs for the standalone arranger."""

from PySide6.QtWidgets import (QDialog, QLabel, QLineEdit, QSpinBox, QComboBox, 
                                QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout)
from PySide6.QtCore import Qt

from ..state import NOTE_NAMES, SCALES, PALETTE


class PatternDialog(QDialog):
    """Dialog for creating or editing a melodic pattern."""

    def __init__(self, parent, app, pattern_id=None):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self.pattern_id = pattern_id
        self.result = None

        pat = self.state.find_pattern(pattern_id) if pattern_id else None
        self.setWindowTitle('Edit Pattern' if pat else 'New Pattern')
        self.setFixedSize(320, 260)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Name
        form_layout = QFormLayout()
        self.name_edit = QLineEdit(pat.name if pat else f'Pattern {len(self.state.patterns) + 1}')
        form_layout.addRow('Name:', self.name_edit)

        # Length
        self.len_spin = QSpinBox()
        self.len_spin.setRange(1, 128)
        self.len_spin.setValue(int(pat.length) if pat else self.state.ts_num)
        form_layout.addRow('Length (beats):', self.len_spin)

        layout.addLayout(form_layout)

        # Key and Scale
        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel('Key:'))
        self.key_combo = QComboBox()
        self.key_combo.addItems(NOTE_NAMES)
        self.key_combo.setCurrentText(pat.key if pat else 'C')
        key_layout.addWidget(self.key_combo)

        key_layout.addSpacing(8)
        key_layout.addWidget(QLabel('Scale:'))
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(list(SCALES.keys()))
        self.scale_combo.setCurrentText(pat.scale if pat else 'major')
        key_layout.addWidget(self.scale_combo)
        key_layout.addStretch()

        layout.addLayout(key_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        ok_btn = QPushButton('OK')
        ok_btn.clicked.connect(self._ok)
        ok_btn.setDefault(True)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)

        layout.addStretch()
        layout.addLayout(btn_layout)

        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _ok(self):
        from ..state import Pattern

        name = self.name_edit.text() or 'Pattern'
        length = max(1, self.len_spin.value())
        key = self.key_combo.currentText()
        scale = self.scale_combo.currentText()

        if self.pattern_id:
            pat = self.state.find_pattern(self.pattern_id)
            if pat:
                pat.name = name
                pat.length = length
                pat.key = key
                pat.scale = scale
        else:
            pat = Pattern(
                id=self.state.new_id(), 
                name=name, 
                length=length,
                notes=[], 
                color=PALETTE[len(self.state.patterns) % len(PALETTE)],
                key=key, 
                scale=scale,
            )
            self.state.patterns.append(pat)
            self.state.sel_pat = pat.id
            self.state.sel_beat_pat = None

        self.state.notify('pattern_dialog')
        self.accept()


class BeatPatternDialog(QDialog):
    """Dialog for creating or editing a beat pattern."""

    def __init__(self, parent, app, pattern_id=None):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self.pattern_id = pattern_id

        pat = self.state.find_beat_pattern(pattern_id) if pattern_id else None
        self.setWindowTitle('Edit Beat Pattern' if pat else 'New Beat Pattern')
        self.setFixedSize(320, 220)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Name
        form_layout = QFormLayout()
        self.name_edit = QLineEdit(pat.name if pat else f'Beat {len(self.state.beat_patterns) + 1}')
        form_layout.addRow('Name:', self.name_edit)

        # Length
        self.len_spin = QSpinBox()
        self.len_spin.setRange(1, 128)
        self.len_spin.setValue(int(pat.length) if pat else self.state.ts_num)
        form_layout.addRow('Length (beats):', self.len_spin)

        # Subdivision
        self.subdiv_combo = QComboBox()
        self.subdiv_combo.addItems(['2', '3', '4', '6'])
        self.subdiv_combo.setCurrentText(str(pat.subdivision) if pat else '4')
        form_layout.addRow('Subdivision:', self.subdiv_combo)

        layout.addLayout(form_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        ok_btn = QPushButton('OK')
        ok_btn.clicked.connect(self._ok)
        ok_btn.setDefault(True)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)

        layout.addStretch()
        layout.addLayout(btn_layout)

        self.name_edit.setFocus()

    def _ok(self):
        from ..state import BeatPattern

        name = self.name_edit.text() or 'Beat'
        length = max(1, self.len_spin.value())
        try:
            subdiv = int(self.subdiv_combo.currentText())
        except ValueError:
            subdiv = 4

        if self.pattern_id:
            pat = self.state.find_beat_pattern(self.pattern_id)
            if pat:
                old_len = int(pat.length * pat.subdivision)
                new_len = length * subdiv
                pat.name = name
                pat.length = length
                pat.subdivision = subdiv
                if old_len != new_len:
                    for inst in self.state.beat_kit:
                        old_grid = pat.grid.get(inst.id, [])
                        new_grid = [0] * new_len
                        for i in range(min(len(old_grid), new_len)):
                            new_grid[i] = old_grid[i]
                        pat.grid[inst.id] = new_grid
        else:
            grid = {}
            for inst in self.state.beat_kit:
                grid[inst.id] = [0] * (length * subdiv)
            pat = BeatPattern(
                id=self.state.new_id(), 
                name=name, 
                length=length,
                subdivision=subdiv,
                color=PALETTE[len(self.state.beat_patterns) % len(PALETTE)],
                grid=grid,
            )
            self.state.beat_patterns.append(pat)
            self.state.sel_beat_pat = pat.id
            self.state.sel_pat = None

        self.state.notify('beat_pattern_dialog')
        self.accept()


class SF2Dialog(QDialog):
    """Dialog for loading a SoundFont file."""

    def __init__(self, parent, app, sf2_list):
        super().__init__(parent)
        self.app = app
        self.sf2_list = sf2_list
        self.result = None

        self.setWindowTitle('Load SoundFont')
        self.setFixedSize(360, 180)
        self.setModal(True)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel('Select .sf2 file from instruments/ directory:'))

        self.sf2_combo = QComboBox()
        if sf2_list:
            names = [sf2.name for sf2 in sf2_list]
            self.sf2_combo.addItems(names)
        else:
            self.sf2_combo.addItem('No .sf2 files found')
        layout.addWidget(self.sf2_combo)

        info_label = QLabel('Place .sf2 files in the instruments/ directory')
        info_label.setStyleSheet('font-size: 8pt;')
        layout.addWidget(info_label)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        load_btn = QPushButton('Load')
        load_btn.clicked.connect(self._load)
        load_btn.setDefault(True)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(load_btn)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _load(self):
        if not self.sf2_list:
            self.reject()
            return
        name = self.sf2_combo.currentText()
        for sf2 in self.sf2_list:
            if sf2.name == name:
                self.result = sf2
                break
        self.accept()
