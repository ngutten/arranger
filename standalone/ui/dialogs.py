"""Modal dialogs for the standalone arranger."""

import tkinter as tk
from tkinter import ttk

from ..state import NOTE_NAMES, SCALES, PALETTE


class PatternDialog(tk.Toplevel):
    """Dialog for creating or editing a melodic pattern."""

    def __init__(self, parent, app, pattern_id=None):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self.pattern_id = pattern_id
        self.result = None

        pat = self.state.find_pattern(pattern_id) if pattern_id else None
        self.title('Edit Pattern' if pat else 'New Pattern')
        self.geometry('320x260')
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.configure(bg='#16213e')

        # Name
        ttk.Label(self, text='Name:').pack(anchor='w', padx=12, pady=(12, 2))
        self.name_var = tk.StringVar(value=pat.name if pat else
                                     f'Pattern {len(self.state.patterns) + 1}')
        name_entry = ttk.Entry(self, textvariable=self.name_var, width=30)
        name_entry.pack(padx=12, fill=tk.X)

        # Length
        ttk.Label(self, text='Length (beats):').pack(anchor='w', padx=12, pady=(8, 2))
        self.len_var = tk.IntVar(value=int(pat.length) if pat else self.state.ts_num)
        ttk.Spinbox(self, from_=1, to=128, textvariable=self.len_var, width=8).pack(
            anchor='w', padx=12)

        # Key and Scale
        key_frame = ttk.Frame(self)
        key_frame.pack(fill=tk.X, padx=12, pady=(8, 2))
        ttk.Label(key_frame, text='Key:').pack(side=tk.LEFT)
        self.key_var = tk.StringVar(value=pat.key if pat else 'C')
        ttk.Combobox(key_frame, textvariable=self.key_var, values=NOTE_NAMES,
                      state='readonly', width=4).pack(side=tk.LEFT, padx=4)
        ttk.Label(key_frame, text='Scale:').pack(side=tk.LEFT, padx=(8, 0))
        self.scale_var = tk.StringVar(value=pat.scale if pat else 'major')
        ttk.Combobox(key_frame, textvariable=self.scale_var,
                      values=list(SCALES.keys()), state='readonly', width=10).pack(
            side=tk.LEFT, padx=4)

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btn_frame, text='Cancel', command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text='OK', command=self._ok).pack(side=tk.RIGHT)

        name_entry.focus_set()
        name_entry.select_range(0, tk.END)

    def _ok(self):
        from ..state import Pattern, Note
        name = self.name_var.get() or 'Pattern'
        length = max(1, self.len_var.get())
        key = self.key_var.get()
        scale = self.scale_var.get()

        if self.pattern_id:
            pat = self.state.find_pattern(self.pattern_id)
            if pat:
                pat.name = name
                pat.length = length
                pat.key = key
                pat.scale = scale
        else:
            pat = Pattern(
                id=self.state.new_id(), name=name, length=length,
                notes=[], color=PALETTE[len(self.state.patterns) % len(PALETTE)],
                key=key, scale=scale,
            )
            self.state.patterns.append(pat)
            self.state.sel_pat = pat.id
            self.state.sel_beat_pat = None

        self.state.notify('pattern_dialog')
        self.destroy()


