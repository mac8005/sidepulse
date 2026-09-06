from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import append_status_audit_record
from .collector import (
    TOOL_FAILURE_MARKERS,
    StatusMetadata,
    status_from_event,
    title_from_event,
)
from .ipc import send_hook_event
from .log_trim import trim_log_if_needed
from .origin import annotate_payload_with_origin
from .providers import detect_log_path, infer_provider_from_payload, parse_log_line

# Tool output is 70-90 % of every hook log, and nothing reads more of it than
# the failure markers near its end. Keep a head and a tail of each string;
# markers that fall in the dropped middle are named in the trim note so the
# failure detector still sees them.
COMPACT_FIELDS = ("tool_response", "toolResponse", "tool_input", "toolInput")
COMPACT_HEAD_CHARS = 1024
COMPACT_TAIL_CHARS = 2048
# Below this the trim note would eat most of the savings.
COMPACT_MIN_SAVINGS = 256


def format_hook_payload(
    provider: str,
    payload_text: str,
    *,
    logged_at: str | None = None,
    include_origin: bool = True,
) -> dict[str, Any]:
    timestamp = logged_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        payload: Any = json.loads(payload_text or "{}")
    except json.JSONDecodeError as exc:
        payload = {
            "hook_event_name": "ParseError",
            "raw": payload_text,
            "parse_error": str(exc),
        }

    if isinstance(payload, dict):
        payload = compact_hook_payload(payload)
    if include_origin and isinstance(payload, dict):
        payload = annotate_payload_with_origin(provider, payload)
    if provider == "codex":
        return {"logged_at": timestamp, "event": payload}
    if isinstance(payload, dict):
        line = dict(payload)
        line["logged_at"] = line.get("logged_at") or timestamp
        return line
    return {"logged_at": timestamp, "event": payload}


def compact_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Shorten the tool output fields of a hook payload; other fields are untouched."""
    compacted = payload
    for key in COMPACT_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        replacement = _compact_value(value)
        if replacement is not value:
            if compacted is payload:
                compacted = dict(payload)
            compacted[key] = replacement
    return compacted


def _compact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _compact_text(value)
    if isinstance(value, dict):
        items = {key: _compact_value(item) for key, item in value.items()}
        return items if any(items[key] is not value[key] for key in value) else value
    if isinstance(value, list):
        items = [_compact_value(item) for item in value]
        return items if any(new is not old for new, old in zip(items, value)) else value
    return value


def _compact_text(text: str) -> str:
    limit = COMPACT_HEAD_CHARS + COMPACT_TAIL_CHARS
    if len(text) <= limit + COMPACT_MIN_SAVINGS:
        return text
    middle = text[COMPACT_HEAD_CHARS:-COMPACT_TAIL_CHARS]
    found = [marker for marker in TOOL_FAILURE_MARKERS if marker in middle.lower()]
    note = f"[{len(middle)} chars trimmed" + (": " + ", ".join(found) if found else "") + "]"
    return f"{text[:COMPACT_HEAD_CHARS]}\n…{note}…\n{text[-COMPACT_TAIL_CHARS:]}"


def write_hook_line(log_path: Path, line: dict[str, Any]) -> None:
    log_path = log_path.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")
    trim_log_if_needed(log_path)


def write_hook_payload(provider: str, log_path: Path, payload_text: str) -> None:
    line = format_hook_payload(provider, payload_text)
    write_hook_line(log_path, line)


def routed_hook_payload(
    provider: str,
    log_path: Path,
    payload_text: str,
) -> tuple[str, Path, dict[str, Any]]:
    line = format_hook_payload(provider, payload_text, include_origin=False)
    actual_provider = infer_provider_from_hook_line(provider, line)
    line = annotate_hook_line(actual_provider, line)
    actual_log_path = log_path
    if actual_provider != provider:
        actual_log_path = detect_log_path(actual_provider)
    return actual_provider, actual_log_path, line


def annotate_hook_line(provider: str, line: dict[str, Any]) -> dict[str, Any]:
    if isinstance(line.get("event"), dict):
        annotated = dict(line)
        annotated["event"] = annotate_payload_with_origin(provider, line["event"])
        return annotated
    return annotate_payload_with_origin(provider, line)


def infer_provider_from_hook_line(provider: str, line: dict[str, Any]) -> str:
    raw = line.get("event") if provider == "codex" else line
    if isinstance(raw, dict):
        return infer_provider_from_payload(provider, raw)
    return provider


def write_hook_status_audit(provider: str, line: dict[str, Any]) -> None:
    try:
        record = parse_log_line(
            provider,
            json.dumps(line, separators=(",", ":"), ensure_ascii=False),
        )
        if record is None:
            return
        metadata = StatusMetadata(cwd=record.cwd, title=title_from_event(record))
        append_status_audit_record(record, status_from_event(record, metadata))
    except Exception:
        pass


def hook_event_socket_disabled() -> bool:
    return os.environ.get("SIDEPULSE_DISABLE_EVENT_SOCKET", "").lower() in {
        "1",
        "true",
        "yes",
    }


def hook_log_main(provider: str, log_path: Path) -> int:
    try:
        actual_provider, actual_log_path, line = routed_hook_payload(
            provider,
            log_path,
            sys.stdin.read(),
        )
        try:
            write_hook_line(actual_log_path, line)
        except Exception:
            pass
        try:
            write_hook_status_audit(actual_provider, line)
        except Exception:
            pass
        try:
            if not hook_event_socket_disabled():
                send_hook_event(actual_provider, line)
        except Exception:
            pass
    except Exception:
        return 0
    return 0
