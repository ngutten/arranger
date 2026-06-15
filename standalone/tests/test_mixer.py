"""Tests for per-track mixer state: pan (CC10), mute, solo.

Covers Track serialization and the schedule emission rules in
_build_server_schedule (pan setup events, mute/solo note filtering).
"""

import pytest

from standalone.state import (AppState, Track, Pattern, Note, Placement,
                              BeatTrack, BeatPattern, BeatPlacement,
                              BeatInstrument)
from standalone.core.binding_engine import _build_server_schedule

try:
    from standalone.arranger_engine import AudioServer, AudioEngineConfig
    import standalone.core.binding_engine  # noqa: F401  (loads dynamic plugins)
    _HAVE_ENGINE = True
except Exception:
    _HAVE_ENGINE = False


def _ENGINE_SRV():
    cfg = AudioEngineConfig()
    cfg.sample_rate = 44100
    cfg.block_size = 512
    return AudioServer(cfg)


def _beat_setup(s, vel=100):
    """A beat track with one instrument hitting on every step of a 1-bar grid."""
    inst = BeatInstrument(id=s.new_id(), name='Kick', channel=9, pitch=36)
    s.beat_kit.append(inst)
    bt = BeatTrack(id=s.new_id(), name='Drums')
    s.beat_tracks.append(bt)
    bpat = BeatPattern(id=s.new_id(), name='B', length=4.0, subdivision=4,
                       color='#fff', grid={inst.id: [vel, vel, vel, vel]})
    s.beat_patterns.append(bpat)
    s.beat_placements.append(BeatPlacement(id=s.new_id(), track_id=bt.id,
                                           pattern_id=bpat.id, time=0.0, repeats=1))
    return bt, inst


def _track_with_notes(s, name='T', channel=0):
    t = Track(id=s.new_id(), name=name, channel=channel)
    s.tracks.append(t)
    pat = Pattern(id=s.new_id(), name='P', length=4.0, color='#fff',
                  notes=[Note(pitch=60, start=0.0, duration=1.0, velocity=100)])
    s.patterns.append(pat)
    s.placements.append(Placement(id=s.new_id(), track_id=t.id,
                                  pattern_id=pat.id, time=0.0, repeats=1))
    return t


def test_track_roundtrip_pan_mute_solo():
    t = Track(id=1, name='T', pan=-0.5, mute=True, solo=True)
    t2 = Track.from_dict(t.to_dict())
    assert t2.pan == pytest.approx(-0.5)
    assert t2.mute is True
    assert t2.solo is True


def test_track_defaults():
    t = Track(id=1, name='T')
    assert t.pan == pytest.approx(0.0)
    assert t.mute is False
    assert t.solo is False


def test_pan_event_emitted_and_mapped():
    s = AppState()
    t = _track_with_notes(s)
    t.pan = 0.0
    pans = [e for e in _build_server_schedule(s) if e.get('type') == 'pan']
    assert len(pans) >= 1
    assert pans[0]['pitch'] == 64          # center → CC10 = 64
    assert pans[0]['beat'] == -1           # setup event

    t.pan = -1.0
    pans = [e for e in _build_server_schedule(s) if e.get('type') == 'pan']
    assert pans[0]['pitch'] == 0           # hard left

    t.pan = 1.0
    pans = [e for e in _build_server_schedule(s) if e.get('type') == 'pan']
    assert pans[0]['pitch'] == 127         # hard right


def test_mute_removes_note_events_but_keeps_setup():
    s = AppState()
    t = _track_with_notes(s)
    t.mute = True
    events = _build_server_schedule(s)
    assert not any(e.get('type') == 'note_on' for e in events)
    # Setup (program/volume/pan) still emitted so live un-mute is instant.
    assert any(e.get('type') == 'pan' for e in events)
    assert any(e.get('type') == 'volume' for e in events)


def test_solo_silences_non_soloed_tracks():
    s = AppState()
    a = _track_with_notes(s, name='A', channel=0)
    b = _track_with_notes(s, name='B', channel=1)
    b.solo = True
    events = _build_server_schedule(s)
    on = [e for e in events if e.get('type') == 'note_on']
    # Only the soloed track's channel sounds.
    assert on and all(e['channel'] == 1 for e in on)


def test_solo_plus_mute_on_same_track_is_silent():
    s = AppState()
    t = _track_with_notes(s)
    t.solo = True
    t.mute = True   # mute wins
    events = _build_server_schedule(s)
    assert not any(e.get('type') == 'note_on' for e in events)


# ---- Beat tracks ----------------------------------------------------------

def test_beattrack_roundtrip():
    bt = BeatTrack(id=1, name='D', volume=70, mute=True, solo=True)
    bt2 = BeatTrack.from_dict(bt.to_dict())
    assert (bt2.volume, bt2.mute, bt2.solo) == (70, True, True)


def test_beattrack_volume_scales_velocity():
    s = AppState()
    bt, inst = _beat_setup(s, vel=100)
    bt.volume = 50
    on = [e for e in _build_server_schedule(s) if e.get('type') == 'note_on']
    assert on and all(e['velocity'] == 50 for e in on)   # 100 * 0.5


