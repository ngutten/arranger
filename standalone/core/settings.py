"""User-facing settings - persisted to ~/.config/arranger/settings.json.

Covers both audio engine parameters (previously the only things here) and
new user preferences: MIDI input device, default SF2 path, and autosave.

Hard-coded values that are plausible candidates to move here in the future:
  - Piano roll NH/BW (note row height / pixels per beat) — display density
  - Undo stack max_size (currently 100)
  - Autosave interval in seconds (currently 60)
  - Default pattern length in beats (currently ts_num at creation time)
  - Default BPM for new projects (currently 120)
"""

import json
from pathlib import Path

CONFIG_PATH = Path.home() / '.config' / 'arranger' / 'settings.json'

DEFAULTS = {
    'audio_block_size': 512,
    'sample_rate': 44100,
    'midi_input_device': '',   # empty string = none selected
    'sf2_path': '',            # empty string = no default soundfont
    'autosave_interval': 60,   # seconds; 0 to disable
}


class Settings:
    def __init__(self, path=None):
        self.path = Path(path) if path else CONFIG_PATH
        self.block_size: int = DEFAULTS['audio_block_size']
        self.sample_rate: int = DEFAULTS['sample_rate']
        self.midi_input_device: str = DEFAULTS['midi_input_device']
        self.sf2_path: str = DEFAULTS['sf2_path']
        self.autosave_interval: int = DEFAULTS['autosave_interval']
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.block_size = int(d.get('audio_block_size', self.block_size))
            self.sample_rate = int(d.get('sample_rate', self.sample_rate))
            self.midi_input_device = str(d.get('midi_input_device', self.midi_input_device))
            self.sf2_path = str(d.get('sf2_path', self.sf2_path))
            self.autosave_interval = int(d.get('autosave_interval', self.autosave_interval))
        except Exception:
            pass  # keep defaults on any parse error

    def save(self):
        """Persist current settings to the user config file."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'w') as f:
                json.dump({
                    'audio_block_size': self.block_size,
                    'sample_rate': self.sample_rate,
                    'midi_input_device': self.midi_input_device,
                    'sf2_path': self.sf2_path,
                    'autosave_interval': self.autosave_interval,
                }, f, indent=2)
        except Exception:
            pass  # non-fatal if we can't write
