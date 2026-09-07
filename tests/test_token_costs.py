from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from sidepulse.usage_monitor import UsageMonitor, normalise_token_cost, run_codexbar_cost


def cost_payload(provider="codex", **changes):
    return {
        "provider": provider, "source": "local", "currencyCode": "USD",
        "updatedAt": "2026-09-07T18:05:00Z", "historyCoverageIsEstablished": True,
        "sessionTokens": 57_603_949, "sessionCostUSD": 88.5604172,
        "last30DaysTokens": 6_927_047_367, "last30DaysCostUSD": 4020.08030176,
        "coverage": {"priced": 29, "unpriced": 0, "unmetered": 0, "estimated": 0},
        # These are included in the totals already and must not be added again.
        "totals": {"cacheReadTokens": 6_706_418_688, "reasoningTokens": 9_378_500},
        **changes,
    }


def monitor(cost_runner, **changes):
    return UsageMonitor(
        binary="/fake/codexbar", cost_runner=cost_runner,
        claude_fetcher=lambda: {"usage": {"five_hour": {"utilization": 12}}},
        codex_fetcher=lambda: {"usage": {"rate_limit": {
            "primary_window": {"used_percent": 30, "limit_window_seconds": 18000},
        }}},
        **changes,
    )


def test_cost_uses_codexbar_totals_without_repricing_or_double_counting():
    result = normalise_token_cost(cost_payload(), 0)
    assert result == {
        "today": {"tokens": 57_603_949, "costUSD": 88.5604172},
        "last30Days": {"tokens": 6_927_047_367, "costUSD": 4020.08030176},
        "updatedAt": 1788804300.0, "partial": False, "stale": False,
    }


@pytest.mark.parametrize("value", [None, -1, True, "12", float("nan"), float("inf")])
def test_bad_cost_values_remain_unknown_not_zero(value):
    result = normalise_token_cost(cost_payload(sessionCostUSD=value), 0)
    assert result["today"] == {"tokens": 57_603_949, "costUSD": None}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("value", [None, -1, True, "12", 1.5, 2**63])
def test_bad_token_values_remain_unknown(value):
    result = normalise_token_cost(cost_payload(sessionTokens=value), 0)
    assert result["today"]["tokens"] is None


def test_real_zero_and_unknown_are_distinct():
    result = normalise_token_cost(cost_payload(sessionTokens=0, sessionCostUSD=0), 0)
    assert result["today"] == {"tokens": 0, "costUSD": 0}
    assert normalise_token_cost({"source": "local"}, 0) is None


@pytest.mark.parametrize("changes", [
    {"source": "web"}, {"currencyCode": "EUR"}, {"error": "reader unavailable"},
])
def test_other_sources_currencies_and_errors_are_not_claimed_as_local_usd_cost(changes):
    assert normalise_token_cost(cost_payload(**changes), 0) is None


@pytest.mark.parametrize("changes", [
    {"coverage": {"unpriced": 1}}, {"coverage": {"unmetered": 1}},
    {"historyCoverageIsEstablished": False},
])
def test_incomplete_history_or_pricing_is_flagged(changes):
    assert normalise_token_cost(cost_payload(**changes), 0)["partial"] is True


def test_all_unpriced_tokens_are_not_displayed_as_free():
    result = normalise_token_cost(cost_payload(
        sessionCostUSD=0, last30DaysCostUSD=0,
        coverage={"priced": 0, "unpriced": 30, "unmetered": 0, "estimated": 0},
    ), 0)
    assert result["today"]["costUSD"] is None
    assert result["last30Days"]["costUSD"] is None


def test_cli_uses_cached_bounded_scan_and_accepts_partial_provider_success(monkeypatch):
    def run(argv, **kwargs):
        assert argv == ["/fake/codexbar", "cost", "--provider", "both", "--json",
                        "--no-color", "--days", "30"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 20.0, "check": False}
        return subprocess.CompletedProcess(argv, 1, json.dumps([cost_payload(), {
            "provider": "claude", "error": "unavailable",
        }]), "")

    monkeypatch.setattr("sidepulse.usage_monitor.subprocess.run", run)
    result = run_codexbar_cost("/fake/codexbar")
    assert len(result) == 2
    assert result[0]["provider"] == "codex"


