from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlparse

class GatewayClosed(RuntimeError):
    pass

class _WebSocket:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or "gateway.discord.gg"
        port = parsed.port or 443
        path = parsed.path or "/"
        query = parsed.query
        if query:
            path = f"{path}?{query}"
        raw = socket.create_connection((host, port), timeout=timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        self._buffer = b""
        self._fragments = bytearray()
        self._fragment_opcode = None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: EndstoneDiscordLink/2.2.3\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_headers()
        status_line = response.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            self.close()
            raise GatewayClosed(f"Échec de l'établissement de la connexion WebSocket : {status_line}")
        headers = {}
        for line in response.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        accept = headers.get("sec-websocket-accept", "")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            self.close()
            raise GatewayClosed("Clé d'acceptation WebSocket invalide")
        self.sock.settimeout(1.0)

    def _read_headers(self) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise GatewayClosed("Connexion fermée pendant l'établissement de la connexion WebSocket")
            data.extend(chunk)
            if len(data) > 65536:
                raise GatewayClosed("En-têtes de la réponse WebSocket trop volumineux")
        head, rest = bytes(data).split(b"\r\n\r\n", 1)
        self._buffer = rest
        return head.decode("latin1")

    def _parse_frame(self):
        data = self._buffer
        if len(data) < 2:
            return None
        b1, b2 = data[0], data[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        offset = 2
        if length == 126:
            if len(data) < offset + 2:
                return None
            length = struct.unpack("!H", data[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(data) < offset + 8:
                return None
            length = struct.unpack("!Q", data[offset:offset + 8])[0]
            offset += 8
        mask = b""
        if masked:
            if len(data) < offset + 4:
                return None
            mask = data[offset:offset + 4]
            offset += 4
        if len(data) < offset + length:
            return None
        payload = data[offset:offset + length]
        self._buffer = data[offset + length:]
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self) -> str | None:
        while True:
            frame = self._parse_frame()
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise GatewayClosed("Connexion WebSocket fermée")
                self._buffer += chunk
                continue
            fin, opcode, payload = frame
            if opcode == 8:
                raise GatewayClosed("Discord a fermé la connexion de la passerelle")
            if opcode == 9:
                self._send_frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                if fin:
                    return payload.decode("utf-8")
                self._fragments = bytearray(payload)
                self._fragment_opcode = opcode
                continue
            if opcode == 0 and self._fragment_opcode is not None:
                self._fragments.extend(payload)
                if fin:
                    text = bytes(self._fragments).decode("utf-8")
                    self._fragments = bytearray()
                    self._fragment_opcode = None
                    return text

    def send_json(self, payload: dict) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 1)

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        first = 0x80 | (opcode & 0x0F)
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length < 65536:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

class DiscordGateway:
    def __init__(self, client, on_interaction, logger) -> None:
        self.client = client
        self.on_interaction = on_interaction
        self.logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: _WebSocket | None = None
        self._sequence: int | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discordlink-gateway", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            ws.close()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._ws = None

    def _run(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            try:
                gateway = self.client.get_gateway_bot()
                url = str(gateway.get("url", "wss://gateway.discord.gg"))
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}v=10&encoding=json"
                self._connect_loop(url)
                delay = 2.0
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.logger.warning(f"Passerelle Discord déconnectée : {exc}")
                self._stop.wait(delay)
                delay = min(delay * 1.8, 30.0)

    def _connect_loop(self, url: str) -> None:
        self._sequence = None
        ws = _WebSocket(url)
        self._ws = ws
        heartbeat_interval = None
        next_heartbeat = None
        acked = True
        identified = False
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if heartbeat_interval is not None and next_heartbeat is not None and now >= next_heartbeat:
                    if not acked:
                        raise GatewayClosed("Aucune réponse au heartbeat de la passerelle Discord")
                    ws.send_json({"op": 1, "d": self._sequence})
                    acked = False
                    next_heartbeat = now + heartbeat_interval
                try:
                    text = ws.recv_text()
                except socket.timeout:
                    continue
                if text is None:
                    continue
                payload = json.loads(text)
                op = int(payload.get("op", -1))
                sequence = payload.get("s")
                if sequence is not None:
                    self._sequence = int(sequence)
                if op == 10:
                    heartbeat_interval = max(float(payload["d"]["heartbeat_interval"]) / 1000.0, 1.0)
                    next_heartbeat = time.monotonic() + heartbeat_interval * random.random()
                    if not identified:
                        ws.send_json(
                            {
                                "op": 2,
                                "d": {
                                    "token": self.client.token,
                                    "intents": 1,
                                    "properties": {
                                        "os": "linux",
                                        "browser": "EndstoneDiscordLink",
                                        "device": "EndstoneDiscordLink",
                                    },
                                },
                            }
                        )
                        identified = True
                elif op == 11:
                    acked = True
                elif op == 1:
                    ws.send_json({"op": 1, "d": self._sequence})
                elif op == 7:
                    raise GatewayClosed("Discord demande une reconnexion")
                elif op == 9:
                    raise GatewayClosed("Session de la passerelle Discord invalide")
                elif op == 0 and payload.get("t") == "READY":
                    user = payload.get("d", {}).get("user", {})
                    self.logger.info(f"Passerelle Discord prête (utilisateur : {user.get('username', 'bot')})")
                elif op == 0 and payload.get("t") == "INTERACTION_CREATE":
                    try:
                        self.on_interaction(payload.get("d", {}))
                    except Exception as exc:
                        self.logger.error(f"Échec du gestionnaire d'interaction Discord : {exc}")
        finally:
            ws.close()
            if self._ws is ws:
                self._ws = None
