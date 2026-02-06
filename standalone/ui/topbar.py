"""Top control bar - BPM, time signature, snap, tool buttons, and action buttons."""

import tkinter as tk
from tkinter import ttk


class TopBar(ttk.Frame):
    """Top bar with transport controls, BPM, time sig, snap, and action buttons."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.state = app.state
        self._build()

    def _build(self):
        s = self.state
        self.configure(padding=(6, 3))

        # Title
        lbl = ttk.Label(self, text="Arranger", font=('TkDefaultFont', 11, 'bold'),
                         foreground='#e94560')
        lbl.pack(side=tk.LEFT, padx=(0, 12))

        # Play button
        self.play_btn = ttk.Button(self, text='\u25B6', width=3,
                                    command=self.app.toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=2)

        # Loop button
        self.loop_btn = ttk.Button(self, text='Loop', width=4,
                                    command=self.app.toggle_loop)
        self.loop_btn.pack(side=tk.LEFT, padx=2)

        # BPM
        ttk.Label(self, text='BPM').pack(side=tk.LEFT, padx=(8, 2))
        self.bpm_var = tk.IntVar(value=s.bpm)
        self.bpm_spin = ttk.Spinbox(self, from_=20, to=300, width=5,
                                     textvariable=self.bpm_var,
                                     command=self._on_bpm)
        self.bpm_spin.pack(side=tk.LEFT, padx=2)
        self.bpm_spin.bind('<Return>', lambda e: self._on_bpm())

        # Time signature
        ttk.Label(self, text='TS').pack(side=tk.LEFT, padx=(8, 2))
        self.ts_num_var = tk.StringVar(value=str(s.ts_num))
        ts_num_cb = ttk.Combobox(self, textvariable=self.ts_num_var, width=3,
                                  values=['2', '3', '4', '5', '6', '7'],
                                  state='readonly')
        ts_num_cb.pack(side=tk.LEFT, padx=1)
        ts_num_cb.bind('<<ComboboxSelected>>', self._on_ts)

        ttk.Label(self, text='/').pack(side=tk.LEFT)
        self.ts_den_var = tk.StringVar(value=str(s.ts_den))
        ts_den_cb = ttk.Combobox(self, textvariable=self.ts_den_var, width=3,
                                  values=['2', '4', '8'], state='readonly')
        ts_den_cb.pack(side=tk.LEFT, padx=1)
        ts_den_cb.bind('<<ComboboxSelected>>', self._on_ts)

        # Snap
        ttk.Label(self, text='Snap').pack(side=tk.LEFT, padx=(8, 2))
        self.snap_var = tk.StringVar(value=str(s.snap))
        snap_cb = ttk.Combobox(self, textvariable=self.snap_var, width=5,
                                values=['1', '0.5', '0.25', '0.125', '0.0625'],
                                state='readonly')
        snap_cb.pack(side=tk.LEFT, padx=1)
        snap_cb.bind('<<ComboboxSelected>>', self._on_snap)

        # Spacer
        ttk.Frame(self).pack(side=tk.LEFT, expand=True, fill=tk.X)

        # Action buttons
        ttk.Button(self, text='Load SF2', command=self.app.load_sf2).pack(side=tk.LEFT, padx=2)
        ttk.Button(self, text='+ Track', command=self.app.add_track).pack(side=tk.LEFT, padx=2)
        ttk.Button(self, text='+ Beat Track', command=self.app.add_beat_track).pack(side=tk.LEFT, padx=2)

        sep = ttk.Separator(self, orient=tk.VERTICAL)
        sep.pack(side=tk.LEFT, padx=4, fill=tk.Y, pady=2)

        ttk.Button(self, text='MIDI', command=lambda: self.app.do_export('midi')).pack(side=tk.LEFT, padx=2)
        ttk.Button(self, text='WAV', command=lambda: self.app.do_export('wav')).pack(side=tk.LEFT, padx=2)
        ttk.Button(self, text='MP3', command=lambda: self.app.do_export('mp3')).pack(side=tk.LEFT, padx=2)

        sep2 = ttk.Separator(self, orient=tk.VERTICAL)
        sep2.pack(side=tk.LEFT, padx=4, fill=tk.Y, pady=2)

        ttk.Button(self, text='Save', command=self.app.save_project).pack(side=tk.LEFT, padx=2)
        ttk.Button(self, text='Load', command=self.app.load_project).pack(side=tk.LEFT, padx=2)

    def _on_bpm(self):
        try:
            self.state.bpm = self.bpm_var.get()
        except Exception:
            pass

    def _on_ts(self, event=None):
        try:
            self.state.ts_num = int(self.ts_num_var.get())
            self.state.ts_den = int(self.ts_den_var.get())
            self.state.notify('ts')
        except Exception:
            pass

    def _on_snap(self, event=None):
        try:
            self.state.snap = float(self.snap_var.get())
        except Exception:
            pass

    def refresh(self):
        """Update controls from state."""
        self.bpm_var.set(self.state.bpm)
        self.ts_num_var.set(str(self.state.ts_num))
        self.ts_den_var.set(str(self.state.ts_den))
        self.snap_var.set(str(self.state.snap))
        self.play_btn.configure(text='\u23F9' if self.state.playing else '\u25B6')
