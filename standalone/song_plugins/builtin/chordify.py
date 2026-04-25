"""Chordify — generate chord voicings from tagged root notes.

A ``generate``-capability plugin. For every note in scope that carries
the ``chord_root`` tag, the plugin:

1. Resolves the chord spec (quality, extras, inversion) to a set of
   MIDI pitches via :mod:`song_plugins.analysis.chord_voicings`.
2. Deletes any existing voicing notes whose ``chord_voicing.root_id``
   points back to one of the roots in scope (idempotent re-run).
3. Emits new voicing notes, each tagged ``chord_voicing`` so the next
   run can find and replace them.

Roots are authored interactively in the piano roll (middle-click to
cycle quality; ``q`` to open the full vocab popup). This plugin is the
batch/live regenerator that keeps the voicings in sync with the roots.

Plugin tag schema:

- ``chord_root``: ``{'quality', 'extras', 'inversion'}`` — placed on
  user-authored notes the user has marked as chord roots.
- ``chord_voicing``: ``{'root_id': int, 'gen_pitch': int,
  'gen_start': float, 'gen_dur': float, 'gen_vel': int}`` — placed
  by this plugin on every generated note. The ``gen_*`` fields record
  the geometry the plugin created, so re-runs can distinguish
  untouched voicings (which should be regenerated) from user-edited
  ones (which should be left alone — "auto-frozen" on first edit).

Voicings inherit the root's duration and velocity. They land in the
same pattern (or variation) as the root.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..analysis.chord_voicings import build_voicing
from ..api import (
    AddNote, DeleteNote, ParamSpec, PluginManifest, PluginResult, Progress,
    Scope, SongPlugin, VariationAddNote, VariationDeleteNote,
)


# Sentinel tag keys — kept as constants so UI and plugin agree.
CHORD_ROOT_TAG = 'chord_root'
CHORD_VOICING_TAG = 'chord_voicing'


# Geometry comparison tolerance (beats / velocity units).
_EPS = 1e-6


def _is_untouched(v: dict) -> bool:
    """True if a voicing's current geometry matches its generated record.

    A voicing with no gen_* record is treated as legacy/pre-edit-detection
    and is considered untouched (the old behaviour). A voicing whose
    gen_* record differs from its current geometry is considered
    user-edited and must not be clobbered on re-run.
    """
    if v.get('gen_pitch') is None:
        return True
    if v['gen_pitch'] != v['curr_pitch']:
        return False
    if abs((v['gen_start'] or 0.0) - v['curr_start']) > _EPS:
        return False
    if abs((v['gen_dur'] or 0.0) - v['curr_dur']) > _EPS:
        return False
    if int(v['gen_vel'] or 0) != int(v['curr_vel']):
        return False
    return True


class ChordifyPlugin(SongPlugin):
    manifest = PluginManifest(
        id='builtin.chordify',
        name='Chordify',
        version='1.0.0',
        description=(
            'Generates chord voicings from notes tagged as chord roots. '
            'Middle-click a note in the piano roll to mark it as a chord '
            'root; this plugin fills in the voicing above it.'
        ),
        capabilities=('generate',),
        schemas=(),
        params=(
            ParamSpec(
                key='scope', type='enum',
                label='Scope', default='whole',
                choices=('whole', 'selection'),
                help='Restrict to currently selected notes/placements.',
            ),
            ParamSpec(
                key='include_root', type='bool',
                label='Keep root pitch in voicing', default=True,
                help='If off, the root note is omitted from the generated '
                     'voicing (avoids doubling).',
            ),
        ),
        scopes=('whole', 'selection'),
        selection_kinds=('notes', 'placements'),
        deps=('midi',),
        live_supported=True,
        persistence_default='transient',
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        scope_kind = params.get('scope', 'whole')
        include_root = bool(params.get('include_root', True))

        scope = self._resolve_scope(view, scope_kind)

        patterns_by_id = {p.id: p for p in view.patterns()}
        variations_by_id = {v.id: v for v in view.variations()}

        progress.phase('scan')

        # Dedupe roots and voicings across placements / repeats.
        # Key: (source_kind, source_id, note_id).
        roots_seen: Dict[Tuple[str, int, int], dict] = {}
        voicings_seen: Dict[Tuple[str, int, int], dict] = {}

        for n in view.notes_in(scope):
            if progress.cancelled:
                break
            if n.note_id == 0:
                continue
            if not n.tags:
                continue
            key = (n.source_kind, n.source_id, n.note_id)
            if CHORD_ROOT_TAG in n.tags and key not in roots_seen:
                # Recover pattern-local start. Repeats stack every pat_length.
                pat_len = self._source_length(n, patterns_by_id, variations_by_id)
                placement = view.placement(n.placement_id)
                local_start = n.start_beat - (placement.time + n.repeat_index * pat_len)
                # Undo the placement transpose so we store the note's own pitch.
                local_pitch = n.pitch - (placement.transpose or 0)
                roots_seen[key] = {
                    'source_kind': n.source_kind,
                    'source_id':  n.source_id,
                    'note_id':    n.note_id,
                    'pitch':      local_pitch,
                    'start':      local_start,
                    'duration':   n.duration_beats,
                    'velocity':   n.velocity,
                    'spec':       n.tags[CHORD_ROOT_TAG],
                }
            if CHORD_VOICING_TAG in n.tags and key not in voicings_seen:
                vtag = n.tags[CHORD_VOICING_TAG]
                if not isinstance(vtag, dict):
                    continue
                # Recover pattern-local start for geometry comparison.
                pat_len = self._source_length(n, patterns_by_id, variations_by_id)
                placement = view.placement(n.placement_id)
                local_start = n.start_beat - (placement.time + n.repeat_index * pat_len)
                local_pitch = n.pitch - (placement.transpose or 0)
                voicings_seen[key] = {
                    'source_kind': n.source_kind,
                    'source_id':   n.source_id,
                    'note_id':     n.note_id,
                    'root_id':     vtag.get('root_id'),
                    'curr_pitch':  local_pitch,
                    'curr_start':  local_start,
                    'curr_dur':    n.duration_beats,
                    'curr_vel':    n.velocity,
                    'gen_pitch':   vtag.get('gen_pitch'),
                    'gen_start':   vtag.get('gen_start'),
                    'gen_dur':     vtag.get('gen_dur'),
                    'gen_vel':     vtag.get('gen_vel'),
                }

        progress.phase('generate')

        ops: List = []

        # Index voicings by (source, root_id) for per-root decisions.
        voicings_by_root: Dict[Tuple[str, int, int], list] = defaultdict(list)
        for v in voicings_seen.values():
            key = (v['source_kind'], v['source_id'], v['root_id'])
            voicings_by_root[key].append(v)

        # All live note IDs per source — used to detect orphans.
        all_note_ids_by_source: Dict[Tuple[str, int], set] = defaultdict(set)
        for pat in view.patterns():
            all_note_ids_by_source[('pattern', pat.id)] |= set(pat.note_ids)
        for varv in view.variations():
            parent = patterns_by_id.get(varv.parent_id)
            if parent:
                all_note_ids_by_source[('variation', varv.id)] |= set(parent.note_ids)
            all_note_ids_by_source[('variation', varv.id)] |= set(varv.added_note_ids)

        # 1. Orphan voicings — their root doesn't exist any more. Safe
        #    to delete regardless of edit state, since they can never
        #    be meaningful again.
        roots_in_scope = {
            (r['source_kind'], r['source_id'], r['note_id'])
            for r in roots_seen.values()
        }
        for v in voicings_seen.values():
            src_key = (v['source_kind'], v['source_id'])
            root_id = v['root_id']
            if root_id is None or root_id not in all_note_ids_by_source.get(src_key, set()):
                ops.append(self._delete_op(v['source_kind'], v['source_id'], v['note_id']))

        # 2. For each root in scope: compute target pitches, delete
        #    untouched voicings, add any target pitches not already
        #    claimed by an edited voicing.
        key_name_for_source: Dict[Tuple[str, int], Tuple[str, str]] = {}
        for pat in view.patterns():
            key_name_for_source[('pattern', pat.id)] = (pat.key, pat.scale)
        for varv in view.variations():
            parent = patterns_by_id.get(varv.parent_id)
            if parent:
                key_name_for_source[('variation', varv.id)] = (parent.key, parent.scale)

        for r in roots_seen.values():
            src_key = (r['source_kind'], r['source_id'])
            key, scale = key_name_for_source.get(src_key, ('C', 'major'))
            target_pitches = set(build_voicing(
                r['pitch'], key, scale, r['spec'], include_root=include_root))
            # The user's own note occupies the root pitch — never generate
            # a voicing at that pitch (would duplicate it).
            target_pitches.discard(r['pitch'])

            root_key = (r['source_kind'], r['source_id'], r['note_id'])
            existing = voicings_by_root.get(root_key, [])
            edited_pitches = {
                v['gen_pitch'] for v in existing if not _is_untouched(v)
            }

            # Delete untouched voicings — they will be regenerated (if
            # still in target) or retired (if the chord changed).
            for v in existing:
                if _is_untouched(v):
                    ops.append(self._delete_op(
                        v['source_kind'], v['source_id'], v['note_id']))

            # Generate voicings for each target pitch not already held
            # by an edited voicing (which claims its gen_pitch slot).
            for p in sorted(target_pitches - edited_pitches):
                ops.append(self._add_op(
                    r['source_kind'], r['source_id'],
                    pitch=p, start=r['start'], duration=r['duration'],
                    velocity=r['velocity'],
                    tags={CHORD_VOICING_TAG: {
                        'root_id':  r['note_id'],
                        'gen_pitch': p,
                        'gen_start': r['start'],
                        'gen_dur':   r['duration'],
                        'gen_vel':   r['velocity'],
                    }},
                ))

        progress.update(1.0, 'done')
        n_roots = len(roots_seen)
        n_adds = sum(1 for o in ops if isinstance(o, (AddNote, VariationAddNote)))
        n_dels = sum(1 for o in ops if isinstance(o, (DeleteNote, VariationDeleteNote)))
        return PluginResult(
            operations=tuple(ops),
            message=f'{n_roots} root(s), +{n_adds} voicings, -{n_dels} stale',
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _resolve_scope(view, scope_kind: str) -> Scope:
        if scope_kind != 'selection':
            return Scope(kind='whole')
        sel = view.selection()
        if sel.primary == 'placements' and sel.placements:
            return Scope(kind='placements',
                         placement_ids=tuple(sorted(sel.placements)))
        if sel.notes:
            return Scope(kind='notes', note_ids=tuple(sorted(sel.notes)))
        if sel.placements:
            return Scope(kind='placements',
                         placement_ids=tuple(sorted(sel.placements)))
        return Scope(kind='whole')

    @staticmethod
    def _source_length(n, patterns_by_id, variations_by_id) -> float:
        if n.source_kind == 'pattern':
            pat = patterns_by_id.get(n.source_id)
            return pat.length if pat else 0.0
        var = variations_by_id.get(n.source_id)
        if not var:
            return 0.0
        parent = patterns_by_id.get(var.parent_id)
        return parent.length if parent else 0.0

    @staticmethod
    def _add_op(source_kind, source_id, *, pitch, start, duration,
                velocity, tags):
        if source_kind == 'variation':
            return VariationAddNote(
                variation_id=source_id, pitch=pitch, start=start,
                duration=duration, velocity=velocity, tags=tags,
            )
        return AddNote(
            pattern_id=source_id, pitch=pitch, start=start,
            duration=duration, velocity=velocity, tags=tags,
        )

    @staticmethod
    def _delete_op(source_kind, source_id, note_id):
        if source_kind == 'variation':
            return VariationDeleteNote(variation_id=source_id, note_id=note_id)
        return DeleteNote(pattern_id=source_id, note_id=note_id)


PLUGIN = ChordifyPlugin