@pytest.mark.parametrize("payload", ["null", "3", '"invalid"', "not JSON"])
def test_malformed_cli_result_raises_without_becoming_zero_cost(payload, monkeypatch):
    monkeypatch.setattr("sidepulse.usage_monitor.subprocess.run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 1, payload, ""))
    with pytest.raises(ValueError):
        run_codexbar_cost("/fake/codexbar")


def test_costs_join_the_right_provider_and_keep_oauth_quotas():
    instance = monitor(lambda _: [cost_payload(), cost_payload("claude", sessionCostUSD=36.4)])
    snapshot = instance.refresh()
    claude, codex = snapshot["providers"]
    assert claude["id"] == "claude" and claude["tokenCost"]["today"]["costUSD"] == 36.4
    assert codex["id"] == "codex" and codex["tokenCost"]["today"]["costUSD"] == 88.5604172
    assert claude["windows"][0]["usedPercent"] == 12
    assert codex["windows"][0]["usedPercent"] == 30
    assert snapshot["error"] is None


def test_cost_failure_keeps_last_estimate_explicitly_stale_without_hiding_usage():
    payloads = [[cost_payload()], RuntimeError("private path must not leak")]

    def fetch(_):
        value = payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    instance = monitor(fetch)
    first = instance.refresh()
    original = copy.deepcopy(first)
    second = instance.refresh()
    assert first == original
    assert second["error"] is None
    codex = second["providers"][1]
    assert codex["tokenCost"] == {**first["providers"][1]["tokenCost"], "stale": True}
    assert codex["windows"][0]["usedPercent"] == 30
    assert "private path" not in json.dumps(second)


def test_partial_provider_failure_does_not_stale_the_successful_provider():
    readings = [[cost_payload(), cost_payload("claude")], [
        cost_payload(sessionCostUSD=90), {"provider": "claude", "error": "no reader"},
    ]]
    instance = monitor(lambda _: readings.pop(0))
    instance.refresh()
    claude, codex = instance.refresh()["providers"]
    assert claude["tokenCost"]["stale"] is True
    assert codex["tokenCost"]["stale"] is False
    assert codex["tokenCost"]["today"]["costUSD"] == 90


def test_missing_cli_and_empty_costs_do_not_hide_usage(monkeypatch):
    instance = monitor(lambda _: [])
    for provider in instance.refresh()["providers"]:
        assert provider["tokenCost"] is None
        assert provider["tokenCostError"] == "Token cost unavailable"
        assert provider["windows"]
    instance._binary = None
    monkeypatch.setattr("sidepulse.usage_monitor.codexbar_binary", lambda: None)
    instance._cost_runner = lambda _: pytest.fail("must not scan without the CLI")
    assert len(instance.refresh()["providers"]) == 2


def test_costs_are_available_even_when_quota_sources_fail():
    def fail(*_):
        raise RuntimeError("quota unavailable")

    instance = UsageMonitor(binary="/fake/codexbar", claude_fetcher=fail,
                            codex_fetcher=fail, runner=fail,
                            cost_runner=lambda _: [cost_payload()])
    provider = instance.refresh()["providers"][0]
    assert provider["id"] == "codex"
    assert provider["windows"] == []
    assert provider["tokenCost"]["today"]["costUSD"] == 88.5604172


def test_cost_only_provider_keeps_its_estimate_when_the_next_reading_is_missing():
    def no_codex_quota():
        raise RuntimeError("Codex quota unavailable")

    readings = [[cost_payload()], []]
    instance = UsageMonitor(
        binary="/fake/codexbar", cost_runner=lambda _: readings.pop(0),
        claude_fetcher=lambda: {"usage": {"five_hour": {"utilization": 12}}},
        codex_fetcher=no_codex_quota, runner=lambda *_: [],
    )
    first = instance.refresh()["providers"]
    second = instance.refresh()["providers"]
    assert [provider["id"] for provider in second] == ["claude", "codex"]
    assert second[1]["tokenCost"] == {**first[1]["tokenCost"], "stale": True}


def test_usage_alerts_are_not_delayed_by_cost_collection():
    order = []
    instance = monitor(lambda _: order.append("cost") or [])
    instance._notify_usage_alerts = lambda *_: order.append("alerts")
    instance.refresh()
    assert order == ["alerts", "cost"]


def test_ios_decodes_costs_and_legacy_usage(tmp_path):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("Swift/macOS required")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "usage-cost-tests"
    subprocess.run([
        "swiftc", "-j", "1", "-num-threads", "1", "-swift-version", "5",
        "-module-cache-path", str(tmp_path / "ModuleCache"), "-o", str(binary),
        str(root / "ios/SidePulse/SidePulse/UsageClient.swift"),
        str(root / "tests/test_ios_usage_cost.swift"),
    ], check=True, capture_output=True, text=True, timeout=120)
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)
    assert "Usage cost tests passed" in result.stdout
