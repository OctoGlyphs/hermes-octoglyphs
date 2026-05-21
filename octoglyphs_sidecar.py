"""Small native Python sidecar for the OctoGlyphs Hermes plugin."""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

DEFAULT_PORT = 18792
HOST_KIND = "hermes"
PROTOCOL = "octoglyphs.events.v1"
STATIC_ROOT = Path(__file__).resolve().parent / "public"
_CLIENTS: set["StreamClient"] = set()
_CLIENTS_LOCK = threading.Lock()

_ALLOWED_EVENT_FIELDS = {
    "session.started": {"type", "timestamp"},
    "session.ended": {"type", "timestamp", "duration_ms"},
    "prompt.sent": {"type", "timestamp", "prompt_chars", "prompt_tokens"},
    "response.started": {"type", "timestamp"},
    "response.chunk": {"type", "timestamp", "chunk_index"},
    "response.completed": {"type", "timestamp", "duration_ms", "completion_tokens", "chunk_count", "tool_call_count"},
    "tool.used": {"type", "timestamp", "tool_kind", "duration_ms", "success"},
    "build.finished": {"type", "timestamp", "kind", "duration_ms", "success"},
    "commit.created": {"type", "timestamp", "files_changed_count", "insertions_count", "deletions_count"},
}

_TOOL_KIND_ALLOWLIST = {"file_read", "file_write", "shell", "web", "build", "test", "git", "search", "memory", "other"}


class StreamClient:
    def __init__(self) -> None:
        self.messages: "queue.Queue[str]" = queue.Queue()
        self.closed = False

    def write(self, chunk: str) -> None:
        if not self.closed:
            self.messages.put(chunk)

    def close(self) -> None:
        self.closed = True
        self.messages.put("")


class OctoGlyphsHandler(BaseHTTPRequestHandler):
    server_version = "OctoGlyphsHermes/0.1"

    def do_GET(self) -> None:
        path = _request_path(self.path)
        if path == "/octoglyphs/health":
            self._write_json(200, {"ok": True, "host": HOST_KIND, "companion": "/octoglyphs", "stream": "/octoglyphs/stream", "protocol": PROTOCOL})
            return
        if path == "/octoglyphs/stream":
            self._open_stream()
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = _request_path(self.path)
        if path != "/octoglyphs/events":
            self._write_json(404, {"ok": False, "error": "not_found"})
            return
        try:
            size = int(self.headers.get("content-length", "0"))
        except ValueError:
            size = 0
        if size > 65536:
            self._write_json(413, {"ok": False, "error": "request_too_large"})
            return
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except Exception:
            self._write_json(400, {"ok": False, "error": "invalid_json"})
            return
        event = _sanitize_event(payload.get("event") if isinstance(payload, dict) else None)
        if not event:
            self._write_json(400, {"ok": False, "error": "invalid_event"})
            return
        _broadcast(event)
        self._write_json(200, {"ok": True})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _open_stream(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache, no-transform")
        self.send_header("connection", "keep-alive")
        self.end_headers()
        client = StreamClient()
        with _CLIENTS_LOCK:
            _CLIENTS.add(client)
        client.write(": octoglyphs stream connected\n\n")
        try:
            while not client.closed:
                try:
                    chunk = client.messages.get(timeout=25)
                except queue.Empty:
                    # Send SSE keepalive comment to prevent timeout disconnect
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if not chunk:
                    break
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            client.close()
            with _CLIENTS_LOCK:
                _CLIENTS.discard(client)

    def _serve_static(self, path: str) -> None:
        file_path = _resolve_static_path(path)
        if not file_path or not file_path.exists() or not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("content-type", mime_type)
        self.send_header("cache-control", "no-cache" if file_path.name == "index.html" else "public, max-age=31536000, immutable")
        self.end_headers()
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 128)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _write_json(self, status: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _broadcast(event: Dict[str, Any]) -> None:
    payload = f"event: octoglyphs\ndata: {json.dumps({'protocol': PROTOCOL, 'event': event})}\n\n"
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS)
    for client in clients:
        client.write(payload)


def _sanitize_event(event: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    allowed = _ALLOWED_EVENT_FIELDS.get(event_type)
    if not allowed:
        return None
    clean: Dict[str, Any] = {"type": event_type, "timestamp": _safe_int(event.get("timestamp"), _now_ms())}
    for key in allowed:
        if key in ("type", "timestamp") or key not in event:
            continue
        value = event.get(key)
        if key == "tool_kind":
            clean[key] = _clean_tool_kind(value)
        elif key == "success":
            clean[key] = bool(value)
        elif key == "kind":
            clean[key] = str(value) if str(value) in {"build", "test", "lint", "typecheck", "unknown"} else "unknown"
        else:
            clean[key] = _safe_int(value, 0)
    if event_type == "tool.used" and "tool_kind" not in clean:
        clean["tool_kind"] = "other"
    return clean


def _clean_tool_kind(value: Any) -> str:
    text = str(value or "other").lower()
    return text if text in _TOOL_KIND_ALLOWLIST else "other"


def _resolve_static_path(path: str) -> Optional[Path]:
    if path in ("/", "/octoglyphs", "/octoglyphs/"):
        relative = "index.html"
    elif path.startswith("/octoglyphs/"):
        relative = unquote(path[len("/octoglyphs/"):])
    elif path.startswith("/assets/"):
        relative = unquote(path.lstrip("/"))
    else:
        relative = unquote(path.lstrip("/"))
    candidate = (STATIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(STATIC_ROOT.resolve())
    except ValueError:
        return None
    return candidate


def _request_path(raw_path: str) -> str:
    return urlparse(raw_path or "/").path


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        number = int(float(value))
    except Exception:
        return fallback
    return max(0, number)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_port() -> int:
    raw = os.environ.get("OCTOGLYPHS_HERMES_PORT", "")
    if raw.isdigit():
        port = int(raw)
        if 0 < port < 65536:
            return port
    return DEFAULT_PORT


def main() -> None:
    port = _read_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), OctoGlyphsHandler)
    print(f"OctoGlyphs Hermes companion running at http://localhost:{port}/octoglyphs", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
