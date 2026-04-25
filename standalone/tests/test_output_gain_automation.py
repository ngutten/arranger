"""Tests for output-gain automation (target='output_gain:<N>').

The binding engine should walk automation tracks whose target is
"output_gain:<N>" and emit Control events with node_id="mixer" and
port_id="gain_<N>" — the C++ dispatcher delivers these via set_param
to avoid needing a control_source wiring.
"""

import pytest

from standalone.state import (
    AppState, Track, AutomationTrack, AutomationPattern, AutomationPoint,
    AutomationPlacement,
)
from standalone.core.binding_engine import _build_server_schedule


@pytest.fixture
def state_with_gain_automation():
    """State with a single automation track targeting output_gain:0.

    A single 4-beat pattern with two points (0,0) and (4,1) ramps the
    gain from 0 to 1 over 4 beats.
    """
    s = AppState()
    s.bpm = 120.0

    t = Track(id=s.new_id(), name='T', channel=0)
    s.tracks.append(t)

    auto = AutomationTrack(id=s.new_id(), name='Output Vol', target='output_gain:0')
    s.automation_tracks.append(auto)

    pat = AutomationPattern(
        id=s.new_id(), name='Ramp', length=4.0, color='#fff',
        min_value=0.0, max_value=1.0,
        points=[
            AutomationPoint(time=0.0, value=0.0, curve='linear'),
            AutomationPoint(time=4.0, value=1.0, curve='linear'),
        ],
    )
    s.automation_patterns.append(pat)

    ap = AutomationPlacement(id=s.new_id(), track_id=auto.id,
                             pattern_id=pat.id, time=0.0, repeats=1)
    s.automation_placements.append(ap)

    return s


class TestOutputGainAutomation:
    def test_emits_control_events_targeting_mixer(self, state_with_gain_automation):
        events = _build_server_schedule(state_with_gain_automation)
        gain_events = [e for e in events
                       if e.get('type') == 'control'
                       and e.get('node_id') == 'mixer']
        assert len(gain_events) > 0
        for e in gain_events:
            assert e['port_id'] == 'gain_0'
            assert 0.0 <= e['value'] <= 1.0

    def test_ramp_is_monotonically_increasing(self, state_with_gain_automation):
        """Linear ramp from 0 to 1 should produce non-decreasing values."""
        events = _build_server_schedule(state_with_gain_automation)
        gain_events = sorted(
            [e for e in events
             if e.get('node_id') == 'mixer' and e.get('port_id') == 'gain_0'],
            key=lambda e: e['beat'])
        values = [e['value'] for e in gain_events]
        assert values[0] == pytest.approx(0.0, abs=1e-3)
        assert values[-1] == pytest.approx(1.0, abs=1e-3)
        for i in range(1, len(values)):
            assert values[i] >= values[i - 1] - 1e-6

    def test_dense_sampling_at_least_16_per_beat(self, state_with_gain_automation):
        events = _build_server_schedule(state_with_gain_automation)
        gain_events = [e for e in events
                       if e.get('node_id') == 'mixer'
                       and e.get('port_id') == 'gain_0']
        # 4-beat pattern × 16 samples/beat = 64, + 1 endpoint
        assert len(gain_events) >= 65


class TestOutputGainRouting:
    def test_channel_parsing(self):
        s = AppState()
        s.bpm = 120.0
        for ch in (0, 2, 7):
            t = Track(id=s.new_id(), name='T', channel=0)
            s.tracks.append(t)
            auto = AutomationTrack(id=s.new_id(), name=f'G{ch}',
                                   target=f'output_gain:{ch}')
            s.automation_tracks.append(auto)
            pat = AutomationPattern(
                id=s.new_id(), name='P', length=1.0, color='#fff',
                min_value=0.0, max_value=1.0,
                points=[AutomationPoint(time=0.0, value=0.5, curve='linear')])
            s.automation_patterns.append(pat)
            s.automation_placements.append(
                AutomationPlacement(id=s.new_id(), track_id=auto.id,
                                    pattern_id=pat.id, time=0.0, repeats=1))

        events = _build_server_schedule(s)
        port_ids = {e['port_id'] for e in events
                    if e.get('node_id') == 'mixer'}
        assert port_ids == {'gain_0', 'gain_2', 'gain_7'}

    def test_malformed_target_ignored(self):
        s = AppState()
        s.bpm = 120.0
        t = Track(id=s.new_id(), name='T', channel=0)
        s.tracks.append(t)
        auto = AutomationTrack(id=s.new_id(), name='Bad',
                               target='output_gain:not-a-number')
        s.automation_tracks.append(auto)
        pat = AutomationPattern(
            id=s.new_id(), name='P', length=1.0, color='#fff',
            min_value=0.0, max_value=1.0,
            points=[AutomationPoint(time=0.0, value=0.5, curve='linear')])
        s.automation_patterns.append(pat)
        s.automation_placements.append(
            AutomationPlacement(id=s.new_id(), track_id=auto.id,
                                pattern_id=pat.id, time=0.0, repeats=1))
        # Should not raise, should just skip the malformed track.
        events = _build_server_schedule(s)
        assert not any(e.get('node_id') == 'mixer' for e in events)

    def test_no_automation_emits_no_gain_events(self):
        s = AppState()
        s.bpm = 120.0
        events = _build_server_schedule(s)
        assert not any(e.get('node_id') == 'mixer' for e in events)

    def test_scaled_to_pattern_range(self):
        s = AppState()
        s.bpm = 120.0
        t = Track(id=s.new_id(), name='T', channel=0)
        s.tracks.append(t)
        auto = AutomationTrack(id=s.new_id(), name='G',
                               target='output_gain:0')
        s.automation_tracks.append(auto)
        pat = AutomationPattern(
            id=s.new_id(), name='P', length=1.0, color='#fff',
            min_value=0.25, max_value=0.75,  # custom range
            points=[AutomationPoint(time=0.0, value=1.0, curve='linear')])
        s.automation_patterns.append(pat)
        s.automation_placements.append(
            AutomationPlacement(id=s.new_id(), track_id=auto.id,
                                pattern_id=pat.id, time=0.0, repeats=1))
        events = _build_server_schedule(s)
        vals = [e['value'] for e in events
                if e.get('node_id') == 'mixer']
        assert vals
        # All should be at max of pattern range (0.75)
        for v in vals:
            assert v == pytest.approx(0.75, abs=1e-3)
