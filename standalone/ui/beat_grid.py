"""Beat grid editor - drum pattern editing on a step sequencer grid."""

import tkinter as tk
from tkinter import ttk

from ..state import PALETTE, vel_color


class BeatGrid(ttk.Frame):
    """Beat grid editor displayed when a beat pattern is selected."""

    RH = 28    # row height
    CW = 24    # cell width

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self._build()

    def _build(self):
        # Header
        hdr = ttk.Frame(self)
        hdr.pack(fill=tk.X)

        self.name_label = ttk.Label(hdr, text='No beat pattern', font=('TkDefaultFont', 9))
        self.name_label.pack(side=tk.LEFT, padx=8)

        ttk.Button(hdr, text='Preview', command=self.app.preview_beat_pattern).pack(
            side=tk.LEFT, padx=4)

        # Main area: lane labels + grid canvas
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)

        # Lane labels
        self.lane_canvas = tk.Canvas(body, width=70, bg='#16213e', highlightthickness=0)
        self.lane_canvas.pack(side=tk.LEFT, fill=tk.Y)

        # Grid canvas
        grid_frame = ttk.Frame(body)
        grid_frame.pack(fill=tk.BOTH, expand=True)

        self.hscroll = ttk.Scrollbar(grid_frame, orient=tk.HORIZONTAL)
        self.vscroll = ttk.Scrollbar(grid_frame, orient=tk.VERTICAL)
        self.canvas = tk.Canvas(grid_frame, bg='#1a1a30', highlightthickness=0,
                                 xscrollcommand=self.hscroll.set,
                                 yscrollcommand=self._on_vscroll)
        self.hscroll.configure(command=self.canvas.xview)
        self.vscroll.configure(command=self._scroll_both_y)

        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.vscroll.grid(row=0, column=1, sticky='ns')
        self.hscroll.grid(row=1, column=0, sticky='ew')
        grid_frame.grid_rowconfigure(0, weight=1)
        grid_frame.grid_columnconfigure(0, weight=1)

        # Mouse events
        self.canvas.bind('<Button-1>', self._on_click)
        self.canvas.bind('<Button-3>', self._on_right_click)

        # Lane click for preview
        self.lane_canvas.bind('<Button-1>', self._on_lane_click)

    def _scroll_both_y(self, *args):
        self.canvas.yview(*args)
        self.lane_canvas.yview(*args)

    def _on_vscroll(self, *args):
        self.vscroll.set(*args)
        self.lane_canvas.yview_moveto(float(args[0]))

    def _canvas_coords(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _on_click(self, event):
        pat = self.state.find_beat_pattern(self.state.sel_beat_pat)
        if not pat or not self.state.beat_kit:
            return
        x, y = self._canvas_coords(event)
        row = int(y // self.RH)
        col = int(x // self.CW)
        if row < 0 or row >= len(self.state.beat_kit):
            return
        num_cols = int(pat.length * pat.subdivision)
        if col < 0 or col >= num_cols:
            return

        inst = self.state.beat_kit[row]
        grid = pat.grid.get(inst.id)
        if grid is None:
            return

        vel = self.state.default_vel
        if grid[col] > 0:
            grid[col] = 0
        else:
            grid[col] = vel
            self.app.play_beat_hit(inst.id)
        self.refresh()

    def _on_right_click(self, event):
        pat = self.state.find_beat_pattern(self.state.sel_beat_pat)
        if not pat or not self.state.beat_kit:
            return
        x, y = self._canvas_coords(event)
        row = int(y // self.RH)
        col = int(x // self.CW)
        if row < 0 or row >= len(self.state.beat_kit):
            return
        num_cols = int(pat.length * pat.subdivision)
        if col < 0 or col >= num_cols:
            return

        inst = self.state.beat_kit[row]
        grid = pat.grid.get(inst.id)
        if grid is None:
            return
        grid[col] = 0
        self.refresh()

    def _on_lane_click(self, event):
        y = self.lane_canvas.canvasy(event.y)
        row = int(y // self.RH)
        if 0 <= row < len(self.state.beat_kit):
            self.app.play_beat_hit(self.state.beat_kit[row].id)

    def refresh(self):
        """Redraw the beat grid."""
        pat = self.state.find_beat_pattern(self.state.sel_beat_pat)

        if pat:
            self.name_label.configure(
                text=f'{pat.name} ({pat.length}b, /{pat.subdivision})')
        else:
            self.name_label.configure(text='No beat pattern')

        self._draw_lanes()
        self._draw_grid(pat)

    def _draw_lanes(self):
        lc = self.lane_canvas
        lc.delete('all')
        num_rows = len(self.state.beat_kit)
        total_h = max(num_rows * self.RH, lc.winfo_height())
        lc.configure(scrollregion=(0, 0, 70, total_h))

        for i, inst in enumerate(self.state.beat_kit):
            y = i * self.RH
            color = PALETTE[i % len(PALETTE)]
            lc.create_rectangle(0, y, 70, y + self.RH, fill='#16213e', outline='#222244')
            # Color dot
            lc.create_oval(6, y + self.RH // 2 - 4, 14, y + self.RH // 2 + 4,
                            fill=color, outline='')
            # Name
            lc.create_text(18, y + self.RH // 2, text=inst.name, fill='#eee',
                            font=('TkDefaultFont', 7), anchor='w')

    def _draw_grid(self, pat):
        cv = self.canvas
        cv.delete('all')

        if not pat:
            return

        num_rows = len(self.state.beat_kit)
        num_cols = int(pat.length * pat.subdivision)
        total_w = max(num_cols * self.CW, cv.winfo_width())
        total_h = max(num_rows * self.RH, cv.winfo_height())
        cv.configure(scrollregion=(0, 0, total_w, total_h))

        bpm_beats = self.state.ts_num * (4 / self.state.ts_den)

        # Row backgrounds
        for i in range(num_rows):
            y = i * self.RH
            bg = '#181828' if i % 2 else '#1a1a30'
            cv.create_rectangle(0, y, total_w, y + self.RH, fill=bg, outline='')
            cv.create_line(0, y + self.RH, total_w, y + self.RH, fill='#222244')

        # Column lines
        for col in range(num_cols + 1):
            x = col * self.CW
            beat_num = col / pat.subdivision
            is_measure = abs(beat_num % bpm_beats) < 0.001 or beat_num == 0
            is_beat = col % pat.subdivision == 0

            if is_measure:
                color, width = '#4a4a8a', 1.5
            elif is_beat:
                color, width = '#3a3a6a', 1
            else:
                color, width = '#2a2a4a', 0.5
            cv.create_line(x, 0, x, total_h, fill=color, width=width)

        # Grid cells
        for row, inst in enumerate(self.state.beat_kit):
            grid = pat.grid.get(inst.id)
            if not grid:
                continue

            y = row * self.RH
            color = PALETTE[row % len(PALETTE)]

            for col, vel in enumerate(grid):
                if vel > 0:
                    x = col * self.CW
                    vc = vel_color(vel)
                    cv.create_rectangle(x + 1, y + 2, x + self.CW - 1, y + self.RH - 2,
                                         fill=vc, outline=color)
                    # Show velocity if cell is wide enough
                    if self.CW >= 20 and vel >= 10:
                        cv.create_text(x + 4, y + self.RH - 6, text=str(vel),
                                        fill='#fff', font=('TkDefaultFont', 6), anchor='w')
