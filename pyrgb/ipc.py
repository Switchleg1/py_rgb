"""Tiny line-delimited JSON control channel between the GUI and the daemon.

A loopback TCP socket is used rather than a named pipe because a Windows
service lives in session 0 while the GUI runs in the interactive session;
``127.0.0.1`` works across that boundary without any security descriptor
juggling.  An optional shared token guards against other local processes.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6743
ENCODING = "utf-8"
TIMEOUT = 3.0

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class ControlServer:
    """Accepts one-shot JSON commands and replies with a JSON object."""

    def __init__(
        self,
        handler: Handler,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str = "",
    ) -> None:
        self.handler = handler
        self.host = host
        self.port = port
        self.token = token or ""
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                f"cannot bind the control port {self.host}:{self.port} "
                "(another py_rgb daemon is probably running)"
            ) from exc
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="pyrgb-control", daemon=True)
        self._thread.start()
        log.info("control server listening on %s:%s", self.host, self.port)

    def _serve(self) -> None:
        while not self._stop.is_set():
            assert self._sock is not None
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(TIMEOUT)
            try:
                data = _read_line(conn)
                if not data:
                    return
                message = json.loads(data)
                if self.token and message.get("token") != self.token:
                    reply: dict[str, Any] = {"ok": False, "error": "bad token"}
                else:
                    reply = self.handler(message)
            except Exception as exc:  # noqa: BLE001 - never kill the server
                reply = {"ok": False, "error": str(exc)}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode(ENCODING))
            except OSError:  # pragma: no cover
                pass

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)


def _read_line(conn: socket.socket) -> str:
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).decode(ENCODING, errors="replace").strip()


def send_command(
    command: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str = "",
    timeout: float = TIMEOUT,
    **payload: Any,
) -> dict[str, Any]:
    """Send one command to the daemon and return its reply.

    Raises ``ConnectionError`` when no daemon is listening.
    """
    message: dict[str, Any] = {"cmd": command}
    if token:
        message["token"] = token
    message.update(payload)
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall((json.dumps(message) + "\n").encode(ENCODING))
            raw = _read_line(conn)
    except (OSError, socket.timeout) as exc:
        raise ConnectionError(f"py_rgb daemon not reachable on {host}:{port} ({exc})") from exc
    if not raw:
        raise ConnectionError("daemon closed the connection without replying")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConnectionError(f"malformed reply from daemon: {raw!r}") from exc


def daemon_status(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str = ""
) -> dict[str, Any] | None:
    """Return the daemon status, or ``None`` when it is not running."""
    try:
        reply = send_command("status", host=host, port=port, token=token, timeout=1.0)
    except ConnectionError:
        return None
    return reply if reply.get("ok") else None


def is_running(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str = "") -> bool:
    return daemon_status(host, port, token) is not None
