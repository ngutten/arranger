"""Tests for the master section: makeup gain, look-ahead limiter, metering.

Covers AppState persistence, schedule emission of master setup events, and a
DSP regression guard that the limiter holds its ceiling on a sustained signal
(rather than letting peaks hard-clip).
"""

import base64
import io
import json
import math
import wave
import array

import pytest

from standalone.state import AppState
from standalone.core.binding_engine import _build_server_schedule

try:
    from standalone.arranger_engine import AudioServer, AudioEngineConfig
    import standalone.core.binding_engine  # noqa: F401  (loads dynamic plugins)
    _HAVE_ENGINE = True
except Exception:
    _HAVE_ENGINE = False


def test_state_roundtrip_master():
    s = AppState()
    s.master_gain = 3.25
    s.master_limiter = False
    s.master_ceiling_db = -0.5
    s2 = AppState()
    s2.load_json(s.to_json())
    assert s2.master_gain == pytest.approx(3.25)
    assert s2.master_limiter is False
    assert s2.master_ceiling_db == pytest.approx(-0.5)


def test_schedule_emits_master_setup():
    s = AppState()
    s.master_gain = 4.0
    s.master_limiter = True
    s.master_ceiling_db = -1.0
    events = _build_server_schedule(s)
    master = {e['port_id']: e for e in events
              if e.get('node_id') == 'mixer' and e.get('type') == 'control'
              and not str(e.get('port_id', '')).startswith('gain_')}
    assert master['master_gain']['value'] == pytest.approx(4.0)
    assert master['limiter_enabled']['value'] == pytest.approx(1.0)
    # -1 dBFS → linear ≈ 0.8913
    assert master['limiter_threshold']['value'] == pytest.approx(0.8913, abs=1e-3)
    # Setup events fire before note-ons.
    for e in master.values():
        assert e['beat'] == -1


def test_schedule_limiter_disabled_flag():
    s = AppState()
    s.master_limiter = False
    events = _build_server_schedule(s)
    le = next(e for e in events if e.get('node_id') == 'mixer'
              and e.get('port_id') == 'limiter_enabled')
    assert le['value'] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# DSP regression guard
# ---------------------------------------------------------------------------

def _render_sustained(gain, limiter, sr=44100):
    """Render a sustained sine through the master mixer; return (peak, rms)."""
    cfg = AudioEngineConfig()
    cfg.sample_rate = sr
    cfg.block_size = 512
    srv = AudioServer(cfg)

    def send(d):
        return json.loads(srv.handle(json.dumps(d)))

    send({"cmd": "set_graph", "bpm": 120, "nodes": [
        {"id": "track_0", "type": "track_source"},
        {"id": "synth", "type": "sine"},
        {"id": "mixer", "type": "mixer", "channel_count": 1},
    ], "connections": [
        {"from_node": "track_0", "from_port": "events_out",
         "to_node": "synth", "to_port": "events_in"},
        {"from_node": "synth", "from_port": "audio_out_L",
         "to_node": "mixer", "to_port": "audio_in_L_0"},
        {"from_node": "synth", "from_port": "audio_out_R",
         "to_node": "mixer", "to_port": "audio_in_R_0"},
    ]})
    thr = 10.0 ** (-1.0 / 20.0)
    evs = [
        {"beat": -1, "type": "control", "node_id": "mixer",
         "port_id": "master_gain", "value": gain,
         "channel": 0, "pitch": 0, "velocity": 0},
        {"beat": -1, "type": "control", "node_id": "mixer",
         "port_id": "limiter_enabled", "value": 1.0 if limiter else 0.0,
         "channel": 0, "pitch": 0, "velocity": 0},
        {"beat": -1, "type": "control", "node_id": "mixer",
         "port_id": "limiter_threshold", "value": thr,
         "channel": 0, "pitch": 0, "velocity": 0},
        {"beat": 0.0, "type": "note_on", "node_id": "track_0",
         "channel": 0, "pitch": 69, "velocity": 100, "value": 0.0},
        {"beat": 3.5, "type": "note_off", "node_id": "track_0",
         "channel": 0, "pitch": 69, "velocity": 0, "value": 0.0},
    ]
    send({"cmd": "set_bpm", "bpm": 120})
    send({"cmd": "set_schedule", "events": evs})
    send({"cmd": "prerender"})
    r = send({"cmd": "render", "format": "wav"})
    wav = base64.b64decode(r["data"])
    wf = wave.open(io.BytesIO(wav), "rb")
    n = wf.getnframes()
    a = array.array("h")
    a.frombytes(wf.readframes(n))
    # Skip onset/offset to measure the steady state.
    skip = int(0.05 * sr) * 2
    seg = a[skip:len(a) - skip]
    peak = max((abs(x) for x in seg), default=0)
    rms = math.sqrt(sum(x * x for x in seg) / len(seg)) if seg else 0.0
    return peak / 32767.0, rms / 32767.0


@pytest.mark.skipif(not _HAVE_ENGINE, reason="C++ engine not built")
def test_limiter_transparent_below_threshold():
    # Gain that keeps the signal under the ceiling: limiter must not alter it.
    pk_on, rms_on = _render_sustained(5.0, True)
    pk_off, rms_off = _render_sustained(5.0, False)
    assert pk_on < 0.8913  # below -1 dBFS ceiling
    assert pk_on == pytest.approx(pk_off, abs=1e-3)
    assert rms_on == pytest.approx(rms_off, abs=1e-3)


@pytest.mark.skipif(not _HAVE_ENGINE, reason="C++ engine not built")
def test_limiter_holds_ceiling():
    # High gain: limiter ON must hold ~-1 dBFS; OFF hard-clips to 0 dBFS.
    pk_on, _ = _render_sustained(20.0, True)
    pk_off, _ = _render_sustained(20.0, False)
    # On: at or just under the -1 dBFS ceiling (small transient overshoot ok).
    assert 0.84 <= pk_on <= 0.92, pk_on
    # Off: clips to full scale.
    assert pk_off >= 0.999, pk_off
