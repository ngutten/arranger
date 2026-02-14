"""Top control bar - BPM, time signature, snap, tool buttons, and action buttons."""

from PySide6.QtWidgets import (QFrame, QLabel, QPushButton, QSpinBox, QComboBox, 
                                QHBoxLayout)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


class TopBar(QFrame):
    """Top bar with transport controls, BPM, time sig, snap, and action buttons."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self._build()

    def _build(self):
        s = self.state
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(6)

        # Title
        title_label = QLabel("Arranger")
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        title_label.setFont(font)
        title_label.setStyleSheet('color: #e94560;')
        layout.addWidget(title_label)
        layout.addSpacing(12)

        # Play button
        self.play_btn = QPushButton('▶')
        self.play_btn.setMaximumWidth(40)
        self.play_btn.clicked.connect(self.app.toggle_play)
        layout.addWidget(self.play_btn)

        # Loop button
        self.loop_btn = QPushButton('Loop')
        self.loop_btn.setMaximumWidth(50)
        self.loop_btn.clicked.connect(self.app.toggle_loop)
        layout.addWidget(self.loop_btn)

        layout.addSpacing(8)

        # BPM
        layout.addWidget(QLabel('BPM'))
        self.bpm_spin = QSpinBox()
        self.bpm_spin.setRange(20, 300)
        self.bpm_spin.setValue(s.bpm)
        self.bpm_spin.setMaximumWidth(60)
        self.bpm_spin.valueChanged.connect(self._on_bpm)
        layout.addWidget(self.bpm_spin)

        layout.addSpacing(8)

        # Time signature
        layout.addWidget(QLabel('TS'))
        self.ts_num_combo = QComboBox()
        self.ts_num_combo.addItems(['2', '3', '4', '5', '6', '7'])
        self.ts_num_combo.setCurrentText(str(s.ts_num))
        self.ts_num_combo.setMaximumWidth(50)
        self.ts_num_combo.currentTextChanged.connect(self._on_ts)
        layout.addWidget(self.ts_num_combo)

        layout.addWidget(QLabel('/'))

        self.ts_den_combo = QComboBox()
        self.ts_den_combo.addItems(['2', '4', '8'])
        self.ts_den_combo.setCurrentText(str(s.ts_den))
        self.ts_den_combo.setMaximumWidth(50)
        self.ts_den_combo.currentTextChanged.connect(self._on_ts)
        layout.addWidget(self.ts_den_combo)

        layout.addSpacing(8)

        # Snap
        layout.addWidget(QLabel('Snap'))
        self.snap_combo = QComboBox()
        self.snap_combo.addItems(['1', '1/2', '1/4', '1/8', '1/16'])
        # Map current snap value to display format
        snap_map = {1: '1', 0.5: '1/2', 0.25: '1/4', 0.125: '1/8', 0.0625: '1/16'}
        self.snap_combo.setCurrentText(snap_map.get(s.snap, '1/4'))
        self.snap_combo.setMaximumWidth(70)
        self.snap_combo.currentTextChanged.connect(self._on_snap)
        layout.addWidget(self.snap_combo)

        # Spacer
        layout.addStretch()

        # Action buttons
        config_btn = QPushButton('Config')
        config_btn.clicked.connect(self.app.open_config)
        layout.addWidget(config_btn)

        # Graph Editor button — enabled only when server engine is active
        self.graph_btn = QPushButton('Graph ⬡')
        self.graph_btn.setToolTip(
            'Open signal graph editor\n'
            '(requires the C++ audio server backend)')
        self.graph_btn.clicked.connect(self.app.open_graph_editor)
        layout.addWidget(self.graph_btn)

        add_track_btn = QPushButton('+ Track')
        add_track_btn.clicked.connect(self.app.add_track)
        layout.addWidget(add_track_btn)

        add_beat_track_btn = QPushButton('+ Beat Track')
        add_beat_track_btn.clicked.connect(self.app.add_beat_track)
        layout.addWidget(add_beat_track_btn)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.VLine)
        sep1.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep1)

        midi_btn = QPushButton('MIDI')
        midi_btn.clicked.connect(lambda: self.app.do_export('midi'))
        layout.addWidget(midi_btn)

        wav_btn = QPushButton('WAV')
        wav_btn.clicked.connect(lambda: self.app.do_export('wav'))
        layout.addWidget(wav_btn)

        mp3_btn = QPushButton('MP3')
        mp3_btn.clicked.connect(lambda: self.app.do_export('mp3'))
        layout.addWidget(mp3_btn)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep2)

        new_btn = QPushButton('New')
        new_btn.clicked.connect(self.app.new_project)
        layout.addWidget(new_btn)

        save_btn = QPushButton('Save')
        save_btn.clicked.connect(self.app.save_project)
        layout.addWidget(save_btn)

        load_btn = QPushButton('Load')
        load_btn.clicked.connect(self.app.load_project)
        layout.addWidget(load_btn)

    def _on_bpm(self, value):
        self.state.bpm = value

    def _on_ts(self):
        try:
            self.state.ts_num = int(self.ts_num_combo.currentText())
            self.state.ts_den = int(self.ts_den_combo.currentText())
            self.state.notify('ts')
        except Exception:
            pass

    def _on_snap(self):
        try:
            text = self.snap_combo.currentText()
            # Parse fraction format
            if '/' in text:
                parts = text.split('/')
                self.state.snap = float(parts[0]) / float(parts[1])
            else:
                self.state.snap = float(text)
        except Exception:
            pass

    def refresh(self):
        """Update controls from state."""
        self.bpm_spin.setValue(self.state.bpm)
        self.ts_num_combo.setCurrentText(str(self.state.ts_num))
        self.ts_den_combo.setCurrentText(str(self.state.ts_den))
        
        # Map snap value to display format
        snap_map = {1: '1', 0.5: '1/2', 0.25: '1/4', 0.125: '1/8', 0.0625: '1/16'}
        self.snap_combo.setCurrentText(snap_map.get(self.state.snap, '1/4'))
        
        self.play_btn.setText('⏹' if self.state.playing else '▶')

        # Enable graph editor button only when server engine is active
        try:
            from ..core.server_engine import ServerEngine
            graph_available = isinstance(self.app.engine, ServerEngine)
        except Exception:
            graph_available = False
        self.graph_btn.setEnabled(graph_available)
        self.graph_btn.setToolTip(
            'Open signal graph editor' if graph_available
            else 'Signal graph editor requires the C++ audio server backend'
        )
