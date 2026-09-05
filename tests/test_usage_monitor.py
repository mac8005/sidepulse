from __future__ import annotations

from sidepulse.usage_monitor import (
    UsageMonitor,
    normalise_claude,
    normalise_usage,
    window_label,
)

# Trimmed from a real `codexbar usage --provider both --json` run: Codex has no
# 5-hour window and carries reset credits, Claude has both windows and no
# account identity.
CODEXBAR_PAYLOAD = [
    {
        "provider": "codex",
        "source": "oauth",
        "pace": {
            "secondary": {
                "expectedUsedPercent": 77,
                "summary": "14% in deficit | Expected 77% used | Runs out in 12h 45m",
            }
        },
        "usage": {
            "updatedAt": "2026-09-05T12:35:45Z",
            "primary": None,
            "secondary": {
                "resetsAt": "2026-09-07T03:41:55Z",
                "usedPercent": 91,
                "windowMinutes": 10080,
            },
            "tertiary": None,
            "codexResetCredits": {
                "credits": [
                    {"status": "available", "expires_at": "2026-10-04T02:00:35Z"},
                    {"status": "available", "expires_at": "2026-09-20T23:58:15Z"},
                    {"status": "used", "expires_at": "2026-09-01T00:00:00Z"},
                ],
                "availableCount": 2,
            },
            "accountEmail": "massimo@cerqui.ch",
            "loginMethod": "pro",
        },
    },
    {
        "provider": "claude",
        "source": "claude",
        "pace": {"primary": {"summary": "10% in reserve | Expected 12% used | Lasts until reset"}},
        "usage": {
            "updatedAt": "2026-09-05T12:35:57Z",
            "primary": {
                "resetsAt": "2026-09-05T17:00:00Z",
                "usedPercent": 2.4,
                "windowMinutes": 300,
            },
            "secondary": {
                "resetsAt": "2026-09-10T23:00:00Z",
                "usedPercent": 4,
                "windowMinutes": 10080,
            },
            "tertiary": None,
        },
    },
]

# Trimmed from `GET https://api.anthropic.com/api/oauth/usage`: the per-model
# weekly cap only appears in `limits` as a scoped entry.
CLAUDE_OAUTH_RESULT = {
    "plan": "max",
    "usage": {
        "five_hour": {"utilization": 12.4, "resets_at": "2026-09-05T17:00:00.280799+00:00"},
        "seven_day": {"utilization": 6.0, "resets_at": "2026-09-10T23:00:00.280816+00:00"},
        "limits": [
            {"kind": "session", "group": "session", "percent": 12, "scope": None},
            {"kind": "weekly_all", "group": "weekly", "percent": 6, "scope": None},
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 31,
                "resets_at": "2026-09-10T23:00:00.281005+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            },
        ],
    },
}


def test_window_labels_follow_codexbar_window_lengths() -> None:
    assert window_label(300) == "5-hour"
    assert window_label(10080) == "Weekly"
    assert window_label(1440) == "1-day"
    assert window_label(None) == "Window"


def test_normalise_orders_claude_first_and_keeps_only_reported_windows() -> None:
    snapshot = normalise_usage(CODEXBAR_PAYLOAD, now=1_000.0)

    assert snapshot["updatedAt"] == 1_000.0
    assert snapshot["source"] == "codexbar"
    assert snapshot["error"] is None
    assert [item["id"] for item in snapshot["providers"]] == ["claude", "codex"]

    claude, codex = snapshot["providers"]
    assert [w["id"] for w in claude["windows"]] == ["primary", "secondary"]
    assert claude["windows"][0] == {
        "id": "primary",
        "label": "5-hour",
        "usedPercent": 2,
        "resetsAt": 1788627600.0,
        "windowMinutes": 300,
        "pace": "10% in reserve | Expected 12% used | Lasts until reset",
    }
    assert "pace" not in claude["windows"][1]
    assert claude["account"] is None
    assert claude["resetCredits"] is None
    assert claude["resetCreditsExpireAt"] is None

    assert [w["label"] for w in codex["windows"]] == ["Weekly"]
    assert codex["windows"][0]["usedPercent"] == 91
    assert codex["account"] == "massimo@cerqui.ch"
    assert codex["plan"] == "pro"
    assert codex["updatedAt"] == 1788611745.0


def test_codex_reset_credits_report_count_and_earliest_available_expiry() -> None:
    codex = normalise_usage(CODEXBAR_PAYLOAD)["providers"][1]
    assert codex["resetCredits"] == 2
    # The used credit expires first but must not count; the earliest available one wins.
    assert codex["resetCreditsExpireAt"] == 1789948695.0  # 2026-09-20T23:58:15Z


