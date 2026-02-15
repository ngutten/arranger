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


class ConfigDialog(QDialog):
    """Application configuration dialog.

    Covers: MIDI input device selection, default soundfont, audio backend
    selection, and read-only display of audio settings.  Changes are saved on OK.
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.settings = app.settings
        self.setWindowTitle('Configuration')
        self.setFixedSize(440, 300)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        # ---- MIDI input device ----
        self.midi_combo = QComboBox()
        self._midi_ports = []
        self._populate_midi_ports()
        form.addRow('MIDI Input:', self.midi_combo)

        # ---- Default SF2 ----
        sf2_row = QHBoxLayout()
        self.sf2_label = QLabel(self._short_path(self.settings.sf2_path) or '(none)')
        self.sf2_label.setStyleSheet('color: #aaa;')
        sf2_row.addWidget(self.sf2_label, 1)
        browse_btn = QPushButton('Browse…')
        browse_btn.setMaximumWidth(70)
        browse_btn.clicked.connect(self._browse_sf2)
        sf2_row.addWidget(browse_btn)
        clear_sf2_btn = QPushButton('Clear')
        clear_sf2_btn.setMaximumWidth(50)
        clear_sf2_btn.clicked.connect(self._clear_sf2)
        sf2_row.addWidget(clear_sf2_btn)
        form.addRow('Default SF2:', sf2_row)
        self._sf2_path = self.settings.sf2_path

        # ---- Audio backend ----
        self.backend_combo = QComboBox()
        self.backend_combo.addItem('Built-in C++ (recommended)', 'binding')
        self.backend_combo.addItem('FluidSynth (Python fallback)', 'fluidsynth')
        for i in range(self.backend_combo.count()):
            if self.backend_combo.itemData(i) == self.settings.audio_backend:
                self.backend_combo.setCurrentIndex(i)
                break
        form.addRow('Audio Backend:', self.backend_combo)

        # ---- Audio info (read-only) ----
        audio_info = QLabel(
            f'{self.settings.sample_rate} Hz  ·  block {self.settings.block_size}'
        )
        audio_info.setStyleSheet('color: #888; font-size: 8pt;')
        form.addRow('Audio:', audio_info)

        layout.addLayout(form)

        note = QLabel(
            'Audio settings (sample rate, block size) are set in\n'
            '~/.config/sequencer/settings.json and take effect on restart.\n'
            'Backend changes take effect immediately.'
        )
        note.setStyleSheet('color: #666; font-size: 8pt;')
        layout.addWidget(note)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        ok_btn = QPushButton('OK')
        ok_btn.clicked.connect(self._ok)
        ok_btn.setDefault(True)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _populate_midi_ports(self):
        """Enumerate rtmidi input ports; fall back gracefully if unavailable."""
        self.midi_combo.clear()
        self.midi_combo.addItem('(none)')
        self._midi_ports = []
        try:
            import rtmidi
            midi_in = rtmidi.MidiIn()
            ports = midi_in.get_ports()
            self._midi_ports = ports
            for name in ports:
                self.midi_combo.addItem(name)
            # Restore saved selection
            saved = self.settings.midi_input_device
            if saved in ports:
                self.midi_combo.setCurrentText(saved)
        except Exception:
            self.midi_combo.addItem('(rtmidi not installed)')
            self.midi_combo.setEnabled(False)

    def _browse_sf2(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select SoundFont', '', 'SoundFont files (*.sf2);;All files (*.*)')
        if path:
            self._sf2_path = path
            self.sf2_label.setText(self._short_path(path))

    def _clear_sf2(self):
        self._sf2_path = ''
        self.sf2_label.setText('(none)')

    @staticmethod
    def _short_path(path):
        """Show just the filename to keep the label compact."""
        from pathlib import Path as P
        return P(path).name if path else ''

    def _ok(self):
        # MIDI device
        idx = self.midi_combo.currentIndex()
        if idx > 0 and (idx - 1) < len(self._midi_ports):
            self.settings.midi_input_device = self._midi_ports[idx - 1]
        else:
            self.settings.midi_input_device = ''

        # SF2 path
        self.settings.sf2_path = self._sf2_path

        # Audio backend — switch immediately if it changed
        new_backend = self.backend_combo.currentData()
        backend_changed = new_backend != self.settings.audio_backend

        self.settings.save()

        if backend_changed and hasattr(self.app, 'switch_backend'):
            self.app.switch_backend(new_backend)
        else:
            # Apply SF2 immediately if it changed and engine is running
            if self._sf2_path and self.app.engine:
                try:
                    self.app.engine.load_sf2(self._sf2_path)
                except Exception:
                    pass
                try:
                    from ..core.sf2 import SF2Info
                    self.app.state.sf2 = SF2Info(self._sf2_path)
                    self.app.state.notify('sf2_loaded')
                except Exception:
                    pass

        # Notify app so Rec button can update its enabled state
        if hasattr(self.app, '_on_config_changed'):
            self.app._on_config_changed()

        self.accept()
