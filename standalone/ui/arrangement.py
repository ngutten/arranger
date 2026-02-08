"""Arrangement timeline canvas - track lanes, placements, and playhead."""

from PySide6.QtWidgets import QFrame, QWidget, QScrollArea, QVBoxLayout, QHBoxLayout, QScrollBar
from PySide6.QtCore import Qt, QRect, QPoint, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont

from ..state import preset_name
from ..clipboard import MarqueeSelection, ArrangementClipboard, select_placements_in_rect



class ArrangementView(QFrame):
    """Arrangement timeline with track labels, canvas, and timeline header."""

    # Layout constants
    TH = 56    # track height
    BW = 30    # pixels per beat
    MIN_BEATS = 64 # Minimum number of beats to show
    LOOKAHEAD_FACTOR = 1.5 # How much to extend the scrollbar past the current song extent

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state

        # Drag state
        self._drag_pl = None
        self._drag_offset = 0
        self._resize_pl = None
        self._drag_beat_pl = None
        self._resize_beat_pl = None

        # Dynamic extent tracking
        self._max_scroll_beats = self.MIN_BEATS
        
        # Loop marker drag state
        self._drag_loop_marker = None  # 'start' or 'end'
        
        # Selection and clipboard
        self.marquee = MarqueeSelection()
        self.clipboard = ArrangementClipboard()
        self.selected_placements = []
        self.selected_beat_placements = []
        
        # Ghost placement state (for paste preview)
        self._ghost_placements = []  # List of dicts with placement data
        self._ghost_beat_placements = []
        self._ghost_offset = 0.0  # Time offset from clipboard min_time

        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Timeline header - use HBoxLayout to add offset matching track labels
        timeline_container = QWidget()
        timeline_layout = QHBoxLayout(timeline_container)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(0)
        
        # Add spacer matching track label width
        timeline_spacer = QWidget()
        timeline_spacer.setFixedWidth(150)
        timeline_layout.addWidget(timeline_spacer)
        
        self.timeline_widget = TimelineWidget(self)
        self.timeline_widget.setFixedHeight(28)
        timeline_layout.addWidget(self.timeline_widget, 1)
        
        layout.addWidget(timeline_container)

        # Main area: track labels + canvas
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Track labels (left side)
        self.trk_scroll = QScrollArea()
        self.trk_scroll.setWidgetResizable(False)
        self.trk_scroll.setFixedWidth(150)
        self.trk_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.trk_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.trk_widget = TrackLabelsWidget(self)
        self.trk_scroll.setWidget(self.trk_widget)
        main_layout.addWidget(self.trk_scroll)

        # Arrangement canvas (scrollable)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        
        self.canvas_widget = ArrangementCanvas(self)
        self.scroll_area.setWidget(self.canvas_widget)
        main_layout.addWidget(self.scroll_area, 1)

        layout.addLayout(main_layout)


        # Sync scrolling
        self.scroll_area.horizontalScrollBar().valueChanged.connect(self._on_hscroll)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_vscroll)

    def _on_hscroll(self, value):
        # Sync timeline with horizontal scroll
        self.timeline_widget.scroll_offset = value
        self.timeline_widget.update()
        
        # Dynamic expansion: if scrolled past 75% of current extent, expand
        scrollbar = self.scroll_area.horizontalScrollBar()
        current_beat = value / self.BW
        
        # Expand if we're in the lookahead zone
        if current_beat > self._max_scroll_beats * 0.75:
            self._max_scroll_beats = current_beat * self.LOOKAHEAD_FACTOR
            self.refresh()
        
        # Rubber-band: contract if scrolled back and no content far out
        elif current_beat < self._max_scroll_beats * 0.4:
            content_extent = self._compute_content_extent()
            # Only contract if there's no content requiring this much space
            if content_extent < self._max_scroll_beats * 0.6:
                self._max_scroll_beats = max(
                    self.MIN_BEATS,
                    content_extent * self.LOOKAHEAD_FACTOR
                )
                self.refresh()
                
    def _on_vscroll(self, value):
        # Sync track labels with vertical scroll
        self.trk_scroll.verticalScrollBar().setValue(value)

    def _snap(self, beat):
        return round(beat / self.state.snap) * self.state.snap

    def _compute_content_extent(self):
        """Calculate the rightmost beat position of any placement."""
        max_beat = 0
        
        # Check melodic placements
        for pl in self.state.placements:
            pat = self.state.find_pattern(pl.pattern_id)
            if pat:
                end_beat = pl.time + pat.length * (pl.repeats or 1)
                max_beat = max(max_beat, end_beat)
        
        # Check beat placements
        for bp in self.state.beat_placements:
            pat = self.state.find_beat_pattern(bp.pattern_id)
            if pat:
                end_beat = bp.time + pat.length * (bp.repeats or 1)
                max_beat = max(max_beat, end_beat)
        
        return max_beat
        
    def _hit_placement(self, x, y):
        """Hit test for melodic placements. Returns (placement, is_resize_handle)."""
        ti = int(y // self.TH)
        beat = x / self.BW
        if ti < 0 or ti >= len(self.state.tracks):
            return None, False
        tid = self.state.tracks[ti].id
        for pl in reversed(self.state.placements):
            if pl.track_id != tid:
                continue
            pat = self.state.find_pattern(pl.pattern_id)
            if not pat:
                continue
            tl = pat.length * (pl.repeats or 1)
            if pl.time <= beat < pl.time + tl:
                is_resize = beat > pl.time + tl - 0.5
                return pl, is_resize
        return None, False

    def _hit_beat_placement(self, x, y):
        """Hit test for beat placements."""
        ti = int(y // self.TH) - len(self.state.tracks)
        beat = x / self.BW
        if ti < 0 or ti >= len(self.state.beat_tracks):
            return None, False
        tid = self.state.beat_tracks[ti].id
        for bp in reversed(self.state.beat_placements):
            if bp.track_id != tid:
                continue
            pat = self.state.find_beat_pattern(bp.pattern_id)
            if not pat:
                continue
            tl = pat.length * (bp.repeats or 1)
            if bp.time <= beat < bp.time + tl:
                is_resize = beat > bp.time + tl - 0.5
                return bp, is_resize
        return None, False

    def refresh(self):
        """Redraw all components."""
        # Calculate dynamic extent based on content and scroll position
        content_extent = self._compute_content_extent()
        
        # Ensure we have enough space for content plus lookahead
        self._max_scroll_beats = max(
            self.MIN_BEATS,
            self._max_scroll_beats,
            content_extent * self.LOOKAHEAD_FACTOR
        )
        
        total_tracks = len(self.state.tracks) + len(self.state.beat_tracks)
        ch = max(total_tracks * self.TH, 400)
        cw = int(self._max_scroll_beats * self.BW)
                
        self.canvas_widget.setMinimumSize(cw, ch)
        self.trk_widget.setMinimumSize(150, ch)
        
        self.canvas_widget.update()
        self.trk_widget.update()
        self.timeline_widget.update()
    
    def copy_selection(self):
        """Copy selected placements to clipboard."""
        if not self.selected_placements and not self.selected_beat_placements:
            return
        self.clipboard.copy(self.selected_placements, 
                           self.selected_beat_placements, self.state)
        # Don't activate ghost mode yet - wait for paste
    
    def cut_selection(self):
        """Cut selected placements to clipboard."""
        if not self.selected_placements and not self.selected_beat_placements:
            return
        self.copy_selection()
        for pl in self.selected_placements:
            self.state.placements.remove(pl)
        for bp in self.selected_beat_placements:
            self.state.beat_placements.remove(bp)
        self.selected_placements = []
        self.selected_beat_placements = []
        self.state.notify('cut_placements')
        self.refresh()
        # Activate ghost mode immediately after cut for quick repositioning
        self._activate_ghost_mode()
    
    def paste_at_playhead(self):
        """Activate ghost mode for paste preview."""
        if not self.clipboard.has_data():
            return
        # Activate ghost mode - user will click to commit
        self._activate_ghost_mode()
    
    def _activate_ghost_mode(self):
        """Activate ghost placement preview mode."""
        if not self.clipboard.has_data():
            return
        
        # Store ghost data from clipboard
        self._ghost_placements = list(self.clipboard.data.placements)
        self._ghost_beat_placements = list(self.clipboard.data.beat_placements)
        self._ghost_offset = 0.0
        
        # Update display
        self.canvas_widget.update()
    
    def _commit_ghost_placements(self, paste_time: float):
        """Commit ghost placements to actual state."""
        if not self._ghost_placements and not self._ghost_beat_placements:
            return
        
        new_pls, new_bps = self.clipboard.paste(paste_time, self.state)
        
        # Add to state
        self.state.placements.extend(new_pls)
        self.state.beat_placements.extend(new_bps)
        
        # Select the pasted items
        self.selected_placements = new_pls
        self.selected_beat_placements = new_bps
        
        # Clear ghost mode
        self._ghost_placements = []
        self._ghost_beat_placements = []
        self._ghost_offset = 0.0
        
        self.state.notify('paste_placements')
        self.refresh()
    
    def _cancel_ghost_mode(self):
        """Cancel ghost placement mode without pasting."""
        self._ghost_placements = []
        self._ghost_beat_placements = []
        self._ghost_offset = 0.0
        self.canvas_widget.update()
    
    def delete_selection(self):
        """Delete selected placements."""
        if not self.selected_placements and not self.selected_beat_placements:
            return
        for pl in self.selected_placements:
            if pl in self.state.placements:
                self.state.placements.remove(pl)
        for bp in self.selected_beat_placements:
            if bp in self.state.beat_placements:
                self.state.beat_placements.remove(bp)
        self.selected_placements = []
        self.selected_beat_placements = []
        self.state.notify('delete_placements')
        self.refresh()
    
    def select_all(self):
        """Select all placements."""
        self.selected_placements = list(self.state.placements)
        self.selected_beat_placements = list(self.state.beat_placements)
        self.state.notify('selection_changed')
        self.canvas_widget.update()


class TimelineWidget(QWidget):
    """Timeline header showing beat numbers and loop markers."""

    MARKER_W = 8  # width of loop marker handle

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_arr = parent
        self.scroll_offset = 0
        self.setMinimumHeight(28)
        self.setMouseTracking(True)
        self._drag_marker = None  # 'start', 'end', or None

    def _beat_to_x(self, beat):
        return beat * self.parent_arr.BW - self.scroll_offset

    def _x_to_beat(self, x):
        return (x + self.scroll_offset) / self.parent_arr.BW

    def _hit_loop_marker(self, x):
        """Return 'start', 'end', or None."""
        s = self.parent_arr.state
        if not s.looping:
            return None
        ls = s.loop_start if s.loop_start is not None else 0.0
        le = s.loop_end
        if le is None:
            return None
        lx_start = self._beat_to_x(ls)
        lx_end = self._beat_to_x(le)
        if abs(x - lx_start) < self.MARKER_W + 2:
            return 'start'
        if abs(x - lx_end) < self.MARKER_W + 2:
            return 'end'
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            marker = self._hit_loop_marker(event.pos().x())
            if marker:
                self._drag_marker = marker
                return
            # Click on timeline to set playhead position (seek)
            if self.parent_arr.state.playing and self.parent_arr.app.engine:
                beat = max(0, self._x_to_beat(event.pos().x()))
                self.parent_arr.app.engine.seek(beat)

    def mouseMoveEvent(self, event):
        if self._drag_marker:
            s = self.parent_arr.state
            beat = max(0, self.parent_arr._snap(self._x_to_beat(event.pos().x())))
            if self._drag_marker == 'start':
                le = s.loop_end if s.loop_end is not None else self.parent_arr.TOT
                s.loop_start = min(beat, le - s.snap)
            elif self._drag_marker == 'end':
                ls = s.loop_start if s.loop_start is not None else 0.0
                s.loop_end = max(beat, ls + s.snap)
            # Update engine loop points live
            self.parent_arr.app._sync_loop_to_engine()
            self.parent_arr.refresh()
            return
        # Update cursor
        marker = self._hit_loop_marker(event.pos().x())
        if marker:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self._drag_marker:
            self._drag_marker = None
            self.parent_arr.state.notify('loop_markers')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        s = self.parent_arr.state
        bpm_beats = s.ts_num * (4 / s.ts_den)
        
        # Background
        painter.fillRect(self.rect(), QColor('#16213e'))

        # Loop region highlight on timeline
        if s.looping and s.loop_end is not None:
            ls = s.loop_start if s.loop_start is not None else 0.0
            le = s.loop_end
            lx1 = self._beat_to_x(ls)
            lx2 = self._beat_to_x(le)
            loop_color = QColor('#00b4d8')
            loop_color.setAlpha(40)
            painter.fillRect(int(lx1), 0, int(lx2 - lx1), 28, loop_color)
        
        # Draw beat markers - use dynamic extent
        total_beats = int(self.parent_arr._max_scroll_beats)
        for b in range(total_beats + 1):
            x = b * self.parent_arr.BW - self.scroll_offset
            if x < -50 or x > self.width() + 50:
                continue
                
            is_measure = (abs(b % bpm_beats) < 0.001) or b == 0
            if is_measure and b < total_beats:
                # Calculate absolute measure number from beat position
                measure_num = int(b / bpm_beats) + 1
                painter.setPen(QColor('#aaa'))
                painter.setFont(QFont('TkDefaultFont', 8))
                painter.drawText(x + 3, 16, str(measure_num))
                painter.setPen(QColor('#4a4a8a'))
                painter.drawLine(x, 14, x, 28)
            else:
                painter.setPen(QPen(QColor('#222244'), 0.5))
                painter.drawLine(x, 14, x, 28)
                
        # Loop markers
        if s.looping and s.loop_end is not None:
            ls = s.loop_start if s.loop_start is not None else 0.0
            le = s.loop_end
            loop_color = QColor('#00b4d8')

            for beat, is_start in [(ls, True), (le, False)]:
                mx = int(self._beat_to_x(beat))
                # Vertical line
                painter.setPen(QPen(loop_color, 2))
                painter.drawLine(mx, 0, mx, 28)
                # Triangle marker
                painter.setPen(Qt.NoPen)
                painter.setBrush(loop_color)
                if is_start:
                    # Right-pointing triangle at top
                    from PySide6.QtGui import QPolygon
                    painter.drawPolygon(QPolygon([
                        QPoint(mx, 0), QPoint(mx + 8, 6), QPoint(mx, 12)
                    ]))
                else:
                    # Left-pointing triangle at top
                    from PySide6.QtGui import QPolygon
                    painter.drawPolygon(QPolygon([
                        QPoint(mx, 0), QPoint(mx - 8, 6), QPoint(mx, 12)
                    ]))

        # Draw playhead indicator on timeline
        if s.playing and s.playhead is not None:
            px = s.playhead * self.parent_arr.BW - self.scroll_offset
            # Draw red line on timeline
            painter.setPen(QPen(QColor('#ff3355'), 2))
            painter.drawLine(int(px), 0, int(px), 28)
            
            # Draw current beat number
            current_beat = int(s.playhead) + 1
            painter.setPen(QColor('#ff3355'))
            painter.setFont(QFont('TkDefaultFont', 9, QFont.Bold))
            beat_text = f"Beat {current_beat}"
            text_width = painter.fontMetrics().horizontalAdvance(beat_text)
            text_x = int(px - text_width / 2)
            # Ensure text stays on screen
            text_x = max(2, min(text_x, self.width() - text_width - 2))
            painter.drawText(text_x, 11, beat_text)


class TrackLabelsWidget(QWidget):
    """Track labels on the left side."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_arr = parent
        self.setMinimumWidth(150)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            y = event.pos().y()
            self._on_track_click(y)
        elif event.button() == Qt.RightButton:
            y = event.pos().y()
            self._on_track_right_click(y)

    def _on_track_click(self, y):
        ti = int(y // self.parent_arr.TH)
        if ti < len(self.parent_arr.state.tracks):
            self.parent_arr._select_track(self.parent_arr.state.tracks[ti].id)
        else:
            bti = ti - len(self.parent_arr.state.tracks)
            if 0 <= bti < len(self.parent_arr.state.beat_tracks):
                self.parent_arr._select_beat_track(self.parent_arr.state.beat_tracks[bti].id)

    def _on_track_right_click(self, y):
        ti = int(y // self.parent_arr.TH)
        if ti < len(self.parent_arr.state.tracks):
            tid = self.parent_arr.state.tracks[ti].id
            self.parent_arr.app.delete_track(tid)
        else:
            bti = ti - len(self.parent_arr.state.tracks)
            if 0 <= bti < len(self.parent_arr.state.beat_tracks):
                self.parent_arr.app.delete_beat_track(self.parent_arr.state.beat_tracks[bti].id)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        s = self.parent_arr.state
        presets = s.sf2.presets if s.sf2 and hasattr(s.sf2, 'presets') else None
        if s.sf2 and isinstance(s.sf2, dict):
            presets = s.sf2.get('presets')

        # Draw melodic tracks
        for i, t in enumerate(s.tracks):
            y = i * self.parent_arr.TH
            sel = s.sel_trk == t.id
            
            if sel:
                painter.fillRect(0, y, 150, self.parent_arr.TH, QColor('#1e2040'))
                painter.setPen(QPen(QColor('#e94560'), 3))
                painter.drawLine(0, y, 0, y + self.parent_arr.TH)
            
            painter.setPen(QColor('#222244'))
            painter.drawLine(0, y + self.parent_arr.TH - 1, 150, y + self.parent_arr.TH - 1)
            
            painter.setPen(QColor('#eee'))
            painter.setFont(QFont('TkDefaultFont', 9, QFont.Bold))
            painter.drawText(8, y + 21, t.name)
            
            painter.setPen(QColor('#888'))
            painter.setFont(QFont('TkDefaultFont', 7))
            painter.drawText(8, y + 33, f'ch{t.channel + 1}')
            
            pn = preset_name(t.bank, t.program, presets)
            painter.drawText(8, y + 45, pn)

        # Draw beat tracks
        for i, bt in enumerate(s.beat_tracks):
            y = (len(s.tracks) + i) * self.parent_arr.TH
            sel = s.sel_beat_trk == bt.id
            
            if sel:
                painter.fillRect(0, y, 150, self.parent_arr.TH, QColor('#1e2040'))
                painter.setPen(QPen(QColor('#e94560'), 3))
                painter.drawLine(0, y, 0, y + self.parent_arr.TH)
            
            painter.setPen(QColor('#222244'))
            painter.drawLine(0, y + self.parent_arr.TH - 1, 150, y + self.parent_arr.TH - 1)
            
            painter.setPen(QColor('#eee'))
            painter.setFont(QFont('TkDefaultFont', 9, QFont.Bold))
            painter.drawText(8, y + 23, bt.name)
            
            painter.setPen(QColor('#e94560'))
            painter.setFont(QFont('TkDefaultFont', 7))
            painter.drawText(8, y + 38, 'Beat Track')


class ArrangementCanvas(QWidget):
    """Main arrangement canvas with placements."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_arr = parent
        # Enable keyboard events
        self.setFocusPolicy(Qt.StrongFocus)
        # Enable mouse tracking for ghost placement updates
        self.setMouseTracking(True)
    
    def keyPressEvent(self, event):
        """Handle keyboard events."""
        if event.key() == Qt.Key_Escape:
            # Cancel ghost mode
            if self.parent_arr._ghost_placements or self.parent_arr._ghost_beat_placements:
                self.parent_arr._cancel_ghost_mode()
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        x, y = event.pos().x(), event.pos().y()
        ctrl_held = event.modifiers() & Qt.ControlModifier
        shift_held = event.modifiers() & Qt.ShiftModifier
        
        if event.button() == Qt.LeftButton:
            # If in ghost mode, commit the paste
            if self.parent_arr._ghost_placements or self.parent_arr._ghost_beat_placements:
                beat_pos = max(0, self.parent_arr._snap(x / self.parent_arr.BW))
                self.parent_arr._commit_ghost_placements(beat_pos)
                return
            
            # Check existing placement click
            pl, is_resize = self.parent_arr._hit_placement(x, y)
            if pl:
                # Clear piano roll selection when selecting placement
                self.parent_arr.app.piano_roll.clear_selection()
                
                # Shift or Ctrl for multi-select
                if shift_held or ctrl_held:
                    # Toggle selection
                    if pl in self.parent_arr.selected_placements:
                        self.parent_arr.selected_placements.remove(pl)
                    else:
                        self.parent_arr.selected_placements.append(pl)
                    self.update()
                    return
                # Regular click - select this placement only
                self.parent_arr.selected_placements = [pl]
                self.parent_arr.selected_beat_placements = []
                self.parent_arr._on_click(event)
                return
            
            # Check beat placement
            bp, is_resize = self.parent_arr._hit_beat_placement(x, y)
            if bp:
                # Clear piano roll selection when selecting beat placement
                self.parent_arr.app.piano_roll.clear_selection()
                
                # Shift or Ctrl for multi-select
                if shift_held or ctrl_held:
                    if bp in self.parent_arr.selected_beat_placements:
                        self.parent_arr.selected_beat_placements.remove(bp)
                    else:
                        self.parent_arr.selected_beat_placements.append(bp)
                    self.update()
                    return
                # Regular click - select this placement only
                self.parent_arr.selected_placements = []
                self.parent_arr.selected_beat_placements = [bp]
                self.parent_arr._on_click(event)
                return
            
            # Shift+drag starts marquee selection (no placement hit)
            if shift_held:
                self.parent_arr.marquee.start(x, y)
                # Clear piano roll selection when starting marquee in arranger
                self.parent_arr.app.piano_roll.clear_selection()
                return
            
            # Clear selection if not ctrl/shift-clicking
            if not ctrl_held and not shift_held:
                self.parent_arr.selected_placements = []
                self.parent_arr.selected_beat_placements = []
            
            # No existing placement clicked - place selected pattern
            state = self.parent_arr.state
            
            # Calculate position
            beat_pos = max(0, self.parent_arr._snap(x / self.parent_arr.BW))
            track_idx = int(y // self.parent_arr.TH)
            
            # Place selected melodic pattern
            if state.sel_pat and not state.sel_beat_pat:
                from ..state import Placement, Track
                
                # Get or create track
                if track_idx >= len(state.tracks):
                    new_track = Track(
                        id=state.new_id(),
                        name=f'Track {len(state.tracks) + 1}',
                        channel=len(state.tracks) % 16,
                        bank=0,
                        program=0,
                        volume=100
                    )
                    state.tracks.append(new_track)
                    track = new_track
                else:
                    track = state.tracks[track_idx]
                
                # Create placement
                pl = Placement(
                    id=state.new_id(),
                    track_id=track.id,
                    pattern_id=state.sel_pat,
                    time=beat_pos,
                    transpose=0,
                    repeats=1
                )
                state.placements.append(pl)
                state.sel_pl = pl.id
                state.notify('placement_added')
                self.parent_arr.refresh()
                
            # Place selected beat pattern
            elif state.sel_beat_pat and not state.sel_pat:
                from ..state import BeatPlacement, BeatTrack
                
                # Adjust for beat tracks (they come after melodic tracks)
                beat_track_idx = track_idx - len(state.tracks)
                
                # Get or create beat track
                if beat_track_idx >= len(state.beat_tracks):
                    new_track = BeatTrack(
                        id=state.new_id(),
                        name=f'Beat Track {len(state.beat_tracks) + 1}'
                    )
                    state.beat_tracks.append(new_track)
                    track = new_track
                else:
                    if beat_track_idx < 0:
                        # Clicked on melodic track - create new beat track
                        new_track = BeatTrack(
                            id=state.new_id(),
                            name=f'Beat Track {len(state.beat_tracks) + 1}'
                        )
                        state.beat_tracks.append(new_track)
                        track = new_track
                    else:
                        track = state.beat_tracks[beat_track_idx]
                
                # Create beat placement
                bp = BeatPlacement(
                    id=state.new_id(),
                    track_id=track.id,
                    pattern_id=state.sel_beat_pat,
                    time=beat_pos,
                    repeats=1
                )
                state.beat_placements.append(bp)
                state.sel_beat_pl = bp.id
                state.notify('beat_placement_added')
                self.parent_arr.refresh()
            else:
                # Nothing selected or both selected - just deselect
                self.parent_arr._on_click(event)
                
        elif event.button() == Qt.RightButton:
            # Right click cancels ghost mode
            if self.parent_arr._ghost_placements or self.parent_arr._ghost_beat_placements:
                self.parent_arr._cancel_ghost_mode()
                return
            self.parent_arr._on_right_click(event)

    def mouseMoveEvent(self, event):
        # Update ghost placement position if in ghost mode
        if self.parent_arr._ghost_placements or self.parent_arr._ghost_beat_placements:
            x = event.pos().x()
            beat_pos = max(0, self.parent_arr._snap(x / self.parent_arr.BW))
            self.parent_arr._ghost_offset = beat_pos - self.parent_arr.clipboard.data.min_time
            self.update()
            return
        
        if self.parent_arr.marquee.is_active:
            self.parent_arr.marquee.update(event.pos().x(), event.pos().y())
            self.update()
            return
        if event.buttons() & Qt.LeftButton:
            self.parent_arr._on_drag(event)

    def mouseReleaseEvent(self, event):
        if self.parent_arr.marquee.is_active:
            rect = self.parent_arr.marquee.finish()
            pls, bps = select_placements_in_rect(
                rect, self.parent_arr.state,
                self.parent_arr.BW, self.parent_arr.TH
            )
            self.parent_arr.selected_placements = pls
            self.parent_arr.selected_beat_placements = bps
            self.update()
            return
        self.parent_arr._on_release(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        s = self.parent_arr.state
        bpm_beats = s.ts_num * (4 / s.ts_den)
        total_tracks = len(s.tracks) + len(s.beat_tracks)
        cw = self.width()
        ch = self.height()

        # Background
        painter.fillRect(self.rect(), QColor('#1a1a30'))

        # Track backgrounds
        for i in range(total_tracks):
            y = i * self.parent_arr.TH
            color = QColor('#181828') if i % 2 else QColor('#1a1a30')
            painter.fillRect(0, y, cw, self.parent_arr.TH, color)
            painter.setPen(QColor('#222244'))
            painter.drawLine(0, y + self.parent_arr.TH, cw, y + self.parent_arr.TH)
        
        # Beat grid lines
        total_beats = int(self.parent_arr._max_scroll_beats)
        for b in range(total_beats + 1):
            x = b * self.parent_arr.BW
            is_measure = (abs(b % bpm_beats) < 0.001) or b == 0
            color = QColor('#3a3a7a') if is_measure else QColor('#1e1e3a')
            width = 1 if is_measure else 0.5
            painter.setPen(QPen(color, width))
            painter.drawLine(x, 0, x, ch)

        # Melodic placements
        for pl in s.placements:
            ti = next((i for i, t in enumerate(s.tracks) if t.id == pl.track_id), -1)
            pat = s.find_pattern(pl.pattern_id)
            if ti < 0 or not pat:
                continue
            y = ti * self.parent_arr.TH
            x = pl.time * self.parent_arr.BW
            tl = pat.length * (pl.repeats or 1)
            w = tl * self.parent_arr.BW
            sel = s.sel_pl == pl.id

            # Block with transparency
            if sel:
                painter.setPen(QPen(QColor('#fff'), 2))
                painter.setBrush(QColor(pat.color))
            else:
                painter.setPen(Qt.NoPen)
                fill_color = QColor(pat.color)
                fill_color.setAlpha(136)  # 0x88
                painter.setBrush(fill_color)
            
            painter.drawRect(int(x), y + 2, int(w - 1), self.parent_arr.TH - 4)

            # Repeat dividers
            for r in range(1, pl.repeats or 1):
                rx = x + r * pat.length * self.parent_arr.BW
                painter.setPen(QPen(QColor(255, 255, 255, 68), 1, Qt.DashLine))
                painter.drawLine(int(rx), y + 4, int(rx), y + self.parent_arr.TH - 4)

            # Mini note preview
            if pat.notes:
                pitches = [n.pitch for n in pat.notes]
                mn, mx = min(pitches), max(pitches)
                rg = max(1, mx - mn)
                for n in pat.notes:
                    ny = y + self.parent_arr.TH - 6 - ((n.pitch - mn) / rg) * (self.parent_arr.TH - 12)
                    nx = x + n.start / pat.length * pat.length * self.parent_arr.BW
                    nw = max(2, n.duration / pat.length * pat.length * self.parent_arr.BW)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(255, 255, 255, 85))
                    painter.drawRect(int(nx + 2), int(ny), int(nw - 1), 2)

            # Label
            label = pat.name
            ts = s.compute_transpose(pl)
            if ts:
                label += f' ({ts:+d})'
            if pl.target_key and pl.target_key != (pat.key or 'C'):
                label += f' -> {pl.target_key}'
            painter.setPen(QColor('#fff'))
            painter.setFont(QFont('TkDefaultFont', 8))
            painter.drawText(int(x + 4), y + 20, label)

            # Resize handle
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 68))
            painter.drawRect(int(x + w - 5), y + 2, 4, self.parent_arr.TH - 4)

        # Beat placements
        for bp in s.beat_placements:
            ti = next((i for i, t in enumerate(s.beat_tracks) if t.id == bp.track_id), -1)
            pat = s.find_beat_pattern(bp.pattern_id)
            if ti < 0 or not pat:
                continue
            y = (len(s.tracks) + ti) * self.parent_arr.TH
            x = bp.time * self.parent_arr.BW
            tl = pat.length * (bp.repeats or 1)
            w = tl * self.parent_arr.BW
            sel = s.sel_beat_pl == bp.id

            if sel:
                painter.setPen(QPen(QColor('#fff'), 2))
                painter.setBrush(QColor(pat.color))
            else:
                painter.setPen(Qt.NoPen)
                fill_color = QColor(pat.color)
                fill_color.setAlpha(136)
                painter.setBrush(fill_color)
            
            painter.drawRect(int(x), y + 2, int(w - 1), self.parent_arr.TH - 4)

            for r in range(1, bp.repeats or 1):
                rx = x + r * pat.length * self.parent_arr.BW
                painter.setPen(QPen(QColor(255, 255, 255, 68), 1, Qt.DashLine))
                painter.drawLine(int(rx), y + 4, int(rx), y + self.parent_arr.TH - 4)

            painter.setPen(QColor('#fff'))
            painter.setFont(QFont('TkDefaultFont', 8))
            painter.drawText(int(x + 4), y + 20, pat.name)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 68))
            painter.drawRect(int(x + w - 5), y + 2, 4, self.parent_arr.TH - 4)

        # Loop region shading on canvas
        if s.looping and s.loop_end is not None:
            ls = s.loop_start if s.loop_start is not None else 0.0
            le = s.loop_end
            lx1 = int(ls * self.parent_arr.BW)
            lx2 = int(le * self.parent_arr.BW)

            # Dim areas outside the loop
            outside_color = QColor(0, 0, 0, 60)
            if lx1 > 0:
                painter.fillRect(0, 0, lx1, ch, outside_color)
            if lx2 < cw:
                painter.fillRect(lx2, 0, cw - lx2, ch, outside_color)

            # Loop boundary lines
            loop_color = QColor('#00b4d8')
            painter.setPen(QPen(loop_color, 1.5, Qt.DashLine))
            painter.drawLine(lx1, 0, lx1, ch)
            painter.drawLine(lx2, 0, lx2, ch)

        # Playhead
        if s.playing and s.playhead is not None:
            px = s.playhead * self.parent_arr.BW
            # Draw playhead as a bright red line
            painter.setPen(QPen(QColor('#ff3355'), 2))
            painter.drawLine(int(px), 0, int(px), ch)
        
        # Draw ghost placements (paste preview)
        if self.parent_arr._ghost_placements or self.parent_arr._ghost_beat_placements:
            time_offset = self.parent_arr._ghost_offset
            
            # Draw ghost melodic placements
            for pl_dict in self.parent_arr._ghost_placements:
                ti = next((i for i, t in enumerate(s.tracks) if t.id == pl_dict['trackId']), -1)
                pat = s.find_pattern(pl_dict['patternId'])
                if ti < 0 or not pat:
                    continue
                
                y = ti * self.parent_arr.TH
                ghost_time = pl_dict['time'] + time_offset
                x = ghost_time * self.parent_arr.BW
                w = pat.length * pl_dict.get('repeats', 1) * self.parent_arr.BW
                
                # Semi-transparent ghost appearance
                ghost_color = QColor(pat.color)
                ghost_color.setAlpha(80)
                painter.setPen(QPen(QColor('#fff'), 1, Qt.DashLine))
                painter.setBrush(ghost_color)
                painter.drawRect(int(x), y + 2, int(w - 1), self.parent_arr.TH - 4)
                
                # Label
                painter.setPen(QColor(255, 255, 255, 120))
                painter.setFont(QFont('TkDefaultFont', 8))
                painter.drawText(int(x + 4), y + 20, pat.name)
            
            # Draw ghost beat placements
            for bp_dict in self.parent_arr._ghost_beat_placements:
                ti = next((i for i, t in enumerate(s.beat_tracks) if t.id == bp_dict['trackId']), -1)
                pat = s.find_beat_pattern(bp_dict['patternId'])
                if ti < 0 or not pat:
                    continue
                
                y = (len(s.tracks) + ti) * self.parent_arr.TH
                ghost_time = bp_dict['time'] + time_offset
                x = ghost_time * self.parent_arr.BW
                w = pat.length * bp_dict.get('repeats', 1) * self.parent_arr.BW
                
                # Semi-transparent ghost appearance
                ghost_color = QColor(pat.color)
                ghost_color.setAlpha(80)
                painter.setPen(QPen(QColor('#fff'), 1, Qt.DashLine))
                painter.setBrush(ghost_color)
                painter.drawRect(int(x), y + 2, int(w - 1), self.parent_arr.TH - 4)
                
                painter.setPen(QColor(255, 255, 255, 120))
                painter.setFont(QFont('TkDefaultFont', 8))
                painter.drawText(int(x + 4), y + 20, pat.name)
        
        # Draw selection highlights
        for pl in self.parent_arr.selected_placements:
            ti = next((i for i, t in enumerate(s.tracks) if t.id == pl.track_id), -1)
            pat = s.find_pattern(pl.pattern_id)
            if ti >= 0 and pat:
                y = ti * self.parent_arr.TH
                x = pl.time * self.parent_arr.BW
                w = pat.length * (pl.repeats or 1) * self.parent_arr.BW
                painter.setPen(QPen(QColor('#00ff88'), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(int(x), y + 2, int(w - 1), self.parent_arr.TH - 4)
        
        for bp in self.parent_arr.selected_beat_placements:
            ti = next((i for i, t in enumerate(s.beat_tracks) if t.id == bp.track_id), -1)
            pat = s.find_beat_pattern(bp.pattern_id)
            if ti >= 0 and pat:
                y = (len(s.tracks) + ti) * self.parent_arr.TH
                x = bp.time * self.parent_arr.BW
                w = pat.length * (bp.repeats or 1) * self.parent_arr.BW
                painter.setPen(QPen(QColor('#00ff88'), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(int(x), y + 2, int(w - 1), self.parent_arr.TH - 4)
        
        # Draw marquee rectangle
        if self.parent_arr.marquee.is_active:
            rect = self.parent_arr.marquee.get_rect()
            painter.setPen(QPen(QColor('#00ff88'), 1, Qt.DashLine))
            fill = QColor('#00ff88')
            fill.setAlpha(30)
            painter.setBrush(fill)
            painter.drawRect(rect)


# Add missing methods to ArrangementView
def _on_click(self, event):
    x, y = event.pos().x(), event.pos().y()
    # Check melodic placements
    pl, is_resize = self._hit_placement(x, y)
    if pl:
        self.state.sel_pl = pl.id
        self.state.sel_beat_pl = None
        if is_resize:
            self._resize_pl = pl
        else:
            self._drag_pl = pl
            self._drag_offset = x / self.BW - pl.time
        self.state.notify('sel_pl')
        return

    # Check beat placements
    bp, is_resize = self._hit_beat_placement(x, y)
    if bp:
        self.state.sel_beat_pl = bp.id
        self.state.sel_pl = None
        if is_resize:
            self._resize_beat_pl = bp
        else:
            self._drag_beat_pl = bp
            self._drag_offset = x / self.BW - bp.time
        self.state.notify('sel_beat_pl')
        return

    # Clicked empty space - deselect
    self.state.sel_pl = None
    self.state.sel_beat_pl = None
    self.state.notify('sel_pl')

def _on_right_click(self, event):
    x, y = event.pos().x(), event.pos().y()
    # Delete melodic placement
    pl, _ = self._hit_placement(x, y)
    if pl:
        self.state.placements = [p for p in self.state.placements if p.id != pl.id]
        self.state.sel_pl = None
        self.state.notify('del_pl')
        return
    # Delete beat placement
    bp, _ = self._hit_beat_placement(x, y)
    if bp:
        self.state.beat_placements = [p for p in self.state.beat_placements if p.id != bp.id]
        self.state.sel_beat_pl = None
        self.state.notify('del_beat_pl')
        return

def _on_drag(self, event):
    x, y = event.pos().x(), event.pos().y()
    beat = x / self.BW

    if self._drag_pl:
        self._drag_pl.time = max(0, self._snap(beat - self._drag_offset))
        ti = int(y // self.TH)
        if 0 <= ti < len(self.state.tracks):
            self._drag_pl.track_id = self.state.tracks[ti].id
        self.refresh()
    elif self._resize_pl:
        new_len = max(self.state.snap, self._snap(beat - self._resize_pl.time))
        pat = self.state.find_pattern(self._resize_pl.pattern_id)
        if pat:
            self._resize_pl.repeats = max(1, round(new_len / pat.length))
        self.refresh()
    elif self._drag_beat_pl:
        self._drag_beat_pl.time = max(0, self._snap(beat - self._drag_offset))
        ti = int(y // self.TH) - len(self.state.tracks)
        if 0 <= ti < len(self.state.beat_tracks):
            self._drag_beat_pl.track_id = self.state.beat_tracks[ti].id
        self.refresh()
    elif self._resize_beat_pl:
        new_len = max(self.state.snap, self._snap(beat - self._resize_beat_pl.time))
        pat = self.state.find_beat_pattern(self._resize_beat_pl.pattern_id)
        if pat:
            self._resize_beat_pl.repeats = max(1, round(new_len / pat.length))
        self.refresh()

def _on_release(self, event):
    if self._drag_pl or self._resize_pl:
        self.state.notify('placement_edit')
    if self._drag_beat_pl or self._resize_beat_pl:
        self.state.notify('beat_placement_edit')
    self._drag_pl = None
    self._resize_pl = None
    self._drag_beat_pl = None
    self._resize_beat_pl = None

def _select_track(self, tid):
    self.state.sel_trk = tid
    self.state.sel_beat_trk = None
    self.state.notify('sel_trk')

def _select_beat_track(self, btid):
    self.state.sel_beat_trk = btid
    self.state.sel_trk = None
    self.state.notify('sel_beat_trk')

# Attach methods to ArrangementView
ArrangementView._on_click = _on_click
ArrangementView._on_right_click = _on_right_click
ArrangementView._on_drag = _on_drag
ArrangementView._on_release = _on_release
ArrangementView._select_track = _select_track
ArrangementView._select_beat_track = _select_beat_track
