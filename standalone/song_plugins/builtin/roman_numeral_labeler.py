"""Roman-numeral labeler — colored boxes behind notes with chord function.

Scans each pattern, detects chord regions using the shared analysis
library, then labels each region with a roman numeral for the pattern's
declared key + scale. Emits a ``regions`` annotation that lands on the
piano-roll overlay.

Keyed off ``pattern.key`` / ``pattern.scale`` rather than song-level
key detection — patterns already carry a declared key, and the point of
this plugin is to show chord function *in the pattern's own key*. If
the user wants to see how the same chord reads in a different key,
they temporarily change the pattern key and watch the labels re-project.
"""

from __future__ import annotations

import time
from typing import Dict, List

from ..analysis import ChordRegion, detect_chord_regions, roman_numeral
from ..api import (
    Annotation, ParamSpec, PluginManifest, PluginResult, Progress,
    Scope, SongPlugin,
)


# Pitch-class lookup for pattern.key strings like "C", "C#", "Db", etc.
_PC_BY_NAME: Dict[str, int] = {
    "C": 0, "B#": 0,
    "C#": 1, "Db": 1,
    "D": 2,
    "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4,
    "F": 5, "E#": 5,
    "F#": 6, "Gb": 6,
    "G": 7,
    "G#": 8, "Ab": 8,
    "A": 9,
    "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11,
}


def _parse_key(key: str, scale: str):
    """Return ``(tonic_pc, mode)`` or ``None`` if the key can't be parsed.

    ``mode`` is normalized to ``"maj"`` or ``"min"``.
    """
    pc = _PC_BY_NAME.get(str(key).strip())
    if pc is None:
        return None
    s = str(scale).strip().lower()
    if s.startswith("min") or s in ("aeolian", "natural minor",
                                     "harmonic minor", "melodic minor"):
        mode = "min"
    else:
        mode = "maj"
    return (pc, mode)


# Palette — reuse the major/minor-quality distinction. Major-chord boxes
# take a warm hue, minor a cool hue, dissonant / non-diatonic chords
# get a distinct accent color.
_COLOR_MAJOR = "#e0b060"
_COLOR_MINOR = "#6fa8e0"
_COLOR_DIM = "#c080d0"
_COLOR_AUG = "#e07090"
_COLOR_SUS = "#80d0a0"
_COLOR_DEFAULT = "#cccccc"


def _color_for_quality(q: str) -> str:
    return {
        "maj": _COLOR_MAJOR, "maj7": _COLOR_MAJOR, "maj6": _COLOR_MAJOR,
        "min": _COLOR_MINOR, "m7": _COLOR_MINOR,
        "minMaj7": _COLOR_MINOR, "min6": _COLOR_MINOR,
        "dom7": _COLOR_MAJOR,
        "dim": _COLOR_DIM, "m7b5": _COLOR_DIM,
        "aug": _COLOR_AUG,
        "sus4": _COLOR_SUS, "sus2": _COLOR_SUS,
    }.get(q, _COLOR_DEFAULT)


class RomanNumeralLabelerPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.roman_numeral_labeler",
        name="Roman Numeral Labeler",
        version="1.0.0",
        description=(
            "Detect chord regions within each pattern and label them with "
            "roman numerals in the pattern's declared key. Colored boxes "
            "paint behind the notes on the piano roll, with each chord's "
            "symbol + roman numeral shown in the corner of the box."
        ),
        capabilities=("analyze",),
        schemas=("regions",),
        params=(
            ParamSpec(
                key="window_beats", type="float",
                label="Window (beats)", default=1.0,
                min=0.25, max=8.0,
                help="Each analysis window produces one chord label. "
                     "Smaller windows catch quicker chord changes but "
                     "are noisier.",
            ),
            ParamSpec(
                key="min_confidence", type="float",
                label="Min confidence", default=0.6,
                min=0.0, max=1.0,
                help="Chord-template cosine fit below this score is "
                     "dropped rather than labeled. Lone notes score "
                     "~0.58; clean triads ≥ 0.85. Lower this to see "
                     "speculative labels over passing-tone regions.",
            ),
            ParamSpec(
                key="show_symbols", type="bool",
                label="Show chord symbols (e.g. G7) alongside numerals",
                default=True,
            ),
        ),
        scopes=("whole",),
        deps=("midi", "structure"),
        live_supported=True,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        t0 = time.monotonic()
        window = float(params.get("window_beats", 1.0))
        min_conf = float(params.get("min_confidence", 0.05))
        show_symbols = bool(params.get("show_symbols", True))

        progress.phase("scan")
        # Gather notes per pattern, keyed by the placement so we can
        # resolve pattern length correctly. We only analyze patterns
        # that actually have placements (otherwise they don't play and
        # the labels are wasted work), but we collect notes from the
        # pattern's own note list — absolute beats in the schedule are
        # irrelevant to pattern-local analysis.
        patterns_with_placements = set()
        for pl in view.placements():
            patterns_with_placements.add(pl.pattern_id)

        regions_out: List[dict] = []
        pat_views = {p.id: p for p in view.patterns()}

        progress.phase("analyze")
        total = max(1, len(patterns_with_placements))
        for i, pat_id in enumerate(sorted(patterns_with_placements)):
            if progress.cancelled:
                break
            pat = pat_views.get(pat_id)
            if pat is None:
                continue
            key_pair = _parse_key(pat.key, pat.scale)
            # Pull the raw Pattern from state to access its notes directly.
            raw_pat = view._state.find_pattern(pat_id)  # noqa: SLF001
            if raw_pat is None or not raw_pat.notes:
                continue
            # Wrap notes in the duck-typed shape detect_chord_regions expects.
            class _PN:
                __slots__ = ("pitch", "start_beat", "duration_beats",
                             "velocity")

                def __init__(self, n):
                    self.pitch = n.pitch
                    self.start_beat = n.start
                    self.duration_beats = n.duration
                    self.velocity = n.velocity

            pattern_notes = [_PN(n) for n in raw_pat.notes]
            chord_regions = detect_chord_regions(
                pattern_notes,
                total_beats=pat.length,
                window_beats=window,
                min_confidence=min_conf,
            )
            for cr in chord_regions:
                label = _format_label(
                    cr, key_pair, show_symbols,
                )
                regions_out.append({
                    "start_beat": cr.start_beat,
                    "end_beat": cr.end_beat,
                    "pattern_id": pat_id,
                    "color": _color_for_quality(cr.quality),
                    "label": label,
                    "payload": {
                        "chord": cr.label,
                        "root_pc": cr.root_pc,
                        "quality": cr.quality,
                        "key": key_pair,
                        "confidence": cr.confidence,
                    },
                })
            progress.update(i / total, None)

        annotation = Annotation(
            id="builtin.roman_numeral_labeler/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Roman Numerals",
            schema="regions",
            data=regions_out,
            render_hint={},
            declared_deps=self.manifest.deps,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, "done")
        return PluginResult(
            annotation=annotation,
            message=f"{len(regions_out)} chord regions across "
                    f"{len(patterns_with_placements)} patterns",
        )


def _format_label(cr: ChordRegion, key_pair, show_symbols: bool) -> str:
    if key_pair is None:
        return cr.label if show_symbols else ""
    rn = roman_numeral(cr.root_pc, cr.quality, key_pair[0], key_pair[1])
    if show_symbols:
        return f"{cr.label}  ({rn})"
    return rn


PLUGIN = RomanNumeralLabelerPlugin
