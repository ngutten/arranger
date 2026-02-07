"""Audio engine settings - loaded from settings.json with sensible defaults."""

import json
from pathlib import Path


DEFAULTS = {
    'audio_block_size': 512,
    'sample_rate': 44100,
}


class Settings:
    def __init__(self, path=None):
        self.block_size: int = DEFAULTS['audio_block_size']
        self.sample_rate: int = DEFAULTS['sample_rate']
        if path:
            self._load(path)

    def _load(self, path):
        p = Path(path)
        if not p.exists():
            return
        try:
            with open(p) as f:
                d = json.load(f)
            self.block_size = int(d.get('audio_block_size', self.block_size))
            self.sample_rate = int(d.get('sample_rate', self.sample_rate))
        except Exception:
            pass  # keep defaults
