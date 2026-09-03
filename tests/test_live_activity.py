from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from sidepulse.live_activity import (
    MAX_AGENT_ROWS,
    TokenStore,
    build_content_state,
    status_row,
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


def test_content_state_compacts_mobile_project_labels_without_mutating_statuses():
    statuses = [
        make_status(
            "trader",
            AgentMode.WORKING,
            name="CSPennyScalpingTrader: Tune entry rules; working",
        ),
        make_status(
            "scaler",
            AgentMode.WORKING,
            name="CSPennyScaler: Review fills; working",
        ),
        make_status(
            "other",
            AgentMode.WORKING,
            name="SidePulse: Improve titles; working",
        ),
    ]
    canonical_rows = {status.agent_id: status_row(status) for status in statuses}

    state = build_content_state(statuses, aggregate_mode="working")
    mobile_rows = {row["id"]: row for row in state["agents"]}

    assert mobile_rows["trader"]["name"] == "Trading: Tune entry rules; working"
    assert mobile_rows["scaler"]["name"] == "Trading: Review fills; working"
    assert mobile_rows["other"]["name"] == "SidePulse: Improve titles; working"
    assert statuses[0].display_name == (
        "CSPennyScalpingTrader: Tune entry rules; working"
    )
    assert canonical_rows["trader"]["name"] == statuses[0].display_name
    assert status_row(statuses[1])["name"] == statuses[1].display_name


def test_content_state_compacts_recent_rows_before_dedupe_without_mutating_inputs():
    running = make_status(
        "current",
        AgentMode.WORKING,
        name="CSPennyScalpingTrader: Review fills",
    )
    finished = [
        {
            "id": "duplicate",
            "name": "CSPennyScaler: Review fills",
            "mode": "completed",
            "finishedAt": 3.0,
        },
        {
            "id": "finished-trading",
            "name": "CSPennyScaler: Reconcile positions; completed",
            "mode": "completed",
            "finishedAt": 2.0,
        },
        {
            "id": "finished-other",
            "name": "Aura: Calendar repaired; completed",
            "mode": "completed",
            "finishedAt": 1.0,
        },
    ]
    canonical_finished = json.loads(json.dumps(finished))

    state = build_content_state(
        [running], aggregate_mode="working", recent_finished=finished
    )
    mobile_rows = {row["id"]: row for row in state["agents"]}

    assert "duplicate" not in mobile_rows
    assert mobile_rows["finished-trading"]["name"] == (
        "Trading: Reconcile positions; completed"
    )
    assert mobile_rows["finished-other"]["name"] == (
        "Aura: Calendar repaired; completed"
    )
    assert finished == canonical_finished


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
    store.register("dot_device", "cc33", {"device": "iPhone"})

    reloaded = TokenStore(tmp_path / "tokens.json")
    assert reloaded.tokens("push_to_start") == ["aa11"]
    assert reloaded.tokens("update") == ["bb22"]
    assert reloaded.tokens("dot_device") == ["cc33"]
    assert reloaded.replace("dot_device", "dd44", {"device": "new iPhone"}) is True
    assert reloaded.replace("dot_device", "dd44", {"device": "new iPhone"}) is False
    assert reloaded.tokens("dot_device") == ["dd44"]

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
    assert blocked_alert["kind"] == "blocked_error"
    assert next(alert for alert in alerts if "Finished" in alert["title"])["kind"] == "completed"


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


def test_rate_limited_structural_change_still_pushes(tmp_path, monkeypatch):
    # A structural change skipped for rate limiting must stay pending and go
    # out on a later tick. Comparing against the last computed state instead
    # of the last pushed one silently dropped it (a cleared unread badge
    # then lingered until the next unrelated change or the heartbeat).
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("update", "upd", {"device": "phone", "activity_id": "a"})
    pushes = []
    monkeypatch.setattr(
        daemon,
        "_apns_fanout",
        lambda kind, payload, priority=10: (pushes.append(priority) or True),
    )

    def state(unread: bool, name: str = "done Y") -> dict:
        return {
            "aggregateMode": "idle_ready",
            "activeCount": 0,
            "agents": [{"id": "b", "mode": "completed", "name": name, "unread": unread}],
            "updatedAt": 0.0,
        }

    daemon._push_update(state(True), now=1000.0)
    assert pushes == [10]

    # The unread clear lands inside the minimum interval: nothing goes out...
    daemon._last_push_at = 1000.0
    assert daemon._differs_from_pushed(state(False)) is True

    # ...and the very next tick must still see it as pending and push it.
    daemon._push_update(state(False), now=1000.5)
    assert pushes == [10, 10]
    assert daemon._differs_from_pushed(state(False)) is False


def test_reregistering_push_to_start_reopens_the_start_cap(tmp_path, monkeypatch):
    # iOS drops start pushes while an app stays force-quit, so those attempts
    # burn the cap. Launching the app re-registers its (unchanged) token, and
    # that evidence must reopen the cap or the activity never comes back.
    from sidepulse.live_activity import (
        MAX_UNANSWERED_START_PUSHES,
        LiveActivityConfig,
        LiveActivityDaemon,
        TokenStore,
    )

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("push_to_start", "p2s", {"device": "phone", "activity_id": ""})
    daemon._start_push_attempts = MAX_UNANSWERED_START_PUSHES

    sent = []
    monkeypatch.setattr(daemon, "_apns_fanout", lambda kind, payload, priority=10: sent.append(kind))
    state = {"aggregateMode": "working", "activeCount": 1, "agents": [], "updatedAt": 0.0}

    daemon._maybe_push_to_start(state, 10_000.0)
    assert sent == [], "capped: no more activities may be stacked"

    # What the register handler does for an already-known token.
    daemon._start_push_attempts = 0
    daemon._maybe_push_to_start(state, 10_000.0)
    assert sent == ["push_to_start"]


def test_shrink_payload_fits_oversized_pushes():
    from sidepulse.live_activity import APNS_PAYLOAD_LIMIT_BYTES, shrink_payload
    import json as _json

    small = {"aps": {"content-state": {"agents": [{"id": "a", "name": "short"}]}}}
    assert shrink_payload(small) is small  # untouched

    big = {
        "aps": {
            "event": "update",
            "content-state": {
                "aggregateMode": "working",
                "activeCount": 9,
                "agents": [
                    {
                        "id": f"claude:session:{i}",
                        "name": "A very long session summary that keeps going " * 4,
                        "mode": "working",
                        "detail": "Bash",
                        "cwd": "some/deep/directory",
                        "deepLink": "https://claude.ai/chat/" + "x" * 60,
                    }
                    for i in range(20)
                ],
                "updatedAt": 1.0,
            },
        }
    }
    assert len(_json.dumps(big, separators=(",", ":")).encode()) > APNS_PAYLOAD_LIMIT_BYTES

    fitted = shrink_payload(big)
    assert len(_json.dumps(fitted, separators=(",", ":")).encode()) <= APNS_PAYLOAD_LIMIT_BYTES
    state = fitted["aps"]["content-state"]
    assert state["aggregateMode"] == "working"  # structure survives
    assert state["agents"], "some rows must survive"
    assert big["aps"]["content-state"]["agents"][0].get("deepLink")  # original untouched


def test_apns_send_retries_transient_transport_errors(monkeypatch):
    import httpx
    from sidepulse.live_activity import APNsLiveActivityClient, LiveActivityConfig
    from pathlib import Path as _Path

    config = LiveActivityConfig(apns_key_path=_Path("/tmp/k.p8"), apns_key_id="K", apns_team_id="T")
    client = APNsLiveActivityClient(config)
    monkeypatch.setattr(client, "_token", lambda: "jwt")
    monkeypatch.setattr("sidepulse.live_activity.time.sleep", lambda _s: None)

    attempts = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeClient:
        def post(self, url, json, headers):
            attempts.append(url)
            if len(attempts) < 3:
                raise httpx.ConnectError("[Errno 35] Resource temporarily unavailable")
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeClient())
    status, body = client.send("tok", {"aps": {}})
    assert (status, body) == (200, "ok")
    assert len(attempts) == 3

    # Exhausted retries report the transport error rather than a fake success.
    attempts.clear()

    class AlwaysFails(FakeClient):
        def post(self, url, json, headers):
            attempts.append(url)
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: AlwaysFails())
    status, body = client.send("tok", {"aps": {}})
    assert status == 0 and "boom" in body
    assert len(attempts) == 3


def test_apns_send_supports_persistent_collapsed_dot_headers(monkeypatch):
    from pathlib import Path as _Path

    from sidepulse.live_activity import APNsLiveActivityClient, LiveActivityConfig

    config = LiveActivityConfig(
        apns_key_path=_Path("/tmp/k.p8"), apns_key_id="K", apns_team_id="T"
    )
    client = APNsLiveActivityClient(config)
    monkeypatch.setattr(client, "_token", lambda: "jwt")
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeClient:
        def post(self, url, json, headers):
            captured.update(headers)
            return FakeResponse()

    client._client = FakeClient()
    status, _ = client.send(
        "tok",
        {"aps": {"content-available": 1}},
        priority=5,
        push_type="background",
        topic=config.bundle_id,
        expiration=4600,
        collapse_id="sidepulse-dot-state",
    )

    assert status == 200
    assert captured["apns-priority"] == "5"
    assert captured["apns-expiration"] == "4600"
    assert captured["apns-collapse-id"] == "sidepulse-dot-state"


