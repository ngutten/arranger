"""Left panel - pattern and beat pattern lists with drag support."""

import tkinter as tk
from tkinter import ttk

from ..state import PALETTE


class PatternList(ttk.Frame):
    """Left panel containing melodic pattern list and beat pattern list."""

    def __init__(self, parent, app):
        super().__init__(parent, width=220)
        self.app = app
        self.state = app.state
        self.pack_propagate(False)
        self._drag_data = None
        self._build()

    def _build(self):
        # Melodic patterns section
        hdr = ttk.Frame(self)
        hdr.pack(fill=tk.X, padx=4, pady=(4, 0))
        ttk.Label(hdr, text='Patterns', font=('TkDefaultFont', 10, 'bold')).pack(side=tk.LEFT)
        ttk.Button(hdr, text='+ New', width=6, command=self._new_pattern).pack(side=tk.RIGHT)

        self.pat_frame = ttk.Frame(self)
        self.pat_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        self.pat_canvas = tk.Canvas(self.pat_frame, bg='#16213e', highlightthickness=0)
        self.pat_scrollbar = ttk.Scrollbar(self.pat_frame, orient=tk.VERTICAL,
                                            command=self.pat_canvas.yview)
        self.pat_inner = ttk.Frame(self.pat_canvas)
        self.pat_canvas.configure(yscrollcommand=self.pat_scrollbar.set)
        self.pat_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.pat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.pat_canvas_window = self.pat_canvas.create_window((0, 0), window=self.pat_inner,
                                                                 anchor='nw')
        self.pat_inner.bind('<Configure>',
                            lambda e: self.pat_canvas.configure(scrollregion=self.pat_canvas.bbox('all')))
        self.pat_canvas.bind('<Configure>',
                             lambda e: self.pat_canvas.itemconfig(self.pat_canvas_window, width=e.width))

        # Key info label
        self.key_info = ttk.Label(self, text='', font=('TkDefaultFont', 9))
        self.key_info.pack(fill=tk.X, padx=8, pady=2)

        # Separator
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=4, pady=4)

        # Beat patterns section
        bhdr = ttk.Frame(self)
        bhdr.pack(fill=tk.X, padx=4)
        ttk.Label(bhdr, text='Beat Patterns', font=('TkDefaultFont', 10, 'bold')).pack(side=tk.LEFT)
        ttk.Button(bhdr, text='+ New', width=6, command=self._new_beat_pattern).pack(side=tk.RIGHT)

        self.beat_frame = ttk.Frame(self)
        self.beat_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        self.beat_canvas = tk.Canvas(self.beat_frame, bg='#16213e', highlightthickness=0)
        self.beat_scrollbar = ttk.Scrollbar(self.beat_frame, orient=tk.VERTICAL,
                                             command=self.beat_canvas.yview)
        self.beat_inner = ttk.Frame(self.beat_canvas)
        self.beat_canvas.configure(yscrollcommand=self.beat_scrollbar.set)
        self.beat_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.beat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.beat_canvas_window = self.beat_canvas.create_window((0, 0), window=self.beat_inner,
                                                                   anchor='nw')
        self.beat_inner.bind('<Configure>',
                              lambda e: self.beat_canvas.configure(scrollregion=self.beat_canvas.bbox('all')))
        self.beat_canvas.bind('<Configure>',
                               lambda e: self.beat_canvas.itemconfig(self.beat_canvas_window, width=e.width))

    def _new_pattern(self):
        self.app.show_pattern_dialog()

    def _new_beat_pattern(self):
        self.app.show_beat_pattern_dialog()

    def refresh(self):
        """Rebuild pattern lists from state."""
        self._render_patterns()
        self._render_beat_patterns()
        # Key info
        pat = self.state.find_pattern(self.state.sel_pat)
        if pat:
            self.key_info.configure(text=f'Key: {pat.key} {pat.scale}')
        else:
            self.key_info.configure(text='')

    def _render_patterns(self):
        for w in self.pat_inner.winfo_children():
            w.destroy()

        for pat in self.state.patterns:
            sel = self.state.sel_pat == pat.id
            f = tk.Frame(self.pat_inner, bg='#1e2a4a' if sel else '#16213e',
                         highlightbackground='#e94560' if sel else '#16213e',
                         highlightthickness=1 if sel else 0, cursor='hand2')
            f.pack(fill=tk.X, pady=1)

            # Color dot
            dot = tk.Canvas(f, width=12, height=12, bg=f['bg'], highlightthickness=0)
            dot.pack(side=tk.LEFT, padx=(6, 4), pady=4)
            dot.create_oval(2, 2, 10, 10, fill=pat.color, outline='')

            # Name label
            info = f'{pat.length}b {pat.key} {pat.scale[:3]}'
            lbl = tk.Label(f, text=f'{pat.name}  {info}', bg=f['bg'], fg='#eee',
                           font=('TkDefaultFont', 9), anchor='w')
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

            # Action buttons
            btn_frame = tk.Frame(f, bg=f['bg'])
            btn_frame.pack(side=tk.RIGHT, padx=4)

            pid = pat.id
            dup_btn = tk.Button(btn_frame, text='\u29C9', bg=f['bg'], fg='#aaa',
                                 relief='flat', font=('TkDefaultFont', 8),
                                 command=lambda p=pid: self._dup_pat(p))
            dup_btn.pack(side=tk.LEFT)
            edit_btn = tk.Button(btn_frame, text='\u270E', bg=f['bg'], fg='#aaa',
                                  relief='flat', font=('TkDefaultFont', 8),
                                  command=lambda p=pid: self.app.show_pattern_dialog(p))
            edit_btn.pack(side=tk.LEFT)
            del_btn = tk.Button(btn_frame, text='\u2715', bg=f['bg'], fg='#aaa',
                                 relief='flat', font=('TkDefaultFont', 8),
                                 command=lambda p=pid: self._del_pat(p))
            del_btn.pack(side=tk.LEFT)

            # Click to select
            for widget in [f, dot, lbl]:
                widget.bind('<Button-1>', lambda e, p=pid: self._select_pat(p))

            # Drag support
            for widget in [f, lbl]:
                widget.bind('<B1-Motion>', lambda e, p=pid: self._start_drag('pattern', p, e))
                widget.bind('<ButtonRelease-1>', self._end_drag)

    def _render_beat_patterns(self):
        for w in self.beat_inner.winfo_children():
            w.destroy()

        if not self.state.beat_patterns:
            lbl = tk.Label(self.beat_inner, text='No beat patterns', bg='#16213e',
                           fg='#666', font=('TkDefaultFont', 9))
            lbl.pack(pady=8)
            return

        for pat in self.state.beat_patterns:
            sel = self.state.sel_beat_pat == pat.id
            f = tk.Frame(self.beat_inner, bg='#1e2a4a' if sel else '#16213e',
                         highlightbackground='#e94560' if sel else '#16213e',
                         highlightthickness=1 if sel else 0, cursor='hand2')
            f.pack(fill=tk.X, pady=1)

            dot = tk.Canvas(f, width=12, height=12, bg=f['bg'], highlightthickness=0)
            dot.pack(side=tk.LEFT, padx=(6, 4), pady=4)
            dot.create_oval(2, 2, 10, 10, fill=pat.color, outline='')

            info = f'{pat.length}b \u00F7{pat.subdivision}'
            lbl = tk.Label(f, text=f'{pat.name}  {info}', bg=f['bg'], fg='#eee',
                           font=('TkDefaultFont', 9), anchor='w')
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

            btn_frame = tk.Frame(f, bg=f['bg'])
            btn_frame.pack(side=tk.RIGHT, padx=4)

            pid = pat.id
            dup_btn = tk.Button(btn_frame, text='\u29C9', bg=f['bg'], fg='#aaa',
                                 relief='flat', font=('TkDefaultFont', 8),
                                 command=lambda p=pid: self._dup_beat_pat(p))
            dup_btn.pack(side=tk.LEFT)
            edit_btn = tk.Button(btn_frame, text='\u270E', bg=f['bg'], fg='#aaa',
                                  relief='flat', font=('TkDefaultFont', 8),
                                  command=lambda p=pid: self.app.show_beat_pattern_dialog(p))
            edit_btn.pack(side=tk.LEFT)
            del_btn = tk.Button(btn_frame, text='\u2715', bg=f['bg'], fg='#aaa',
                                 relief='flat', font=('TkDefaultFont', 8),
                                 command=lambda p=pid: self._del_beat_pat(p))
            del_btn.pack(side=tk.LEFT)

            for widget in [f, dot, lbl]:
                widget.bind('<Button-1>', lambda e, p=pid: self._select_beat_pat(p))

            for widget in [f, lbl]:
                widget.bind('<B1-Motion>', lambda e, p=pid: self._start_drag('beatPattern', p, e))
                widget.bind('<ButtonRelease-1>', self._end_drag)

    def _select_pat(self, pid):
        self.state.sel_pat = pid
        self.state.sel_beat_pat = None
        self.state.notify('sel_pat')

    def _select_beat_pat(self, pid):
        self.state.sel_beat_pat = pid
        self.state.sel_pat = None
        self.state.notify('sel_beat_pat')

    def _del_pat(self, pid):
        self.state.patterns = [p for p in self.state.patterns if p.id != pid]
        self.state.placements = [p for p in self.state.placements if p.pattern_id != pid]
        if self.state.sel_pat == pid:
            self.state.sel_pat = None
        self.state.notify('del_pat')

    def _dup_pat(self, pid):
        import copy
        pat = self.state.find_pattern(pid)
        if not pat:
            return
        new_pat = copy.deepcopy(pat)
        new_pat.id = self.state.new_id()
        new_pat.name = pat.name + ' copy'
        new_pat.color = PALETTE[len(self.state.patterns) % len(PALETTE)]
        self.state.patterns.append(new_pat)
        self.state.sel_pat = new_pat.id
        self.state.notify('dup_pat')

    def _del_beat_pat(self, pid):
        self.state.beat_patterns = [p for p in self.state.beat_patterns if p.id != pid]
        self.state.beat_placements = [p for p in self.state.beat_placements if p.pattern_id != pid]
        if self.state.sel_beat_pat == pid:
            self.state.sel_beat_pat = None
        self.state.notify('del_beat_pat')

    def _dup_beat_pat(self, pid):
        import copy
        pat = self.state.find_beat_pattern(pid)
        if not pat:
            return
        new_pat = copy.deepcopy(pat)
        new_pat.id = self.state.new_id()
        new_pat.name = pat.name + ' copy'
        new_pat.color = PALETTE[len(self.state.beat_patterns) % len(PALETTE)]
        self.state.beat_patterns.append(new_pat)
        self.state.sel_beat_pat = new_pat.id
        self.state.sel_pat = None
        self.state.notify('dup_beat_pat')

    def _start_drag(self, dtype, pid, event):
        self._drag_data = {'type': dtype, 'pid': pid}
        self.app.start_drag(dtype, pid, event)

    def _end_drag(self, event):
        if self._drag_data:
            self.app.end_drag(event)
            self._drag_data = None
