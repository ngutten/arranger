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
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / '.config' / 'arranger' / 'settings.json'

DEFAULTS = {
    'audio_block_size': 512,
    'sample_rate': 44100,
    'midi_input_device': '',   # empty string = none selected
    'sf2_path': '',            # empty string = no default soundfont
    'autosave_interval': 60,   # seconds; 0 to disable
    # 'binding'    = C++ engine in-process via pybind11 (default, fastest)
    # 'server'     = C++ audio_server process via IPC (fallback / headless)
    # 'fluidsynth' = internal Python engine (final fallback, no C++ required)
    'audio_backend': 'binding',
    'server_address': '',      # empty = platform default (/tmp/audio_server.sock or named pipe)
}


class Settings:
    def __init__(self, path=None):
        self.path = Path(path) if path else CONFIG_PATH
        self.block_size: int = DEFAULTS['audio_block_size']
        self.sample_rate: int = DEFAULTS['sample_rate']
        self.midi_input_device: str = DEFAULTS['midi_input_device']
        self.sf2_path: str = DEFAULTS['sf2_path']
        self.autosave_interval: int = DEFAULTS['autosave_interval']
        self.audio_backend: str = DEFAULTS['audio_backend']
        self.server_address: str = DEFAULTS['server_address']
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
            self.audio_backend = str(d.get('audio_backend', self.audio_backend))
            self.server_address = str(d.get('server_address', self.server_address))
        except Exception:
            logger.exception("Failed to load settings from %s, using defaults", self.path)
        self._validate()

    def _validate(self):
        """Clamp settings to valid ranges, logging warnings for bad values."""
        if self.sample_rate not in (22050, 44100, 48000, 88200, 96000):
            logger.warning("Invalid sample_rate %s, falling back to %d",
                           self.sample_rate, DEFAULTS['sample_rate'])
            self.sample_rate = DEFAULTS['sample_rate']
        if not (32 <= self.block_size <= 8192):
            logger.warning("Invalid block_size %s, falling back to %d",
                           self.block_size, DEFAULTS['audio_block_size'])
            self.block_size = DEFAULTS['audio_block_size']
        if self.audio_backend not in ('binding', 'server', 'fluidsynth'):
            logger.warning("Invalid audio_backend '%s', falling back to '%s'",
                           self.audio_backend, DEFAULTS['audio_backend'])
            self.audio_backend = DEFAULTS['audio_backend']
        if self.autosave_interval < 0:
            self.autosave_interval = DEFAULTS['autosave_interval']
        if self.sf2_path and not Path(self.sf2_path).exists():
            logger.warning("SF2 path does not exist: %s", self.sf2_path)

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
                    'audio_backend': self.audio_backend,
                    'server_address': self.server_address,
                }, f, indent=2)
        except Exception:
            logger.exception("Failed to save settings to %s", self.path)
