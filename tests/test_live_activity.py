from __future__ import annotations

import json
from datetime import datetime, timezone

from sidepulse.live_activity import (
    MAX_AGENT_ROWS,
    TokenStore,
    build_content_state,
)
from sidepulse.models import AgentMode, AgentStatus


def make_status(
    agent_id: str,
    mode: AgentMode,
    name: str = "",
    tool: str | None = None,
    session_id: str | None = None,
) -> AgentStatus:
    return AgentStatus(
        provider="claude",
        agent_id=agent_id,
        display_name=name or agent_id,
        mode=mode,
        updated_at=datetime.now(timezone.utc),
        event_name="PreToolUse",
        tool_name=tool,
        session_id=session_id,
    )


def test_content_state_orders_by_mode_priority_and_caps_rows():
    statuses = [make_status(f"work-{i}", AgentMode.WORKING) for i in range(MAX_AGENT_ROWS)]
    statuses.append(make_status("blocked", AgentMode.BLOCKED_ERROR))
    state = build_content_state(statuses, aggregate_mode="blocked_error")

    # activeCount counts non-terminal sessions; rows cap at MAX_AGENT_ROWS.
    assert state["activeCount"] == MAX_AGENT_ROWS + 1
    assert len(state["agents"]) == MAX_AGENT_ROWS
    assert state["agents"][0]["id"] == "blocked"
    assert state["aggregateMode"] == "blocked_error"


def test_content_state_appends_recent_finished_without_duplicates():
    working = make_status("a", AgentMode.WORKING, name="Running")
    finished = [
        {"id": "gone", "name": "Old Task", "mode": "completed", "detail": None,
         "provider": "claude", "cwd": "repo", "finishedAt": 1000.0},
        {"id": "a", "name": "Running", "mode": "completed", "detail": None,
         "provider": "claude", "cwd": "repo", "finishedAt": 2000.0},
    ]
    state = build_content_state([working], aggregate_mode="working", recent_finished=finished)

    assert state["activeCount"] == 1
    ids = [row["id"] for row in state["agents"]]
    # The active row wins; its stale finished entry is not duplicated.
    assert ids == ["a", "gone"]
    assert state["agents"][1]["mode"] == "completed"


def test_finished_rows_dedupe_by_name_for_reconnected_sessions():
    running = make_status("codex:session:new", AgentMode.WORKING, name="Kleido: rework")
    finished = [
        {"id": "codex:session:old", "name": "Kleido: rework", "mode": "completed",
         "detail": None, "provider": "codex", "cwd": "Git", "finishedAt": 1000.0},
    ]
    state = build_content_state([running], aggregate_mode="working", recent_finished=finished)
    assert [row["id"] for row in state["agents"]] == ["codex:session:new"]


def test_content_state_truncates_long_fields_and_serializes():
    status = make_status("a", AgentMode.TOOL_RUNNING, name="x" * 200, tool="y" * 200)
    state = build_content_state([status], aggregate_mode="tool_running")

    row = state["agents"][0]
    assert len(row["name"]) <= 121
    assert len(row["detail"]) <= 33
    # The whole payload must stay well under the 4 KB APNs content-state cap.
    assert len(json.dumps(state)) < 2000


