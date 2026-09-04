"""Mirror the agents of a Paseo daemon into the SidePulse event stream.

Paseo (github.com/getpaseo/paseo) hosts Claude Code, Codex, OpenCode and ACP
agents behind one daemon that its iOS and desktop apps attach to. Those
agents never run our hooks the way a terminal session does, so this monitor
subscribes to the daemon's agent directory over its WebSocket API and rewrites
every snapshot change as a hook-style line for the ``paseo`` provider. From
there the collector, status bar, Live Activity and remote-host stream treat
them like any other session. Each line carries the app deep link
``paseo://h/<serverId>/agent/<agentId>``, which opens the exact agent in the
Paseo app on both iOS and macOS.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

PASEO_PROVIDER = "paseo"
DEFAULT_PASEO_HOST = "127.0.0.1:6767"
PROTOCOL_VERSION = 1
CLIENT_ID_PREFIX = "sidepulse-"
RECEIVE_TIMEOUT_SECONDS = 30.0
RECONNECT_MAX_DELAY_SECONDS = 30.0
# Matches encodeURIComponent, which the Paseo apps use when building links.
_URI_COMPONENT_SAFE = "-_.!~*'()"


def paseo_home() -> Path:
    return Path(os.environ.get("PASEO_HOME") or Path.home() / ".paseo")


def paseo_server_id(home: Path | None = None) -> str | None:
    """The stable id the daemon writes to <home>/server-id on first start."""
    try:
        text = ((home or paseo_home()) / "server-id").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def paseo_agent_link(server_id: str | None, agent_id: str | None) -> str | None:
    if not server_id or not agent_id:
        return None
    return (
        f"paseo://h/{quote(server_id, safe=_URI_COMPONENT_SAFE)}"
        f"/agent/{quote(agent_id, safe=_URI_COMPONENT_SAFE)}"
    )


def hook_line_for_agent(agent: dict[str, Any], server_id: str | None) -> dict[str, Any] | None:
    """Translate one Paseo agent snapshot into a hook-style log line.

    The mode is declared explicitly (``sidepulse_mode``) so the collector
    never has to guess from the event name; the event name only exists so
    the record passes the usual parsing and title bookkeeping.
    """
    agent_id = _string(agent.get("id"))
    if not agent_id:
        return None

    status = _string(agent.get("status")) or ""
    attention = _string(agent.get("attentionReason"))
    pending = [item for item in agent.get("pendingPermissions") or [] if isinstance(item, dict)]
    archived = bool(agent.get("archivedAt"))
    title = _string(agent.get("title"))
    message: str | None = None
    tool_name: str | None = None

    if status == "closed" or archived:
        event_name, mode = "SessionEnd", "completed"
    elif pending or attention == "permission":
        event_name, mode = "PermissionRequest", "waiting_for_input"
        if pending:
            tool_name = _string(pending[0].get("name"))
            message = _string(pending[0].get("title")) or _string(pending[0].get("description"))
    elif status == "error":
        event_name, mode = "PostToolUseFailure", "blocked_error"
        message = _string(agent.get("lastError"))
    elif status in {"running", "initializing"}:
        event_name, mode = "UserPromptSubmit", "working"
    elif attention == "finished":
        event_name, mode = "Stop", "completed"
    else:
        event_name, mode = "SessionStart", "idle_ready"

    line: dict[str, Any] = {
        "hook_event_name": event_name,
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "session_id": agent_id,
        "cwd": _string(agent.get("cwd")),
        "sidepulse_mode": mode,
        "agent_origin": f"Paseo {agent.get('provider')}" if agent.get("provider") else "Paseo",
        "paseo_provider": agent.get("provider"),
        "model": agent.get("model"),
        "paseo_status": status,
    }
    if title:
        # The prompt is what the collector titles a session from.
        line["prompt"] = title
    if tool_name:
        line["tool_name"] = tool_name
    if message:
        line["message"] = message
    link = paseo_agent_link(server_id, agent_id)
    if link:
        line["sidepulse_deep_link"] = link
    return line


def agent_signature(agent: dict[str, Any]) -> tuple[Any, ...]:
    """The parts of a snapshot whose change is worth a new log line."""
    pending = agent.get("pendingPermissions") or []
    return (
        agent.get("status"),
        agent.get("attentionReason"),
        tuple(item.get("id") for item in pending if isinstance(item, dict)),
        bool(agent.get("archivedAt")),
        agent.get("title"),
        agent.get("lastError"),
    )


def closed_line(agent_id: str, server_id: str | None) -> dict[str, Any]:
    return hook_line_for_agent({"id": agent_id, "status": "closed"}, server_id) or {}


class PaseoMonitor:
    """Keeps one subscription to the daemon and emits lines on change."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_PASEO_HOST,
        password: str | None = None,
        server_id: str | None = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.host = host
        self.password = password
        self.server_id = server_id
        self.emit = emit or emit_hook_line
        self.log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        self.signatures: dict[str, tuple[Any, ...]] = {}

    def run_forever(self) -> None:
        delay = 1.0
        while True:
            started = time.monotonic()
            try:
                self.run_once()
            except (OSError, ValueError) as exc:
                self.log(f"paseo connection lost: {exc}")
            if time.monotonic() - started > 60:
                delay = 1.0
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)

    def run_once(self) -> None:
        host, port = split_host_port(self.host)
        subprotocol = f"paseo.bearer.{self.password}" if self.password else None
        ws = WebSocket.connect(host, port, "/ws", subprotocol=subprotocol)
        try:
            ws.send_json(
                {
                    "type": "hello",
                    "clientId": f"{CLIENT_ID_PREFIX}{uuid.uuid4().hex[:12]}",
                    "clientType": "cli",
                    "protocolVersion": PROTOCOL_VERSION,
                }
            )
            ws.send_json(
                {
                    "type": "session",
                    "message": {
                        "type": "fetch_agents_request",
                        "requestId": uuid.uuid4().hex,
                        "filter": {"includeArchived": False},
                        "page": {"limit": 200},
                        "subscribe": {"subscriptionId": uuid.uuid4().hex},
                    },
                }
            )
            self.log(f"subscribed to paseo agents at {self.host}")
            while True:
                text = ws.receive_text(timeout=RECEIVE_TIMEOUT_SECONDS)
                if text is None:
                    ws.send_json({"type": "ping", "requestId": uuid.uuid4().hex})
                    continue
                self.handle_message(text)
        finally:
            ws.close()

    def handle_message(self, text: str) -> None:
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(envelope, dict):
            return
        if envelope.get("type") != "session":
            return
        message = envelope.get("message")
        if not isinstance(message, dict):
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        kind = message.get("type")

        if kind == "status" and payload.get("status") == "server_info":
            server_id = _string(payload.get("serverId"))
            if server_id and server_id != self.server_id:
                self.server_id = server_id
                self.log(f"paseo server id {server_id}")
            return
        if kind == "fetch_agents_response":
            seen: set[str] = set()
            for entry in payload.get("entries") or []:
                agent = entry.get("agent") if isinstance(entry, dict) else None
                if isinstance(agent, dict) and _string(agent.get("id")):
                    seen.add(str(agent["id"]))
                    self.upsert(agent)
            # The directory is authoritative: anything we still track but the
            # daemon no longer lists was closed while we were disconnected.
            for agent_id in list(self.signatures):
                if agent_id not in seen:
                    self.remove(agent_id)
            return
        if kind == "agent_update":
            if payload.get("kind") == "upsert" and isinstance(payload.get("agent"), dict):
                self.upsert(payload["agent"])
            elif payload.get("kind") == "remove" and _string(payload.get("agentId")):
                self.remove(str(payload["agentId"]))

    def upsert(self, agent: dict[str, Any]) -> None:
        agent_id = str(agent["id"])
        signature = agent_signature(agent)
        if self.signatures.get(agent_id) == signature:
            return
        line = hook_line_for_agent(agent, self.server_id)
        if line is None:
            return
        self.signatures[agent_id] = signature
        if line["hook_event_name"] == "SessionEnd":
            self.signatures.pop(agent_id, None)
        self.emit(PASEO_PROVIDER, line)

    def remove(self, agent_id: str) -> None:
        if self.signatures.pop(agent_id, None) is None:
            return
        self.emit(PASEO_PROVIDER, closed_line(agent_id, self.server_id))


