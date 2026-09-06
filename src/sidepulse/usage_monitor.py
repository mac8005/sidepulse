"""Quota usage for Claude Code and Codex for the iOS app's usage view.

Claude is read straight from the OAuth usage endpoint Claude Code's own
``/usage`` command uses, with the access token Claude Code keeps in the login
Keychain. That is the only source that reports the per-model weekly caps
(e.g. the Fable window) next to the 5-hour and weekly meters.

Codex is read the same way from the ChatGPT backend endpoints the ``codex``
CLI uses for ``/limits``, with the login it keeps in ``~/.codex/auth.json``;
that also covers the free rate-limit reset credits. The CodexBar CLI
(github.com/steipete/CodexBar) is the fallback for whichever provider the
direct read cannot serve (missing login, expired token, network), minus
Claude's per-model windows.

The CLI takes 10-20 seconds per run (and hangs outright behind a Gatekeeper
prompt after a Homebrew upgrade), so refreshes only ever happen on their own
thread; request handlers read the cached snapshot (``GET /usage``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from sidepulse.models import parse_datetime, provider_label

USAGE_PROVIDERS = ("claude", "codex")
DEFAULT_REFRESH_SECONDS = 300.0
# Warn once when a window reaches this much, and again when that window
# resets, so the phone hears "you can continue" without anyone polling.
USAGE_ALERT_PERCENT = 90
CLI_TIMEOUT_SECONDS = 90.0
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
# The endpoints `codex` itself reads for /limits.
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
# The endpoint `codex` itself calls when a free reset is redeemed from /limits.
CODEX_RESET_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
CODEX_RESET_MESSAGES = {
    "reset": "Codex usage limits reset",
    "nothing_to_reset": "Codex usage does not need a reset right now",
    "no_credit": "No free Codex resets available",
    "already_redeemed": "This reset was already redeemed",
}
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


def _codex_headers(auth_path: Path) -> dict[str, str]:
    """Bearer headers from the Codex CLI's login file."""
    try:
        tokens = json.loads(auth_path.read_text()).get("tokens") or {}
    except (OSError, ValueError, AttributeError):
        raise RuntimeError("Codex is not logged in on the Mac") from None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Codex is not logged in on the Mac")
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def _get_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def fetch_codex_usage(auth_path: Path = CODEX_AUTH_PATH) -> dict[str, Any]:
    """Return ``{"usage": <wham/usage payload>, "credits": <reset credits payload>}``.

    ``codex`` refreshes its login file whenever it runs; an expired token
    surfaces as an HTTP error and the caller falls back to CodexBar. The
    credits list only adds expiry dates (the count comes with the usage
    payload), so losing it is not an error.
    """
    headers = _codex_headers(auth_path)
    usage = _get_json(CODEX_USAGE_URL, headers)
    if not isinstance(usage, dict):
        raise RuntimeError("Unexpected Codex usage response")
    try:
        credits = _get_json(CODEX_RESET_CREDITS_URL, headers)
    except (OSError, ValueError):
        credits = None
    return {"usage": usage, "credits": credits if isinstance(credits, dict) else None}


