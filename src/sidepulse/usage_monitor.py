"""Quota usage for Claude Code and Codex for the iOS app's usage view.

Claude is read straight from the OAuth usage endpoint Claude Code's own
``/usage`` command uses, with the access token Claude Code keeps in the login
Keychain. That is the only source that reports the per-model weekly caps
(e.g. the Fable window) next to the 5-hour and weekly meters.

Codex comes from the CodexBar CLI (github.com/steipete/CodexBar), which knows
how to read the Codex credentials and the free rate-limit reset credits. When
the Claude endpoint is unavailable the CLI covers Claude too, minus the
per-model windows.

The CLI takes 10-20 seconds per run, so refreshes only ever happen on their
own thread; request handlers read the cached snapshot (``GET /usage``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from sidepulse.models import parse_datetime, provider_label

USAGE_PROVIDERS = ("claude", "codex")
DEFAULT_REFRESH_SECONDS = 300.0
CLI_TIMEOUT_SECONDS = 90.0
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CODEXBAR_FALLBACKS = (
    Path("/opt/homebrew/bin/codexbar"),
    Path("/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI"),
)
# CodexBar reports each rate-limit window under a fixed slot name.
_WINDOW_SLOTS = ("primary", "secondary", "tertiary")
# The Claude endpoint names its two account-wide windows; they map onto the
# same slots so the app shows Claude the same way whichever source served it.
_CLAUDE_WINDOWS = (("primary", "five_hour", 300), ("secondary", "seven_day", 10080))


def codexbar_binary() -> str | None:
    found = shutil.which("codexbar")
    if found:
        return found
    for candidate in _CODEXBAR_FALLBACKS:
        if candidate.is_file():
            return str(candidate)
    return None


def run_codexbar(binary: str, provider: str = "both") -> list[dict[str, Any]]:
    """Run the CLI once and return its JSON payload (one entry per provider)."""
    completed = subprocess.run(
        [binary, "usage", "--provider", provider, "--json", "--no-color"],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        detail = completed.stderr.strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"codexbar exited {completed.returncode}")
    payload = json.loads(completed.stdout)
    if isinstance(payload, dict):
        payload = [payload]
    return [entry for entry in payload if isinstance(entry, dict)]


def fetch_claude_usage() -> dict[str, Any]:
    """Return ``{"usage": <oauth usage payload>, "plan": <subscription>}``.

    Claude Code refreshes the Keychain token whenever it runs, so this needs
    no token handling of its own; an expired token surfaces as an HTTP error
    and the caller falls back to CodexBar.
    """
    completed = subprocess.run(
        ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Claude Code credentials not found in the Keychain")
    oauth = json.loads(completed.stdout).get("claudeAiOauth")
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Claude Code Keychain item has no OAuth token")
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "sidepulse-usage",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        usage = json.load(response)
    if not isinstance(usage, dict):
        raise RuntimeError("Unexpected Claude usage response")
    return {"usage": usage, "plan": oauth.get("subscriptionType")}


def window_label(window_minutes: Any) -> str:
    minutes = window_minutes if isinstance(window_minutes, (int, float)) else 0
    if minutes == 300:
        return "5-hour"
    if minutes == 10080:
        return "Weekly"
    if minutes and minutes % 1440 == 0:
        return f"{int(minutes // 1440)}-day"
    if minutes and minutes % 60 == 0:
        return f"{int(minutes // 60)}-hour"
    return "Window"


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    return parse_datetime(value).timestamp()


def _percent(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(round(value))))


def normalise_claude(result: dict[str, Any], now: float) -> dict[str, Any]:
    """Build the Claude provider entry from the OAuth usage payload."""
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    windows: list[dict[str, Any]] = []
    for slot, key, minutes in _CLAUDE_WINDOWS:
        block = usage.get(key)
        used = _percent(block.get("utilization")) if isinstance(block, dict) else None
        if used is None:
            continue
        windows.append(
            {
                "id": slot,
                "label": window_label(minutes),
                "usedPercent": used,
                "resetsAt": _epoch(block.get("resets_at")),
                "windowMinutes": minutes,
            }
        )
    # Per-model weekly caps ("weekly_scoped") only exist in the limits list.
    for limit in usage.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        name = model.get("display_name")
        used = _percent(limit.get("percent"))
        if not isinstance(name, str) or not name or used is None:
            continue
        windows.append(
            {
                "id": f"scoped:{name.lower()}",
                "label": f"{name} weekly",
                "usedPercent": used,
                "resetsAt": _epoch(limit.get("resets_at")),
                "windowMinutes": 10080,
            }
        )
    plan = result.get("plan")
    return {
        "id": "claude",
        "label": provider_label("claude"),
        "account": None,
        "plan": plan if isinstance(plan, str) else None,
        "windows": windows,
        "resetCredits": None,
        "resetCreditsExpireAt": None,
        "updatedAt": now,
        "error": None if windows else "No usage windows reported",
    }


def normalise_provider(entry: dict[str, Any]) -> dict[str, Any] | None:
    provider = entry.get("provider")
    if not isinstance(provider, str) or not provider:
        return None
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    pace = entry.get("pace") if isinstance(entry.get("pace"), dict) else {}

    windows: list[dict[str, Any]] = []
    for slot in _WINDOW_SLOTS:
        window = usage.get(slot)
        used = _percent(window.get("usedPercent")) if isinstance(window, dict) else None
        if used is None:
            continue
        item: dict[str, Any] = {
            "id": slot,
            "label": window_label(window.get("windowMinutes")),
            "usedPercent": used,
            "resetsAt": _epoch(window.get("resetsAt")),
            "windowMinutes": window.get("windowMinutes"),
        }
        slot_pace = pace.get(slot)
        if isinstance(slot_pace, dict) and isinstance(slot_pace.get("summary"), str):
            item["pace"] = slot_pace["summary"]
        windows.append(item)

    reset_credits = usage.get("codexResetCredits")
    available_count = None
    expires: list[float] = []
    if isinstance(reset_credits, dict):
        available_count = reset_credits.get("availableCount")
        for credit in reset_credits.get("credits") or []:
            if not isinstance(credit, dict) or credit.get("status") != "available":
                continue
            expiry = _epoch(credit.get("expires_at"))
            if expiry is not None:
                expires.append(expiry)
    error = entry.get("error")
    result: dict[str, Any] = {
        "id": provider,
        "label": provider_label(provider),
        "account": usage.get("accountEmail"),
        "plan": usage.get("loginMethod"),
        "windows": windows,
        "resetCredits": available_count if isinstance(available_count, int) else None,
        "resetCreditsExpireAt": min(expires) if expires else None,
        "updatedAt": _epoch(usage.get("updatedAt")),
        "error": error if isinstance(error, str) else None,
    }
    if not windows and result["error"] is None:
        result["error"] = "No usage windows reported"
    return result


def normalise_usage(
    payload: list[dict[str, Any]],
    now: float | None = None,
    claude: dict[str, Any] | None = None,
) -> dict[str, Any]:
    providers = [item for item in map(normalise_provider, payload) if item is not None]
    if claude is not None:
        providers = [item for item in providers if item["id"] != "claude"] + [claude]
    providers.sort(
        key=lambda item: (
            USAGE_PROVIDERS.index(item["id"]) if item["id"] in USAGE_PROVIDERS else len(USAGE_PROVIDERS),
            item["id"],
        )
    )
    return {
        "updatedAt": now if now is not None else time.time(),
        "source": "claude-oauth+codexbar" if claude is not None else "codexbar",
        "providers": providers,
        "error": None,
    }


class UsageMonitor:
    """Keeps the newest usage snapshot fresh on a background thread."""

    def __init__(
        self,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        runner: Callable[[str, str], list[dict[str, Any]]] = run_codexbar,
        claude_fetcher: Callable[[], dict[str, Any]] = fetch_claude_usage,
        binary: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.refresh_seconds = refresh_seconds
        self._runner = runner
        self._claude_fetcher = claude_fetcher
        self._binary = binary
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def binary(self) -> str | None:
        if self._binary is None:
            self._binary = codexbar_binary()
        return self._binary

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is not None:
                return dict(self._snapshot)
        return {
            "updatedAt": None,
            "source": "codexbar",
            "providers": [],
            "error": "Waiting for the first usage reading" if self.binary else "codexbar CLI not installed",
        }

    def refresh(self) -> dict[str, Any]:
        now = self._clock()
        try:
            claude = normalise_claude(self._claude_fetcher(), now)
        except Exception:  # Keychain, network or auth failure: CodexBar covers Claude
            claude = None
        binary = self.binary
        if not binary:
            snapshot = normalise_usage([], now=now, claude=claude)
            snapshot["error"] = "codexbar CLI not installed"
        else:
            try:
                payload = self._runner(binary, "codex" if claude is not None else "both")
                snapshot = normalise_usage(payload, now=now, claude=claude)
            except Exception as exc:  # subprocess, JSON or timeout failures
                snapshot = self._failed(str(exc) or exc.__class__.__name__, claude)
        with self._lock:
            self._snapshot = snapshot
        return dict(snapshot)

    def _failed(self, message: str, claude: dict[str, Any] | None) -> dict[str, Any]:
        # Keep the last good providers so the view degrades to stale numbers
        # with an error banner instead of going blank.
        with self._lock:
            previous = self._snapshot or {}
        providers = list(previous.get("providers") or [])
        if claude is not None:
            providers = [item for item in providers if item["id"] != "claude"] + [claude]
            providers.sort(key=lambda item: item["id"] != "claude")
        return {
            "updatedAt": previous.get("updatedAt"),
            "source": previous.get("source", "codexbar"),
            "providers": providers,
            "error": message,
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sidepulse-usage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.refresh_seconds)
