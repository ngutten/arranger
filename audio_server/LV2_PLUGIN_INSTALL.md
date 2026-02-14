# Installing LV2 Plugins

The audio server discovers plugins at startup using `lilv_world_load_all()`, which
scans the standard LV2 search path. No configuration is needed on the server side —
install plugins where lilv expects them and they will appear in the graph editor's
**Plugins (LV2)** menu the next time the editor window is opened.

---

## Standard install paths

### Linux

lilv searches these directories in order:

| Path | Scope |
|------|-------|
| `~/.lv2/` | Per-user (preferred for manual installs) |
| `/usr/lib/lv2/` | System-wide (distro packages) |
| `/usr/local/lib/lv2/` | System-wide (compiled from source) |

Each plugin lives in its own subdirectory (called a *bundle*):

```
~/.lv2/
  my-plugin.lv2/
    manifest.ttl
    my-plugin.ttl
    my-plugin.so
```

The bundle name is arbitrary; the `.lv2` suffix is conventional but not required.
What matters is that the directory contains a `manifest.ttl`.

**Via package manager** (easiest):

```bash
# Debian/Ubuntu
sudo apt install lv2-dev calf-plugins guitarix swh-plugins

# Fedora / RHEL
sudo dnf install calf-lv2 guitarix-lv2

# Arch
sudo pacman -S calf
```

**Manual install** (single user):

```bash
mkdir -p ~/.lv2
cp -r path/to/myplugin.lv2 ~/.lv2/
```

**Build from source** (system-wide):

```bash
./configure --prefix=/usr/local
make
sudo make install           # installs to /usr/local/lib/lv2/
```

### macOS

lilv searches:

| Path | Scope |
|------|-------|
| `~/Library/Audio/Plug-Ins/LV2/` | Per-user |
| `/Library/Audio/Plug-Ins/LV2/` | System-wide |

Drop the `.lv2` bundle into either location. Most macOS LV2 distributions ship
as drag-and-drop bundles; copying them to `~/Library/Audio/Plug-Ins/LV2/` is
sufficient.

### Windows

lilv searches:

| Path | Scope |
|------|-------|
| `%APPDATA%\LV2\` | Per-user |
| `%COMMONPROGRAMFILES%\LV2\` | System-wide |

Drop the `.lv2` bundle into one of these. Alternatively, set `LV2_PATH`
(see below) to point at a custom directory.

---

## Custom search path (`LV2_PATH`)

If you keep plugins outside the standard locations, set the `LV2_PATH`
environment variable before launching the arranger. lilv appends this to its
default search list.

```bash
# Linux/macOS — one directory
export LV2_PATH=/opt/my-lv2-plugins

# Linux/macOS — multiple directories (colon-separated)
export LV2_PATH=/opt/plugins:/home/user/dev-plugins

# Windows (semicolon-separated, set before running arranger.py)
set LV2_PATH=C:\Plugins\LV2;D:\OtherPlugins
```

The variable must be set in the environment that launches the audio server
process (i.e. the same terminal or launcher that runs `arranger.py`).

---

## Verifying discovery

After installing, open the **Signal Graph Editor** (the graph icon in the
top bar). Click **＋ Add Node → Plugins (LV2)**. The submenu is populated
once when the editor window opens by querying the server with
`{"cmd": "list_plugins"}`. If the menu shows "No LV2 plugins installed",
the server found nothing in its search path.

To check from the command line without starting the UI:

```python
# From the arranger root, with the server already running
import socket, struct, json

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/tmp/audio_server.sock")

msg = json.dumps({"cmd": "list_plugins"}).encode()
sock.sendall(struct.pack("<I", len(msg)) + msg)

def recv_exact(sock, n):
    """Read exactly n bytes (recv is not guaranteed to return all at once)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("server disconnected")
        buf += chunk
    return bytes(buf)

resp_len = struct.unpack("<I", recv_exact(sock, 4))[0]
resp = json.loads(recv_exact(sock, resp_len))
print(json.dumps(resp["plugins"], indent=2))
```

Or run the bundled test client:

```bash
python audio_server/test/test_client.py list_plugins
```

Each discovered plugin is reported as:

```json
{
  "uri":  "http://example.com/plugins/myplugin",
  "name": "My Plugin",
  "ports": [
    {"symbol": "in",      "name": "Audio In",  "type": "audio",   "direction": "input"},
    {"symbol": "out",     "name": "Audio Out", "type": "audio",   "direction": "output"},
    {"symbol": "gain",    "name": "Gain",      "type": "control", "direction": "input",
     "default": 1.0, "min": 0.0, "max": 2.0}
  ]
}
```

---

## Build flag

LV2 support is compiled in only when `AS_ENABLE_LV2` is defined
(see `CMakeLists.txt`). If **Plugins (LV2)** does not appear as a menu
category at all, the server was built without LV2 support. You can confirm
with a `ping`:

```python
{"cmd": "ping"}
# → {"status": "ok", "version": "...", "features": ["lv2", "fluidsynth", ...]}
```

The `features` list will include `"lv2"` if the build flag was set.

To rebuild with LV2 enabled:

```bash
cd audio_server
cmake -B build -DAS_ENABLE_LV2=ON
cmake --build build
```

You will need `liblilv` (and its headers) installed first:

```bash
# Debian/Ubuntu
sudo apt install liblilv-dev

# Fedora
sudo dnf install lilv-devel

# macOS (Homebrew)
brew install lilv

# Arch
sudo pacman -S lilv
```

---

## Troubleshooting

**Menu says "Loading…" and never updates.**
The `list_plugins` request is made on a background thread when the graph editor
opens. If the server is not running or not yet connected, the request silently
fails and the menu stays in its placeholder state. Check that the server
process started correctly (look for `[audio_server] Listening on:` in the
terminal).

**Plugin appears in the menu but the graph reports an error when loaded.**
The server instantiates the plugin's `LilvInstance` in `LV2Node::activate()`.
Instantiation can fail if the plugin's shared library (`.so` / `.dylib` /
`.dll`) is missing or was compiled for a different architecture. Run
`lv2ls` (from the `lilv-utils` package) to check that the plugin validates:

```bash
lv2ls                          # list all discovered URIs
lv2info <uri>                  # detailed port and metadata dump
lv2lint <uri>                  # spec-compliance checker (if installed)
```

**Plugin is present in `lv2ls` output but absent from the arranger menu.**
The `list_plugins` call accepts an optional `uri_prefix` filter (empty by
default, meaning all plugins are returned). If the server was invoked with a
prefix filter argument somehow, or if you are testing with a filtered call,
that would hide otherwise-valid plugins. The arranger frontend sends
`{"cmd": "list_plugins"}` with no prefix, so this should not arise in
normal use.
