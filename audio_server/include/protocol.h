#pragma once
// protocol.h
// Wire protocol between the Python frontend and the audio server.
//
// Transport: Unix domain socket (Linux) or named pipe (Windows).
// Framing:   4-byte little-endian length prefix, then UTF-8 JSON payload.
//
// All messages are JSON objects with a required "cmd" field.
// Responses always have "status": "ok" | "error", plus cmd-specific fields.
//
// This header is the single authoritative definition of the protocol.
// The Python test client (test_client.py) mirrors these constants.

#include <cstdint>

namespace protocol {

// -------------------------------------------------------------------------
// Socket path / named pipe name
// -------------------------------------------------------------------------

#ifdef AS_PLATFORM_WINDOWS
    // Named pipe — path is the "server address" string passed to IPC layer
    constexpr const char* DEFAULT_ADDRESS = "\\\\.\\pipe\\AudioServer";
#else
    // Unix domain socket in /tmp
    constexpr const char* DEFAULT_ADDRESS = "/tmp/audio_server.sock";
#endif

// -------------------------------------------------------------------------
// Message framing
// -------------------------------------------------------------------------
// Each message = [uint32_t length (LE)] [length bytes of UTF-8 JSON]
// Max message size: 64 MB (generous upper bound for large graph descriptions)
constexpr uint32_t MAX_MESSAGE_BYTES = 64 * 1024 * 1024;

// -------------------------------------------------------------------------
// Commands  (cmd field values)
// -------------------------------------------------------------------------

// -- Server lifecycle --
constexpr const char* CMD_PING          = "ping";           // → {status, version}
constexpr const char* CMD_SHUTDOWN      = "shutdown";       // → {status}

// -- Graph management --
// Set the complete signal graph. Replaces any existing graph atomically.
// Payload: see GraphDesc below.
constexpr const char* CMD_SET_GRAPH     = "set_graph";      // → {status}

// -- Transport --
constexpr const char* CMD_PLAY          = "play";           // → {status}
constexpr const char* CMD_STOP          = "stop";           // → {status}
constexpr const char* CMD_SEEK          = "seek";           // {beat: float} → {status}
constexpr const char* CMD_SET_LOOP      = "set_loop";       // {start,end} or {enabled:false} → {status}
constexpr const char* CMD_GET_POSITION  = "get_position";   // → {beat: float, playing: bool}
constexpr const char* CMD_SET_BPM       = "set_bpm";        // {bpm: float} → {status}

// -- Event stream --
// Send a batch of timed MIDI-style events (replaces current schedule).
// Payload: see EventBatch below.
constexpr const char* CMD_SET_SCHEDULE  = "set_schedule";   // → {status}

// -- Offline render --
// Render the entire schedule offline, return raw PCM as base64.
// {format: "wav"|"raw_f32"} → {status, data: "<base64>", sample_rate, channels}
constexpr const char* CMD_RENDER        = "render";

// -- Node parameter control (realtime, low-latency path) --
// {node_id: str, param_id: str, value: float} → {status}
constexpr const char* CMD_SET_PARAM     = "set_param";

// -- Plugin management --
// {uri: str} → {status, node_id: str, ports: [...]}   (LV2 URI)
constexpr const char* CMD_LOAD_PLUGIN   = "load_plugin";
// {path: str} → {status, node_id: str}                (SF2 file)
constexpr const char* CMD_LOAD_SF2      = "load_sf2";
// {node_id: str} → {status}
constexpr const char* CMD_UNLOAD_NODE   = "unload_node";

// -- Query --
// → {nodes: [...], connections: [...]}
constexpr const char* CMD_GET_GRAPH     = "get_graph";
// {uri_prefix: str (optional)} → {plugins: [...]}
constexpr const char* CMD_LIST_PLUGINS  = "list_plugins";

// -- Plugin descriptor query (new plugin API) --
// → {plugins: [{id, display_name, category, doc, ports: [...], config_params: [...]}]}
// Lists all plugins registered via the new Plugin API (REGISTER_PLUGIN).
constexpr const char* CMD_LIST_REGISTERED_PLUGINS = "list_registered_plugins";

// -- Preview note injection (bypass transport/schedule) --
// {node_id: str, channel: int, pitch: int, velocity: int} → {status}
constexpr const char* CMD_NOTE_ON       = "note_on";
// {node_id: str, channel: int, pitch: int} → {status}
constexpr const char* CMD_NOTE_OFF      = "note_off";
// {node_id: str (optional)} → {status}  omit node_id to silence all sources
constexpr const char* CMD_ALL_NOTES_OFF = "all_notes_off";

// -- Live node reconfiguration --
// {node_id: str, config: {key: value, ...}} → {status}
constexpr const char* CMD_SET_NODE_CONFIG = "set_node_config";

/// Retrieve plugin-specific graph/monitor data for a node.
/// {node_id: str, port_id: str} → {status, data: str (JSON)}
constexpr const char* CMD_GET_NODE_DATA = "get_node_data";

// -------------------------------------------------------------------------
// Graph description (JSON schema, documented as C++ comments)
// -------------------------------------------------------------------------
//
// GraphDesc = {
//   "bpm": float,
//   "sample_rate": int,           // must match server start-up SR (ignored if mismatch)
//   "nodes": [NodeDesc, ...],
//   "connections": [Connection, ...]
// }
//
// NodeDesc = {
//   "id": str,                    // unique within graph, chosen by caller
//   "type": "fluidsynth"          // SF2-backed MIDI synth
//           | "sine"              // built-in sine fallback
//           | "sampler"           // sample player (pitch+vel adjusted)
//           | "lv2"               // LV2 plugin
//           | "mixer"             // N-input stereo mixer (always one, id="mixer")
//           | "control_source"    // emits control values from event stream
//           | "track_source",     // addressable event source for one sequencer track
//                                 //   — no audio ports; fans scheduled + preview events
//                                 //     to downstream processor nodes
//   // type-specific fields:
//   "sf2_path": str,              // fluidsynth only
//   "lv2_uri": str,               // lv2 only
//   "sample_path": str,           // sampler only
//   "channel_count": int,         // mixer: number of input channels (default 2)
//   "params": {str: float, ...}   // initial parameter values
// }
//
// Connection = {
//   "from_node": str,
//   "from_port": str,             // "audio_out_L", "audio_out_R", "control_out"
//   "to_node":   str,
//   "to_port":   str              // "audio_in_L", "audio_in_R", or LV2 port symbol
// }
//
// -------------------------------------------------------------------------
// Schedule / event stream
// -------------------------------------------------------------------------
//
// EventBatch = {
//   "events": [Event, ...]
// }
//
// Event = {
//   "beat":    float,
//   "type":    "note_on" | "note_off" | "program" | "volume" | "bend" | "control",
//   "node_id": str,               // target synth or control_source node
//   "channel": int,               // MIDI channel (0-15)
//   "pitch":   int,               // note / program number
//   "velocity":int,               // 0-127
//   "value":   float              // control events only (normalized 0..1)
// }
//
// The "control" event type delivers a timestamped value to a control_source
// node, which then fans out to any audio nodes whose parameters are connected
// to that control_source's "control_out" port.
//
// -------------------------------------------------------------------------
// Port names (standard, non-LV2)
// -------------------------------------------------------------------------

constexpr const char* PORT_AUDIO_L    = "audio_out_L";
constexpr const char* PORT_AUDIO_R    = "audio_out_R";
constexpr const char* PORT_CONTROL    = "control_out";
constexpr const char* PORT_MIDI       = "midi_out";

} // namespace protocol
