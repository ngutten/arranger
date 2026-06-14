"""Piano roll editor - note editing on a pitch/time grid."""

import time
import math

from PySide6.QtWidgets import (QFrame, QWidget, QScrollArea, QLabel, QPushButton,
                                QComboBox, QSlider, QVBoxLayout, QHBoxLayout,
                                QLineEdit, QMenu)
from PySide6.QtCore import Qt, QRect, QPoint, QPointF, QRectF
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QFont, QKeyEvent,
                            QPainterPath, QCursor, QAction, QActionGroup)

from ..state import NOTE_NAMES, scale_set, vel_color, Note
from ..clipboard import NoteClipboard
from ..core.curve_utils import interpolate_curve
from ..ops.variations import resolve_variation, compute_split_baselines
from ..song_plugins.analysis.chord_voicings import (
    QUALITIES, TIER3_EXTENSIONS, TIER4_ALTERATIONS,
    chord_label as cv_chord_label, roman_label as cv_roman_label,
    default_spec,
)
from ..song_plugins.builtin.chordify import (
    CHORD_ROOT_TAG, CHORD_VOICING_TAG, ChordifyPlugin,
)


def _state_has_chord_roots(state) -> bool:
    """True if any note in any pattern or variation carries a chord_root tag.

    Cheap O(N) walk; used to skip chordify cost when no roots exist.
    """
    for pat in state.patterns:
        for n in pat.notes:
            if n.tags and CHORD_ROOT_TAG in n.tags:
                return True
    for var in state.variations:
        for a in var.additions:
            if a.tags and CHORD_ROOT_TAG in a.tags:
                return True
    return False

