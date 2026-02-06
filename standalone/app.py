"""Main application class - creates the window, wires up UI components."""

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from .state import (
    AppState, Pattern, Track, BeatTrack, BeatInstrument, BeatPlacement,
    Placement, PALETTE, NOTE_NAMES,
)
from .core.sf2 import SF2Info, scan_directory
from .core.midi import create_midi
from .core.audio import (
    render_fluidsynth, render_basic, wav_to_mp3,
    generate_preview_tone, render_sample, AudioPlayer,
)
from .ui.topbar import TopBar
from .ui.pattern_list import PatternList
from .ui.arrangement import ArrangementView
from .ui.piano_roll import PianoRoll
from .ui.beat_grid import BeatGrid
from .ui.track_panel import TrackPanel
from .ui.dialogs import PatternDialog, BeatPatternDialog, SF2Dialog


class App:
    """Main application - owns the state, creates the window, coordinates UI."""

    def __init__(self, root, instruments_dir=None):
        self.root = root
        self.state = AppState()
        self.player = AudioPlayer()
        self.instruments_dir = instruments_dir or str(
            Path(__file__).parent.parent / 'instruments')

        # Drag-and-drop state
        self._drag_type = None
        self._drag_pid = None

        # Playback state
        self._play_after_id = None

        self._setup_theme()
        self._build_ui()
        self._bind_keys()
        self._init_state()

        # Connect state observer
        self.state.on_change(self._on_state_change)

    def _setup_theme(self):
        """Configure ttk theme for dark mode."""
        style = ttk.Style()
        style.theme_use('clam')

        # Dark color scheme
        bg = '#1a1a2e'
        bg2 = '#16213e'
        bg3 = '#0f3460'
        accent = '#e94560'
        text = '#eeeeee'
        text2 = '#aaaaaa'
        grid_color = '#2a2a4a'

        style.configure('.', background=bg2, foreground=text, borderwidth=0,
                         font=('TkDefaultFont', 9))
        style.configure('TFrame', background=bg2)
        style.configure('TLabel', background=bg2, foreground=text)
        style.configure('TButton', background=bg, foreground=text, borderwidth=1,
                         padding=(4, 2))
        style.map('TButton',
                   background=[('active', accent), ('pressed', accent)],
                   foreground=[('active', '#fff'), ('pressed', '#fff')])
        style.configure('TEntry', fieldbackground=bg, foreground=text,
                         borderwidth=1)
        style.configure('TSpinbox', fieldbackground=bg, foreground=text,
                         borderwidth=1, arrowsize=12)
        style.configure('TCombobox', fieldbackground=bg, foreground=text,
                         borderwidth=1)
        style.map('TCombobox', fieldbackground=[('readonly', bg)])
        style.configure('TLabelframe', background=bg2, foreground=accent,
                         borderwidth=1)
        style.configure('TLabelframe.Label', background=bg2, foreground=accent,
                         font=('TkDefaultFont', 9, 'bold'))
        style.configure('TSeparator', background=grid_color)
        style.configure('TScrollbar', background=bg, troughcolor=bg2,
                         borderwidth=0, arrowsize=12)
        style.configure('TScale', background=bg2, troughcolor=bg)

        # Configure root window
        self.root.configure(bg=bg)

    def _build_ui(self):
        """Build the main UI layout."""
        self.root.title('Music Arranger')
        self.root.geometry('1200x750')
        self.root.minsize(800, 500)

        # Top bar
        self.topbar = TopBar(self.root, self)
        self.topbar.pack(fill=tk.X, side=tk.TOP)

        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # Main area
        main = ttk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True)

        # Left panel (pattern list)
        self.pattern_list = PatternList(main, self)
        self.pattern_list.pack(side=tk.LEFT, fill=tk.Y)

        # Right panel (track settings)
        self.track_panel = TrackPanel(main, self)
        self.track_panel.pack(side=tk.RIGHT, fill=tk.Y)

        # Center area (arrangement + piano roll / beat grid)
        center = ttk.Frame(main)
        center.pack(fill=tk.BOTH, expand=True)

        # Use PanedWindow for resizable split
        self.paned = tk.PanedWindow(center, orient=tk.VERTICAL, bg='#e94560',
                                      sashwidth=4, sashrelief=tk.FLAT)
        self.paned.pack(fill=tk.BOTH, expand=True)

        # Arrangement view (top)
        self.arrangement = ArrangementView(self.paned, self)
        self.paned.add(self.arrangement, minsize=100, stretch='always')

        # Editor area (bottom) - switches between piano roll and beat grid
        self.editor_frame = ttk.Frame(self.paned)
        self.paned.add(self.editor_frame, minsize=100, height=280)

        self.piano_roll = PianoRoll(self.editor_frame, self)
        self.beat_grid = BeatGrid(self.editor_frame, self)

        # Start with piano roll visible
        self.piano_roll.pack(fill=tk.BOTH, expand=True)
        self._current_editor = 'piano_roll'

    def _bind_keys(self):
        """Bind keyboard shortcuts."""
        self.root.bind('<space>', self._on_space)
        self.root.bind('<Control-c>', self._on_copy)
        self.root.bind('<Control-v>', self._on_paste)
        self.root.bind('<Control-a>', self._on_select_all)
        self.root.bind('<Delete>', self._on_delete)
        self.root.bind('<BackSpace>', self._on_delete)

    def _init_state(self):
        """Set up initial state with one pattern and one track."""
        # Create default pattern
        pat = Pattern(
            id=self.state.new_id(), name='Pattern 1', length=4,
            notes=[], color=PALETTE[0], key='C', scale='major',
        )
        self.state.patterns.append(pat)
        self.state.sel_pat = pat.id

        # Create default track
        trk = Track(id=self.state.new_id(), name='Track 1', channel=0)
        self.state.tracks.append(trk)
        self.state.sel_trk = trk.id

        # Auto-load first SF2
        self._auto_load_sf2()

        # Initial render
        self._refresh_all()

    def _auto_load_sf2(self):
        """Try to load the first SF2 file from the instruments directory."""
        sf2_list = scan_directory(self.instruments_dir)
        if sf2_list:
            self.state.sf2 = sf2_list[0]

    def _on_state_change(self, source=None):
        """Called whenever state changes. Refreshes relevant UI components."""
        self._refresh_all()

    def _refresh_all(self):
        """Refresh all UI components from current state."""
        self._switch_editor()
        self.topbar.refresh()
        self.pattern_list.refresh()
        self.arrangement.refresh()
        if self._current_editor == 'piano_roll':
            self.piano_roll.refresh()
        else:
            self.beat_grid.refresh()
        self.track_panel.refresh()

    def _switch_editor(self):
        """Switch between piano roll and beat grid based on selection."""
        if self.state.sel_beat_pat and self._current_editor != 'beat_grid':
            self.piano_roll.pack_forget()
            self.beat_grid.pack(fill=tk.BOTH, expand=True)
            self._current_editor = 'beat_grid'
        elif not self.state.sel_beat_pat and self._current_editor != 'piano_roll':
            self.beat_grid.pack_forget()
            self.piano_roll.pack(fill=tk.BOTH, expand=True)
            self._current_editor = 'piano_roll'

    # ---- Keyboard handlers ----

    def _on_space(self, event):
        if event.widget.winfo_class() in ('Entry', 'TEntry', 'Spinbox', 'TSpinbox',
                                            'Combobox', 'TCombobox'):
            return
        self.toggle_play()

    def _on_copy(self, event):
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return
        sel = self.piano_roll._selected
        if sel:
            self._clipboard = [pat.notes[i].to_dict() for i in sel if i < len(pat.notes)]
        else:
            self._clipboard = [n.to_dict() for n in pat.notes]

    def _on_paste(self, event):
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not hasattr(self, '_clipboard') or not self._clipboard:
            return
        from .state import Note
        mx = max((n.start + n.duration for n in pat.notes), default=0)
        cm = min(n['start'] for n in self._clipboard)
        off = mx - cm
        self.piano_roll.clear_selection()
        for nd in self._clipboard:
            note = Note.from_dict(nd)
            note.start += off
            pat.notes.append(note)
            self.piano_roll._selected.add(len(pat.notes) - 1)
        ne = max(n.start + n.duration for n in pat.notes)
        if ne > pat.length:
            pat.length = int(ne) + 1
        self.state.notify('paste')

    def _on_select_all(self, event):
        if event.widget.winfo_class() in ('Entry', 'TEntry'):
            return
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat:
            return
        self.piano_roll._selected = set(range(len(pat.notes)))
        self.piano_roll.refresh()

    def _on_delete(self, event):
        if event.widget.winfo_class() in ('Entry', 'TEntry'):
            return
        pat = self.state.find_pattern(self.state.sel_pat)
        if not pat or not self.piano_roll._selected:
            return
        if self.state.tool == 'select':
            keep = [n for i, n in enumerate(pat.notes) if i not in self.piano_roll._selected]
            pat.notes.clear()
            pat.notes.extend(keep)
            self.piano_roll.clear_selection()
            self.state.notify('delete_notes')

    # ---- Drag and drop ----

    def start_drag(self, dtype, pid, event):
        self._drag_type = dtype
        self._drag_pid = pid

    def end_drag(self, event):
        if self._drag_type and self._drag_pid is not None:
            # Get root coordinates
            x = event.x_root
            y = event.y_root
            self.arrangement.handle_drop(self._drag_type, self._drag_pid, x, y)
        self._drag_type = None
        self._drag_pid = None

    # ---- Actions ----

    def add_track(self):
        trk = Track(
            id=self.state.new_id(),
            name=f'Track {len(self.state.tracks) + 1}',
            channel=len(self.state.tracks) % 16,
        )
        self.state.tracks.append(trk)
        if not self.state.sel_trk:
            self.state.sel_trk = trk.id
        self.state.notify('add_track')

    def add_beat_track(self):
        bt = BeatTrack(
            id=self.state.new_id(),
            name=f'Beat {len(self.state.beat_tracks) + 1}',
        )
        self.state.beat_tracks.append(bt)
        if not self.state.sel_beat_trk:
            self.state.sel_beat_trk = bt.id
        self.state.notify('add_beat_track')

    def delete_track(self, tid):
        if messagebox.askyesno('Delete Track', 'Delete this track and all its placements?'):
            self.state.tracks = [t for t in self.state.tracks if t.id != tid]
            self.state.placements = [p for p in self.state.placements if p.track_id != tid]
            if self.state.sel_trk == tid:
                self.state.sel_trk = self.state.tracks[0].id if self.state.tracks else None
            self.state.notify('del_track')

    def delete_beat_track(self, btid):
        if messagebox.askyesno('Delete Beat Track', 'Delete this beat track?'):
            self.state.beat_tracks = [t for t in self.state.beat_tracks if t.id != btid]
            self.state.beat_placements = [p for p in self.state.beat_placements
                                           if p.track_id != btid]
            if self.state.sel_beat_trk == btid:
                self.state.sel_beat_trk = (self.state.beat_tracks[0].id
                                            if self.state.beat_tracks else None)
            self.state.notify('del_beat_track')

    def add_beat_instrument(self):
        last = self.state.beat_kit[-1] if self.state.beat_kit else None
        ch = last.channel if last else 9
        pitch = 36
        if last and last.channel == 9:
            used = {i.pitch for i in self.state.beat_kit}
            pitch = last.pitch + 1
            while pitch in used and pitch < 128:
                pitch += 1

        inst = BeatInstrument(
            id=self.state.new_id(),
            name=f'Inst {len(self.state.beat_kit) + 1}',
            channel=ch, pitch=pitch,
        )
        self.state.beat_kit.append(inst)

        # Initialize grid in all existing beat patterns
        for pat in self.state.beat_patterns:
            pat.grid[inst.id] = [0] * int(pat.length * pat.subdivision)

        self.state.notify('add_beat_inst')

    def delete_beat_instrument(self, iid):
        if messagebox.askyesno('Delete Instrument',
                                'Delete this instrument from all beat patterns?'):
            self.state.beat_kit = [i for i in self.state.beat_kit if i.id != iid]
            for pat in self.state.beat_patterns:
                pat.grid.pop(iid, None)
            self.state.notify('del_beat_inst')

    def show_pattern_dialog(self, pattern_id=None):
        PatternDialog(self.root, self, pattern_id)

    def show_beat_pattern_dialog(self, pattern_id=None):
        BeatPatternDialog(self.root, self, pattern_id)

    def load_sf2(self):
        sf2_list = scan_directory(self.instruments_dir)
        if not sf2_list:
            # Fallback to file dialog
            path = filedialog.askopenfilename(
                title='Load SoundFont',
                filetypes=[('SoundFont files', '*.sf2'), ('All files', '*.*')],
                initialdir=self.instruments_dir,
            )
            if path:
                try:
                    self.state.sf2 = SF2Info(path)
                    self.state.notify('sf2')
                except Exception as e:
                    messagebox.showerror('Error', f'Failed to load SF2: {e}')
            return

        dlg = SF2Dialog(self.root, self, sf2_list)
        self.root.wait_window(dlg)
        if dlg.result:
            self.state.sf2 = dlg.result
            self.state.notify('sf2')

    # ---- Audio ----

    def play_note(self, pitch, velocity=100, duration=0.15):
        """Play a note preview."""
        wav = generate_preview_tone(pitch, velocity, duration)
        self.player.play_async(wav)

    def play_beat_hit(self, inst_id):
        """Play a beat instrument preview."""
        inst = self.state.find_beat_instrument(inst_id)
        if not inst:
            return
        # Try SF2 rendering if available
        if self.state.sf2:
            sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                        else self.state.sf2.get('path'))
            if sf2_path:
                is_drums = inst.channel == 9
                bank = 128 if is_drums else inst.bank
                program = 0 if is_drums else inst.program

                def render_and_play():
                    wav = render_sample(sf2_path, bank, program, inst.pitch,
                                         inst.velocity, 0.3, inst.channel)
                    if wav:
                        self.player.play_async(wav)
                    else:
                        wav = generate_preview_tone(inst.pitch, inst.velocity, 0.15)
                        self.player.play_async(wav)

                threading.Thread(target=render_and_play, daemon=True).start()
                return

        # Fallback to sine
        wav = generate_preview_tone(inst.pitch, inst.velocity, 0.15)
        self.player.play_async(wav)

    def preview_pattern(self):
        """Preview the selected melodic pattern."""
        pat = self.state.find_pattern(self.state.sel_pat)
        trk = self.state.find_track(self.state.sel_trk)
        if not pat or not trk or not pat.notes:
            return

        arr = {
            'bpm': self.state.bpm, 'tsNum': self.state.ts_num,
            'tsDen': self.state.ts_den,
            'tracks': [{
                'name': trk.name, 'channel': trk.channel,
                'bank': trk.bank, 'program': trk.program,
                'volume': trk.volume,
                'placements': [{
                    'pattern': {
                        'notes': [n.to_dict() for n in pat.notes],
                        'length': pat.length
                    },
                    'time': 0, 'transpose': 0, 'repeats': 1,
                }]
            }]
        }
        self._render_and_play(arr)

    def preview_beat_pattern(self):
        """Preview the selected beat pattern."""
        pat = self.state.find_beat_pattern(self.state.sel_beat_pat)
        if not pat or not self.state.beat_kit:
            return

        tracks = []
        for inst in self.state.beat_kit:
            grid = pat.grid.get(inst.id)
            if not grid or not any(v > 0 for v in grid):
                continue
            step_dur = pat.length / len(grid)
            notes = []
            for i, v in enumerate(grid):
                if v > 0:
                    notes.append({
                        'pitch': inst.pitch, 'velocity': v,
                        'start': i * step_dur, 'duration': step_dur * 0.8,
                    })
            tracks.append({
                'name': inst.name, 'channel': inst.channel,
                'bank': inst.bank, 'program': inst.program,
                'volume': 100,
                'placements': [{
                    'pattern': {'notes': notes, 'length': pat.length},
                    'time': 0, 'transpose': 0, 'repeats': 1,
                }]
            })

        if not tracks:
            return

        arr = {'bpm': self.state.bpm, 'tsNum': self.state.ts_num,
               'tsDen': self.state.ts_den, 'tracks': tracks}
        self._render_and_play(arr)

    def _render_and_play(self, arr):
        """Render an arrangement and play it in a background thread."""
        def work():
            midi = create_midi(arr)
            wav = None
            if self.state.sf2:
                sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                            else self.state.sf2.get('path'))
                if sf2_path:
                    wav = render_fluidsynth(midi, sf2_path)
            if wav is None:
                wav = render_basic(arr)
            if wav:
                self.player.play_async(wav)

        threading.Thread(target=work, daemon=True).start()

    # ---- Playback ----

    def toggle_play(self):
        if self.state.playing:
            self.stop_play()
        else:
            self.start_play()

    def toggle_loop(self):
        self.state.looping = not self.state.looping
        self.topbar.refresh()

    def start_play(self):
        """Start full arrangement playback."""
        arr = self.state.build_arrangement()
        # Check if there are any notes
        has_notes = any(
            any(n for p in t.get('placements', []) for n in p.get('pattern', {}).get('notes', []))
            for t in arr.get('tracks', [])
        )
        if not has_notes:
            return

        self.state.playing = True
        self.topbar.refresh()

        def render_and_start():
            midi = create_midi(arr)
            wav = None
            if self.state.sf2:
                sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                            else self.state.sf2.get('path'))
                if sf2_path:
                    wav = render_fluidsynth(midi, sf2_path)
            if wav is None:
                wav = render_basic(arr)
            if wav:
                self.player.play_wav(wav)
                # Calculate total duration
                max_beat = 0
                for pl in self.state.placements:
                    pat = self.state.find_pattern(pl.pattern_id)
                    if pat:
                        max_beat = max(max_beat,
                                       pl.time + pat.length * (pl.repeats or 1))
                for bp in self.state.beat_placements:
                    pat = self.state.find_beat_pattern(bp.pattern_id)
                    if pat:
                        max_beat = max(max_beat,
                                       bp.time + pat.length * (bp.repeats or 1))

                beat_dur = 60.0 / self.state.bpm
                total_dur = max_beat * beat_dur
                # Start playhead animation on main thread
                self.root.after(0, lambda: self._animate_playhead(0, total_dur, beat_dur))

        threading.Thread(target=render_and_start, daemon=True).start()

    def _animate_playhead(self, elapsed, total_dur, beat_dur):
        """Animate the playhead during playback."""
        if not self.state.playing:
            return
        t = elapsed
        if self.state.looping and total_dur > 0:
            t = t % total_dur
        self.state.playhead = t / beat_dur
        self.arrangement.refresh()

        if not self.state.looping and elapsed >= total_dur:
            self.stop_play()
            return

        # Update every ~30ms
        self._play_after_id = self.root.after(
            30, lambda: self._animate_playhead(elapsed + 0.03, total_dur, beat_dur))

    def stop_play(self):
        self.state.playing = False
        self.state.playhead = None
        self.player.stop()
        if self._play_after_id:
            self.root.after_cancel(self._play_after_id)
            self._play_after_id = None
        self.topbar.refresh()
        self.arrangement.refresh()

    # ---- Export ----

    def do_export(self, fmt):
        """Export the arrangement as MIDI, WAV, or MP3."""
        arr = self.state.build_arrangement()
        midi = create_midi(arr)

        if fmt == 'midi':
            path = filedialog.asksaveasfilename(
                title='Export MIDI', defaultextension='.mid',
                filetypes=[('MIDI files', '*.mid'), ('All files', '*.*')],
            )
            if path:
                with open(path, 'wb') as f:
                    f.write(midi)
                messagebox.showinfo('Export', f'MIDI exported to {path}')
            return

        def render_work():
            wav = None
            if self.state.sf2:
                sf2_path = (self.state.sf2.path if hasattr(self.state.sf2, 'path')
                            else self.state.sf2.get('path'))
                if sf2_path:
                    wav = render_fluidsynth(midi, sf2_path)
            if wav is None:
                wav = render_basic(arr)
            if wav is None:
                self.root.after(0, lambda: messagebox.showerror('Error', 'No notes to render'))
                return

            if fmt == 'mp3':
                mp3 = wav_to_mp3(wav)
                if mp3:
                    def save():
                        path = filedialog.asksaveasfilename(
                            title='Export MP3', defaultextension='.mp3',
                            filetypes=[('MP3 files', '*.mp3'), ('All files', '*.*')],
                        )
                        if path:
                            with open(path, 'wb') as f:
                                f.write(mp3)
                            messagebox.showinfo('Export', f'MP3 exported to {path}')
                    self.root.after(0, save)
                else:
                    self.root.after(0, lambda: messagebox.showerror(
                        'Error', 'ffmpeg not available for MP3 conversion'))
            else:
                def save():
                    path = filedialog.asksaveasfilename(
                        title='Export WAV', defaultextension='.wav',
                        filetypes=[('WAV files', '*.wav'), ('All files', '*.*')],
                    )
                    if path:
                        with open(path, 'wb') as f:
                            f.write(wav)
                        messagebox.showinfo('Export', f'WAV exported to {path}')
                self.root.after(0, save)

        threading.Thread(target=render_work, daemon=True).start()

    # ---- Save/Load ----

    def save_project(self):
        path = filedialog.asksaveasfilename(
            title='Save Project', defaultextension='.json',
            filetypes=[('JSON files', '*.json'), ('All files', '*.*')],
        )
        if path:
            with open(path, 'w') as f:
                f.write(self.state.to_json())
            self.state._project_path = path

    def load_project(self):
        path = filedialog.askopenfilename(
            title='Load Project',
            filetypes=[('JSON files', '*.json'), ('All files', '*.*')],
        )
        if path:
            try:
                with open(path) as f:
                    self.state.load_json(f.read())
                self.state._project_path = path
                # Try to reload SF2 if path hint exists
                if hasattr(self.state, '_sf2_path_hint') and self.state._sf2_path_hint:
                    try:
                        self.state.sf2 = SF2Info(self.state._sf2_path_hint)
                    except Exception:
                        pass
                self.piano_roll.clear_selection()
                self.topbar.refresh()
                self._refresh_all()
            except Exception as e:
                messagebox.showerror('Error', f'Failed to load project: {e}')
