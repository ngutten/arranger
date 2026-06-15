"""Full mixer view — per-track channel strips plus the master strip.

Shown as a swappable editor in the bottom pane (toggled from the top bar).
Each strip is a view onto a Track's volume/pan/mute/solo state — editing here
mutates the same fields the inspector uses and routes through state.notify(),
so live playback and export both pick up the change via the normal schedule
rebuild.  The master strip mirrors the top-bar master controls.
"""

import math

from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QSlider,
                               QHBoxLayout, QVBoxLayout, QScrollArea, QSizePolicy)
from PySide6.QtCore import Qt

from .master_meter import MasterMeter


def _vol_to_db_label(vol127: int) -> str:
    # MIDI CC7 is perceptual-ish; show the raw 0..127 value, it's what users tweak.
    return str(int(vol127))


class ChannelStrip(QFrame):
    """One per-track strip: name, pan, vertical fader, mute/solo."""

    def __init__(self, app, track, parent=None, show_pan=True, kind='melodic'):
        super().__init__(parent)
        self.app = app
        self.track = track
        self.show_pan = show_pan
        self.kind = kind   # 'melodic' (CC7 volume) or 'beat' (velocity scale)
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedWidth(72)
        bg = '#241f1b' if kind == 'beat' else '#1b1b27'
        self.setStyleSheet(
            'ChannelStrip { background:%s; border:1px solid #2a2a3a; }' % bg)
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(3)

        name = QLabel(self.track.name)
        name.setAlignment(Qt.AlignCenter)
        name.setStyleSheet('color:#cdd; font-size:10px;')
        name.setWordWrap(False)
        name.setFixedHeight(14)
        v.addWidget(name)
        self.name_label = name

        # Per-track level meter (graph mode: lit when this track owns a unique
        # mixer input channel). Hidden until MixerView assigns a channel.
        self.meter = MasterMeter(compact=True)
        self.meter.setFixedHeight(16)
        self.meter.hide()
        self._meter_channel = None
        v.addWidget(self.meter)

        # Pan (horizontal, -1..1 mapped to -100..100). Beat tracks span
        # multiple shared channels, so they have no per-track pan.
        self.pan = None
        self.pan_label = None
        if self.show_pan:
            self.pan = QSlider(Qt.Horizontal)
            self.pan.setRange(-100, 100)
            self.pan.setValue(int(round(self.track.pan * 100)))
            self.pan.setToolTip('Pan (L / R)')
            self.pan.valueChanged.connect(self._on_pan)
            v.addWidget(self.pan)
            self.pan_label = QLabel(self._pan_text())
            self.pan_label.setAlignment(Qt.AlignCenter)
            self.pan_label.setStyleSheet('color:#889; font-size:9px;')
            v.addWidget(self.pan_label)

        # Volume fader (vertical, 0..127)
        self.fader = QSlider(Qt.Vertical)
        self.fader.setRange(0, 127)
        self.fader.setValue(int(self.track.volume))
        self.fader.setToolTip('Track volume (MIDI CC7)' if self.kind == 'melodic'
                              else 'Beat track level (velocity scale)')
        self.fader.valueChanged.connect(self._on_volume)
        self.fader.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        v.addWidget(self.fader, stretch=1, alignment=Qt.AlignHCenter)

        self.vol_label = QLabel(_vol_to_db_label(self.track.volume))
        self.vol_label.setAlignment(Qt.AlignCenter)
        self.vol_label.setStyleSheet('color:#aab; font-size:9px;')
        v.addWidget(self.vol_label)

        # Mute / solo
        ms = QHBoxLayout()
        ms.setSpacing(2)
        self.mute_btn = QPushButton('M')
        self.mute_btn.setCheckable(True)
        self.mute_btn.setChecked(self.track.mute)
        self.mute_btn.setFixedSize(30, 20)
        self.mute_btn.setToolTip('Mute')
        self.mute_btn.toggled.connect(self._on_mute)
        self.solo_btn = QPushButton('S')
        self.solo_btn.setCheckable(True)
        self.solo_btn.setChecked(self.track.solo)
        self.solo_btn.setFixedSize(30, 20)
        self.solo_btn.setToolTip('Solo')
        self.solo_btn.toggled.connect(self._on_solo)
        ms.addWidget(self.mute_btn)
        ms.addWidget(self.solo_btn)
        v.addLayout(ms)
        self._restyle_ms()

    def _pan_text(self):
        p = self.track.pan
        if abs(p) < 0.005:
            return 'C'
        return ('L%d' if p < 0 else 'R%d') % int(round(abs(p) * 100))

    def _restyle_ms(self):
        self.mute_btn.setStyleSheet(
            'background:#c0492f; color:white;' if self.track.mute
            else 'background:#2a2a38; color:#aaa;')
        self.solo_btn.setStyleSheet(
            'background:#d6b42f; color:black;' if self.track.solo
            else 'background:#2a2a38; color:#aaa;')

    def sync(self):
        """Update widgets from track state without emitting change signals.

        Used on coalesced refreshes so a fader the user is dragging isn't
        destroyed; setting a slider to its current value is a harmless no-op.
        """
        self.name_label.setText(self.track.name)
        self.fader.blockSignals(True)
        self.fader.setValue(int(self.track.volume))
        self.fader.blockSignals(False)
        self.vol_label.setText(_vol_to_db_label(self.track.volume))
        if self.pan is not None:
            self.pan.blockSignals(True)
            self.pan.setValue(int(round(self.track.pan * 100)))
            self.pan.blockSignals(False)
            self.pan_label.setText(self._pan_text())
        for btn, on in ((self.mute_btn, self.track.mute),
                        (self.solo_btn, self.track.solo)):
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)
        self._restyle_ms()

    def set_meter_channel(self, ch):
        """Assign the mixer input channel whose level this strip shows, or None."""
        self._meter_channel = ch
        self.meter.setVisible(ch is not None)

    def update_level(self, channels):
        """channels: list of {peak_l,peak_r,...} from the engine meter poll."""
        ch = self._meter_channel
        if ch is None or ch < 0 or ch >= len(channels):
            return
        c = channels[ch]
        self.meter.set_levels(float(c.get('peak_l', 0.0)),
                              float(c.get('peak_r', 0.0)))

    def _notify(self):
        self.app.state.notify('track_settings')

    def _on_volume(self, val):
        self.track.volume = int(val)
        self.vol_label.setText(_vol_to_db_label(val))
        self._notify()

    def _on_pan(self, val):
        self.track.pan = val / 100.0
        self.pan_label.setText(self._pan_text())
        self._notify()

    def _on_mute(self, checked):
        self.track.mute = bool(checked)
        self._restyle_ms()
        self._notify()

    def _on_solo(self, checked):
        self.track.solo = bool(checked)
        self._restyle_ms()
        self._notify()


