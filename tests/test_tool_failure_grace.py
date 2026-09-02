from datetime import datetime, timedelta, timezone

from sidepulse.collector import (
    TOOL_FAILURE_GRACE_SECONDS,
    snapshot_from_statuses,
    status_for_snapshot,
)
from sidepulse.live_activity import compute_alerts
from sidepulse.models import AgentMode, AgentStatus


def make_status(
    *,
    mode: AgentMode,
    event_name: str,
    updated_at: datetime,
) -> AgentStatus:
    return AgentStatus(
        provider="codex",
        agent_id="codex:session:test-session",
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


def test_tool_failure_stays_working_during_grace() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUseFailure",
        updated_at=now - timedelta(seconds=TOOL_FAILURE_GRACE_SECONDS - 0.1),
    )

    assert effective(status, now).mode == AgentMode.WORKING


def test_failed_codex_tool_output_also_uses_grace() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUse",
        updated_at=now - timedelta(seconds=1),
    )

    assert effective(status, now).mode == AgentMode.WORKING


def test_persistent_tool_failure_surfaces_after_grace() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUseFailure",
        updated_at=now - timedelta(seconds=TOOL_FAILURE_GRACE_SECONDS),
    )

    assert effective(status, now).mode == AgentMode.BLOCKED_ERROR


def test_non_tool_blocker_remains_immediate() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PermissionDenied",
        updated_at=now - timedelta(seconds=1),
    )

    assert effective(status, now).mode == AgentMode.BLOCKED_ERROR


def test_snapshot_aggregate_uses_grace_state() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status = make_status(
        mode=AgentMode.BLOCKED_ERROR,
        event_name="PostToolUseFailure",
        updated_at=now - timedelta(seconds=1),
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


def test_grace_suppresses_blocked_alert_until_failure_persists() -> None:
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

    blocked_status = effective(
        failed,
        now + timedelta(seconds=TOOL_FAILURE_GRACE_SECONDS),
    )
    alerts, blocked_modes = compute_alerts(
        grace_modes,
        [blocked_status],
        now.timestamp() + TOOL_FAILURE_GRACE_SECONDS,
        {},
    )
    assert [alert["kind"] for alert in alerts] == [AgentMode.BLOCKED_ERROR.value]
    assert blocked_modes[failed.agent_id] == AgentMode.BLOCKED_ERROR.value