def test_beattrack_mute_silences():
    s = AppState()
    bt, inst = _beat_setup(s)
    bt.mute = True
    assert not any(e.get('type') == 'note_on' for e in _build_server_schedule(s))


def test_melodic_solo_silences_beat_tracks():
    s = AppState()
    mt = _track_with_notes(s, channel=0)
    bt, inst = _beat_setup(s)
    mt.solo = True   # soloing a melodic track must mute the (non-soloed) beats
    on = [e for e in _build_server_schedule(s) if e.get('type') == 'note_on']
    assert on and all(e['channel'] == 0 for e in on)   # only melodic ch0


# ---- Per-track meters: graph routing → mixer channel mapping ---------------

def _build_routed_graph():
    from standalone.graph_editor.graph_model import (
        GraphModel, GraphNode, GraphConnection)
    g = GraphModel()
    for nid, t, p in [('track_0', 'track_source', {}),
                      ('track_1', 'track_source', {}),
                      ('track_2', 'track_source', {}),
                      ('synthA', 'sine', {}), ('synthB', 'sine', {}),
                      ('mix', 'output', {'channel_count': 2})]:
        g.add_node(GraphNode(node_type=t, node_id=nid, params=p))
    for fn, fp, tn, tp in [
        ('track_0', 'events', 'synthA', 'events_in'),
        ('track_2', 'events', 'synthA', 'events_in'),   # shares synthA → ch0
        ('track_1', 'events', 'synthB', 'events_in'),
        ('synthA', 'audio', 'mix', 'audio_in_0'),
        ('synthB', 'audio', 'mix', 'audio_in_1')]:
        g.connections.append(GraphConnection(
            from_node=fn, from_port=fp, to_node=tn, to_port=tp))
    return g


def test_track_mixer_channel_mapping():
    g = _build_routed_graph()
    m = g.track_mixer_channels()
    assert m['track_0'] == 0
    assert m['track_2'] == 0      # shares synthA → same channel
    assert m['track_1'] == 1


def test_track_mixer_channel_ambiguous():
    # A track whose audio reaches two mixer inputs is reported as -1.
    from standalone.graph_editor.graph_model import GraphConnection
    g = _build_routed_graph()
    g.connections.append(GraphConnection(
        from_node='synthA', from_port='audio', to_node='mix', to_port='audio_in_1'))
    assert g.track_mixer_channels()['track_0'] == -1


def test_track_mixer_channels_no_mixer():
    from standalone.graph_editor.graph_model import GraphModel, GraphNode
    g = GraphModel()
    g.add_node(GraphNode(node_type='track_source', node_id='track_0', params={}))
    assert g.track_mixer_channels() == {}


@pytest.mark.skipif(not _HAVE_ENGINE, reason="C++ engine not built")
def test_per_channel_meter_isolation():
    import json
    srv = _ENGINE_SRV()

    def send(d):
        return json.loads(srv.handle(json.dumps(d)))

    send({"cmd": "set_graph", "bpm": 120, "nodes": [
        {"id": "track_0", "type": "track_source"},
        {"id": "track_1", "type": "track_source"},
        {"id": "synthA", "type": "sine"}, {"id": "synthB", "type": "sine"},
        {"id": "mixer", "type": "mixer", "channel_count": 2}], "connections": [
        {"from_node": "track_0", "from_port": "events_out", "to_node": "synthA", "to_port": "events_in"},
        {"from_node": "track_1", "from_port": "events_out", "to_node": "synthB", "to_port": "events_in"},
        {"from_node": "synthA", "from_port": "audio_out_L", "to_node": "mixer", "to_port": "audio_in_L_0"},
        {"from_node": "synthA", "from_port": "audio_out_R", "to_node": "mixer", "to_port": "audio_in_R_0"},
        {"from_node": "synthB", "from_port": "audio_out_L", "to_node": "mixer", "to_port": "audio_in_L_1"},
        {"from_node": "synthB", "from_port": "audio_out_R", "to_node": "mixer", "to_port": "audio_in_R_1"}]})
    # Note only on track_0 → mixer channel 0; sustained past the render window.
    send({"cmd": "set_schedule", "events": [
        {"beat": 0.0, "type": "note_on", "node_id": "track_0", "channel": 0,
         "pitch": 69, "velocity": 120, "value": 0.0},
        {"beat": 100.0, "type": "note_off", "node_id": "track_0", "channel": 0,
         "pitch": 69, "velocity": 0, "value": 0.0}]})
    send({"cmd": "set_bpm", "bpm": 120})
    send({"cmd": "prerender"})
    send({"cmd": "render", "format": "wav", "duration_beats": 1.0})
    ch = send({"cmd": "get_position"})["meter"]["channels"]
    assert len(ch) == 2
    assert ch[0]["peak_l"] > 0.01      # track_0 sounding
    assert ch[1]["peak_l"] == 0.0      # track_1 silent
