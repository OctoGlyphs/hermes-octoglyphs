"""OctoGlyphs Hermes plugin.

Privacy boundary: this plugin sends metadata only. It never reads or inspects
raw prompts, assistant responses, file contents, tool arguments, tool results,
terminal output, or secrets. Size estimates use only host-provided metadata.
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PORT = 18792
FALLBACK_PORTS = (18792, 18793, 18794, 18795, 18796)
HOST_KIND = "hermes"
PROTOCOL = "octoglyphs.events.v1"
PLUGIN_ROOT = Path(__file__).resolve().parent
SIDECAR_PATH = PLUGIN_ROOT / "octoglyphs_sidecar.py"
STATE_PATH = Path.home() / ".octoglyphs-hermes.json"
_SIDECAR_PROCESS: Optional[subprocess.Popen] = None
_SESSION_STARTS: Dict[str, int] = {}
_LAST_LLM_STARTS: Dict[str, int] = {}

_ALLOWED_EVENT_FIELDS = {
    "session.started": {"type", "timestamp"},
    "session.ended": {"type", "timestamp", "duration_ms"},
    "prompt.sent": {"type", "timestamp", "prompt_chars", "prompt_tokens"},
    "response.started": {"type", "timestamp"},
    "response.completed": {"type", "timestamp", "duration_ms", "completion_tokens", "chunk_count", "tool_call_count"},
    "tool.used": {"type", "timestamp", "tool_kind", "duration_ms", "success"},
    "build.finished": {"type", "timestamp", "kind", "duration_ms", "success"},
    "commit.created": {"type", "timestamp", "files_changed_count", "insertions_count", "deletions_count"},
}

_TOOL_KIND_ALLOWLIST = {
    "file_read",
    "file_write",
    "shell",
    "web",
    "build",
    "test",
    "git",
    "search",
    "memory",
    "other",
}


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_command(
        "octoglyphs",
        handler=_handle_slash_command,
        description="Show OctoGlyphs tank link and sidecar health.",
    )


def on_session_start(session_id: str = "", model: str = "", platform: str = "", **_: Any) -> None:
    port = _ensure_sidecar()
    now = _now_ms()
    _SESSION_STARTS[_safe_session_id(session_id)] = now
    _emit({"type": "session.started", "timestamp": now}, port)
    _emit({"type": "response.started", "timestamp": now}, port)
    print("Your OctoGlyph is blindly feeding on this Hermes session.")
    print(f"Open your tank: http://localhost:{port}/octoglyphs")


def pre_llm_call(
    session_id: str = "",
    user_message: Any = "",
    conversation_history: Any = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "",
    **kwargs: Any,
) -> None:
    port = _ensure_sidecar()
    now = _now_ms()
    _LAST_LLM_STARTS[_safe_session_id(session_id)] = now
    # Privacy: do not read user_message content. Use only host-provided
    # metadata (prompt_chars, prompt_tokens) if available.
    prompt_chars = _safe_int(kwargs.get("prompt_chars") or kwargs.get("metadata", {}).get("prompt_chars"), 0)
    prompt_tokens = _safe_int(kwargs.get("prompt_tokens") or kwargs.get("metadata", {}).get("prompt_tokens"), 0)
    if prompt_tokens == 0 and prompt_chars > 0:
        prompt_tokens = _estimate_tokens(prompt_chars)
    _emit(
        {
            "type": "prompt.sent",
            "timestamp": now,
            "prompt_chars": prompt_chars if prompt_chars > 0 else None,
            "prompt_tokens": prompt_tokens if prompt_tokens > 0 else None,
        },
        port,
    )
    _emit({"type": "response.started", "timestamp": now}, port)
    return None


def post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    success: Any = None,
    error: Any = None,
    **_: Any,
) -> None:
    event_type = "commit.created" if _is_git_commit_tool(tool_name) else "tool.used"
    # Privacy: do not read args or result content. Determine success from
    # host-provided success/error fields only.
    tool_success = True
    if success is not None:
        tool_success = bool(success)
    elif error is not None:
        tool_success = False
    _emit(
        {
            "type": event_type,
            "timestamp": _now_ms(),
            "tool_kind": _categorize_tool(tool_name),
            "duration_ms": 0,
            "success": tool_success,
        }
    )


def post_llm_call(session_id: str = "", model: str = "", platform: str = "", **kwargs: Any) -> None:
    sid = _safe_session_id(session_id)
    started_at = _LAST_LLM_STARTS.pop(sid, None)
    duration_ms = max(0, _now_ms() - started_at) if started_at else 0
    usage = kwargs.get("usage") if isinstance(kwargs.get("usage"), dict) else {}
    completion_tokens = _safe_int(usage.get("output_tokens") or usage.get("completion_tokens"), 0)
    # Privacy: do not read assistant_content_chars from raw response length.
    # Use only token counts from usage metadata.
    _emit(
        {
            "type": "response.completed",
            "timestamp": _now_ms(),
            "duration_ms": duration_ms,
            "completion_tokens": completion_tokens if completion_tokens > 0 else None,
            "tool_call_count": _safe_int(kwargs.get("assistant_tool_call_count"), 0),
        }
    )


def on_session_end(session_id: str = "", completed: bool = True, interrupted: bool = False, **_: Any) -> None:
    _emit_session_end(session_id)


def on_session_finalize(session_id: str = "", platform: str = "", **_: Any) -> None:
    _emit_session_end(session_id)


def on_session_reset(session_id: str = "", platform: str = "", **_: Any) -> None:
    now = _now_ms()
    _SESSION_STARTS[_safe_session_id(session_id)] = now
    _emit({"type": "session.started", "timestamp": now})


def _emit_session_end(session_id: str) -> None:
    sid = _safe_session_id(session_id)
    started_at = _SESSION_STARTS.pop(sid, None)
    duration_ms = max(0, _now_ms() - started_at) if started_at else 0
    _emit({"type": "session.ended", "timestamp": _now_ms(), "duration_ms": duration_ms})


def _handle_slash_command(raw_args: str = "") -> str:
    port = _ensure_sidecar()
    health = "healthy" if _is_sidecar_healthy(port) else "starting"
    return "\n".join(
        [
            "OctoGlyphs Hermes companion",
            f"Tank: http://localhost:{port}/octoglyphs",
            f"Health: http://localhost:{port}/octoglyphs/health",
            f"Status: {health}",
            "Privacy: metadata only. No prompts, responses, file contents, tool args, terminal output, or secrets are sent.",
        ]
    )


def _emit(event: Dict[str, Any], port: Optional[int] = None) -> None:
    clean = _sanitize_event(event)
    if not clean:
        return
    target_port = port or _ensure_sidecar()
    body = json.dumps({"protocol": PROTOCOL, "event": clean}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{target_port}/octoglyphs/events",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=0.35).read()
    except Exception:
        pass


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
        if value is None:
            continue
        if key == "tool_kind":
            clean[key] = _categorize_tool(str(value))
        elif key == "success":
            clean[key] = bool(value)
        elif key.endswith("_ms") or key.endswith("_tokens") or key.endswith("_count") or key.endswith("_chars"):
            clean[key] = _safe_int(value, 0)
        elif key == "kind":
            clean[key] = str(value) if str(value) in {"build", "test", "lint", "typecheck", "unknown"} else "unknown"
    if event_type == "tool.used" and "tool_kind" not in clean:
        clean["tool_kind"] = "other"
    return clean


def _ensure_sidecar() -> int:
    for port in _read_port_candidates():
        if _is_sidecar_healthy(port):
            _write_state(port)
            return port
    for port in _read_port_candidates():
        if _is_port_available(port):
            _start_sidecar(port)
            _write_state(port)
            return port
    return _read_port_candidates()[0]


def _start_sidecar(port: int) -> None:
    global _SIDECAR_PROCESS
    env = dict(os.environ)
    env["OCTOGLYPHS_HERMES_PORT"] = str(port)
    try:
        _SIDECAR_PROCESS = subprocess.Popen(
            [sys.executable, str(SIDECAR_PATH)],
            cwd=str(PLUGIN_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if _is_sidecar_healthy(port):
            return
        time.sleep(0.1)


def _is_sidecar_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/octoglyphs/health", timeout=0.25) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body.get("host") == HOST_KIND and body.get("protocol") == PROTOCOL
    except Exception:
        return False


def _is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _read_port_candidates() -> list[int]:
    raw = os.environ.get("OCTOGLYPHS_HERMES_PORT", "")
    if raw.isdigit():
        port = int(raw)
        if 0 < port < 65536:
            return [port, *[candidate for candidate in FALLBACK_PORTS if candidate != port]]
    return list(FALLBACK_PORTS)


def _write_state(port: int) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps({"port": port, "url": f"http://localhost:{port}/octoglyphs", "updated_at": _now_ms()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _categorize_tool(name: str) -> str:
    text = str(name or "").lower()
    if text in _TOOL_KIND_ALLOWLIST:
        return text
    if "write" in text or "edit" in text or "patch" in text:
        return "file_write"
    if "read" in text or "open" in text:
        return "file_read"
    if "shell" in text or "bash" in text or "terminal" in text or "exec" in text:
        return "shell"
    if "web" in text or "browser" in text or "fetch" in text:
        return "web"
    if "build" in text:
        return "build"
    if "test" in text:
        return "test"
    if "git" in text or "commit" in text:
        return "git"
    if "search" in text or "grep" in text:
        return "search"
    if "memory" in text or "recall" in text:
        return "memory"
    return "other"


def _is_git_commit_tool(tool_name: str) -> bool:
    text = str(tool_name or "").lower()
    return "git" in text and "commit" in text


def _estimate_tokens(char_count: int) -> int:
    return max(0, int((char_count + 3) // 4))


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        number = int(float(value))
    except Exception:
        return fallback
    return max(0, number)


def _safe_session_id(value: Any) -> str:
    text = str(value or "default")
    return text if len(text) <= 128 else text[:128]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _shutdown_sidecar() -> None:
    if _SIDECAR_PROCESS and _SIDECAR_PROCESS.poll() is None:
        try:
            _SIDECAR_PROCESS.terminate()
        except Exception:
            pass


atexit.register(_shutdown_sidecar)