def _make_dot_daemon(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        port=0,
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("dot_device", "device-token", {"device": "phone"})
    return daemon


def _dot_state(mode="completed", active_count=0, updated_at=100.0):
    return {
        "aggregateMode": mode,
        "activeCount": active_count,
        "agents": [],
        "updatedAt": updated_at,
    }


def _post_json(daemon, path, payload):
    import threading
    from http.client import HTTPConnection

    server = daemon._build_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dot_rejection_stays_pending_and_accepted_delivery_retries_are_bounded(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_PUSH_RETRY_OFFSETS_SECONDS

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    calls = []
    responses = [(500, "server error"), (200, ""), (200, ""), (200, "")]

    def send(token, payload, **kwargs):
        calls.append((payload, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(daemon.apns, "send", send)
    daemon._queue_dot_state("done", _dot_state(), now=100.0)
    command_id = daemon._pending_dot.command_id

    daemon._send_pending_dot_if_due(100.0)
    assert daemon._pending_dot.command_id == command_id
    assert daemon._pending_dot.accepted_attempts == 0

    daemon._send_pending_dot_if_due(159.9)
    assert len(calls) == 1
    daemon._send_pending_dot_if_due(160.0)
    assert daemon._pending_dot.accepted_attempts == 1

    first_retry = 160.0 + DOT_PUSH_RETRY_OFFSETS_SECONDS[1]
    daemon._send_pending_dot_if_due(first_retry - 0.1)
    assert len(calls) == 2
    daemon._send_pending_dot_if_due(first_retry)

    second_retry = first_retry + (
        DOT_PUSH_RETRY_OFFSETS_SECONDS[2] - DOT_PUSH_RETRY_OFFSETS_SECONDS[1]
    )
    daemon._send_pending_dot_if_due(second_retry)
    daemon._send_pending_dot_if_due(second_retry + 1_000)
    assert len(calls) == 4
    assert {call[0]["dot"]["commandID"] for call in calls} == {command_id}
    assert all(call[1]["collapse_id"] == "sidepulse-dot-state" for call in calls)


def test_working_ack_schedules_one_refresh_every_twenty_minutes(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_WORKING_REFRESH_SECONDS

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (sent.append(payload) or (200, "")),
    )
    daemon._queue_dot_state(
        "working", _dot_state("working", active_count=1), now=100.0
    )
    initial_command = daemon._pending_dot.command_id
    assert daemon.ack_dot(initial_command, "written", now=100.0) is True

    due = 100.0 + DOT_WORKING_REFRESH_SECONDS
    assert daemon._send_pending_dot_if_due(due - 0.1) is False
    assert daemon._pending_dot is None
    assert daemon._send_pending_dot_if_due(due) is True
    refresh_command = daemon._pending_dot.command_id
    assert refresh_command != initial_command
    assert sent[-1]["dot"]["commandID"] == refresh_command

    assert daemon._send_pending_dot_if_due(due + 1.0) is False
    assert daemon._pending_dot.command_id == refresh_command
    assert len(sent) == 1

    acknowledged_at = due + 5.0
    assert daemon.ack_dot(refresh_command, "written", now=acknowledged_at) is True
    assert daemon._send_pending_dot_if_due(
        acknowledged_at + DOT_WORKING_REFRESH_SECONDS - 0.1
    ) is False
    assert daemon._send_pending_dot_if_due(
        acknowledged_at + DOT_WORKING_REFRESH_SECONDS
    ) is True
    assert len(sent) == 2


def test_due_working_refresh_waits_for_foreground_stream_disconnect(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_WORKING_REFRESH_SECONDS

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (sent.append(payload) or (200, "")),
    )
    daemon._queue_dot_state(
        "working", _dot_state("working", active_count=1), now=100.0
    )
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=100.0)
    assert daemon._dot_stream_connected("device-token") is True

    due = 100.0 + DOT_WORKING_REFRESH_SECONDS
    assert daemon._send_pending_dot_if_due(due + 300.0) is False
    assert daemon._pending_dot is None
    assert sent == []

    daemon._wake.clear()
    daemon._dot_stream_disconnected("device-token")
    assert daemon._wake.is_set()
    assert daemon._send_pending_dot_if_due(due + 300.0) is True
    assert len(sent) == 1


def test_unacked_working_refresh_exhaustion_does_not_requeue_per_tick(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import (
        DOT_PUSH_EXPIRY_SECONDS,
        DOT_PUSH_RETRY_OFFSETS_SECONDS,
        DOT_WORKING_REFRESH_SECONDS,
    )

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (sent.append(payload) or (200, "")),
    )
    daemon._queue_dot_state(
        "working", _dot_state("working", active_count=1), now=100.0
    )
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=100.0)

    due = 100.0 + DOT_WORKING_REFRESH_SECONDS
    assert daemon._send_pending_dot_if_due(due) is True
    refresh_command = daemon._pending_dot.command_id
    assert daemon._send_pending_dot_if_due(
        due + DOT_PUSH_RETRY_OFFSETS_SECONDS[1]
    ) is True
    assert daemon._send_pending_dot_if_due(
        due + DOT_PUSH_RETRY_OFFSETS_SECONDS[2]
    ) is True
    assert daemon._pending_dot.accepted_attempts == 3

    for moment in (due + 1_201.0, due + DOT_PUSH_EXPIRY_SECONDS + 1.0):
        assert daemon._send_pending_dot_if_due(moment) is False
        assert daemon._pending_dot.command_id == refresh_command
    assert len(sent) == 3
    assert {payload["dot"]["commandID"] for payload in sent} == {refresh_command}


def test_working_refresh_is_disabled_for_unavailable_owner_and_nonworking_state(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_WORKING_REFRESH_SECONDS

    due = 100.0 + DOT_WORKING_REFRESH_SECONDS
    for reason in ("brightness_zero", "dnd", "focus", "disconnected"):
        daemon = _make_dot_daemon(tmp_path / reason, monkeypatch)
        daemon._queue_dot_state(
            "working", _dot_state("working", active_count=1), now=100.0
        )
        assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=100.0)
        assert daemon.report_dot_availability(
            "device-token", False, reason, 3600.0, now=101.0
        )
        assert daemon._send_pending_dot_if_due(due) is False
        assert daemon._pending_dot is None

    for state in ("idle", "ask", "done"):
        daemon = _make_dot_daemon(tmp_path / state, monkeypatch)
        daemon._queue_dot_state(
            "working", _dot_state("working", active_count=1), now=100.0
        )
        assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=100.0)
        daemon._queue_dot_state(state, _dot_state(), now=101.0, force=True)
        assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=101.0)
        assert daemon._send_pending_dot_if_due(due) is False
        assert daemon._pending_dot is None
        assert daemon._observe_dot_state(
            "working",
            _dot_state("working", active_count=1, updated_at=due + 1.0),
            now=due + 1.0,
        ) is False
        assert daemon._send_pending_dot_if_due(due + 1.0) is False
        assert daemon._pending_dot is None

    daemon = _make_dot_daemon(tmp_path / "no-owner", monkeypatch)
    daemon._queue_dot_state(
        "working", _dot_state("working", active_count=1), now=100.0
    )
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=100.0)
    daemon.tokens.clear("dot_device")
    assert daemon._send_pending_dot_if_due(due) is False
    assert daemon._pending_dot is None


def test_dot_push_prefers_the_elected_dot_device(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    daemon.tokens.register("device", "other-phone", {"device": "iPad"})
    daemon.tokens.replace("dot_device", "dot-phone", {"device": "iPhone"})
    tokens = []

    def send(token, payload, **kwargs):
        tokens.append(token)
        return 200, ""

    monkeypatch.setattr(daemon.apns, "send", send)
    daemon._queue_dot_state("done", _dot_state(), now=100.0)

    daemon._send_pending_dot_if_due(100.0)

    assert tokens == ["dot-phone"]


def test_dot_push_does_not_fall_back_to_a_generic_device(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    daemon.tokens.clear("dot_device")
    daemon.tokens.register("device", "generic-phone", {"device": "iPhone"})
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda *args, **kwargs: (sent.append(args[0]) or (200, "")),
    )
    daemon._queue_dot_state("done", _dot_state(), now=100.0)

    assert daemon._send_pending_dot_if_due(100.0) is False
    assert sent == []


def test_dot_unavailable_lease_suppresses_pushes_and_persists(
    tmp_path, monkeypatch
):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda *args, **kwargs: (sent.append(args[0]) or (200, "")),
    )

    assert daemon.report_dot_availability(
        "device-token", False, "focus", 1.0, now=100.0
    )
    daemon._queue_dot_state("working", _dot_state("working", 1), now=100.0)
    pending = daemon._pending_dot

    assert daemon._send_pending_dot_if_due(159.9) is False
    assert sent == []
    assert daemon._pending_dot is pending
    assert pending.accepted_attempts == 0
    assert pending.rejected_attempts == 0
    health = daemon._dot_health(now=100.0)
    assert health["dotPendingState"] == "working"
    assert health["dotPendingAttempts"] == 0
    assert health["dotPendingRejected"] == 0
    assert health["dotPendingCommand"] == pending.command_id[:8]
    assert health["dotOutputAvailable"] is False
    assert health["dotUnavailableReason"] == "focus"
    assert health["dotRetryAfterSeconds"] == 60

    persisted = TokenStore(tmp_path / "tok.json").entries("dot_device")["device-token"]
    assert persisted["dot_unavailable_until"] == 160.0
    assert persisted["dot_unavailable_reason"] == "focus"


def test_expired_dot_lease_reissues_one_fresh_current_command(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (sent.append(payload) or (200, "")),
    )
    daemon._queue_dot_state("done", _dot_state(), now=100.0)
    expired_command = daemon._pending_dot.command_id
    daemon.report_dot_availability(
        "device-token", False, "dnd", 60.0, now=100.0
    )

    assert daemon._send_pending_dot_if_due(159.9) is False
    assert daemon._send_pending_dot_if_due(160.0) is True
    assert len(sent) == 1
    assert daemon._pending_dot.command_id != expired_command
    assert sent[0]["dot"]["commandID"] == daemon._pending_dot.command_id
    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert "dot_unavailable_until" not in metadata
    assert "dot_unavailable_reason" not in metadata


def test_dot_availability_endpoint_requires_owner_and_rearms_without_new_command(
    tmp_path, monkeypatch
):
    import time

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    now = time.time()
    daemon._queue_dot_state("done", _dot_state(updated_at=now), now=now)
    old_command = daemon._pending_dot.command_id

    status, body = _post_json(
        daemon,
        "/dot-availability",
        {
            "token": "stale-token",
            "available": False,
            "reason": "disconnected",
            "retryAfterSeconds": 300,
        },
    )
    assert status == 409
    assert body == {"ok": False, "error": "not_dot_owner"}
    assert daemon._dot_health()["dotOutputAvailable"] is True

    status, body = _post_json(
        daemon,
        "/dot-availability",
        {
            "token": "device-token",
            "available": False,
            "reason": "brightness_zero",
            "retryAfterSeconds": 300,
        },
    )
    assert status == 200
    assert body["dotOutputAvailable"] is False

    daemon._wake.clear()
    status, body = _post_json(
        daemon,
        "/dot-availability",
        {"token": "device-token", "available": True},
    )
    assert status == 200
    assert body["dotOutputAvailable"] is True
    assert daemon._wake.is_set()
    assert daemon._pending_dot.command_id == old_command

    assert daemon.ack_dot(old_command, "written") is True
    daemon._wake.clear()
    status, body = _post_json(
        daemon,
        "/dot-availability",
        {"token": "device-token", "available": True},
    )
    assert status == 200
    assert body["dotOutputAvailable"] is True
    assert daemon._pending_dot is None
    assert daemon._wake.is_set() is False


