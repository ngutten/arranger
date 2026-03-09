"""Tests for settings validation and persistence."""

import json
import tempfile
from pathlib import Path

import pytest
from standalone.core.settings import Settings, DEFAULTS


class TestSettingsValidation:
    def test_defaults(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        # Non-existent file → defaults
        Path(path).unlink(missing_ok=True)
        s = Settings(path=path)
        assert s.sample_rate == DEFAULTS['sample_rate']
        assert s.block_size == DEFAULTS['audio_block_size']
        assert s.audio_backend == DEFAULTS['audio_backend']

    def test_invalid_sample_rate_falls_back(self):
        with tempfile.NamedTemporaryFile(suffix='.json', mode='w', delete=False) as f:
            json.dump({'sample_rate': 12345}, f)
            path = f.name
        s = Settings(path=path)
        assert s.sample_rate == DEFAULTS['sample_rate']
        Path(path).unlink()

    def test_invalid_block_size_falls_back(self):
        with tempfile.NamedTemporaryFile(suffix='.json', mode='w', delete=False) as f:
            json.dump({'audio_block_size': 1}, f)
            path = f.name
        s = Settings(path=path)
        assert s.block_size == DEFAULTS['audio_block_size']
        Path(path).unlink()

    def test_invalid_backend_falls_back(self):
        with tempfile.NamedTemporaryFile(suffix='.json', mode='w', delete=False) as f:
            json.dump({'audio_backend': 'nonexistent'}, f)
            path = f.name
        s = Settings(path=path)
        assert s.audio_backend == DEFAULTS['audio_backend']
        Path(path).unlink()

    def test_valid_settings_preserved(self):
        with tempfile.NamedTemporaryFile(suffix='.json', mode='w', delete=False) as f:
            json.dump({'sample_rate': 48000, 'audio_block_size': 256}, f)
            path = f.name
        s = Settings(path=path)
        assert s.sample_rate == 48000
        assert s.block_size == 256
        Path(path).unlink()

    def test_corrupt_json_uses_defaults(self):
        with tempfile.NamedTemporaryFile(suffix='.json', mode='w', delete=False) as f:
            f.write('{invalid json!!!')
            path = f.name
        s = Settings(path=path)
        assert s.sample_rate == DEFAULTS['sample_rate']
        Path(path).unlink()

    def test_save_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        Path(path).unlink(missing_ok=True)
        s = Settings(path=path)
        s.sample_rate = 48000
        s.block_size = 1024
        s.save()
        s2 = Settings(path=path)
        assert s2.sample_rate == 48000
        assert s2.block_size == 1024
        Path(path).unlink()
