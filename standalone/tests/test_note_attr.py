"""End-to-end verification of per-note attributes (NoteAttr events).

Drives the C++ engine through the in-process protocol (same path the app uses)
and proves that note_attr events, latched at note-on, actually change the
rendered audio per-note:

  * waveguide "excitation" (categorical): Pluck vs Strike on otherwise
    identical notes produces different output.
  * subtractive "attack" (continuous multiplier): scaling attack changes the
    onset envelope, so two same-block notes differ from the unscaled baseline.

Run: python -m standalone.tests.test_note_attr   (from repo root)
"""
import struct
import sys
from pathlib import Path

# Importing binding_engine runs _load_plugins_dir(), registering the dynamic
# synth plugins (waveguide_string, subtractive_synth, ...) into the global
# plugin registry before we spin up an AudioServer.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from standalone.arranger_engine import AudioServer, AudioEngineConfig  # noqa: E402
import standalone.core.binding_engine  # noqa: F401,E402  (side effect: load plugins)
import json


def make_server():
    cfg = AudioEngineConfig()
    cfg.sample_rate = 44100.0
    cfg.block_size = 512
    return AudioServer(cfg)


def send(server, req):
    return json.loads(server.handle(json.dumps(req)))


def build_graph(server, synth_type):
    desc = {
        "cmd": "set_graph",
        "bpm": 120,
        "nodes": [
            {"id": "synth", "type": synth_type},
            {"id": "mixer", "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            {"from_node": "synth", "from_port": "audio_out_L",
             "to_node": "mixer", "to_port": "audio_in_L_0"},
            {"from_node": "synth", "from_port": "audio_out_R",
             "to_node": "mixer", "to_port": "audio_in_R_0"},
        ],
    }
    resp = send(server, desc)
    assert resp.get("ok", resp.get("status") != "error"), f"set_graph failed: {resp}"


def render(server, events):
    send(server, {"cmd": "set_bpm", "bpm": 120})
    send(server, {"cmd": "set_schedule", "events": events})
    send(server, {"cmd": "prerender"})
    resp = send(server, {"cmd": "render", "format": "wav"})
    b64 = resp.get("data") or resp.get("wav") or resp.get("audio")
    assert b64, f"render returned no data: {list(resp.keys())}"
    import base64
    return base64.b64decode(b64)


def samples_of(wav):
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE", "not a WAV"
    data_size = struct.unpack_from("<I", wav, 40)[0]
    n = data_size // 2
    return struct.unpack_from("<%dh" % n, wav, 44)


def peak(samples):
    return max(abs(s) for s in samples) if samples else 0


def note(beat, dur, pitch, vel=100):
    return [
        {"beat": beat, "type": "note_on", "node_id": "synth",
         "channel": 0, "pitch": pitch, "velocity": vel},
        {"beat": beat + dur, "type": "note_off", "node_id": "synth",
         "channel": 0, "pitch": pitch, "velocity": 0},
    ]


def attr(beat, pitch, name, value):
    return {"beat": beat, "type": "note_attr", "node_id": "synth",
            "channel": 0, "pitch": pitch, "port_id": name, "value": value}


def test_waveguide_excitation():
    server = make_server()
    build_graph(server, "builtin.waveguide_string")

    pluck = render(server, note(0.0, 1.0, 60))                      # default = Pluck (0)
    strike = render(server, [attr(0.0, 60, "excitation", 1.0)] + note(0.0, 1.0, 60))

    sp, ss = samples_of(pluck), samples_of(strike)
    assert peak(sp) > 100, "pluck render silent"
    assert peak(ss) > 100, "strike render silent"
    assert sp != ss, "excitation attr had NO effect — Pluck and Strike identical"
    print(f"  waveguide excitation: pluck peak={peak(sp)} strike peak={peak(ss)} -> DIFFER ✓")


def test_two_notes_same_block_differ():
    """Two notes starting in the same block but with different excitation must
    diverge — proves per-note latching (not block-level) works."""
    server = make_server()
    build_graph(server, "builtin.waveguide_string")

    # both Pluck
    both_pluck = render(server, note(0.0, 1.0, 60) + note(0.0, 1.0, 67))
    # 60=Pluck, 67=Strike, same onset beat/block
    mixed = render(server,
                   [attr(0.0, 67, "excitation", 1.0)]
                   + note(0.0, 1.0, 60) + note(0.0, 1.0, 67))
    assert samples_of(both_pluck) != samples_of(mixed), \
        "per-note excitation in a shared block had no effect"
    print("  per-note latch in shared block: DIFFER ✓")