class BeatPatternDialog(tk.Toplevel):
    """Dialog for creating or editing a beat pattern."""

    def __init__(self, parent, app, pattern_id=None):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self.pattern_id = pattern_id

        pat = self.state.find_beat_pattern(pattern_id) if pattern_id else None
        self.title('Edit Beat Pattern' if pat else 'New Beat Pattern')
        self.geometry('320x220')
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.configure(bg='#16213e')

        # Name
        ttk.Label(self, text='Name:').pack(anchor='w', padx=12, pady=(12, 2))
        self.name_var = tk.StringVar(value=pat.name if pat else
                                     f'Beat {len(self.state.beat_patterns) + 1}')
        name_entry = ttk.Entry(self, textvariable=self.name_var, width=30)
        name_entry.pack(padx=12, fill=tk.X)

        # Length
        ttk.Label(self, text='Length (beats):').pack(anchor='w', padx=12, pady=(8, 2))
        self.len_var = tk.IntVar(value=int(pat.length) if pat else self.state.ts_num)
        ttk.Spinbox(self, from_=1, to=128, textvariable=self.len_var, width=8).pack(
            anchor='w', padx=12)

        # Subdivision
        ttk.Label(self, text='Subdivision:').pack(anchor='w', padx=12, pady=(8, 2))
        self.subdiv_var = tk.StringVar(value=str(pat.subdivision) if pat else '4')
        ttk.Combobox(self, textvariable=self.subdiv_var,
                      values=[('2', '8th notes'), ('3', 'Triplets'),
                              ('4', '16th notes'), ('6', '16th triplets')],
                      state='readonly', width=20).pack(anchor='w', padx=12)
        # Override display: just use numeric values
        subdiv_cb = ttk.Combobox(self, textvariable=self.subdiv_var, state='readonly', width=20)
        subdiv_cb['values'] = ['2', '3', '4', '6']
        # Replace the previous combobox
        for child in self.winfo_children():
            if isinstance(child, ttk.Combobox) and child != subdiv_cb:
                if child.cget('textvariable') == str(self.subdiv_var):
                    child.destroy()
                    break
        subdiv_cb.pack(anchor='w', padx=12)

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btn_frame, text='Cancel', command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text='OK', command=self._ok).pack(side=tk.RIGHT)

        name_entry.focus_set()

    def _ok(self):
        from ..state import BeatPattern
        name = self.name_var.get() or 'Beat'
        length = max(1, self.len_var.get())
        try:
            subdiv = int(self.subdiv_var.get())
        except ValueError:
            subdiv = 4

        if self.pattern_id:
            pat = self.state.find_beat_pattern(self.pattern_id)
            if pat:
                old_len = int(pat.length * pat.subdivision)
                new_len = length * subdiv
                pat.name = name
                pat.length = length
                pat.subdivision = subdiv
                if old_len != new_len:
                    for inst in self.state.beat_kit:
                        old_grid = pat.grid.get(inst.id, [])
                        new_grid = [0] * new_len
                        for i in range(min(len(old_grid), new_len)):
                            new_grid[i] = old_grid[i]
                        pat.grid[inst.id] = new_grid
        else:
            grid = {}
            for inst in self.state.beat_kit:
                grid[inst.id] = [0] * (length * subdiv)
            pat = BeatPattern(
                id=self.state.new_id(), name=name, length=length,
                subdivision=subdiv,
                color=PALETTE[len(self.state.beat_patterns) % len(PALETTE)],
                grid=grid,
            )
            self.state.beat_patterns.append(pat)
            self.state.sel_beat_pat = pat.id
            self.state.sel_pat = None

        self.state.notify('beat_pattern_dialog')
        self.destroy()


class SF2Dialog(tk.Toplevel):
    """Dialog for loading a SoundFont file."""

    def __init__(self, parent, app, sf2_list):
        super().__init__(parent)
        self.app = app
        self.sf2_list = sf2_list
        self.result = None

        self.title('Load SoundFont')
        self.geometry('360x180')
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.configure(bg='#16213e')

        ttk.Label(self, text='Select .sf2 file from instruments/ directory:').pack(
            anchor='w', padx=12, pady=(12, 4))

        self.sf2_var = tk.StringVar()
        if sf2_list:
            names = [sf2.name for sf2 in sf2_list]
            self.sf2_cb = ttk.Combobox(self, textvariable=self.sf2_var,
                                        values=names, state='readonly', width=40)
            self.sf2_cb.current(0)
        else:
            self.sf2_cb = ttk.Combobox(self, textvariable=self.sf2_var,
                                        values=['No .sf2 files found'], state='readonly',
                                        width=40)
        self.sf2_cb.pack(padx=12, fill=tk.X)

        ttk.Label(self, text='Place .sf2 files in the instruments/ directory',
                   font=('TkDefaultFont', 8)).pack(anchor='w', padx=12, pady=(8, 0))

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btn_frame, text='Cancel', command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text='Load', command=self._load).pack(side=tk.RIGHT)

    def _load(self):
        if not self.sf2_list:
            self.destroy()
            return
        name = self.sf2_var.get()
        for sf2 in self.sf2_list:
            if sf2.name == name:
                self.result = sf2
                break
        self.destroy()
