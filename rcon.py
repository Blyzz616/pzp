"""
Minimal Source RCON protocol client (stdlib only, no dependencies).

Implements the standard Source RCON protocol used by Project Zomboid:
https://developer.valvesoftware.com/wiki/Source_RCON_Protocol

STATUS: verified against the real server -- players, servermsg, and
quit have all been confirmed working (quit specifically via
graceful_stop.py's real shutdown test, players and servermsg via direct
CLI smoke tests). Other commands follow the same documented protocol
but haven't been individually exercised.
"""

__version__ = "4.0.1"

import socket
import struct


class RCONError(Exception):
    pass


class RCONClient:
    SERVERDATA_AUTH = 3
    SERVERDATA_AUTH_RESPONSE = 2
    SERVERDATA_EXECCOMMAND = 2  # same wire value as AUTH_RESPONSE by design
    SERVERDATA_RESPONSE_VALUE = 0

    def __init__(self, host, port, password, timeout=5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock = None
        self._req_id = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        try:
            if not self._auth():
                raise RCONError("RCON authentication failed (bad password?)")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def command(self, cmd):
        if not self.sock:
            raise RCONError("Not connected — call connect() first")
        sent_id = self._send_packet(self.SERVERDATA_EXECCOMMAND, cmd)
        parts = []
        while True:
            req_id, pkt_type, body = self._read_packet()
            if req_id != sent_id:
                # Stray/late packet from a previous command — ignore it.
                continue
            parts.append(body)
            # PZ/Source responses are typically a single packet. Only a
            # response near the ~4KB fragmentation boundary would continue;
            # treat anything shorter as complete.
            if len(body) < 4000:
                break
        return "".join(parts)

    # -- internals ---------------------------------------------------

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    def _send_packet(self, pkt_type, body):
        req_id = self._next_id()
        body_bytes = body.encode("utf-8") + b"\x00"
        payload = struct.pack("<ii", req_id, pkt_type) + body_bytes + b"\x00"
        size = struct.pack("<i", len(payload))
        self.sock.sendall(size + payload)
        return req_id

    def _read_packet(self):
        raw_size = self._recv_exact(4)
        size = struct.unpack("<i", raw_size)[0]
        data = self._recv_exact(size)
        req_id, pkt_type = struct.unpack("<ii", data[:8])
        body = data[8:-2]  # strip the two trailing null terminator bytes
        return req_id, pkt_type, body.decode("utf-8", errors="replace")

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RCONError("Connection closed while reading RCON response")
            buf += chunk
        return buf

    def _auth(self):
        self._send_packet(self.SERVERDATA_AUTH, self.password)
        # Some RCON servers send an empty SERVERDATA_RESPONSE_VALUE packet
        # immediately before the real AUTH_RESPONSE. Drain until we see the
        # actual auth response (id == -1 on failure, our sent id on success).
        while True:
            req_id, pkt_type, _body = self._read_packet()
            if pkt_type == self.SERVERDATA_AUTH_RESPONSE:
                return req_id != -1


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("Usage: python3 rcon.py <host> <port> <password> [command]")
        print('Example: python3 rcon.py 127.0.0.1 27015 mypassword players')
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2])
    password = sys.argv[3]
    cmd = sys.argv[4] if len(sys.argv) > 4 else "players"

    print(f"Connecting to {host}:{port} ...")
    with RCONClient(host, port, password) as client:
        print("Authenticated OK.")
        print(f"Sending command: {cmd!r}")
        response = client.command(cmd)
        print("--- response ---")
        print(response)
        print("--- end ---")
