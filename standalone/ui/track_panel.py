"""Right panel - track settings, soundfont info, placement settings, beat kit."""

from PySide6.QtWidgets import (QFrame, QWidget, QScrollArea, QLabel, QPushButton,
                                QLineEdit, QSpinBox, QComboBox, QSlider, QListWidget,
                                QVBoxLayout, QHBoxLayout, QGroupBox)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor, QFont

from ..state import NOTE_NAMES, SCALES, PALETTE, preset_name, BeatInstrument


class TrackPanel(QFrame):
    """Right panel with track settings, SF2 info, placement settings, and beat kit."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self.setFixedWidth(250)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Scrollable container
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.inner = QWidget()
        self.inner_layout = QVBoxLayout(self.inner)
        self.inner_layout.setSpacing(4)
        
        # Sections with size policies to prevent unwanted expansion
        from PySide6.QtWidgets import QSizePolicy
        
        self.trk_frame = QGroupBox('Track Settings')
        self.trk_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.inner_layout.addWidget(self.trk_frame)

        self.sf2_frame = QGroupBox('Soundfont')
        self.sf2_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.inner_layout.addWidget(self.sf2_frame)

        self.pl_frame = QGroupBox('Placement')
        self.pl_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.inner_layout.addWidget(self.pl_frame)

        self.kit_frame = QGroupBox('Beat Kit')
        self.kit_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.inner_layout.addWidget(self.kit_frame)

        self.inner_layout.addStretch()

        self.scroll_area.setWidget(self.inner)
        layout.addWidget(self.scroll_area)

    def refresh(self):
        """Rebuild all sections from state."""
        self._render_track_settings()
        self._render_sf2_info()
        self._render_placement_settings()
        self._render_beat_kit()

    def _clear_frame(self, frame, keep_widget=None):
        if not frame.layout():
            # Create layout if it doesn't exist yet (first time)
            QVBoxLayout(frame)
        # Clear all widgets from the layout
        while frame.layout().count():
            item = frame.layout().takeAt(0)
            if item.widget() and item.widget() != keep_widget:
                item.widget().deleteLater()

    def _render_track_settings(self):
        self._clear_frame(self.trk_frame)
        layout = self.trk_frame.layout()
        s = self.state

        # Check for beat track selection first
        bt = s.find_beat_track(s.sel_beat_trk) if s.sel_beat_trk else None
        if bt:
            self._row(layout, 'Name', bt.name,
                      lambda v, bt=bt: self._update_beat_track(bt, 'name', v))
            label = QLabel('Beat Track')
            label.setStyleSheet('color: #e94560;')
            label.setFont(QFont('TkDefaultFont', 9))
            layout.addWidget(label)
            
            del_btn = QPushButton('Delete Track')
            del_btn.clicked.connect(lambda: self.app.delete_beat_track(bt.id))
            layout.addWidget(del_btn)
            return

        t = s.find_track(s.sel_trk) if s.sel_trk else None
        if not t:
            label = QLabel('Select a track')
            label.setStyleSheet('color: #888;')
            layout.addWidget(label)
            return

        self._row(layout, 'Name', t.name,
                  lambda v, t=t: self._update_track(t, 'name', v))

        # Channel
        ch_layout = QHBoxLayout()
        ch_layout.addWidget(QLabel('Channel'))
        ch_cb = QComboBox()
        ch_cb.addItems([f'Ch {i+1}' + (' (Drums)' if i == 9 else '')
                        for i in range(16)])
        ch_cb.setCurrentIndex(t.channel)
        ch_cb.currentIndexChanged.connect(
            lambda idx, t=t: self._update_track(t, 'channel', idx))
        ch_layout.addWidget(ch_cb)
        layout.addLayout(ch_layout)

        # Preset name display
        presets = None
        if s.sf2 and hasattr(s.sf2, 'presets'):
            presets = s.sf2.presets
        elif s.sf2 and isinstance(s.sf2, dict):
            presets = s.sf2.get('presets')
        pn = preset_name(t.bank, t.program, presets)
        preset_label = QLabel(f'Preset: {pn}')
        preset_label.setStyleSheet('color: #e94560;')
        preset_label.setFont(QFont('TkDefaultFont', 8))
        layout.addWidget(preset_label)

        # Volume
        vol_layout = QHBoxLayout()
        vol_layout.addWidget(QLabel('Volume'))
        vol_slider = QSlider(Qt.Horizontal)
        vol_slider.setRange(0, 127)
        vol_slider.setValue(t.volume)
        vol_slider.valueChanged.connect(
            lambda v, t=t: self._update_track(t, 'volume', v))
        vol_layout.addWidget(vol_slider)
        vol_label = QLabel(str(t.volume))
        vol_slider.valueChanged.connect(lambda v: vol_label.setText(str(v)))
        vol_layout.addWidget(vol_label)
        layout.addLayout(vol_layout)

        del_btn = QPushButton('Delete Track')
        del_btn.clicked.connect(lambda: self.app.delete_track(t.id))
        layout.addWidget(del_btn)

    def _render_sf2_info(self):
        self._clear_frame(self.sf2_frame)
        layout = self.sf2_frame.layout()
        s = self.state

        if not s.sf2:
            label = QLabel('No soundfont loaded')
            label.setStyleSheet('color: #888;')
            layout.addWidget(label)
            return

        sf2_name = s.sf2.name if hasattr(s.sf2, 'name') else (
            s.sf2.get('name', 'Unknown') if isinstance(s.sf2, dict) else 'Unknown')
        name_label = QLabel(sf2_name)
        name_label.setFont(QFont('TkDefaultFont', 8))
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        presets = s.sf2.presets if hasattr(s.sf2, 'presets') else (
            s.sf2.get('presets', []) if isinstance(s.sf2, dict) else [])
        if not presets:
            return

        # Bank filter
        t = s.find_track(s.sel_trk)
        current_bank = t.bank if t else 0
        banks = sorted(set(p['bank'] for p in presets))

        bank_layout = QHBoxLayout()
        bank_layout.addWidget(QLabel('Bank'))
        bank_cb = QComboBox()
        bank_cb.addItems([f'Bank {b}' for b in banks])
        try:
            bank_cb.setCurrentIndex(banks.index(current_bank))
        except ValueError:
            if banks:
                bank_cb.setCurrentIndex(0)
        bank_cb.currentIndexChanged.connect(
            lambda idx: self._on_bank_change(banks[idx]))
        bank_layout.addWidget(bank_cb)
        layout.addLayout(bank_layout)

        # Preset list - reduced max height to prevent expansion
        filtered = [p for p in presets if p['bank'] == current_bank]
        # Sort by program number
        filtered.sort(key=lambda p: p['program'])
        self.preset_listbox = QListWidget()
        self.preset_listbox.setMaximumHeight(120)
        self.preset_listbox.setMinimumHeight(80)
        for p in filtered:
            self.preset_listbox.addItem(f"{p['program']:3d}  {p['name']}")
        # Single click to select
        self.preset_listbox.itemClicked.connect(
            lambda: self._on_preset_select(filtered))
        # Double-click also works
        self.preset_listbox.itemDoubleClicked.connect(
            lambda: self._on_preset_select(filtered))
        layout.addWidget(self.preset_listbox)

    def _render_placement_settings(self):
        self._clear_frame(self.pl_frame)
        layout = self.pl_frame.layout()
        s = self.state

        # Check for beat placement first
        bp = s.find_beat_placement(s.sel_beat_pl) if s.sel_beat_pl else None
        if bp:
            label = QLabel('Beat Placement')
            label.setStyleSheet('color: #e94560;')
            label.setFont(QFont('TkDefaultFont', 8))
            layout.addWidget(label)

            self._num_row(layout, 'Repeats', bp.repeats, 1, 128,
                          lambda v, bp=bp: self._update_beat_pl(bp, 'repeats', v))

            del_btn = QPushButton('Remove')
            del_btn.clicked.connect(lambda: self._del_beat_pl(bp.id))
            layout.addWidget(del_btn)
            return

        pl = s.find_placement(s.sel_pl) if s.sel_pl else None
        if not pl:
            label = QLabel('Select a placement')
            label.setStyleSheet('color: #888;')
            layout.addWidget(label)
            return

        # Target key
        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel('To Key'))
        key_cb = QComboBox()
        key_cb.addItems(['(none)'] + NOTE_NAMES[:12])
        if pl.target_key:
            try:
                key_cb.setCurrentText(pl.target_key)
            except:
                key_cb.setCurrentIndex(0)
        else:
            key_cb.setCurrentIndex(0)
        
        def on_key_change(idx):
            if idx == 0:
                pl.target_key = None
            else:
                pl.target_key = key_cb.currentText()
            self.state.notify('placement_settings')
        
        key_cb.currentIndexChanged.connect(on_key_change)
        key_layout.addWidget(key_cb)
        layout.addLayout(key_layout)

        # Manual transpose
        self._num_row(layout, 'Shift', pl.transpose, -48, 48,
                      lambda v, pl=pl: self._update_pl(pl, 'transpose', v))

        # Show computed transpose
        pat = s.find_pattern(pl.pattern_id)
        ts = s.compute_transpose(pl)
        pk = pat.key if pat else 'C'
        ps = pat.scale if pat else 'major'
        info_label = QLabel(f'Base: {pk} {ps} -> total shift: {ts} semi')
        info_label.setStyleSheet('color: #888;')
        info_label.setFont(QFont('TkDefaultFont', 7))
        layout.addWidget(info_label)

        self._num_row(layout, 'Repeats', pl.repeats, 1, 128,
                      lambda v, pl=pl: self._update_pl(pl, 'repeats', v))

        del_btn = QPushButton('Remove')
        del_btn.clicked.connect(lambda: self._del_pl(pl.id))
        layout.addWidget(del_btn)

    def _render_beat_kit(self):
        self._clear_frame(self.kit_frame)
        layout = self.kit_frame.layout()

        if not self.state.beat_kit:
            label = QLabel('No instruments. Click + to add.')
            label.setStyleSheet('color: #888;')
            label.setFont(QFont('TkDefaultFont', 8))
            layout.addWidget(label)
        else:
            for i, inst in enumerate(self.state.beat_kit):
                color = PALETTE[i % len(PALETTE)]
                inst_widget = self._create_inst_widget(inst, color)
                layout.addWidget(inst_widget)

        add_btn = QPushButton('+ Instrument')
        add_btn.clicked.connect(self.app.add_beat_instrument)
        layout.addWidget(add_btn)

    def _create_inst_widget(self, inst, color):
        widget = QFrame()
        widget_layout = QVBoxLayout(widget)
        widget_layout.setContentsMargins(4, 4, 4, 4)
        widget_layout.setSpacing(0)

        # Header row (clickable to expand/collapse)
        hdr_frame = QFrame()
        hdr_frame.setCursor(Qt.PointingHandCursor)
        hdr_layout = QHBoxLayout(hdr_frame)
        hdr_layout.setContentsMargins(0, 0, 0, 4)
        
        # Color indicator
        color_widget = ColorDot(color)
        hdr_layout.addWidget(color_widget)
        
        name_label = QLabel(inst.name)
        name_label.setFont(QFont('TkDefaultFont', 8, QFont.Bold))
        hdr_layout.addWidget(name_label, 1)

        play_btn = QPushButton('▶')
        play_btn.setMaximumWidth(30)
        play_btn.clicked.connect(lambda: self.app.play_beat_hit(inst.id))
        hdr_layout.addWidget(play_btn)

        del_btn = QPushButton('✕')
        del_btn.setMaximumWidth(30)
        del_btn.clicked.connect(lambda: self.app.delete_beat_instrument(inst.id))
        hdr_layout.addWidget(del_btn)

        widget_layout.addWidget(hdr_frame)

        # Detail rows (collapsible)
        det_frame = QFrame()
        det_layout = QVBoxLayout(det_frame)
        det_layout.setContentsMargins(12, 0, 0, 0)

        # Name
        self._small_row(det_layout, 'Name', inst.name,
                        lambda v, inst=inst: self._update_inst(inst, 'name', v))

        # Channel
        ch_layout = QHBoxLayout()
        ch_label = QLabel('Ch')
        ch_label.setMinimumWidth(40)
        ch_label.setFont(QFont('TkDefaultFont', 7))
        ch_layout.addWidget(ch_label)
        
        ch_cb = QComboBox()
        ch_cb.addItems([f'Ch {i+1}' + (' (Drums)' if i == 9 else '')
                        for i in range(16)])
        ch_cb.setCurrentIndex(inst.channel)
        ch_cb.currentIndexChanged.connect(
            lambda idx, inst=inst: self._update_inst(inst, 'channel', idx))
        ch_layout.addWidget(ch_cb)
        det_layout.addLayout(ch_layout)

        # For drum channel (9/Ch 10), only show pitch
        # For other channels, show bank and program dropdowns
        if inst.channel == 9:  # Drum channel
            # Pitch
            pitch_layout = QHBoxLayout()
            pitch_label = QLabel('Pitch')
            pitch_label.setMinimumWidth(40)
            pitch_label.setFont(QFont('TkDefaultFont', 7))
            pitch_layout.addWidget(pitch_label)
            
            pitch_spin = QSpinBox()
            pitch_spin.setRange(0, 127)
            pitch_spin.setValue(inst.pitch)
            pitch_spin.valueChanged.connect(
                lambda v, inst=inst: self._update_inst(inst, 'pitch', v))
            pitch_layout.addWidget(pitch_spin)
            
            nn = NOTE_NAMES[inst.pitch % 12]
            octave = inst.pitch // 12 - 1
            pitch_note = QLabel(f'{nn}{octave}')
            pitch_note.setStyleSheet('color: #888;')
            pitch_note.setFont(QFont('TkDefaultFont', 7))
            pitch_layout.addWidget(pitch_note)
            det_layout.addLayout(pitch_layout)
        else:
            # Get presets from soundfont if available
            presets = None
            if self.state.sf2 and hasattr(self.state.sf2, 'presets'):
                presets = self.state.sf2.presets
            elif self.state.sf2 and isinstance(self.state.sf2, dict):
                presets = self.state.sf2.get('presets')

            if presets:
                # Bank dropdown
                banks = sorted(set(p['bank'] for p in presets))
                bank_layout = QHBoxLayout()
                bank_label = QLabel('Bank')
                bank_label.setMinimumWidth(40)
                bank_label.setFont(QFont('TkDefaultFont', 7))
                bank_layout.addWidget(bank_label)
                
                bank_cb = QComboBox()
                bank_cb.addItems([f'{b}' for b in banks])
                try:
                    bank_cb.setCurrentIndex(banks.index(inst.bank))
                except ValueError:
                    if banks:
                        bank_cb.setCurrentIndex(0)
                bank_cb.currentIndexChanged.connect(
                    lambda idx, inst=inst, banks=banks: self._update_inst_bank_and_refresh(inst, banks[idx]))
                bank_layout.addWidget(bank_cb)
                det_layout.addLayout(bank_layout)

                # Program dropdown with names
                current_bank = inst.bank
                filtered = [p for p in presets if p['bank'] == current_bank]
                filtered.sort(key=lambda p: p['program'])
                
                prog_layout = QHBoxLayout()
                prog_label = QLabel('Prog')
                prog_label.setMinimumWidth(40)
                prog_label.setFont(QFont('TkDefaultFont', 7))
                prog_layout.addWidget(prog_label)
                
                prog_cb = QComboBox()
                prog_cb.setMaximumHeight(200)
                for p in filtered:
                    prog_cb.addItem(f"{p['program']:3d} {p['name']}", p['program'])
                # Find current program
                try:
                    current_idx = next(i for i, p in enumerate(filtered) if p['program'] == inst.program)
                    prog_cb.setCurrentIndex(current_idx)
                except StopIteration:
                    pass
                prog_cb.currentIndexChanged.connect(
                    lambda idx, inst=inst, filtered=filtered: 
                        self._update_inst(inst, 'program', filtered[idx]['program']) if idx < len(filtered) else None)
                prog_layout.addWidget(prog_cb)
                det_layout.addLayout(prog_layout)
            else:
                # No soundfont - show numeric spinners
                bank_layout = QHBoxLayout()
                bank_label = QLabel('Bank')
                bank_label.setMinimumWidth(40)
                bank_label.setFont(QFont('TkDefaultFont', 7))
                bank_layout.addWidget(bank_label)
                
                bank_spin = QSpinBox()
                bank_spin.setRange(0, 16383)
                bank_spin.setValue(inst.bank)
                bank_spin.valueChanged.connect(
                    lambda v, inst=inst: self._update_inst(inst, 'bank', v))
                bank_layout.addWidget(bank_spin)
                det_layout.addLayout(bank_layout)

                prog_layout = QHBoxLayout()
                prog_label = QLabel('Prog')
                prog_label.setMinimumWidth(40)
                prog_label.setFont(QFont('TkDefaultFont', 7))
                prog_layout.addWidget(prog_label)
                
                prog_spin = QSpinBox()
                prog_spin.setRange(0, 127)
                prog_spin.setValue(inst.program)
                prog_spin.valueChanged.connect(
                    lambda v, inst=inst: self._update_inst(inst, 'program', v))
                prog_layout.addWidget(prog_spin)
                det_layout.addLayout(prog_layout)

            # Pitch (still shown for non-drum channels)
            pitch_layout = QHBoxLayout()
            pitch_label = QLabel('Pitch')
            pitch_label.setMinimumWidth(40)
            pitch_label.setFont(QFont('TkDefaultFont', 7))
            pitch_layout.addWidget(pitch_label)
            
            pitch_spin = QSpinBox()
            pitch_spin.setRange(0, 127)
            pitch_spin.setValue(inst.pitch)
            pitch_spin.valueChanged.connect(
                lambda v, inst=inst: self._update_inst(inst, 'pitch', v))
            pitch_layout.addWidget(pitch_spin)
            
            nn = NOTE_NAMES[inst.pitch % 12]
            octave = inst.pitch // 12 - 1
            pitch_note = QLabel(f'{nn}{octave}')
            pitch_note.setStyleSheet('color: #888;')
            pitch_note.setFont(QFont('TkDefaultFont', 7))
            pitch_layout.addWidget(pitch_note)
            det_layout.addLayout(pitch_layout)

        # Velocity
        vel_layout = QHBoxLayout()
        vel_label = QLabel('Vel')
        vel_label.setMinimumWidth(40)
        vel_label.setFont(QFont('TkDefaultFont', 7))
        vel_layout.addWidget(vel_label)
        
        vel_slider = QSlider(Qt.Horizontal)
        vel_slider.setRange(1, 127)
        vel_slider.setValue(inst.velocity)
        vel_slider.valueChanged.connect(
            lambda v, inst=inst: self._update_inst(inst, 'velocity', v))
        vel_layout.addWidget(vel_slider)
        
        vel_num = QLabel(str(inst.velocity))
        vel_slider.valueChanged.connect(lambda v: vel_num.setText(str(v)))
        vel_layout.addWidget(vel_num)
        det_layout.addLayout(vel_layout)

        widget_layout.addWidget(det_frame)
        
        # Store collapsed state on widget itself
        if not hasattr(inst, '_ui_collapsed'):
            inst._ui_collapsed = False
        det_frame.setVisible(not inst._ui_collapsed)
        
        # Click handler for header to toggle collapse
        def toggle_collapse(event):
            inst._ui_collapsed = not inst._ui_collapsed
            det_frame.setVisible(not inst._ui_collapsed)
        
        hdr_frame.mousePressEvent = toggle_collapse
        
        return widget

    # Helpers
    def _row(self, layout, label, value, on_change):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        entry = QLineEdit(value)
        entry.editingFinished.connect(lambda: on_change(entry.text()))
        row.addWidget(entry)
        layout.addLayout(row)

    def _small_row(self, layout, label, value, on_change):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFont(QFont('TkDefaultFont', 7))
        lbl.setMinimumWidth(40)
        row.addWidget(lbl)
        entry = QLineEdit(value)
        entry.setFont(QFont('TkDefaultFont', 7))
        entry.editingFinished.connect(lambda: on_change(entry.text()))
        row.addWidget(entry)
        layout.addLayout(row)

    def _num_row(self, layout, label, value, min_val, max_val, on_change,
                 step=1, is_float=False):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        spin = QSpinBox()
        spin.setRange(min_val, max_val)
        spin.setSingleStep(step)
        spin.setValue(int(value))
        spin.valueChanged.connect(lambda v: on_change(int(v)))
        row.addWidget(spin)
        layout.addLayout(row)

    def _update_track(self, track, key, value):
        setattr(track, key, value)
        self.state.notify('track_settings')

    def _update_beat_track(self, bt, key, value):
        setattr(bt, key, value)
        self.state.notify('beat_track_settings')

    def _update_pl(self, pl, key, value):
        setattr(pl, key, value)
        self.state.notify('placement_settings')

    def _update_beat_pl(self, bp, key, value):
        setattr(bp, key, value)
        self.state.notify('beat_placement_settings')

    def _update_inst(self, inst, key, value):
        setattr(inst, key, value)
        self.state.notify('beat_kit')

    def _update_inst_bank_and_refresh(self, inst, bank):
        """Update bank and refresh to reload program dropdown."""
        inst.bank = bank
        self.state.notify('beat_kit')

    def _del_pl(self, plid):
        self.state.placements = [p for p in self.state.placements if p.id != plid]
        self.state.sel_pl = None
        self.state.notify('del_pl')

    def _del_beat_pl(self, bplid):
        self.state.beat_placements = [p for p in self.state.beat_placements if p.id != bplid]
        self.state.sel_beat_pl = None
        self.state.notify('del_beat_pl')

    def _on_bank_change(self, bank):
        t = self.state.find_track(self.state.sel_trk)
        if t:
            t.bank = bank
            self.state.notify('track_settings')

    def _on_preset_select(self, filtered_presets):
        idx = self.preset_listbox.currentRow()
        if idx >= 0 and idx < len(filtered_presets):
            p = filtered_presets[idx]
            t = self.state.find_track(self.state.sel_trk)
            if t:
                t.bank = p['bank']
                t.program = p['program']
                # Notify state change so other components update
                self.state.notify('track_settings')
                # Refresh track settings section
                self._render_track_settings()


class ColorDot(QWidget):
    """Small colored circle indicator."""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.setFixedSize(10, 10)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(self.color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(1, 1, 8, 8)
