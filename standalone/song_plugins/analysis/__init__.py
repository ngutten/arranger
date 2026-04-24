"""Shared analysis primitives for song plugins.

Pure-function utilities that any plugin can import. Kept here so plugins
don't depend on each other through annotation data-flow. The broadcast
UI model allows only one plugin active at a time, so plugins that need
the same analysis (chord regions, key-fit scores, roman numerals) import
the computation directly rather than reading another plugin's output.

Modules:

- ``pitch_class``: windowed pitch-class histograms over notes.
- ``key_fit``: Krumhansl-Kessler fit scores per window per key.
- ``chord_regions``: root + quality detection over time.
- ``romans``: chord-root-and-quality + key → roman numeral label.
"""

from .pitch_class import (
    PITCH_CLASS_NAMES, windowed_chroma, chroma_for_notes,
)
from .key_fit import (
    KS_MAJOR, KS_MINOR, key_fit_scores, best_key,
)
from .chord_regions import (
    ChordRegion, detect_chord_regions, chord_quality_templates,
)
from .romans import (
    roman_numeral, roman_numeral_with_alternates,
)


__all__ = [
    "PITCH_CLASS_NAMES",
    "windowed_chroma", "chroma_for_notes",
    "KS_MAJOR", "KS_MINOR", "key_fit_scores", "best_key",
    "ChordRegion", "detect_chord_regions", "chord_quality_templates",
    "roman_numeral", "roman_numeral_with_alternates",
]