def test_stale_dot_ack_cannot_change_availability(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    daemon._queue_dot_state("done", _dot_state(), now=100.0)
    stale_command = daemon._pending_dot.command_id
    daemon._queue_dot_state(
        "working", _dot_state("working", 1, 101.0), now=101.0
    )
    current_command = daemon._pending_dot.command_id
    unavailable = {
        "available": False,
        "reason": "write_failed",
        "retryAfterSeconds": 600,
        "reportedAt": 200.0,
    }

    status, body = _post_json(
        daemon,
        "/dot-ack",
        {"commandID": stale_command, "status": "failed", **unavailable},
    )
    assert status == 200
    assert body == {"ok": True, "acknowledged": False}
    assert daemon._dot_health()["dotOutputAvailable"] is True

    status, body = _post_json(
        daemon,
        "/dot-ack",
        {"commandID": current_command, "status": "failed", **unavailable},
    )
    assert status == 200
    assert body == {"ok": True, "acknowledged": False}
    assert daemon._dot_health()["dotOutputAvailable"] is False
    assert daemon._pending_dot.command_id == current_command


def test_dot_availability_rejects_malformed_reports(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    malformed = [
        {"token": "device-token", "available": "false"},
        {
            "token": "device-token",
            "available": False,
            "reason": "unknown",
            "retryAfterSeconds": 60,
        },
        {
            "token": "device-token",
            "available": False,
            "reason": "focus",
            "retryAfterSeconds": 0,
        },
        {"token": "device-token", "available": True, "reportedAt": "later"},
        {
            "token": "device-token",
            "available": True,
            "dndScheduleEnabled": True,
            "reportedAt": 100.0,
        },
        {
            "token": "device-token",
            "available": True,
            "dndScheduleEnabled": False,
            "nextDndTransitionAt": 200.0,
            "nextDndTransitionEnabled": True,
            "reportedAt": 100.0,
        },
    ]

    for payload in malformed:
        status, body = _post_json(daemon, "/dot-availability", payload)
        assert status == 400
        assert "error" in body
    assert daemon._dot_health()["dotOutputAvailable"] is True


def test_older_dot_availability_cannot_overwrite_a_newer_report(
    tmp_path, monkeypatch
):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    status, _ = _post_json(
        daemon,
        "/dot-availability",
        {"token": "device-token", "available": True, "reportedAt": 200.0},
    )
    assert status == 200
    status, body = _post_json(
        daemon,
        "/dot-availability",
        {
            "token": "device-token",
            "available": False,
            "reason": "focus",
            "retryAfterSeconds": 600.0,
            "reportedAt": 100.0,
        },
    )

    assert status == 200
    assert body["dotOutputAvailable"] is True
    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert metadata["dot_client_reported_at"] == 200.0
    assert "dot_unavailable_until" not in metadata


def test_legacy_dot_availability_uses_arrival_order_without_losing_timestamp(
    tmp_path, monkeypatch
):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    assert daemon.report_dot_availability(
        "device-token", True, reported_at=200.0, now=100.0
    )
    assert daemon.report_dot_availability(
        "device-token", False, "dnd", 600.0, now=101.0
    )
    assert daemon.report_dot_availability(
        "device-token", True, reported_at=100.0, now=102.0
    )

    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert metadata["dot_client_reported_at"] == 200.0
    assert metadata["dot_unavailable_reason"] == "dnd"
    assert metadata["dot_unavailable_until"] == 701.0


def test_future_dot_report_cannot_poison_client_timestamp_ordering(
    tmp_path, monkeypatch
):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    assert daemon.report_dot_availability(
        "device-token", True, reported_at=200.0, now=100.0
    )
    assert daemon.report_dot_availability(
        "device-token",
        False,
        "focus",
        600.0,
        reported_at=10_000.0,
        now=101.0,
    )
    assert daemon.report_dot_availability(
        "device-token", True, reported_at=202.0, now=102.0
    )

    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert metadata["dot_client_reported_at"] == 202.0
    assert "dot_unavailable_until" not in metadata
    assert "dot_unavailable_reason" not in metadata


def test_dot_suppression_does_not_block_live_activity_pushes(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    daemon.tokens.register("update", "activity-token", {"activity_id": "a"})
    daemon.tokens.register("push_to_start", "start-token", {"device": "phone"})
    daemon.report_dot_availability(
        "device-token", False, "focus", 600.0, now=100.0
    )
    daemon._queue_dot_state("working", _dot_state("working", 1), now=100.0)
    sent = []

    def send(token, payload, **kwargs):
        sent.append((token, kwargs["push_type"]))
        return 200, ""

    monkeypatch.setattr(daemon.apns, "send", send)
    assert daemon._send_pending_dot_if_due(100.0) is False
    daemon._push_update(_dot_state("working", 1), now=100.0)
    daemon._maybe_push_to_start(_dot_state("working", 1), now=100.0)

    assert sent == [
        ("activity-token", "liveactivity"),
        ("start-token", "liveactivity"),
    ]


def test_elected_owner_stream_suppresses_dot_push_and_disconnect_rearms(
    tmp_path, monkeypatch
):
    import threading
    import time
    from http.client import HTTPConnection

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (sent.append(payload) or (200, "")),
    )
    server = daemon._build_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/stream?dotToken=device-token")
        response = connection.getresponse()
        assert response.status == 200
        assert response.readline().startswith(b"data: ")
        assert daemon._dot_health()["dotForegroundStreams"] == 1
        assert daemon._dot_stream_connected("not-the-owner") is False

        daemon._queue_dot_state("done", _dot_state(), now=100.0)
        assert daemon._send_pending_dot_if_due(100.0) is False
        assert sent == []
    finally:
        daemon._stop.set()
        with daemon._condition:
            daemon._condition.notify_all()
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    deadline = time.time() + 1.0
    while daemon._dot_health()["dotForegroundStreams"] and time.time() < deadline:
        time.sleep(0.01)
    assert daemon._dot_health()["dotForegroundStreams"] == 0
    assert daemon._wake.is_set()


def test_dnd_schedule_forces_each_boundary_and_end_clears_dnd_lease(
    tmp_path, monkeypatch
):
    import time

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (sent.append(payload) or (200, "")),
    )
    now = time.time()
    daemon._queue_dot_state("done", _dot_state(updated_at=now), now=now)
    baseline_command = daemon._pending_dot.command_id
    assert daemon.ack_dot(baseline_command, "written", now=now) is True

    status, body = _post_json(
        daemon,
        "/dot-availability",
        {
            "token": "device-token",
            "available": True,
            "reportedAt": now,
            "dndScheduleEnabled": True,
            "nextDndTransitionAt": now + 30.0,
            "nextDndTransitionEnabled": True,
        },
    )
    assert status == 200
    assert body["dotOutputAvailable"] is True
    assert daemon._pending_dot is None
    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert metadata["dot_schedule_reported_at"] == now
    assert metadata["dot_next_dnd_transition_at"] == now + 30.0

    assert daemon._send_pending_dot_if_due(now + 29.9) is False
    assert daemon._send_pending_dot_if_due(now + 30.0) is True
    assert len(sent) == 1
    start_command = sent[-1]["dot"]["commandID"]
    assert start_command != baseline_command

    status, body = _post_json(
        daemon,
        "/dot-ack",
        {
            "commandID": start_command,
            "status": "failed",
            "available": False,
            "reason": "dnd",
            "retryAfterSeconds": 600.0,
            "reportedAt": now + 31.0,
            "dndScheduleEnabled": True,
            "nextDndTransitionAt": now + 60.0,
            "nextDndTransitionEnabled": False,
        },
    )
    assert status == 200
    assert body == {"ok": True, "acknowledged": False}
    assert daemon._dot_health(now=now + 31.0)["dotUnavailableReason"] == "dnd"

    assert daemon._send_pending_dot_if_due(now + 59.9) is False
    assert daemon._send_pending_dot_if_due(now + 60.0) is True
    assert len(sent) == 2
    assert sent[-1]["dot"]["commandID"] != start_command
    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert "dot_unavailable_until" not in metadata
    assert "dot_unavailable_reason" not in metadata
    assert "dot_next_dnd_transition_at" not in metadata
    assert "dot_next_dnd_transition_enabled" not in metadata


def test_older_dnd_schedule_report_cannot_replace_newer_boundary(
    tmp_path, monkeypatch
):
    import time

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    now = time.time()
    newer = {
        "token": "device-token",
        "available": True,
        "reportedAt": now,
        "dndScheduleEnabled": True,
        "nextDndTransitionAt": now + 120.0,
        "nextDndTransitionEnabled": True,
    }
    assert _post_json(daemon, "/dot-availability", newer)[0] == 200
    older = {
        **newer,
        "reportedAt": now - 1.0,
        "nextDndTransitionAt": now + 60.0,
        "nextDndTransitionEnabled": False,
    }
    assert _post_json(daemon, "/dot-availability", older)[0] == 200

    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert metadata["dot_schedule_reported_at"] == now
    assert metadata["dot_next_dnd_transition_at"] == now + 120.0
    assert metadata["dot_next_dnd_transition_enabled"] is True


def test_focus_endpoint_is_ordered_clears_focus_lease_and_rearms_current_command(
    tmp_path, monkeypatch
):
    import time

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (sent.append(payload) or (200, "")),
    )
    now = time.time()
    daemon._queue_dot_state("done", _dot_state(updated_at=now), now=now)
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=now)

    status, body = _post_json(
        daemon,
        "/dot-focus",
        {"token": "device-token", "focused": True, "reportedAt": now},
    )
    assert status == 200
    assert body["updated"] is True
    assert body["dotFocusActive"] is True
    assert daemon._send_pending_dot_if_due(time.time()) is True
    assert "focusActive" not in sent[-1]["dot"]
    focused_command = daemon._pending_dot.command_id

    status, body = _post_json(
        daemon,
        "/dot-focus",
        {"token": "device-token", "focused": True, "reportedAt": now + 0.5},
    )
    assert status == 200
    assert body["updated"] is True
    assert daemon._pending_dot.command_id != focused_command
    focused_command = daemon._pending_dot.command_id

    status, _ = _post_json(
        daemon,
        "/dot-ack",
        {
            "commandID": focused_command,
            "status": "failed",
            "available": False,
            "reason": "focus",
            "retryAfterSeconds": 600.0,
            "reportedAt": now + 1.0,
        },
    )
    assert status == 200
    status, body = _post_json(
        daemon,
        "/dot-focus",
        {"token": "device-token", "focused": False, "reportedAt": now + 2.0},
    )
    assert status == 200
    assert body["dotFocusActive"] is False
    assert body["dotOutputAvailable"] is True
    assert daemon._pending_dot.command_id != focused_command
    assert daemon._send_pending_dot_if_due(time.time()) is True
    assert "focusActive" not in sent[-1]["dot"]

    current_command = daemon._pending_dot.command_id
    status, body = _post_json(
        daemon,
        "/dot-focus",
        {"token": "device-token", "focused": True, "reportedAt": now + 1.0},
    )
    assert status == 200
    assert body["updated"] is False
    assert body["dotFocusActive"] is False
    assert daemon._pending_dot.command_id == current_command

    daemon._pending_dot = None
    assert daemon.report_dot_availability(
        "device-token", False, "dnd", 600.0, now=time.time()
    )
    status, body = _post_json(
        daemon,
        "/dot-focus",
        {"token": "device-token", "focused": True, "reportedAt": now + 3.0},
    )
    assert status == 200
    assert body["dotFocusActive"] is True
    assert body["dotUnavailableReason"] == "dnd"
    assert daemon._pending_dot is None

    malformed = [
        {"token": "device-token", "focused": "yes", "reportedAt": now},
        {"token": "device-token", "focused": True, "reportedAt": "now"},
        {"token": "wrong", "focused": True, "reportedAt": now + 4.0},
    ]
    expected_statuses = [400, 400, 409]
    for payload, expected in zip(malformed, expected_statuses, strict=True):
        assert _post_json(daemon, "/dot-focus", payload)[0] == expected