def test_token_store_round_trip(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.register("push_to_start", "aa11", {"device": "iPhone"})
    store.register("update", "bb22", {"device": "iPhone", "activity_id": "A1"})

    reloaded = TokenStore(tmp_path / "tokens.json")
    assert reloaded.tokens("push_to_start") == ["aa11"]
    assert reloaded.tokens("update") == ["bb22"]

    reloaded.drop("update", "bb22")
    assert reloaded.tokens("update") == []
    assert TokenStore(tmp_path / "tokens.json").tokens("update") == []


def test_compute_alerts_only_on_transition_with_cooldown():
    from sidepulse.live_activity import compute_alerts

    working = make_status("a", AgentMode.WORKING, name="Session A")
    waiting = make_status("a", AgentMode.WAITING_FOR_INPUT, name="Session A")
    last_alerts: dict[tuple[str, str], float] = {}

    # First tick after restart: seeds state, never alerts.
    alerts, modes = compute_alerts({}, [waiting], now=100.0, last_alerts=last_alerts)
    assert alerts == []

    # working -> waiting transitions and alerts once.
    alerts, modes = compute_alerts({"a": "working"}, [waiting], now=200.0, last_alerts=last_alerts)
    assert len(alerts) == 1
    assert "Needs your input" in alerts[0]["title"]
    assert "Session A" in alerts[0]["title"]

    # Flapping back within the cooldown stays silent.
    alerts, modes = compute_alerts(modes, [working], now=210.0, last_alerts=last_alerts)
    assert alerts == []
    alerts, modes = compute_alerts(modes, [waiting], now=220.0, last_alerts=last_alerts)
    assert alerts == []

    # After the cooldown the same transition alerts again.
    alerts, modes = compute_alerts({"a": "working"}, [waiting], now=400.0, last_alerts=last_alerts)
    assert len(alerts) == 1


def test_compute_alerts_completed_and_blocked():
    from sidepulse.live_activity import compute_alerts

    done = make_status("b", AgentMode.COMPLETED, name="Deploy")
    blocked = make_status("c", AgentMode.BLOCKED_ERROR, name="Tests", tool="pytest")
    alerts, _ = compute_alerts(
        {"b": "working", "c": "tool_running"}, [done, blocked], now=50.0, last_alerts={}
    )
    titles = sorted(alert["title"] for alert in alerts)
    assert any("Finished" in title for title in titles)
    assert any("Blocked" in title for title in titles)
    blocked_alert = next(alert for alert in alerts if "Blocked" in alert["title"])
    assert blocked_alert["body"] == "pytest"


def test_finished_waits_for_subagents():
    from sidepulse.live_activity import compute_alerts

    main_done = make_status(
        "claude:session:s1", AgentMode.COMPLETED, name="Big Task", session_id="s1"
    )
    subagent = make_status(
        "claude:agent:sub1", AgentMode.TOOL_RUNNING, name="Subtask", session_id="s1"
    )
    prev = {"claude:session:s1": "working", "claude:agent:sub1": "tool_running"}
    last_alerts: dict[tuple[str, str], float] = {}

    # Main session completed but a subagent still runs: no Finished alert.
    alerts, modes = compute_alerts(prev, [main_done, subagent], now=100.0, last_alerts=last_alerts)
    assert alerts == []

    # Subagent finishes too: exactly one Finished alert, named after the session.
    sub_done = make_status(
        "claude:agent:sub1", AgentMode.COMPLETED, name="Subtask", session_id="s1"
    )
    alerts, modes = compute_alerts(modes, [main_done, sub_done], now=200.0, last_alerts=last_alerts)
    assert len(alerts) == 1
    assert alerts[0]["title"] == "Finished: Big Task"


def test_background_and_default_ignored_sessions_are_filtered(monkeypatch, tmp_path):
    from sidepulse.collector import StatusMetadata, should_ignore_record
    from sidepulse.models import HookEvent

    def record(cwd, *, background=False):
        return HookEvent(
            provider="claude",
            logged_at=datetime.now(timezone.utc),
            event_name="PreToolUse",
            raw={"sidepulse_background_session": background},
            session_id="s1",
            cwd=cwd,
        )

    meta = StatusMetadata(cwd=None)
    assert should_ignore_record(record("/Users/x/.claude/memories"), meta)
    assert not should_ignore_record(record("/Users/x/Git/aura-server"), meta)
    assert not should_ignore_record(record("/Users/x/Git/aura"), meta)
    assert not should_ignore_record(record("/Users/x/Git/sidepulse"), meta)
    assert should_ignore_record(record("/Users/x/Git", background=True), meta)

    transcript = tmp_path / "headless.jsonl"
    transcript.write_text(json.dumps({"type": "user", "entrypoint": "sdk-cli"}) + "\n")
    headless = HookEvent(
        provider="claude",
        logged_at=datetime.now(timezone.utc),
        event_name="UserPromptSubmit",
        raw={
            "cwd": "/Users/x/Git/aura-server",
            "transcript_path": str(transcript),
        },
        session_id="s2",
        cwd="/Users/x/Git/aura-server",
    )
    assert should_ignore_record(headless, meta)

    # An Aura path alone is not enough to hide a session; launch evidence is.
    assert not should_ignore_record(
        record("/Users/x/Git/aura-server/runs/20260823-120000-routine-inbox"), meta
    )

    monkeypatch.setenv("SIDEPULSE_IGNORE_DIRS", "scratch")
    assert should_ignore_record(record("/tmp/scratch"), meta)


def test_background_marker_removes_an_existing_live_status(tmp_path):
    from sidepulse.collector import LiveAgentMonitor
    from sidepulse.models import HookEvent

    monitor = LiveAgentMonitor(latest_state_path=tmp_path / "latest.json")
    visible = HookEvent(
        provider="claude",
        logged_at=datetime.now(timezone.utc),
        event_name="UserPromptSubmit",
        raw={"prompt": "Interactive task"},
        session_id="s1",
        cwd="/Users/x/Git/aura",
    )
    monitor.ingest_record(visible)
    assert "claude:session:s1" in monitor.statuses_by_key

    background = HookEvent(
        provider="claude",
        logged_at=datetime.now(timezone.utc),
        event_name="PreToolUse",
        raw={"sidepulse_background_session": True},
        session_id="s1",
        cwd="/Users/x/Git/aura",
    )
    monitor.ingest_record(background)
    assert "claude:session:s1" not in monitor.statuses_by_key


def test_ignored_display_name_prefix():
    from sidepulse.collector import is_ignored_display_name

    assert not is_ignored_display_name("aura-server: You are an autonomous agent")
    assert not is_ignored_display_name("aura: Process the family inbox")
    assert is_ignored_display_name("memories: Memory Writing Agent")
    assert not is_ignored_display_name("sidepulse: Merge main")


def test_structure_signature_ignores_text_churn():
    from sidepulse.live_activity import _structure_signature

    base = {
        "aggregateMode": "working",
        "activeCount": 1,
        "agents": [
            {"id": "a", "mode": "working", "name": "doing X", "detail": "Bash"},
            {"id": "b", "mode": "completed", "name": "done Y", "unread": True},
        ],
        "updatedAt": 1.0,
    }
    renamed = {**base, "agents": [
        {"id": "a", "mode": "working", "name": "doing Z", "detail": "Read"},
        {"id": "b", "mode": "completed", "name": "done Y", "unread": True},
    ]}
    seen = {**base, "agents": [
        base["agents"][0],
        {"id": "b", "mode": "completed", "name": "done Y", "unread": False},
    ]}
    assert _structure_signature(base) == _structure_signature(renamed)
    assert _structure_signature(base) != _structure_signature(seen)


def test_finished_rows_track_unread_until_marked_seen(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore, build_content_state

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))

    done = make_status("claude:session:s1", AgentMode.COMPLETED, name="T", session_id="s1")
    daemon._remember_finished([done], now=100.0)
    row = daemon._recent_finished["claude:session:s1"]
    assert row["unread"] is True

    # Re-remembering the same completed session must not reset read state.
    assert daemon.mark_finished_seen("claude:session:s1") is True
    daemon._remember_finished([done], now=110.0)
    assert daemon._recent_finished["claude:session:s1"]["unread"] is False
    assert daemon.mark_finished_seen("claude:session:s1") is False  # already read
    assert daemon.mark_finished_seen("missing") is False

    # The wire rows carry the flag for every surface.
    state = build_content_state([], "idle_ready", recent_finished=list(daemon._recent_finished.values()))
    assert state["agents"][0]["unread"] is False


