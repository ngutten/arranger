"""Arrangement timeline canvas - track lanes, placements, and playhead."""

import tkinter as tk
from tkinter import ttk
import math

from ..state import preset_name


class ArrangementView(ttk.Frame):
    """Arrangement timeline with track labels, canvas, and timeline header."""

    # Layout constants
    TH = 56    # track height
    BW = 30    # pixels per beat
    TOT = 64   # total beats shown

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

        self._build()

    def _build(self):
        # Timeline header
        self.timeline = tk.Canvas(self, height=28, bg='#16213e', highlightthickness=0)
        self.timeline.pack(fill=tk.X, side=tk.TOP)

        # Main area: track labels + canvas
        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True)

        # Track labels (left side)
        self.trk_frame = tk.Frame(main, width=150, bg='#16213e')
        self.trk_frame.pack(side=tk.LEFT, fill=tk.Y)
        self.trk_frame.pack_propagate(False)

        self.trk_canvas = tk.Canvas(self.trk_frame, bg='#16213e', highlightthickness=0,
                                     width=150)
        self.trk_canvas.pack(fill=tk.BOTH, expand=True)

        # Arrangement canvas (scrollable)
        canvas_frame = ttk.Frame(main)
        canvas_frame.pack(fill=tk.BOTH, expand=True)

        self.hscroll = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
        self.vscroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self.canvas = tk.Canvas(canvas_frame, bg='#1a1a30', highlightthickness=0,
                                 xscrollcommand=self.hscroll.set,
                                 yscrollcommand=self.vscroll.set)

        self.hscroll.configure(command=self.canvas.xview)
        self.vscroll.configure(command=self.canvas.yview)

        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.vscroll.grid(row=0, column=1, sticky='ns')
        self.hscroll.grid(row=1, column=0, sticky='ew')
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        # Scroll sync
        self.canvas.bind('<Configure>', lambda e: self._on_configure())
        self.canvas.configure(xscrollcommand=self._on_hscroll,
                               yscrollcommand=self._on_vscroll)

        # Mouse events
        self.canvas.bind('<Button-1>', self._on_click)
        self.canvas.bind('<Button-3>', self._on_right_click)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)

    def _on_configure(self):
        self.refresh()

    def _on_hscroll(self, *args):
        self.hscroll.set(*args)
        # Sync timeline with horizontal scroll
        self.timeline.xview_moveto(float(args[0]))

    def _on_vscroll(self, *args):
        self.vscroll.set(*args)
        # Sync track labels with vertical scroll
        self.trk_canvas.yview_moveto(float(args[0]))

    def _canvas_coords(self, event):
        """Convert event coords to canvas coords."""
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        return x, y

    def _snap(self, beat):
        return round(beat / self.state.snap) * self.state.snap

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

    def _on_click(self, event):
        x, y = self._canvas_coords(event)
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
        x, y = self._canvas_coords(event)
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
        x, y = self._canvas_coords(event)
        beat = x / self.BW

        if self._drag_pl:
            self._drag_pl.time = max(0, self._snap(beat - self._drag_offset))
            ti = int(y // self.TH)
            if 0 <= ti < len(self.state.tracks):
                self._drag_pl.track_id = self.state.tracks[ti].id
            self.refresh()
        elif self._resize_pl:
            pat = self.state.find_pattern(self._resize_pl.pattern_id)
            if pat:
                self._resize_pl.repeats = max(1, round((beat - self._resize_pl.time) / pat.length))
            self.refresh()
        elif self._drag_beat_pl:
            self._drag_beat_pl.time = max(0, self._snap(beat - self._drag_offset))
            ti = int(y // self.TH) - len(self.state.tracks)
            if 0 <= ti < len(self.state.beat_tracks):
                self._drag_beat_pl.track_id = self.state.beat_tracks[ti].id
            self.refresh()
        elif self._resize_beat_pl:
            pat = self.state.find_beat_pattern(self._resize_beat_pl.pattern_id)
            if pat:
                self._resize_beat_pl.repeats = max(1, round((beat - self._resize_beat_pl.time) / pat.length))
            self.refresh()

    def _on_release(self, event):
        self._drag_pl = None
        self._resize_pl = None
        self._drag_beat_pl = None
        self._resize_beat_pl = None
        self.state.notify('arr_release')

    def handle_drop(self, dtype, pid, x, y):
        """Handle a pattern drop from the pattern list."""
        # Convert root window coords to canvas coords
        cx = x - self.canvas.winfo_rootx()
        cy = y - self.canvas.winfo_rooty()
        canvas_x = self.canvas.canvasx(cx)
        canvas_y = self.canvas.canvasy(cy)
        ti = int(canvas_y // self.TH)
        beat = self._snap(canvas_x / self.BW)

        if dtype == 'pattern' and 0 <= ti < len(self.state.tracks):
            from ..state import Placement
            pat = self.state.find_pattern(pid)
            pl = Placement(
                id=self.state.new_id(), track_id=self.state.tracks[ti].id,
                pattern_id=pid, time=beat, transpose=0, repeats=1,
                target_key=pat.key if pat else 'C',
                target_scale=pat.scale if pat else 'major',
            )
            self.state.placements.append(pl)
            self.state.notify('drop_pl')
        elif dtype == 'beatPattern':
            bti = ti - len(self.state.tracks)
            if 0 <= bti < len(self.state.beat_tracks):
                from ..state import BeatPlacement
                bp = BeatPlacement(
                    id=self.state.new_id(),
                    track_id=self.state.beat_tracks[bti].id,
                    pattern_id=pid, time=beat, repeats=1,
                )
                self.state.beat_placements.append(bp)
                self.state.notify('drop_beat_pl')

    def refresh(self):
        """Redraw the arrangement canvas."""
        s = self.state
        total_tracks = len(s.tracks) + len(s.beat_tracks)
        bpm_beats = s.ts_num * (4 / s.ts_den)

        cw = max(self.TOT * self.BW, self.canvas.winfo_width())
        ch = max(total_tracks * self.TH, self.canvas.winfo_height())

        self.canvas.delete('all')
        self.canvas.configure(scrollregion=(0, 0, cw, ch))

        # Track backgrounds
        for i in range(total_tracks):
            y = i * self.TH
            color = '#181828' if i % 2 else '#1a1a30'
            self.canvas.create_rectangle(0, y, cw, y + self.TH, fill=color, outline='')
            self.canvas.create_line(0, y + self.TH, cw, y + self.TH, fill='#222244')

        # Beat grid lines
        for b in range(self.TOT + 1):
            x = b * self.BW
            is_measure = (abs(b % bpm_beats) < 0.001) or b == 0
            color = '#3a3a7a' if is_measure else '#1e1e3a'
            width = 1 if is_measure else 0.5
            self.canvas.create_line(x, 0, x, ch, fill=color, width=width)

        # Melodic placements
        for pl in s.placements:
            ti = next((i for i, t in enumerate(s.tracks) if t.id == pl.track_id), -1)
            pat = s.find_pattern(pl.pattern_id)
            if ti < 0 or not pat:
                continue
            y = ti * self.TH
            x = pl.time * self.BW
            tl = pat.length * (pl.repeats or 1)
            w = tl * self.BW
            sel = s.sel_pl == pl.id

            # Block
            alpha_hex = 'cc' if sel else '88'
            self.canvas.create_rectangle(x, y + 2, x + w - 1, y + self.TH - 2,
                                          fill=pat.color, outline='#fff' if sel else '',
                                          width=2 if sel else 0,
                                          stipple='gray50' if not sel else '')

            # Repeat dividers
            for r in range(1, pl.repeats or 1):
                rx = x + r * pat.length * self.BW
                self.canvas.create_line(rx, y + 4, rx, y + self.TH - 4,
                                         fill='#ffffff44', dash=(3, 3))

            # Mini note preview
            if pat.notes:
                pitches = [n.pitch for n in pat.notes]
                mn, mx = min(pitches), max(pitches)
                rg = max(1, mx - mn)
                for n in pat.notes:
                    ny = y + self.TH - 6 - ((n.pitch - mn) / rg) * (self.TH - 12)
                    nx = x + n.start / pat.length * pat.length * self.BW
                    nw = max(2, n.duration / pat.length * pat.length * self.BW)
                    self.canvas.create_rectangle(nx + 2, ny, nx + nw - 1, ny + 2,
                                                  fill='#ffffff55', outline='')

            # Label
            label = pat.name
            ts = s.compute_transpose(pl)
            if ts:
                label += f' ({ts:+d})'
            if pl.target_key and pl.target_key != (pat.key or 'C'):
                label += f' -> {pl.target_key}'
            self.canvas.create_text(x + 4, y + 12, text=label, fill='#fff',
                                     font=('TkDefaultFont', 8), anchor='w')

            # Resize handle
            self.canvas.create_rectangle(x + w - 5, y + 2, x + w - 1, y + self.TH - 2,
                                          fill='#ffffff44', outline='')

        # Beat placements
        for bp in s.beat_placements:
            ti = next((i for i, t in enumerate(s.beat_tracks) if t.id == bp.track_id), -1)
            pat = s.find_beat_pattern(bp.pattern_id)
            if ti < 0 or not pat:
                continue
            y = (len(s.tracks) + ti) * self.TH
            x = bp.time * self.BW
            tl = pat.length * (bp.repeats or 1)
            w = tl * self.BW
            sel = s.sel_beat_pl == bp.id

            self.canvas.create_rectangle(x, y + 2, x + w - 1, y + self.TH - 2,
                                          fill=pat.color, outline='#fff' if sel else '',
                                          width=2 if sel else 0,
                                          stipple='gray50' if not sel else '')

            for r in range(1, bp.repeats or 1):
                rx = x + r * pat.length * self.BW
                self.canvas.create_line(rx, y + 4, rx, y + self.TH - 4,
                                         fill='#ffffff44', dash=(3, 3))

            self.canvas.create_text(x + 4, y + 12, text=pat.name, fill='#fff',
                                     font=('TkDefaultFont', 8), anchor='w')

            self.canvas.create_rectangle(x + w - 5, y + 2, x + w - 1, y + self.TH - 2,
                                          fill='#ffffff44', outline='')

        # Playhead
        if s.playing and s.playhead is not None:
            px = s.playhead * self.BW
            self.canvas.create_line(px, 0, px, ch, fill='#fff', width=2)

        # Timeline header
        self._draw_timeline(cw, bpm_beats)

        # Track labels
        self._draw_track_labels(total_tracks)

    def _draw_timeline(self, cw, bpm_beats):
        tl = self.timeline
        tl.delete('all')
        tl.configure(scrollregion=(0, 0, cw, 28))
        tl.create_rectangle(0, 0, cw, 28, fill='#16213e', outline='')
        mn = 1
        for b in range(self.TOT + 1):
            x = b * self.BW
            is_measure = (abs(b % bpm_beats) < 0.001) or b == 0
            if is_measure and b < self.TOT:
                tl.create_text(x + 3, 8, text=str(mn), fill='#aaa',
                                font=('TkDefaultFont', 8), anchor='w')
                mn += 1
                tl.create_line(x, 14, x, 28, fill='#4a4a8a')
            else:
                tl.create_line(x, 14, x, 28, fill='#222244', width=0.5)

    def _draw_track_labels(self, total_tracks):
        tc = self.trk_canvas
        tc.delete('all')
        ch = max(total_tracks * self.TH, tc.winfo_height())
        tc.configure(scrollregion=(0, 0, 150, ch))

        presets = self.state.sf2.presets if self.state.sf2 and hasattr(self.state.sf2, 'presets') else None
        if self.state.sf2 and isinstance(self.state.sf2, dict):
            presets = self.state.sf2.get('presets')

        for i, t in enumerate(self.state.tracks):
            y = i * self.TH
            sel = self.state.sel_trk == t.id
            if sel:
                tc.create_rectangle(0, y, 150, y + self.TH, fill='#1e2040', outline='')
                tc.create_line(0, y, 0, y + self.TH, fill='#e94560', width=3)
            tc.create_rectangle(0, y + self.TH - 1, 150, y + self.TH, fill='#222244', outline='')
            tc.create_text(8, y + 12, text=f'{t.name}', fill='#eee',
                            font=('TkDefaultFont', 9, 'bold'), anchor='w')
            tc.create_text(8, y + 25, text=f'ch{t.channel + 1}', fill='#888',
                            font=('TkDefaultFont', 7), anchor='w')
            pn = preset_name(t.bank, t.program, presets)
            tc.create_text(8, y + 37, text=pn, fill='#888',
                            font=('TkDefaultFont', 7), anchor='w')

            # Bind click
            tc.tag_bind(f'trk_{t.id}', '<Button-1>',
                         lambda e, tid=t.id: self._select_track(tid))

        # Beat tracks
        for i, bt in enumerate(self.state.beat_tracks):
            y = (len(self.state.tracks) + i) * self.TH
            sel = self.state.sel_beat_trk == bt.id
            if sel:
                tc.create_rectangle(0, y, 150, y + self.TH, fill='#1e2040', outline='')
                tc.create_line(0, y, 0, y + self.TH, fill='#e94560', width=3)
            tc.create_rectangle(0, y + self.TH - 1, 150, y + self.TH, fill='#222244', outline='')
            tc.create_text(8, y + 15, text=f'{bt.name}', fill='#eee',
                            font=('TkDefaultFont', 9, 'bold'), anchor='w')
            tc.create_text(8, y + 30, text='Beat Track', fill='#e94560',
                            font=('TkDefaultFont', 7), anchor='w')

        # Rebind all track label clicks
        tc.bind('<Button-1>', self._on_track_click)
        tc.bind('<Button-3>', self._on_track_right_click)

    def _on_track_click(self, event):
        y = self.trk_canvas.canvasy(event.y)
        ti = int(y // self.TH)
        if ti < len(self.state.tracks):
            self._select_track(self.state.tracks[ti].id)
        else:
            bti = ti - len(self.state.tracks)
            if 0 <= bti < len(self.state.beat_tracks):
                self._select_beat_track(self.state.beat_tracks[bti].id)

    def _on_track_right_click(self, event):
        y = self.trk_canvas.canvasy(event.y)
        ti = int(y // self.TH)
        if ti < len(self.state.tracks):
            tid = self.state.tracks[ti].id
            self.app.delete_track(tid)
        else:
            bti = ti - len(self.state.tracks)
            if 0 <= bti < len(self.state.beat_tracks):
                self.app.delete_beat_track(self.state.beat_tracks[bti].id)

    def _select_track(self, tid):
        self.state.sel_trk = tid
        self.state.sel_beat_trk = None
        self.state.notify('sel_trk')

    def _select_beat_track(self, btid):
        self.state.sel_beat_trk = btid
        self.state.sel_trk = None
        self.state.notify('sel_beat_trk')