class PianoRoll(QFrame):
    """Piano roll editor with piano keys, note grid, and velocity lane."""

    NH = 14    # note row height
    BW = 80    # pixels per beat
    LO = 24    # lowest pitch displayed
    HI = 96    # highest pitch displayed

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state

        # Interaction state
        self._drag_note = None
        self._drag_offset_x = 0
        self._drag_start_pos = None  # QPoint to track initial click position for deadzone
        self._resize_note = None
        self._selected = set()
        
        # New interaction states
        self._marquee_start = None  # QPoint for marquee selection
        self._ghost_notes = []  # List of Note objects in ghost/paste mode
        self._ghost_offset = None  # (dx, dy) offset from original positions
        self._note_clipboard = NoteClipboard()

        # Bend tool state
        self._bend_drag_note = None   # Note currently being edited in bend mode
        self._bend_drag_point_idx = None  # index of control point being dragged, or None

        # Bottom-lane mode: 'velocity' or a note-attr id (e.g. 'attack').
        # The set of available attr lanes is derived from the synths present in
        # the current signal graph (see _refresh_lane_options).
        self._active_lane = 'velocity'
        self._active_attr_decl = None  # decl dict for the active attr lane, if any

        # MIDI recording state
        self._rec_midi_in = None       # rtmidi.MidiIn instance while armed/recording
        self._rec_notes = {}           # {pitch: start_time} for open note-ons
        self._rec_events = []          # [(start_beat, duration, pitch, velocity)]
        self._rec_start_time = None    # time.monotonic() when first note landed
        self._rec_armed = False        # True = waiting for first note-on
        self._rec_recording = False    # True = recording in progress

        # Variation editing cache
        self._var_resolved_notes = None  # cached resolved notes for current variation
        self._var_note_categories = {}   # note_id -> category string

        # Fine-edit loupe state (Alt-held magnifier with scaled cursor deltas)
        self._alt_loupe = False
        self._loupe_anchor = None        # (x, y) floats — visual center of loupe
        self._loupe_virtual = None       # (x, y) floats — virtual cursor pos (edit coords)
        self._loupe_scale = 4.0          # adaptive zoom factor
        self._loupe_radius = 90          # on-screen radius in px
        self._last_real_pos = None       # QPoint: last raw cursor pos in grid widget

        self._build()

    # -- Variation editing helpers --

    def _is_variation_mode(self):
        """Check if we're editing a variation."""
        return self.state.sel_variation is not None

    def _get_edit_pattern(self):
        """Get the pattern being edited (parent pattern for variations)."""
        if self._is_variation_mode():
            var = self.state.find_variation(self.state.sel_variation)
            if var:
                return self.state.find_pattern(var.parent_id)
            return None
        return self.state.find_pattern(self.state.sel_pat)

    def _get_edit_notes(self):
        """Get the notes to display/edit. For variations, returns resolved notes."""
        if self._is_variation_mode():
            # During drag/resize, return cached notes to avoid re-resolving
            if (self._drag_note or self._resize_note) and self._var_resolved_notes:
                return self._var_resolved_notes
            var = self.state.find_variation(self.state.sel_variation)
            if var:
                self._var_resolved_notes = resolve_variation(self.state, var.id)
                self._update_note_categories(var)
                return self._var_resolved_notes
            return []
        pat = self.state.find_pattern(self.state.sel_pat)
        return pat.notes if pat else []

    def _update_note_categories(self, var):
        """Categorize notes for color-coding in variation mode."""
        self._var_note_categories = {}
        mod_ids = {m.note_id for m in var.modifications}
        split_ids = set()
        for s in var.splits:
            split_ids.add(s.note_id)
            split_ids.add(s.right_note_id)
        added_ids = {a.note_id for a in var.additions}
        added_map = {a.note_id: a for a in var.additions}

        pat = self.state.find_pattern(var.parent_id)
        parent_ids = {n.note_id for n in pat.notes} if pat else set()

        for nid in mod_ids | split_ids:
            self._var_note_categories[nid] = 'modified'
        for nid, added in added_map.items():
            if added.ref_note_id:
                self._var_note_categories[nid] = 'bound'
            else:
                self._var_note_categories[nid] = 'free'

    def _note_border_color(self, note):
        """Get border color for a note based on its variation category."""
        if not self._is_variation_mode():
            return None
        cat = self._var_note_categories.get(note.note_id)
        if cat == 'modified':
            return QColor('#e6a817')  # amber
        elif cat == 'bound':
            return QColor('#00bcd4')  # teal
        elif cat == 'free':
            return QColor('#e040fb')  # magenta
        return None

    def _find_note_by_id(self, note_id):
        """Find a note in the current resolved notes by note_id."""
        if self._var_resolved_notes:
            for n in self._var_resolved_notes:
                if n.note_id == note_id:
                    return n
        return None

    def _persist_variation_edits(self):
        """After drag/resize, persist resolved note positions as NoteDelta.

        Notes fall into four categories with different delta baselines:
          - Added note: update AddedNote fields directly
          - Split note: baseline from compute_split_baselines (handles chained splits)
          - Regular parent note: baseline = parent note values → var.modifications
        """
        var = self.state.find_variation(self.state.sel_variation)
        if not var:
            return
        pat = self.state.find_pattern(var.parent_id)
        if not pat:
            return

        from ..ops.variations import variation_modify_note
        parent_by_id = {n.note_id: n for n in pat.notes}
        added_ids = {a.note_id for a in var.additions}

        # Build split lookup: note_id → (SplitOp, 'left'|'right')
        # Later splits overwrite earlier ones, so a chained note gets its
        # most-direct owning split.
        split_lookup = {}
        for sp in var.splits:
            split_lookup[sp.note_id] = (sp, 'left')
            split_lookup[sp.right_note_id] = (sp, 'right')

        # Compute baselines for all split notes (handles chained splits)
        split_baselines = compute_split_baselines(var, pat)

        notes = self._var_resolved_notes or []

        for n in notes:
            if n.note_id in added_ids:
                # Update AddedNote directly
                for a in var.additions:
                    if a.note_id == n.note_id:
                        a.pitch = n.pitch
                        a.start = n.start
                        a.duration = n.duration
                        a.velocity = n.velocity
                        a.attrs = dict(n.attrs or {})
                        # Update binding offsets relative to current resolved ref
                        if a.ref_note_id:
                            ref = next((rn for rn in notes if rn.note_id == a.ref_note_id), None)
                            if ref:
                                a.ref_pitch_offset = n.pitch - ref.pitch
                                a.ref_start_offset = n.start - ref.start
                                a.ref_dur_offset = n.duration - ref.duration
                        break
            elif n.note_id in split_baselines:
                base_start, base_dur, base_pitch, base_vel = split_baselines[n.note_id]

                d_start = n.start - base_start
                d_duration = n.duration - base_dur
                d_pitch = n.pitch - base_pitch
                d_velocity = n.velocity - base_vel

                if abs(d_start) > 1e-6 or abs(d_duration) > 1e-6 or d_pitch or d_velocity:
                    # variation_modify_note routes to sp.left_delta/right_delta
                    variation_modify_note(var, n.note_id,
                                          d_start=d_start, d_duration=d_duration,
                                          d_pitch=d_pitch, d_velocity=d_velocity)
                else:
                    # Clear delta if note matches baseline
                    if n.note_id in split_lookup:
                        sp, side = split_lookup[n.note_id]
                        if side == 'left':
                            sp.left_delta = None
                        else:
                            sp.right_delta = None
            elif n.note_id in parent_by_id:
                pn = parent_by_id[n.note_id]
                d_start = n.start - pn.start
                d_duration = n.duration - pn.duration
                d_pitch = n.pitch - pn.pitch
                d_velocity = n.velocity - pn.velocity
                # Only create/update delta if something changed
                if abs(d_start) > 1e-6 or abs(d_duration) > 1e-6 or d_pitch or d_velocity:
                    variation_modify_note(var, n.note_id,
                                          d_start=d_start, d_duration=d_duration,
                                          d_pitch=d_pitch, d_velocity=d_velocity)
                # Per-note attrs: store an override delta when they diverge
                # from the parent (None on the delta = inherit).
                if (n.attrs or {}) != (pn.attrs or {}):
                    variation_modify_note(var, n.note_id, attrs=dict(n.attrs or {}))

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header bar
        hdr = QFrame()
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(8, 4, 8, 4)

        self.name_label = QLabel('No pattern')
        self.name_label.setFont(QFont('TkDefaultFont', 9))
        hdr_layout.addWidget(self.name_label)

        preview_btn = QPushButton('Preview')
        preview_btn.clicked.connect(self.app.preview_pattern)
        hdr_layout.addWidget(preview_btn)

        hdr_layout.addStretch()

        # Note length
        hdr_layout.addWidget(QLabel('Len'))
        self.note_len_cb = QComboBox()
        self.note_len_cb.addItems(['snap', 'last', '1/16', '1/8', '1/4',
                                    '1/2', '1', '2', '4'])
        self.note_len_cb.setCurrentText(self.state.note_len)
        self.note_len_cb.currentTextChanged.connect(self._on_note_len)
        hdr_layout.addWidget(self.note_len_cb)

        # Tool buttons - Edit, Slice, and Bend
        self.edit_btn = QPushButton('Edit')
        self.edit_btn.setCheckable(True)
        self.edit_btn.clicked.connect(lambda: self._set_tool('edit'))
        hdr_layout.addWidget(self.edit_btn)

        self.slice_btn = QPushButton('Slice')
        self.slice_btn.setCheckable(True)
        self.slice_btn.clicked.connect(lambda: self._set_tool('slice'))
        hdr_layout.addWidget(self.slice_btn)

        self.bend_btn = QPushButton('Bend')
        self.bend_btn.setCheckable(True)
        self.bend_btn.clicked.connect(lambda: self._set_tool('bend'))
        hdr_layout.addWidget(self.bend_btn)

        self.lyrics_btn = QPushButton('Lyrics')
        self.lyrics_btn.setCheckable(True)
        self.lyrics_btn.clicked.connect(lambda: self._set_tool('lyrics'))
        hdr_layout.addWidget(self.lyrics_btn)

        # Velocity slider
        hdr_layout.addWidget(QLabel('Vel'))
        self.vel_slider = QSlider(Qt.Horizontal)
        self.vel_slider.setRange(1, 127)
        self.vel_slider.setValue(self.state.default_vel)
        self.vel_slider.valueChanged.connect(self._on_vel_change)
        self.vel_slider.setMaximumWidth(100)
        hdr_layout.addWidget(self.vel_slider)

        self.vel_label = QLabel('100')
        self.vel_label.setMinimumWidth(30)
        hdr_layout.addWidget(self.vel_label)

        # Bottom-lane selector: Velocity (default) or a per-note attribute lane
        # advertised by a synth in the current signal graph.
        hdr_layout.addWidget(QLabel('Lane'))
        self.lane_cb = QComboBox()
        self.lane_cb.setMinimumWidth(90)
        self.lane_cb.currentIndexChanged.connect(self._on_lane_change)
        hdr_layout.addWidget(self.lane_cb)

        # MIDI record button
        self.rec_btn = QPushButton('Rec')
        self.rec_btn.setCheckable(True)
        self.rec_btn.setStyleSheet(
            'QPushButton { color: #aaa; }'
            'QPushButton:checked { background-color: #c0392b; color: #fff; }'
            'QPushButton:disabled { color: #444; }'
        )
        self.rec_btn.setToolTip('Record from MIDI input (arm — starts on first note)')
        self.rec_btn.clicked.connect(self._toggle_rec)
        hdr_layout.addWidget(self.rec_btn)
        self._update_rec_btn_enabled()

        layout.addWidget(hdr)

        grid_container = QFrame()
        grid_body = QHBoxLayout(grid_container)
        grid_body.setContentsMargins(0, 0, 0, 0)
        grid_body.setSpacing(0)
        
        self.keys_scroll = QScrollArea()
        self.keys_scroll.setFixedWidth(44)
        self.keys_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.keys_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.keys_scroll.setWidgetResizable(False)
        self.keys_scroll.setFrameShape(QFrame.NoFrame) # Remove border for tighter fit
        
        self.keys_widget = PianoKeysWidget(self)
        self.keys_scroll.setWidget(self.keys_widget)
        grid_body.addWidget(self.keys_scroll)

        # Note canvas scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        
        self.grid_widget = PianoGridWidget(self)
        self.scroll_area.setWidget(self.grid_widget)
        grid_body.addWidget(self.scroll_area)        

        layout.addWidget(grid_container, 1) # Priority 1 to expand

        # Add the velocity lane directly to the bottom of the main layout
        # This keeps it outside the horizontal alignment of the keys
        self.vel_widget = VelocityWidget(self)
        self.vel_widget.setFixedHeight(50)
        
        # Add a small left margin to the velocity lane to align it with the grid,
        # skipping the 44px width of the piano keys.
        vel_layout_wrapper = QHBoxLayout()
        vel_layout_wrapper.setContentsMargins(44, 0, 0, 0) 
        vel_layout_wrapper.addWidget(self.vel_widget)
        layout.addLayout(vel_layout_wrapper)

        # Sync scrolling (Grid -> Keys, Grid -> Velocity)
        self.scroll_area.verticalScrollBar().valueChanged.connect(
            self.keys_scroll.verticalScrollBar().setValue
        )
        self.keys_scroll.verticalScrollBar().valueChanged.connect(
            self.scroll_area.verticalScrollBar().setValue
        )
        self.scroll_area.horizontalScrollBar().valueChanged.connect(
            self.vel_widget.update
        )
        # When the grid's horizontal scrollbar appears/disappears, its vertical
        # viewport changes by the scrollbar height. Pad the keys widget to match
        # so both vertical scrollbars share the same range and stay aligned.
        self.scroll_area.horizontalScrollBar().rangeChanged.connect(
            self._sync_keys_height
        )

        self.setFocusPolicy(Qt.StrongFocus)
        self._refresh_lane_options()

    def _on_note_len(self, text):
        self.state.note_len = text

    # -- Bottom-lane (velocity / note-attr) handling ----------------------

    def _refresh_lane_options(self):
        """Rebuild the lane dropdown from the synths in the current graph.

        Always offers 'Velocity'; appends one entry per per-note attribute any
        synth in the signal graph consumes. Preserves the active lane if it's
        still available, else falls back to velocity.
        """
        try:
            from ..graph_editor import note_attrs_in_graph
            attrs = note_attrs_in_graph(getattr(self.state, 'signal_graph', None))
        except Exception:
            attrs = []
        # Skip the rebuild (and its signal churn) when the lane set is unchanged.
        sig = tuple(a['id'] for a in attrs)
        if sig == getattr(self, '_lane_sig', None):
            return
        self._lane_sig = sig
        self._lane_attrs = {a['id']: a for a in attrs}

        cb = self.lane_cb
        cb.blockSignals(True)
        cb.clear()
        cb.addItem('Velocity', 'velocity')
        for a in attrs:
            cb.addItem(a.get('display_name', a['id']), a['id'])
        # Restore selection if still present
        want = self._active_lane
        idx = cb.findData(want)
        if idx < 0:
            idx = 0
            self._active_lane = 'velocity'
            self._active_attr_decl = None
        cb.setCurrentIndex(idx)
        self._active_attr_decl = self._lane_attrs.get(self._active_lane)
        cb.blockSignals(False)

    def _on_lane_change(self, _idx):
        self._active_lane = self.lane_cb.currentData() or 'velocity'
        self._active_attr_decl = getattr(self, '_lane_attrs', {}).get(self._active_lane)
        if hasattr(self, 'vel_widget'):
            self.vel_widget.update()

    def _set_tool(self, tool):
        self.state.tool = tool
        self._update_tool_buttons()

    def _update_tool_buttons(self):
        self.edit_btn.setChecked(self.state.tool == 'edit')
        self.slice_btn.setChecked(self.state.tool == 'slice')
        self.bend_btn.setChecked(self.state.tool == 'bend')
        self.lyrics_btn.setChecked(self.state.tool == 'lyrics')

    def _on_vel_change(self, value):
        self.vel_label.setText(str(value))
        self.state.default_vel = value

        # If notes are selected, update their velocities
        if self._selected:
            notes = self._get_edit_notes()
            if notes:
                for idx in self._selected:
                    if 0 <= idx < len(notes):
                        notes[idx].velocity = value
                if self._is_variation_mode():
                    self._persist_variation_edits()
                self.refresh()

    def _snap(self, beat):
        return int(beat / self.state.snap) * self.state.snap

    # -- Fine-edit loupe helpers --

    def _xy(self, event):
        """Return the logical (x, y) for this event. In loupe mode, use the
        virtual cursor so edits track the magnified view, not the real mouse."""
        if self._alt_loupe and self._loupe_virtual is not None:
            return self._loupe_virtual
        p = event.pos()
        return (float(p.x()), float(p.y()))

    def _compute_loupe_scale(self, x, y):
        """Pick a zoom factor so the smallest note near (x, y) renders at a
        comfortable size. Capped at 6x; minimum 2x."""
        search_px = 120
        beat_lo = (x - search_px) / self.BW
        beat_hi = (x + search_px) / self.BW
        pitch_lo = self.HI - int((y + search_px) / self.NH)
        pitch_hi = self.HI - int((y - search_px) / self.NH)
        notes = self._get_edit_notes()
        min_dur_px = None
        for n in notes:
            if (pitch_lo <= n.pitch <= pitch_hi
                    and n.start < beat_hi
                    and n.start + n.duration > beat_lo):
                dur_px = n.duration * self.BW
                if min_dur_px is None or dur_px < min_dur_px:
                    min_dur_px = dur_px
        if min_dur_px is None or min_dur_px >= 20:
            return 3.0
        return max(2.0, min(6.0, 20.0 / max(1.0, min_dur_px)))

    def _activate_loupe(self):
        """Turn on fine-edit loupe, anchored at the current cursor position."""
        if self._alt_loupe or self._last_real_pos is None:
            return
        x = float(self._last_real_pos.x())
        y = float(self._last_real_pos.y())
        self._alt_loupe = True
        self._loupe_anchor = (x, y)
        self._loupe_virtual = (x, y)
        self._loupe_scale = self._compute_loupe_scale(x, y)
        # Hide the real OS cursor — the virtual crosshair at the loupe centre
        # replaces it. Avoids the "cursor drifts off the loupe" confusion.
        self.grid_widget.setCursor(Qt.BlankCursor)
        self.grid_widget.update()

    def _deactivate_loupe(self):
        """Turn off fine-edit loupe. Warp the real cursor to the virtual
        position so edits continue smoothly after release."""
        if not self._alt_loupe:
            return
        if self._loupe_virtual is not None:
            vx, vy = self._loupe_virtual
            target = QPoint(int(vx), int(vy))
            global_pos = self.grid_widget.mapToGlobal(target)
            QCursor.setPos(global_pos)
            self._last_real_pos = target
        self._alt_loupe = False
        self._loupe_anchor = None
        self._loupe_virtual = None
        self.grid_widget.unsetCursor()
        self.grid_widget.update()

    def _hit_bend_point(self, x, y, radius=6):
        """Hit-test bend control points. Returns (note, point_index) or (None, -1)."""
        notes = self._get_edit_notes()
        if not notes:
            return None, -1
        for n in notes:
            if not n.bend:
                continue
            note_y_top = (self.HI - n.pitch) * self.NH
            note_y_center = note_y_top + self.NH // 2
            for i, (beat_off, semitones) in enumerate(n.bend):
                px = (n.start + beat_off) * self.BW
                # semitones in [-2, 2] -> y offset within the note row, ±2 rows
                py = note_y_center - int(semitones / 2.0 * self.NH * 2)
                if abs(x - px) <= radius and abs(y - py) <= radius:
                    return n, i
        return None, -1

    # Resize handle width in pixels, capped so it never swallows the whole
    # note. 6px feels right for pointer hit-testing; the cap prevents short
    # notes from being resize-only (previously 0.15 beats covered a 1/16).
    RESIZE_HANDLE_PX = 6.0
    RESIZE_HANDLE_MAX_FRAC = 0.35

    def _resize_handle_beats(self, note):
        """How many beats from the end of `note` count as the resize zone."""
        note_px = note.duration * self.BW
        handle_px = min(self.RESIZE_HANDLE_PX,
                        max(2.0, note_px * self.RESIZE_HANDLE_MAX_FRAC))
        return handle_px / self.BW

    def _hit_note(self, x, y):
        """Hit test for notes. Returns (note, index, is_resize_handle)."""
        notes = self._get_edit_notes()
        if not notes:
            return None, -1, False
        pitch = self.HI - int(y / self.NH)
        beat = x / self.BW
        for i in range(len(notes) - 1, -1, -1):
            n = notes[i]
            if n.pitch == pitch and n.start <= beat < n.start + n.duration:
                handle_beats = self._resize_handle_beats(n)
                is_resize = beat > n.start + n.duration - handle_beats
                return n, i, is_resize
        return None, -1, False
    
    def _coords_to_beat_pitch(self, x, y):
        """Convert pixel coordinates to (beat, pitch)."""
        pitch = self.HI - int(y / self.NH)
        beat = x / self.BW
        return beat, pitch

    def refresh(self):
        """Redraw the piano roll."""
        pat = self._get_edit_pattern()

        # Update header
        if self._is_variation_mode():
            var = self.state.find_variation(self.state.sel_variation)
            if var and pat:
                self.name_label.setText(
                    f'{var.name} (var of {pat.name}, {pat.length}b)')
            else:
                self.name_label.setText('No variation')
        elif pat:
            self.name_label.setText(
                f'{pat.name} ({pat.length}b, {pat.key} {pat.scale})')
        else:
            self.name_label.setText('No pattern')

        self._update_tool_buttons()
        self._refresh_lane_options()

        # Update widget sizes
        pitch_range = self.HI - self.LO + 1
        total_h = pitch_range * self.NH
        beats = pat.length if pat else 16
        total_w = int(beats * self.BW)

        self.grid_widget.setMinimumSize(total_w, total_h)
        self.keys_widget.setMinimumSize(44, total_h + self._hscroll_pad())

        self.keys_widget.update()
        self.grid_widget.update()
        self.vel_widget.update()

    def _hscroll_pad(self):
        hs = self.scroll_area.horizontalScrollBar()
        return hs.sizeHint().height() if hs.maximum() > 0 else 0

    def _sync_keys_height(self):
        pitch_range = self.HI - self.LO + 1
        total_h = pitch_range * self.NH + self._hscroll_pad()
        self.keys_widget.setMinimumSize(44, total_h)

    def clear_selection(self):
        self._selected.clear()
        self.refresh()
    
    def _copy_to_clipboard(self):
        """Copy selected notes to clipboard."""
        notes = self._get_edit_notes()
        if not notes or not self._selected:
            return
        notes_to_copy = [notes[i] for i in sorted(self._selected)
                         if 0 <= i < len(notes)]
        self._note_clipboard.copy(notes_to_copy)

    def _cut_to_clipboard(self):
        """Cut selected notes (copy + delete), enter ghost mode."""
        if not self._selected:
            return

        self._copy_to_clipboard()
        self._delete_selected()

        # Enter ghost mode immediately with clipboard contents
        self._ghost_notes = self._note_clipboard.paste()
        self._ghost_offset = (0, 0)

        self.refresh()
    
    def _paste_from_clipboard(self):
        """Enter ghost mode with clipboard contents."""
        if not self._note_clipboard.has_data():
            return
        self._ghost_notes = self._note_clipboard.paste()
        self._ghost_offset = (0, 0)
        self.refresh()
    
    def _duplicate_selection(self):
        """Duplicate selected notes with smart offset."""
        notes = self._get_edit_notes()
        if not notes or not self._selected:
            return

        if self._is_variation_mode():
            var = self.state.find_variation(self.state.sel_variation)
            if not var:
                return
            from ..ops.variations import variation_add_note
            offset_beats = max(self.state.snap, 1.0)
            new_indices = set()
            for idx in sorted(self._selected):
                if 0 <= idx < len(notes):
                    n = notes[idx]
                    variation_add_note(self.state, var, n.pitch,
                                       n.start + offset_beats, n.duration, n.velocity,
                                       [list(p) for p in n.bend] if n.bend else None,
                                       n.lyric)
            self._selected = set()
        else:
            pat = self.state.find_pattern(self.state.sel_pat)
            if not pat:
                return
            self._copy_to_clipboard()
            offset_beats = max(self.state.snap, 1.0)
            from ..ops.note_edit import duplicate_notes
            self._selected = duplicate_notes(
                pat, self._selected, self._note_clipboard.notes, offset_beats)

        self.state.notify('note_add')
        self.refresh()

    def _commit_ghost_notes(self, mouse_x, mouse_y):
        """Commit ghost notes to the pattern at current mouse position."""
        pat = self._get_edit_pattern()
        if not pat or not self._ghost_notes:
            return

        beat, pitch = self._coords_to_beat_pitch(mouse_x, mouse_y)

        if self._is_variation_mode():
            var = self.state.find_variation(self.state.sel_variation)
            if var:
                from ..ops.variations import variation_add_note
                min_s = min(n.start for n in self._ghost_notes)
                min_p = min(n.pitch for n in self._ghost_notes)
                snap_beat = self._snap(beat)
                for n in self._ghost_notes:
                    new_start = max(0, snap_beat + (n.start - min_s))
                    new_pitch = max(self.LO, min(self.HI, pitch + (n.pitch - min_p)))
                    variation_add_note(self.state, var, new_pitch, new_start,
                                       n.duration, n.velocity,
                                       [list(p) for p in n.bend] if n.bend else None,
                                       n.lyric)
        else:
            from ..ops.note_edit import commit_ghost_notes
            self._selected = commit_ghost_notes(
                pat, self._ghost_notes, beat, pitch,
                self._snap, self.LO, self.HI)

        self._ghost_notes = []
        self._ghost_offset = None

        self.state.notify('note_add')
        self.refresh()
    
    def _cancel_ghost_mode(self):
        """Cancel ghost mode without placing notes."""
        self._ghost_notes = []
        self._ghost_offset = None
        self.refresh()
    
    def _delete_selected(self):
        """Delete all selected notes."""
        if not self._selected:
            return

        if self._is_variation_mode():
            var = self.state.find_variation(self.state.sel_variation)
            if not var:
                return
            notes = self._get_edit_notes()
            from ..ops.variations import variation_delete_note, variation_remove_added_note
            added_ids = {a.note_id for a in var.additions}
            for idx in sorted(self._selected, reverse=True):
                if 0 <= idx < len(notes):
                    nid = notes[idx].note_id
                    if nid in added_ids:
                        variation_remove_added_note(var, nid)
                    else:
                        variation_delete_note(var, nid)
            self._selected = set()
        else:
            pat = self.state.find_pattern(self.state.sel_pat)
            if not pat:
                return
            from ..ops.note_edit import delete_selected
            self._selected = delete_selected(pat, self._selected)

        self.state.notify('note_edit')
        self.refresh()
    
    def _merge_selected_notes(self):
        """Merge two selected adjacent notes at the same pitch."""
        if self._is_variation_mode():
            return  # merge not supported in variation mode
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or len(self._selected) != 2:
            return
        
        from ..ops.note_edit import merge_notes
        result = merge_notes(pat, self._selected)
        if result is not None:
            self._selected = result
            self.state.notify('note_edit')
            self.refresh()

    # ---- MIDI recording ----

    def _update_rec_btn_enabled(self):
        """Enable Rec only when a MIDI device is configured."""
        try:
            has_device = bool(
                hasattr(self.app, 'settings') and
                self.app.settings.midi_input_device
            )
        except Exception:
            has_device = False
        self.rec_btn.setEnabled(has_device)
        if not has_device:
            self.rec_btn.setToolTip('No MIDI input device configured (see Config)')

    def _toggle_rec(self, checked):
        if checked:
            self._arm_recording()
        else:
            self._stop_recording()

    def _arm_recording(self):
        """Open the MIDI port and wait for the first note-on."""
        pat = self._get_edit_pattern()
        if not pat:
            self.rec_btn.setChecked(False)
            return

        device_name = getattr(getattr(self.app, 'settings', None), 'midi_input_device', '')
        if not device_name:
            self.rec_btn.setChecked(False)
            return

        # Pause the live-preview router so we can open the same port for recording
        if hasattr(self.app, '_midi_router') and self.app._midi_router is not None:
            self.app._midi_router.stop()

        try:
            import rtmidi
            midi_in = rtmidi.MidiIn()
            ports = midi_in.get_ports()
            if device_name not in ports:
                self.rec_btn.setChecked(False)
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, 'MIDI Error',
                    f'Device "{device_name}" not found.\nCheck Config for available devices.')
                return
            midi_in.open_port(ports.index(device_name))
            midi_in.set_callback(self._midi_callback)
            midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
            self._rec_midi_in = midi_in
        except Exception as e:
            self.rec_btn.setChecked(False)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, 'MIDI Error', f'Could not open MIDI port:\n{e}')
            return

        self._rec_notes = {}
        self._rec_events = []
        self._rec_start_time = None
        self._rec_armed = True
        self._rec_recording = False
        self.rec_btn.setText('■ Stop')
        self.rec_btn.setToolTip('Recording — click to stop')

    def _midi_callback(self, event, data=None):
        """Called from rtmidi thread on each incoming MIDI message."""
        msg, _delta_t = event
        if not msg:
            return
        status = msg[0] & 0xF0
        now = time.monotonic()

        if status == 0x90 and len(msg) >= 3 and msg[2] > 0:   # note-on
            pitch, vel = msg[1], msg[2]
            if self._rec_armed and not self._rec_recording:
                self._rec_start_time = now
                self._rec_armed = False
                self._rec_recording = True
            if self._rec_recording and self._rec_start_time is not None:
                beat = (now - self._rec_start_time) / (60.0 / self.state.bpm)
                self._rec_notes[pitch] = (beat, vel)

        elif status in (0x80,) or (status == 0x90 and len(msg) >= 3 and msg[2] == 0):
            pitch = msg[1]
            if pitch in self._rec_notes and self._rec_recording:
                start_beat, vel = self._rec_notes.pop(pitch)
                if self._rec_start_time is not None:
                    end_beat = (now - self._rec_start_time) / (60.0 / self.state.bpm)
                    duration = max(0.0625, end_beat - start_beat)
                    self._rec_events.append((start_beat, duration, pitch, vel))

    def _stop_recording(self):
        """Close MIDI port and commit recorded notes to the pattern."""
        self._rec_armed = False
        self._rec_recording = False

        # Close still-open notes at current wall time
        if self._rec_start_time is not None:
            now = time.monotonic()
            for pitch, (start_beat, vel) in list(self._rec_notes.items()):
                end_beat = (now - self._rec_start_time) / (60.0 / self.state.bpm)
                duration = max(0.0625, end_beat - start_beat)
                self._rec_events.append((start_beat, duration, pitch, vel))
        self._rec_notes = {}

        if self._rec_midi_in:
            try:
                self._rec_midi_in.close_port()
            except Exception:
                pass
            self._rec_midi_in = None

        self.rec_btn.setText('Rec')
        self.rec_btn.setChecked(False)
        self.rec_btn.setToolTip('Record from MIDI input (arm — starts on first note)')

        # Resume the live-preview router now that the port is free
        if hasattr(self.app, '_restart_midi_router'):
            self.app._restart_midi_router()

        if not self._rec_events:
            return

        pat = self._get_edit_pattern()
        if not pat:
            return

        from ..state import Note

        # Shift so first note lands at beat 0
        t0 = min(s for s, d, p, v in self._rec_events)
        notes = []
        for start, dur, pitch, vel in self._rec_events:
            notes.append(Note(pitch=pitch, start=round(start - t0, 6),
                               duration=round(dur, 6), velocity=vel))

        # Expand pattern if needed, rounding up to a whole bar
        max_end = max(n.start + n.duration for n in notes)
        if max_end > pat.length:
            bar = self.state.ts_num
            pat.length = bar * (int(max_end / bar) + 1)

        pat.notes.extend(notes)
        self._rec_events = []
        self.state.notify('note_add')
        self.refresh()

    def _edit_lyric_for_note(self, note, x, y):
        """Open an inline QLineEdit at the note's position to edit its lyric."""
        note_x = int(note.start * self.BW)
        note_y = (self.HI - note.pitch) * self.NH
        note_w = max(60, int(note.duration * self.BW))

        editor = QLineEdit(self.grid_widget)
        editor.setText(note.lyric or '')
        editor.setGeometry(note_x, note_y + 1, note_w, self.NH - 2)
        editor.setFont(QFont('TkDefaultFont', 7))
        editor.setStyleSheet(
            'QLineEdit { background: #1a1a2e; color: #fff;'
            ' border: 1px solid #9b5de5; padding: 0px; }'
        )
        editor.show()
        editor.setFocus()
        editor.selectAll()

        committed = [False]

        def commit():
            if committed[0]:
                return
            committed[0] = True
            note.lyric = editor.text().strip()
            editor.deleteLater()
            self.state.notify('note_edit')
            self.refresh()

        editor.editingFinished.connect(commit)

    def keyPressEvent(self, event: QKeyEvent):
        """Handle keyboard shortcuts."""
        modifiers = event.modifiers()
        key = event.key()

        # Z — activate fine-edit loupe (magnified, slowed cursor).
        # Alt was avoided because most Linux WMs grab Alt+drag for window-move.
        if key == Qt.Key_Z and not event.isAutoRepeat():
            self._activate_loupe()
            event.accept()
            return

        # Arrow keys — nudge selected notes by snap (no modifier = move,
        # Shift = resize)
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            if self._nudge_selection(key, modifiers):
                return

        # Escape - clear selection or cancel ghost mode
        if key == Qt.Key_Escape:
            if self._ghost_notes:
                self._cancel_ghost_mode()
            else:
                self.clear_selection()
            return
        
        # Ctrl/Cmd + C - Copy
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_C:
            self._copy_to_clipboard()
            return
        
        # Ctrl/Cmd + X - Cut
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_X:
            self._cut_to_clipboard()
            return
        
        # Ctrl/Cmd + V - Paste
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_V:
            self._paste_from_clipboard()
            return
        
        # Ctrl/Cmd + D - Duplicate
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_D:
            self._duplicate_selection()
            return
        
        # Ctrl/Cmd + A - Select all
        if (modifiers & Qt.ControlModifier) and key == Qt.Key_A:
            notes = self._get_edit_notes()
            if notes:
                self._selected = set(range(len(notes)))
                self.refresh()
            return
        
        # Delete or Backspace - Delete selected
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            self._delete_selected()
            return
        
        # M - Merge selected notes
        if key == Qt.Key_M:
            self._merge_selected_notes()
            return
        
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        """Handle key releases — used to exit the loupe."""
        if event.key() == Qt.Key_Z and not event.isAutoRepeat():
            self._deactivate_loupe()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _nudge_selection(self, key, modifiers):
        """Keyboard-driven editing for selected notes. Arrow keys move by
        snap (Up/Down = pitch), Shift+Left/Right resizes duration. Returns
        True if the event was handled."""
        if not self._selected:
            return False
        notes = self._get_edit_notes()
        if not notes:
            return False

        snap = self.state.snap
        shift = bool(modifiers & Qt.ShiftModifier)
        changed = False

        for idx in self._selected:
            if not (0 <= idx < len(notes)):
                continue
            n = notes[idx]
            if key == Qt.Key_Left:
                if shift:
                    new_dur = max(snap, n.duration - snap)
                    if new_dur != n.duration:
                        n.duration = new_dur
                        changed = True
                else:
                    new_start = max(0, n.start - snap)
                    if new_start != n.start:
                        n.start = new_start
                        changed = True
            elif key == Qt.Key_Right:
                if shift:
                    n.duration = n.duration + snap
                    changed = True
                else:
                    n.start = n.start + snap
                    changed = True
            elif key == Qt.Key_Up and not shift:
                new_pitch = min(self.HI, n.pitch + 1)
                if new_pitch != n.pitch:
                    n.pitch = new_pitch
                    changed = True
            elif key == Qt.Key_Down and not shift:
                new_pitch = max(self.LO, n.pitch - 1)
                if new_pitch != n.pitch:
                    n.pitch = new_pitch
                    changed = True

        if changed:
            if self._is_variation_mode():
                self._persist_variation_edits()
            self.state.notify('note_edit')
            self.refresh()
        return True

    def _on_click(self, event):
        """Handle left mouse button press."""
        pat = self._get_edit_pattern()
        if not pat:
            return

        x, y = self._xy(event)
        beat, pitch = self._coords_to_beat_pitch(x, y)
        modifiers = event.modifiers()

        # Store initial click position for deadzone check
        self._drag_start_pos = QPoint(int(x), int(y))

        # If in ghost mode, commit the paste
        if self._ghost_notes:
            self._commit_ghost_notes(x, y)
            return

        if self.state.tool == 'edit':
            n, i, is_resize = self._hit_note(x, y)

            # Clear arranger selection when interacting with piano roll
            self.app.arrangement.selected_placements = []
            self.app.arrangement.selected_beat_placements = []

            # Shift modifier - marquee select or multi-select
            if modifiers & Qt.ShiftModifier:
                if n:
                    # Multi-select toggle
                    if i in self._selected:
                        self._selected.discard(i)
                    else:
                        self._selected.add(i)
                    self.refresh()
                else:
                    # Start marquee selection
                    self._marquee_start = QPoint(int(x), int(y))
            else:
                # Regular click
                if n and is_resize:
                    # Resize existing note (but will check deadzone in drag).
                    # Also select so that a click-release always selects —
                    # previously clicking on the handle of a short note could
                    # fail to ever select it.
                    if i not in self._selected:
                        self._selected = {i}
                    self._resize_note = n
                    self.refresh()
                elif n:
                    # Select and prepare to drag
                    if i not in self._selected:
                        self._selected = {i}
                    self._drag_note = n
                    self._drag_offset_x = beat - n.start
                    self.refresh()
                else:
                    # Create new note
                    self._selected.clear()
                    vel = self.vel_slider.value()
                    dur = self.state.snap

                    if self.state.note_len == 'snap':
                        dur = self.state.snap
                    elif self.state.note_len == 'last':
                        dur = self.state.last_note_len
                    else:
                        text = self.state.note_len
                        try:
                            if '/' in text:
                                parts = text.split('/')
                                dur = float(parts[0]) / float(parts[1])
                            else:
                                dur = float(text)
                        except ValueError:
                            dur = self.state.snap

                    snap_value = min(self.state.snap, dur)
                    snap_beat = int(beat / snap_value) * snap_value

                    if self._is_variation_mode():
                        var = self.state.find_variation(self.state.sel_variation)
                        if var:
                            from ..ops.variations import variation_add_note
                            variation_add_note(self.state, var, pitch, snap_beat, dur, vel)
                    else:
                        nn = Note(pitch=pitch, start=snap_beat, duration=dur, velocity=vel,
                                  note_id=self.state.new_id())
                        pat.notes.append(nn)
                    self.state.last_note_len = dur
                    self.app.play_note(pitch, vel, track_id=self.state.sel_trk)
                    self.state.notify('note_add')
                    self.refresh()

        elif self.state.tool == 'slice':
            n, i, _ = self._hit_note(x, y)
            if n:
                # Split the note at the current beat position
                if n.start < beat < n.start + n.duration:
                    if self._is_variation_mode():
                        var = self.state.find_variation(self.state.sel_variation)
                        if var and n.note_id:
                            split_offset = beat - n.start
                            added_ids = {a.note_id for a in var.additions}
                            if n.note_id in added_ids:
                                from ..ops.variations import variation_split_added_note
                                variation_split_added_note(
                                    self.state, var, n.note_id, split_offset)
                            else:
                                from ..ops.variations import variation_record_split
                                variation_record_split(
                                    self.state, var, n.note_id, split_offset)
                    else:
                        # Create new note for the right portion
                        right_note = Note(
                            pitch=n.pitch,
                            start=beat,
                            duration=(n.start + n.duration) - beat,
                            velocity=n.velocity,
                            note_id=self.state.new_id(),
                        )
                        # Shorten the left portion, strip its bend (can't cleanly split a curve)
                        n.duration = beat - n.start
                        n.bend = []
                        # Add the right portion
                        pat.notes.append(right_note)

                    self.state.notify('note_edit')
                    self.refresh()

        elif self.state.tool == 'bend':
            # Hit-test existing control points first
            bn, bi = self._hit_bend_point(x, y)
            if bn is not None:
                # Start dragging this control point
                self._bend_drag_note = bn
                self._bend_drag_point_idx = bi
            else:
                # Click on a note body — add new control point
                n, i, _ = self._hit_note(x, y)
                if n:
                    beat_off = max(0.0, min(n.duration, beat - n.start))
                    note_y_center = (self.HI - n.pitch) * self.NH + self.NH // 2
                    semitones = max(-2.0, min(2.0, -(y - note_y_center) / (self.NH * 2.0) * 2.0))
                    n.bend.append([beat_off, round(semitones, 3)])
                    n.bend.sort(key=lambda p: p[0])
                    # Start dragging the new point
                    self._bend_drag_note = n
                    self._bend_drag_point_idx = next(
                        k for k, p in enumerate(n.bend) if abs(p[0] - beat_off) < 1e-6)
                    self.app.engine.mark_dirty()
                    self.state.notify('note_edit')
                    self.refresh()

        elif self.state.tool == 'lyrics':
            n, i, _ = self._hit_note(x, y)
            if n:
                self._edit_lyric_for_note(n, x, y)

    def _on_drag(self, event):
        """Handle mouse drag."""
        pat = self._get_edit_pattern()
        if not pat:
            return

        x, y = self._xy(event)
        beat, pitch = self._coords_to_beat_pitch(x, y)

        # Update ghost note preview
        if self._ghost_notes:
            self.grid_widget.update()
            return

        # Marquee selection
        if self._marquee_start:
            # Update marquee rectangle (drawing happens in paintEvent)
            self.grid_widget.update()
            return

        if self.state.tool == 'edit':
            # Check deadzone for resize operations (10 pixels)
            if self._resize_note and self._drag_start_pos:
                dist = (QPoint(int(x), int(y)) - self._drag_start_pos).manhattanLength()
                if dist < 10:
                    # Still in deadzone, don't resize yet
                    return
                else:
                    # Past deadzone, clear the start position so we don't check again
                    self._drag_start_pos = None

            if self._resize_note:
                self._resize_note.duration = max(self.state.snap,
                                                  self._snap(beat - self._resize_note.start))
                self.refresh()
            elif self._drag_note:
                # Check deadzone for drag operations
                if self._drag_start_pos:
                    dist = (QPoint(int(x), int(y)) - self._drag_start_pos).manhattanLength()
                    if dist < 10:
                        # Still in deadzone, don't move yet
                        return
                    else:
                        # Past deadzone
                        self._drag_start_pos = None

                # Calculate delta from the note we're dragging
                new_start = max(0, self._snap(beat - self._drag_offset_x))
                new_pitch = max(self.LO, min(self.HI, pitch))

                delta_start = new_start - self._drag_note.start
                delta_pitch = new_pitch - self._drag_note.pitch

                # Apply delta to all selected notes
                notes = self._get_edit_notes() if self._is_variation_mode() else pat.notes
                for idx in self._selected:
                    if 0 <= idx < len(notes):
                        notes[idx].start = max(0, notes[idx].start + delta_start)
                        notes[idx].pitch = max(self.LO, min(self.HI,
                                                            notes[idx].pitch + delta_pitch))

                # Update the drag note position for next delta calculation
                self._drag_note.start = new_start
                self._drag_note.pitch = new_pitch

                self.refresh()

        elif self.state.tool == 'bend':
            if self._bend_drag_note is not None and self._bend_drag_point_idx is not None:
                n = self._bend_drag_note
                idx = self._bend_drag_point_idx
                if 0 <= idx < len(n.bend):
                    new_beat_off = max(0.0, min(n.duration, beat - n.start))
                    note_y_center = (self.HI - n.pitch) * self.NH + self.NH // 2
                    new_semitones = max(-2.0, min(2.0, -(y - note_y_center) / (self.NH * 2.0) * 2.0))
                    n.bend[idx] = [new_beat_off, round(new_semitones, 3)]
                    n.bend.sort(key=lambda p: p[0])
                    # Point may have shifted index after sort — find it by proximity
                    self._bend_drag_point_idx = min(
                        range(len(n.bend)),
                        key=lambda k: abs(n.bend[k][0] - new_beat_off))
                    self.app.engine.mark_dirty()
                    self.grid_widget.update()

    def _on_release(self, event):
        """Handle mouse button release."""
        # Finalize marquee selection
        if self._marquee_start:
            pat = self._get_edit_pattern()
            if pat:
                from PySide6.QtGui import QCursor
                cursor_pos = self.grid_widget.mapFromGlobal(QCursor.pos())

                from ..ops.note_edit import marquee_select
                new_sel = marquee_select(
                    pat,
                    (self._marquee_start.x(), self._marquee_start.y()),
                    (cursor_pos.x(), cursor_pos.y()),
                    self.BW, self.NH, self.HI)
                self._selected |= new_sel

            self._marquee_start = None
            self.refresh()
            return

        if self._resize_note:
            self.state.last_note_len = self._resize_note.duration
        if self._resize_note or self._drag_note:
            # In variation mode, persist changes as NoteDelta
            if self._is_variation_mode():
                self._persist_variation_edits()
            self.state.notify('note_edit')
            # Edit-completed hook: if any chord roots exist and one of
            # them was (directly or otherwise) affected by this edit,
            # regenerate voicings. The plugin is idempotent so we don't
            # need to pinpoint "was a root touched?" — the cheap state
            # walk in _maybe_run_chordify_live is enough.
            self._maybe_run_chordify_live()

        self._drag_note = None
        self._resize_note = None
        self._drag_start_pos = None

        # Finalise bend drag
        if self._bend_drag_note is not None:
            self.app.engine.mark_dirty()
            self.state.notify('note_edit')
            self._bend_drag_note = None
            self._bend_drag_point_idx = None

    def _on_right_click(self, event):
        """Handle right-click."""
        pat = self._get_edit_pattern()
        if not pat:
            return

        x, y = self._xy(event)

        if self.state.tool == 'bend':
            # Delete bend control point under cursor
            bn, bi = self._hit_bend_point(x, y)
            if bn is not None and bi >= 0:
                bn.bend.pop(bi)
                self.app.engine.mark_dirty()
                self.state.notify('note_edit')
                self.refresh()
            return

        if self.state.tool == 'lyrics':
            # Right-click in lyrics mode clears the lyric on the hit note
            n, i, _ = self._hit_note(x, y)
            if n and n.lyric:
                if self._is_variation_mode():
                    var = self.state.find_variation(self.state.sel_variation)
                    if var:
                        from ..ops.variations import variation_modify_note
                        variation_modify_note(var, n.note_id, lyric='')
                else:
                    n.lyric = ''
                self.state.notify('note_edit')
                self.refresh()
            return

        n, i, _ = self._hit_note(x, y)

        if n:
            if self._is_variation_mode():
                var = self.state.find_variation(self.state.sel_variation)
                if var and n.note_id:
                    # Check if it's an added note or a parent note
                    added_ids = {a.note_id for a in var.additions}
                    if n.note_id in added_ids:
                        from ..ops.variations import variation_remove_added_note
                        variation_remove_added_note(var, n.note_id)
                    else:
                        from ..ops.variations import variation_delete_note
                        variation_delete_note(var, n.note_id)
                self._selected.discard(i)
            else:
                from ..ops.note_edit import delete_note_at
                self._selected = delete_note_at(pat, i, self._selected)
            self.refresh()
            self.state.notify('note_edit')

    # ------------------------------------------------------------------
    # Chord-root interaction (middle-click)
    # ------------------------------------------------------------------

    def _on_middle_click(self, event):
        """Open the chord-root quality menu for the note under the cursor."""
        pat = self._get_edit_pattern()
        if not pat:
            return
        x, y = self._xy(event)
        n, _, _ = self._hit_note(x, y)
        if n is None:
            return
        if CHORD_VOICING_TAG in (n.tags or {}):
            # Middle-clicking a generated voicing shouldn't reroot it.
            return
        global_pos = event.globalPosition().toPoint() if hasattr(event, 'globalPosition') \
            else event.globalPos()
        menu = self._build_chord_root_menu(n, pat)
        menu.exec(global_pos)

    def _build_chord_root_menu(self, note, pat) -> QMenu:
        """Menu that edits ``note.tags['chord_root']`` and reruns chordify."""
        current_spec = (note.tags or {}).get(CHORD_ROOT_TAG)
        current_quality = current_spec.get('quality') if current_spec else None
        current_extras = tuple(current_spec.get('extras') or ()) if current_spec else ()

        menu = QMenu(self)
        header_text = cv_roman_label(note.pitch, pat.key, pat.scale, current_spec) \
            if current_spec else '(none)'
        header = menu.addAction(f'Current: {header_text}')
        header.setEnabled(False)
        menu.addSeparator()

        un = menu.addAction('Not a chord root')
        un.setCheckable(True)
        un.setChecked(current_spec is None)
        un.triggered.connect(lambda: self._apply_chord_root(note, None))
        menu.addSeparator()

        # Tier 1: diatonic dispatch.
        for qid, qlabel in (('diatonic', 'Diatonic'),
                            ('diatonic7', 'Diatonic 7th')):
            spec = dict(default_spec()); spec['quality'] = qid
            label = cv_chord_label(note.pitch, pat.key, pat.scale, spec)
            act = menu.addAction(f'{qlabel} ({label})')
            act.setCheckable(True)
            act.setChecked(current_quality == qid)
            act.triggered.connect(
                lambda _=False, q=qid: self._apply_chord_root_quality(note, q))

        menu.addSeparator()

        # Tier 2: common concrete qualities.
        tier2_main = (('maj', 'Major'), ('min', 'Minor'),
                      ('dim', 'Diminished'), ('aug', 'Augmented'),
                      ('sus2', 'Sus2'), ('sus4', 'Sus4'))
        for qid, qlabel in tier2_main:
            spec = dict(default_spec()); spec['quality'] = qid
            label = cv_chord_label(note.pitch, pat.key, pat.scale, spec)
            act = menu.addAction(f'{qlabel} ({label})')
            act.setCheckable(True)
            act.setChecked(current_quality == qid)
            act.triggered.connect(
                lambda _=False, q=qid: self._apply_chord_root_quality(note, q))

        # Submenu: 7ths & 6ths.
        sevenths = menu.addMenu('Seventh chords')
        for qid, qlabel in (('maj7', 'Major 7'), ('m7', 'Minor 7'),
                            ('dom7', 'Dominant 7'),
                            ('m7b5', 'Half-diminished (m7b5)'),
                            ('dim7', 'Diminished 7'),
                            ('minMaj7', 'Minor-Major 7'),
                            ('maj6', '6 (major 6)'),
                            ('min6', 'm6 (minor 6)')):
            spec = dict(default_spec()); spec['quality'] = qid
            label = cv_chord_label(note.pitch, pat.key, pat.scale, spec)
            act = sevenths.addAction(f'{qlabel} ({label})')
            act.setCheckable(True)
            act.setChecked(current_quality == qid)
            act.triggered.connect(
                lambda _=False, q=qid: self._apply_chord_root_quality(note, q))

        # Submenu: extensions.
        ext_menu = menu.addMenu('Extensions')
        for qid in TIER3_EXTENSIONS:
            spec = dict(default_spec()); spec['quality'] = qid
            label = cv_chord_label(note.pitch, pat.key, pat.scale, spec)
            act = ext_menu.addAction(f'{qid} ({label})')
            act.setCheckable(True)
            act.setChecked(current_quality == qid)
            act.triggered.connect(
                lambda _=False, q=qid: self._apply_chord_root_quality(note, q))

        # Submenu: alterations (toggles on current spec's extras list).
        alt_menu = menu.addMenu('Alterations')
        alt_menu.setEnabled(current_spec is not None)
        for alt in TIER4_ALTERATIONS:
            act = alt_menu.addAction(alt)
            act.setCheckable(True)
            act.setChecked(alt in current_extras)
            act.triggered.connect(
                lambda _=False, a=alt: self._toggle_chord_root_alteration(note, a))

        return menu

    def _apply_chord_root(self, note, spec):
        """Set or clear chord_root on ``note`` and rerun chordify."""
        if spec is None:
            if note.tags:
                note.tags.pop(CHORD_ROOT_TAG, None)
        else:
            if not note.tags:
                note.tags = {}
            note.tags[CHORD_ROOT_TAG] = spec
        self._run_chordify()
        self.state.notify('note_edit')
        self.refresh()

    def _apply_chord_root_quality(self, note, quality):
        spec = dict(note.tags.get(CHORD_ROOT_TAG) or default_spec()) \
            if note.tags else default_spec()
        spec['quality'] = quality
        spec.setdefault('extras', [])
        spec.setdefault('inversion', 'root')
        self._apply_chord_root(note, spec)

    def _toggle_chord_root_alteration(self, note, alt):
        spec = dict((note.tags or {}).get(CHORD_ROOT_TAG) or default_spec())
        extras = list(spec.get('extras') or [])
        if alt in extras:
            extras.remove(alt)
        else:
            extras.append(alt)
        spec['extras'] = extras
        spec.setdefault('quality', 'diatonic')
        spec.setdefault('inversion', 'root')
        self._apply_chord_root(note, spec)

    def _run_chordify(self):
        """Run the chordify plugin in-process and apply its ops.

        Scoped to 'whole' — the plugin itself decides what to do per-root
        based on tags present in the state. Re-entrant calls during a run
        (e.g. via notify cascades) are suppressed.
        """
        if getattr(self, '_chordify_running', False):
            return
        try:
            from ..song_plugins.song_view import SongView
            from ..song_plugins.api import SelectionSnapshot
            from ..song_plugins.apply_ops import apply_ops
        except Exception:
            return  # song-plugin machinery not available in this build

        sel = SelectionSnapshot(
            notes=frozenset(), placements=frozenset(), primary='none',
            current_pattern_id=None, current_variation_id=None,
            current_beat_pattern_id=None, current_auto_pattern_id=None,
        )
        view = SongView(self.app.state, sel)

        class _Progress:
            cancelled = False
            def phase(self, _): pass
            def update(self, _f, _m=None): pass

        self._chordify_running = True
        try:
            plugin = ChordifyPlugin()
            result = plugin.run(view, {}, _Progress())
            if not result.operations:
                return
            try:
                apply_ops(result.operations, self.app, 'chordify')
            except Exception:
                import logging
                logging.getLogger(__name__).exception('chordify failed')
                return
            if hasattr(self.app, 'engine') and hasattr(self.app.engine, 'mark_dirty'):
                self.app.engine.mark_dirty()
        finally:
            self._chordify_running = False

    def _maybe_run_chordify_live(self):
        """Fast path for post-edit regeneration.

        Cheap early-out when no chord roots exist anywhere in the state
        so most piano-roll edits never pay plugin-run cost.
        """
        if getattr(self, '_chordify_running', False):
            return
        if not _state_has_chord_roots(self.app.state):
            return
        self._run_chordify()