def test_publish_summary_writes_hook_records(tmp_path, monkeypatch):
    from sidepulse.collector import SourceSpec
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore
    from sidepulse.providers import parse_log_line

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    claude_log = tmp_path / "claude.jsonl"
    codex_log = tmp_path / "codex.jsonl"
    daemon.monitor.sources = (
        SourceSpec("claude", claude_log),
        SourceSpec("codex", codex_log),
    )

    status = make_status("claude:session:s1", AgentMode.COMPLETED, session_id="s1")
    daemon._publish_summary(status, "proj: parser fixed")
    daemon._publish_summary(status, "proj: parser fixed")  # dedup: no second line

    lines = claude_log.read_text().splitlines()
    assert len(lines) == 1
    record = parse_log_line("claude", lines[0])
    assert record is not None
    assert record.event_name == "SidepulseSummary"
    assert record.session_id == "s1"
    assert record.raw.get("summary") == "proj: parser fixed"

    # A changed summary publishes again.
    daemon._publish_summary(status, "proj: tests added")
    assert len(claude_log.read_text().splitlines()) == 2

    # Codex records use the enveloped log format.
    codex_status = type(status)(**{**status.__dict__, "provider": "codex", "session_id": "c1"})
    daemon._publish_summary(codex_status, "kleido: import flow reworked")
    codex_record = parse_log_line("codex", codex_log.read_text().splitlines()[0])
    assert codex_record is not None
    assert codex_record.event_name == "SidepulseSummary"
    assert codex_record.session_id == "c1"
    assert codex_record.raw.get("summary") == "kleido: import flow reworked"


def test_push_to_start_retries_until_activity_registers(tmp_path, monkeypatch):
    from sidepulse.live_activity import (
        PUSH_TO_START_COOLDOWN_SECONDS,
        LiveActivityConfig,
        LiveActivityDaemon,
        TokenStore,
    )

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("push_to_start", "p2s", {"device": "phone", "activity_id": ""})
    sent = []
    monkeypatch.setattr(daemon, "_apns_fanout", lambda kind, payload, priority=10: sent.append(kind))
    state = {"aggregateMode": "working", "activeCount": 1, "agents": [], "updatedAt": 0.0}

    daemon._maybe_push_to_start(state, 1000.0)
    assert sent == ["push_to_start"]
    # A sent start push is not a started activity.
    assert daemon._activity_live is False

    daemon._maybe_push_to_start(state, 1000.0 + 5)
    assert sent == ["push_to_start"]  # inside the cooldown

    daemon._maybe_push_to_start(state, 1000.0 + PUSH_TO_START_COOLDOWN_SECONDS + 1)
    assert sent == ["push_to_start", "push_to_start"]  # keeps retrying

    # Backoff doubles: the third attempt waits twice as long.
    t2 = 1000.0 + PUSH_TO_START_COOLDOWN_SECONDS + 1
    daemon._maybe_push_to_start(state, t2 + PUSH_TO_START_COOLDOWN_SECONDS + 1)
    assert len(sent) == 2
    daemon._maybe_push_to_start(state, t2 + 2 * PUSH_TO_START_COOLDOWN_SECONDS + 1)
    assert len(sent) == 3


