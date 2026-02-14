# From the arranger root, with the server already running
import socket, struct, json

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/tmp/audio_server.sock")

msg = json.dumps({"cmd": "list_plugins"}).encode()
sock.sendall(struct.pack("<I", len(msg)) + msg)

resp_len = struct.unpack("<I", sock.recv(4))[0]
resp = json.loads(sock.recv(resp_len))
print(json.dumps(resp["plugins"], indent=2))
