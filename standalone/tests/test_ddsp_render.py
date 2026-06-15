"""End-to-end smoke test for the standardised (acids-ircam) DDSP plugin.

Drives the C++ engine through the in-process protocol, pointing the DDSP node
at a converted model directory (decoder.onnx + config.json).  Verifies that a
rendered note is finite and non-silent, exercising the full path: ONNX load,
frame-rate inference with GRU-cache threading, and C++ harmonic+noise synthesis.

Usage:
    python scripts/convert_ddsp.py --self_test --output_dir /tmp/ddsp_selftest
    python -m standalone.tests.test_ddsp_render /tmp/ddsp_selftest
"""
import base64
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from standalone.arranger_engine import AudioServer, AudioEngineConfig  # noqa: E402
import standalone.core.binding_engine  # noqa: F401,E402  (loads dynamic plugins)


def send(server, req):
    return json.loads(server.handle(json.dumps(req)))


def samples_of(wav):
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE", "not a WAV"
    data_size = struct.unpack_from("<I", wav, 40)[0]
    n = data_size // 2
    return struct.unpack_from("<%dh" % n, wav, 44)


def main(model_dir):
    cfg = AudioEngineConfig()
    cfg.sample_rate = 48000.0
    cfg.block_size = 512
    server = AudioServer(cfg)

    graph = {
        "cmd": "set_graph", "bpm": 120,
        "nodes": [
            {"id": "synth", "type": "builtin.ddsp"},
            {"id": "mixer", "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            {"from_node": "synth", "from_port": "audio_out_L",
             "to_node": "mixer", "to_port": "audio_in_L_0"},
            {"from_node": "synth", "from_port": "audio_out_R",
             "to_node": "mixer", "to_port": "audio_in_R_0"},
        ],
    }
    r = send(server, graph)
    assert r.get("status") != "error", f"set_graph failed: {r}"

    r = send(server, {"cmd": "set_node_config", "node_id": "synth",
                      "config": {"model_dir": model_dir,
                                 "loud_ceil_db": "0", "loud_floor_db": "-60"}})
    assert r.get("status") != "error", f"set_node_config failed: {r}"

    events = [
        {"beat": 0.0, "type": "note_on", "node_id": "synth",
         "channel": 0, "pitch": 60, "velocity": 110},
        {"beat": 1.5, "type": "note_off", "node_id": "synth",
         "channel": 0, "pitch": 60, "velocity": 0},
    ]
    send(server, {"cmd": "set_bpm", "bpm": 120})
    send(server, {"cmd": "set_schedule", "events": events})
    send(server, {"cmd": "prerender"})
    resp = send(server, {"cmd": "render", "format": "wav"})
    assert resp.get("data"), f"render returned no data: {resp}"
    wav = base64.b64decode(resp["data"])
    s = samples_of(wav)

    n = len(s)
    assert n > 0, "no samples"
    finite = all(math.isfinite(x) for x in s)
    peak = max(abs(x) for x in s)
    # RMS over the sustained portion (skip the very start / release tail).
    mid = s[n // 4: n // 2]
    rms = math.sqrt(sum(x * x for x in mid) / max(1, len(mid)))

    print(f"samples={n} peak={peak} rms={rms:.1f} finite={finite}")
    assert finite, "non-finite samples in output"
    assert peak > 0, "output is silent"
    print("PASS: DDSP plugin rendered finite, non-silent audio")


if __name__ == "__main__":
    model_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ddsp_selftest"
    main(model_dir)