def test_moonside_marker_follows_background_tasks(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    monkeypatch.setenv("MOONSIDE_RUNTIME_DIR", str(tmp_path))
    sessions = tmp_path / "moonside_sessions"
    sessions.mkdir()
    (sessions / "s1").write_text("idle\nStop\nturn\n/tmp/t.jsonl\n")

    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))

    # Stop classified as long-task (running background tasks): marker flips.
    holding = make_status(
        "claude:session:s1", AgentMode.LONG_TASK_PROGRESS, name="T", session_id="s1"
    )
    holding = type(holding)(**{**holding.__dict__, "event_name": "Stop"})
    daemon._sync_background_tasks([holding], now=100.0)
    assert (sessions / "s1").read_text().splitlines()[0] == "working"

    # Tasks close (session completes): marker restored.
    done = make_status("claude:session:s1", AgentMode.COMPLETED, name="T", session_id="s1")
    daemon._sync_background_tasks([done], now=200.0)
    assert (sessions / "s1").read_text().splitlines()[0] == "idle"
    assert daemon._bg_holding == set()

    # A real hook write (user resumed) is never clobbered.
    (sessions / "s1").write_text("working\nUserPromptSubmit\nturn\n/tmp/t.jsonl\n")
    daemon._sync_background_tasks([holding], now=300.0)
    daemon._sync_background_tasks([done], now=400.0)
    assert (sessions / "s1").read_text().splitlines()[0] == "working"


def test_stop_with_running_background_tasks_is_long_task():
    from sidepulse.collector import mode_for_event
    from sidepulse.models import HookEvent

    def stop(background):
        return HookEvent(
            provider="claude",
            logged_at=datetime.now(timezone.utc),
            event_name="Stop",
            raw={"background_tasks": background},
            session_id="s1",
        )

    running = [{"id": "b1", "type": "shell", "status": "running"}]
    assert mode_for_event(stop(running)) == AgentMode.LONG_TASK_PROGRESS
    assert mode_for_event(stop([])) == AgentMode.COMPLETED
    assert mode_for_event(stop([{"id": "b1", "status": "completed"}])) == AgentMode.COMPLETED


def test_summarizer_replaces_display_name(tmp_path, monkeypatch):
    import time as _time

    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 'sidepulse: TestFlight build deployed'\n")
    fake.chmod(0o755)

    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.summarizer.claude = str(fake)

    done = make_status("claude:session:s1", AgentMode.COMPLETED, name="old prompt", session_id="s1")
    done = type(done)(**{**done.__dict__, "event_name": "Stop",
                         "message": "The build is on TestFlight.", "cwd": "/Users/x/Git/sidepulse"})

    # First pass queues generation; poll until the worker finishes.
    daemon._apply_summary(done)
    for _ in range(50):
        result = daemon._apply_summary(done)
        if result.display_name != "old prompt":
            break
        _time.sleep(0.1)
    assert result.display_name == "sidepulse: TestFlight build deployed"

    # Working sessions keep their prompt-based name.
    busy = make_status("claude:session:s2", AgentMode.WORKING, name="prompt", session_id="s2")
    assert daemon._apply_summary(busy).display_name == "prompt"


def test_recent_finished_keeps_newest_three_beyond_window(tmp_path, monkeypatch):
    from sidepulse.live_activity import (
        LiveActivityConfig,
        LiveActivityDaemon,
        RECENT_FINISHED_SECONDS,
        TokenStore,
    )

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "t.json"))

    now = 100000.0
    # Five finished sessions, all older than the expiry window.
    for i in range(5):
        daemon._recent_finished[f"s{i}"] = {
            "id": f"s{i}", "name": f"S{i}", "mode": "completed",
            "finishedAt": now - RECENT_FINISHED_SECONDS - 1000 + i,
        }
    daemon._remember_finished([], now)

    # The three newest survive despite being past the window; older ones drop.
    survivors = set(daemon._recent_finished)
    assert survivors == {"s2", "s3", "s4"}