def test_newer_focus_report_fences_delayed_availability_in_both_directions(
    tmp_path, monkeypatch
):
    import time

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    now = time.time()
    daemon._queue_dot_state("done", _dot_state(updated_at=now), now=now)
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=now)

    assert _post_json(
        daemon,
        "/dot-availability",
        {
            "token": "device-token",
            "available": False,
            "reason": "focus",
            "retryAfterSeconds": 600.0,
            "reportedAt": now + 5.0,
        },
    )[0] == 200
    assert _post_json(
        daemon,
        "/dot-focus",
        {"token": "device-token", "focused": True, "reportedAt": now + 10.0},
    )[0] == 200

    status, body = _post_json(
        daemon,
        "/dot-availability",
        {"token": "device-token", "available": True, "reportedAt": now + 9.0},
    )
    assert status == 200
    assert body["dotUnavailableReason"] == "focus"

    status, body = _post_json(
        daemon,
        "/dot-focus",
        {"token": "device-token", "focused": False, "reportedAt": now + 20.0},
    )
    assert status == 200
    assert body["dotOutputAvailable"] is True
    resume_command = daemon._pending_dot.command_id

    status, body = _post_json(
        daemon,
        "/dot-availability",
        {
            "token": "device-token",
            "available": False,
            "reason": "focus",
            "retryAfterSeconds": 600.0,
            "reportedAt": now + 19.0,
        },
    )
    assert status == 200
    assert body["dotOutputAvailable"] is True
    assert daemon._pending_dot.command_id == resume_command
    metadata = daemon.tokens.entries("dot_device")["device-token"]
    assert metadata["dot_client_reported_at"] == now + 5.0


