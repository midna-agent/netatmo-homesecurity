"""
Minimal RFC6455 WebSocket client (stdlib only) for the Netatmo realtime
channel at wss://app-ws.netatmo.net/ws/.

The official app keeps this socket open for the whole foreground session to
receive live events (doorbell presses, motion, module state changes). This
implementation reproduces the same handshake (okhttp UA, permessage-deflate
offer) and keeps the connection alive with automatic reconnect and periodic
pings, so our client behaves like the real app rather than a one-shot script.

Only a dependency-free subset of the protocol is implemented: text/binary
data frames, ping/pong, close, client-side masking, and permessage-deflate
inflation (RFC 7692, context takeover).
"""

from __future__ import annotations

import base64
import os
import ssl
import socket
import struct
import threading
import time
import zlib
from typing import Callable, Optional
from urllib.parse import urlparse

from .api import OKHTTP_USER_AGENT

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class WebSocketChannel:
    """Persistent, self-healing websocket connection."""

    def __init__(self, url: str,
                 on_message: Optional[Callable[[str | bytes], None]] = None,
                 on_state: Optional[Callable[[str], None]] = None,
                 ping_interval: float = 30.0,
                 reconnect_min: float = 1.0,
                 reconnect_max: float = 60.0):
        self.url = url
        self.on_message = on_message or (lambda m: None)
        self.on_state = on_state or (lambda s: None)
        self.ping_interval = ping_interval
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._inflate = None  # zlib decompressobj when permessage-deflate is on

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True,
                                        name="netatmo-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_socket()
        if self._thread:
            self._thread.join(timeout=5)

    def send_text(self, text: str) -> None:
        self._send_frame(OPCODE_TEXT, text.encode("utf-8"))

    # -- connection loop ----------------------------------------------------

    def _run_forever(self) -> None:
        backoff = self.reconnect_min
        while not self._stop.is_set():
            try:
                self._connect()
                self.on_state("connected")
                backoff = self.reconnect_min
                self._read_loop()
            except Exception as exc:  # noqa: BLE001 - keep the session alive
                self.on_state(f"error: {exc}")
            finally:
                self._close_socket()
                self.on_state("disconnected")
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max)

    def _connect(self) -> None:
        u = urlparse(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=20)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate\r\n"
            "Accept-Encoding: gzip\r\n"
            f"User-Agent: {OKHTTP_USER_AGENT}\r\n"
            "\r\n"
        )
        raw.sendall(handshake.encode())

        resp = self._read_until(b"\r\n\r\n")
        status_line = resp.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            raise ConnectionError(f"handshake failed: {status_line}")
        if b"permessage-deflate" in resp.lower():
            # Server accepted compression; prepare a context-takeover inflater.
            self._inflate = zlib.decompressobj(-zlib.MAX_WBITS)
        else:
            self._inflate = None

        raw.settimeout(1.0)  # short timeout so we can honour ping_interval/stop
        self._last_ping = time.time()

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- framing ------------------------------------------------------------

    def _read_until(self, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed during handshake")
            buf += chunk
        return buf

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            try:
                chunk = self._sock.recv(n - len(data))
            except socket.timeout:
                if self._stop.is_set():
                    raise ConnectionError("stopping")
                self._maybe_ping()
                continue
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    def _read_loop(self) -> None:
        frag_op = None
        frag_data = b""
        frag_compressed = False
        while not self._stop.is_set():
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            rsv1 = bool(b0 & 0x40)       # permessage-deflate marker
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked and payload:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))

            if opcode == OPCODE_PING:
                self._send_frame(OPCODE_PONG, payload)
                continue
            if opcode == OPCODE_PONG:
                continue
            if opcode == OPCODE_CLOSE:
                raise ConnectionError("server sent close")

            if opcode in (OPCODE_TEXT, OPCODE_BINARY):
                frag_op = opcode
                frag_data = payload
                frag_compressed = rsv1
            elif opcode == OPCODE_CONT:
                frag_data += payload
            else:
                continue

            if fin:
                data = frag_data
                if frag_compressed and self._inflate is not None:
                    data = self._inflate.decompress(data + b"\x00\x00\xff\xff")
                if frag_op == OPCODE_TEXT:
                    self.on_message(data.decode("utf-8", "replace"))
                else:
                    self.on_message(data)
                frag_op, frag_data, frag_compressed = None, b"", False

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            return
        fin_op = 0x80 | opcode
        mask = os.urandom(4)
        masked_payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        header = bytearray([fin_op])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        with self._send_lock:
            try:
                self._sock.sendall(bytes(header) + masked_payload)
            except OSError as exc:
                raise ConnectionError(f"send failed: {exc}")

    def _maybe_ping(self) -> None:
        now = time.time()
        if now - getattr(self, "_last_ping", 0) >= self.ping_interval:
            self._last_ping = now
            try:
                self._send_frame(OPCODE_PING, b"")
            except ConnectionError:
                pass
