"""SongView — immutable read-only facade over AppState for plugins."""

from __future__ import annotations

import math
from typing import Iterator, List, Optional, Tuple

from .api import (
    TrackView, PatternView, PlacementView, VariationView,
    BeatInstrumentView, BeatPatternView, BeatTrackView, BeatPlacementView,
    AutomationTrackView, AutomationPatternView, AutomationPlacementView,
    AutomationPointView, ResolvedNote, BeatEvent, TempoMapView,
    SelectionSnapshot, Scope,
)


def _build_tempo_map(state) -> TempoMapView:
    tempo_track = state.find_tempo_track()
    if tempo_track is None:
        return TempoMapView(state.bpm, [])
    # Collect placements on this tempo track; flatten to beat ranges of curves.
    segments = []
    pls = [ap for ap in state.automation_placements
           if ap.track_id == tempo_track.id]
    pls.sort(key=lambda p: p.time)
    for ap in pls:
        pat = state.find_automation_pattern(ap.pattern_id)
        if not pat or not pat.points:
            continue
        reps = max(1, ap.repeats or 1)
        pts = sorted(pat.points, key=lambda p: p.time)
        span = _map_range(pts, pat.min_value, pat.max_value)
        for r in range(reps):
            base = ap.time + r * pat.length
            for i in range(len(span) - 1):
                t0, v0, curve = span[i]
                t1, v1, _ = span[i + 1]
                segments.append(
                    (base + t0, base + t1, v0, v1, curve)
                )
    segments.sort(key=lambda s: s[0])
    return TempoMapView(state.bpm, segments)


def _map_range(points, lo, hi):
    """Convert AutomationPoints (in [lo,hi]) into (time, bpm, curve) tuples.

    Since automation points encode the raw value directly (which *is* bpm
    when target==tempo), this is currently a straight pass-through."""
    out = []
    for p in points:
        out.append((p.time, float(p.value), p.curve))
    return out


def _total_beats(state) -> float:
    m = 0.0
    for pl in state.placements:
        pat = (state.find_variation(pl.pattern_id)
               if pl.is_variation else state.find_pattern(pl.pattern_id))
        if pl.is_variation:
            var = state.find_variation(pl.pattern_id)
            par = state.find_pattern(var.parent_id) if var else None
            plen = par.length if par else 0.0
        else:
            plen = pat.length if pat else 0.0
        m = max(m, pl.time + plen * max(1, pl.repeats or 1))
    for bp in state.beat_placements:
        pat = state.find_beat_pattern(bp.pattern_id)
        if pat:
            m = max(m, bp.time + pat.length * max(1, bp.repeats or 1))
    for ap in state.automation_placements:
        pat = state.find_automation_pattern(ap.pattern_id)
        if pat:
            m = max(m, ap.time + pat.length * max(1, ap.repeats or 1))
    return m


def _placement_duration(state, pl) -> float:
    if pl.is_variation:
        var = state.find_variation(pl.pattern_id)
        pat = state.find_pattern(var.parent_id) if var else None
        plen = pat.length if pat else 0.0
    else:
        pat = state.find_pattern(pl.pattern_id)
        plen = pat.length if pat else 0.0
    return plen * max(1, pl.repeats or 1)


