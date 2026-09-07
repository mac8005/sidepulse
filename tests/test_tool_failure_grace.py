from datetime import datetime, timedelta, timezone

import pytest

from sidepulse.collector import (
    LiveAgentMonitor,
    snapshot_from_statuses,
    status_for_snapshot,
)
from sidepulse.live_activity import compute_alerts
from sidepulse.models import AgentMode, AgentStatus, HookEvent


def make_status(
    *,
    mode: AgentMode,
    event_name: str,
    updated_at: datetime,
    provider: str = "codex",
) -> AgentStatus:
    return AgentStatus(
        provider=provider,
        agent_id=f"{provider}:session:test-session",
        display_name="SidePulse: test error grace",
        mode=mode,
        updated_at=updated_at,
        event_name=event_name,
        session_id="test-session",
        cwd="/tmp/sidepulse",
        tool_name="Bash",
    )


def effective(status: AgentStatus, now: datetime) -> AgentStatus:
    return status_for_snapshot(
        status,
        now,
        post_tool_working_visible_seconds=0,
    )


def test_tool_failure_stays_working() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUseFailure",
        updated_at=now - timedelta(seconds=9.9),
    )

    assert effective(status, now).mode == AgentMode.WORKING


def test_failed_codex_tool_output_stays_working() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUse",
        updated_at=now - timedelta(seconds=1),
    )

    assert effective(status, now).mode == AgentMode.WORKING


@pytest.mark.parametrize("provider", ["codex", "claude", "grok"])
@pytest.mark.parametrize("event_name", ["PostToolUse", "PostToolUseFailure", "PermissionDenied"])
@pytest.mark.parametrize("age", [10, 30, 120, 900, 1800])
def test_elapsed_time_never_promotes_tool_failure_to_blocked(provider, event_name, age) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name=event_name,
        updated_at=now - timedelta(seconds=age),
        provider=provider,
    )

    visible = effective(status, now)
    assert visible.mode == AgentMode.WORKING
    assert visible.event_name == event_name
    assert visible.updated_at == status.updated_at
    assert status.mode == AgentMode.BLOCKED_ERROR


def test_non_tool_blocker_remains_immediate() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="StopFailure",
        updated_at=now - timedelta(seconds=1),
    )

    assert effective(status, now).mode == AgentMode.BLOCKED_ERROR


def test_snapshot_aggregate_keeps_recovering_session_working() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUseFailure",
        updated_at=now - timedelta(seconds=30),
    )

    snapshot = snapshot_from_statuses(
        (status,),
        sources=(),
        collected_at=now,
        stale_after_seconds=3600,
        tool_running_timeout_seconds=0,
        completed_visible_seconds=20 * 60,
        idle_visible_seconds=0,
        post_tool_working_visible_seconds=0,
    )

    assert snapshot.statuses[0].mode == AgentMode.WORKING
    assert snapshot.aggregate.mode == AgentMode.WORKING


def test_tool_failure_alerts_only_when_input_is_actually_requested() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    failed = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUseFailure",
        updated_at=now,
    )
    previous_modes = {
        failed.agent_id: AgentMode.WORKING.value,
        f"group:{failed.provider}:{failed.session_id}": "active",
    }

    grace_status = effective(failed, now + timedelta(seconds=1))
    alerts, grace_modes = compute_alerts(
        previous_modes,
        [grace_status],
        now.timestamp() + 1,
        {},
    )
    assert alerts == []
    assert grace_modes[failed.agent_id] == AgentMode.WORKING.value

    recovering_status = effective(
        failed,
        now + timedelta(seconds=600),
    )
    alerts, recovering_modes = compute_alerts(
        grace_modes,
        [recovering_status],
        now.timestamp() + 600,
        {},
    )
    assert alerts == []
    assert recovering_modes[failed.agent_id] == AgentMode.WORKING.value

    waiting = make_status(
        mode=AgentMode.WAITING_FOR_INPUT,
        event_name="PermissionRequest",
        updated_at=now + timedelta(seconds=601),
    )
    alerts, _ = compute_alerts(recovering_modes, [waiting], now.timestamp() + 601, {})
    assert [alert["kind"] for alert in alerts] == [AgentMode.WAITING_FOR_INPUT.value]


@pytest.mark.parametrize("resolution", ["PostToolUseFailure", "PermissionDenied"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_resolved_permission_does_not_hide_recovery_or_completion(resolution, provider) -> None:
    monitor = LiveAgentMonitor()
    for event_name, mode in (
        ("PermissionRequest", AgentMode.WAITING_FOR_INPUT),
        (resolution, AgentMode.WORKING),
        ("PreToolUse", AgentMode.TOOL_RUNNING),
        ("Stop", AgentMode.COMPLETED),
    ):
        monitor.ingest_record(HookEvent(
            provider=provider,
            logged_at=datetime.now(timezone.utc),
            event_name=event_name,
            raw={"tool_name": "Bash", "tool_input": {"command": "git fetch"}},
            session_id="test-session",
            tool_name="Bash",
        ))
        assert monitor.snapshot().statuses[0].mode == mode


def test_agent_that_stops_to_ask_for_help_still_needs_input() -> None:
    monitor = LiveAgentMonitor()
    for event_name, raw, expected in (
        ("PostToolUseFailure", {}, AgentMode.WORKING),
        ("Stop", {"last_assistant_message": "Can you confirm which account I should use?"}, AgentMode.WAITING_FOR_INPUT),
    ):
        monitor.ingest_record(HookEvent(
            provider="claude", logged_at=datetime.now(timezone.utc),
            event_name=event_name, raw=raw, session_id="test-session",
        ))
        assert monitor.snapshot().statuses[0].mode == expected
