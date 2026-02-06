"""Piano roll editor - note editing on a pitch/time grid."""

import tkinter as tk
from tkinter import ttk

from ..state import NOTE_NAMES, scale_set, vel_color, Note


class PianoRoll(ttk.Frame):
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
        self._resize_note = None
        self._selected = set()

        self._build()

    def _build(self):
        # Header bar
        hdr = ttk.Frame(self)
        hdr.pack(fill=tk.X)

        self.name_label = ttk.Label(hdr, text='No pattern', font=('TkDefaultFont', 9))
        self.name_label.pack(side=tk.LEFT, padx=8)

        ttk.Button(hdr, text='Preview', command=self.app.preview_pattern).pack(
            side=tk.LEFT, padx=4)

        ttk.Frame(hdr).pack(side=tk.LEFT, expand=True, fill=tk.X)

        # Note length
        ttk.Label(hdr, text='Len').pack(side=tk.LEFT, padx=(4, 2))
        self.note_len_var = tk.StringVar(value=self.state.note_len)
        len_cb = ttk.Combobox(hdr, textvariable=self.note_len_var, width=5,
                               values=['snap', 'last', '0.0625', '0.125', '0.25',
                                       '0.5', '1', '2', '4'],
                               state='readonly')
        len_cb.pack(side=tk.LEFT, padx=2)
        len_cb.bind('<<ComboboxSelected>>', self._on_note_len)

        # Tool buttons
        self.tool_frame = ttk.Frame(hdr)
        self.tool_frame.pack(side=tk.LEFT, padx=8)
        self.draw_btn = ttk.Button(self.tool_frame, text='Draw', width=5,
                                    command=lambda: self._set_tool('draw'))
        self.draw_btn.pack(side=tk.LEFT, padx=1)
        self.sel_btn = ttk.Button(self.tool_frame, text='Sel', width=4,
                                   command=lambda: self._set_tool('select'))
        self.sel_btn.pack(side=tk.LEFT, padx=1)
        self.del_btn = ttk.Button(self.tool_frame, text='Del', width=4,
                                   command=lambda: self._set_tool('erase'))
        self.del_btn.pack(side=tk.LEFT, padx=1)

        # Velocity slider
        ttk.Label(hdr, text='Vel').pack(side=tk.LEFT, padx=(8, 2))
        self.vel_var = tk.IntVar(value=self.state.default_vel)
        self.vel_scale = ttk.Scale(hdr, from_=1, to=127, variable=self.vel_var,
                                    orient=tk.HORIZONTAL, length=60)
        self.vel_scale.pack(side=tk.LEFT, padx=2)
        self.vel_label = ttk.Label(hdr, text='100', width=3)
        self.vel_label.pack(side=tk.LEFT)
        self.vel_var.trace_add('write', self._on_vel_change)

        # Main area: piano keys + canvas + velocity lane
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)

        # Piano keys
        self.keys_canvas = tk.Canvas(body, width=44, bg='#16213e', highlightthickness=0)
        self.keys_canvas.pack(side=tk.LEFT, fill=tk.Y)

        # Right side: note grid + velocity lane
        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Note canvas with scrollbars
        grid_frame = ttk.Frame(right)
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

        # Velocity lane
        self.vel_canvas = tk.Canvas(right, height=50, bg='#12121f', highlightthickness=0)
        self.vel_canvas.pack(fill=tk.X)

        # Mouse events
        self.canvas.bind('<Button-1>', self._on_click)
        self.canvas.bind('<Button-3>', self._on_right_click)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)

        # Piano key clicks
        self.keys_canvas.bind('<Button-1>', self._on_key_click)

        # Velocity lane drag
        self._vel_dragging = False
        self.vel_canvas.bind('<Button-1>', self._on_vel_click)
        self.vel_canvas.bind('<B1-Motion>', self._on_vel_drag)
        self.vel_canvas.bind('<ButtonRelease-1>', lambda e: setattr(self, '_vel_dragging', False))

    def _scroll_both_y(self, *args):
        self.canvas.yview(*args)
        self.keys_canvas.yview(*args)

    def _on_vscroll(self, *args):
        self.vscroll.set(*args)
        self.keys_canvas.yview_moveto(float(args[0]))

    def _on_note_len(self, event=None):
        self.state.note_len = self.note_len_var.get()

    def _set_tool(self, tool):
        self.state.tool = tool
        self._update_tool_buttons()

    def _update_tool_buttons(self):
        for btn, name in [(self.draw_btn, 'draw'), (self.sel_btn, 'select'),
                           (self.del_btn, 'erase')]:
            if self.state.tool == name:
                btn.state(['pressed'])
            else:
                btn.state(['!pressed'])

    def _on_vel_change(self, *args):
        try:
            v = self.vel_var.get()
            self.vel_label.configure(text=str(v))
            self.state.default_vel = v
        except Exception:
            pass

    def _canvas_coords(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _snap(self, beat):
        return int(beat / self.state.snap) * self.state.snap

    def _hit_note(self, x, y):
        """Hit test for notes. Returns (note, index, is_resize_handle)."""
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return None, -1, False
        pitch = self.HI - int(y / self.NH)
        beat = x / self.BW
        for i in range(len(pat.notes) - 1, -1, -1):
            n = pat.notes[i]
            if n.pitch == pitch and n.start <= beat < n.start + n.duration:
                is_resize = beat > n.start + n.duration - 0.15
                return n, i, is_resize
        return None, -1, False

    def _on_click(self, event):
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return
        x, y = self._canvas_coords(event)
        pitch = self.HI - int(y / self.NH)
        beat = x / self.BW

        if self.state.tool == 'draw':
            n, i, is_resize = self._hit_note(x, y)
            if n and is_resize:
                self._resize_note = n
            elif n:
                self._drag_note = n
                self._drag_offset_x = beat - n.start
            else:
                # Create new note
                vel = self.vel_var.get()
                dur = self.state.snap
                if self.state.note_len == 'snap':
                    dur = self.state.snap
                elif self.state.note_len == 'last':
                    dur = self.state.last_note_len
                else:
                    try:
                        dur = float(self.state.note_len)
                    except ValueError:
                        dur = self.state.snap
                nn = Note(pitch=pitch, start=self._snap(beat), duration=dur, velocity=vel)
                pat.notes.append(nn)
                self._resize_note = nn
                self.state.last_note_len = dur
                self.app.play_note(pitch, vel)
                self.refresh()
        elif self.state.tool == 'erase':
            n, i, _ = self._hit_note(x, y)
            if n:
                pat.notes.pop(i)
                self._selected.discard(i)
                self.refresh()
                self.state.notify('note_edit')
        elif self.state.tool == 'select':
            n, i, is_resize = self._hit_note(x, y)
            if n:
                if not (event.state & 0x1):  # Shift not held
                    self._selected.clear()
                if i in self._selected:
                    self._selected.discard(i)
                else:
                    self._selected.add(i)
                if is_resize:
                    self._resize_note = n
                else:
                    self._drag_note = n
                    self._drag_offset_x = beat - n.start
            else:
                self._selected.clear()
            self.refresh()

    def _on_right_click(self, event):
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return
        x, y = self._canvas_coords(event)
        n, i, _ = self._hit_note(x, y)
        if n:
            pat.notes.pop(i)
            self._selected.discard(i)
            self.refresh()
            self.state.notify('note_edit')

    def _on_drag(self, event):
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return
        x, y = self._canvas_coords(event)
        pitch = self.HI - int(y / self.NH)
        beat = x / self.BW

        if self._resize_note:
            self._resize_note.duration = max(self.state.snap,
                                              self._snap(beat - self._resize_note.start))
            self.refresh()
        elif self._drag_note:
            self._drag_note.start = max(0, self._snap(beat - self._drag_offset_x))
            self._drag_note.pitch = max(self.LO, min(self.HI, pitch))
            self.refresh()

    def _on_release(self, event):
        if self._resize_note:
            self.state.last_note_len = self._resize_note.duration
        if self._resize_note or self._drag_note:
            self.state.notify('note_edit')
        self._drag_note = None
        self._resize_note = None

    def _on_key_click(self, event):
        y = self.keys_canvas.canvasy(event.y)
        pitch = self.HI - int(y / self.NH)
        if self.LO <= pitch <= self.HI:
            self.app.play_note(pitch, 100)

    def _on_vel_click(self, event):
        self._vel_dragging = True
        self._set_vel_at(event)

    def _on_vel_drag(self, event):
        if self._vel_dragging:
            self._set_vel_at(event)

    def _set_vel_at(self, event):
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return
        x = event.x + self.canvas.canvasx(0)  # Approximate scroll sync
        y = event.y
        vel = max(1, min(127, int((1 - y / 48) * 127)))
        beat = x / self.BW
        best = -1
        best_dist = float('inf')
        for i, n in enumerate(pat.notes):
            d = abs(beat - n.start)
            if d < best_dist and d < 0.5:
                best_dist = d
                best = i
        if best >= 0:
            pat.notes[best].velocity = vel
            self.refresh()

    def refresh(self):
        """Redraw the piano roll."""
        pat = self.state.find_pattern(self.state.sel_pat)

        # Update header
        if pat:
            self.name_label.configure(
                text=f'{pat.name} ({pat.length}b, {pat.key} {pat.scale})')
        else:
            self.name_label.configure(text='No pattern')

        self._update_tool_buttons()
        self._draw_keys(pat)
        self._draw_grid(pat)
        self._draw_velocity(pat)

    def _draw_keys(self, pat):
        kc = self.keys_canvas
        kc.delete('all')
        pitch_range = self.HI - self.LO + 1
        total_h = pitch_range * self.NH
        kc.configure(scrollregion=(0, 0, 44, total_h))

        in_key = scale_set(pat.key, pat.scale) if pat else set()

        for p in range(self.LO, self.HI + 1):
            y = (self.HI - p) * self.NH
            nm = NOTE_NAMES[p % 12]
            is_black = '#' in nm
            is_c = p % 12 == 0
            ik = (p % 12) in in_key
            oct = p // 12 - 1

            if is_black:
                bg = '#1a1530' if ik else '#111'
            else:
                bg = '#1e1a35' if ik else '#16213e'

            kc.create_rectangle(0, y, 44, y + self.NH, fill=bg, outline='#1a1a2e')

            if is_c:
                kc.create_text(40, y + self.NH // 2, text=f'C{oct}',
                                fill='#eee', font=('TkDefaultFont', 6), anchor='e')
                kc.create_line(0, y + self.NH, 44, y + self.NH, fill='#533483')
            elif not is_black:
                kc.create_text(40, y + self.NH // 2, text=f'{nm}{oct}',
                                fill='#888', font=('TkDefaultFont', 5), anchor='e')

    def _draw_grid(self, pat):
        cv = self.canvas
        cv.delete('all')

        pitch_range = self.HI - self.LO + 1
        total_h = pitch_range * self.NH
        beats = pat.length if pat else 16
        total_w = max(int(beats * self.BW), cv.winfo_width())
        cv.configure(scrollregion=(0, 0, total_w, total_h))

        in_key = scale_set(pat.key, pat.scale) if pat else set()
        bpm_beats = self.state.ts_num * (4 / self.state.ts_den)

        # Row backgrounds
        for p in range(self.LO, self.HI + 1):
            y = (self.HI - p) * self.NH
            nm = NOTE_NAMES[p % 12]
            is_black = '#' in nm
            is_c = p % 12 == 0
            ik = (p % 12) in in_key
            if is_black:
                bg = '#1a1530' if ik else '#15152a'
            else:
                bg = '#1e1a35' if ik else '#1a1a30'
            cv.create_rectangle(0, y, total_w, y + self.NH, fill=bg, outline='')
            line_color = '#3a3a6a' if is_c else '#222244'
            cv.create_line(0, y, total_w, y, fill=line_color, width=1 if is_c else 0.5)

        # Beat lines
        total_subdivs = int(beats * 4)
        for b in range(total_subdivs + 1):
            x = b * self.BW / 4
            bn = b / 4
            is_measure = (abs(bn % bpm_beats) < 0.001) or (abs(bn % bpm_beats - bpm_beats) < 0.001)
            is_beat = b % 4 == 0
            if is_measure:
                color, width = '#4a4a8a', 1.5
            elif is_beat:
                color, width = '#3a3a6a', 1
            elif b % 2 == 0:
                color, width = '#2a2a5a', 0.5
            else:
                color, width = '#222244', 0.5
            cv.create_line(x, 0, x, total_h, fill=color, width=width)

        if not pat:
            return

        # Notes
        for i, n in enumerate(pat.notes):
            x = n.start * self.BW
            y = (self.HI - n.pitch) * self.NH
            w = n.duration * self.BW
            sel = i in self._selected
            color = vel_color(n.velocity)

            cv.create_rectangle(x, y + 1, x + w - 1, y + self.NH - 1,
                                 fill=color, outline='#fff' if sel else pat.color,
                                 width=2 if sel else 1)

            # Resize handle
            cv.create_rectangle(x + w - 4, y + 1, x + w - 1, y + self.NH - 1,
                                 fill='#ffffff33', outline='')

            # Velocity text for selected notes
            if sel:
                cv.create_text(x + 2, y + self.NH - 3, text=f'v{n.velocity}',
                                fill='#fff', font=('TkDefaultFont', 6), anchor='sw')

    def _draw_velocity(self, pat):
        vc = self.vel_canvas
        vc.delete('all')
        beats = pat.length if pat else 16
        total_w = max(int(beats * self.BW), vc.winfo_width())
        vc.create_rectangle(0, 0, total_w, 50, fill='#12121f', outline='')
        vc.create_line(0, 25, total_w, 25, fill='#2a2a4a', width=0.5)

        if not pat:
            return

        bw = max(3, self.state.snap * self.BW * 0.6)
        for i, n in enumerate(pat.notes):
            x = n.start * self.BW + 2
            h = n.velocity / 127 * 46
            color = '#fff' if i in self._selected else vel_color(n.velocity)
            vc.create_rectangle(x, 48 - h, x + bw, 48, fill=color, outline='')

    def clear_selection(self):
        self._selected.clear()