class SongView:
    """Read-only facade over AppState for use by song plugins."""

    def __init__(self, state, selection: SelectionSnapshot):
        self._state = state
        self._selection = selection
        self.bpm = float(state.bpm)
        self.time_signature = (state.ts_num, state.ts_den)
        self.total_beats = _total_beats(state)
        self.tempo_map = _build_tempo_map(state)

        # Eagerly build collection views.
        self._tracks = [
            TrackView(id=t.id, name=t.name, channel=t.channel,
                      bank=t.bank, program=t.program, volume=t.volume)
            for t in state.tracks
        ]
        self._tracks_by_id = {tv.id: tv for tv in self._tracks}

        self._patterns = [
            PatternView(
                id=p.id, name=p.name, length=p.length, color=p.color,
                key=p.key, scale=p.scale,
                note_ids=tuple(n.note_id for n in p.notes),
            )
            for p in state.patterns
        ]
        self._patterns_by_id = {pv.id: pv for pv in self._patterns}

        self._placements = [
            PlacementView(
                id=pl.id, track_id=pl.track_id, pattern_id=pl.pattern_id,
                time=pl.time, transpose=pl.transpose,
                repeats=pl.repeats, target_key=pl.target_key,
                target_scale=pl.target_scale,
                is_variation=pl.is_variation,
                duration_beats=_placement_duration(state, pl),
            )
            for pl in state.placements
        ]
        self._placements_by_id = {pv.id: pv for pv in self._placements}

        self._variations = [
            VariationView(
                id=v.id, name=v.name, parent_id=v.parent_id, color=v.color,
                modification_note_ids=tuple(m.note_id for m in v.modifications),
                deleted_note_ids=tuple(v.deletions),
                added_note_ids=tuple(a.note_id for a in v.additions),
                split_note_ids=tuple(s.note_id for s in v.splits),
            )
            for v in state.variations
        ]
        self._variations_by_id = {v.id: v for v in self._variations}

        self._beat_kit = [
            BeatInstrumentView(
                id=i.id, name=i.name, channel=i.channel, bank=i.bank,
                program=i.program, pitch=i.pitch, velocity=i.velocity,
            )
            for i in state.beat_kit
        ]

        self._beat_patterns = [
            BeatPatternView(
                id=p.id, name=p.name, length=p.length,
                subdivision=p.subdivision, color=p.color,
                grid={int(k): tuple(v) for k, v in p.grid.items()},
            )
            for p in state.beat_patterns
        ]
        self._beat_patterns_by_id = {p.id: p for p in self._beat_patterns}

        self._beat_tracks = [
            BeatTrackView(id=t.id, name=t.name) for t in state.beat_tracks
        ]

        self._beat_placements = []
        for bp in state.beat_placements:
            pat = state.find_beat_pattern(bp.pattern_id)
            dur = (pat.length if pat else 0.0) * max(1, bp.repeats or 1)
            self._beat_placements.append(BeatPlacementView(
                id=bp.id, track_id=bp.track_id, pattern_id=bp.pattern_id,
                time=bp.time, repeats=bp.repeats, duration_beats=dur,
            ))

        self._auto_tracks = [
            AutomationTrackView(id=t.id, name=t.name, target=t.target)
            for t in state.automation_tracks
        ]
        self._auto_tracks_by_id = {t.id: t for t in self._auto_tracks}

        self._auto_patterns = [
            AutomationPatternView(
                id=p.id, name=p.name, length=p.length, color=p.color,
                min_value=p.min_value, max_value=p.max_value,
                points=tuple(
                    AutomationPointView(time=pt.time, value=pt.value,
                                        curve=pt.curve)
                    for pt in p.points
                ),
            )
            for p in state.automation_patterns
        ]
        self._auto_patterns_by_id = {p.id: p for p in self._auto_patterns}

        self._auto_placements = []
        for ap in state.automation_placements:
            pat = state.find_automation_pattern(ap.pattern_id)
            dur = (pat.length if pat else 0.0) * max(1, ap.repeats or 1)
            self._auto_placements.append(AutomationPlacementView(
                id=ap.id, track_id=ap.track_id, pattern_id=ap.pattern_id,
                time=ap.time, repeats=ap.repeats, duration_beats=dur,
            ))

    # -- Simple accessors ------------------------------------------------

    def tracks(self) -> List[TrackView]:
        return list(self._tracks)

    def track(self, id: int) -> TrackView:
        return self._tracks_by_id[id]

    def patterns(self) -> List[PatternView]:
        return list(self._patterns)

    def pattern(self, id: int) -> PatternView:
        return self._patterns_by_id[id]

    def variations(self) -> List[VariationView]:
        return list(self._variations)

    def variation(self, id: int) -> VariationView:
        return self._variations_by_id[id]

    def placements(self) -> List[PlacementView]:
        return list(self._placements)

    def placement(self, id: int) -> PlacementView:
        return self._placements_by_id[id]

    def beat_kit(self) -> List[BeatInstrumentView]:
        return list(self._beat_kit)

    def beat_patterns(self) -> List[BeatPatternView]:
        return list(self._beat_patterns)

    def beat_tracks(self) -> List[BeatTrackView]:
        return list(self._beat_tracks)

    def beat_placements(self) -> List[BeatPlacementView]:
        return list(self._beat_placements)

    def automation_tracks(self) -> List[AutomationTrackView]:
        return list(self._auto_tracks)

    def automation_track(self, id: int) -> AutomationTrackView:
        return self._auto_tracks_by_id[id]

    def automation_patterns(self) -> List[AutomationPatternView]:
        return list(self._auto_patterns)

    def automation_placements(self) -> List[AutomationPlacementView]:
        return list(self._auto_placements)

    def tempo_track(self) -> Optional[AutomationTrackView]:
        for t in self._auto_tracks:
            if t.target == 'tempo':
                return t
        return None

    def selection(self) -> SelectionSnapshot:
        return self._selection

    # -- Note resolution -------------------------------------------------

    def notes_in(self, scope: Scope) -> Iterator[ResolvedNote]:
        yield from self._iter_resolved_notes(scope)

    def _iter_resolved_notes(self, scope: Scope) -> Iterator[ResolvedNote]:
        from ..ops.variations import resolve_placement_notes

        note_id_filter = None
        if scope.kind == "notes" and scope.note_ids is not None:
            note_id_filter = set(scope.note_ids)

        placement_filter = None
        if scope.kind == "placements" and scope.placement_ids is not None:
            placement_filter = set(scope.placement_ids)

        for pl in self._state.placements:
            if placement_filter is not None and pl.id not in placement_filter:
                continue
            if scope.kind == "tracks" and scope.track_ids is not None:
                if pl.track_id not in scope.track_ids:
                    continue

            notes, pat_length, _k, _s = resolve_placement_notes(self._state, pl)
            if notes is None:
                continue
            transpose = self._state.compute_transpose(pl)
            reps = max(1, pl.repeats or 1)
            source_kind = "variation" if pl.is_variation else "pattern"
            source_id = pl.pattern_id

            for r in range(reps):
                base = pl.time + r * pat_length
                for n in notes:
                    absolute_start = base + n.start
                    if scope.kind == "range":
                        if (scope.start_beat is not None
                                and absolute_start + n.duration <= scope.start_beat):
                            continue
                        if (scope.end_beat is not None
                                and absolute_start >= scope.end_beat):
                            continue
                    if note_id_filter is not None and n.note_id not in note_id_filter:
                        continue
                    yield ResolvedNote(
                        note_id=n.note_id,
                        pitch=n.pitch + transpose,
                        start_beat=absolute_start,
                        duration_beats=n.duration,
                        velocity=n.velocity,
                        lyric=n.lyric,
                        bend=tuple(tuple(b) for b in (n.bend or ())),
                        track_id=pl.track_id,
                        placement_id=pl.id,
                        source_id=source_id,
                        source_kind=source_kind,
                        repeat_index=r,
                    )

    def placements_in(self, scope: Scope) -> Iterator[PlacementView]:
        for pv in self._placements:
            if scope.kind == "whole":
                yield pv
            elif scope.kind == "tracks" and scope.track_ids is not None:
                if pv.track_id in scope.track_ids:
                    yield pv
            elif scope.kind == "range":
                end = pv.time + pv.duration_beats
                if (scope.start_beat is not None and end <= scope.start_beat):
                    continue
                if (scope.end_beat is not None and pv.time >= scope.end_beat):
                    continue
                yield pv
            elif scope.kind == "placements" and scope.placement_ids is not None:
                if pv.id in scope.placement_ids:
                    yield pv
            elif scope.kind == "notes":
                # Include placements whose pattern contains any of the note_ids.
                if scope.note_ids is None:
                    continue
                ids = set(scope.note_ids)
                pat = self._patterns_by_id.get(pv.pattern_id)
                if pat is None and pv.is_variation:
                    var = self._variations_by_id.get(pv.pattern_id)
                    if var:
                        pat = self._patterns_by_id.get(var.parent_id)
                if pat and any(nid in ids for nid in pat.note_ids):
                    yield pv

    def beat_events_in(self, scope: Scope) -> Iterator[BeatEvent]:
        placement_filter = None
        if scope.kind == "placements" and scope.placement_ids is not None:
            placement_filter = set(scope.placement_ids)

        for bp in self._state.beat_placements:
            if placement_filter is not None and bp.id not in placement_filter:
                continue
            if scope.kind == "tracks" and scope.track_ids is not None:
                if bp.track_id not in scope.track_ids:
                    continue
            pat = self._state.find_beat_pattern(bp.pattern_id)
            if not pat:
                continue
            reps = max(1, bp.repeats or 1)
            for inst_id, row in pat.grid.items():
                if not row:
                    continue
                step_dur = pat.length / len(row)
                inst = self._state.find_beat_instrument(inst_id)
                if inst is None:
                    continue
                for r in range(reps):
                    base = bp.time + r * pat.length
                    for step_idx, vel in enumerate(row):
                        if vel <= 0:
                            continue
                        start = base + step_idx * step_dur
                        if scope.kind == "range":
                            if (scope.start_beat is not None
                                    and start + step_dur <= scope.start_beat):
                                continue
                            if (scope.end_beat is not None
                                    and start >= scope.end_beat):
                                continue
                        yield BeatEvent(
                            inst_id=inst_id,
                            pitch=inst.pitch,
                            start_beat=start,
                            velocity=vel,
                            track_id=bp.track_id,
                            placement_id=bp.id,
                            pattern_id=bp.pattern_id,
                            step=step_idx,
                            repeat_index=r,
                        )

    # -- Helpers ---------------------------------------------------------

    def bin_notes_by_beat(self, scope: Scope,
                          window: float) -> List[Tuple[float, List[ResolvedNote]]]:
        if window <= 0:
            raise ValueError("window must be > 0")
        bins: dict = {}
        for n in self.notes_in(scope):
            idx = int(math.floor(n.start_beat / window))
            bins.setdefault(idx, []).append(n)
        return [(idx * window, notes) for idx, notes in sorted(bins.items())]

    def sample_automation(self, track_id: int, beat: float) -> float:
        track = self._state.find_automation_track(track_id)
        if track is None:
            raise KeyError(f"automation track {track_id} not found")
        # Find placement containing this beat.
        pls = [ap for ap in self._state.automation_placements
               if ap.track_id == track_id]
        pls.sort(key=lambda p: p.time)
        for ap in pls:
            pat = self._state.find_automation_pattern(ap.pattern_id)
            if not pat:
                continue
            reps = max(1, ap.repeats or 1)
            total = pat.length * reps
            if ap.time <= beat < ap.time + total:
                local = (beat - ap.time) % pat.length
                return _sample_curve(pat, local)
        # Default: last known value from nearest placement, else 0.
        return 0.0


def _sample_curve(pat, local_beat: float) -> float:
    pts = sorted(pat.points, key=lambda p: p.time)
    if not pts:
        return 0.0
    if local_beat <= pts[0].time:
        return pts[0].value
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if a.time <= local_beat < b.time:
            if a.curve == 'step' or b.time == a.time:
                return a.value
            t = (local_beat - a.time) / (b.time - a.time)
            return a.value + (b.value - a.value) * t
    return pts[-1].value