def test_activity_alerts_use_the_bundled_sound_for_each_kind(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    payloads = []
    monkeypatch.setattr(
        daemon,
        "_apns_fanout",
        lambda kind, payload, priority=10: (payloads.append(payload) or True),
    )
    sounds = {
        "completed": "AgentFinished.caf",
        "waiting_for_input": "AgentNeedsInput.caf",
        "blocked_error": "AgentBlocked.caf",
    }

    for index, (kind, sound) in enumerate(sounds.items()):
        daemon._push_update(
            _dot_state("working", 1),
            now=100.0 + index,
            alert={"kind": kind, "title": "Title", "body": "Body"},
        )
        assert payloads[-1]["aps"]["alert"]["sound"] == sound


def test_deferred_finished_alert_preserves_its_kind(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)

    class Summarizer:
        ready = False

        def summary_for(self, *args, **kwargs):
            return "Task; completed" if self.ready else None

    summarizer = Summarizer()
    daemon.summarizer = summarizer
    alert = {
        "kind": "completed",
        "title": "Finished: Task",
        "body": "Completed",
        "thread_id": "group:codex:s1",
    }

    assert daemon._defer_finished_alerts([alert], [], now=100.0) == []
    summarizer.ready = True
    ready = daemon._defer_finished_alerts([], [], now=101.0)
    assert ready[0]["kind"] == "completed"


def test_dot_push_reports_unread_finished_rows_without_changing_public_mode(
    tmp_path, monkeypatch
):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    payloads = []

    def send(token, payload, **kwargs):
        payloads.append(payload)
        return 200, ""

    monkeypatch.setattr(daemon.apns, "send", send)
    cases = [
        ([], False),
        ([{"mode": "completed", "unread": False}], False),
        ([{"mode": "working", "unread": True}], False),
        ([{"mode": "completed", "unread": True}], True),
    ]
    for index, (agents, expected) in enumerate(cases):
        state = {**_dot_state("working", active_count=1), "agents": agents}
        daemon._queue_dot_state("working", state, now=100.0 + index, force=True)
        daemon._send_pending_dot_if_due(100.0 + index)

        assert payloads[-1]["dot"]["aggregateMode"] == "working"
        assert payloads[-1]["dot"]["hasUnreadFinished"] is expected


def test_dot_unread_finished_changes_queue_activation_and_clear_immediately(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_STATE_SETTLE_SECONDS

    daemon = _make_dot_daemon(tmp_path, monkeypatch)

    def state(unread: bool) -> dict:
        return {
            **_dot_state("working", active_count=1),
            "agents": [{"id": "done", "mode": "completed", "unread": unread}],
        }

    baseline = state(False)
    daemon._observe_dot_state("working", baseline, now=100.0)
    daemon._observe_dot_state(
        "working", baseline, now=100.0 + DOT_STATE_SETTLE_SECONDS
    )
    baseline_id = daemon._pending_dot.command_id
    assert daemon.ack_dot(baseline_id, "written") is True

    activated = state(True)
    assert daemon._observe_dot_state("working", activated, now=200.0) is True
    activation_id = daemon._pending_dot.command_id
    assert activation_id != baseline_id
    assert daemon._pending_dot.state == "working"
    assert daemon._pending_dot.has_unread_finished is True
    assert daemon.ack_dot(activation_id, "written") is True

    cleared = state(False)
    assert daemon._observe_dot_state("working", cleared, now=300.0) is True
    assert daemon._pending_dot.command_id != activation_id
    assert daemon._pending_dot.state == "working"
    assert daemon._pending_dot.has_unread_finished is False


def test_reading_last_finished_session_queues_immediate_idle_dot_push(
    tmp_path, monkeypatch
):
    import types

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    done = make_status(
        "claude:session:s1",
        AgentMode.COMPLETED,
        name="SidePulse: Verify finished indicator; completed",
        session_id="s1",
    )
    daemon.monitor = types.SimpleNamespace(
        snapshot=lambda include_stale=False: types.SimpleNamespace(
            statuses=[done],
            aggregate=types.SimpleNamespace(mode=AgentMode.COMPLETED),
        )
    )
    payloads = []
    monkeypatch.setattr(
        daemon.apns,
        "send",
        lambda token, payload, **kwargs: (payloads.append(payload) or (200, "")),
    )

    daemon._tick()
    first_command = daemon._pending_dot.command_id
    assert payloads[-1]["dot"]["aggregateMode"] == "completed"
    assert payloads[-1]["dot"]["hasUnreadFinished"] is True
    assert daemon.ack_dot(first_command, "written") is True

    finished_at = daemon._recent_finished[done.agent_id]["finishedAt"]
    daemon._wake.clear()
    status, body = _post_seen(
        daemon,
        {"id": done.agent_id, "finishedAt": finished_at},
    )
    assert status == 200
    assert body == {"ok": True, "marked": True}
    assert daemon._wake.is_set()

    payloads.clear()
    daemon._tick()

    assert daemon._latest["aggregateMode"] == "completed"
    assert daemon._pending_dot.state == "idle"
    assert daemon._pending_dot.has_unread_finished is False
    assert len(payloads) == 1
    assert payloads[0]["dot"]["aggregateMode"] == "idle_ready"
    assert payloads[0]["dot"]["activeCount"] == 0
    assert payloads[0]["dot"]["hasUnreadFinished"] is False


def test_attention_dot_states_bypass_settle_but_working_does_not(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_STATE_SETTLE_SECONDS

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    asking = _dot_state("waiting_for_input", active_count=1)
    assert daemon._observe_dot_state("ask", asking, now=100.0) is True
    ask_command = daemon._pending_dot.command_id
    assert daemon.ack_dot(ask_command, "written") is True

    working = _dot_state("working", active_count=1, updated_at=101.0)
    assert daemon._observe_dot_state("working", working, now=101.0) is False
    assert daemon._pending_dot is None
    assert daemon._observe_dot_state(
        "working", working, now=101.0 + DOT_STATE_SETTLE_SECONDS
    ) is True
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written") is True

    done = _dot_state("completed", updated_at=200.0)
    assert daemon._observe_dot_state("done", done, now=200.0) is True
    assert daemon._pending_dot.state == "done"


def test_expired_dot_command_waits_for_registration_before_reissuing(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_PUSH_EXPIRY_SECONDS

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    calls = []

    def reject(token, payload, **kwargs):
        calls.append((payload, kwargs))
        return 500, "server error"

    monkeypatch.setattr(daemon.apns, "send", reject)
    daemon._queue_dot_state("done", _dot_state(), now=100.0)
    expired_id = daemon._pending_dot.command_id
    daemon._send_pending_dot_if_due(100.0)

    renewal_time = 100.0 + DOT_PUSH_EXPIRY_SECONDS
    daemon._send_pending_dot_if_due(renewal_time)
    assert daemon._pending_dot.command_id == expired_id
    assert len(calls) == 1

    assert daemon.request_dot_resync(now=renewal_time + 1) is True
    assert daemon._pending_dot.command_id != expired_id
    daemon._send_pending_dot_if_due(renewal_time + 1)
    assert calls[-1][0]["dot"]["commandID"] == daemon._pending_dot.command_id
    assert calls[-1][1]["expiration"] == int(
        renewal_time + 1 + DOT_PUSH_EXPIRY_SECONDS
    )


def test_dot_ack_only_clears_the_matching_current_command(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    daemon._queue_dot_state("done", _dot_state(), now=100.0)
    old_id = daemon._pending_dot.command_id

    daemon._queue_dot_state(
        "working", _dot_state("working", active_count=1, updated_at=101.0), now=101.0
    )
    current_id = daemon._pending_dot.command_id
    assert current_id != old_id

    assert daemon.ack_dot(old_id, "written") is False
    assert daemon._pending_dot.command_id == current_id
    assert daemon.ack_dot(current_id, "failed") is False
    assert daemon._pending_dot.command_id == current_id
    assert daemon.ack_dot(current_id, "noFolder") is False
    assert daemon._pending_dot.command_id == current_id
    assert daemon.ack_dot(current_id, "alreadyCurrent") is True
    assert daemon._pending_dot is None
    assert daemon._last_dot_state == "working"


def test_dot_state_must_settle_and_same_state_does_not_reset_pending(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import DOT_STATE_SETTLE_SECONDS

    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    state = _dot_state("working", active_count=1)

    daemon._observe_dot_state("working", state, now=100.0)
    daemon._observe_dot_state(
        "working",
        {**state, "updatedAt": 109.0},
        now=100.0 + DOT_STATE_SETTLE_SECONDS - 0.1,
    )
    assert daemon._pending_dot is None

    daemon._observe_dot_state(
        "working",
        {**state, "updatedAt": 110.0},
        now=100.0 + DOT_STATE_SETTLE_SECONDS,
    )
    pending = daemon._pending_dot
    assert pending is not None

    daemon._observe_dot_state(
        "working", {**state, "updatedAt": 120.0}, now=120.0
    )
    assert daemon._pending_dot.command_id == pending.command_id
    assert daemon._pending_dot.created_at == pending.created_at


def test_device_registration_can_requeue_the_current_dot_state(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    state = {
        **_dot_state(),
        "agents": [{"id": "done", "mode": "completed", "unread": True}],
    }
    daemon._observe_dot_state("done", state, now=100.0)
    daemon._observe_dot_state("done", state, now=200.0)
    command_id = daemon._pending_dot.command_id
    assert daemon.ack_dot(command_id, "written") is True
    assert daemon._pending_dot is None

    assert daemon.request_dot_resync(now=300.0) is True
    assert daemon._pending_dot is not None
    assert daemon._pending_dot.state == "done"
    assert daemon._pending_dot.has_unread_finished is True
    assert daemon._pending_dot.command_id != command_id
    resync_id = daemon._pending_dot.command_id
    assert daemon.request_dot_resync(now=302.0) is False
    assert daemon._pending_dot.command_id == resync_id


def test_changed_dot_owner_forces_a_new_command_id(tmp_path, monkeypatch):
    daemon = _make_dot_daemon(tmp_path, monkeypatch)
    state = _dot_state()
    daemon._observe_dot_state("done", state, now=100.0)
    daemon._observe_dot_state("done", state, now=200.0)
    old_id = daemon._pending_dot.command_id

    assert daemon.request_dot_resync(now=201.0, force=True) is True
    assert daemon._pending_dot.command_id != old_id
    assert daemon.ack_dot(old_id, "written") is False
    assert daemon._pending_dot is not None


def test_structure_signature_coalesces_busy_churn_and_row_reordering():
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
    tool_running_reordered = {
        **base,
        "aggregateMode": "tool_running",
        "agents": [
            base["agents"][1],
            {**base["agents"][0], "mode": "tool_running"},
        ],
    }
    waiting = {
        **base,
        "aggregateMode": "waiting_for_input",
        "agents": [
            {**base["agents"][0], "mode": "waiting_for_input"},
            base["agents"][1],
        ],
    }
    assert _structure_signature(base) == _structure_signature(renamed)
    assert _structure_signature(base) == _structure_signature(tool_running_reordered)
    assert _structure_signature(base) != _structure_signature(seen)
    assert _structure_signature(base) != _structure_signature(waiting)


def test_new_update_token_forces_current_state_hydration(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    monkeypatch.setattr("sidepulse.live_activity.time.time", lambda: 200.0)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register(
        "update",
        "old",
        {"activity_id": "old-activity", "activity_started_at": 100.0},
    )
    daemon._last_pushed_signature = ("active", 1, ())
    daemon._last_pushed_state = {
        "aggregateMode": "working",
        "activeCount": 1,
        "agents": [],
        "updatedAt": 150.0,
    }
    daemon._last_push_at = 199.0
    daemon._pushes_this_activity = 50

    assert daemon.register_update_token(
        "new", {"device": "phone", "activity_id": "new-activity"}
    ) is True

    assert daemon.tokens.tokens("update") == ["new"]
    assert daemon._last_pushed_signature is None
    assert daemon._last_pushed_state is None
    assert daemon._last_push_at == 0.0
    assert daemon._pushes_this_activity == 0
    assert daemon._wake.is_set()


def test_update_token_rotation_preserves_activity_age(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    monkeypatch.setattr("sidepulse.live_activity.time.time", lambda: 200.0)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register(
        "update",
        "old",
        {"activity_id": "same-activity", "activity_started_at": 100.0},
    )
    daemon._pushes_this_activity = 7

    assert daemon.register_update_token(
        "rotated", {"device": "phone", "activity_id": "same-activity"}
    ) is True

    entries = daemon.tokens.entries("update")
    assert list(entries) == ["rotated"]
    assert entries["rotated"]["activity_started_at"] == 100.0
    assert daemon._pushes_this_activity == 7


def test_new_push_to_start_token_keeps_current_activity(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("update", "current", {"activity_id": "activity-a"})
    ended = []
    monkeypatch.setattr(daemon, "_end_stale_activity", lambda reason: ended.append(reason))

    assert daemon.register_push_to_start_token("new-p2s", {"device": "phone"}) is True

    assert daemon.tokens.tokens("update") == ["current"]
    assert ended == []
    assert daemon._start_push_attempts == 0


def test_push_to_start_rotation_replaces_only_the_same_device(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))

    daemon.register_push_to_start_token("phone-old", {"device": "phone"})
    daemon.register_push_to_start_token("tablet", {"device": "tablet"})
    daemon.register_push_to_start_token("phone-new", {"device": "phone"})

    assert set(daemon.tokens.tokens("push_to_start")) == {"phone-new", "tablet"}


def test_reset_from_stale_activity_keeps_current_token(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("update", "current", {"activity_id": "activity-new"})

    assert daemon.reset_activity("activity-old") is False
    assert daemon.tokens.tokens("update") == ["current"]


def test_ended_activity_cannot_reregister_its_token(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.register_update_token("current", {"activity_id": "activity-a"})
    monkeypatch.setattr(daemon, "_apns_fanout", lambda *args, **kwargs: True)

    assert daemon.reset_activity("activity-a") is True
    assert daemon.tokens.tokens("update") == []
    assert daemon.register_update_token(
        "late", {"activity_id": "activity-a"}
    ) is False
    assert daemon.tokens.tokens("update") == []


def test_token_change_during_push_keeps_new_activity_hydration_pending(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.register_update_token(
        "old", {"activity_id": "activity-old", "activity_observed_at": 1.0}
    )
    state = {
        "aggregateMode": "working",
        "activeCount": 1,
        "agents": [],
        "updatedAt": 2.0,
    }

    def register_new_during_send(*args, **kwargs):
        daemon.register_update_token(
            "new", {"activity_id": "activity-new", "activity_observed_at": 2.0}
        )
        return True

    monkeypatch.setattr(daemon, "_apns_fanout", register_new_during_send)
    daemon._push_update(state, now=3.0)

    assert daemon.tokens.tokens("update") == ["new"]
    assert daemon._last_pushed_signature is None
    assert daemon._last_pushed_state is None
    assert daemon._wake.is_set()


def test_retired_activity_retry_cannot_steal_update_token(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))

    assert daemon.register_update_token(
        "old", {"activity_id": "activity-old", "activity_observed_at": 1.0}
    )
    assert daemon.register_update_token(
        "new", {"activity_id": "activity-new", "activity_observed_at": 2.0}
    )
    assert daemon.register_update_token(
        "late-old", {"activity_id": "activity-old", "activity_observed_at": 1.0}
    ) is False
    assert daemon.tokens.tokens("update") == ["new"]


def test_failed_update_does_not_mark_content_as_pushed(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.register_update_token("current", {"activity_id": "activity-a"})
    monkeypatch.setattr(daemon, "_apns_fanout", lambda *args, **kwargs: False)
    state = {
        "aggregateMode": "working",
        "activeCount": 1,
        "agents": [],
        "updatedAt": 2.0,
    }

    daemon._push_update(state, now=3.0)

    assert daemon._last_pushed_signature is None
    assert daemon._last_pushed_state is None


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


def _make_seen_endpoint_daemon(tmp_path, monkeypatch, *, unread=True, finished_at=100.25):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8",
        apns_key_id="X",
        apns_team_id="Y",
        port=0,
        summaries_enabled=False,
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    row = {
        "id": "codex:session:s1",
        "name": "SidePulse: Test completion reads; completed",
        "mode": "completed",
        "provider": "codex",
        "finishedAt": finished_at,
        "unread": unread,
    }
    daemon._recent_finished = {row["id"]: row}
    daemon._save_recent_finished()
    daemon._latest = build_content_state([], "idle_ready", [row])
    return daemon


def _post_seen(daemon, payload):
    return _post_json(daemon, "/seen", payload)


def test_seen_endpoint_marks_exact_generation_and_refreshes_snapshot(
    tmp_path, monkeypatch
):
    daemon = _make_seen_endpoint_daemon(tmp_path, monkeypatch)
    previous_snapshot = daemon._latest

    status, body = _post_seen(
        daemon,
        {"id": "codex:session:s1", "finishedAt": 100.25},
    )

    assert status == 200
    assert body == {"ok": True, "marked": True}
    assert daemon._recent_finished["codex:session:s1"]["unread"] is False
    assert daemon._latest is not previous_snapshot
    assert daemon._latest["agents"][0]["unread"] is False


def test_seen_endpoint_is_idempotent_for_exact_read_generation(tmp_path, monkeypatch):
    daemon = _make_seen_endpoint_daemon(tmp_path, monkeypatch, unread=False)

    status, body = _post_seen(
        daemon,
        {"id": "codex:session:s1", "finishedAt": 100.25},
    )

    assert status == 200
    assert body == {"ok": True, "marked": False}
    assert daemon._recent_finished["codex:session:s1"]["unread"] is False


def test_seen_endpoint_rejects_a_stale_completion_generation(tmp_path, monkeypatch):
    daemon = _make_seen_endpoint_daemon(tmp_path, monkeypatch, finished_at=200.5)
    previous_snapshot = daemon._latest

    status, body = _post_seen(
        daemon,
        {"id": "codex:session:s1", "finishedAt": 100.25},
    )

    assert status == 409
    assert body == {"ok": False, "error": "stale_completion"}
    assert daemon._recent_finished["codex:session:s1"]["unread"] is True
    assert daemon._latest is previous_snapshot


def test_seen_endpoint_keeps_legacy_id_only_requests_compatible(tmp_path, monkeypatch):
    daemon = _make_seen_endpoint_daemon(tmp_path, monkeypatch)

    status, body = _post_seen(daemon, {"id": "codex:session:s1"})

    assert status == 200
    assert body == {"ok": True, "marked": True}
    assert daemon._recent_finished["codex:session:s1"]["unread"] is False


def test_tick_cannot_resurrect_unread_after_completion_is_seen(
    tmp_path, monkeypatch
):
    import threading
    import types

    import sidepulse.live_activity as live_activity

    daemon = _make_seen_endpoint_daemon(tmp_path, monkeypatch)
    daemon.monitor = types.SimpleNamespace(
        snapshot=lambda include_stale=False: types.SimpleNamespace(
            statuses=[], aggregate=types.SimpleNamespace(mode=AgentMode.IDLE_READY)
        )
    )
    real_build = live_activity.build_content_state
    build_started = threading.Event()
    release_build = threading.Event()

    def blocked_build(*args, **kwargs):
        state = real_build(*args, **kwargs)
        build_started.set()
        if not release_build.wait(timeout=5):
            raise TimeoutError("test did not release content-state build")
        return state

    monkeypatch.setattr(live_activity, "build_content_state", blocked_build)
    errors = []

    def tick():
        try:
            daemon._tick()
        except BaseException as exc:
            errors.append(exc)

    tick_thread = threading.Thread(target=tick)
    tick_thread.start()
    assert build_started.wait(timeout=2)

    # Before the fix the tick released this lock after copying unread=true,
    # allowing /seen to publish unread=false before the stale tick overwrote it.
    acquired_between_copy_and_publish = daemon._recent_finished_lock.acquire(
        blocking=False
    )
    if acquired_between_copy_and_publish:
        try:
            assert daemon.mark_finished_seen(
                "codex:session:s1", 100.25
            ) is True
        finally:
            daemon._recent_finished_lock.release()

    release_build.set()
    tick_thread.join(timeout=2)
    assert not tick_thread.is_alive()
    assert errors == []
    if not acquired_between_copy_and_publish:
        assert daemon.mark_finished_seen("codex:session:s1", 100.25) is True

    assert daemon._recent_finished["codex:session:s1"]["unread"] is False
    assert daemon._latest["agents"][0]["unread"] is False


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
        PUSH_TO_START_MAX_BACKOFF_SECONDS,
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

    # Unanswered starts stop after the cap: more would only stack activities
    # the daemon can never end.
    daemon._maybe_push_to_start(state, t2 + 10 * PUSH_TO_START_MAX_BACKOFF_SECONDS)
    assert len(sent) == 3


def test_dead_update_token_cannot_spawn_a_stack_of_activities(tmp_path, monkeypatch):
    # A flapping activity token used to zero the cooldown, so every 410
    # immediately started another activity — and an activity whose token
    # never reaches the daemon can never be ended remotely.
    from sidepulse.live_activity import (
        START_PUSH_MIN_GAP_SECONDS,
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
    assert len(sent) == 1

    # An activity registers, then its token dies moments later.
    daemon.tokens.register("update", "upd", {"device": "phone", "activity_id": "a"})
    daemon._start_push_attempts = 0
    daemon.tokens.drop("update", "upd")
    daemon._start_push_attempts = 0  # what the dead-token path does

    daemon._maybe_push_to_start(state, 1000.0 + 1)
    assert len(sent) == 1, "a dead token must not bypass the minimum gap"

    daemon._maybe_push_to_start(state, 1000.0 + START_PUSH_MIN_GAP_SECONDS + 1)
    assert len(sent) == 2


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


def test_summarizer_disables_all_claude_tools(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sidepulse.live_activity import SessionSummarizer

    command = []

    call = {}

    def fake_run(args, **kwargs):
        command.extend(args)
        call.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="Kleido: Deploy TestFlight build; upload running\n",
            stderr="",
        )

    summarizer = object.__new__(SessionSummarizer)
    summarizer.model = "claude-haiku-test"
    summarizer.claude = "/usr/local/bin/claude"
    summarizer.workdir = tmp_path
    summarizer.moonside_dir = tmp_path / "moonside"
    monkeypatch.setattr("sidepulse.live_activity.subprocess.run", fake_run)

    assert summarizer._generate(
        "Upload to TestFlight: success.",
        "repository observed for this session: live-translator",
        style="task",
    ) == (
        "Deploy TestFlight build; upload running"
    )
    assert "task; latest state" in call["input"].lower()
    assert call["input"] not in command
    tools_index = command.index("--tools")
    assert command[tools_index + 1] == ""
    prompt = call["input"].lower()
    assert "kleido" not in prompt
    assert "sidepulse:" not in prompt
    assert "task; latest state" in prompt
    assert "never replace the task" in prompt
    assert "low-level command" in prompt


def test_summarizer_never_returns_a_cached_result_for_changed_content():
    import hashlib
    import queue
    import threading

    from sidepulse.live_activity import (
        SUMMARY_PROMPT_VERSION,
        SessionSummarizer,
        _summary_cache_key,
    )

    summarizer = object.__new__(SessionSummarizer)
    key = _summary_cache_key("s1", "task")
    old_hash = hashlib.sha256(
        f"{SUMMARY_PROMPT_VERSION}\0\0old source".encode()
    ).hexdigest()[:16]
    summarizer._results = {key: (old_hash, "Old task; old state")}
    summarizer._requested_hashes = {}
    summarizer._pending = set()
    summarizer._lock = threading.Lock()
    summarizer._queue = queue.Queue()
    summarizer._failure_count = 0
    summarizer._retry_after = 0.0

    assert summarizer.summary_for("s1", "new source", style="task") is None
    assert summarizer.summary_for("s1", None, style="task") is None
    queued = summarizer._queue.get_nowait()
    assert queued[2] == "new source"
    summarizer._record_generation_result(queued[0], queued[1], None)
    assert summarizer.summary_for("s1", None, style="task") is None


def test_summarizer_failure_backoff_is_global_and_resets_on_success(monkeypatch):
    import queue
    import threading

    from sidepulse.live_activity import SessionSummarizer

    now = [100.0]
    monkeypatch.setattr("sidepulse.live_activity.time.monotonic", lambda: now[0])
    summarizer = object.__new__(SessionSummarizer)
    summarizer._results = {}
    summarizer._requested_hashes = {}
    summarizer._pending = {"first"}
    summarizer._lock = threading.Lock()
    summarizer._queue = queue.Queue()
    summarizer._failure_count = 0
    summarizer._retry_after = 0.0

    assert summarizer._record_generation_result("first", "hash", None) == 60.0
    assert summarizer.summary_for("second", "source", style="task") is None
    assert summarizer._queue.empty()

    # A concurrent failure in the same wave does not double the backoff.
    now[0] = 101.0
    assert summarizer._record_generation_result("other", "hash", None) == 0.0
    assert summarizer._failure_count == 1

    now[0] = 160.0
    assert summarizer.summary_for("second", "source", style="task") is None
    queued = summarizer._queue.get_nowait()
    assert queued[0].startswith("second|")
    assert summarizer._record_generation_result(queued[0], queued[1], None) == 120.0

    for expected_delay in (240.0, 480.0, 900.0, 900.0):
        now[0] = summarizer._retry_after
        assert (
            summarizer._record_generation_result("retry", "hash", None)
            == expected_delay
        )

    summarizer._record_generation_result("success", "new", "Task; done")
    assert summarizer._failure_count == 0
    assert summarizer._retry_after == 0.0


def test_summarizer_logs_cli_errors_from_stdout(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sidepulse.live_activity import SessionSummarizer

    messages = []
    summarizer = object.__new__(SessionSummarizer)
    summarizer.model = "claude-haiku-test"
    summarizer.claude = "/usr/local/bin/claude"
    summarizer.workdir = tmp_path
    summarizer.moonside_dir = tmp_path / "moonside"
    monkeypatch.setattr(
        "sidepulse.live_activity.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="You've hit your session limit\n",
            stderr="",
        ),
    )
    monkeypatch.setattr("sidepulse.live_activity._log", messages.append)

    assert summarizer._generate("Content", "", style="task") is None
    assert any("session limit" in message for message in messages)


def test_summarizer_rejects_a_title_without_task_and_state(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sidepulse.live_activity import SessionSummarizer

    summarizer = object.__new__(SessionSummarizer)
    summarizer.model = "claude-haiku-test"
    summarizer.claude = "/usr/local/bin/claude"
    summarizer.workdir = tmp_path
    summarizer.moonside_dir = tmp_path / "moonside"
    monkeypatch.setattr(
        "sidepulse.live_activity.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="SidePulse: Improving session titles\n",
            stderr="",
        ),
    )

    assert summarizer._generate(
        "Current request:\nImprove titles\n\nSession state:\nWorking",
        "repository observed for this session: SidePulse",
        style="task",
    ) is None


def test_prompt_tracker_ignores_protocol_notifications_without_clearing_actions(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import PromptTracker

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    records = [
        {
            "session_id": "s1",
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/Users/x/Git/live-translator",
            "prompt": "Go to voice translator app repo.",
        },
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "cwd": "/Users/x/Git/live-translator",
            "tool_input": {"description": "Wait 20 seconds"},
        },
        {
            "session_id": "s1",
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/Users/x/Git",
            "prompt": (
                '<task-notification><summary>Monitor event: "TestFlight build with '
                'summary chip"</summary><event>Upload: success</event></task-notification>'
            ),
        },
    ]
    (tmp_path / "claude.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )

    tracker = PromptTracker()
    tracker.poll()

    assert tracker.project_for("s1", "/Users/x/Git") == "live-translator"
    assert tracker.trusted_context_for("s1") == (
        "repository observed for this session: live-translator"
    )
    assert tracker.prompt_for("s1") == "Go to voice translator app repo."
    assert tracker.actions_for("s1") == ["Wait 20 seconds"]


def test_prompt_tracker_resolves_scoped_project_and_ignores_subagent_actions(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import PromptTracker

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    records = [
        {
            "event": {
                "session_id": "s1",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/Users/x/Git/sidepulse",
                "prompt": (
                    "Improve the session titles in Side Pulse. It should name "
                    "the product, e.g. Kleido or Side Pulse."
                ),
            }
        },
        {
            "event": {
                "session_id": "s1",
                "hook_event_name": "PreToolUse",
                "cwd": "/Users/x/Git/sidepulse",
                "tool_input": {"command": "pytest tests/test_live_activity.py"},
            }
        },
        {
            "event": {
                "session_id": "s1",
                "agent_id": "subagent-1",
                "hook_event_name": "PreToolUse",
                "cwd": "/Users/x/Git/wardrobe-app",
                "tool_input": {"command": "deploy unrelated project"},
            }
        },
    ]
    (tmp_path / "codex.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )

    tracker = PromptTracker()
    tracker.poll()

    assert tracker.project_for("s1", "/Users/x/Git") == "SidePulse"
    assert tracker.actions_for("s1") == ["pytest tests/test_live_activity.py"]


def test_prompt_tracker_does_not_guess_a_repo_from_prompt_text(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import PromptTracker

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    workspace = tmp_path / "Git"
    (workspace / "aura" / ".git").mkdir(parents=True)
    (tmp_path / "codex.jsonl").write_text(
        json.dumps(
            {
                "event": {
                    "session_id": "s1",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(workspace),
                    "prompt": "Improve the calendar in Aura and deploy it.",
                }
            }
        )
        + "\n"
    )

    tracker = PromptTracker()
    tracker.poll()

    assert tracker.project_for("s1", str(workspace)) is None


def test_prompt_tracker_uses_structured_codex_tool_workdir(tmp_path, monkeypatch):
    from sidepulse.live_activity import PromptTracker

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    repo = tmp_path / "Git" / "sidepulse"
    (repo / ".git").mkdir(parents=True)
    other_repo = tmp_path / "Git" / "wardrobe-app"
    (other_repo / ".git").mkdir(parents=True)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "content": f"An unrelated example mentions {other_repo}",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": (
                        "const ignored = 'tools.exec_command({workdir: \""
                        + str(other_repo)
                        + "\"})';\n"
                        + "const r = await tools.exec_command({\"cmd\": \"pytest\", "
                        + f'\"workdir\": \"{repo}\", \"yield_time_ms\": 10000}});'
                    ),
                },
            }
        )
        + "\n"
    )
    (tmp_path / "codex.jsonl").write_text(
        json.dumps(
            {
                "event": {
                    "session_id": "s1",
                    "hook_event_name": "PreToolUse",
                    "cwd": "/Users/x/Git/sidepulse",
                    "transcript_path": str(transcript),
                    "tool_input": {"command": "pytest"},
                }
            }
        )
        + "\n"
    )

    tracker = PromptTracker()
    tracker.poll()

    assert tracker.project_for("s1", "/Users/x/Git") == "SidePulse"


def test_prompt_tracker_reads_first_incremental_transcript_record(tmp_path, monkeypatch):
    from sidepulse.live_activity import PromptTracker

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    repo = tmp_path / "Git" / "sidepulse"
    (repo / ".git").mkdir(parents=True)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({"type": "response_item", "payload": {}}) + "\n")
    tracker = PromptTracker()

    assert tracker._project_from_codex_transcript(str(transcript)) is None
    with transcript.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": {"cmd": "pytest", "workdir": str(repo)},
                    },
                }
            )
            + "\n"
        )

    assert tracker._project_from_codex_transcript(str(transcript)) == "SidePulse"


def test_prompt_tracker_strips_attachment_preamble_from_request(tmp_path, monkeypatch):
    from sidepulse.live_activity import PromptTracker

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    (tmp_path / "codex.jsonl").write_text(
        json.dumps(
            {
                "event": {
                    "session_id": "s1",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": "/Users/x/Git/sidepulse",
                    "prompt": (
                        "# Files mentioned by the user:\n\n"
                        "## Pasted Image.jpg: /tmp/image.jpg\n\n"
                        "## My request:\nAlso it shows ‘nonproject’"
                    ),
                }
            }
        )
        + "\n"
    )

    tracker = PromptTracker()
    tracker.poll()

    assert tracker.prompt_for("s1") == "Also it shows ‘nonproject’"


def test_project_display_name_maps_wardrobe_repo_to_kleido():
    from sidepulse.live_activity import _project_name_from_cwd

    assert _project_name_from_cwd("/Users/x/Git/wardrobe-app") == "Kleido"


def test_generated_title_cannot_override_observed_repository(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    (tmp_path / "claude.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/Users/x/Git/live-translator",
                "prompt": "Improve the live translator and deploy it.",
            }
        )
        + "\n"
    )
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y"
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon._prompt_tracker.poll()

    calls = []

    class FakeSummarizer:
        def summary_for(self, session_id, message, context="", style="outcome"):
            calls.append((message, context, style))
            return "Improve live translator; TestFlight build uploaded"

    daemon.summarizer = FakeSummarizer()
    done = make_status(
        "claude:session:s1",
        AgentMode.COMPLETED,
        name="Kleido: deploying TestFlight build IPA",
        session_id="s1",
    )
    done = type(done)(
        **{
            **done.__dict__,
            "event_name": "Stop",
            "message": "Uploaded. Waiting on processing.",
            "cwd": "/Users/x/Git/live-translator",
        }
    )

    result = daemon._apply_summary(done)

    assert result.display_name == (
        "live-translator: Improve live translator; TestFlight build uploaded"
    )
    assert all("Kleido" not in context for _, context, _ in calls)
    assert "Current request:" in calls[0][0]
    assert "Latest result or blocker:" in calls[0][0]
    assert "Uploaded. Waiting on processing." in calls[0][0]


def test_blocked_session_title_keeps_task_and_concrete_state(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    (tmp_path / "codex.jsonl").write_text(
        json.dumps(
            {
                "event": {
                    "session_id": "s1",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": "/Users/x/Git/sidepulse",
                    "prompt": "Fix session titles in SidePulse.",
                }
            }
        )
        + "\n"
    )
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y"
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon._prompt_tracker.poll()
    calls = []

    class FakeSummarizer:
        def summary_for(self, session_id, message, context="", style="outcome"):
            calls.append((message, style))
            return "Fix session titles; blocked by failing tests"

    daemon.summarizer = FakeSummarizer()
    blocked = make_status(
        "codex:session:s1", AgentMode.BLOCKED_ERROR, name="old", session_id="s1"
    )
    blocked = type(blocked)(
        **{
            **blocked.__dict__,
            "event_name": "PostToolUseFailure",
            "message": "Three title tests failed.",
            "cwd": "/Users/x/Git/sidepulse",
        }
    )

    result = daemon._apply_summary(blocked)

    assert result.display_name == (
        "SidePulse: Fix session titles; blocked by failing tests"
    )
    assert calls[0][1] == "outcome"
    assert "Three title tests failed." in calls[0][0]


def test_new_prompt_invalidates_the_active_summary_source(tmp_path, monkeypatch):
    import time

    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y"
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon._prompt_tracker._prompts["s1"] = "Deploy the new session titles."
    daemon._prompt_tracker._projects["s1"] = "SidePulse"
    daemon._task_sources["s1"] = (
        "old-prompt-hash",
        "Current request:\nOld task",
        time.time(),
    )
    calls = []

    class FakeSummarizer:
        def summary_for(self, session_id, message, context="", style="outcome"):
            calls.append((message, style))
            return "Deploy session titles; running verification"

    daemon.summarizer = FakeSummarizer()
    busy = make_status(
        "codex:session:s1", AgentMode.WORKING, name="old", session_id="s1"
    )
    result = daemon._apply_summary(busy)

    assert result.display_name == (
        "SidePulse: Deploy session titles; running verification"
    )
    assert "Deploy the new session titles." in calls[0][0]
    assert "Old task" not in calls[0][0]


def test_title_truncation_preserves_latest_state(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y"
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))

    title = daemon._summary_title(
        "s1",
        "Improve a deliberately very long session title that would otherwise hide the state; "
        "blocked by failing tests",
        "/Users/x/Git/long-project-name",
    )

    assert len(title) <= 90
    assert title.endswith("; blocked by failing tests")

    fitting = daemon._summary_title(
        "s1",
        "Improve session titles; pushed code, service restarting on Mini",
        "/Users/x/Git/sidepulse",
    )
    assert fitting == (
        "SidePulse: Improve session titles; pushed code, service restarting on Mini"
    )


def test_summarizer_replaces_display_name(tmp_path, monkeypatch):
    import time as _time

    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\necho 'sidepulse: Deploy TestFlight build; build deployed'\n"
    )
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
        if result.display_name == "SidePulse: Deploy TestFlight build; build deployed":
            break
        _time.sleep(0.1)
    assert result.display_name == "SidePulse: Deploy TestFlight build; build deployed"

    # Working sessions keep their prompt-based name.
    busy = make_status("claude:session:s2", AgentMode.WORKING, name="prompt", session_id="s2")
    assert daemon._apply_summary(busy).display_name == "Prompt; working"


def test_finished_row_refreshes_when_async_outcome_arrives(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    (tmp_path / "claude.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/Users/x/Git/sidepulse",
                "prompt": "Improve session titles.",
            }
        )
        + "\n"
    )
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y"
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon._prompt_tracker.poll()

    class DeferredSummarizer:
        ready = False

        def summary_for(self, session_id, message, context="", style="outcome"):
            return (
                "Improve session titles; deployed and verified"
                if self.ready
                else None
            )

    deferred = DeferredSummarizer()
    daemon.summarizer = deferred
    done = make_status(
        "claude:session:s1", AgentMode.COMPLETED, name="old", session_id="s1"
    )
    done = type(done)(
        **{
            **done.__dict__,
            "event_name": "Stop",
            "message": "Deployment finished on all Macs.",
            "cwd": "/Users/x/Git/sidepulse",
        }
    )

    summarized = daemon._apply_summary(done)
    daemon._remember_finished([summarized], now=100.0)
    assert daemon._recent_finished[done.agent_id]["name"].endswith("; completed")

    deferred.ready = True
    daemon._refresh_finished_summaries()
    assert daemon._recent_finished[done.agent_id]["name"] == (
        "SidePulse: Improve session titles; deployed and verified"
    )


def test_vanished_session_replaces_working_state_with_completed(tmp_path, monkeypatch):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y"
    )
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    agent_id = "codex:session:s1"
    daemon._agent_modes[agent_id] = "working"
    daemon._last_rows[agent_id] = {
        "id": agent_id,
        "name": "SidePulse: Improve session titles; testing implementation",
        "mode": "working",
        "detail": "Bash",
        "provider": "codex",
        "cwd": "Git",
    }

    daemon._remember_finished([], now=100.0)

    assert daemon._recent_finished[agent_id]["name"] == (
        "SidePulse: Improve session titles; completed"
    )


def test_loaded_finished_rows_gain_canonical_project_and_completed_state(
    tmp_path, monkeypatch
):
    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    (tmp_path / "recent_finished.json").write_text(
        json.dumps(
            {
                "claude:session:s1": {
                    "id": "claude:session:s1",
                    "name": "sidepulse: Verifying restarted agents stay up",
                    "mode": "completed",
                    "provider": "claude",
                    "finishedAt": 1.0,
                },
                "claude:session:s2": {
                    "id": "claude:session:s2",
                    "name": "wardrobe-app: Pricing changes deployed; working",
                    "mode": "completed",
                    "provider": "claude",
                    "finishedAt": 2.0,
                },
            }
        )
    )
    config = LiveActivityConfig(
        apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y"
    )

    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))

    assert daemon._recent_finished["claude:session:s1"]["name"] == (
        "SidePulse: Verifying restarted agents stay up; completed"
    )
    assert daemon._recent_finished["claude:session:s2"]["name"] == (
        "Kleido: Pricing changes deployed; completed"
    )


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


def test_activity_rotates_before_the_eight_hour_cap(tmp_path, monkeypatch):
    # iOS ends a Live Activity eight hours in: the Dynamic Island slot goes
    # immediately while a dead card sits on the Lock Screen for hours, and
    # the daemon only learns of it when a later push 410s. Rotate first.
    from sidepulse.live_activity import (
        ACTIVITY_MAX_AGE_SECONDS,
        LiveActivityConfig,
        LiveActivityDaemon,
        TokenStore,
    )

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    start = 1000.0
    daemon.tokens.register(
        "update",
        "upd",
        {"device": "phone", "activity_id": "a", "activity_started_at": start},
    )
    daemon._activity_live = True

    assert daemon._activity_age(start + 3600) == 3600

    # The app re-registers the same activity (iOS may hand it a new token);
    # the clock must keep running, not restart from the registration.
    assert daemon._activity_started_at("a") == start
    assert daemon._activity_started_at("b") is None

    sent = []
    monkeypatch.setattr(
        daemon,
        "_apns_fanout",
        lambda kind, payload, priority=10: sent.append(payload["aps"]["event"]),
    )

    # Past the rotation age the activity ends and its token is forgotten,
    # which reopens the start path for a fresh one.
    daemon._end_stale_activity("test")
    assert sent == ["end"]
    assert daemon.tokens.tokens("update") == []
    assert daemon._activity_live is False
    assert daemon._activity_age(start + ACTIVITY_MAX_AGE_SECONDS) is None


def test_a_dead_activity_restarts_while_only_finished_rows_remain(tmp_path, monkeypatch):
    # The island used to come back only when NEW work started. An activity
    # that died while the host idled therefore stayed dead — the phone showed
    # nothing for as long as the user stayed idle, even though the finished
    # rows the daemon kept alive were still worth an island.
    import time
    import types

    from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("push_to_start", "p2s", {"device": "phone", "activity_id": ""})
    daemon.monitor = types.SimpleNamespace(
        snapshot=lambda include_stale=False: types.SimpleNamespace(
            statuses=[], aggregate=types.SimpleNamespace(mode=AgentMode.IDLE_READY)
        )
    )
    daemon._recent_finished = {
        "claude:session:a": {
            "id": "claude:session:a",
            "name": "done",
            "mode": "completed",
            "provider": "claude",
            "finishedAt": time.time(),
            "unread": True,
        }
    }
    sent = []
    monkeypatch.setattr(
        daemon,
        "_apns_fanout",
        lambda kind, payload, priority=10: sent.append((kind, payload)),
    )

    daemon._tick()
    assert [kind for kind, _ in sent] == ["push_to_start"]
    # Putting the island back for work that already finished is a repair, not
    # news: it must not buzz the phone.
    assert "alert" not in sent[0][1]["aps"]

    # With nothing at all to show, no activity is started.
    sent.clear()
    daemon._recent_finished = {}
    daemon._tick()
    assert sent == []


def test_an_idle_activity_is_probed_so_a_silent_death_is_noticed(tmp_path, monkeypatch):
    # The heartbeat used to require active work. With everything finished the
    # content stops changing, so nothing was pushed at all — and only a push
    # can come back 410, so an activity that died while the user idled stayed
    # "live" in the daemon's belief until new work began.
    import time
    import types

    from sidepulse.live_activity import (
        IDLE_HEARTBEAT_SECONDS,
        LiveActivityConfig,
        LiveActivityDaemon,
        TokenStore,
    )

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))
    daemon.tokens.register("update", "upd", {"device": "phone", "activity_id": "a"})
    daemon.monitor = types.SimpleNamespace(
        snapshot=lambda include_stale=False: types.SimpleNamespace(
            statuses=[], aggregate=types.SimpleNamespace(mode=AgentMode.IDLE_READY)
        )
    )
    daemon._recent_finished = {
        "claude:session:a": {
            "id": "claude:session:a",
            "name": "done",
            "mode": "completed",
            "provider": "claude",
            "finishedAt": time.time(),
            "unread": False,
        }
    }
    sent = []
    monkeypatch.setattr(
        daemon,
        "_apns_fanout",
        lambda kind, payload, priority=10: (sent.append(priority) or True),
    )

    daemon._tick()
    assert sent == [10], "the first tick is a structural change"

    # Nothing changes from here on: only the heartbeat can push.
    daemon._tick()
    assert sent == [10]

    daemon._last_push_at -= IDLE_HEARTBEAT_SECONDS - 1
    daemon._tick()
    assert sent == [10], "not due yet"

    daemon._last_push_at -= 2
    daemon._tick()
    assert sent == [10, 5], "a silent probe, so a dead activity 410s"