class PianoKeysWidget(QWidget):
    """Piano keyboard on left side."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_roll = parent

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            y = event.pos().y()
            pitch = self.parent_roll.HI - int(y / self.parent_roll.NH)
            if self.parent_roll.LO <= pitch <= self.parent_roll.HI:
                self.parent_roll.app.play_note(pitch, 100, track_id=self.parent_roll.state.sel_trk)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pat = self.parent_roll._get_edit_pattern()
        in_key = scale_set(pat.key, pat.scale) if pat else set()

        for p in range(self.parent_roll.LO, self.parent_roll.HI + 1):
            y = (self.parent_roll.HI - p) * self.parent_roll.NH
            nm = NOTE_NAMES[p % 12]
            is_black = '#' in nm
            is_c = p % 12 == 0
            ik = (p % 12) in in_key
            oct = p // 12 - 1

            if is_black:
                bg = QColor('#2a1a50') if ik else QColor('#111')
            else:
                bg = QColor('#2e2450') if ik else QColor('#16213e')

            painter.fillRect(0, y, 44, self.parent_roll.NH, bg)
            painter.setPen(QColor('#1a1a2e'))
            painter.drawRect(0, y, 44, self.parent_roll.NH)

            if is_c:
                painter.setPen(QColor('#eee'))
                painter.setFont(QFont('TkDefaultFont', 6))
                painter.drawText(QRect(0, y, 40, self.parent_roll.NH),
                                Qt.AlignRight | Qt.AlignVCenter, f'C{oct}')
                painter.setPen(QColor('#533483'))
                painter.drawLine(0, y + self.parent_roll.NH, 44, y + self.parent_roll.NH)
            elif not is_black:
                painter.setPen(QColor('#888'))
                painter.setFont(QFont('TkDefaultFont', 5))
                painter.drawText(QRect(0, y, 40, self.parent_roll.NH),
                                Qt.AlignRight | Qt.AlignVCenter, f'{nm}{oct}')


class PianoGridWidget(QWidget):
    """Note grid for piano roll."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_roll = parent
        self._bg_note_fade = {}  # (pitch, pattern_id) -> fade_level (0.0-1.0)
        self._slice_hover_pos = None  # (note_idx, beat) for slice mode preview
        
        # Enable keyboard focus
        self.setFocusPolicy(Qt.StrongFocus)
        
        # Enable mouse tracking to receive move events without button press
        self.setMouseTracking(True)
    
    def keyPressEvent(self, event):
        """Forward keyboard events to parent roll."""
        self.parent_roll.keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """Forward key releases (e.g. Alt) to parent roll."""
        self.parent_roll.keyReleaseEvent(event)

    def mousePressEvent(self, event):
        # Ensure grid widget has keyboard focus
        self.setFocus()

        # Track raw cursor position so the loupe and its fine deltas work
        self.parent_roll._last_real_pos = event.pos()

        if event.button() == Qt.LeftButton:
            self.parent_roll._on_click(event)
        elif event.button() == Qt.RightButton:
            self.parent_roll._on_right_click(event)
        elif event.button() == Qt.MiddleButton:
            self.parent_roll._on_middle_click(event)

    def mouseMoveEvent(self, event):
        roll = self.parent_roll
        # Update virtual cursor first: in loupe mode, scale the raw delta down;
        # otherwise, just follow the real cursor.
        if roll._alt_loupe and roll._last_real_pos is not None and roll._loupe_virtual is not None:
            dx = event.pos().x() - roll._last_real_pos.x()
            dy = event.pos().y() - roll._last_real_pos.y()
            vx, vy = roll._loupe_virtual
            vx += dx / roll._loupe_scale
            vy += dy / roll._loupe_scale
            # Clamp to widget bounds
            vx = max(0.0, min(float(self.width() - 1), vx))
            vy = max(0.0, min(float(self.height() - 1), vy))
            roll._loupe_virtual = (vx, vy)
            # Anchor follows virtual — the loupe drifts along with the edit
            # instead of leaving the cursor behind.
            roll._loupe_anchor = (vx, vy)
            self.update()
        roll._last_real_pos = event.pos()

        if event.buttons() & Qt.LeftButton:
            roll._on_drag(event)
        else:
            x, y = roll._xy(event)
            # Update slice preview
            if roll.state.tool == 'slice':
                n, i, _ = roll._hit_note(x, y)
                if n:
                    beat, _ = roll._coords_to_beat_pitch(x, y)
                    self._slice_hover_pos = (i, beat)
                else:
                    self._slice_hover_pos = None
                self.update()
            # Cursor hint — show a horizontal-resize cursor over the handle
            # so users can tell where "resize" vs "move" starts. Only in
            # edit mode; other tools have their own affordances.
            elif roll.state.tool == 'edit' and not roll._alt_loupe:
                n, _, is_resize = roll._hit_note(x, y)
                if n and is_resize:
                    self.setCursor(Qt.SizeHorCursor)
                else:
                    self.unsetCursor()

        # Always update ghost note position when ghost notes are active
        if roll._ghost_notes:
            self.update()

    def mouseReleaseEvent(self, event):
        self.parent_roll._on_release(event)
    
    def leaveEvent(self, event):
        """Clear slice preview when mouse leaves widget."""
        if self._slice_hover_pos:
            self._slice_hover_pos = None
            self.update()

    def _paint_regions(self, painter, pat, total_h):
        """Paint plugin-emitted region boxes behind the note grid.

        Reads the broadcast overlay on the app (may be None if the
        song-plugins subsystem isn't wired in this build). Regions are
        scoped to the currently-edited pattern via ``pattern_id``;
        entries without a pattern_id are treated as global and drawn on
        every pattern.
        """
        app = getattr(self.parent_roll, "app", None)
        overlay = getattr(app, "piano_roll_overlay", None)
        if overlay is None or not overlay.has_regions():
            return
        # Which variation (if any) is being edited?
        var_id = None
        state = self.parent_roll.state
        if self.parent_roll._is_variation_mode():
            var_id = state.sel_variation
        regions = overlay.regions_for_pattern(pat.id, variation_id=var_id)
        if not regions:
            return
        BW = self.parent_roll.BW
        NH = self.parent_roll.NH
        HI = self.parent_roll.HI
        LO = self.parent_roll.LO

        # Paint larger extents first so smaller regions stack visually
        # on top. Ties broken by declaration order for stable layering.
        ordered = sorted(
            enumerate(regions),
            key=lambda iv: (
                -((iv[1].get("end_beat", 0) - iv[1].get("start_beat", 0))
                  * max(1,
                        (iv[1].get("max_pitch") or 127) -
                        (iv[1].get("min_pitch") or 0))),
                iv[0],
            ),
        )
        # A small halo around each box so tightly-fit regions (e.g. the
        # scale-conformance hinter's one-pitch-row boxes) remain visible
        # behind the note. For broad regions (full-pitch chord bands)
        # the halo is imperceptible.
        HALO = 2
        painter.save()
        try:
            for _, r in ordered:
                try:
                    s_beat = float(r.get("start_beat", 0.0))
                    e_beat = float(r.get("end_beat", 0.0))
                except (TypeError, ValueError):
                    continue
                if e_beat <= s_beat:
                    continue
                min_pitch = r.get("min_pitch")
                max_pitch = r.get("max_pitch")
                if min_pitch is None:
                    min_pitch = LO
                if max_pitch is None:
                    max_pitch = HI
                min_pitch = max(LO, int(min_pitch))
                max_pitch = min(HI, int(max_pitch))
                if max_pitch < min_pitch:
                    continue

                x0 = int(s_beat * BW) - HALO
                x1 = int(e_beat * BW) + HALO
                y0 = (HI - max_pitch) * NH - HALO
                y1 = (HI - min_pitch + 1) * NH + HALO
                w = max(1, x1 - x0)
                h = max(1, y1 - y0)

                color_hex = r.get("color") or "#7ad0ff"
                try:
                    fill = QColor(str(color_hex))
                except Exception:
                    fill = QColor("#7ad0ff")
                border = QColor(fill)
                fill.setAlpha(60)
                border.setAlpha(180)
                painter.setPen(QPen(border, 1))
                painter.setBrush(fill)
                painter.drawRect(x0, y0, w, h)

                label = r.get("label")
                if label and w >= 20 and h >= 12:
                    painter.setPen(QColor(230, 236, 250, 220))
                    painter.setFont(QFont('TkDefaultFont', 7))
                    painter.setClipRect(x0 + 3, y0, w - 6, h)
                    painter.drawText(
                        x0 + 4, y0 + 2, w - 8, h - 4,
                        Qt.AlignTop | Qt.AlignLeft, str(label),
                    )
                    painter.setClipping(False)
        finally:
            painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pat = self.parent_roll._get_edit_pattern()
        s = self.parent_roll.state

        pitch_range = self.parent_roll.HI - self.parent_roll.LO + 1
        total_h = pitch_range * self.parent_roll.NH
        beats = pat.length if pat else 16
        total_w = self.width()

        in_key = scale_set(pat.key, pat.scale) if pat else set()
        bpm_beats = s.ts_num * (4 / s.ts_den)

        # Row backgrounds
        for p in range(self.parent_roll.LO, self.parent_roll.HI + 1):
            y = (self.parent_roll.HI - p) * self.parent_roll.NH
            nm = NOTE_NAMES[p % 12]
            is_black = '#' in nm
            is_c = p % 12 == 0
            ik = (p % 12) in in_key
            
            if is_black:
                bg = QColor('#1e1a40') if ik else QColor('#15152a')
            else:
                bg = QColor('#252050') if ik else QColor('#1a1a30')
            
            painter.fillRect(0, y, total_w, self.parent_roll.NH, bg)
            
            line_color = QColor('#3a3a6a') if is_c else QColor('#222244')
            width = 1 if is_c else 0.5
            painter.setPen(QPen(line_color, width))
            painter.drawLine(0, y, total_w, y)

        # Beat lines
        total_subdivs = int(beats * 4)
        for b in range(total_subdivs + 1):
            x = b * self.parent_roll.BW / 4
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
            painter.drawLine(int(x), 0, int(x), total_h)

        if not pat:
            return

        # Plugin-broadcast region overlays — painted behind everything so
        # they act as coloured bands behind notes. Only drawn when a
        # plugin broadcasting a ``regions`` schema is active.
        self._paint_regions(painter, pat, total_h)

        # Background notes (other patterns) - overlay system
        bg_notes = []
        
        # Determine which other patterns are currently playing (playhead is
        # inside one of their placements on the arrangement timeline).
        playing_pattern_ids = set()
        if s.playing and s.playhead is not None:
            for pl in s.placements:
                other = s.find_pattern(pl.pattern_id)
                if other and other.id != pat.id:
                    pl_end = pl.time + other.length * (pl.repeats or 1)
                    if pl.time <= s.playhead < pl_end:
                        playing_pattern_ids.add(other.id)
        
        def find_smart_offset(current_pat_id, other_pat_id):
            """Find the offset to align first overlap in pattern-relative coordinates."""
            curr_pls = [pl for pl in s.placements if pl.pattern_id == current_pat_id]
            other_pls = [pl for pl in s.placements if pl.pattern_id == other_pat_id]
            
            if not curr_pls or not other_pls:
                return 0.0
            
            curr_pat = s.find_pattern(current_pat_id)
            other_pat = s.find_pattern(other_pat_id)
            if not curr_pat or not other_pat:
                return 0.0
            
            for curr_pl in sorted(curr_pls, key=lambda p: p.time):
                curr_reps = curr_pl.repeats or 1
                for curr_rep in range(curr_reps):
                    curr_arr_start = curr_pl.time + curr_rep * curr_pat.length
                    curr_arr_end = curr_arr_start + curr_pat.length
                    
                    for other_pl in sorted(other_pls, key=lambda p: p.time):
                        other_reps = other_pl.repeats or 1
                        for other_rep in range(other_reps):
                            other_arr_start = other_pl.time + other_rep * other_pat.length
                            other_arr_end = other_arr_start + other_pat.length
                            
                            if not (curr_arr_end <= other_arr_start or other_arr_end <= curr_arr_start):
                                return other_arr_start - curr_arr_start
            
            return 0.0
        
        for pl in s.placements:
            if pl.pattern_id == pat.id:
                continue
            other_pat = s.find_pattern(pl.pattern_id)
            if not other_pat:
                continue
            
            if other_pat.overlay_mode == 'off':
                continue
            
            # 'playing' mode only shows when pattern is actively playing
            if other_pat.overlay_mode == 'playing' and other_pat.id not in playing_pattern_ids:
                continue
            
            t = s.find_track(pl.track_id)
            if not t:
                continue
            
            transpose = s.compute_transpose(pl)
            pattern_offset = find_smart_offset(pat.id, other_pat.id)
            
            # 'always' draws at fixed alpha; 'playing' uses the fade system
            is_always = other_pat.overlay_mode == 'always'
            
            for n in other_pat.notes:
                bg_notes.append({
                    'pitch': n.pitch + transpose,
                    'start': n.start + pattern_offset,
                    'duration': n.duration,
                    'velocity': n.velocity,
                    'key': (n.pitch + transpose, other_pat.id),
                    'is_always': is_always,
                })
            
            # For 'playing' mode, mark these notes as active so they fade in
            if not is_always:
                for n in other_pat.notes:
                    key = (n.pitch + transpose, other_pat.id)
                    self._bg_note_fade[key] = 1.0
        
        # Decay fading notes
        keys_to_remove = []
        for key in self._bg_note_fade:
            self._bg_note_fade[key] = max(0.0, self._bg_note_fade[key] - 0.05)
            if self._bg_note_fade[key] <= 0:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self._bg_note_fade[key]
        
        for n in bg_notes:
            x = n['start'] * self.parent_roll.BW
            y = (self.parent_roll.HI - n['pitch']) * self.parent_roll.NH
            w = n['duration'] * self.parent_roll.BW
            
            if 0 <= n['pitch'] <= 127 and -w < x < total_w:
                if n['is_always']:
                    alpha = 40
                else:
                    fade = self._bg_note_fade.get(n['key'], 0.0)
                    if fade <= 0:
                        continue
                    alpha = int(40 * fade)
                
                painter.setPen(Qt.NoPen)
                color = QColor('#cccccc')
                color.setAlpha(alpha)
                painter.setBrush(color)
                painter.drawRect(int(x), y + 1, int(w - 1), self.parent_roll.NH - 2)
        
        if self._bg_note_fade:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(33, self.update)

        # Ghost parent notes when editing a variation
        var_mode = self.parent_roll._is_variation_mode()
        if var_mode:
            var = self.parent_roll.state.find_variation(self.parent_roll.state.sel_variation)
            if var:
                deleted_ids = set(var.deletions)
                for n in pat.notes:
                    x = n.start * self.parent_roll.BW
                    y = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH
                    w = n.duration * self.parent_roll.BW
                    ghost_color = QColor('#888888')
                    ghost_color.setAlpha(40)
                    painter.setPen(QPen(QColor('#666666'), 1, Qt.DotLine))
                    painter.setBrush(ghost_color)
                    painter.drawRect(int(x), y + 1, int(w - 1), self.parent_roll.NH - 2)
                    # Strikethrough for deleted notes
                    if n.note_id in deleted_ids:
                        painter.setPen(QPen(QColor('#ff4444'), 1))
                        mid_y = y + self.parent_roll.NH // 2
                        painter.drawLine(int(x), mid_y, int(x + w - 1), mid_y)

        # Notes from current pattern / resolved variation
        notes = self.parent_roll._get_edit_notes() if var_mode else pat.notes
        for i, n in enumerate(notes):
            x = n.start * self.parent_roll.BW
            y = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH
            w = n.duration * self.parent_roll.BW
            sel = i in self.parent_roll._selected
            color = QColor(vel_color(n.velocity))

            border_color = self.parent_roll._note_border_color(n)
            # Dashed outline for live (untouched) voicings — see chordify
            # plugin docstring. Edited voicings render solid so the user
            # can tell at a glance which notes the plugin will regenerate.
            is_voicing = CHORD_VOICING_TAG in (n.tags or {})
            is_live_voicing = False
            if is_voicing:
                vtag = n.tags[CHORD_VOICING_TAG]
                if isinstance(vtag, dict) and vtag.get('gen_pitch') is not None:
                    is_live_voicing = (
                        vtag.get('gen_pitch') == n.pitch
                        and abs((vtag.get('gen_start') or 0.0) - n.start) < 1e-6
                        and abs((vtag.get('gen_dur') or 0.0) - n.duration) < 1e-6
                        and int(vtag.get('gen_vel') or 0) == int(n.velocity)
                    )
            pen_style = Qt.DashLine if is_live_voicing else Qt.SolidLine

            if sel:
                painter.setPen(QPen(QColor('#fff'), 2, pen_style))
            elif border_color:
                painter.setPen(QPen(border_color, 2, pen_style))
            else:
                painter.setPen(QPen(QColor(pat.color), 1, pen_style))

            painter.setBrush(color)
            painter.drawRect(int(x), y + 1, int(w - 1), self.parent_roll.NH - 2)

            # Chord-root label above the note — roman-numeral form by
            # default. Always visible so the user can see which notes
            # are rooted at a glance.
            tags = n.tags or {}
            if CHORD_ROOT_TAG in tags:
                label = cv_roman_label(
                    n.pitch, pat.key, pat.scale, tags[CHORD_ROOT_TAG])
                if label:
                    painter.setPen(QColor('#ffcc66'))
                    painter.setFont(QFont('TkDefaultFont', 7, QFont.Bold))
                    painter.drawText(int(x + 2), y - 1, label)

            # Resize handle — width matches the hit zone so the visual cue is
            # honest about where the handle starts.
            handle_beats = self.parent_roll._resize_handle_beats(n)
            handle_w = handle_beats * self.parent_roll.BW
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 80))
            painter.drawRect(QRectF(x + w - handle_w, y + 1,
                                     max(1.0, handle_w - 1),
                                     self.parent_roll.NH - 2))

            # Velocity text for selected notes
            if sel:
                painter.setPen(QColor('#fff'))
                painter.setFont(QFont('TkDefaultFont', 6))
                painter.drawText(int(x + 2), y + self.parent_roll.NH - 3, f'v{n.velocity}')

            # Lyric text drawn inside the note block (always visible when present)
            if n.lyric and w > 8:
                lyr_color = QColor('#fee440') if sel else QColor('#ffe08a')
                painter.setPen(lyr_color)
                painter.setFont(QFont('TkDefaultFont', 7))
                painter.setClipRect(int(x + 2), y + 1, int(w - 4), self.parent_roll.NH - 2)
                painter.drawText(
                    int(x + 2), y + 1, int(w - 4), self.parent_roll.NH - 2,
                    Qt.AlignLeft | Qt.AlignVCenter, n.lyric)
                painter.setClipping(False)

        # Reference binding connectors — draw lines from bound added notes to their ref notes
        if var_mode:
            var = self.parent_roll.state.find_variation(self.parent_roll.state.sel_variation)
            if var:
                added_map = {a.note_id: a for a in var.additions}
                # Build ref note position lookup from ghost parent notes + resolved notes
                ref_positions = {}  # note_id → (x_center, y_center)
                if pat:
                    for n in pat.notes:
                        cx = n.start * self.parent_roll.BW + n.duration * self.parent_roll.BW / 2
                        cy = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH + self.parent_roll.NH // 2
                        ref_positions[n.note_id] = (cx, cy)
                for n in notes:
                    if n.note_id in added_map:
                        added = added_map[n.note_id]
                        if not added.ref_note_id:
                            continue
                        ref_pos = ref_positions.get(added.ref_note_id)
                        if not ref_pos:
                            continue
                        # Added note center
                        ax = n.start * self.parent_roll.BW + n.duration * self.parent_roll.BW / 2
                        ay = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH + self.parent_roll.NH // 2
                        rx, ry = ref_pos
                        # Draw connector line
                        line_color = QColor('#00bcd4') if added.ref_bind == 'full' else QColor('#4dd0e1')
                        line_color.setAlpha(160)
                        painter.setPen(QPen(line_color, 1, Qt.DashLine))
                        painter.drawLine(int(ax), int(ay), int(rx), int(ry))
                        # Draw bind type label at midpoint
                        mx, my = (ax + rx) / 2, (ay + ry) / 2
                        label = 'F' if added.ref_bind == 'full' else 'P'
                        painter.setPen(QColor('#ffffff'))
                        painter.setFont(QFont('TkDefaultFont', 6))
                        painter.setBrush(QColor(0, 0, 0, 140))
                        painter.drawRect(int(mx) - 4, int(my) - 5, 9, 10)
                        painter.drawText(int(mx) - 3, int(my) + 4, label)

        # Bend curves — drawn on top of all notes
        _in_bend_mode = s.tool == 'bend'
        for i, n in enumerate(notes):
            if not n.bend and not _in_bend_mode:
                continue

            note_x0 = n.start * self.parent_roll.BW
            note_y_center = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH + self.parent_roll.NH // 2

            if n.bend:
                # Convert bend points to (time, value, curve_type) tuples
                # All bend points use 'smooth' interpolation
                bend_points = [(t, v, 'smooth') for t, v in n.bend]

                def _curve_y(beat_off):
                    # Use shared interpolation utility
                    semitones = interpolate_curve(bend_points, beat_off, n.duration, default_value=0.0)
                    return note_y_center - int(semitones / 2.0 * self.parent_roll.NH * 2)
                    
                # Draw smooth curve by sampling
                from PySide6.QtCore import QLineF
                curve_color = QColor('#00f5d4')
                curve_color.setAlpha(200)
                painter.setPen(QPen(curve_color, 1.5))
                steps = max(16, int(n.duration * 32))
                prev_cx = note_x0
                prev_cy = _curve_y(0.0)
                for s_idx in range(1, steps + 1):
                    t = s_idx / steps * n.duration
                    cx = note_x0 + t * self.parent_roll.BW
                    cy = _curve_y(t)
                    painter.drawLine(int(prev_cx), int(prev_cy), int(cx), int(cy))
                    prev_cx, prev_cy = cx, cy

                # Draw control point handles
                for pt_idx, (beat_off, semitones) in enumerate(n.bend):
                    px = int(note_x0 + beat_off * self.parent_roll.BW)
                    py = note_y_center - int(semitones / 2.0 * self.parent_roll.NH * 2)
                    is_dragging = (self.parent_roll._bend_drag_note is n and
                                   self.parent_roll._bend_drag_point_idx == pt_idx)
                    handle_color = QColor('#fee440') if is_dragging else QColor('#00f5d4')
                    painter.setPen(QPen(QColor('#000'), 1))
                    painter.setBrush(handle_color)
                    painter.drawEllipse(px - 4, py - 4, 8, 8)

            elif _in_bend_mode:
                # In bend mode, show a faint zero-line on notes with no bend yet
                painter.setPen(QPen(QColor(255, 255, 255, 40), 1, Qt.DashLine))
                nx0 = int(note_x0)
                nx1 = int(note_x0 + n.duration * self.parent_roll.BW)
                painter.drawLine(nx0, note_y_center, nx1, note_y_center)
        
        # Draw slice preview line
        if self._slice_hover_pos and s.tool == 'slice':
            note_idx, beat = self._slice_hover_pos
            if 0 <= note_idx < len(notes):
                n = notes[note_idx]
                x = beat * self.parent_roll.BW
                y = (self.parent_roll.HI - n.pitch) * self.parent_roll.NH
                
                painter.setPen(QPen(QColor('#ff0000'), 2))
                painter.drawLine(int(x), y + 1, int(x), y + self.parent_roll.NH - 1)
        
        # Draw marquee selection rectangle
        if self.parent_roll._marquee_start:
            from PySide6.QtGui import QCursor
            cursor_pos = self.mapFromGlobal(QCursor.pos())
            start = self.parent_roll._marquee_start
            rect = QRectF(
                min(start.x(), cursor_pos.x()),
                min(start.y(), cursor_pos.y()),
                abs(cursor_pos.x() - start.x()),
                abs(cursor_pos.y() - start.y())
            )
            painter.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
            painter.setBrush(QColor(255, 255, 255, 30))
            painter.drawRect(rect)
        
        # Draw ghost notes (semi-transparent)
        if self.parent_roll._ghost_notes:
            from PySide6.QtGui import QCursor
            cursor_pos = self.mapFromGlobal(QCursor.pos())
            
            # Calculate offset from first note
            if self.parent_roll._ghost_notes:
                min_start = min(n.start for n in self.parent_roll._ghost_notes)
                min_pitch = min(n.pitch for n in self.parent_roll._ghost_notes)
                
                cursor_beat, cursor_pitch = self.parent_roll._coords_to_beat_pitch(
                    cursor_pos.x(), cursor_pos.y()
                )
                snapped_beat = self.parent_roll._snap(cursor_beat)
                
                beat_offset = snapped_beat - min_start
                pitch_offset = cursor_pitch - min_pitch
                
                for n in self.parent_roll._ghost_notes:
                    ghost_pitch = n.pitch + pitch_offset
                    ghost_start = n.start + beat_offset
                    
                    # Clamp to valid range
                    if ghost_pitch < self.parent_roll.LO or ghost_pitch > self.parent_roll.HI:
                        continue
                    
                    x = ghost_start * self.parent_roll.BW
                    y = (self.parent_roll.HI - ghost_pitch) * self.parent_roll.NH
                    w = n.duration * self.parent_roll.BW
                    
                    # Draw semi-transparent
                    color = QColor(vel_color(n.velocity))
                    color.setAlpha(128)
                    painter.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
                    painter.setBrush(color)
                    painter.drawRect(int(x), y + 1, int(w - 1), self.parent_roll.NH - 2)

        # Fine-edit loupe overlay — draws a magnified, clipped view of the
        # region around the virtual cursor so users can edit tiny notes.
        if self.parent_roll._alt_loupe:
            self._paint_loupe(painter)

    def _paint_loupe(self, painter):
        """Draw the fine-edit magnifier. The loupe is anchored where Alt was
        pressed; mouse deltas while Alt is held move a virtual cursor at
        1/scale speed, and the loupe renders a magnified view around it."""
        roll = self.parent_roll
        if roll._loupe_anchor is None or roll._loupe_virtual is None:
            return
        scale = roll._loupe_scale
        radius = roll._loupe_radius
        ax, ay = roll._loupe_anchor
        vx, vy = roll._loupe_virtual

        dest_rect = QRectF(ax - radius, ay - radius, 2 * radius, 2 * radius)
        src_half = radius / scale
        src_rect = QRectF(vx - src_half, vy - src_half, 2 * src_half, 2 * src_half)

        painter.save()
        clip = QPainterPath()
        clip.addEllipse(dest_rect)
        painter.setClipPath(clip)

        # Background tint
        painter.fillRect(dest_rect, QColor('#12122a'))

        # Transform: point (vx, vy) in source space maps to (ax, ay) on screen,
        # scaled up by `scale` around that pivot.
        painter.translate(ax, ay)
        painter.scale(scale, scale)
        painter.translate(-vx, -vy)

        # Draw row backgrounds + key-scale shading within the loupe viewport
        pat = roll._get_edit_pattern()
        in_key = scale_set(pat.key, pat.scale) if pat else set()
        pitch_top = int(roll.HI - src_rect.top() / roll.NH) + 1
        pitch_bot = int(roll.HI - src_rect.bottom() / roll.NH) - 1
        for p in range(pitch_bot, pitch_top + 1):
            if p < roll.LO or p > roll.HI:
                continue
            y = (roll.HI - p) * roll.NH
            nm = NOTE_NAMES[p % 12]
            is_black = '#' in nm
            ik = (p % 12) in in_key
            if is_black:
                bg = QColor('#1e1a40') if ik else QColor('#15152a')
            else:
                bg = QColor('#252050') if ik else QColor('#1a1a30')
            painter.fillRect(QRectF(src_rect.left(), y, src_rect.width(), roll.NH), bg)

        # Beat subdivision lines
        beat_lo = max(0.0, src_rect.left() / roll.BW)
        beat_hi = src_rect.right() / roll.BW
        first_sub = int(beat_lo * 4) - 1
        last_sub = int(beat_hi * 4) + 1
        for b in range(first_sub, last_sub + 1):
            gx = b * roll.BW / 4.0
            if b % 4 == 0:
                color = QColor('#3a3a6a')
            elif b % 2 == 0:
                color = QColor('#2a2a5a')
            else:
                color = QColor('#222244')
            pen = QPen(color, 1.0 / scale)
            painter.setPen(pen)
            painter.drawLine(QPointF(gx, src_rect.top()), QPointF(gx, src_rect.bottom()))

        # Notes within source rect (same logic as main paint, simplified)
        notes = roll._get_edit_notes() if roll._is_variation_mode() else (pat.notes if pat else [])
        for i, n in enumerate(notes):
            nx = n.start * roll.BW
            ny = (roll.HI - n.pitch) * roll.NH
            nw = n.duration * roll.BW
            if nx + nw < src_rect.left() or nx > src_rect.right():
                continue
            if ny + roll.NH < src_rect.top() or ny > src_rect.bottom():
                continue
            sel = i in roll._selected
            color = QColor(vel_color(n.velocity))
            border_color = roll._note_border_color(n)
            if sel:
                painter.setPen(QPen(QColor('#fff'), 2.0 / scale))
            elif border_color:
                painter.setPen(QPen(border_color, 2.0 / scale))
            else:
                painter.setPen(QPen(QColor(pat.color) if pat else QColor('#888'), 1.0 / scale))
            painter.setBrush(color)
            painter.drawRect(QRectF(nx, ny + 1, max(1.0, nw - 1), roll.NH - 2))

            # Resize handle hint — match main-paint sizing for consistency
            handle_w = roll._resize_handle_beats(n) * roll.BW
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 110))
            painter.drawRect(QRectF(nx + nw - handle_w, ny + 1,
                                     max(1.0, handle_w - 1), roll.NH - 2))

        painter.restore()

        # Loupe border
        painter.setPen(QPen(QColor('#fee440'), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(dest_rect)

        # Crosshair at virtual cursor — by construction it's at the centre of
        # the loupe in screen space.
        painter.setPen(QPen(QColor('#fee440'), 1))
        painter.drawLine(int(ax - 6), int(ay), int(ax + 6), int(ay))
        painter.drawLine(int(ax), int(ay - 6), int(ax), int(ay + 6))


# Distinct swatch colors for categorical note-attr choices (cycled by index).
_ATTR_CHOICE_COLORS = ['#4d96ff', '#e84393', '#00b894', '#fdcb6e',
                       '#a29bfe', '#ff7675', '#55efc4', '#fab1a0']


def _attr_log_norm(decl, value):
    """Map a continuous attr value to [0,1] in log space around its range."""
    lo = max(1e-4, float(decl.get('min', 0.125)))
    hi = max(lo * 1.0001, float(decl.get('max', 8.0)))
    v = min(hi, max(lo, float(value)))
    return (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))


def _attr_value_from_norm(decl, norm):
    """Inverse of _attr_log_norm: [0,1] → value in the decl's range."""
    lo = max(1e-4, float(decl.get('min', 0.125)))
    hi = max(lo * 1.0001, float(decl.get('max', 8.0)))
    norm = min(1.0, max(0.0, norm))
    return math.exp(math.log(lo) + norm * (math.log(hi) - math.log(lo)))


class VelocityWidget(QWidget):
    """Bottom lane: edits velocity, or a per-note attribute when one is the
    active lane (selected via the piano roll's Lane dropdown)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_roll = parent
        self._vel_dragging = False

    # -- input -----------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            # Right-click clears a note-attr override (back to synth default).
            if self.parent_roll._active_lane != 'velocity':
                self._clear_attr_at(event)
            return
        if event.button() == Qt.LeftButton:
            self._vel_dragging = True
            self._edit_at(event, is_press=True)

    def mouseMoveEvent(self, event):
        if self._vel_dragging:
            self._edit_at(event, is_press=False)

    def mouseReleaseEvent(self, event):
        self._vel_dragging = False

    def _target_indices(self, notes, event):
        """Note indices to edit: the selection if any, else the nearest note."""
        if self.parent_roll._selected:
            return [i for i in self.parent_roll._selected if 0 <= i < len(notes)]
        scroll_off = self.parent_roll.scroll_area.horizontalScrollBar().value()
        beat = (event.pos().x() + scroll_off) / self.parent_roll.BW
        best, best_dist = -1, float('inf')
        for i, n in enumerate(notes):
            d = abs(beat - n.start)
            if d < best_dist and d < 0.5:
                best_dist, best = d, i
        return [best] if best >= 0 else []

    def _edit_at(self, event, is_press):
        lane = self.parent_roll._active_lane
        if lane == 'velocity':
            self._set_vel_at(event)
        else:
            self._set_attr_at(event, is_press)

    def _set_vel_at(self, event):
        notes = self.parent_roll._get_edit_notes()
        if not notes:
            return
        vel = max(1, min(127, int((1 - event.pos().y() / 48) * 127)))
        targets = self._target_indices(notes, event)
        if not targets:
            return
        for idx in targets:
            notes[idx].velocity = vel
        if self.parent_roll._is_variation_mode():
            self.parent_roll._persist_variation_edits()
        if self.parent_roll._selected:
            self.parent_roll.vel_slider.setValue(vel)
        self.parent_roll.refresh()

    def _set_attr_at(self, event, is_press):
        decl = self.parent_roll._active_attr_decl
        if not decl:
            return
        notes = self.parent_roll._get_edit_notes()
        if not notes:
            return
        targets = self._target_indices(notes, event)
        if not targets:
            return
        attr_id = decl['id']
        is_categorical = decl.get('hint') == 'categorical'

        if is_categorical:
            # Click cycles to the next choice; drag does nothing further.
            if not is_press:
                return
            n_choices = max(1, len(decl.get('choices', [])))
            for idx in targets:
                cur = notes[idx].attrs.get(attr_id)
                nxt = 0 if cur is None else (int(round(cur)) + 1) % n_choices
                notes[idx].attrs[attr_id] = float(nxt)
        else:
            value = _attr_value_from_norm(decl, 1 - event.pos().y() / 48)
            default = float(decl.get('default', 1.0))
            # Snap to neutral near the default and drop the key entirely.
            neutral = abs(math.log(max(1e-4, value)) - math.log(max(1e-4, default))) < 0.04
            for idx in targets:
                if neutral:
                    notes[idx].attrs.pop(attr_id, None)
                else:
                    notes[idx].attrs[attr_id] = value

        if self.parent_roll._is_variation_mode():
            self.parent_roll._persist_variation_edits()
        self.parent_roll.refresh()

    def _clear_attr_at(self, event):
        decl = self.parent_roll._active_attr_decl
        if not decl:
            return
        notes = self.parent_roll._get_edit_notes()
        if not notes:
            return
        targets = self._target_indices(notes, event)
        changed = False
        for idx in targets:
            if notes[idx].attrs.pop(decl['id'], None) is not None:
                changed = True
        if changed:
            if self.parent_roll._is_variation_mode():
                self.parent_roll._persist_variation_edits()
            self.parent_roll.refresh()

    # -- paint -----------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        pat = self.parent_roll._get_edit_pattern()
        total_w = self.width()
        painter.fillRect(self.rect(), QColor('#12121f'))
        painter.setPen(QPen(QColor('#2a2a4a'), 0.5))
        painter.drawLine(0, 25, total_w, 25)
        if not pat:
            return

        if self.parent_roll._active_lane == 'velocity':
            self._paint_velocity(painter)
        else:
            self._paint_attr(painter)

    def _paint_velocity(self, painter):
        notes = self.parent_roll._get_edit_notes()
        bw = max(3, self.parent_roll.state.snap * self.parent_roll.BW * 0.6)
        scroll_off = self.parent_roll.scroll_area.horizontalScrollBar().value()
        for i, n in enumerate(notes):
            x = n.start * self.parent_roll.BW + 2 - scroll_off
            h = n.velocity / 127 * 46
            color = QColor('#fff') if i in self.parent_roll._selected else QColor(vel_color(n.velocity))
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRect(int(x), int(48 - h), int(bw), int(h))

    def _paint_attr(self, painter):
        decl = self.parent_roll._active_attr_decl
        if not decl:
            return
        attr_id = decl['id']
        is_categorical = decl.get('hint') == 'categorical'
        choices = decl.get('choices', [])
        notes = self.parent_roll._get_edit_notes()
        bw = max(3, self.parent_roll.state.snap * self.parent_roll.BW * 0.6)
        scroll_off = self.parent_roll.scroll_area.horizontalScrollBar().value()

        # Neutral reference line for continuous lanes (the default multiplier).
        if not is_categorical:
            nh = _attr_log_norm(decl, float(decl.get('default', 1.0))) * 46
            painter.setPen(QPen(QColor('#3a3a5a'), 1, Qt.DashLine))
            painter.drawLine(0, int(48 - nh), self.width(), int(48 - nh))

        font = QFont('Segoe UI', 6)
        painter.setFont(font)
        for i, n in enumerate(notes):
            x = n.start * self.parent_roll.BW + 2 - scroll_off
            val = n.attrs.get(attr_id)
            sel = i in self.parent_roll._selected
            if is_categorical:
                if val is None:
                    # Unset → faint outline (note uses the synth's param default).
                    painter.setPen(QPen(QColor('#444'), 1))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(int(x), 30, int(bw), 14)
                    continue
                ci = int(round(val)) % max(1, len(_ATTR_CHOICE_COLORS))
                col = QColor(_ATTR_CHOICE_COLORS[ci])
                painter.setPen(QPen(QColor('#fff'), 1) if sel else Qt.NoPen)
                painter.setBrush(col)
                painter.drawRect(int(x), 12, int(bw), 32)
                if int(bw) >= 10 and 0 <= int(round(val)) < len(choices):
                    painter.setPen(QPen(QColor('#000')))
                    painter.drawText(int(x) + 1, 40, choices[int(round(val))][:3])
            else:
                if val is None:
                    continue  # neutral — nothing drawn (uses default)
                h = _attr_log_norm(decl, val) * 46
                col = QColor('#fff') if sel else QColor('#4d96ff')
                painter.setPen(Qt.NoPen)
                painter.setBrush(col)
                painter.drawRect(int(x), int(48 - h), int(bw), int(h))