class MasterStrip(QFrame):
    """Master fader + limiter toggle + meter, mirroring the top-bar controls."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedWidth(88)
        self.setStyleSheet('MasterStrip { background:#241b27; border:1px solid #3a2a3a; }')
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(3)

        title = QLabel('MASTER')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet('color:#e94560; font-size:10px; font-weight:bold;')
        title.setFixedHeight(14)
        v.addWidget(title)

        self.meter = MasterMeter()
        self.meter.setMinimumSize(40, 60)
        self.meter.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        # Fader in dB (-60..+24) next to the meter.
        body = QHBoxLayout()
        self.fader = QSlider(Qt.Vertical)
        self.fader.setRange(-600, 240)   # tenths of a dB
        self.fader.setValue(self._db_to_ticks(self._cur_db()))
        self.fader.setToolTip('Master makeup gain (dB)')
        self.fader.valueChanged.connect(self._on_gain)
        body.addWidget(self.fader, alignment=Qt.AlignHCenter)
        body.addWidget(self.meter)
        v.addLayout(body, stretch=1)

        self.gain_label = QLabel(self._db_text())
        self.gain_label.setAlignment(Qt.AlignCenter)
        self.gain_label.setStyleSheet('color:#aab; font-size:9px;')
        v.addWidget(self.gain_label)

        self.lim_btn = QPushButton('LIM')
        self.lim_btn.setCheckable(True)
        self.lim_btn.setChecked(bool(self.app.state.master_limiter))
        self.lim_btn.setFixedHeight(20)
        self.lim_btn.toggled.connect(self._on_limiter)
        v.addWidget(self.lim_btn)

    # --- dB helpers (fader stores tenths of a dB) ---
    @staticmethod
    def _db_to_ticks(db):
        return int(round(max(-60.0, min(24.0, db)) * 10))

    @staticmethod
    def _lin_to_db(lin):
        if lin <= 1e-4:
            return -60.0
        return max(-60.0, min(24.0, 20.0 * math.log10(lin)))

    def _cur_db(self):
        return self._lin_to_db(self.app.state.master_gain)

    def _db_text(self):
        db = self.fader.value() / 10.0
        return '-inf' if db <= -60.0 else ('%+.1f dB' % db)

    def _engine(self):
        eng = self.app.engine
        return eng if (eng and hasattr(eng, 'set_master_gain')) else None

    def _on_gain(self, ticks):
        db = ticks / 10.0
        lin = 0.0 if db <= -60.0 else 10.0 ** (db / 20.0)
        self.app.state.master_gain = lin
        self.gain_label.setText(self._db_text())
        eng = self._engine()
        if eng:
            eng.set_master_gain(lin)
        self.app.state.notify('master')

    def _on_limiter(self, checked):
        self.app.state.master_limiter = bool(checked)
        eng = self._engine()
        if eng:
            eng.set_master_limiter(bool(checked))
        self.app.state.notify('master')

    def update_meter(self):
        eng = self._engine()
        if not eng:
            return
        m = getattr(eng, 'master_meter', None)
        if m:
            self.meter.set_levels(float(m.get('peak_l', 0.0)),
                                  float(m.get('peak_r', 0.0)),
                                  float(m.get('gr', 1.0)))

    def refresh(self):
        self.fader.blockSignals(True)
        self.fader.setValue(self._db_to_ticks(self._cur_db()))
        self.fader.blockSignals(False)
        self.gain_label.setText(self._db_text())
        self.lim_btn.blockSignals(True)
        self.lim_btn.setChecked(bool(self.app.state.master_limiter))
        self.lim_btn.blockSignals(False)


class MixerView(QWidget):
    """Scrollable row of per-track strips followed by the master strip."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self._strips = []
        self._track_ids = []
        self.master_strip = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer.addWidget(self.scroll)

        self._container = QWidget()
        self._row = QHBoxLayout(self._container)
        self._row.setContentsMargins(6, 6, 6, 6)
        self._row.setSpacing(6)
        self._row.setAlignment(Qt.AlignLeft)
        self.scroll.setWidget(self._container)

        self.refresh()

    def refresh(self):
        # Only rebuild when the track list itself changes; otherwise just sync
        # values so an in-progress fader drag isn't destroyed mid-gesture.
        ids = ([('m', t.id) for t in self.state.tracks] +
               [('b', bt.id) for bt in self.state.beat_tracks])
        if ids == self._track_ids and self.master_strip is not None:
            for strip in self._strips:
                strip.sync()
            self.master_strip.refresh()
            self._assign_meter_channels()
            return

        while self._row.count():
            item = self._row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._strips = []
        self._track_ids = ids

        for t in self.state.tracks:
            strip = ChannelStrip(self.app, t, show_pan=True, kind='melodic')
            self._row.addWidget(strip)
            self._strips.append(strip)

        # Beat tracks: level (velocity scale) + mute/solo, no pan.
        if self.state.beat_tracks:
            sep = QFrame()
            sep.setFrameShape(QFrame.VLine)
            sep.setStyleSheet('color:#3a3a4a;')
            self._row.addWidget(sep)
            for bt in self.state.beat_tracks:
                strip = ChannelStrip(self.app, bt, show_pan=False, kind='beat')
                self._row.addWidget(strip)
                self._strips.append(strip)

        if not self.state.tracks and not self.state.beat_tracks:
            empty = QLabel('No tracks. Add a track to see channel strips.')
            empty.setStyleSheet('color:#778; padding:12px;')
            self._row.addWidget(empty)

        self._row.addStretch(1)
        self.master_strip = MasterStrip(self.app)
        self._row.addWidget(self.master_strip)
        self._assign_meter_channels()

    def _assign_meter_channels(self):
        """Light a strip's meter only when its track uniquely owns a mixer
        input channel (graph-mode routing). Shared/ambiguous/unrouted → off."""
        sg = getattr(self.state, 'signal_graph', None)
        chan_map = sg.track_mixer_channels() if sg is not None else {}
        counts = {}
        for ch in chan_map.values():
            if ch is not None and ch >= 0:
                counts[ch] = counts.get(ch, 0) + 1
        for strip in self._strips:
            ch = chan_map.get(f"track_{strip.track.id}")
            unique = ch is not None and ch >= 0 and counts.get(ch, 0) == 1
            strip.set_meter_channel(ch if unique else None)

    def update_meter(self):
        if self.master_strip:
            self.master_strip.update_meter()
        eng = self.app.engine
        m = getattr(eng, 'master_meter', None) if eng else None
        channels = m.get('channels') if isinstance(m, dict) else None
        if channels:
            for strip in self._strips:
                strip.update_level(channels)
