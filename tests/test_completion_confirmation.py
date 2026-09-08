import io
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sidepulse import codex_goals, collector, remote_hosts
from sidepulse.collector import (
    LiveAgentMonitor, agent_status_from_dict, codex_transcript_event,
    mode_for_event, status_for_snapshot, status_from_event,
)
from sidepulse.live_activity import compute_alerts
from sidepulse.models import AgentMode, AgentStatus, HookEvent
from sidepulse.providers import parse_log_line


NOW = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)


def row(provider="codex", **changes):
    return replace(AgentStatus(
        provider=provider, agent_id=f"{provider}:session:test-goal",
        session_id="test-goal", display_name="Trading: validation",
        mode=AgentMode.COMPLETED, event_name="Stop", updated_at=NOW,
    ), **changes)


def visible(status, seconds):
    return status_for_snapshot(status, NOW + timedelta(seconds=seconds),
                               post_tool_working_visible_seconds=120)


@pytest.fixture(autouse=True)
def isolated_goals(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    codex_goals._read_goal_states.cache_clear()


@pytest.mark.parametrize("provider", ["codex", "claude", "grok", "paseo"])
def test_completion_requires_ten_quiet_seconds(provider):
    status = row(provider)
    assert visible(status, 0).mode == AgentMode.WORKING
    assert visible(status, 9.99).mode == AgentMode.WORKING
    assert visible(status, 10).mode == AgentMode.COMPLETED
    assert status.mode == AgentMode.COMPLETED


def test_continuation_never_generates_a_completion_alert(monkeypatch):
    monitor = LiveAgentMonitor()
    previous = {}
    for name, seconds in [("UserPromptSubmit", 0), ("Stop", 1), ("PreToolUse", 4), ("Stop", 30)]:
        monitor.ingest_record(HookEvent(provider="codex", session_id="test-goal",
            logged_at=NOW + timedelta(seconds=seconds), event_name=name, raw={}))
        state = visible(monitor.statuses_by_key["codex:session:test-goal"], seconds + 1)
        alerts, previous = compute_alerts(previous, [state], NOW.timestamp() + seconds + 1, {})
        assert not any(alert["kind"] == "completed" for alert in alerts)
    state = visible(monitor.statuses_by_key["codex:session:test-goal"], 40)
    alerts, _ = compute_alerts(previous, [state], NOW.timestamp() + 40, {})
    assert [alert["kind"] for alert in alerts] == ["completed"]


def test_active_goal_holds_turn_completion_and_silent_reasoning(monkeypatch):
    states = {"test-goal": "active"}
    monkeypatch.setattr(collector, "goal_states", lambda: states)
    assert visible(row(), 300).mode == AgentMode.WORKING
    thinking = row(mode=AgentMode.WORKING, event_name="PostToolUse")
    assert visible(thinking, 1800).mode == AgentMode.WORKING
    for terminal in ["complete", "paused", "blocked", "budget_limited", "usage_limited"]:
        states["test-goal"] = terminal
        assert visible(row(), 300).mode == AgentMode.COMPLETED


def test_real_input_request_and_subagent_completion_are_not_hidden(monkeypatch):
    monkeypatch.setattr(collector, "goal_states", lambda: {"test-goal": "active"})
    assert visible(row(mode=AgentMode.WAITING_FOR_INPUT, event_name="PermissionRequest"), 0).mode == AgentMode.WAITING_FOR_INPUT
    assert visible(row(agent_id="codex:agent:child", event_name="SubagentStop"), 20).mode == AgentMode.COMPLETED


@pytest.mark.parametrize("channel", ["commentary", "analysis"])
def test_transcript_progress_is_not_a_stop(channel, tmp_path):
    event = codex_transcript_event({"type": "message", "role": "assistant",
        "channel": channel, "content": [{"type": "output_text", "text": "Continuing the validation."}]},
        session_id="test-goal", turn_id="turn", cwd=None, timestamp=NOW, path=tmp_path / "rollout.jsonl")
    assert mode_for_event(event) == AgentMode.WORKING


@pytest.mark.parametrize("kind,channel,expected", [
    ("message", "final", AgentMode.COMPLETED),
    ("task_complete", None, AgentMode.COMPLETED),
    ("task_started", None, AgentMode.WORKING),
])
def test_transcript_turn_lifecycle(kind, channel, expected, tmp_path):
    event = codex_transcript_event({"type": kind, "role": "assistant", "channel": channel},
        session_id="test-goal", turn_id="turn", cwd=None, timestamp=NOW, path=tmp_path / "rollout.jsonl")
    assert mode_for_event(event) == expected


def test_goal_database_is_read_only_and_refreshes(tmp_path):
    path = tmp_path / "goals_1.sqlite"
    assert codex_goals.goal_states() == {}
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE thread_goals(thread_id TEXT PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO thread_goals VALUES ('test-goal', 'active')")
    assert codex_goals._read_goal_states(str(path), 1) == {"test-goal": "active"}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE thread_goals SET status = 'complete'")
    assert codex_goals._read_goal_states(str(path), 2) == {"test-goal": "complete"}


def test_remote_goal_uses_source_host_and_survives_persistence(monkeypatch):
    monkeypatch.setattr(remote_hosts, "goal_states", lambda: {"test-goal": "active"})
    monkeypatch.setattr(remote_hosts, "remote_session_web_link", lambda *args: None)
    output = io.StringIO()
    remote_hosts._emit_envelope("codex", json.dumps({"logged_at": NOW.isoformat(), "event": {
        "hook_event_name": "Stop", "session_id": "test-goal"}}), output)
    line = remote_hosts.qualify_remote_line("codex", json.loads(output.getvalue())["line"], "mini")
    status = status_from_event(parse_log_line("codex", json.dumps(line)))
    restored = agent_status_from_dict(status.to_dict())
    assert restored.goal_status == "active"
    assert visible(restored, 300).mode == AgentMode.WORKING
    assert visible(replace(restored, goal_status="complete"), 300).mode == AgentMode.COMPLETED


def test_remote_stream_refreshes_goal_without_new_turn(monkeypatch, tmp_path):
    path = tmp_path / "codex.jsonl"
    path.write_text(json.dumps({"logged_at": NOW.isoformat(), "event": {
        "hook_event_name": "Stop", "session_id": "test-goal"}}) + "\n" +
        json.dumps({"event": {"hook_event_name": "SidepulseSummary", "session_id": "test-goal"}}) + "\n" +
        json.dumps({"event": {"hook_event_name": "SubagentStop", "session_id": "test-goal", "agent_id": "child"}}) + "\n")
    states = {"test-goal": "active"}
    monkeypatch.setattr(remote_hosts, "goal_states", lambda: dict(states))
    monkeypatch.setattr(remote_hosts, "detect_log_path", lambda provider: path)
    monkeypatch.setattr(remote_hosts, "remote_session_web_link", lambda *args: None)
    def tick(seconds):
        if states["test-goal"] == "complete":
            raise KeyboardInterrupt
        states["test-goal"] = "complete"
    monkeypatch.setattr(remote_hosts.time, "sleep", tick)
    output = io.StringIO()
    assert remote_hosts.stream_remote_events(["codex"], output=output) == 0
    lines = [json.loads(line)["line"] for line in output.getvalue().splitlines()]
    assert lines[0]["event"]["sidepulse_goal_status"] == "active"
    assert lines[-1]["event"]["sidepulse_goal_status"] == "complete"
    assert lines[-1]["event"]["hook_event_name"] == "Stop"
    assert lines[-1]["logged_at"] == NOW.isoformat()