def test_claude_oauth_adds_scoped_model_window_after_the_account_windows() -> None:
    claude = normalise_claude(CLAUDE_OAUTH_RESULT, now=1_000.0)

    assert claude["plan"] == "max"
    assert claude["updatedAt"] == 1_000.0
    assert claude["error"] is None
    assert [(w["id"], w["label"], w["usedPercent"]) for w in claude["windows"]] == [
        ("primary", "5-hour", 12),
        ("secondary", "Weekly", 6),
        ("scoped:fable", "Fable weekly", 31),
    ]
    assert claude["windows"][2]["resetsAt"] == 1789081200.281005
    assert claude["windows"][2]["windowMinutes"] == 10080


def test_claude_oauth_replaces_the_codexbar_claude_entry() -> None:
    claude = normalise_claude(CLAUDE_OAUTH_RESULT, now=5.0)
    snapshot = normalise_usage(CODEXBAR_PAYLOAD, now=5.0, claude=claude)

    assert snapshot["source"] == "claude-oauth+codexbar"
    assert [item["id"] for item in snapshot["providers"]] == ["claude", "codex"]
    assert snapshot["providers"][0] is claude


def test_provider_without_windows_reports_an_error() -> None:
    snapshot = normalise_usage([{"provider": "claude", "usage": {}}])
    assert snapshot["providers"][0]["error"] == "No usage windows reported"
    assert normalise_claude({"usage": {}}, now=0.0)["error"] == "No usage windows reported"


def _failing_claude() -> dict[str, object]:
    raise RuntimeError("Claude Code credentials not found in the Keychain")


def test_monitor_asks_codexbar_only_for_codex_when_claude_oauth_works() -> None:
    calls: list[tuple[str, str]] = []

    def runner(binary: str, provider: str) -> list[dict[str, object]]:
        calls.append((binary, provider))
        return [entry for entry in CODEXBAR_PAYLOAD if entry["provider"] == provider]

    monitor = UsageMonitor(
        runner=runner,
        claude_fetcher=lambda: CLAUDE_OAUTH_RESULT,
        binary="/fake/codexbar",
        clock=lambda: 42.0,
    )
    snapshot = monitor.refresh()

    assert calls == [("/fake/codexbar", "codex")]
    assert snapshot["error"] is None
    claude, codex = snapshot["providers"]
    assert [w["id"] for w in claude["windows"]] == ["primary", "secondary", "scoped:fable"]
    assert codex["resetCredits"] == 2


def test_monitor_falls_back_to_codexbar_for_claude_when_oauth_fails() -> None:
    calls: list[str] = []

    def runner(binary: str, provider: str) -> list[dict[str, object]]:
        calls.append(provider)
        return CODEXBAR_PAYLOAD

    monitor = UsageMonitor(runner=runner, claude_fetcher=_failing_claude, binary="/fake/codexbar")
    snapshot = monitor.refresh()

    assert calls == ["both"]
    assert snapshot["source"] == "codexbar"
    assert [w["id"] for w in snapshot["providers"][0]["windows"]] == ["primary", "secondary"]


def test_monitor_refresh_keeps_last_reading_when_the_cli_fails() -> None:
    calls: list[str] = []

    def runner(binary: str, provider: str) -> list[dict[str, object]]:
        calls.append(binary)
        if len(calls) > 1:
            raise RuntimeError("codexbar exited 1")
        return CODEXBAR_PAYLOAD

    monitor = UsageMonitor(
        runner=runner, claude_fetcher=_failing_claude, binary="/fake/codexbar", clock=lambda: 42.0
    )
    assert monitor.snapshot()["error"] == "Waiting for the first usage reading"

    first = monitor.refresh()
    assert first["error"] is None
    assert len(first["providers"]) == 2

    second = monitor.refresh()
    assert second["error"] == "codexbar exited 1"
    assert second["updatedAt"] == 42.0
    assert [item["id"] for item in second["providers"]] == ["claude", "codex"]
    assert monitor.snapshot() == second


def test_monitor_keeps_fresh_claude_reading_when_only_the_cli_fails() -> None:
    def runner(binary: str, provider: str) -> list[dict[str, object]]:
        raise RuntimeError("codexbar exited 1")

    monitor = UsageMonitor(
        runner=runner, claude_fetcher=lambda: CLAUDE_OAUTH_RESULT, binary="/fake/codexbar"
    )
    snapshot = monitor.refresh()

    assert snapshot["error"] == "codexbar exited 1"
    assert [item["id"] for item in snapshot["providers"]] == ["claude"]
    assert snapshot["providers"][0]["plan"] == "max"


def test_monitor_without_cli_reports_it_instead_of_running(monkeypatch) -> None:
    monkeypatch.setattr("sidepulse.usage_monitor.codexbar_binary", lambda: None)
    calls: list[str] = []
    monitor = UsageMonitor(
        runner=lambda binary, provider: calls.append(binary) or [],
        claude_fetcher=_failing_claude,
    )

    assert monitor.snapshot()["error"] == "codexbar CLI not installed"
    snapshot = monitor.refresh()
    assert snapshot["providers"] == []
    assert snapshot["error"] == "codexbar CLI not installed"
    assert calls == []
