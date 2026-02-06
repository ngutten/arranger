"""Right panel - track settings, soundfont info, placement settings, beat kit."""

import tkinter as tk
from tkinter import ttk

from ..state import NOTE_NAMES, SCALES, PALETTE, preset_name, BeatInstrument


class TrackPanel(ttk.Frame):
    """Right panel with track settings, SF2 info, placement settings, and beat kit."""

    def __init__(self, parent, app):
        super().__init__(parent, width=250)
        self.app = app
        self.state = app.state
        self.pack_propagate(False)
        self._build()

    def _build(self):
        # Scrollable container
        self.canvas = tk.Canvas(self, bg='#16213e', highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.inner, anchor='nw')

        self.inner.bind('<Configure>',
                         lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',
                          lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width))

        # Sections will be built in refresh()
        self.trk_frame = ttk.LabelFrame(self.inner, text='Track Settings', padding=4)
        self.trk_frame.pack(fill=tk.X, padx=4, pady=4)

        self.sf2_frame = ttk.LabelFrame(self.inner, text='Soundfont', padding=4)
        self.sf2_frame.pack(fill=tk.X, padx=4, pady=4)

        self.pl_frame = ttk.LabelFrame(self.inner, text='Placement', padding=4)
        self.pl_frame.pack(fill=tk.X, padx=4, pady=4)

        self.kit_frame = ttk.LabelFrame(self.inner, text='Beat Kit', padding=4)
        self.kit_frame.pack(fill=tk.X, padx=4, pady=4)

        # Add instrument button
        self.add_inst_btn = ttk.Button(self.kit_frame, text='+ Instrument',
                                        command=self.app.add_beat_instrument)
        self.add_inst_btn.pack(anchor='w', pady=2)

    def refresh(self):
        """Rebuild all sections from state."""
        self._render_track_settings()
        self._render_sf2_info()
        self._render_placement_settings()
        self._render_beat_kit()

    def _clear_frame(self, frame, keep_btn=False):
        for w in frame.winfo_children():
            if keep_btn and w == self.add_inst_btn:
                continue
            w.destroy()

    def _render_track_settings(self):
        self._clear_frame(self.trk_frame)
        s = self.state

        # Check for beat track selection first
        bt = s.find_beat_track(s.sel_beat_trk) if s.sel_beat_trk else None
        if bt:
            self._row(self.trk_frame, 'Name', bt.name,
                       lambda v, bt=bt: self._update_beat_track(bt, 'name', v))
            ttk.Label(self.trk_frame, text='Beat Track', foreground='#e94560',
                       font=('TkDefaultFont', 9)).pack(anchor='w', pady=2)
            ttk.Button(self.trk_frame, text='Delete Track',
                        command=lambda: self.app.delete_beat_track(bt.id)).pack(
                anchor='w', pady=4)
            return

        t = s.find_track(s.sel_trk) if s.sel_trk else None
        if not t:
            ttk.Label(self.trk_frame, text='Select a track',
                       foreground='#888').pack(anchor='w')
            return

        self._row(self.trk_frame, 'Name', t.name,
                   lambda v, t=t: self._update_track(t, 'name', v))

        # Channel
        ch_frame = ttk.Frame(self.trk_frame)
        ch_frame.pack(fill=tk.X, pady=2)
        ttk.Label(ch_frame, text='Channel', width=8).pack(side=tk.LEFT)
        ch_var = tk.StringVar(value=str(t.channel))
        ch_cb = ttk.Combobox(ch_frame, textvariable=ch_var, width=12, state='readonly',
                              values=[f'Ch {i+1}' + (' (Drums)' if i == 9 else '')
                                      for i in range(16)])
        ch_cb.current(t.channel)
        ch_cb.pack(side=tk.LEFT, padx=4)
        ch_cb.bind('<<ComboboxSelected>>',
                    lambda e, t=t: self._update_track(t, 'channel', ch_cb.current()))

        # Bank
        self._num_row(self.trk_frame, 'Bank', t.bank, 0, 16383,
                       lambda v, t=t: self._update_track(t, 'bank', v))

        # Program
        self._num_row(self.trk_frame, 'Program', t.program, 0, 127,
                       lambda v, t=t: self._update_track(t, 'program', v))

        # Preset name display
        presets = None
        if s.sf2 and hasattr(s.sf2, 'presets'):
            presets = s.sf2.presets
        elif s.sf2 and isinstance(s.sf2, dict):
            presets = s.sf2.get('presets')
        pn = preset_name(t.bank, t.program, presets)
        ttk.Label(self.trk_frame, text=f'Preset: {pn}', foreground='#e94560',
                   font=('TkDefaultFont', 8)).pack(anchor='w', pady=2)

        # Volume
        vol_frame = ttk.Frame(self.trk_frame)
        vol_frame.pack(fill=tk.X, pady=2)
        ttk.Label(vol_frame, text='Volume', width=8).pack(side=tk.LEFT)
        vol_var = tk.IntVar(value=t.volume)
        ttk.Scale(vol_frame, from_=0, to=127, variable=vol_var,
                   orient=tk.HORIZONTAL, length=100,
                   command=lambda v, t=t: self._update_track(t, 'volume', int(float(v)))).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(vol_frame, textvariable=vol_var, width=3).pack(side=tk.LEFT)

        ttk.Button(self.trk_frame, text='Delete Track',
                    command=lambda: self.app.delete_track(t.id)).pack(anchor='w', pady=4)

    def _render_sf2_info(self):
        self._clear_frame(self.sf2_frame)
        s = self.state

        if not s.sf2:
            ttk.Label(self.sf2_frame, text='No soundfont loaded',
                       foreground='#888').pack(anchor='w')
            return

        sf2_name = s.sf2.name if hasattr(s.sf2, 'name') else (
            s.sf2.get('name', 'Unknown') if isinstance(s.sf2, dict) else 'Unknown')
        ttk.Label(self.sf2_frame, text=sf2_name, font=('TkDefaultFont', 8)).pack(
            anchor='w', pady=2)

        presets = s.sf2.presets if hasattr(s.sf2, 'presets') else (
            s.sf2.get('presets', []) if isinstance(s.sf2, dict) else [])
        if not presets:
            return

        # Bank filter
        t = s.find_track(s.sel_trk)
        current_bank = t.bank if t else 0
        banks = sorted(set(p['bank'] for p in presets))

        bank_frame = ttk.Frame(self.sf2_frame)
        bank_frame.pack(fill=tk.X, pady=2)
        ttk.Label(bank_frame, text='Bank', width=6).pack(side=tk.LEFT)
        bank_var = tk.StringVar(value=str(current_bank))
        bank_cb = ttk.Combobox(bank_frame, textvariable=bank_var, width=8, state='readonly',
                                values=[f'Bank {b}' for b in banks])
        try:
            bank_cb.current(banks.index(current_bank))
        except ValueError:
            if banks:
                bank_cb.current(0)
        bank_cb.pack(side=tk.LEFT, padx=4)
        bank_cb.bind('<<ComboboxSelected>>',
                      lambda e: self._on_bank_change(banks[bank_cb.current()]))

        # Preset list
        filtered = [p for p in presets if p['bank'] == current_bank]
        list_frame = ttk.Frame(self.sf2_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=2)

        self.preset_listbox = tk.Listbox(list_frame, bg='#1a1a30', fg='#eee',
                                          selectbackground='#e94560',
                                          font=('TkDefaultFont', 8), height=8,
                                          highlightthickness=0, borderwidth=1)
        preset_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                       command=self.preset_listbox.yview)
        self.preset_listbox.configure(yscrollcommand=preset_scroll.set)
        self.preset_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        preset_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for p in filtered:
            marker = ' *' if (t and t.bank == p['bank'] and t.program == p['program']) else ''
            self.preset_listbox.insert(tk.END, f"{p['program']}: {p['name']}{marker}")

        self.preset_listbox.bind('<<ListboxSelect>>',
                                  lambda e: self._on_preset_select(filtered))

    def _render_placement_settings(self):
        self._clear_frame(self.pl_frame)
        s = self.state

        # Check beat placement first
        bp = s.find_beat_placement(s.sel_beat_pl) if s.sel_beat_pl else None
        if bp:
            pat = s.find_beat_pattern(bp.pattern_id)
            ttk.Label(self.pl_frame,
                       text=f'Pattern: {pat.name if pat else "?"}',
                       foreground='#e94560').pack(anchor='w', pady=2)
            self._num_row(self.pl_frame, 'Time', bp.time, 0, 999,
                           lambda v, bp=bp: self._update_beat_pl(bp, 'time', v),
                           step=s.snap, is_float=True)
            self._num_row(self.pl_frame, 'Repeats', bp.repeats, 1, 128,
                           lambda v, bp=bp: self._update_beat_pl(bp, 'repeats', v))
            ttk.Button(self.pl_frame, text='Remove',
                        command=lambda: self._del_beat_pl(bp.id)).pack(anchor='w', pady=4)
            return

        pl = s.find_placement(s.sel_pl) if s.sel_pl else None
        if not pl:
            ttk.Label(self.pl_frame, text='Click a placement',
                       foreground='#888').pack(anchor='w')
            return

        pat = s.find_pattern(pl.pattern_id)
        ttk.Label(self.pl_frame,
                   text=f'Pattern: {pat.name if pat else "?"}').pack(anchor='w', pady=2)

        self._num_row(self.pl_frame, 'Time', pl.time, 0, 999,
                       lambda v, pl=pl: self._update_pl(pl, 'time', v),
                       step=s.snap, is_float=True)

        self._num_row(self.pl_frame, 'Transpose', pl.transpose, -48, 48,
                       lambda v, pl=pl: self._update_pl(pl, 'transpose', v))

        # Target key
        key_frame = ttk.Frame(self.pl_frame)
        key_frame.pack(fill=tk.X, pady=2)
        ttk.Label(key_frame, text='Target Key', width=10).pack(side=tk.LEFT)
        key_var = tk.StringVar(value=pl.target_key or (pat.key if pat else 'C'))
        key_cb = ttk.Combobox(key_frame, textvariable=key_var, values=NOTE_NAMES,
                               state='readonly', width=4)
        key_cb.pack(side=tk.LEFT, padx=2)
        key_cb.bind('<<ComboboxSelected>>',
                      lambda e, pl=pl: self._update_pl(pl, 'target_key', key_var.get()))

        scale_var = tk.StringVar(value=pl.target_scale or (pat.scale if pat else 'major'))
        scale_cb = ttk.Combobox(key_frame, textvariable=scale_var,
                                 values=list(SCALES.keys()), state='readonly', width=10)
        scale_cb.pack(side=tk.LEFT, padx=2)
        scale_cb.bind('<<ComboboxSelected>>',
                        lambda e, pl=pl: self._update_pl(pl, 'target_scale', scale_var.get()))

        # Total shift info
        ts = s.compute_transpose(pl)
        pk = pat.key if pat else 'C'
        ps = pat.scale if pat else 'major'
        ttk.Label(self.pl_frame,
                   text=f'Base: {pk} {ps} -> total shift: {ts} semi',
                   foreground='#888', font=('TkDefaultFont', 7)).pack(anchor='w', pady=2)

        self._num_row(self.pl_frame, 'Repeats', pl.repeats, 1, 128,
                       lambda v, pl=pl: self._update_pl(pl, 'repeats', v))

        ttk.Button(self.pl_frame, text='Remove',
                    command=lambda: self._del_pl(pl.id)).pack(anchor='w', pady=4)

    def _render_beat_kit(self):
        self._clear_frame(self.kit_frame, keep_btn=True)

        if not self.state.beat_kit:
            ttk.Label(self.kit_frame, text='No instruments. Click + to add.',
                       foreground='#888', font=('TkDefaultFont', 8)).pack(
                anchor='w', pady=2, before=self.add_inst_btn)
            return

        for i, inst in enumerate(self.state.beat_kit):
            color = PALETTE[i % len(PALETTE)]
            inst_frame = ttk.Frame(self.kit_frame)
            inst_frame.pack(fill=tk.X, pady=2, before=self.add_inst_btn)

            # Header row
            hdr = ttk.Frame(inst_frame)
            hdr.pack(fill=tk.X)

            # Color indicator
            dot = tk.Canvas(hdr, width=10, height=10, highlightthickness=0)
            dot.pack(side=tk.LEFT, padx=2)
            dot.create_oval(1, 1, 9, 9, fill=color, outline='')

            ttk.Label(hdr, text=inst.name, font=('TkDefaultFont', 8, 'bold')).pack(
                side=tk.LEFT, padx=4)

            ttk.Button(hdr, text='\u25B6', width=2,
                        command=lambda iid=inst.id: self.app.play_beat_hit(iid)).pack(
                side=tk.RIGHT, padx=1)
            ttk.Button(hdr, text='\u2715', width=2,
                        command=lambda iid=inst.id: self.app.delete_beat_instrument(iid)).pack(
                side=tk.RIGHT, padx=1)

            # Detail rows
            det = ttk.Frame(inst_frame)
            det.pack(fill=tk.X, padx=12, pady=2)

            # Name
            self._small_row(det, 'Name', inst.name,
                             lambda v, inst=inst: self._update_inst(inst, 'name', v))

            # Channel
            ch_frame = ttk.Frame(det)
            ch_frame.pack(fill=tk.X, pady=1)
            ttk.Label(ch_frame, text='Ch', width=5, font=('TkDefaultFont', 7)).pack(side=tk.LEFT)
            ch_var = tk.StringVar(value=str(inst.channel))
            ch_cb = ttk.Combobox(ch_frame, textvariable=ch_var, width=10, state='readonly',
                                  values=[f'Ch {i+1}' + (' (Drums)' if i == 9 else '')
                                          for i in range(16)])
            ch_cb.current(inst.channel)
            ch_cb.pack(side=tk.LEFT, padx=2)
            ch_cb.bind('<<ComboboxSelected>>',
                        lambda e, inst=inst, cb=ch_cb: self._update_inst(inst, 'channel',
                                                                          cb.current()))

            # Pitch
            pitch_frame = ttk.Frame(det)
            pitch_frame.pack(fill=tk.X, pady=1)
            ttk.Label(pitch_frame, text='Pitch', width=5, font=('TkDefaultFont', 7)).pack(
                side=tk.LEFT)
            p_var = tk.IntVar(value=inst.pitch)
            ttk.Spinbox(pitch_frame, from_=0, to=127, width=5, textvariable=p_var,
                          command=lambda inst=inst, v=p_var: self._update_inst(
                              inst, 'pitch', v.get())).pack(side=tk.LEFT, padx=2)
            from ..state import NOTE_NAMES
            nn = NOTE_NAMES[inst.pitch % 12]
            octave = inst.pitch // 12 - 1
            ttk.Label(pitch_frame, text=f'{nn}{octave}',
                       font=('TkDefaultFont', 7), foreground='#888').pack(side=tk.LEFT, padx=2)

            # Velocity
            vel_frame = ttk.Frame(det)
            vel_frame.pack(fill=tk.X, pady=1)
            ttk.Label(vel_frame, text='Vel', width=5, font=('TkDefaultFont', 7)).pack(
                side=tk.LEFT)
            v_var = tk.IntVar(value=inst.velocity)
            ttk.Scale(vel_frame, from_=1, to=127, variable=v_var, orient=tk.HORIZONTAL,
                       length=80,
                       command=lambda v, inst=inst: self._update_inst(
                           inst, 'velocity', int(float(v)))).pack(side=tk.LEFT, padx=2)
            ttk.Label(vel_frame, textvariable=v_var, width=3,
                       font=('TkDefaultFont', 7)).pack(side=tk.LEFT)

    # Helpers
    def _row(self, parent, label, value, on_change):
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=2)
        ttk.Label(f, text=label, width=8).pack(side=tk.LEFT)
        var = tk.StringVar(value=value)
        entry = ttk.Entry(f, textvariable=var, width=16)
        entry.pack(side=tk.LEFT, padx=4)
        entry.bind('<Return>', lambda e: on_change(var.get()))
        entry.bind('<FocusOut>', lambda e: on_change(var.get()))

    def _small_row(self, parent, label, value, on_change):
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=1)
        ttk.Label(f, text=label, width=5, font=('TkDefaultFont', 7)).pack(side=tk.LEFT)
        var = tk.StringVar(value=value)
        entry = ttk.Entry(f, textvariable=var, width=12, font=('TkDefaultFont', 7))
        entry.pack(side=tk.LEFT, padx=2)
        entry.bind('<Return>', lambda e: on_change(var.get()))
        entry.bind('<FocusOut>', lambda e: on_change(var.get()))

    def _num_row(self, parent, label, value, min_val, max_val, on_change,
                  step=1, is_float=False):
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=2)
        ttk.Label(f, text=label, width=10).pack(side=tk.LEFT)
        if is_float:
            var = tk.DoubleVar(value=value)
        else:
            var = tk.IntVar(value=int(value))
        spin = ttk.Spinbox(f, from_=min_val, to=max_val, increment=step,
                            textvariable=var, width=8)
        spin.pack(side=tk.LEFT, padx=4)

        def commit(*args):
            try:
                v = var.get()
                on_change(int(v) if not is_float else v)
            except Exception:
                pass

        spin.configure(command=commit)
        spin.bind('<Return>', commit)

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
        sel = self.preset_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(filtered_presets):
            p = filtered_presets[idx]
            t = self.state.find_track(self.state.sel_trk)
            if t:
                t.bank = p['bank']
                t.program = p['program']
                self.state.notify('track_settings')