def test_a_reset_echoing_our_own_start_push_is_ignored(tmp_path, monkeypatch):
    # Starting an activity ends the previous one, and .immediate dismissal
    # flips that one to .dismissed — which the app cannot tell from a
    # swipe-away, so it reports "no activity" a second after every start
    # push. Believing it ended the activity we had just created, and the
    # next start push repeated the whole cycle: the island never survived.
    from sidepulse.live_activity import (
        RESET_ECHO_SECONDS,
        LiveActivityConfig,
        LiveActivityDaemon,
        TokenStore,
    )

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = LiveActivityDaemon(config, token_store=TokenStore(tmp_path / "tok.json"))

    # No start push has ever gone out: a reset is genuine news.
    assert daemon._is_reset_echo(10_000.0) is False

    daemon._last_start_push_at = 10_000.0
    assert daemon._is_reset_echo(10_001.0) is True, "one second later: our own echo"
    assert daemon._is_reset_echo(10_000.0 + RESET_ECHO_SECONDS + 1) is False, "later: genuine"


def test_deep_link_resolver_builds_url_from_bridge_session_id(tmp_path):
    from sidepulse.live_activity import DeepLinkResolver

    project = tmp_path / "-Users-me-repo"
    project.mkdir()
    (project / "abc.jsonl").write_text(
        json.dumps({
            "type": "bridge-session",
            "sessionId": "abc",
            "bridgeSessionId": "cse_01Example",
        }) + "\n"
    )
    (project / "rc.jsonl").write_text(
        json.dumps({
            "type": "system",
            "url": "https://claude.ai/code/session_01FromUrlField",
        }) + "\n"
    )

    resolver = DeepLinkResolver()
    resolver._roots = [tmp_path]
    resolver._registry = tmp_path / "sessions"
    assert resolver.link_for("claude", "abc") == "https://claude.ai/code/session_01Example"
    assert resolver.link_for("claude", "rc") == "https://claude.ai/code/session_01FromUrlField"
    assert resolver.link_for("codex", "abc") is None
    assert resolver.link_for("claude", "remote:air:abc") is None


