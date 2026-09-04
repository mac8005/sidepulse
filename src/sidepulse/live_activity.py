"""Mirror agent statuses to an iOS Live Activity and an in-app live stream.

The daemon has two delivery paths that share one snapshot loop:

- An HTTP server (``/register``, ``/snapshot``, ``/stream``, ``/health``).
  ``/stream`` is Server-Sent Events and feeds the iOS app's realtime view
  over LAN or Tailscale while the app is in the foreground.
- APNs ``liveactivity`` pushes keep a Lock Screen / Dynamic Island Live
  Activity current while the phone is locked. With an iOS 17.2+
  push-to-start token the daemon also *starts* the activity whenever
  agents become active, and ends it when the host goes idle.

APNs needs ``httpx[http2]`` and ``cryptography`` (the ``live-activity``
extra); the HTTP server and SSE stream are stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

from .collector import AgentMonitor
from .ipc import HookEventServer
from .hook import write_hook_line
from .led_status import display_state_for_mode
from .paseo_monitor import paseo_agent_link, paseo_server_id
from .providers import SUMMARY_EVENT_NAME
from .models import MODE_PRIORITY, AgentStatus
from .providers import default_state_dir
from .title_integrity import (
    humanize_title_text,
    is_readable_session_title,
    normalize_user_request,
)

MAX_AGENT_ROWS = 6
MAX_FINISHED_ROWS = 3
RECENT_FINISHED_SECONDS = 30 * 60.0
TERMINAL_MODES = {"completed", "idle_ready"}
MAX_NAME_CHARS = 120
MAX_DETAIL_CHARS = 32
PUSH_MIN_INTERVAL_SECONDS = 1.0
# Cosmetic content (summary/detail text) coalesces into low-priority pushes
# on this cadence, so the iOS update budget stays available for the
# structural changes people actually watch for.
COSMETIC_PUSH_INTERVAL_SECONDS = 60.0
# Leave enough margin for APNs to defer a low-priority heartbeat without
# expiring the Dynamic Island. The previous six-minute deadline was reached
# during one short burst of throttling even though the daemon stayed healthy.
STALE_AFTER_SECONDS = 15 * 60.0
PUSH_HEARTBEAT_SECONDS = 5 * 60.0
# Once everything is finished the content stops changing, so nothing gets
# pushed — and an activity that died on the phone meanwhile stays "live" in
# the daemon's belief, because only a push can come back 410. Probe slowly
# while idle: that is what turns a silent death into a restart.
IDLE_HEARTBEAT_SECONDS = 900.0
# Push-to-start retry while there is something to show but the phone has
# not registered an activity: a start push can be lost or throttled, and the
# system ends activities after eight hours. Every start carries Apple's
# required alert, so retries back off (2, 4, 8, 16, then every 30 minutes).
PUSH_TO_START_COOLDOWN_SECONDS = 120.0
PUSH_TO_START_MAX_BACKOFF_SECONDS = 1800.0
# Every start push creates a NEW activity, and one whose token never reaches
# the daemon can no longer be ended remotely — it just sits on the Lock
# Screen. So keep a hard floor between any two start pushes, and stop after
# a few unanswered ones until the phone proves something changed.
START_PUSH_MIN_GAP_SECONDS = 45.0
# The app reports "no activity" when our OWN dedup ends the previous one:
# an immediate end flips that activity to .dismissed, which the app cannot
# tell from a swipe-away. Acting on that echo ends the activity we just
# started, and the next start push repeats it. Ignore resets this close to
# a start push — a real "the phone has nothing" reset survives the wait.
RESET_ECHO_SECONDS = 60.0
MAX_UNANSWERED_START_PUSHES = 3
# Once an unanswered start burst is exhausted, ask the ordinary app process
# to reconcile ActivityKit once. This push cannot create a duplicate activity;
# it only gives the app a chance to report what iOS actually has.
START_RECONCILE_NUDGE_DELAY_SECONDS = 60.0
START_RECONCILE_RETRY_SECONDS = 30 * 60.0
ACTIVITY_REPORT_STALE_SECONDS = 30 * 60.0
START_RECONCILE_NUDGE_EXPIRY_SECONDS = 15 * 60.0
START_RECONCILE_COLLAPSE_ID = "sidepulse-live-activity-reconcile"
# An unconfirmed push-to-start activity can remain active for eight hours and
# its ended card can linger for four more. Only reopen the autonomous start
# burst after that complete safety window, so recovery is eventual without
# building an unreachable stack on the Lock Screen.
START_PUSH_SAFE_RECOVERY_SECONDS = 12 * 3600.0
# iOS ends a Live Activity eight hours in: the Dynamic Island slot goes at
# once while a dead card lingers on the Lock Screen for hours. Rotate a
# little early, so the swap happens while the update token still answers.
ACTIVITY_MAX_AGE_SECONDS = 7.5 * 3600
SSE_HEARTBEAT_SECONDS = 10.0
ATTRIBUTES_TYPE = "AgentActivityAttributes"
# A Dot plugged into the phone shows one of four states. Background pushes
# are best-effort and heavily budgeted by iOS, so brief aggregate-mode flaps
# settle before becoming commands. APNs may retain the newest collapsed
# command for an hour; accepted commands retry twice within that TTL unless
# the phone acknowledges the actual LED write.
DOT_STATE_SETTLE_SECONDS = 10.0
DOT_PUSH_EXPIRY_SECONDS = 3600.0
DOT_PUSH_FAILURE_RETRY_SECONDS = 60.0
DOT_PUSH_FAILURE_MAX_RETRY_SECONDS = 20 * 60.0
DOT_PUSH_RETRY_OFFSETS_SECONDS = (0.0, 2 * 60.0, 20 * 60.0)
DOT_RESYNC_COOLDOWN_SECONDS = 60.0
DOT_WORKING_REFRESH_SECONDS = 20 * 60.0
DOT_COLLAPSE_ID = "sidepulse-dot-state"
DOT_ACK_SUCCESS_STATUSES = {"written", "alreadyCurrent"}
DOT_UNAVAILABLE_MIN_SECONDS = 60.0
DOT_UNAVAILABLE_MAX_SECONDS = 24 * 60 * 60.0
DOT_REPORTED_AT_MAX_FUTURE_SECONDS = 5 * 60.0
DOT_DND_TRANSITION_MAX_FUTURE_SECONDS = 3 * 24 * 60 * 60.0
DOT_STREAM_TOKEN_MAX_CHARS = 512
DOT_UNAVAILABLE_REASONS = {
    "brightness_zero",
    "disconnected",
    "dnd",
    "focus",
    "no_folder",
    "write_failed",
}
DOT_UNAVAILABLE_METADATA_KEYS = (
    "dot_unavailable_until",
    "dot_unavailable_reason",
    "dot_status_at",
    "dot_client_reported_at",
    "dot_dnd_schedule_enabled",
    "dot_next_dnd_transition_at",
    "dot_next_dnd_transition_enabled",
    "dot_schedule_reported_at",
    "dot_focus_active",
    "dot_focus_reported_at",
)

# Modes worth interrupting the user for, and their notification titles.
ALERT_MODES = {
    "waiting_for_input": "Needs your input",
    "blocked_error": "Blocked",
    "completed": "Finished",
}
ALERT_SOUNDS = {
    "completed": "AgentFinished.caf",
    "waiting_for_input": "AgentNeedsInput.caf",
    "blocked_error": "AgentBlocked.caf",
}
ALERT_COOLDOWN_SECONDS = 90.0
FINISHED_ALERT_DEFER_SECONDS = 20.0


@dataclass(frozen=True)
class LiveActivityConfig:
    apns_key_path: Path
    apns_key_id: str
    apns_team_id: str
    bundle_id: str = "io.sidepulse.app"
    apns_environment: str = "production"
    host_label: str = field(default_factory=lambda: socket.gethostname().split(".")[0])
    port: int = 8787
    poll_seconds: float = 2.0
    idle_end_minutes: float = 10.0
    summaries_enabled: bool = True
    summary_model: str = "claude-haiku-4-5-20251001"

    @property
    def apns_host(self) -> str:
        if self.apns_environment.lower() in {"prod", "production"}:
            return "api.push.apple.com"
        return "api.sandbox.push.apple.com"

    @property
    def liveactivity_topic(self) -> str:
        return f"{self.bundle_id}.push-type.liveactivity"


@dataclass
class PendingDotPush:
    command_id: str
    state: str
    has_unread_finished: bool
    content_state: dict[str, Any]
    created_at: float
    issued_at: float
    accepted_attempts: int = 0
    rejected_attempts: int = 0
    next_attempt_at: float = 0.0


@dataclass(frozen=True)
class DotDndSchedule:
    enabled: bool
    next_transition_at: float | None
    next_transition_enabled: bool | None
    reported_at: float


class StaleCompletionError(Exception):
    """The client tried to acknowledge an older completion generation."""


LIVE_ACTIVITY_STATES = {
    "active",
    "pending",
    "stale",
    "ended",
    "dismissed",
    "none",
    "unknown",
}
LIVE_ACTIVITY_NONLIVE_STATES = {"ended", "dismissed", "none", "unknown"}
LIVE_ACTIVITY_METADATA_KEYS = (
    "activity_state",
    "activities_enabled",
    "frequent_pushes_enabled",
)
DEVICE_ID_MAX_CHARS = 128


def _parse_live_activity_metadata(body: dict[str, Any]) -> dict[str, Any]:
    """Validate optional ActivityKit capability and lifecycle evidence."""
    metadata: dict[str, Any] = {}
    if "activity_state" in body:
        state = body["activity_state"]
        if not isinstance(state, str):
            raise ValueError("activity_state must be a string")
        state = state.strip().lower().removeprefix(".")
        if state not in LIVE_ACTIVITY_STATES:
            raise ValueError("invalid activity_state")
        metadata["activity_state"] = state
    for key in ("activities_enabled", "frequent_pushes_enabled"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        metadata[key] = value
    return metadata


def _parse_dot_availability(
    body: dict[str, Any],
) -> tuple[bool, str | None, float | None, float | None] | None:
    """Validate optional Dot availability fields from an iOS report."""
    if "available" not in body:
        return None
    available = body["available"]
    if not isinstance(available, bool):
        raise ValueError("available must be a boolean")
    reported_at = body.get("reportedAt")
    if reported_at is not None and (
        isinstance(reported_at, bool)
        or not isinstance(reported_at, (int, float))
        or not math.isfinite(reported_at)
    ):
        raise ValueError("reportedAt must be a number")
    client_time = float(reported_at) if reported_at is not None else None
    if available:
        return True, None, None, client_time

    reason = body.get("reason")
    if reason not in DOT_UNAVAILABLE_REASONS:
        raise ValueError("invalid Dot unavailability reason")
    retry_after = body.get("retryAfterSeconds")
    if (
        isinstance(retry_after, bool)
        or not isinstance(retry_after, (int, float))
        or not math.isfinite(retry_after)
        or retry_after <= 0
    ):
        raise ValueError("retryAfterSeconds must be a positive number")
    return (
        False,
        reason,
        max(
            DOT_UNAVAILABLE_MIN_SECONDS,
            min(float(retry_after), DOT_UNAVAILABLE_MAX_SECONDS),
        ),
        client_time,
    )


def _parse_dot_dnd_schedule(body: dict[str, Any]) -> DotDndSchedule | None:
    """Validate optional next-boundary metadata from the elected phone."""
    keys = {
        "dndScheduleEnabled",
        "nextDndTransitionAt",
        "nextDndTransitionEnabled",
    }
    if not keys.intersection(body):
        return None

    enabled = body.get("dndScheduleEnabled")
    if not isinstance(enabled, bool):
        raise ValueError("dndScheduleEnabled must be a boolean")
    reported_at = body.get("reportedAt")
    if (
        isinstance(reported_at, bool)
        or not isinstance(reported_at, (int, float))
        or not math.isfinite(reported_at)
    ):
        raise ValueError("reportedAt is required with DND schedule metadata")
    if reported_at > time.time() + DOT_REPORTED_AT_MAX_FUTURE_SECONDS:
        raise ValueError("reportedAt is too far in the future")

    transition_at = body.get("nextDndTransitionAt")
    transition_enabled = body.get("nextDndTransitionEnabled")
    if not enabled:
        if transition_at is not None or transition_enabled is not None:
            raise ValueError("disabled DND schedule cannot have a next transition")
        return DotDndSchedule(False, None, None, float(reported_at))
    if (
        isinstance(transition_at, bool)
        or not isinstance(transition_at, (int, float))
        or not math.isfinite(transition_at)
        or transition_at <= 0
    ):
        raise ValueError("nextDndTransitionAt must be a positive number")
    if transition_at > reported_at + DOT_DND_TRANSITION_MAX_FUTURE_SECONDS:
        raise ValueError("nextDndTransitionAt is too far in the future")
    if not isinstance(transition_enabled, bool):
        raise ValueError("nextDndTransitionEnabled must be a boolean")
    return DotDndSchedule(
        True,
        float(transition_at),
        transition_enabled,
        float(reported_at),
    )


def _parse_dot_focus(body: dict[str, Any]) -> tuple[bool, float]:
    """Validate an ordered Focus-state report."""
    focused = body.get("focused")
    if not isinstance(focused, bool):
        raise ValueError("focused must be a boolean")
    reported_at = body.get("reportedAt")
    if (
        isinstance(reported_at, bool)
        or not isinstance(reported_at, (int, float))
        or not math.isfinite(reported_at)
    ):
        raise ValueError("reportedAt must be a number")
    if reported_at > time.time() + DOT_REPORTED_AT_MAX_FUTURE_SECONDS:
        raise ValueError("reportedAt is too far in the future")
    return focused, float(reported_at)


def _log(message: str) -> None:
    """One timestamped daemon line.

    launchd captures stdout to a file that is only ever read after the fact,
    when the question is "what happened at 11:15?" — an untimestamped line
    cannot answer it.
    """
    print(f"{datetime.now().strftime('%H:%M:%S')} live-activity: {message}", flush=True)


def default_token_store_path() -> Path:
    return default_state_dir() / "live_activity_tokens.json"


class TokenStore:
    """Registered APNs tokens, persisted so restarts keep the phone linked."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_token_store_path()
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, Any]]] = {
            "push_to_start": {},
            "update": {},
            "device": {},
            "dot_device": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for kind in ("push_to_start", "update", "device", "dot_device"):
            entries = raw.get(kind)
            if isinstance(entries, dict):
                self._data[kind] = {
                    str(token): dict(meta)
                    for token, meta in entries.items()
                    if isinstance(meta, dict)
                }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError:
            pass

    def register(self, kind: str, token: str, meta: dict[str, Any]) -> None:
        with self._lock:
            meta = dict(meta)
            meta["registered_at"] = datetime.now(timezone.utc).isoformat()
            self._data[kind][token] = meta
            self._save()

    def replace(self, kind: str, token: str, meta: dict[str, Any]) -> bool:
        """Atomically elect one token; return whether the owner changed."""
        with self._lock:
            changed = list(self._data[kind]) != [token]
            meta = dict(meta)
            meta["registered_at"] = datetime.now(timezone.utc).isoformat()
            self._data[kind] = {token: meta}
            self._save()
            return changed

    def replace_for_device(self, kind: str, token: str, meta: dict[str, Any]) -> bool:
        """Keep one token per device while preserving other registered phones."""
        with self._lock:
            device = str(meta.get("device", ""))
            device_id = str(meta.get("device_id", ""))
            previous = set(self._data[kind])
            self._data[kind] = {
                old_token: old_meta
                for old_token, old_meta in self._data[kind].items()
                if old_token == token
                or (
                    str(old_meta.get("device_id", "")) != device_id
                    if device_id
                    else bool(str(old_meta.get("device_id", "")))
                    or not device
                    or str(old_meta.get("device", "")) != device
                )
            }
            stored_meta = dict(meta)
            stored_meta["registered_at"] = datetime.now(timezone.utc).isoformat()
            self._data[kind][token] = stored_meta
            self._save()
            return set(self._data[kind]) != previous

    def tokens(self, kind: str) -> list[str]:
        with self._lock:
            return list(self._data[kind])

    def entries(self, kind: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {token: dict(meta) for token, meta in self._data[kind].items()}

    def contains(self, kind: str, token: str) -> bool:
        with self._lock:
            return token in self._data[kind]

    def update_metadata(
        self, kind: str, token: str, values: dict[str, Any]
    ) -> bool:
        """Update one registered token without changing token ownership."""
        with self._lock:
            current = self._data[kind].get(token)
            if current is None:
                return False
            updated = dict(current)
            for key, value in values.items():
                if value is None:
                    updated.pop(key, None)
                else:
                    updated[key] = value
            self._data[kind][token] = updated
            self._save()
            return True

    def drop(self, kind: str, token: str) -> None:
        with self._lock:
            if self._data[kind].pop(token, None) is not None:
                self._save()

    def clear(self, kind: str) -> None:
        with self._lock:
            if self._data[kind]:
                self._data[kind] = {}
                self._save()

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {kind: len(entries) for kind, entries in self._data.items()}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


class DeepLinkResolver:
    """Finds the exact mobile URL for a Claude or Codex session.

    Claude uses three sources, cheapest first: the per-pid session registry
    ~/.claude/sessions/<pid>.json (workers the remote-control daemon spawns
    record their `bridgeSessionId` only here, never in the transcript), then
    ~/.claude/projects/**/<session_id>.jsonl for a `bridgeSessionId` on a
    bridge-session record or a top-level `url` field (written only by
    sessions started with --rc). The bridge id is the claude.ai/code URL
    suffix — prefixed cse_ in transcripts and session_ in the registry.
    Only the session's own URL is safe to deep-link; the environment URL
    spawns a NEW session when at capacity.

    Codex Remote links combine the thread UUID with the owning Mac's current
    Remote environment id. The /app wrapper is claimed by ChatGPT's iOS
    associated domains and unwraps to the native remote-thread route.

    Paseo links combine the agent id with the local daemon's server id; the
    iOS and desktop apps resolve the server id to the paired host.
    """

    MISS_TTL_SECONDS = 120.0
    CODEX_ENVIRONMENT_KEY = "electron-local-remote-control-environment-id"

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._misses: dict[str, float] = {}
        self._roots = [Path.home() / ".claude" / "projects"]
        self._registry = Path.home() / ".claude" / "sessions"
        self._codex_state_dir = Path.home() / ".codex"
        self._codex_global_state = self._codex_state_dir / ".codex-global-state.json"
        self._codex_environment: str | None = None
        self._codex_environment_checked_at: float | None = None
        self._paseo_server_id: str | None = None

    def link_for(self, provider: str, session_id: str | None) -> str | None:
        # remote: rows live on another host, so their transcript can never
        # be found locally and their Codex host id would be wrong. The origin
        # host attaches those links before streaming the event.
        if not session_id or session_id.startswith("remote:"):
            return None
        if provider == "codex":
            return self._codex_link(session_id)
        if provider == "paseo":
            if self._paseo_server_id is None:
                self._paseo_server_id = paseo_server_id()
            return paseo_agent_link(self._paseo_server_id, session_id)
        if provider != "claude":
            return None
        cached = self._cache.get(session_id)
        if cached is not None:
            return cached
        # A session can attach to the bridge after we first looked, so a
        # miss is retried once the TTL lapses instead of sticking forever.
        missed_at = self._misses.get(session_id)
        if missed_at is not None and time.time() - missed_at < self.MISS_TTL_SECONDS:
            return None
        url = self._scan(session_id)
        if url:
            self._cache[session_id] = url
            self._misses.pop(session_id, None)
        else:
            self._misses[session_id] = time.time()
        return url

    def _codex_link(self, session_id: str) -> str | None:
        try:
            thread_id = str(UUID(session_id))
        except (AttributeError, ValueError):
            return None
        if thread_id != session_id.lower():
            return None
        environment_id = self._codex_environment_id()
        if not environment_id:
            return None
        host_id = f"slingshot:{environment_id}:8765"
        return (
            f"https://chatgpt.com/app/codex/remote/thread/{thread_id}?"
            + urlencode({"hostId": host_id})
        )

    def _codex_environment_id(self) -> str | None:
        now = time.time()
        if (
            self._codex_environment_checked_at is not None
            and now - self._codex_environment_checked_at < self.MISS_TTL_SECONDS
        ):
            return self._codex_environment

        environment_id = self._codex_environment_from_global_state()
        if environment_id is None:
            environment_id = self._codex_environment_from_enrollments()
        self._codex_environment = environment_id
        self._codex_environment_checked_at = now
        return environment_id

    @staticmethod
    def _valid_codex_environment_id(value: Any) -> str | None:
        if not isinstance(value, str) or not value.startswith("env_e_"):
            return None
        suffix = value[len("env_e_"):]
        return value if suffix and suffix.isalnum() else None

    def _codex_environment_from_global_state(self) -> str | None:
        try:
            state = json.loads(self._codex_global_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict):
            return None
        return self._valid_codex_environment_id(state.get(self.CODEX_ENVIRONMENT_KEY))

    def _codex_environment_from_enrollments(self) -> str | None:
        newest: tuple[int, str] | None = None
        try:
            paths = tuple(self._codex_state_dir.glob("state_*.sqlite"))
        except OSError:
            return None
        for path in paths:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro",
                    uri=True,
                    timeout=0.1,
                )
                row = connection.execute(
                    "SELECT environment_id, updated_at "
                    "FROM remote_control_enrollments "
                    "WHERE remote_control_enabled = 1 "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
            except (OSError, sqlite3.Error):
                continue
            finally:
                if connection is not None:
                    connection.close()
            if not row:
                continue
            environment_id = self._valid_codex_environment_id(row[0])
            if environment_id is None:
                continue
            candidate = (int(row[1] or 0), environment_id)
            if newest is None or candidate[0] > newest[0]:
                newest = candidate
        return newest[1] if newest else None

    def _scan(self, session_id: str) -> str | None:
        url = self._from_registry(session_id)
        if url:
            return url
        import glob as _glob

        for root in self._roots:
            matches = _glob.glob(str(root / "**" / f"{session_id}.jsonl"), recursive=True)
            for path in matches:
                url = self._extract(Path(path))
                if url:
                    return url
        return None

    def _from_registry(self, session_id: str) -> str | None:
        def mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        try:
            entries = sorted(self._registry.glob("*.json"), key=mtime, reverse=True)
        except OSError:
            return None
        for path in entries:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("sessionId") != session_id:
                continue
            # The newest registry entry for the session decides; an older
            # pid's bridge may belong to an archived incarnation.
            bridge_id = record.get("bridgeSessionId")
            if isinstance(bridge_id, str):
                for prefix in ("session_", "cse_"):
                    if bridge_id.startswith(prefix):
                        return "https://claude.ai/code/session_" + bridge_id[len(prefix):]
            return None
        return None

    def _extract(self, path: Path) -> str | None:
        # The url rides a top-level `url` field on an early `system` record;
        # the bridge id a top-level `bridgeSessionId` on the bridge-session
        # record at line 1. Only top-level keys are trusted, so git output
        # that happens to quote such a URL in a tool result is ignored.
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for lineno, raw in enumerate(handle):
                    if lineno > 5000:
                        break
                    has_url = '"url"' in raw and "claude.ai/code/session_" in raw
                    if not has_url and '"bridgeSessionId"' not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    url = record.get("url")
                    if isinstance(url, str) and "claude.ai/code/session_" in url:
                        return url
                    bridge_id = record.get("bridgeSessionId")
                    if isinstance(bridge_id, str) and bridge_id.startswith("cse_"):
                        return "https://claude.ai/code/session_" + bridge_id[len("cse_"):]
        except OSError:
            return None
        return None


_DEEP_LINKS: DeepLinkResolver | None = None


APNS_PAYLOAD_LIMIT_BYTES = 4096


def shrink_payload(payload: dict[str, Any], limit: int = APNS_PAYLOAD_LIMIT_BYTES) -> dict[str, Any]:
    """Keep a push under the APNs size limit.

    An oversized payload is rejected outright (413) and the update is lost
    entirely, so deliver a trimmed one instead. Sheds the least useful
    content first: deep links, directories and tool details, then long
    names, and only then whole rows from the end (finished sessions sort
    last, so active work survives longest).
    """

    def encoded(value: dict[str, Any]) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode())

    if encoded(payload) <= limit:
        return payload

    trimmed = json.loads(json.dumps(payload))
    rows = trimmed.get("aps", {}).get("content-state", {}).get("agents")
    if not isinstance(rows, list):
        return trimmed

    for key in ("deepLink", "cwd", "detail"):
        if encoded(trimmed) <= limit:
            return trimmed
        for row in rows:
            row.pop(key, None)

    for cap in (80, 60, 40):
        if encoded(trimmed) <= limit:
            return trimmed
        for row in rows:
            name = row.get("name")
            if isinstance(name, str) and len(name) > cap:
                row["name"] = name[: cap - 1] + "\u2026"

    while rows and encoded(trimmed) > limit:
        rows.pop()
    return trimmed


def _attention_mode(mode: Any) -> Any:
    """Collapse busy sub-states that render as the same overall activity."""
    if mode in {"working", "tool_running", "long_task_progress"}:
        return "active"
    return mode


def _structure_signature(content_state: dict[str, Any]) -> tuple:
    """Changes that deserve an immediate, high-priority APNs update.

    Codex emits a PreToolUse/PostToolUse pair for almost every tool call. The
    resulting working/tool-running flip, row reordering, names, and details
    can wait for the coalesced cosmetic update. Session membership, active
    count, unread completion, and attention states still push immediately.
    """
    return (
        _attention_mode(content_state.get("aggregateMode")),
        content_state.get("activeCount"),
        tuple(sorted(
            (
                str(row.get("id", "")),
                _attention_mode(row.get("mode")),
                bool(row.get("unread")),
            )
            for row in content_state.get("agents", [])
        )),
    )


def status_row(status: AgentStatus) -> dict[str, Any]:
    row = {
        "id": status.agent_id,
        "name": _truncate(status.display_name.strip(), MAX_NAME_CHARS),
        "mode": status.mode.value,
        "detail": _truncate(status.tool_name, MAX_DETAIL_CHARS) if status.tool_name else None,
        "provider": status.provider,
        "cwd": _truncate(Path(status.cwd).name, MAX_DETAIL_CHARS) if status.cwd else None,
    }
    link = status.deep_link
    if not link and _DEEP_LINKS is not None:
        link = _DEEP_LINKS.link_for(status.provider, status.session_id)
    if link:
        row["deepLink"] = link
    return row


def build_content_state(
    statuses: list[AgentStatus],
    aggregate_mode: str,
    recent_finished: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wire format shared with AgentActivityAttributes.ContentState.

    Active sessions come first, then recently finished ones — either still
    reported as completed by the collector or remembered by the daemon after
    the session closed. ``activeCount`` counts only non-terminal sessions.
    """
    ordered = sorted(
        statuses,
        key=lambda status: (MODE_PRIORITY.get(status.mode, 99), -status.updated_at.timestamp()),
    )
    active_rows = [
        _ios_content_row(status_row(status))
        for status in ordered
        if status.mode.value not in TERMINAL_MODES
    ][:MAX_AGENT_ROWS]

    seen_ids = {row["id"] for row in active_rows}
    seen_names = {row["name"] for row in active_rows}
    finished_rows = []
    for saved_row in sorted(recent_finished or [], key=lambda r: -r.get("finishedAt", 0.0)):
        row = _ios_content_row(saved_row)
        if row["id"] in seen_ids or row["name"] in seen_names:
            continue
        seen_ids.add(row["id"])
        seen_names.add(row["name"])
        finished_rows.append(row)
        if len(finished_rows) >= MAX_FINISHED_ROWS:
            break

    return {
        "aggregateMode": aggregate_mode,
        "activeCount": sum(
            1 for status in statuses if status.mode.value not in TERMINAL_MODES
        ),
        "agents": active_rows + finished_rows,
        "updatedAt": round(time.time(), 1),
    }


def _has_unread_finished(content_state: dict[str, Any]) -> bool:
    """Whether the visible rows contain a finished item not yet opened."""
    rows = content_state.get("agents")
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict)
        and row.get("mode") == "completed"
        and row.get("unread") is True
        for row in rows
    )


def _normalize_dot_state(
    dot_state: str, content_state: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Turn the Dot off once there is no active or unread work left."""
    if content_state.get("activeCount") == 0 and not _has_unread_finished(
        content_state
    ):
        return "idle", {**content_state, "aggregateMode": "idle_ready"}
    return dot_state, content_state


def compute_alerts(
    previous_modes: dict[str, str],
    statuses: list[AgentStatus],
    now: float,
    last_alerts: dict[tuple[str, str], float],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Alerts for agents that TRANSITIONED into an alertable mode.

    Needs-input and blocked alert per agent, immediately. Finished alerts
    per SESSION GROUP: a main session reports completed while its subagents
    are still running, so "Finished" only fires once every member of the
    session is terminal.

    Returns (alerts, new_modes). ``last_alerts`` is mutated with sent
    timestamps so repeated flapping stays inside ALERT_COOLDOWN_SECONDS.
    An empty ``previous_modes`` produces no alerts — the first tick after a
    daemon restart must not replay every current state as news.
    """
    new_modes = {status.agent_id: status.mode.value for status in statuses}

    groups: dict[str, list[AgentStatus]] = {}
    for status in statuses:
        key = f"group:{status.provider}:{status.session_id or status.agent_id}"
        groups.setdefault(key, []).append(status)
    for group_key, members in groups.items():
        all_done = all(member.mode.value in TERMINAL_MODES for member in members)
        new_modes[group_key] = "completed" if all_done else "active"

    alerts: list[dict[str, str]] = []
    if not previous_modes:
        return alerts, new_modes

    def fire(
        key: tuple[str, str], title: str, body: str, thread_id: str, kind: str
    ) -> None:
        last_sent = last_alerts.get(key)
        if last_sent is not None and now - last_sent < ALERT_COOLDOWN_SECONDS:
            return
        last_alerts[key] = now
        alerts.append(
            {"title": title, "body": body, "thread_id": thread_id, "kind": kind}
        )

    for status in statuses:
        mode = status.mode.value
        if mode not in ("waiting_for_input", "blocked_error"):
            continue
        if previous_modes.get(status.agent_id) == mode:
            continue
        fire(
            (status.agent_id, mode),
            f"{ALERT_MODES[mode]}: {_truncate(status.display_name, MAX_NAME_CHARS)}",
            status.tool_name or status.message or status.mode_label,
            status.agent_id,
            mode,
        )

    for group_key, members in groups.items():
        if new_modes[group_key] != "completed":
            continue
        was_active = previous_modes.get(group_key) == "active" or any(
            previous_modes.get(member.agent_id) not in (None, *TERMINAL_MODES)
            for member in members
        )
        if not was_active:
            continue
        main = next(
            (member for member in members if ":session:" in member.agent_id),
            members[0],
        )
        fire(
            (group_key, "completed"),
            f"{ALERT_MODES['completed']}: {_truncate(main.display_name, MAX_NAME_CHARS)}",
            main.mode_label,
            group_key,
            "completed",
        )
    return alerts, new_modes


class APNsLiveActivityClient:
    """Minimal APNs client for liveactivity pushes (JWT auth, HTTP/2)."""

    def __init__(self, config: LiveActivityConfig) -> None:
        self.config = config
        self._jwt: str | None = None
        self._jwt_issued_at = 0.0
        self._client = None

    def _token(self) -> str:
        now = time.time()
        if self._jwt and now - self._jwt_issued_at < 50 * 60:
            return self._jwt
        import base64

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        def b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        private_key = serialization.load_pem_private_key(
            self.config.apns_key_path.read_bytes(), password=None
        )
        header = b64(json.dumps({"alg": "ES256", "kid": self.config.apns_key_id}).encode())
        claims = b64(json.dumps({"iss": self.config.apns_team_id, "iat": int(now)}).encode())
        signing_input = f"{header}.{claims}".encode()
        der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der_signature)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        self._jwt = f"{header}.{claims}.{b64(raw)}"
        self._jwt_issued_at = now
        return self._jwt

    def send(
        self,
        token: str,
        payload: dict[str, Any],
        priority: int = 10,
        push_type: str = "liveactivity",
        topic: str | None = None,
        expiration: int = 0,
        collapse_id: str | None = None,
    ) -> tuple[int, str]:
        import httpx

        url = f"https://{self.config.apns_host}/3/device/{token}"
        headers = {
            "authorization": f"bearer {self._token()}",
            "apns-topic": topic or self.config.liveactivity_topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
            "apns-expiration": str(expiration),
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id
        last_error = ""
        for attempt in range(3):
            if self._client is None:
                self._client = httpx.Client(http2=True, timeout=10.0)
            try:
                response = self._client.post(url, json=payload, headers=headers)
                return response.status_code, response.text
            except httpx.HTTPError as exc:
                # A half-dead HTTP/2 connection surfaces here (seen as
                # "[Errno 35] Resource temporarily unavailable"), and the
                # push was simply lost. Reconnect and try again.
                last_error = str(exc)
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        return 0, last_error


SUMMARY_MAX_CHARS = 90
SUMMARY_PROMPT_VERSION = 4
SUMMARY_FAILURE_BACKOFF_BASE_SECONDS = 60.0
SUMMARY_FAILURE_BACKOFF_MAX_SECONDS = 15 * 60.0
SUMMARY_PROGRESS_REFRESH_SECONDS = 45.0
GENERIC_WORKDIR_NAMES = {
    "android",
    "app",
    "apps",
    "git",
    "ios",
    "project",
    "projects",
    "src",
    "tmp",
    "workspace",
    "workspaces",
}
PROJECT_DISPLAY_NAMES = {
    "kleido": "Kleido",
    "side pulse": "SidePulse",
    "sidepulse": "SidePulse",
    "sidepulse feature": "SidePulse",
    "wardrobe app": "Kleido",
}
IOS_COMPACT_PROJECT_LABELS = {
    "cspennyscaler": "Trading",
    "cspennyscalpingtrader": "Trading",
}
CODEX_TRANSCRIPT_RECOVERY_BYTES = 2 * 1024 * 1024
CODEX_TRANSCRIPT_RECOVERY_LINES = 500
CODEX_EXEC_COMMAND = "tools.exec_command"


def _normalized_project_name(name: str) -> str:
    return " ".join(
        name.strip().replace("_", " ").replace("-", " ").split()
    ).casefold()


def _project_display_name(name: str) -> str:
    normalized = _normalized_project_name(name)
    return PROJECT_DISPLAY_NAMES.get(normalized, name.strip())


def _ios_content_row(row: dict[str, Any]) -> dict[str, Any]:
    mobile_row = dict(row)
    name = mobile_row.get("name")
    if not isinstance(name, str):
        return mobile_row
    project, separator, task = name.partition(": ")
    compact_project = IOS_COMPACT_PROJECT_LABELS.get(
        _normalized_project_name(project)
    )
    if separator and compact_project:
        mobile_row["name"] = f"{compact_project}: {task}"
    return mobile_row


def _project_name_from_cwd(cwd: str | None) -> str | None:
    """Return a repository name only when the path identifies one."""
    if not cwd:
        return None
    parts = Path(cwd).parts
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "git":
            candidate = parts[index + 1]
            if candidate.casefold() not in GENERIC_WORKDIR_NAMES:
                return _project_display_name(candidate)
    candidate = Path(cwd).name
    if candidate and candidate.casefold() not in GENERIC_WORKDIR_NAMES:
        return _project_display_name(candidate)
    return None


def _project_name_from_repo_workdir(workdir: Any) -> str | None:
    """Derive identity only from an existing repository working directory."""
    if not isinstance(workdir, str):
        return None
    path = Path(workdir)
    if not path.is_absolute():
        return None
    try:
        path = path.resolve()
        if not path.is_dir():
            return None
    except OSError:
        return None
    for candidate in (path, *path.parents):
        try:
            if (candidate / ".git").exists():
                return _project_display_name(candidate.name)
        except OSError:
            return None
    return None


def _request_text(prompt: str) -> str | None:
    """Drop harness envelopes while retaining the genuine user request."""
    return normalize_user_request(prompt)


def _js_string(source: str, start: int) -> tuple[str, int] | None:
    """Read one JavaScript string literal and return its value and end."""
    if start >= len(source) or source[start] not in {"'", '"', "`"}:
        return None
    quote = source[start]
    value: list[str] = []
    cursor = start + 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    while cursor < len(source):
        char = source[cursor]
        if char == quote:
            return "".join(value), cursor + 1
        if char == "\\" and cursor + 1 < len(source):
            cursor += 1
            escaped = source[cursor]
            if escaped == "u" and cursor + 4 < len(source):
                digits = source[cursor + 1:cursor + 5]
                try:
                    value.append(chr(int(digits, 16)))
                    cursor += 5
                    continue
                except ValueError:
                    pass
            value.append(escapes.get(escaped, escaped))
        else:
            value.append(char)
        cursor += 1
    return None


def _skip_js_space_and_comments(source: str, start: int) -> int:
    cursor = start
    while cursor < len(source):
        if source[cursor].isspace():
            cursor += 1
        elif source.startswith("//", cursor):
            newline = source.find("\n", cursor + 2)
            cursor = len(source) if newline < 0 else newline + 1
        elif source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            cursor = len(source) if end < 0 else end + 2
        else:
            break
    return cursor


def _top_level_workdir(source: str, start: int) -> tuple[str | None, int]:
    """Read the literal workdir property of one JavaScript object."""
    if start >= len(source) or source[start] != "{":
        return None, start
    depth = 1
    cursor = start + 1
    property_start = True
    while cursor < len(source) and depth:
        advanced = _skip_js_space_and_comments(source, cursor)
        if advanced != cursor:
            cursor = advanced
            continue
        if cursor >= len(source):
            break
        char = source[cursor]
        key: str | None = None
        key_end = cursor
        if depth == 1 and property_start and char in {"'", '"'}:
            parsed = _js_string(source, cursor)
            if parsed is None:
                break
            key, key_end = parsed
        elif depth == 1 and property_start and (char.isalpha() or char in {"_", "$"}):
            key_end += 1
            while key_end < len(source) and (
                source[key_end].isalnum() or source[key_end] in {"_", "$"}
            ):
                key_end += 1
            key = source[cursor:key_end]
        if key is not None:
            colon = _skip_js_space_and_comments(source, key_end)
            if colon < len(source) and source[colon] == ":":
                value_start = _skip_js_space_and_comments(source, colon + 1)
                if key == "workdir" and value_start < len(source) and source[value_start] in {"'", '"'}:
                    parsed_value = _js_string(source, value_start)
                    if parsed_value is not None:
                        return parsed_value[0], parsed_value[1]
                property_start = False
                cursor = colon + 1
                continue
        if char in {"'", '"', "`"}:
            parsed = _js_string(source, cursor)
            cursor = parsed[1] if parsed is not None else len(source)
            continue
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 1:
            property_start = True
        elif depth == 1 and not char.isspace():
            property_start = False
        cursor += 1
    return None, cursor


def _exec_command_workdirs(source: str) -> list[str]:
    """Extract only literal workdirs from actual tools.exec_command calls."""
    workdirs: list[str] = []
    cursor = 0
    while cursor < len(source):
        advanced = _skip_js_space_and_comments(source, cursor)
        if advanced != cursor:
            cursor = advanced
            continue
        if cursor >= len(source):
            break
        if source[cursor] in {"'", '"', "`"}:
            parsed = _js_string(source, cursor)
            cursor = parsed[1] if parsed is not None else len(source)
            continue
        if source.startswith(CODEX_EXEC_COMMAND, cursor):
            before_ok = cursor == 0 or not (
                source[cursor - 1].isalnum() or source[cursor - 1] in {"_", "$"}
            )
            call_start = _skip_js_space_and_comments(
                source, cursor + len(CODEX_EXEC_COMMAND)
            )
            if before_ok and call_start < len(source) and source[call_start] == "(":
                object_start = _skip_js_space_and_comments(source, call_start + 1)
                workdir, end = _top_level_workdir(source, object_start)
                if workdir is not None:
                    workdirs.append(workdir)
                cursor = max(cursor + 1, end)
                continue
        cursor += 1
    return workdirs


def _summary_cache_key(session_id: str, style: str) -> str:
    # Versioned because title semantics are user-visible and an old cached
    # phrase can otherwise survive a daemon upgrade indefinitely.
    return f"{session_id}|{style}|v{SUMMARY_PROMPT_VERSION}"


def _title_with_state(title: str, state: str) -> str:
    """Replace a title's state without truncating the new state away."""
    task = title.rsplit("; ", 1)[0].strip()
    if ": " in task:
        project, task_body = task.split(": ", 1)
        task = f"{_project_display_name(project)}: {task_body}"
    suffix = f"; {state}"
    return _truncate(task, max(12, SUMMARY_MAX_CHARS - len(suffix))) + suffix


class PromptTracker:
    """Latest user prompt per session, tailed from the hook logs.

    Sampling snapshot statuses misses UserPromptSubmit between ticks, and a
    session's display name carries only its FIRST prompt — useless for
    summarizing what a long session is doing now.
    """

    def __init__(self) -> None:
        self._prompts: dict[str, str] = {}
        self._actions: dict[str, list[str]] = {}
        self._projects: dict[str, str] = {}
        self._offsets: dict[str, int] = {}
        self._transcript_offsets: dict[str, int] = {}

    def prompt_for(self, session_id: str) -> str | None:
        return self._prompts.get(session_id)

    def actions_for(self, session_id: str) -> list[str]:
        return self._actions.get(session_id, [])

    def project_for(self, session_id: str, cwd: str | None = None) -> str | None:
        return _project_name_from_cwd(cwd) or self._projects.get(session_id)

    def trusted_context_for(self, session_id: str, cwd: str | None = None) -> str:
        if project := self.project_for(session_id, cwd):
            return f"repository observed for this session: {project}"
        return ""

    def _project_from_codex_transcript(self, transcript_path: Any) -> str | None:
        """Read only structured tool working directories from a Codex rollout.

        Desktop Codex hooks report the session's original cwd, which may be a
        generic workspace, while the actual exec call records its explicit
        ``workdir`` in the rollout. Messages, generated titles, command text,
        and tool output are deliberately ignored as identity sources.
        """
        if not isinstance(transcript_path, str) or not transcript_path:
            return None
        path = Path(transcript_path)
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return None
        first_read = key not in self._transcript_offsets
        offset = self._transcript_offsets.get(
            key, max(0, size - CODEX_TRANSCRIPT_RECOVERY_BYTES)
        )
        if size < offset:
            first_read = True
            offset = 0
        if size == offset:
            return None
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(size - offset)
        except OSError:
            return None
        self._transcript_offsets[key] = size
        lines = chunk.splitlines()
        if first_read and offset and lines:
            lines = lines[1:]
        lines = lines[-CODEX_TRANSCRIPT_RECOVERY_LINES:]

        latest: str | None = None
        for raw_line in lines:
            try:
                record = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            workdirs: list[str] = []
            if payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
                source = payload.get("input")
                if isinstance(source, str):
                    workdirs.extend(_exec_command_workdirs(source))
            elif payload.get("type") == "function_call" and payload.get("name") == "exec_command":
                arguments = payload.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}
                if isinstance(arguments, dict) and isinstance(arguments.get("workdir"), str):
                    workdirs.append(arguments["workdir"])
            projects = {
                project
                for workdir in workdirs
                if (project := _project_name_from_repo_workdir(workdir)) is not None
            }
            if len(projects) == 1:
                latest = next(iter(projects))
        return latest

    def poll(self) -> None:
        for name in ("claude.jsonl", "codex.jsonl"):
            path = default_state_dir() / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self._offsets.get(name, max(0, size - 8_388_608))
            if size < offset:
                offset = 0  # rotated/truncated
            if size == offset:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(size - offset)
            except OSError:
                continue
            self._offsets[name] = size
            for raw_line in chunk.splitlines():
                try:
                    record = json.loads(raw_line)
                except ValueError:
                    continue
                event = record.get("event", record)
                if not isinstance(event, dict):
                    continue
                session_id = event.get("session_id")
                if not isinstance(session_id, str):
                    continue
                # Codex subagents share the parent session id. Their commands
                # are separate work streams and must not race to retitle the
                # parent session or change its project identity.
                if event.get("agent_id"):
                    continue
                project = _project_name_from_cwd(event.get("cwd"))
                if project:
                    self._projects[session_id] = project
                elif name == "codex.jsonl":
                    tool_project = self._project_from_codex_transcript(
                        event.get("transcript_path")
                    )
                    if tool_project:
                        self._projects[session_id] = tool_project
                hook = event.get("hook_event_name")
                if hook == "UserPromptSubmit":
                    prompt = event.get("prompt")
                    if isinstance(prompt, str) and prompt.strip():
                        request = _request_text(prompt)
                        if request:
                            self._prompts[session_id] = request
                            self._actions[session_id] = []  # genuine new turn
                elif hook == "PreToolUse":
                    tool_input = event.get("tool_input")
                    description = None
                    if isinstance(tool_input, dict):
                        # Claude supplies a human description; Codex only the
                        # raw command — the summarizer reads either.
                        description = tool_input.get("description") or tool_input.get("command")
                    if isinstance(description, str) and description.strip():
                        actions = self._actions.setdefault(session_id, [])
                        actions.append(description.strip().splitlines()[0][:80])
                        del actions[:-4]



class SessionSummarizer:
    """Turns a session's last assistant message into a tiny outcome line
    ("TestFlight build deployed") via `claude -p` on a fast model.

    Runs the CLI without tools and with an isolated cwd whose path contains
    an ignored directory name and a private MOONSIDE_RUNTIME_DIR, so the
    summary sessions cannot mutate files and never appear in any monitor or
    on the lamp.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
        self._results: dict[str, tuple[str, str]] = {}  # session -> (source_hash, summary)
        self._requested_hashes: dict[str, str] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[str, str, str, str, str]]" = queue.Queue()
        self._failure_count = 0
        self._retry_after = 0.0
        base = default_state_dir() / "summarizer"
        # "memories" is on the ignored-directory list, hiding these runs
        # from every sidepulse consumer.
        self.workdir = base / "memories"
        self.moonside_dir = base / "moonside"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.moonside_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = base / "summaries.json"
        self._load_cache()
        for _ in range(2):
            threading.Thread(target=self._worker, daemon=True).start()

    def summary_for(
        self,
        session_id: str,
        message: str | None,
        context: str = "",
        style: str = "outcome",
    ) -> str | None:
        key = _summary_cache_key(session_id, style)
        if not message:
            with self._lock:
                cached = self._results.get(key)
                pending = key in self._pending
                requested_hash = self._requested_hashes.get(key)
            return (
                cached[1]
                if cached
                and not pending
                and (requested_hash is None or cached[0] == requested_hash)
                else None
            )
        source_hash = hashlib.sha256(
            f"{SUMMARY_PROMPT_VERSION}\0{context}\0{message}".encode()
        ).hexdigest()[:16]
        with self._lock:
            self._requested_hashes[key] = source_hash
            cached = self._results.get(key)
            if cached and cached[0] == source_hash:
                return cached[1]
            if key not in self._pending and time.monotonic() >= self._retry_after:
                self._pending.add(key)
                self._queue.put((key, source_hash, message, context, style))
            # A cached phrase for different source text is stale. Showing it
            # as the new state is worse than the deterministic fallback used
            # by the daemon while this request is in flight.
            return None

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self._cache_path.read_text())
            self._results = {
                str(sid): (str(pair[0]), str(pair[1]))
                for sid, pair in raw.items()
                if isinstance(pair, list) and len(pair) == 2
            }
        except (OSError, ValueError):
            pass

    def _save_cache(self) -> None:
        try:
            with self._lock:
                data = {sid: list(pair) for sid, pair in list(self._results.items())[-200:]}
            self._cache_path.write_text(json.dumps(data))
        except OSError:
            pass

    def _worker(self) -> None:
        while True:
            key, source_hash, message, context, style = self._queue.get()
            with self._lock:
                if time.monotonic() < self._retry_after:
                    self._pending.discard(key)
                    continue
            summary = self._generate(message, context, style)
            delay = self._record_generation_result(key, source_hash, summary)
            if delay:
                _log(f"summary generation paused for {delay:.0f}s after failure")
            if summary:
                self._save_cache()

    def _record_generation_result(
        self, key: str, source_hash: str, summary: str | None
    ) -> float:
        """Finish one job and arm one global backoff per failure wave."""
        now = time.monotonic()
        delay = 0.0
        with self._lock:
            self._pending.discard(key)
            if summary:
                self._results[key] = (source_hash, summary)
                self._failure_count = 0
                self._retry_after = 0.0
            elif now >= self._retry_after:
                self._failure_count += 1
                delay = min(
                    SUMMARY_FAILURE_BACKOFF_BASE_SECONDS
                    * (2 ** min(self._failure_count - 1, 4)),
                    SUMMARY_FAILURE_BACKOFF_MAX_SECONDS,
                )
                self._retry_after = now + delay
        return delay

    def _generate(self, message: str, context: str, style: str = "outcome") -> str | None:
        if style == "task":
            instruction = (
                "Write a compact session title body with two clauses. The first "
                "clause must preserve the overall task from Current request. The "
                "second must state the latest meaningful phase from Latest "
                "progress and Session state. Never replace the task with a "
                "low-level command. "
            )
        else:
            instruction = (
                "Write a compact session title body with two clauses. The first "
                "clause must identify the task from Current request. The second "
                "must state the latest outcome, blocker, or requested input from "
                "Latest result or blocker and Session state. "
            )
        prompt = (
            instruction
            + "Use the exact format `Task; latest state`, sentence case, at most "
            "twelve words total. Read through typos. Never invent work. Do not "
            "include or guess a project or product name; the caller adds a "
            "trusted label separately. No quotes or final period. Return only "
            "the title body.\n\n"
            f"Trusted session context: {context[:800]}\n\n"
            f"Content:\n{message[:3000]}"
        )
        env = dict(os.environ)
        env["MOONSIDE_RUNTIME_DIR"] = str(self.moonside_dir)
        # Under launchd the PATH lacks Homebrew, so the CLI's node-based
        # hooks fail noisily and slow the call down.
        env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "/usr/bin:/bin")
        try:
            result = subprocess.run(
                [
                    self.claude,
                    "-p",
                    "--model", self.model,
                    # Strip startup weight: no MCP servers, no hooks, no
                    # session persistence. Roughly halves the latency.
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--settings", '{"disableAllHooks":true}',
                    "--no-session-persistence",
                    # The complete source text is already in the prompt; the
                    # summary worker needs no filesystem or shell access.
                    "--tools", "",
                ],
                capture_output=True,
                text=True,
                input=prompt,
                timeout=120,
                cwd=self.workdir,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"summary generation failed: {exc}")
            return None
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip()).replace("\n", " ")
            _log(f"claude -p exited {result.returncode}: {detail[:120]}")
            return None
        line = result.stdout.strip().splitlines()
        # Models sometimes add a trailing period or stray spaces; row text
        # must be clean — it renders as a one-line title.
        text = line[0].strip().strip("\"'").rstrip(".").strip() if line else ""
        # Defensive boundary for a model that still emits a project prefix.
        # Project identity is supplied deterministically by the daemon.
        semicolon_at = text.find("; ")
        colon_at = text.find(": ")
        if colon_at >= 0 and (semicolon_at < 0 or colon_at < semicolon_at):
            text = text[colon_at + 2:].strip()
        else:
            trusted_project = (
                context.rsplit(": ", 1)[-1].strip() if ": " in context else ""
            )
            prefix = f"{trusted_project} — "
            if trusted_project and text.casefold().startswith(prefix.casefold()):
                text = text[len(prefix):].strip()
        if "; " not in text:
            _log("summary rejected: missing task/state separator")
            return None
        if not is_readable_session_title(text):
            _log("summary rejected: protocol or tool metadata")
            return None
        if text:
            _log(f"summary -> {text[:70]}")
            return _truncate(text, SUMMARY_MAX_CHARS)
        return None


class LiveActivityDaemon:
    def __init__(self, config: LiveActivityConfig, token_store: TokenStore | None = None) -> None:
        self.config = config
        self.tokens = token_store or TokenStore()
        self.apns = APNsLiveActivityClient(config)
        self.monitor = AgentMonitor.from_default_sources()
        self._condition = threading.Condition()
        self._latest: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_pushed_signature: tuple | None = None
        self._last_pushed_state: dict[str, Any] | None = None
        self._last_push_at = 0.0
        self._push_state_lock = threading.RLock()
        self._update_token_generation = 0
        self._retired_activity_ids: set[str] = set()
        self._last_push_state: str | None = None
        self._last_start_push_at = 0.0
        self._last_reconcile_nudge_at = 0.0
        self._last_reconcile_nudge_accepted: bool | None = None
        self._pushes_this_activity = 0
        self._last_dot_state: str | None = None
        self._last_dot_has_unread_finished: bool | None = None
        self._last_dot_issued_at = 0.0
        self._current_dot_state: str | None = None
        self._current_dot_content_state: dict[str, Any] | None = None
        self._dot_candidate_signature: tuple[str, bool] | None = None
        self._dot_candidate_since = 0.0
        self._pending_dot: PendingDotPush | None = None
        self._dot_lock = threading.RLock()
        self._last_dot_resync_at = 0.0
        self._last_dot_working_ack_at: float | None = None
        self._dot_streams: dict[str, int] = {}
        self._idle_since: float | None = None
        self._activity_live = False
        self._start_push_attempts = 0
        self._last_activity_report: dict[str, Any] = {}
        self._activity_recovery_path = (
            default_state_dir() / "live_activity_recovery.json"
        )
        self._load_activity_recovery_state()
        for token, metadata in self.tokens.entries("update").items():
            if metadata.get("activity_state") not in LIVE_ACTIVITY_NONLIVE_STATES:
                continue
            activity_id = str(metadata.get("activity_id", ""))
            if activity_id:
                self._retired_activity_ids.add(activity_id)
            self.tokens.drop("update", token)
        self._activity_live = bool(self.tokens.tokens("update"))
        self._agent_modes: dict[str, str] = {}
        self._last_alerts: dict[tuple[str, str], float] = {}
        self._last_rows: dict[str, dict[str, Any]] = {}
        self._recent_finished_lock = threading.RLock()
        self._recent_finished: dict[str, dict[str, Any]] = {}
        self._recent_finished_path = default_state_dir() / "recent_finished.json"
        self._load_recent_finished()
        self._bg_holding: set[str] = set()
        self.summarizer = (
            SessionSummarizer(config.summary_model) if config.summaries_enabled else None
        )
        self._prompt_tracker = PromptTracker()
        global _DEEP_LINKS
        _DEEP_LINKS = DeepLinkResolver()
        self._deferred_alerts: list[dict[str, Any]] = []
        self._task_sources: dict[str, tuple[str, str, float]] = {}
        self._settled_statuses: dict[str, AgentStatus] = {}
        self._published_summaries: dict[str, str] = {}

    # -- snapshot loop -------------------------------------------------

    def run(self) -> None:
        server = self._build_server()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        # Hooks broadcast every event to the local unix socket as they log
        # it; listening there turns the poll loop event-driven, so a state
        # change reaches the phone in about a second instead of up to a
        # full poll interval later. Polling remains as the fallback (and
        # the socket may be owned by a status bar on this machine).
        event_server = None
        try:
            event_server = HookEventServer(
                lambda _provider, _line: self._wake.set()
            )
            event_server.start()
            _log("hook event socket armed (instant ticks)")
        except OSError as exc:
            event_server = None
            _log(f"no event socket ({exc}); polling only")
        _log(
            f"serving on 0.0.0.0:{self.config.port}, "
            f"topic {self.config.liveactivity_topic}, tokens {self.tokens.summary()}"
        )
        try:
            while not self._stop.is_set():
                started = time.time()
                try:
                    self._tick()
                except Exception as exc:  # keep the loop alive
                    _log(f"tick failed: {exc}")
                elapsed = time.time() - started
                # A hook event wakes the loop immediately; otherwise poll.
                # Wait first, then clear: an event that arrived during the
                # tick returns instantly instead of being lost to the poll.
                self._wake.wait(max(0.2, self.config.poll_seconds - elapsed))
                self._wake.clear()
        finally:
            if event_server is not None:
                event_server.stop()
            server.shutdown()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._condition:
            self._condition.notify_all()

    def _tick(self) -> None:
        snapshot = self.monitor.snapshot(include_stale=False)
        now_ts = time.time()
        # Subagents (Task tool) surface as their own :agent: rows; they are
        # sub-work of a session and often orphan in long-task state, so they
        # duplicate the session row. Monitor sessions only.
        statuses = [s for s in snapshot.statuses if ":agent:" not in s.agent_id]
        self._sync_background_tasks(statuses, now_ts)
        if self.summarizer is not None:
            self._prompt_tracker.poll()
            statuses = [self._apply_summary(status) for status in statuses]
        with self._recent_finished_lock:
            self._remember_finished(statuses, now_ts)
            self._refresh_finished_summaries()
            recent_finished = [dict(row) for row in self._recent_finished.values()]
            has_recent_finished = bool(self._recent_finished)
            content_state = build_content_state(
                statuses,
                snapshot.aggregate.mode.value,
                recent_finished=recent_finished,
            )
            with self._condition:
                changed = self._meaningfully_changed(content_state)
                self._latest = content_state
                if changed:
                    self._condition.notify_all()

        now = time.time()
        active = content_state["activeCount"] > 0

        # Beat iOS to the eight-hour cap: end this activity ourselves, so the
        # start path below can put a fresh one in the island instead of
        # leaving a dead card on the Lock Screen and an empty island.
        age = self._activity_age(now)
        if age is not None and age >= ACTIVITY_MAX_AGE_SECONDS:
            self._end_stale_activity(f"activity is {age / 3600:.1f}h old")

        alerts, self._agent_modes = compute_alerts(
            self._agent_modes, statuses, now, self._last_alerts
        )
        ready_alerts = self._defer_finished_alerts(alerts, statuses, now)
        if ready_alerts and self.tokens.tokens("update"):
            # Alerting Live Activity update: buzzes and highlights the
            # activity without posting a separate notification banner.
            self._push_update(content_state, now, alert=ready_alerts[0])
        elif self.tokens.tokens("update"):
            # Compare against what was actually PUSHED, never against the
            # last computed state: a change skipped for rate limiting must
            # stay pending and go out on a later tick, not be forgotten
            # because the in-memory state already moved on.
            structural = _structure_signature(content_state) != self._last_pushed_signature
            cosmetic = not structural and self._differs_from_pushed(content_state)
            if structural and now - self._last_push_at >= PUSH_MIN_INTERVAL_SECONDS:
                # A structural change (mode, row set, unread, counts) —
                # deliver immediately at noticeable priority.
                self._push_update(content_state, now, important=True)
            elif cosmetic and now - self._last_push_at >= COSMETIC_PUSH_INTERVAL_SECONDS:
                # Text-only churn (summaries, tool names) coalesces quietly.
                self._push_update(content_state, now, important=False)
            elif now - self._last_push_at >= (
                PUSH_HEARTBEAT_SECONDS if active else IDLE_HEARTBEAT_SECONDS
            ):
                # Silent keep-alive against the stale-date, and the only
                # liveness probe there is; low priority.
                self._push_update(content_state, now, important=False)

        # The Dot plugged into the phone (DotStatusMirror in the iOS app):
        # while the app is in the background only a push can wake it to
        # rewrite LEDS.LED. Brief mode flaps coalesce, and the resulting
        # command remains pending until the phone confirms the file write.
        dot_state = display_state_for_mode(snapshot.aggregate.mode).value
        dot_state, dot_content_state = _normalize_dot_state(dot_state, content_state)
        self._observe_dot_state(dot_state, dot_content_state, now)
        self._send_pending_dot_if_due(now)

        if active:
            self._idle_since = None
        else:
            if self._idle_since is None:
                self._idle_since = now
            elif (
                self._activity_live
                and not has_recent_finished
                and now - self._idle_since >= self.config.idle_end_minutes * 60
            ):
                # Nothing active and nothing recently finished — safe to end.
                self._push_end(content_state, now)

        # Evidence over belief: only a registered update token proves an
        # activity is live on the phone. Keep asking (rate-limited) until one
        # arrives, whatever an earlier start push claimed. Finished rows earn
        # an island too: gating this on active work meant an activity that
        # died while the host idled stayed dead until new work began.
        if active or has_recent_finished:
            if self.tokens.tokens("update"):
                self._maybe_reconcile_stale_activity(now)
            else:
                self._maybe_push_to_start(content_state, now)

    def _sync_background_tasks(self, statuses, now: float) -> None:
        """Mirror held-open sessions onto the Moonside lamp markers.

        The collector already classifies a Stop that reports running
        background tasks as long-task progress (the harness includes the
        list in the hook payload), so every sidepulse consumer agrees by
        itself. Moonside has its own marker files whose Stop hook writes
        idle immediately; flip them to working while the harness holds the
        session open, and restore when it truly finishes.
        """
        holding_now: set[str] = set()
        for status in statuses:
            if status.provider != "claude" or not status.session_id:
                continue
            if status.mode.value == "long_task_progress" and status.event_name == "Stop":
                holding_now.add(status.session_id)

        for session_id in holding_now - self._bg_holding:
            self._moonside_marker(session_id, "working", expect=("idle", None))
            _log(f"{session_id[:8]} has background tasks; holding busy")
        for session_id in self._bg_holding - holding_now:
            # Finished or resumed; real hook writes win, this only cleans up
            # a marker still showing our flip.
            self._moonside_marker(session_id, "idle", expect=("working", "Stop"))
        self._bg_holding = holding_now

    def _moonside_marker(
        self, session_id: str, state: str, expect: tuple[str, str | None]
    ) -> None:
        """Flip line 1 of the Moonside session marker, only when it still
        looks the way we left it (or the way Stop left it)."""
        runtime_dir = Path(os.environ.get("MOONSIDE_RUNTIME_DIR", "/tmp"))
        marker = runtime_dir / "moonside_sessions" / session_id
        try:
            lines = marker.read_text().splitlines()
        except OSError:
            return
        if not lines or lines[0] != expect[0]:
            return
        if expect[1] is not None and (len(lines) < 2 or lines[1] != expect[1]):
            return
        lines[0] = state
        tmp = marker.with_name(f".{marker.name}.la")
        try:
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(marker)
        except OSError:
            tmp.unlink(missing_ok=True)

    def _apply_summary(self, status: AgentStatus) -> AgentStatus:
        """Show the stable task and its latest meaningful state."""
        from dataclasses import replace as dataclass_replace

        if status.provider not in {"claude", "codex"} or not status.session_id:
            return status
        prompt = self._prompt_tracker.prompt_for(status.session_id)
        settled = (
            status.mode.value in {"completed", "waiting_for_input", "blocked_error"}
            or (
                status.event_name in {"Stop", "SubagentStop", "SessionEnd"}
                and status.mode.value == "long_task_progress"
            )
        )
        trusted_context = self._prompt_tracker.trusted_context_for(
            status.session_id, status.cwd
        )
        if settled:
            self._settled_statuses[status.session_id] = status
            result = status.message or status.tool_name or status.mode_label
            source = (
                f"Current request:\n{prompt or '(request unavailable)'}\n\n"
                f"Latest result or blocker:\n{result}\n\n"
                f"Session state:\n{status.mode_label}"
            )
            summary = self.summarizer.summary_for(
                status.session_id, source, trusted_context
            )
        elif status.mode.value in {"working", "tool_running", "long_task_progress"}:
            self._settled_statuses.pop(status.session_id, None)
            # While working, summarize the CURRENT prompt (tracked from the
            # hook logs — the display name only ever carries the first one).
            # Without a tracked prompt, use the deterministic title fallback.
            if prompt:
                # Refresh the progress source at most every 45s so the
                # summary follows the work without hammering the API.
                import time as _time

                cached_source = self._task_sources.get(status.session_id)
                prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
                if (
                    cached_source
                    and cached_source[0] == prompt_hash
                    and _time.time() - cached_source[2]
                    < SUMMARY_PROGRESS_REFRESH_SECONDS
                ):
                    source = cached_source[1]
                else:
                    actions = self._prompt_tracker.actions_for(status.session_id)
                    source = f"Current request:\n{prompt[:1500]}"
                    if actions:
                        source += (
                            "\n\nLatest progress (latest last):\n- "
                            + "\n- ".join(actions)
                        )
                    source += f"\n\nSession state:\n{status.mode_label}"
                    self._task_sources[status.session_id] = (
                        prompt_hash,
                        source,
                        _time.time(),
                    )
                summary = self.summarizer.summary_for(
                    status.session_id, source, trusted_context, style="task"
                )
            else:
                summary = None
        else:
            return status
        if (
            not summary
            or "; " not in summary
            or not is_readable_session_title(summary)
        ):
            summary = self._fallback_summary(prompt, status)
        summary = self._summary_title(status.session_id, summary, status.cwd)
        if not is_readable_session_title(summary):
            summary = self._summary_title(
                status.session_id,
                self._fallback_summary(None, status),
                status.cwd,
            )
        self._publish_summary(status, summary)
        return dataclass_replace(status, display_name=summary)

    @staticmethod
    def _fallback_summary(prompt: str | None, status: AgentStatus) -> str:
        text = humanize_title_text(prompt) or ""
        if text:
            ends = [
                index
                for marker in (". ", "? ", "! ")
                if (index := text.find(marker)) >= 0
            ]
            if ends:
                text = text[:min(ends)]
        else:
            text = status.display_name.split(";", 1)[0].strip()
            if ": " in text:
                text = text.split(": ", 1)[1]
            text = humanize_title_text(text) or ""
        task = _truncate(text.rstrip(".?!") or "Current task", 56)
        if task:
            task = task[0].upper() + task[1:]

        detail = humanize_title_text(status.message) or ""
        if status.mode.value == "blocked_error":
            state = f"blocked by {_truncate(detail, 24)}" if detail else "blocked"
        elif status.mode.value == "waiting_for_input":
            state = "waiting for input"
        elif status.mode.value == "completed":
            state = "completed"
        elif status.mode.value == "long_task_progress":
            state = "background work running"
        else:
            state = "working"
        return f"{task}; {state}"

    def _summary_title(
        self, session_id: str, action: str, cwd: str | None = None
    ) -> str:
        project = self._prompt_tracker.project_for(session_id, cwd)
        prefix = f"{project}: " if project else ""
        if "; " not in action:
            return _truncate(prefix + action, SUMMARY_MAX_CHARS)
        if len(prefix) + len(action) <= SUMMARY_MAX_CHARS:
            return prefix + action
        task, state = action.rsplit("; ", 1)
        body_limit = max(24, SUMMARY_MAX_CHARS - len(prefix))
        state = _truncate(state, max(12, body_limit - 14))
        task_limit = max(12, body_limit - len(state) - 2)
        return _truncate(
            f"{prefix}{_truncate(task, task_limit)}; {state}",
            SUMMARY_MAX_CHARS,
        )

    def _refresh_finished_summaries(self) -> None:
        """Refresh rows that outlived the status used to queue their result."""
        changed = False
        for agent_id, row in self._recent_finished.items():
            name = row.get("name")
            if not isinstance(name, str) or not name.startswith("No project: "):
                continue
            session_id = agent_id.rsplit(":", 1)[-1]
            project = self._prompt_tracker.project_for(session_id)
            row["name"] = f"{project}: {name[12:]}" if project else name[12:]
            changed = True
        for session_id, status in tuple(self._settled_statuses.items()):
            row = self._recent_finished.get(status.agent_id)
            if row is None:
                self._settled_statuses.pop(session_id, None)
                continue
            summarized = self._apply_summary(status)
            if summarized.display_name == row.get("name"):
                continue
            row["name"] = summarized.display_name
            changed = True
        if changed:
            self._save_recent_finished()

    def _publish_summary(self, status: AgentStatus, summary: str) -> None:
        """Write the summary into the provider's hook log so every consumer
        — the local status bar and remote clients via the stream — titles
        the session with it."""
        if not is_readable_session_title(summary):
            return
        if self._published_summaries.get(status.session_id) == summary:
            return
        path = next(
            (s.path for s in self.monitor.sources if s.provider == status.provider),
            None,
        )
        if path is None:
            return
        payload: dict[str, Any] = {
            "hook_event_name": SUMMARY_EVENT_NAME,
            "session_id": status.session_id,
            "summary": summary,
        }
        if status.cwd:
            payload["cwd"] = status.cwd
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line: dict[str, Any] = {"logged_at": timestamp}
        if status.provider == "codex":
            line["event"] = payload
        else:
            line.update(payload)
        try:
            write_hook_line(path, line)
        except OSError:
            return
        self._published_summaries[status.session_id] = summary

    def _remember_finished(self, statuses: list[AgentStatus], now: float) -> None:
        current = {status.agent_id: status for status in statuses}

        # Sessions that vanished while doing something count as finished:
        # a closed session emits SessionEnd and drops out of the collector
        # before its completed state becomes visible anywhere.
        for agent_id, prev_mode in self._agent_modes.items():
            if agent_id in current or prev_mode in TERMINAL_MODES:
                continue
            row = self._last_rows.get(agent_id)
            if row:
                name = str(row.get("name") or "")
                if not is_readable_session_title(name):
                    name = self._finished_title_fallback(row)
                self._recent_finished[agent_id] = {
                    **row,
                    "name": _title_with_state(name, "completed"),
                    "mode": "completed",
                    "detail": None,
                    "finishedAt": now,
                    "unread": True,
                }

        for status in current.values():
            if status.mode.value == "completed":
                previous = self._recent_finished.get(status.agent_id, {})
                self._recent_finished[status.agent_id] = {
                    **status_row(status),
                    "detail": None,
                    "finishedAt": previous.get("finishedAt", now),
                    # Stays unread until the user opens it in the app.
                    "unread": previous.get("unread", True),
                }
            elif status.mode.value not in TERMINAL_MODES:
                # Reactivated: it is no longer "recently finished".
                self._recent_finished.pop(status.agent_id, None)

        # Always retain the newest MAX_FINISHED_ROWS finished sessions so the
        # list still shows "the last 3 are done" after everything wraps up
        # (e.g. overnight); expire only older ones past the window.
        keep = {
            agent_id
            for agent_id, _ in sorted(
                self._recent_finished.items(),
                key=lambda kv: -kv[1].get("finishedAt", 0.0),
            )[:MAX_FINISHED_ROWS]
        }
        for agent_id in list(self._recent_finished):
            if agent_id in keep:
                continue
            if now - self._recent_finished[agent_id].get("finishedAt", 0.0) > RECENT_FINISHED_SECONDS:
                del self._recent_finished[agent_id]

        # Remembered rows outlive the collector (and daemon restarts), so a
        # link that resolves late — or code that learned a new source —
        # still reaches rows stored before it existed. Misses are cheap:
        # the resolver TTL-caches them.
        if _DEEP_LINKS is not None:
            for agent_id, row in self._recent_finished.items():
                if not row.get("deepLink"):
                    provider = row.get("provider") or ""
                    prefix = f"{provider}:session:"
                    session_id = agent_id[len(prefix):] if agent_id.startswith(prefix) else None
                    link = _DEEP_LINKS.link_for(provider, session_id)
                    if link:
                        row["deepLink"] = link

        self._last_rows = {status.agent_id: status_row(status) for status in current.values()}
        self._save_recent_finished()

    def _load_recent_finished(self) -> None:
        # The last-3-finished ring must survive daemon restarts so the
        # morning-after "all done" view is there even after a reboot.
        try:
            raw = json.loads(self._recent_finished_path.read_text())
            if isinstance(raw, dict):
                self._recent_finished = {
                    str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
                }
                for row in self._recent_finished.values():
                    if row.get("mode") == "completed" and isinstance(row.get("name"), str):
                        name = row["name"]
                        if not is_readable_session_title(name):
                            name = self._finished_title_fallback(row)
                        row["name"] = _title_with_state(name, "completed")
        except (OSError, ValueError):
            pass

    @staticmethod
    def _finished_title_fallback(row: dict[str, Any]) -> str:
        provider = str(row.get("provider") or "Agent").strip().capitalize()
        task = f"{provider} task" if provider else "Agent task"
        cwd = row.get("cwd")
        project = _project_name_from_cwd(cwd if isinstance(cwd, str) else None)
        return f"{project}: {task}" if project else task

    def mark_finished_seen(
        self,
        agent_id: str,
        finished_at: float | int | None = None,
    ) -> bool:
        """The user opened this finished session in the app; stop
        highlighting it everywhere on the next push."""
        with self._recent_finished_lock:
            row = self._recent_finished.get(agent_id)
            if finished_at is not None and (
                row is None or row.get("finishedAt") != finished_at
            ):
                raise StaleCompletionError
            if not row or not row.get("unread"):
                return False

            row["unread"] = False
            self._save_recent_finished()
            marked_finished_at = row.get("finishedAt")

        # Do not wait for the next collector poll to make the daemon's
        # authoritative foreground snapshot agree with persisted state.
        with self._condition:
            latest = self._latest
            agents = latest.get("agents") if latest is not None else None
            if isinstance(agents, list):
                updated_agents = []
                snapshot_changed = False
                for visible in agents:
                    if (
                        isinstance(visible, dict)
                        and visible.get("id") == agent_id
                        and visible.get("mode") == "completed"
                        and visible.get("finishedAt") == marked_finished_at
                        and visible.get("unread") is not False
                    ):
                        visible = {**visible, "unread": False}
                        snapshot_changed = True
                    updated_agents.append(visible)
                if snapshot_changed:
                    self._latest = {
                        **latest,
                        "agents": updated_agents,
                        "updatedAt": round(time.time(), 1),
                    }
            self._condition.notify_all()
        return True

    def _save_recent_finished(self) -> None:
        try:
            self._recent_finished_path.parent.mkdir(parents=True, exist_ok=True)
            self._recent_finished_path.write_text(json.dumps(self._recent_finished))
        except OSError:
            pass

    def _differs_from_pushed(self, content_state: dict[str, Any]) -> bool:
        if self._last_pushed_state is None:
            return True
        old = {k: v for k, v in self._last_pushed_state.items() if k != "updatedAt"}
        new = {k: v for k, v in content_state.items() if k != "updatedAt"}
        return old != new

    def _meaningfully_changed(self, content_state: dict[str, Any]) -> bool:
        if self._latest is None:
            return True
        old = {k: v for k, v in self._latest.items() if k != "updatedAt"}
        new = {k: v for k, v in content_state.items() if k != "updatedAt"}
        return old != new

    # -- APNs ----------------------------------------------------------

    def _apns_fanout(
        self,
        kind: str,
        payload: dict[str, Any],
        priority: int = 10,
        push_type: str = "liveactivity",
        topic: str | None = None,
        expiration: int | None = None,
        collapse_id: str | None = None,
        target_tokens: list[str] | None = None,
    ) -> bool:
        payload = shrink_payload(payload)
        accepted = False
        for token in (
            target_tokens if target_tokens is not None else self.tokens.tokens(kind)
        ):
            options: dict[str, Any] = {
                "priority": priority,
                "push_type": push_type,
                "topic": topic,
            }
            if expiration is not None:
                options["expiration"] = expiration
            if collapse_id is not None:
                options["collapse_id"] = collapse_id
            status, body = self.apns.send(token, payload, **options)
            accepted = accepted or status == 200
            if kind == "update" and status == 200:
                self._pushes_this_activity += 1
            if status == 410 or (status == 400 and "BadDeviceToken" in body):
                _log(f"dropping dead {kind} token ({status})")
                self.tokens.drop(kind, token)
                if kind == "update" and not self.tokens.tokens("update"):
                    # The phone's activity is gone; allow a restart on the
                    # next tick (the floor still applies, so a flapping token
                    # cannot spawn a stack of activities).
                    self._activity_live = False
                    self._start_push_attempts = 0
                    self._save_activity_recovery_state()
            elif status != 200:
                _log(f"APNs {kind} push -> {status} {body[:120]}")
        return accepted

    def _defer_finished_alerts(
        self, alerts: list[dict[str, str]], statuses, now: float
    ) -> list[dict[str, str]]:
        """Hold Finished buzzes until the outcome summary exists, so the
        alert names what happened rather than quoting the stale prompt.
        Needs-input and blocked alerts stay immediate."""
        ready: list[dict[str, str]] = []
        for alert in alerts:
            session_id = alert["thread_id"].split(":")[-1]
            if (
                self.summarizer is None
                or alert["kind"] != "completed"
                or self.summarizer.summary_for(session_id, None, style="outcome")
            ):
                ready.append(alert)
            else:
                self._deferred_alerts.append(
                    {
                        **alert,
                        "session_id": session_id,
                        "cwd": next(
                            (status.cwd for status in statuses if status.session_id == session_id),
                            None,
                        ),
                        "deadline": now + FINISHED_ALERT_DEFER_SECONDS,
                    }
                )

        still_waiting = []
        for pending in self._deferred_alerts:
            summary = (
                self.summarizer.summary_for(pending["session_id"], None, style="outcome")
                if self.summarizer
                else None
            )
            if summary:
                summary = self._summary_title(
                    pending["session_id"], summary, pending.get("cwd")
                )
                ready.append(
                    {
                        "title": f"{ALERT_MODES['completed']}: {_truncate(summary, MAX_NAME_CHARS)}",
                        "body": pending["body"],
                        "thread_id": pending["thread_id"],
                        "kind": pending["kind"],
                    }
                )
            elif now >= pending["deadline"]:
                ready.append(
                    {
                        k: pending[k]
                        for k in ("title", "body", "thread_id", "kind")
                    }
                )
            else:
                still_waiting.append(pending)
        self._deferred_alerts = still_waiting
        return ready

    def _is_reset_echo(self, now: float) -> bool:
        """Is this reset our own dedup coming back at us?

        Starting an activity ends the previous one, and the app cannot tell
        that dismissal from a swipe-away, so it reports "no activity" about a
        second after every start push. Believing it kills the new activity.
        """
        return bool(self._last_start_push_at) and now - self._last_start_push_at < RESET_ECHO_SECONDS

    def _load_activity_recovery_state(self) -> None:
        """Restore start safety and the last client report across restarts."""
        try:
            raw = json.loads(self._activity_recovery_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        attempts = raw.get("start_push_attempts")
        if (
            isinstance(attempts, int)
            and not isinstance(attempts, bool)
            and 0 <= attempts <= MAX_UNANSWERED_START_PUSHES
        ):
            self._start_push_attempts = attempts
        for key, attribute in (
            ("last_start_push_at", "_last_start_push_at"),
            ("last_reconcile_nudge_at", "_last_reconcile_nudge_at"),
        ):
            value = raw.get(key)
            if (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and value >= 0
            ):
                setattr(self, attribute, float(value))
        accepted = raw.get("last_reconcile_nudge_accepted")
        if isinstance(accepted, bool):
            self._last_reconcile_nudge_accepted = accepted
        report = raw.get("last_activity_report")
        if isinstance(report, dict):
            self._last_activity_report = {
                key: value
                for key, value in report.items()
                if key
                in {
                    *LIVE_ACTIVITY_METADATA_KEYS,
                    "activity_id",
                    "activity_observed_at",
                    "device",
                    "device_id",
                    "reported_at",
                    "source",
                }
            }

    def _save_activity_recovery_state(self) -> None:
        with self._push_state_lock:
            data = {
                "start_push_attempts": self._start_push_attempts,
                "last_start_push_at": self._last_start_push_at,
                "last_reconcile_nudge_at": self._last_reconcile_nudge_at,
                "last_reconcile_nudge_accepted": self._last_reconcile_nudge_accepted,
                "last_activity_report": self._last_activity_report,
            }
            temporary = self._activity_recovery_path.with_name(
                f".{self._activity_recovery_path.name}.tmp"
            )
            try:
                self._activity_recovery_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(data, sort_keys=True))
                temporary.replace(self._activity_recovery_path)
            except OSError:
                temporary.unlink(missing_ok=True)

    def _record_activity_report(
        self,
        meta: dict[str, Any],
        source: str,
        *,
        persist_for_device: bool = False,
    ) -> None:
        evidence = {
            key: meta[key]
            for key in LIVE_ACTIVITY_METADATA_KEYS
            if key in meta
        }
        if not evidence:
            return
        moment = time.time()
        report = {
            **evidence,
            "activity_id": str(meta.get("activity_id", "")),
            "activity_observed_at": meta.get("activity_observed_at"),
            "device": str(meta.get("device", "")),
            "device_id": str(meta.get("device_id", "")),
            "reported_at": moment,
            "source": source,
        }
        with self._push_state_lock:
            self._last_activity_report = report
            if persist_for_device:
                device = report["device"]
                device_id = report["device_id"]
                values = {**evidence, "activity_status_at": moment}
                for kind in ("push_to_start", "device", "dot_device"):
                    entries = self.tokens.entries(kind)
                    for token, stored in entries.items():
                        if device_id and str(stored.get("device_id", "")) != device_id:
                            continue
                        if (
                            not device_id
                            and device
                            and str(stored.get("device", "")) != device
                        ):
                            continue
                        if not device_id and not device and len(entries) != 1:
                            continue
                        self.tokens.update_metadata(kind, token, values)
            self._save_activity_recovery_state()
        labels = []
        if "activity_state" in evidence:
            labels.append(f"state={evidence['activity_state']}")
        if "activities_enabled" in evidence:
            labels.append(
                "activities=" + ("on" if evidence["activities_enabled"] else "off")
            )
        if "frequent_pushes_enabled" in evidence:
            labels.append(
                "frequent="
                + ("on" if evidence["frequent_pushes_enabled"] else "off")
            )
        _log(
            f"{source} activity report from {report['device'] or 'unknown'}: "
            + ", ".join(labels)
        )

    def _activity_health(self, now: float | None = None) -> dict[str, Any]:
        """Concise evidence for ActivityKit state and bounded recovery."""
        moment = time.time() if now is None else now
        with self._push_state_lock:
            update_entries = self.tokens.entries("update")
            current = next(iter(update_entries.values()), {})
            report = self._last_activity_report
            current_state = current.get("activity_state")
            current_id = str(current.get("activity_id", ""))
            report_id = str(report.get("activity_id", ""))
            if current_state not in LIVE_ACTIVITY_STATES and (
                not update_entries or not report_id or report_id == current_id
            ):
                current_state = report.get("activity_state")
            if current_state not in LIVE_ACTIVITY_STATES:
                current_state = None

            def capability(key: str) -> bool | None:
                value = report.get(key)
                if isinstance(value, bool):
                    return value
                value = current.get(key)
                return value if isinstance(value, bool) else None

            report_time = report.get("reported_at")
            report_age = (
                max(0.0, round(moment - float(report_time), 1))
                if not isinstance(report_time, bool)
                and isinstance(report_time, (int, float))
                and math.isfinite(report_time)
                else None
            )
            live = bool(update_entries) and (
                current_state not in LIVE_ACTIVITY_NONLIVE_STATES
            )
            if live:
                recovery = "live"
            elif capability("activities_enabled") is False:
                recovery = "disabled"
            elif self._start_push_attempts >= MAX_UNANSWERED_START_PUSHES:
                recovery = (
                    "reconciling"
                    if self._last_reconcile_nudge_at >= self._last_start_push_at
                    and self._last_reconcile_nudge_at > 0
                    else "exhausted"
                )
            elif self._start_push_attempts:
                recovery = "awaiting_registration"
            else:
                recovery = "ready"
            return {
                "activityLive": live,
                "activityClientState": current_state,
                "activitiesEnabled": capability("activities_enabled"),
                "frequentPushesEnabled": capability(
                    "frequent_pushes_enabled"
                ),
                "activityReportAgeSeconds": report_age,
                "startPushAttempts": self._start_push_attempts,
                "startRecovery": recovery,
                "secondsSinceLastStartPush": (
                    max(0.0, round(moment - self._last_start_push_at, 1))
                    if self._last_start_push_at
                    else None
                ),
                "secondsSinceReconcileNudge": (
                    max(0.0, round(moment - self._last_reconcile_nudge_at, 1))
                    if self._last_reconcile_nudge_at
                    else None
                ),
                "reconcileNudgeAccepted": self._last_reconcile_nudge_accepted,
            }

    def _activities_can_start(self) -> bool:
        entries = self.tokens.entries("push_to_start")
        return any(
            metadata.get("activities_enabled") is not False
            for metadata in entries.values()
        )

    def _current_activity_report_is_fresh(self, now: float) -> bool:
        report = self._last_activity_report
        reported_at = report.get("reported_at")
        if (
            isinstance(reported_at, bool)
            or not isinstance(reported_at, (int, float))
            or not math.isfinite(reported_at)
            or now - reported_at >= ACTIVITY_REPORT_STALE_SECONDS
            or report.get("activity_state") not in {"active", "pending", "stale"}
        ):
            return False
        current = next(iter(self.tokens.entries("update").values()), {})
        current_device_id = current.get("device_id")
        report_device_id = report.get("device_id")
        if isinstance(current_device_id, str) and current_device_id:
            return current_device_id == report_device_id
        current_device = current.get("device")
        report_device = report.get("device")
        return (
            isinstance(current_device, str)
            and bool(current_device)
            and current_device == report_device
        )

    def _maybe_reconcile_stale_activity(self, now: float) -> bool:
        if self._current_activity_report_is_fresh(now) or (
            self._last_reconcile_nudge_at
            and now - self._last_reconcile_nudge_at
            < START_RECONCILE_RETRY_SECONDS
        ):
            return False
        return self._send_activity_reconcile_nudge(
            now, "activity lifecycle report is stale or missing"
        )

    def _activity_started_at(self, activity_id: str) -> float | None:
        """When this activity first registered, across token rotations."""
        if not activity_id:
            return None
        stamps = [
            meta.get("activity_started_at")
            for meta in self.tokens.entries("update").values()
            if meta.get("activity_id") == activity_id
        ]
        stamps = [stamp for stamp in stamps if isinstance(stamp, (int, float))]
        return min(stamps) if stamps else None

    def _activity_age(self, now: float) -> float | None:
        """Seconds since the live activity started, None while unknown.

        The stamp rides in the update token's metadata, so the clock survives
        daemon restarts as well as the token rotations iOS does mid-activity.
        """
        stamps = [
            meta.get("activity_started_at")
            for meta in self.tokens.entries("update").values()
        ]
        stamps = [stamp for stamp in stamps if isinstance(stamp, (int, float))]
        return now - min(stamps) if stamps else None

    def register_update_token(self, token: str, meta: dict[str, Any]) -> bool:
        """Accept the current activity token and hydrate a new owner next tick.

        A locally created activity begins with fallback content. Reusing the
        previous activity's pushed signature made the daemon believe that
        fallback was already current, so the phone could remain on
        ``All quiet`` until an unrelated change. Token rotations also leave
        only one valid update destination.
        """
        with self._push_state_lock:
            activity_id = str(meta.get("activity_id", ""))
            previous = self.tokens.entries("update")
            current_meta = next(iter(previous.values()), {})
            current_activity_id = str(current_meta.get("activity_id", ""))
            if meta.get("activity_state") in LIVE_ACTIVITY_NONLIVE_STATES:
                self._record_activity_report(
                    meta, "update", persist_for_device=True
                )
                if activity_id:
                    self._retired_activity_ids.add(activity_id)
                    while len(self._retired_activity_ids) > 32:
                        self._retired_activity_ids.pop()
                affects_current = (
                    not previous
                    or token in previous
                    or bool(activity_id and activity_id == current_activity_id)
                )
                if affects_current:
                    self._retire_update_tokens()
                    self._activity_live = False
                    self._start_push_attempts = 0
                    self._save_activity_recovery_state()
                    self._wake.set()
                _log(
                    "refusing terminal activity token "
                    f"{activity_id[:8] or 'unknown'} ({meta['activity_state']})"
                )
                return False
            incoming_observed_at = meta.get("activity_observed_at")
            current_observed_at = current_meta.get("activity_observed_at")
            incoming_token_observed_at = meta.get("token_observed_at")
            current_token_observed_at = current_meta.get("token_observed_at")
            if activity_id in self._retired_activity_ids or (
                activity_id
                and current_activity_id
                and activity_id != current_activity_id
                and (
                        isinstance(incoming_observed_at, (int, float))
                        and isinstance(current_observed_at, (int, float))
                        and incoming_observed_at < current_observed_at
                )
            ) or (
                activity_id
                and activity_id == current_activity_id
                and isinstance(incoming_token_observed_at, (int, float))
                and not isinstance(incoming_token_observed_at, bool)
                and isinstance(current_token_observed_at, (int, float))
                and not isinstance(current_token_observed_at, bool)
                and incoming_token_observed_at < current_token_observed_at
            ):
                _log(f"ignoring retired activity token {activity_id[:8]}")
                return False
            started = self._activity_started_at(activity_id)
            registered_meta = {
                **meta,
                "activity_started_at": started if started is not None else time.time(),
            }
            owner_changed = self.tokens.replace("update", token, registered_meta)
            if owner_changed:
                if current_activity_id and current_activity_id != activity_id:
                    self._retired_activity_ids.add(current_activity_id)
                    while len(self._retired_activity_ids) > 32:
                        self._retired_activity_ids.pop()
                previous_activity_ids = {
                    str(entry.get("activity_id", "")) for entry in previous.values()
                }
                if not previous or activity_id not in previous_activity_ids:
                    self._pushes_this_activity = 0
                self._update_token_generation += 1
                self._last_pushed_signature = None
                self._last_pushed_state = None
                self._last_push_at = 0.0
                self._wake.set()
            self._activity_live = True
            self._start_push_attempts = 0
            self._record_activity_report(meta, "update", persist_for_device=True)
        return True

    def register_push_to_start_token(self, token: str, meta: dict[str, Any]) -> bool:
        """Register future-start authority without touching a live activity."""
        changed = self.tokens.replace_for_device("push_to_start", token, meta)
        with self._push_state_lock:
            if changed:
                self._start_push_attempts = 0
            self._record_activity_report(
                meta, "push_to_start", persist_for_device=True
            )
        self._wake.set()
        return changed

    def reset_activity(
        self, activity_id: str = "", meta: dict[str, Any] | None = None
    ) -> bool:
        """Reset only the activity that reported its own dismissal."""
        with self._push_state_lock:
            current_entries = self.tokens.entries("update")
            current = next(iter(current_entries.values()), {})
            current_ids = {
                str(entry.get("activity_id", ""))
                for entry in current_entries.values()
            }
            if activity_id and current_ids and activity_id not in current_ids:
                _log(f"ignoring reset from non-current activity {activity_id[:8]}")
                return False
            state = meta.get("activity_state") if meta is not None else None
            if (
                not activity_id
                and current_ids
                and state in LIVE_ACTIVITY_NONLIVE_STATES
                and not (
                    len(current_entries) == 1
                    and self._unqualified_reset_matches_owner(meta or {}, current)
                )
            ):
                _log("ignoring stale or cross-device unqualified activity reset")
                return False
            if meta is not None:
                self._record_activity_report(
                    meta, "reset", persist_for_device=True
                )
            unqualified_absence_without_owner = (
                not activity_id
                and not current_entries
                and (state is None or state in LIVE_ACTIVITY_NONLIVE_STATES)
            )
            if unqualified_absence_without_owner:
                # A bare "none" cannot prove which unanswered start it refers
                # to. Reconcile nudges can report this repeatedly while iOS is
                # still processing a newer start; reopening the burst here
                # bypasses MAX_UNANSWERED_START_PUSHES and stacks activities.
                self._activity_live = False
                self._save_activity_recovery_state()
                self._wake.set()
                _log(
                    "client reports no identifiable activity; "
                    "preserving unanswered start safety"
                )
                return True
            if state in LIVE_ACTIVITY_NONLIVE_STATES:
                if activity_id:
                    self._retired_activity_ids.add(activity_id)
                self._retire_update_tokens()
                self._activity_live = False
                self._start_push_attempts = 0
                self._save_activity_recovery_state()
                self._wake.set()
                _log(f"client confirmed activity {state}; will restart")
                return True
            self._end_stale_activity("app reports no activity")
            return True

    @staticmethod
    def _unqualified_reset_matches_owner(
        reset_meta: dict[str, Any], current_meta: dict[str, Any]
    ) -> bool:
        """Only a newer same-device report may clear an unqualified ghost."""
        reset_device_id = reset_meta.get("device_id")
        current_device_id = current_meta.get("device_id")
        if not isinstance(reset_device_id, str) or not reset_device_id:
            return False
        if isinstance(current_device_id, str) and current_device_id:
            if reset_device_id != current_device_id:
                return False
        else:
            reset_device = reset_meta.get("device")
            current_device = current_meta.get("device")
            if (
                not isinstance(reset_device, str)
                or not reset_device
                or reset_device != current_device
            ):
                return False
        absence_at = reset_meta.get("activity_observed_at")
        if (
            isinstance(absence_at, bool)
            or not isinstance(absence_at, (int, float))
            or not math.isfinite(absence_at)
        ):
            return False
        current_times = []
        observed_at = current_meta.get("activity_observed_at")
        if (
            not isinstance(observed_at, bool)
            and isinstance(observed_at, (int, float))
            and math.isfinite(observed_at)
        ):
            current_times.append(float(observed_at))
        registered_at = current_meta.get("registered_at")
        if isinstance(registered_at, str):
            try:
                current_times.append(datetime.fromisoformat(registered_at).timestamp())
            except ValueError:
                pass
        return not current_times or max(current_times) <= float(absence_at)

    def _retire_update_tokens(self) -> None:
        """Prevent ended activity observers from reclaiming token ownership."""
        entries = self.tokens.entries("update")
        for meta in entries.values():
            activity_id = str(meta.get("activity_id", ""))
            if activity_id:
                self._retired_activity_ids.add(activity_id)
        while len(self._retired_activity_ids) > 32:
            self._retired_activity_ids.pop()
        self.tokens.clear("update")
        self._update_token_generation += 1
        self._last_pushed_signature = None
        self._last_pushed_state = None

    def _maybe_push_to_start(self, content_state: dict[str, Any], now: float) -> None:
        if not self.tokens.tokens("push_to_start"):
            return
        if not self._activities_can_start():
            return
        if self._start_push_attempts >= MAX_UNANSWERED_START_PUSHES:
            if now - self._last_start_push_at < START_PUSH_SAFE_RECOVERY_SECONDS:
                self._maybe_send_activity_reconcile_nudge(now)
                return
            # By now even the newest unconfirmed activity and its lingering
            # Lock Screen card have aged out. Reopen one bounded start burst.
            _log("unconfirmed start safety window elapsed; reopening recovery")
            self._start_push_attempts = 0
            self._save_activity_recovery_state()
        # First retry after the base cooldown, then doubling per attempt —
        # but never faster than the floor, even right after a reset.
        wait = max(
            START_PUSH_MIN_GAP_SECONDS,
            min(
                PUSH_TO_START_COOLDOWN_SECONDS * (2 ** max(0, self._start_push_attempts - 1)),
                PUSH_TO_START_MAX_BACKOFF_SECONDS,
            )
            if self._start_push_attempts
            else 0.0,
        )
        if now - self._last_start_push_at < wait:
            return
        self._last_start_push_at = now
        if self._start_push_attempts == 0:
            self._last_reconcile_nudge_accepted = None
        self._start_push_attempts += 1
        self._save_activity_recovery_state()
        aps: dict[str, Any] = {
            "timestamp": int(now),
            "event": "start",
            "content-state": content_state,
            "relevance-score": 100,
            "attributes-type": ATTRIBUTES_TYPE,
            "attributes": {"hostLabel": self.config.host_label},
        }
        if content_state["activeCount"]:
            aps["alert"] = {
                "title": f"Agents active on {self.config.host_label}",
                "body": f"{content_state['activeCount']} agent(s) running",
            }
        else:
            # Apple requires an alert for every push-to-start request. Keep
            # the repair quiet by omitting sound, but still provide truthful
            # text when only finished rows remain.
            aps["alert"] = {
                "title": f"SidePulse restored on {self.config.host_label}",
                "body": "Finished session status restored",
            }
        payload = {"aps": aps}
        _log("sending push-to-start")
        self._apns_fanout("push_to_start", payload)
        # _activity_live flips only when the phone registers the activity's
        # update token — a sent start push is not a started activity.

    def _maybe_send_activity_reconcile_nudge(self, now: float) -> bool:
        """Wake the ordinary app at a low cadence while starts are exhausted."""
        nudged_this_burst = (
            self._last_reconcile_nudge_at >= self._last_start_push_at
            and self._last_reconcile_nudge_at > 0
        )
        retry_after = (
            START_RECONCILE_RETRY_SECONDS
            if nudged_this_burst
            else START_RECONCILE_NUDGE_DELAY_SECONDS
        )
        previous = (
            self._last_reconcile_nudge_at
            if nudged_this_burst
            else self._last_start_push_at
        )
        if (
            not self.tokens.tokens("device")
            or now - previous < retry_after
        ):
            return False
        return self._send_activity_reconcile_nudge(
            now, "start attempts exhausted"
        )

    def _send_activity_reconcile_nudge(self, now: float, reason: str) -> bool:
        targets = self._activity_reconcile_targets()
        if not targets:
            return False
        self._last_reconcile_nudge_at = now
        self._last_reconcile_nudge_accepted = None
        self._save_activity_recovery_state()
        _log(f"{reason}; requesting app activity reconcile")
        accepted = self._apns_fanout(
            "device",
            {
                "aps": {"content-available": 1},
                "sidepulseAction": "reconcileLiveActivity",
            },
            priority=5,
            push_type="background",
            topic=self.config.bundle_id,
            expiration=int(now + START_RECONCILE_NUDGE_EXPIRY_SECONDS),
            collapse_id=START_RECONCILE_COLLAPSE_ID,
            target_tokens=targets,
        )
        self._last_reconcile_nudge_accepted = accepted
        self._save_activity_recovery_state()
        return accepted

    def _activity_reconcile_targets(self) -> list[str]:
        """Target the current activity's phone, or all phones if ownerless."""
        devices = self.tokens.entries("device")
        if not devices:
            return []
        updates = self.tokens.entries("update")
        if not updates:
            return list(devices)

        owner = next(iter(updates.values()))
        owner_id = owner.get("device_id")
        if isinstance(owner_id, str) and owner_id:
            exact = [
                token
                for token, meta in devices.items()
                if meta.get("device_id") == owner_id
            ]
            if exact:
                return exact

        owner_device = owner.get("device")
        if not isinstance(owner_device, str) or not owner_device:
            return []
        legacy_matches = [
            (token, meta)
            for token, meta in devices.items()
            if meta.get("device") == owner_device
        ]
        if len(legacy_matches) != 1:
            return []
        token, meta = legacy_matches[0]
        candidate_id = meta.get("device_id")
        if (
            isinstance(owner_id, str)
            and owner_id
            and isinstance(candidate_id, str)
            and candidate_id
            and candidate_id != owner_id
        ):
            return []
        return [token]

    def _push_update(
        self,
        content_state: dict[str, Any],
        now: float,
        alert: dict[str, str] | None = None,
        important: bool = True,
    ) -> None:
        with self._push_state_lock:
            token_generation = self._update_token_generation
            self._last_push_at = now
        aps: dict[str, Any] = {
            "timestamp": int(now),
            "event": "update",
            "content-state": content_state,
            # Highest relevance so that when a second Live Activity (e.g.
            # Now Playing) is active, iOS gives SidePulse the attached,
            # left-of-camera minimal slot rather than the detached circle.
            "relevance-score": 100,
            # While work is live, dim quickly if the daemon stops pushing;
            # once everything's settled the last state should stay crisp
            # (e.g. the overnight "all done" view), so widen the window.
            "stale-date": int(
                now + (STALE_AFTER_SECONDS if content_state.get("activeCount", 0) else 8 * 3600)
            ),
        }
        # Apple: priority 10 for updates people would notice (state changes,
        # alerts); 5 only for the silent heartbeat.
        priority = self._update_push_priority(alert=alert, important=important)
        if alert:
            _log(f"alerting update -> {alert['title']}")
            aps["alert"] = {
                "title": alert["title"],
                "body": alert["body"],
                "sound": ALERT_SOUNDS.get(alert.get("kind"), "default"),
            }
        accepted = self._apns_fanout("update", {"aps": aps}, priority=priority)
        with self._push_state_lock:
            if token_generation != self._update_token_generation:
                # A new activity registered while the old token's push was in
                # flight. Keep its hydration pending for the immediately
                # awakened next tick.
                self._wake.set()
                return
            if not accepted:
                # The signature remains pending, so the next eligible tick
                # retries instead of waiting for the heartbeat.
                return
            self._activity_live = True
            self._last_pushed_signature = _structure_signature(content_state)
            self._last_pushed_state = content_state

    def _update_push_priority(
        self, *, alert: dict[str, str] | None, important: bool
    ) -> int:
        """Respect the elected phone's explicitly reported push budget."""
        if alert:
            return 10
        if not important:
            return 5
        current = next(iter(self.tokens.entries("update").values()), {})
        return 5 if current.get("frequent_pushes_enabled") is False else 10

    def _queue_dot_state(
        self,
        dot_state: str,
        content_state: dict[str, Any],
        now: float,
        *,
        force: bool = False,
    ) -> bool:
        """Create one current Dot command; a newer state supersedes it."""
        has_unread_finished = _has_unread_finished(content_state)
        signature = (dot_state, has_unread_finished)
        with self._dot_lock:
            self._current_dot_state = dot_state
            self._current_dot_content_state = dict(content_state)
            if not force:
                if self._pending_dot is not None and (
                    self._pending_dot.state,
                    self._pending_dot.has_unread_finished,
                ) == signature:
                    return False
                if (
                    self._last_dot_state,
                    self._last_dot_has_unread_finished,
                ) == signature:
                    self._pending_dot = None
                    return False
            issued_at = max(now, self._last_dot_issued_at + 0.001)
            self._last_dot_issued_at = issued_at
            self._pending_dot = PendingDotPush(
                command_id=str(uuid4()),
                state=dot_state,
                has_unread_finished=has_unread_finished,
                content_state=dict(content_state),
                created_at=now,
                issued_at=issued_at,
                next_attempt_at=now,
            )
        return True

    def _dot_owner_availability(
        self, now: float
    ) -> tuple[str | None, float | None, str | None, bool]:
        """Return owner, active suppression, reason, and expiry transition."""
        entries = self.tokens.entries("dot_device")
        if not entries:
            return None, None, None, False
        token, meta = next(iter(entries.items()))
        unavailable_until = meta.get("dot_unavailable_until")
        valid_until = (
            not isinstance(unavailable_until, bool)
            and isinstance(unavailable_until, (int, float))
            and math.isfinite(unavailable_until)
        )
        reason = meta.get("dot_unavailable_reason")
        if (
            valid_until
            and unavailable_until > now
            and reason in DOT_UNAVAILABLE_REASONS
        ):
            return token, float(unavailable_until), reason, False
        return token, None, None, bool(valid_until and unavailable_until <= now)

    def _dot_owner_stream_count(self) -> int:
        entries = self.tokens.entries("dot_device")
        if not entries:
            return 0
        owner = next(iter(entries))
        return self._dot_streams.get(owner, 0)

    def _dot_stream_connected(self, token: str | None) -> bool:
        """Track only a bounded, exact match for the elected Dot owner."""
        if not token or len(token) > DOT_STREAM_TOKEN_MAX_CHARS:
            return False
        with self._dot_lock:
            entries = self.tokens.entries("dot_device")
            if not entries or next(iter(entries)) != token:
                return False
            self._dot_streams[token] = self._dot_streams.get(token, 0) + 1
        return True

    def _dot_stream_disconnected(self, token: str) -> None:
        with self._dot_lock:
            count = self._dot_streams.get(token, 0)
            if count <= 1:
                self._dot_streams.pop(token, None)
            else:
                self._dot_streams[token] = count - 1
        # A state may have settled while the foreground stream suppressed its
        # background push. Process that pending command immediately.
        self._wake.set()

    def report_dot_availability(
        self,
        token: str,
        available: bool,
        reason: str | None = None,
        retry_after_seconds: float | None = None,
        reported_at: float | None = None,
        dnd_schedule: DotDndSchedule | None = None,
        *,
        now: float | None = None,
        force_resync: bool = True,
    ) -> bool:
        """Persist a bounded suppression lease for the elected Dot owner."""
        moment = time.time() if now is None else now
        with self._dot_lock:
            metadata = self.tokens.entries("dot_device").get(token, {})
            was_unavailable = (
                metadata.get("dot_unavailable_reason") in DOT_UNAVAILABLE_REASONS
            )
            owner_matched, applied = self._record_dot_availability(
                token,
                available,
                reason,
                retry_after_seconds,
                reported_at,
                now=moment,
            )
            if owner_matched and dnd_schedule is not None:
                self._record_dot_dnd_schedule(
                    token,
                    dnd_schedule,
                    now=moment,
                )

            should_wake = False
            if owner_matched and applied and available and force_resync:
                current_applied = self._current_dot_content_state is not None and (
                    self._last_dot_state,
                    self._last_dot_has_unread_finished,
                ) == (
                    self._current_dot_state,
                    _has_unread_finished(self._current_dot_content_state),
                )
                if was_unavailable:
                    should_wake = self._rearm_current_dot(
                        moment, include_accepted=True
                    )
                elif not current_applied:
                    should_wake = self._rearm_current_dot(
                        moment, include_accepted=False
                    )
            if should_wake:
                self._wake.set()
        return owner_matched

    def _record_dot_availability(
        self,
        token: str,
        available: bool,
        reason: str | None,
        retry_after_seconds: float | None,
        reported_at: float | None,
        *,
        now: float | None,
    ) -> tuple[bool, bool]:
        moment = time.time() if now is None else now
        ordered_at = (
            reported_at
            if reported_at is None
            or reported_at <= moment + DOT_REPORTED_AT_MAX_FUTURE_SECONDS
            else None
        )
        with self._dot_lock:
            owner, _, _, _ = self._dot_owner_availability(moment)
            if owner != token:
                return False, False
            metadata = self.tokens.entries("dot_device").get(token)
            if metadata is None:
                return False, False
            focus_reported_at = metadata.get("dot_focus_reported_at")
            focus_active = metadata.get("dot_focus_active")
            availability_focus = (
                False
                if available
                else True
                if reason == "focus"
                else None
            )
            if (
                isinstance(focus_active, bool)
                and not isinstance(focus_reported_at, bool)
                and isinstance(focus_reported_at, (int, float))
                and math.isfinite(focus_reported_at)
                and availability_focus is not None
                and availability_focus != focus_active
                and (ordered_at is None or ordered_at <= focus_reported_at)
            ):
                # Focus Intents and main-app availability reports travel on
                # independent requests. A delayed report must not undo the
                # newer Focus generation in either direction.
                return True, False
            previous_client_time = metadata.get("dot_client_reported_at")
            if (
                ordered_at is not None
                and not isinstance(previous_client_time, bool)
                and isinstance(previous_client_time, (int, float))
                and math.isfinite(previous_client_time)
                and ordered_at < previous_client_time
            ):
                return True, False
            values: dict[str, Any] = {"dot_status_at": moment}
            if ordered_at is not None:
                values["dot_client_reported_at"] = ordered_at
            if available:
                values.update(
                    {
                        "dot_unavailable_until": None,
                        "dot_unavailable_reason": None,
                    }
                )
            else:
                lease_seconds = max(
                    DOT_UNAVAILABLE_MIN_SECONDS,
                    min(
                        float(retry_after_seconds or DOT_UNAVAILABLE_MIN_SECONDS),
                        DOT_UNAVAILABLE_MAX_SECONDS,
                    ),
                )
                values.update(
                    {
                        "dot_unavailable_until": moment + lease_seconds,
                        "dot_unavailable_reason": reason,
                    }
                )
            if not self.tokens.update_metadata("dot_device", token, values):
                return False, False
        return True, True

    def _record_dot_dnd_schedule(
        self,
        token: str,
        schedule: DotDndSchedule,
        *,
        now: float | None,
    ) -> tuple[bool, bool]:
        moment = time.time() if now is None else now
        with self._dot_lock:
            entries = self.tokens.entries("dot_device")
            if not entries or next(iter(entries)) != token:
                return False, False
            metadata = entries[token]
            previous = metadata.get("dot_schedule_reported_at")
            if (
                isinstance(previous, (int, float))
                and not isinstance(previous, bool)
                and math.isfinite(previous)
                and schedule.reported_at <= previous
            ):
                return True, False
            values: dict[str, Any] = {
                "dot_status_at": moment,
                "dot_dnd_schedule_enabled": schedule.enabled,
                "dot_next_dnd_transition_at": schedule.next_transition_at,
                "dot_next_dnd_transition_enabled": schedule.next_transition_enabled,
                "dot_schedule_reported_at": schedule.reported_at,
            }
            return True, self.tokens.update_metadata("dot_device", token, values)

    def _rearm_current_dot(self, now: float, *, include_accepted: bool) -> bool:
        """Reuse a viable command; allocate a new id only when none can run."""
        dot_state = self._current_dot_state
        content_state = self._current_dot_content_state
        if dot_state is None or content_state is None:
            return False
        signature = (dot_state, _has_unread_finished(content_state))
        pending = self._pending_dot
        if (
            pending is not None
            and (pending.state, pending.has_unread_finished) == signature
            and now < pending.created_at + DOT_PUSH_EXPIRY_SECONDS
            and pending.accepted_attempts < len(DOT_PUSH_RETRY_OFFSETS_SECONDS)
        ):
            if include_accepted or pending.accepted_attempts == 0:
                pending.next_attempt_at = now
                return True
            return False
        return self._queue_dot_state(dot_state, content_state, now, force=True)

    def _apply_due_dot_dnd_transition(self, now: float) -> bool:
        """Consume one persisted DND boundary and queue a current-state wake."""
        with self._dot_lock:
            entries = self.tokens.entries("dot_device")
            if not entries:
                return False
            token, metadata = next(iter(entries.items()))
            transition_at = metadata.get("dot_next_dnd_transition_at")
            transition_enabled = metadata.get("dot_next_dnd_transition_enabled")
            if (
                metadata.get("dot_dnd_schedule_enabled") is not True
                or isinstance(transition_at, bool)
                or not isinstance(transition_at, (int, float))
                or not math.isfinite(transition_at)
                or transition_at > now
                or not isinstance(transition_enabled, bool)
                or self._current_dot_state is None
                or self._current_dot_content_state is None
            ):
                return False
            values: dict[str, Any] = {
                "dot_next_dnd_transition_at": None,
                "dot_next_dnd_transition_enabled": None,
            }
            if (
                transition_enabled is False
                and metadata.get("dot_unavailable_reason") == "dnd"
            ):
                values.update(
                    {
                        "dot_unavailable_until": None,
                        "dot_unavailable_reason": None,
                    }
                )
            if not self.tokens.update_metadata("dot_device", token, values):
                return False
            queued = self.request_dot_resync(now=now, force=True)
            if queued:
                self._wake.set()
            return queued

    def report_dot_focus(
        self,
        token: str,
        focused: bool,
        reported_at: float,
        *,
        now: float | None = None,
    ) -> tuple[bool, bool]:
        """Persist an ordered owner Focus report and queue its LED refresh."""
        moment = time.time() if now is None else now
        with self._dot_lock:
            entries = self.tokens.entries("dot_device")
            if not entries or next(iter(entries)) != token:
                return False, False
            metadata = entries[token]
            previous_time = metadata.get("dot_focus_reported_at")
            if (
                isinstance(previous_time, (int, float))
                and not isinstance(previous_time, bool)
                and math.isfinite(previous_time)
                and reported_at <= previous_time
            ):
                return True, False
            owner, unavailable_until, reason, _ = self._dot_owner_availability(moment)
            values: dict[str, Any] = {
                "dot_status_at": moment,
                "dot_focus_active": focused,
                "dot_focus_reported_at": reported_at,
            }
            if not focused and reason == "focus":
                values.update(
                    {
                        "dot_unavailable_until": None,
                        "dot_unavailable_reason": None,
                    }
                )
            if not self.tokens.update_metadata("dot_device", token, values):
                return False, False

            another_suppression = (
                owner is not None
                and unavailable_until is not None
                and reason not in (None, "focus")
            )
            should_force = (focused and not another_suppression) or not focused
            if should_force and self.request_dot_resync(now=moment, force=True):
                self._wake.set()
            return True, True

    def _observe_dot_state(
        self, dot_state: str, content_state: dict[str, Any], now: float
    ) -> bool:
        """Coalesce brief state flaps before consuming a silent-push slot."""
        signature = (dot_state, _has_unread_finished(content_state))
        with self._dot_lock:
            previous = self._dot_candidate_signature
            self._current_dot_state = dot_state
            self._current_dot_content_state = dict(content_state)
            if previous != signature:
                self._dot_candidate_signature = signature
                self._dot_candidate_since = now
                if self._pending_dot is not None and (
                    self._pending_dot.state,
                    self._pending_dot.has_unread_finished,
                ) != signature:
                    self._pending_dot = None
            unread_changed = (
                previous is not None and previous[1] != signature[1]
            ) or (previous is None and signature[1])
            settled = (
                dot_state in {"ask", "done"}
                or unread_changed
                or now - self._dot_candidate_since >= DOT_STATE_SETTLE_SECONDS
            )
        if not settled:
            return False
        return self._queue_dot_state(dot_state, content_state, now)

    def request_dot_resync(
        self, now: float | None = None, *, force: bool = False
    ) -> bool:
        """A registering phone asks for the current command even if unchanged."""
        moment = time.time() if now is None else now
        with self._dot_lock:
            dot_state = self._current_dot_state
            content_state = (
                dict(self._current_dot_content_state)
                if self._current_dot_content_state is not None
                else None
            )
            pending = self._pending_dot
            signature = (
                dot_state,
                _has_unread_finished(content_state) if content_state is not None else False,
            )
            if (
                not force
                and moment - self._last_dot_resync_at < DOT_RESYNC_COOLDOWN_SECONDS
            ):
                return False
            self._last_dot_resync_at = moment
            if (
                not force
                and pending is not None
                and (pending.state, pending.has_unread_finished) == signature
                and pending.accepted_attempts < len(DOT_PUSH_RETRY_OFFSETS_SECONDS)
                and moment < pending.created_at + DOT_PUSH_EXPIRY_SECONDS
            ):
                return False
        if dot_state is None or content_state is None:
            return False
        return self._queue_dot_state(dot_state, content_state, moment, force=True)

    def ack_dot(
        self,
        command_id: str,
        status: str,
        availability: tuple[
            bool, str | None, float | None, float | None
        ] | None = None,
        dnd_schedule: DotDndSchedule | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        """Commit delivery only after iOS handled the matching LED command."""
        with self._dot_lock:
            pending = self._pending_dot
            if pending is None or pending.command_id != command_id:
                return False
            availability_applied = False
            if availability is not None:
                owner, _, _, _ = self._dot_owner_availability(
                    time.time() if now is None else now
                )
                if owner is not None:
                    _, availability_applied = self._record_dot_availability(
                        owner,
                        availability[0],
                        availability[1],
                        availability[2],
                        availability[3],
                        now=now,
                    )
            if dnd_schedule is not None:
                owner, _, _, _ = self._dot_owner_availability(
                    time.time() if now is None else now
                )
                if owner is not None:
                    self._record_dot_dnd_schedule(
                        owner,
                        dnd_schedule,
                        now=now,
                    )
            delivered = status in DOT_ACK_SUCCESS_STATUSES and (
                availability is None
                or not availability_applied
                or availability[0]
            )
            if not delivered:
                state = pending.state
            else:
                self._last_dot_state = pending.state
                self._last_dot_has_unread_finished = pending.has_unread_finished
                if pending.state == "working":
                    self._last_dot_working_ack_at = (
                        time.time() if now is None else now
                    )
                else:
                    # A later working transition must settle and earn its own
                    # ACK before periodic refreshes can begin again.
                    self._last_dot_working_ack_at = None
                self._pending_dot = None
                state = pending.state
        if delivered:
            _log(f"dot ack <- {state} ({status})")
            return True
        _log(f"dot ack <- {state} ({status}); still pending")
        return False

    def _send_pending_dot_if_due(self, now: float) -> bool:
        """Send at most three accepted copies over the command's one-hour TTL."""
        with self._dot_lock:
            self._apply_due_dot_dnd_transition(now)
            owner, unavailable_until, _, lease_expired = (
                self._dot_owner_availability(now)
            )
            if owner is None:
                return False
            if lease_expired:
                if not self.tokens.update_metadata(
                    "dot_device",
                    owner,
                    {
                        "dot_unavailable_until": None,
                        "dot_unavailable_reason": None,
                    },
                ):
                    return False
                self.request_dot_resync(now=now, force=True)
            if self._dot_owner_stream_count() > 0 or unavailable_until is not None:
                return False
            if (
                # A maxed-out or expired command deliberately remains pending:
                # a state/resync event may replace it, but ticks must not mint
                # fresh UUIDs and silently exceed the background-push budget.
                self._pending_dot is None
                and self._current_dot_state == "working"
                and self._current_dot_content_state is not None
                and self._last_dot_working_ack_at is not None
                and now
                >= self._last_dot_working_ack_at + DOT_WORKING_REFRESH_SECONDS
            ):
                self._queue_dot_state(
                    "working",
                    self._current_dot_content_state,
                    now,
                    force=True,
                )
            pending = self._pending_dot
            if (
                pending is None
                or now >= pending.created_at + DOT_PUSH_EXPIRY_SECONDS
                or pending.accepted_attempts >= len(DOT_PUSH_RETRY_OFFSETS_SECONDS)
                or now < pending.next_attempt_at
            ):
                return False
            payload = {
                "aps": {"content-available": 1},
                "dot": {
                    "aggregateMode": pending.content_state["aggregateMode"],
                    "activeCount": pending.content_state["activeCount"],
                    "hasUnreadFinished": pending.has_unread_finished,
                    "host": self.config.host_label,
                    "updatedAt": pending.content_state["updatedAt"],
                    "commandID": pending.command_id,
                    "issuedAt": pending.issued_at,
                },
            }
            command_id = pending.command_id
            state = pending.state
            attempt = pending.accepted_attempts + 1
            expiration = int(pending.created_at + DOT_PUSH_EXPIRY_SECONDS)

        _log(
            f"dot -> {state} (attempt {attempt}/{len(DOT_PUSH_RETRY_OFFSETS_SECONDS)}, "
            f"command {command_id[:8]})"
        )
        accepted = self._apns_fanout(
            "dot_device",
            payload,
            priority=5,
            push_type="background",
            topic=self.config.bundle_id,
            expiration=expiration,
            collapse_id=DOT_COLLAPSE_ID,
        )
        with self._dot_lock:
            if self._pending_dot is not pending:
                return accepted
            if accepted:
                pending.accepted_attempts += 1
                pending.rejected_attempts = 0
                if pending.accepted_attempts < len(DOT_PUSH_RETRY_OFFSETS_SECONDS):
                    previous_offset = DOT_PUSH_RETRY_OFFSETS_SECONDS[
                        pending.accepted_attempts - 1
                    ]
                    next_offset = DOT_PUSH_RETRY_OFFSETS_SECONDS[
                        pending.accepted_attempts
                    ]
                    pending.next_attempt_at = min(
                        now + next_offset - previous_offset,
                        pending.created_at + DOT_PUSH_EXPIRY_SECONDS,
                    )
                else:
                    pending.next_attempt_at = float("inf")
            else:
                pending.rejected_attempts += 1
                delay = min(
                    DOT_PUSH_FAILURE_RETRY_SECONDS
                    * (2 ** min(pending.rejected_attempts - 1, 8)),
                    DOT_PUSH_FAILURE_MAX_RETRY_SECONDS,
                )
                pending.next_attempt_at = min(
                    now + delay,
                    pending.created_at + DOT_PUSH_EXPIRY_SECONDS,
                )
        return accepted

    def _dot_health(self, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        with self._dot_lock:
            owner, unavailable_until, reason, _ = self._dot_owner_availability(moment)
            pending = self._pending_dot
            return {
                "dotOutputAvailable": owner is not None and unavailable_until is None,
                "dotUnavailableReason": (
                    "no_device" if owner is None else reason
                ),
                "dotRetryAfterSeconds": (
                    max(0, math.ceil(unavailable_until - moment))
                    if unavailable_until is not None
                    else 0
                ),
                "dotState": self._last_dot_state,
                "dotPendingState": pending.state if pending is not None else None,
                "dotPendingAttempts": (
                    pending.accepted_attempts if pending is not None else 0
                ),
                "dotPendingRejected": (
                    pending.rejected_attempts if pending is not None else 0
                ),
                "dotPendingCommand": (
                    pending.command_id[:8] if pending is not None else None
                ),
                "dotForegroundStreams": self._dot_owner_stream_count(),
                "dotFocusActive": (
                    self.tokens.entries("dot_device")
                    .get(owner or "", {})
                    .get("dot_focus_active")
                    is True
                ),
            }

    def _end_stale_activity(self, reason: str) -> None:
        with self._push_state_lock:
            with self._condition:
                latest = self._latest
            if self.tokens.tokens("update"):
                _log(f"{reason}; ending stale activity")
                payload = {
                    "aps": {
                        "timestamp": int(time.time()),
                        "event": "end",
                        "dismissal-date": int(time.time()),
                        "content-state": latest or {
                            "aggregateMode": "idle_ready",
                            "activeCount": 0,
                            "agents": [],
                            "updatedAt": round(time.time(), 1),
                        },
                    }
                }
                self._apns_fanout("update", payload)
            else:
                _log(f"{reason}; will restart")
            self._retire_update_tokens()
            self._activity_live = False
            self._start_push_attempts = 0
            self._save_activity_recovery_state()

    def _push_end(self, content_state: dict[str, Any], now: float) -> None:
        with self._push_state_lock:
            payload = {
                "aps": {
                    "timestamp": int(now),
                    "event": "end",
                    "content-state": content_state,
                    "dismissal-date": int(now),
                }
            }
            _log("ending activity (idle)")
            self._apns_fanout("update", payload)
            self._retire_update_tokens()
            self._activity_live = False
            self._last_push_state = None

    # -- HTTP ----------------------------------------------------------

    def _build_server(self) -> ThreadingHTTPServer:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _json(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path == "/health":
                    self._json(
                        200,
                        {
                            "ok": True,
                            "tokens": daemon.tokens.summary(),
                            # What a vanished island needs answered: how old
                            # the activity is, how long since iOS last heard
                            # from us (against STALE_AFTER_SECONDS), and how
                            # hard it has been pushed (against the budget).
                            "activityAgeMinutes": (
                                round(age / 60, 1)
                                if (age := daemon._activity_age(time.time())) is not None
                                else None
                            ),
                            "secondsSinceLastPush": (
                                round(time.time() - daemon._last_push_at, 1)
                                if daemon._last_push_at
                                else None
                            ),
                            "pushesThisActivity": daemon._pushes_this_activity,
                            **daemon._activity_health(),
                            **daemon._dot_health(),
                            # The app needs this to label an activity it
                            # starts itself; attributes are fixed at creation.
                            "hostLabel": daemon.config.host_label,
                        },
                    )
                elif parsed.path == "/snapshot":
                    with daemon._condition:
                        latest = daemon._latest
                    self._json(200, latest or {})
                elif parsed.path == "/stream":
                    self._stream(parsed.query)
                else:
                    self._json(404, {"error": "not found"})

            def _stream(self, query: str) -> None:
                try:
                    values = parse_qs(
                        query,
                        keep_blank_values=True,
                        max_num_fields=4,
                    ).get("dotToken", [])
                except ValueError:
                    values = []
                token = values[0] if len(values) == 1 else None
                tracked = daemon._dot_stream_connected(token)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    last_sent: object = object()
                    while not daemon._stop.is_set():
                        with daemon._condition:
                            if daemon._latest is last_sent:
                                daemon._condition.wait_for(
                                    lambda: (
                                        daemon._stop.is_set()
                                        or daemon._latest is not last_sent
                                    ),
                                    SSE_HEARTBEAT_SECONDS,
                                )
                            if daemon._stop.is_set():
                                return
                            latest = daemon._latest
                            if latest is not None:
                                data = json.dumps(latest)
                            else:
                                data = "{}"
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                        last_sent = latest
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                finally:
                    if tracked and token is not None:
                        daemon._dot_stream_disconnected(token)

            def do_POST(self) -> None:
                if self.path == "/dot-ack":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, OSError):
                        self._json(400, {"error": "invalid body"})
                        return
                    if not isinstance(body, dict):
                        self._json(400, {"error": "invalid body"})
                        return
                    command_id = body.get("commandID")
                    status = body.get("status")
                    if not isinstance(command_id, str) or not isinstance(status, str):
                        self._json(400, {"error": "commandID and status are required"})
                        return
                    try:
                        availability = _parse_dot_availability(body)
                        dnd_schedule = _parse_dot_dnd_schedule(body)
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    acknowledged = daemon.ack_dot(
                        command_id, status, availability, dnd_schedule
                    )
                    self._json(200, {"ok": True, "acknowledged": acknowledged})
                    return
                if self.path == "/dot-availability":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, OSError):
                        self._json(400, {"error": "invalid body"})
                        return
                    if not isinstance(body, dict):
                        self._json(400, {"error": "invalid body"})
                        return
                    token = body.get("token")
                    try:
                        availability = _parse_dot_availability(body)
                        dnd_schedule = _parse_dot_dnd_schedule(body)
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    if (
                        not isinstance(token, str)
                        or not token
                        or len(token) > DOT_STREAM_TOKEN_MAX_CHARS
                        or availability is None
                    ):
                        self._json(400, {"error": "token and available are required"})
                        return
                    updated = daemon.report_dot_availability(
                        token,
                        availability[0],
                        availability[1],
                        availability[2],
                        availability[3],
                        dnd_schedule,
                    )
                    if not updated:
                        self._json(409, {"ok": False, "error": "not_dot_owner"})
                        return
                    self._json(200, {"ok": True, **daemon._dot_health()})
                    return
                if self.path == "/dot-focus":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, OSError):
                        self._json(400, {"error": "invalid body"})
                        return
                    if not isinstance(body, dict):
                        self._json(400, {"error": "invalid body"})
                        return
                    token = body.get("token")
                    if (
                        not isinstance(token, str)
                        or not token
                        or len(token) > DOT_STREAM_TOKEN_MAX_CHARS
                    ):
                        self._json(400, {"error": "token is required"})
                        return
                    try:
                        focused, reported_at = _parse_dot_focus(body)
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    owner, updated = daemon.report_dot_focus(
                        token, focused, reported_at
                    )
                    if not owner:
                        self._json(409, {"ok": False, "error": "not_dot_owner"})
                        return
                    self._json(
                        200,
                        {"ok": True, "updated": updated, **daemon._dot_health()},
                    )
                    return
                if self.path == "/seen":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, OSError):
                        self._json(400, {"error": "invalid body"})
                        return
                    if not isinstance(body, dict):
                        self._json(400, {"error": "invalid body"})
                        return
                    finished_at = None
                    if "finishedAt" in body:
                        finished_at = body["finishedAt"]
                        if isinstance(finished_at, bool) or not isinstance(
                            finished_at, (int, float)
                        ):
                            self._json(400, {"error": "finishedAt must be a number"})
                            return
                    try:
                        marked = daemon.mark_finished_seen(
                            str(body.get("id", "")), finished_at
                        )
                    except StaleCompletionError:
                        self._json(409, {"ok": False, "error": "stale_completion"})
                        return
                    if marked:
                        daemon._wake.set()
                    self._json(200, {"ok": True, "marked": marked})
                    return
                if self.path != "/register":
                    self._json(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    self._json(400, {"error": "invalid JSON"})
                    return
                kind = body.get("kind")
                token = body.get("token", "")
                try:
                    activity_metadata = _parse_live_activity_metadata(body)
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                device_id = body.get("device_id", "")
                if (
                    not isinstance(device_id, str)
                    or len(device_id) > DEVICE_ID_MAX_CHARS
                ):
                    self._json(400, {"error": "invalid device_id"})
                    return
                activity_observed_at = body.get("activity_observed_at")
                if activity_observed_at is not None and (
                    isinstance(activity_observed_at, bool)
                    or not isinstance(activity_observed_at, (int, float))
                    or not math.isfinite(activity_observed_at)
                ):
                    self._json(400, {"error": "invalid activity_observed_at"})
                    return
                token_observed_at = body.get("token_observed_at")
                if token_observed_at is not None and (
                    isinstance(token_observed_at, bool)
                    or not isinstance(token_observed_at, (int, float))
                    or not math.isfinite(token_observed_at)
                ):
                    self._json(400, {"error": "invalid token_observed_at"})
                    return
                if kind == "reset":
                    activity_id = str(body.get("activity_id", ""))
                    # Legacy unqualified resets can still echo a start push;
                    # current clients identify the dismissed activity, so a
                    # stale duplicate cannot clear the selected winner.
                    if (
                        not activity_id
                        and activity_metadata.get("activity_state")
                        not in LIVE_ACTIVITY_NONLIVE_STATES
                        and daemon._is_reset_echo(time.time())
                    ):
                        _log("ignoring reset echo just after a start push")
                        self._json(200, {"ok": True, "tokens": daemon.tokens.summary()})
                        return
                    reset_meta = {
                        "device": str(body.get("device", "")),
                        "device_id": device_id,
                        "activity_id": activity_id,
                        "activity_observed_at": activity_observed_at,
                        **activity_metadata,
                    }
                    daemon.reset_activity(activity_id, reset_meta)
                    self._json(200, {"ok": True, "tokens": daemon.tokens.summary()})
                    return
                if (
                    kind not in ("push_to_start", "update", "device", "dot_device")
                    or not isinstance(token, str)
                    or not token
                ):
                    self._json(400, {"error": "invalid token kind or empty token"})
                    return
                meta = {
                    "device": str(body.get("device", "")),
                    "device_id": device_id,
                    "activity_id": str(body.get("activity_id", "")),
                    "activity_observed_at": activity_observed_at,
                    "token_observed_at": token_observed_at,
                    **activity_metadata,
                }
                dot_owner_changed = False
                if kind == "update":
                    update_accepted = daemon.register_update_token(token, meta)
                    if (
                        not update_accepted
                        and meta.get("activity_state")
                        not in LIVE_ACTIVITY_NONLIVE_STATES
                    ):
                        self._json(409, {"ok": False, "error": "stale_update_token"})
                        return
                elif kind == "push_to_start":
                    daemon.register_push_to_start_token(token, meta)
                elif kind == "dot_device":
                    # Alert pushes may target several phones, but only the
                    # most recently registered Dot owner receives LED writes.
                    previous = daemon.tokens.entries(kind)
                    if list(previous) == [token]:
                        for key in DOT_UNAVAILABLE_METADATA_KEYS:
                            if key in previous[token]:
                                meta[key] = previous[token][key]
                    dot_owner_changed = daemon.tokens.replace(kind, token, meta)
                    daemon._record_activity_report(
                        meta, kind, persist_for_device=True
                    )
                elif kind == "device":
                    daemon.tokens.register(kind, token, meta)
                    daemon._record_activity_report(
                        meta, kind, persist_for_device=True
                    )
                if kind in ("device", "dot_device"):
                    if daemon.request_dot_resync(force=dot_owner_changed):
                        daemon._wake.set()
                if not (
                    kind == "update"
                    and meta.get("activity_state") in LIVE_ACTIVITY_NONLIVE_STATES
                ):
                    _log(
                        f"registered {kind} token from "
                        f"{meta['device'] or 'unknown'}"
                    )
                self._json(200, {"ok": True, "tokens": daemon.tokens.summary()})

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self) -> None:
                # HTTPServer.server_bind calls socket.getfqdn(), which can
                # hang for tens of seconds on hosts with broken reverse DNS.
                import socketserver

                socketserver.TCPServer.server_bind(self)
                self.server_name = "sidepulse-live-activity"
                self.server_port = self.server_address[1]

        return Server(("0.0.0.0", self.config.port), Handler)
