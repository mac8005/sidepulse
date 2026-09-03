from __future__ import annotations

import json
from datetime import datetime, timezone

from sidepulse.collector import AgentMonitor, SourceSpec
from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.providers import parse_log_line
from sidepulse.title_integrity import (
    is_readable_session_title,
    normalize_user_request,
)


def _config(tmp_path):
    return LiveActivityConfig(
        apns_key_path=tmp_path / "key.p8",
        apns_key_id="key",
        apns_team_id="team",
        summaries_enabled=False,
    )


def test_request_normalization_is_root_scoped_and_unwraps_real_requests():
    notification = (
        "<task-notification><task-id>bx6eq6gmh</task-id>"
        "<output-file>/private/tmp/claude-501/run/tasks/bx6eq6gmh.output"
        "</output-file></task-notification>"
    )
    assert normalize_user_request(notification) is None
    assert normalize_user_request(
        '<cross-session-message sender="worker">internal state</cross-session-message>'
    ) is None
    assert normalize_user_request(
        "<codex_delegation><input>Improve session titles</input>"
        "<task-id>abc</task-id></codex_delegation>"
    ) == "Improve session titles"
    assert normalize_user_request(
        "<realtime_delegation><input>Fix the LED controls</input>"
        "</realtime_delegation>"
    ) == "Fix the LED controls"
    assert normalize_user_request("<task>Run focused tests</task>") == "Run focused tests"
    assert normalize_user_request(
        "<task>You are delegated a bounded subtask."
        "<input>Run focused tests</input></task>"
    ) == "Run focused tests"

    discussion = "Explain why <task-notification> appears in XML."
    assert normalize_user_request(discussion) == discussion
    assert is_readable_session_title("Explain task notification XML; working")


def test_collector_replay_ignores_a_poisoned_old_summary(tmp_path):
    session_id = "e7f63d8c-3c40-55ef-8633-cd0946cadaac"
    events = [
        {
            "logged_at": "2026-09-03T10:00:00Z",
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "cwd": "/Users/x/Git/sidepulse",
            "prompt": "Sleep for 20s then ask me to choose an option",
        },
        {
            "logged_at": "2026-09-03T10:00:01Z",
            "hook_event_name": "SidepulseSummary",
            "session_id": session_id,
            "summary": "SidePulse: Wait, then ask for a choice; working",
        },
        {
            "logged_at": "2026-09-03T10:00:02Z",
            "hook_event_name": "SidepulseSummary",
            "session_id": session_id,
            "summary": (
                "<task-notification> <task-id>bx6eq6gmh</task-id> "
                "<tool-use-id>toolu_123</tool-use-id>; completed"
            ),
        },
        {
            "logged_at": "2026-09-03T10:00:03Z",
            "hook_event_name": "Stop",
            "session_id": session_id,
        },
    ]
    log = tmp_path / "claude.jsonl"
    log.write_text("".join(json.dumps(event) + "\n" for event in events))

    snapshot = AgentMonitor(
        sources=(SourceSpec("claude", log),),
        stale_after_seconds=999999999,
        completed_visible_seconds=-1,
    ).snapshot()

    assert len(snapshot.statuses) == 1
    assert "Wait, then ask for a choice" in snapshot.statuses[0].display_name
    assert "task-notification" not in snapshot.statuses[0].display_name


def test_bad_generated_title_falls_back_and_cannot_be_published(tmp_path, monkeypatch):
    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    daemon = LiveActivityDaemon(
        _config(tmp_path), token_store=TokenStore(tmp_path / "tokens.json")
    )
    log = tmp_path / "claude.jsonl"
    daemon.monitor.sources = (SourceSpec("claude", log),)
    daemon._prompt_tracker._prompts["s1"] = (
        "Sleep for 20s then ask me to choose an option"
    )

    class PoisonedSummarizer:
        def summary_for(self, *args, **kwargs):
            return "<function_calls><tool_calls>; working"

    daemon.summarizer = PoisonedSummarizer()
    status = AgentStatus(
        provider="claude",
        agent_id="claude:session:s1",
        display_name="Previous readable title",
        mode=AgentMode.WORKING,
        updated_at=datetime.now(timezone.utc),
        event_name="PreToolUse",
        session_id="s1",
        cwd="/Users/x/Git/sidepulse",
    )

    repaired = daemon._apply_summary(status)

    assert repaired.display_name == (
        "SidePulse: Sleep for 20s then ask me to choose an option; working"
    )
    records = log.read_text().splitlines()
    assert len(records) == 1
    published = parse_log_line("claude", records[0])
    assert published is not None
    assert published.raw["summary"] == repaired.display_name
    daemon._publish_summary(status, "<tool_calls>; working")
    assert log.read_text().splitlines() == records


def test_loading_poisoned_finished_row_uses_a_human_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    (tmp_path / "recent_finished.json").write_text(
        json.dumps(
            {
                "claude:session:e7f63d8c": {
                    "id": "claude:session:e7f63d8c",
                    "name": (
                        "<task-notification> <task-id>bx6eq6gmh</task-id> "
                        "/private/tmp/claude-501/run/tasks/bx6eq6gmh.output; completed"
                    ),
                    "mode": "completed",
                    "provider": "claude",
                    "cwd": "/Users/x/Git/sidepulse",
                    "finishedAt": 1.0,
                }
            }
        )
    )

    daemon = LiveActivityDaemon(
        _config(tmp_path), token_store=TokenStore(tmp_path / "tokens.json")
    )

    assert daemon._recent_finished["claude:session:e7f63d8c"]["name"] == (
        "SidePulse: Claude task; completed"
    )