def test_subtractive_attack():
    server = make_server()
    build_graph(server, "builtin.subtractive_synth")

    base = render(server, note(0.0, 0.5, 60))
    # 8x longer attack — onset ramp slower, so early samples are quieter
    slow = render(server, [attr(0.0, 60, "attack", 8.0)] + note(0.0, 0.5, 60))

    sb, ss = samples_of(base), samples_of(slow)
    assert peak(sb) > 100, "baseline silent"
    assert sb != ss, "attack multiplier had NO effect"
    # The slow-attack onset should have a lower early-energy than the baseline.
    early = 2000
    eb = sum(abs(s) for s in sb[:early])
    es = sum(abs(s) for s in ss[:early])
    assert es < eb, f"slow attack not quieter at onset (base={eb} slow={es})"
    print(f"  subtractive attack: onset energy base={eb} slow={es} (slow<base) ✓")


def test_attr_remap_retargets_lane():
    """attr_remap config re-targets a custom-named lane onto a consumed slot.
    'swell:attack' makes a 'swell' note-attr drive the synth's attack."""
    def render_sub(events, params=None):
        server = make_server()
        desc = {
            "cmd": "set_graph", "bpm": 120,
            "nodes": [
                {"id": "synth", "type": "builtin.subtractive_synth",
                 "params": params or {}},
                {"id": "mixer", "type": "mixer", "channel_count": 1},
            ],
            "connections": [
                {"from_node": "synth", "from_port": "audio_out_L",
                 "to_node": "mixer", "to_port": "audio_in_L_0"},
                {"from_node": "synth", "from_port": "audio_out_R",
                 "to_node": "mixer", "to_port": "audio_in_R_0"},
            ],
        }
        send(server, desc)
        return render(server, events)

    base = render_sub(note(0.0, 0.5, 60))
    direct = render_sub([attr(0.0, 60, "attack", 8.0)] + note(0.0, 0.5, 60))
    # An unmapped 'swell' lane is inert; with remap it must reproduce attack=8.
    swell_off = render_sub([attr(0.0, 60, "swell", 8.0)] + note(0.0, 0.5, 60))
    swell_on = render_sub([attr(0.0, 60, "swell", 8.0)] + note(0.0, 0.5, 60),
                          params={"attr_remap": "swell:attack"})

    assert samples_of(swell_off) == samples_of(base), "unmapped lane should be inert"
    assert samples_of(swell_on) == samples_of(direct), "remapped lane should match attack"
    assert samples_of(swell_on) != samples_of(base), "remap had no audible effect"
    print("  attr_remap swell:attack -> reproduces attack, unmapped inert ✓")


def _find_sf2():
    for p in ("/etc/alternatives/default-GM.sf2",):
        if Path(p).exists():
            return p
    return None


def test_fluidsynth_attack():
    """FluidSynth maps the 'attack' multiplier to GEN_VOLENVATTACK timecents.
    A large multiplier lengthens the onset, lowering early-energy."""
    sf2 = _find_sf2()
    if not sf2:
        print("  fluidsynth attack: SKIPPED (no soundfont found)")
        return

    cfg = AudioEngineConfig()
    cfg.sample_rate = 44100.0
    cfg.block_size = 512
    server = AudioServer(cfg)
    desc = {
        "cmd": "set_graph", "bpm": 120,
        "nodes": [
            {"id": "synth", "type": "builtin.fluidsynth",
             "params": {"sf2_path": sf2}},
            {"id": "mixer", "type": "mixer", "channel_count": 1},
        ],
        "connections": [
            {"from_node": "synth", "from_port": "audio_out_L",
             "to_node": "mixer", "to_port": "audio_in_L_0"},
            {"from_node": "synth", "from_port": "audio_out_R",
             "to_node": "mixer", "to_port": "audio_in_R_0"},
        ],
    }
    resp = send(server, desc)
    assert resp.get("ok", resp.get("status") != "error"), f"set_graph failed: {resp}"
    # Program 48 = string ensemble: a slow-bowable patch where attack is audible.
    prog = [{"beat": 0.0, "type": "program", "node_id": "synth",
             "channel": 0, "pitch": 48, "velocity": 0}]

    base = render(server, prog + note(0.0, 1.0, 60))
    slow = render(server, prog + [attr(0.0, 60, "attack", 30.0)] + note(0.0, 1.0, 60))

    sb, ss = samples_of(base), samples_of(slow)
    assert peak(sb) > 100, "fluidsynth baseline silent"
    assert sb != ss, "fluidsynth attack multiplier had NO effect"
    early = 4000
    eb = sum(abs(s) for s in sb[:early])
    es = sum(abs(s) for s in ss[:early])
    assert es < eb, f"slow attack not quieter at onset (base={eb} slow={es})"
    print(f"  fluidsynth attack: onset energy base={eb} slow={es} (slow<base) ✓")


if __name__ == "__main__":
    test_waveguide_excitation()
    test_two_notes_same_block_differ()
    test_subtractive_attack()
    test_attr_remap_retargets_lane()
    test_fluidsynth_attack()
    print("All note-attr tests passed.")