def consume_codex_reset(request_id: str, auth_path: Path = CODEX_AUTH_PATH) -> dict[str, Any]:
    """Redeem one free Codex rate-limit reset with the Codex CLI's login.

    ``request_id`` is the idempotency key the backend uses, so a client that
    retries the same request cannot burn a second credit.
    """
    headers = {**_codex_headers(auth_path), "Content-Type": "application/json"}
    request = urllib.request.Request(
        CODEX_RESET_URL,
        data=json.dumps({"redeem_request_id": request_id}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("Codex login expired on the Mac; run codex once to refresh it") from None
        raise RuntimeError(f"Codex reset failed with HTTP {exc.code}") from None
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(code, str):
        raise RuntimeError("Unexpected Codex reset response")
    return {
        "ok": code == "reset",
        "code": code,
        "windowsReset": payload.get("windows_reset", 0),
        "message": CODEX_RESET_MESSAGES.get(code, code),
    }


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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _percent(value: Any) -> int | None:
    if not _is_number(value):
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


def normalise_codex(result: dict[str, Any], now: float) -> dict[str, Any]:
    """Build the Codex provider entry from the ``wham/usage`` payload.

    Codex reports the 5-hour window as ``primary`` and the weekly one as
    ``secondary``, but once the weekly cap is hit only the weekly window is
    left, as ``primary``; each window is labelled by its own length.
    """
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    limits = usage.get("rate_limit") if isinstance(usage.get("rate_limit"), dict) else {}
    windows: list[dict[str, Any]] = []
    for slot in ("primary", "secondary"):
        window = limits.get(f"{slot}_window")
        used = _percent(window.get("used_percent")) if isinstance(window, dict) else None
        if used is None:
            continue
        seconds = window.get("limit_window_seconds")
        minutes = int(seconds // 60) if _is_number(seconds) else None
        reset_at = window.get("reset_at")
        windows.append(
            {
                "id": slot,
                "label": window_label(minutes),
                "usedPercent": used,
                "resetsAt": float(reset_at) if _is_number(reset_at) else None,
                "windowMinutes": minutes,
            }
        )
    reset_credits = usage.get("rate_limit_reset_credits")
    available = reset_credits.get("available_count") if isinstance(reset_credits, dict) else None
    credits = result.get("credits") if isinstance(result.get("credits"), dict) else {}
    expires: list[float] = []
    for credit in credits.get("credits") or []:
        if not isinstance(credit, dict) or credit.get("status") != "available":
            continue
        expiry = _epoch(credit.get("expires_at"))
        if expiry is not None:
            expires.append(expiry)
    plan = usage.get("plan_type")
    account = usage.get("email")
    return {
        "id": "codex",
        "label": provider_label("codex"),
        "account": account if isinstance(account, str) else None,
        "plan": plan if isinstance(plan, str) else None,
        "windows": windows,
        "resetCredits": int(available) if _is_number(available) else None,
        "resetCreditsExpireAt": min(expires) if expires else None,
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


def _merge_direct(
    providers: list[dict[str, Any]], direct: dict[str, dict[str, Any] | None]
) -> list[dict[str, Any]]:
    """Entries read straight from a provider replace the CLI's for that provider."""
    for provider_id, item in direct.items():
        if item is not None:
            providers = [entry for entry in providers if entry["id"] != provider_id] + [item]
    providers.sort(
        key=lambda item: (
            USAGE_PROVIDERS.index(item["id"]) if item["id"] in USAGE_PROVIDERS else len(USAGE_PROVIDERS),
            item["id"],
        )
    )
    return providers


def normalise_usage(
    payload: list[dict[str, Any]],
    now: float | None = None,
    claude: dict[str, Any] | None = None,
    codex: dict[str, Any] | None = None,
) -> dict[str, Any]:
    direct = {"claude": claude, "codex": codex}
    providers = _merge_direct(
        [item for item in map(normalise_provider, payload) if item is not None], direct
    )
    sources = [f"{provider_id}-oauth" for provider_id, item in direct.items() if item is not None]
    if payload or not sources:
        sources.append("codexbar")
    return {
        "updatedAt": now if now is not None else time.time(),
        "source": "+".join(sources),
        "providers": providers,
        "error": None,
    }


def _relative_time(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def _reset_phrase(window: dict[str, Any], now: float) -> str:
    resets_at = window.get("resetsAt")
    if not _is_number(resets_at) or resets_at <= now:
        return ""
    return f"resets in {_relative_time(resets_at - now)}"


def _warning_alert(
    provider: dict[str, Any], windows: list[dict[str, Any]], now: float
) -> dict[str, str]:
    label = provider.get("label") or provider["id"]
    parts = []
    for window in windows:
        used = window["usedPercent"]
        head = f"{window['label']} limit reached" if used >= 100 else f"{window['label']} at {used}%"
        when = _reset_phrase(window, now)
        parts.append(f"{head}, {when}" if when else head)
    body = "; ".join(parts)
    credits = provider.get("resetCredits")
    if isinstance(credits, int) and credits > 0:
        body += f". {credits} free reset{'' if credits == 1 else 's'} available"
    worst = max(window["usedPercent"] for window in windows)
    title = f"{label} limit reached" if worst >= 100 else f"{label} usage at {worst}%"
    return {"kind": "usage_warning", "title": title, "body": body}


def _reset_alert(provider: dict[str, Any], windows: list[dict[str, Any]]) -> dict[str, str]:
    label = provider.get("label") or provider["id"]
    names = " and ".join(window["label"] for window in windows)
    plural = "s" if len(windows) > 1 else ""
    return {
        "kind": "usage_reset",
        "title": f"{label} usage reset",
        "body": f"{names} window{plural} reset. You can continue.",
    }


def usage_alerts(
    state: dict[str, dict[str, Any]], providers: list[dict[str, Any]], now: float
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Diff fresh provider readings against the armed-window state.

    ``state`` maps ``"<provider>:<window label>"`` to the reading that armed
    it. A window arms with one warning when it reaches USAGE_ALERT_PERCENT
    (once per window instance, told apart by reset time) and disarms with a
    reset alert once its reset time moves on or its usage collapses. Windows
    absent from a reading keep their state: Codex drops the 5-hour window
    while the weekly cap is hit and brings it back at 0% after the reset.
    """
    new_state = dict(state)
    alerts: list[dict[str, str]] = []
    for provider in providers:
        warned: list[dict[str, Any]] = []
        reset: list[dict[str, Any]] = []
        for window in provider.get("windows") or []:
            label = window.get("label")
            used = window.get("usedPercent")
            resets_at = window.get("resetsAt")
            if not isinstance(label, str) or not _is_number(used):
                continue
            key = f"{provider['id']}:{label}"
            armed = state.get(key)
            if used >= USAGE_ALERT_PERCENT:
                if armed is None or (resets_at is not None and armed.get("resetsAt") != resets_at):
                    warned.append(window)
                new_state[key] = {"usedPercent": used, "resetsAt": resets_at}
            elif armed is not None:
                moved_on = (
                    _is_number(resets_at)
                    and _is_number(armed.get("resetsAt"))
                    and resets_at > armed["resetsAt"]
                )
                if moved_on or used < USAGE_ALERT_PERCENT // 2:
                    reset.append(window)
                    new_state.pop(key, None)
        if warned:
            alerts.append(_warning_alert(provider, warned, now))
        if reset:
            alerts.append(_reset_alert(provider, reset))
    return new_state, alerts


class UsageMonitor:
    """Keeps the newest usage snapshot fresh on a background thread."""

    def __init__(
        self,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        runner: Callable[[str, str], list[dict[str, Any]]] = run_codexbar,
        claude_fetcher: Callable[[], dict[str, Any]] = fetch_claude_usage,
        codex_fetcher: Callable[[], dict[str, Any]] = fetch_codex_usage,
        binary: str | None = None,
        clock: Callable[[], float] = time.time,
        on_alert: Callable[[dict[str, str]], None] | None = None,
        alert_state_path: Path | None = None,
    ) -> None:
        self.refresh_seconds = refresh_seconds
        self._runner = runner
        self._claude_fetcher = claude_fetcher
        self._codex_fetcher = codex_fetcher
        self._binary = binary
        self._clock = clock
        self._on_alert = on_alert
        self._alert_state_path = alert_state_path
        self._alert_state: dict[str, dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
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
        direct: dict[str, dict[str, Any] | None] = {}
        for provider_id, fetcher, normalise in (
            ("claude", self._claude_fetcher, normalise_claude),
            ("codex", self._codex_fetcher, normalise_codex),
        ):
            try:
                direct[provider_id] = normalise(fetcher(), now)
            except Exception:  # login, network or auth failure: CodexBar covers it
                direct[provider_id] = None
        missing = [provider_id for provider_id in USAGE_PROVIDERS if direct[provider_id] is None]
        binary = self.binary
        # Only readings taken just now may raise alerts; a failed CLI run
        # carries the previous providers along, and those were judged already.
        fresh = [item for item in direct.values() if item is not None]
        if not missing:
            snapshot = normalise_usage([], now=now, **direct)
        elif not binary:
            snapshot = normalise_usage([], now=now, **direct)
            snapshot["error"] = "codexbar CLI not installed"
        else:
            try:
                payload = self._runner(binary, missing[0] if len(missing) == 1 else "both")
                snapshot = normalise_usage(payload, now=now, **direct)
                fresh = list(snapshot["providers"])
            except Exception as exc:  # subprocess, JSON or timeout failures
                snapshot = self._failed(str(exc) or exc.__class__.__name__, direct)
        with self._lock:
            self._snapshot = snapshot
        self._notify_usage_alerts(fresh, now)
        return dict(snapshot)

    def _notify_usage_alerts(self, providers: list[dict[str, Any]], now: float) -> None:
        if self._on_alert is None:
            return
        if self._alert_state is None:
            self._alert_state = self._load_alert_state()
        new_state, alerts = usage_alerts(self._alert_state, providers, now)
        if new_state != self._alert_state:
            self._alert_state = new_state
            self._save_alert_state()
        for alert in alerts:
            try:
                self._on_alert(alert)
            except Exception:  # a push failure must not stop the refresh loop
                pass

    def _load_alert_state(self) -> dict[str, dict[str, Any]]:
        # Armed windows survive daemon restarts so a deploy cannot repeat a
        # warning or miss the reset that follows it.
        if self._alert_state_path is None:
            return {}
        try:
            raw = json.loads(self._alert_state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def _save_alert_state(self) -> None:
        if self._alert_state_path is None:
            return
        try:
            self._alert_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._alert_state_path.write_text(json.dumps(self._alert_state))
        except OSError:
            pass

    def _failed(
        self, message: str, direct: dict[str, dict[str, Any] | None]
    ) -> dict[str, Any]:
        # Keep the last good providers so the view degrades to stale numbers
        # with an error banner instead of going blank.
        with self._lock:
            previous = self._snapshot or {}
        providers = _merge_direct(list(previous.get("providers") or []), direct)
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
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def request_refresh(self) -> None:
        """Refresh ahead of schedule, e.g. right after a reset credit was used."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._wake.wait(self.refresh_seconds)
            self._wake.clear()