def emit_hook_line(provider: str, line: dict[str, Any]) -> None:
    # Imported here: session_actions needs the link helpers above, and the
    # hook module reaches session_actions again through settings.
    from .hook import write_hook_line
    from .ipc import send_hook_event
    from .providers import detect_log_path

    write_hook_line(detect_log_path(provider), line)
    send_hook_event(provider, line)


def run_paseo_monitor(
    *,
    host: str | None = None,
    password: str | None = None,
) -> int:
    monitor = PaseoMonitor(
        host=host or os.environ.get("PASEO_HOST") or DEFAULT_PASEO_HOST,
        password=password or os.environ.get("PASEO_PASSWORD") or None,
        server_id=paseo_server_id(),
    )
    try:
        monitor.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def split_host_port(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"Paseo host must look like host:port, got {value!r}")
    return host.strip("[]") or "127.0.0.1", int(port)


class WebSocket:
    """Just enough RFC 6455 client for one authenticated text session.

    Paseo authenticates by echoing the bearer token as the requested
    subprotocol, so the handshake is the only unusual part; frames are
    plain text, with ping/pong answered inline.
    """

    def __init__(self, sock: socket.socket, pending: bytes) -> None:
        self.sock = sock
        self.pending = pending

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        path: str,
        *,
        subprotocol: str | None,
        timeout: float = 10.0,
    ) -> "WebSocket":
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            headers = [
                f"GET {path} HTTP/1.1",
                f"Host: {host}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if subprotocol:
                headers.append(f"Sec-WebSocket-Protocol: {subprotocol}")
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("utf-8"))

            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("websocket handshake closed by server")
                response += chunk
                if len(response) > 65536:
                    raise OSError("websocket handshake response too large")
            head, _, rest = response.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            if " 101 " not in status_line:
                raise OSError(f"websocket handshake rejected: {status_line}")
        except BaseException:
            sock.close()
            raise
        return cls(sock, rest)

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()

    def send_json(self, message: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def receive_text(self, *, timeout: float) -> str | None:
        """The next complete text message, or None once ``timeout`` passes."""
        deadline = time.monotonic() + timeout
        fragments: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                fin, opcode, payload = self._receive_frame()
            except socket.timeout:
                return None
            if opcode == 0x8:
                raise OSError("websocket closed by server")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in (0xA, 0x2):
                continue
            if opcode in (0x1, 0x0):
                fragments.append(payload)
                if fin:
                    text = b"".join(fragments).decode("utf-8", errors="replace")
                    fragments = []
                    return text

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _receive_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _read_exact(self, count: int) -> bytes:
        while len(self.pending) < count:
            chunk = self.sock.recv(max(4096, count - len(self.pending)))
            if not chunk:
                raise OSError("websocket connection closed")
            self.pending += chunk
        data, self.pending = self.pending[:count], self.pending[count:]
        return data


def _string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