def test_deep_link_resolver_builds_codex_remote_url(tmp_path):
    from sidepulse.live_activity import DeepLinkResolver

    codex_state = tmp_path / ".codex"
    codex_state.mkdir()
    (codex_state / ".codex-global-state.json").write_text(json.dumps({
        "electron-local-remote-control-environment-id": "env_e_0123abc",
    }))
    resolver = DeepLinkResolver()
    resolver._codex_state_dir = codex_state
    resolver._codex_global_state = codex_state / ".codex-global-state.json"
    thread_id = "01a05c0f-63d5-7401-8b3e-0aef600ecf82"

    assert resolver.link_for("codex", thread_id) == (
        "https://chatgpt.com/app/codex/remote/thread/"
        f"{thread_id}?hostId=slingshot%3Aenv_e_0123abc%3A8765"
    )
    assert resolver.link_for("codex", "not-a-thread") is None
    assert resolver.link_for("codex", f"remote:mini:{thread_id}") is None


def test_deep_link_resolver_falls_back_to_enabled_codex_enrollment(tmp_path):
    from sidepulse.live_activity import DeepLinkResolver

    codex_state = tmp_path / ".codex"
    codex_state.mkdir()
    database = codex_state / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE remote_control_enrollments ("
        "environment_id TEXT NOT NULL, updated_at INTEGER NOT NULL, "
        "remote_control_enabled INTEGER)"
    )
    connection.executemany(
        "INSERT INTO remote_control_enrollments VALUES (?, ?, ?)",
        [
            ("env_e_disabled", 20, None),
            ("env_e_current", 10, 1),
        ],
    )
    connection.commit()
    connection.close()

    resolver = DeepLinkResolver()
    resolver._codex_state_dir = codex_state
    resolver._codex_global_state = codex_state / "missing.json"
    thread_id = "01a05c0f-63d5-7401-8b3e-0aef600ecf82"

    assert resolver.link_for("codex", thread_id) == (
        "https://chatgpt.com/app/codex/remote/thread/"
        f"{thread_id}?hostId=slingshot%3Aenv_e_current%3A8765"
    )


