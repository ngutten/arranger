"""Top control bar - BPM, time signature, snap, tool buttons, and action buttons."""

from PySide6.QtWidgets import (QFrame, QLabel, QPushButton, QSpinBox, QComboBox,
                                QDoubleSpinBox, QHBoxLayout, QMenu, QButtonGroup)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QAction

from .master_meter import MasterMeter


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
        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(20.0, 300.0)
        self.bpm_spin.setDecimals(1)
        self.bpm_spin.setSingleStep(1.0)
        self.bpm_spin.setValue(s.bpm)
        self.bpm_spin.setMaximumWidth(70)
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

        # Note: the snap control now lives in the piano-roll header, since snap
        # is a note-editing concern (the arrangement snaps to measures).

        # ---- Master section (always-on master fader + limiter + meter) ----
        sep_m = QFrame()
        sep_m.setFrameShape(QFrame.VLine)
        sep_m.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep_m)

        layout.addWidget(QLabel('Master'))
        self.master_gain_spin = QDoubleSpinBox()
        self.master_gain_spin.setRange(-60.0, 24.0)
        self.master_gain_spin.setDecimals(1)
        self.master_gain_spin.setSingleStep(0.5)
        self.master_gain_spin.setSuffix(' dB')
        self.master_gain_spin.setValue(self._lin_to_db(s.master_gain))
        self.master_gain_spin.setMaximumWidth(86)
        self.master_gain_spin.setToolTip('Master makeup gain (applies to playback and export)')
        self.master_gain_spin.valueChanged.connect(self._on_master_gain)
        layout.addWidget(self.master_gain_spin)

        self.limiter_btn = QPushButton('LIM')
        self.limiter_btn.setCheckable(True)
        self.limiter_btn.setChecked(bool(s.master_limiter))
        self.limiter_btn.setMaximumWidth(40)
        self.limiter_btn.setToolTip('Master brickwall limiter (ceiling %.1f dBFS)'
                                    % s.master_ceiling_db)
        self.limiter_btn.toggled.connect(self._on_limiter)
        layout.addWidget(self.limiter_btn)
        # Track the limiter's visual state so we only restyle on transitions
        # (off / armed-idle / limiting), not every meter tick.
        self._limiter_state = None
        self._apply_limiter_style('off' if not s.master_limiter else 'armed')

        self.master_meter = MasterMeter()
        layout.addWidget(self.master_meter)

        # ---- Workspace switcher (centred) -------------------------------
        # Arrange / Graph — whole-screen layout modes. Exclusive, so exactly
        # one is active; mirrors app.set_workspace(). (The mixer is a dock,
        # not a workspace — see the Mixer toggle below.)
        layout.addStretch()
        self._workspace_btns = {}
        ws_group = QButtonGroup(self)
        ws_group.setExclusive(True)
        ws_box = QHBoxLayout()
        ws_box.setSpacing(0)
        for name, label, tip in (
            ('arrange', 'Arrange', 'Pattern list, arrangement timeline, and editors'),
            ('graph',   'Graph',   'Signal-graph editor (live sound design)'),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda checked=False, n=name: self.app.set_workspace(n))
            ws_group.addButton(btn)
            ws_box.addWidget(btn)
            self._workspace_btns[name] = btn
        self._workspace_btns['arrange'].setChecked(True)
        layout.addLayout(ws_box)
        layout.addStretch()

        # Action buttons
        config_btn = QPushButton('Config')
        config_btn.clicked.connect(self.app.open_config)

        # Mixer — toggles a bottom dock (movable to a side column if preferred)
        self.mixer_btn = QPushButton('Mixer')
        self.mixer_btn.setCheckable(True)
        self.mixer_btn.setToolTip('Show/hide the mixer (per-track faders + master)')
        self.mixer_btn.clicked.connect(self.app.toggle_mixer)
        layout.addWidget(config_btn)
        layout.addWidget(self.mixer_btn)

        add_track_btn = QPushButton('+ Track')
        add_track_btn.clicked.connect(self.app.add_track)
        layout.addWidget(add_track_btn)

        add_beat_track_btn = QPushButton('+ Beat Track')
        add_beat_track_btn.clicked.connect(self.app.add_beat_track)
        layout.addWidget(add_beat_track_btn)

        add_auto_track_btn = QPushButton('+ Auto Track')
        add_auto_track_btn.clicked.connect(self.app.add_automation_track)
        layout.addWidget(add_auto_track_btn)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.VLine)
        sep1.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep1)

        # Export — consolidated dropdown (was four always-visible buttons)
        export_btn = QPushButton('Export ⏷')
        export_btn.setToolTip('Export the arrangement')
        export_menu = QMenu(export_btn)
        for label, fmt in (('MIDI', 'midi'), ('Audio (WAV)', 'wav'),
                           ('Audio (MP3)', 'mp3'), ('MusicXML', 'musicxml')):
            act = QAction(label, export_menu)
            act.triggered.connect(
                lambda checked=False, f=fmt: self.app.do_export(f))
            export_menu.addAction(act)
        export_btn.setMenu(export_menu)
        layout.addWidget(export_btn)

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

    @staticmethod
    def _lin_to_db(lin: float) -> float:
        import math
        if lin <= 1e-4:
            return -60.0
        return max(-60.0, min(24.0, 20.0 * math.log10(lin)))

    @staticmethod
    def _db_to_lin(db: float) -> float:
        if db <= -60.0:
            return 0.0
        return 10.0 ** (db / 20.0)

    def _engine(self):
        """Return the engine if it exposes the master-section API, else None."""
        eng = self.app.engine
        return eng if (eng and hasattr(eng, 'set_master_gain')) else None

    def _on_master_gain(self, db):
        lin = self._db_to_lin(float(db))
        self.state.master_gain = lin
        eng = self._engine()
        if eng:
            eng.set_master_gain(lin)
        # Keep the running schedule in sync so a loop/restart re-applies this
        # value instead of reverting to the play-time setting; also syncs the
        # mixer's master strip.
        self.state.notify('master')

    def _on_limiter(self, checked):
        self.state.master_limiter = bool(checked)
        eng = self._engine()
        if eng:
            eng.set_master_limiter(bool(checked))
        self.state.notify('master')

    def sync_workspace(self, name: str):
        """Reflect the active workspace in the switcher without re-firing."""
        btn = self._workspace_btns.get(name)
        if btn is None:
            return
        btn.blockSignals(True)
        btn.setChecked(True)
        btn.blockSignals(False)

    def sync_mixer_button(self, is_open: bool):
        """Reflect mixer-dock visibility in the toggle button."""
        self.mixer_btn.blockSignals(True)
        self.mixer_btn.setChecked(bool(is_open))
        self.mixer_btn.blockSignals(False)

    def update_meter(self):
        """Poll the engine's master meter and repaint. Called by app.py timer."""
        eng = self._engine()
        if not eng:
            return
        m = getattr(eng, 'master_meter', None)
        if not m:
            return
        gr = float(m.get('gr', 1.0))
        self.master_meter.set_levels(
            float(m.get('peak_l', 0.0)),
            float(m.get('peak_r', 0.0)),
            gr,
        )
        # Light up LIM only when the limiter is on AND actually pulling gain
        # down (gr < ~ -0.1 dB, i.e. ~0.989 linear).
        if self.state.master_limiter:
            self._apply_limiter_style('limiting' if gr < 0.989 else 'armed')
        else:
            self._apply_limiter_style('off')

    def _on_bpm(self, value):
        self.state.bpm = float(value)

    def _on_ts(self):
        try:
            self.state.ts_num = int(self.ts_num_combo.currentText())
            self.state.ts_den = int(self.ts_den_combo.currentText())
            self.state.notify('ts')
        except Exception:
            pass

    def _apply_limiter_style(self, state: str):
        """Restyle the LIM button. state: 'off' | 'armed' | 'limiting'.

        'off'      — limiter disabled (dim).
        'armed'    — enabled but not currently reducing gain (neutral blue).
        'limiting' — actively reducing gain right now (amber/red alert).
        """
        if state == self._limiter_state:
            return
        self._limiter_state = state
        if state == 'limiting':
            css = ('QPushButton { background-color: #e0573a; color: #fff;'
                   ' border: 1px solid #ff8a6a; font-weight: bold; }')
        elif state == 'armed':
            css = ('QPushButton { background-color: #2c3a55; color: #cdd6e6;'
                   ' border: 1px solid #3a4a66; }')
        else:  # off
            css = 'QPushButton { color: #8a93a6; }'
        self.limiter_btn.setStyleSheet(css)

    def refresh(self):
        """Update controls from state."""
        self.bpm_spin.blockSignals(True)
        self.bpm_spin.setValue(self.state.bpm)
        self.bpm_spin.blockSignals(False)
        self.ts_num_combo.setCurrentText(str(self.state.ts_num))
        self.ts_den_combo.setCurrentText(str(self.state.ts_den))

        self.play_btn.setText('⏹' if self.state.playing else '▶')

        # Master section
        self.master_gain_spin.blockSignals(True)
        self.master_gain_spin.setValue(self._lin_to_db(self.state.master_gain))
        self.master_gain_spin.blockSignals(False)
        self.limiter_btn.blockSignals(True)
        self.limiter_btn.setChecked(bool(self.state.master_limiter))
        self.limiter_btn.blockSignals(False)
        # Baseline LIM look; update_meter() promotes to 'limiting' when reducing.
        if self._limiter_state != 'limiting':
            self._apply_limiter_style(
                'armed' if self.state.master_limiter else 'off')

        # Grey out BPM spinbox when a tempo automation track exists
        has_tempo_track = self.state.find_tempo_track() is not None
        self.bpm_spin.setEnabled(not has_tempo_track)
        if has_tempo_track:
            self.bpm_spin.setToolTip('BPM is controlled by the tempo automation track')
        else:
            self.bpm_spin.setToolTip('')

        # Enable the Graph workspace only when the engine supports the protocol
        graph_available = bool(self.app.engine and hasattr(self.app.engine, '_send'))
        gbtn = self._workspace_btns.get('graph')
        if gbtn is not None:
            gbtn.setEnabled(graph_available)
            gbtn.setToolTip(
                'Signal-graph editor (live sound design)' if graph_available
                else 'Signal graph editor requires the C++ built-in or server backend'
            )
        # Keep the switcher highlight in sync with the active workspace.
        self.sync_workspace(getattr(self.app, 'workspace', 'arrange'))

    def update_bpm_display(self, bpm: float):
        """Update spinbox to show current BPM without triggering _on_bpm."""
        self.bpm_spin.blockSignals(True)
        self.bpm_spin.setValue(bpm)
        self.bpm_spin.blockSignals(False)