def test_status_row_prefers_a_propagated_remote_deep_link(monkeypatch):
    import sidepulse.live_activity as la

    class Stub:
        def link_for(self, provider, session_id):
            raise AssertionError("the remote host's link must win")

    thread_id = "01a05c0f-63d5-7401-8b3e-0aef600ecf82"
    deep_link = (
        "https://chatgpt.com/app/codex/remote/thread/"
        f"{thread_id}?hostId=slingshot%3Aenv_e_0123abc%3A8765"
    )
    status = AgentStatus(
        provider="codex",
        agent_id=f"codex:session:remote:mini:{thread_id}",
        display_name="Remote task",
        mode=AgentMode.WORKING,
        updated_at=datetime.now(timezone.utc),
        event_name="PreToolUse",
        session_id=f"remote:mini:{thread_id}",
        deep_link=deep_link,
    )
    monkeypatch.setattr(la, "_DEEP_LINKS", Stub())

    assert status_row(status)["deepLink"] == deep_link


def test_deep_link_resolver_retries_a_miss_after_ttl(tmp_path, monkeypatch):
    from sidepulse.live_activity import DeepLinkResolver

    project = tmp_path / "-Users-me-repo"
    project.mkdir()
    transcript = project / "late.jsonl"
    transcript.write_text("{}\n")

    resolver = DeepLinkResolver()
    resolver._roots = [tmp_path]
    resolver._registry = tmp_path / "sessions"
    now = 1000.0
    monkeypatch.setattr("sidepulse.live_activity.time.time", lambda: now)

    assert resolver.link_for("claude", "late") is None
    # The bridge attaches only after the first look.
    transcript.write_text(json.dumps({"bridgeSessionId": "cse_01Late"}) + "\n")
    assert resolver.link_for("claude", "late") is None  # still inside the TTL
    now += DeepLinkResolver.MISS_TTL_SECONDS + 1
    assert resolver.link_for("claude", "late") == "https://claude.ai/code/session_01Late"


def test_deep_link_resolver_reads_the_session_registry(tmp_path):
    from sidepulse.live_activity import DeepLinkResolver

    # Remote-control workers record their bridge id only in the per-pid
    # registry, never in the transcript.
    registry = tmp_path / "sessions"
    registry.mkdir()
    (registry / "123.json").write_text(
        json.dumps({
            "pid": 123,
            "sessionId": "worker",
            "bridgeSessionId": "session_01FromRegistry",
        })
    )
    (registry / "456.json").write_text(json.dumps({"pid": 456, "sessionId": "other"}))

    resolver = DeepLinkResolver()
    resolver._roots = [tmp_path / "projects"]
    resolver._registry = registry
    assert resolver.link_for("claude", "worker") == "https://claude.ai/code/session_01FromRegistry"
    assert resolver.link_for("claude", "other") is None


def test_remembered_finished_rows_backfill_deep_links(tmp_path, monkeypatch):
    import sidepulse.live_activity as la

    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    config = la.LiveActivityConfig(apns_key_path=tmp_path / "k.p8", apns_key_id="X", apns_team_id="Y")
    daemon = la.LiveActivityDaemon(config, token_store=la.TokenStore(tmp_path / "tok.json"))
    daemon._recent_finished["claude:session:abc"] = {
        "id": "claude:session:abc", "name": "Old", "mode": "completed",
        "provider": "claude", "finishedAt": 1.0, "unread": True,
    }

    class Stub:
        def link_for(self, provider, session_id):
            assert (provider, session_id) == ("claude", "abc")
            return "https://claude.ai/code/session_01X"

    monkeypatch.setattr(la, "_DEEP_LINKS", Stub())
    daemon._remember_finished([], 2.0)
    assert daemon._recent_finished["claude:session:abc"]["deepLink"] == "https://claude.ai/code/session_01X"
