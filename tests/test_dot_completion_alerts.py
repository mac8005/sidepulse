from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from sidepulse.live_activity import (
    DOT_PUSH_EXPIRY_SECONDS,
    DotDndSchedule,
    LiveActivityConfig,
    LiveActivityDaemon,
    TokenStore,
)


def make_daemon(tmp_path, monkeypatch, *, enabled=True):
    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    daemon = LiveActivityDaemon(
        LiveActivityConfig(tmp_path / "unused.p8", "unused", "unused", summaries_enabled=False, port=0),
        token_store=TokenStore(tmp_path / "tokens.json"),
    )
    if not daemon.tokens.tokens("dot_device"):
        daemon.tokens.register("dot_device", "phone", {})
    if enabled:
        daemon.report_dot_completion_alerts("phone", True)
    sent = []
    monkeypatch.setattr(daemon.apns, "send", lambda token, payload, **options: (sent.append((token, payload, options)) or (200, "")))
    return daemon, sent


def snapshot(now, *completed):
    return {
        "aggregateMode": "working",
        "activeCount": 1,
        "agents": [
            {"id": agent_id, "name": "Private project name", "mode": "completed", "finishedAt": finished_at, "unread": True}
            for agent_id, finished_at in completed
        ],
        "updatedAt": now,
    }


def observe(daemon, state, now):
    daemon._latest = state
    daemon._observe_dot_state("working", state, now)
    return daemon._maybe_send_dot_completion_alert("working", state, now)


def test_visible_completion_is_generic_soundless_and_replaces_first_silent_push(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    assert not observe(daemon, snapshot(100), 100)
    assert observe(daemon, snapshot(110, ("finished", 110)), 110)
    assert len(sent) == 1
    token, payload, options = sent[0]
    assert token == "phone"
    assert options["push_type"] == "alert" and options["priority"] == 10
    assert payload["aps"]["alert"] == {"title": "Session finished", "body": "Open SidePulse to view the result."}
    assert payload["aps"]["mutable-content"] == 1
    assert "sound" not in payload["aps"]
    assert "interruption-level" not in payload["aps"]
    assert "Private project" not in json.dumps(payload)
    assert payload["dot"]["hasUnreadFinished"] is True
    assert payload["dot"]["commandID"] == daemon._pending_dot.command_id
    assert daemon._pending_dot.accepted_attempts == 1
    assert not daemon._send_pending_dot_if_due(110)
    assert daemon._send_pending_dot_if_due(230)
    assert len(sent) == 2 and sent[1][2]["push_type"] == "background"
    assert sent[1][1]["dot"]["commandID"] == payload["dot"]["commandID"]


def test_completion_identity_not_unread_boolean_controls_new_alerts(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    assert not observe(daemon, snapshot(100, ("first", 90)), 100)
    assert observe(daemon, snapshot(110, ("first", 90), ("second", 110)), 110)
    assert not observe(daemon, snapshot(111, ("second", 110), ("first", 90)), 111)
    assert observe(daemon, snapshot(120, ("second", 120), ("first", 90)), 120)
    assert len(sent) == 2


def test_restart_never_replays_historical_or_already_alerted_rows(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    observe(daemon, snapshot(100), 100)
    observe(daemon, snapshot(110, ("first", 110)), 110)
    assert len(sent) == 1
    restarted, sent = make_daemon(tmp_path, monkeypatch)
    assert not observe(restarted, snapshot(120, ("first", 110), ("offline", 115)), 120)
    assert not observe(restarted, snapshot(121, ("first", 110)), 121)
    assert not observe(restarted, snapshot(122, ("offline", 115)), 122)
    assert observe(restarted, snapshot(130, ("new", 130)), 130)
    assert len(sent) == 1


@pytest.mark.parametrize("suppression", ["foreground", "no_folder", "dnd", "focus", "brightness_zero", "write_failed", "disconnected", "scheduled_dnd"])
def test_suppressed_completions_are_consumed_not_replayed(tmp_path, monkeypatch, suppression):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    observe(daemon, snapshot(100), 100)
    if suppression == "foreground":
        daemon._dot_stream_connected("phone")
    elif suppression == "scheduled_dnd":
        daemon._record_dot_dnd_schedule("phone", DotDndSchedule(True, 105, True, 100), now=100)
        daemon._apply_due_dot_dnd_transition(105)
    elif suppression == "focus":
        daemon.report_dot_focus("phone", True, 105, now=105)
    else:
        daemon.report_dot_availability("phone", False, suppression, 600, 105, now=105)
    state = snapshot(110, ("finished", 110))
    assert not observe(daemon, state, 110)
    assert sent == []
    assert daemon.dot_command("phone", now=110)["dot"] is None
    if suppression == "foreground":
        daemon._dot_stream_disconnected("phone")
    elif suppression == "scheduled_dnd":
        daemon._record_dot_dnd_schedule("phone", DotDndSchedule(False, None, None, 120), now=120)
        daemon.report_dot_availability("phone", True, reported_at=120, now=120)
    elif suppression == "focus":
        daemon.report_dot_focus("phone", False, 120, now=120)
    else:
        daemon.report_dot_availability("phone", True, reported_at=120, now=120)
    assert not observe(daemon, {**state, "updatedAt": 121}, 121)
    assert sent == []


def test_manual_dnd_off_inside_schedule_window_allows_new_completions(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    observe(daemon, snapshot(100), 100)
    daemon.report_dot_availability("phone", False, "dnd", 600, 101, now=101)
    daemon.report_dot_availability(
        "phone", True, reported_at=105,
        dnd_schedule=DotDndSchedule(True, 200, False, 105), now=105,
    )
    assert observe(daemon, snapshot(110, ("finished", 110)), 110)
    assert len(sent) == 1
    assert daemon.dot_command("phone", now=110)["available"] is True


def test_manual_dnd_on_outside_schedule_survives_availability_lease(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    observe(daemon, snapshot(100), 100)
    daemon.report_dot_availability(
        "phone", False, "dnd", 60, 105,
        DotDndSchedule(True, 500, True, 105), now=105,
    )
    state = snapshot(200, ("finished", 200))
    assert not observe(daemon, state, 200)
    response = daemon.dot_command("phone", now=200)
    assert response["available"] is False and response["unavailableReason"] == "dnd"
    daemon.report_dot_availability("phone", False, "write_failed", 60, 201, now=201)
    assert daemon.dot_command("phone", now=270)["unavailableReason"] == "dnd"
    daemon.report_dot_availability("phone", True, reported_at=280, now=280)
    assert not observe(daemon, {**state, "updatedAt": 281}, 281)
    assert sent == []
    assert observe(daemon, snapshot(290, ("new", 290)), 290)


def test_default_off_and_enabling_does_not_replay_completed_rows(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch, enabled=False)
    observe(daemon, snapshot(100), 100)
    state = snapshot(110, ("finished", 110))
    assert not observe(daemon, state, 110)
    assert not daemon._dot_health(now=110)["dotCompletionAlertsEnabled"]
    assert daemon.dot_command("phone", now=110)["dot"] is None
    daemon.report_dot_completion_alerts("phone", True)
    assert not observe(daemon, {**state, "updatedAt": 120}, 120)
    assert observe(daemon, snapshot(130, ("new", 130)), 130)
    assert len(sent) == 1


def test_ack_failure_and_apns_failure_do_not_repeat_visible_alert(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon.apns, "send", lambda token, payload, **options: (sent.append((token, payload, options)) or (503, "Unavailable")))
    observe(daemon, snapshot(100), 100)
    state = snapshot(110, ("finished", 110))
    assert not observe(daemon, state, 110)
    command = daemon._pending_dot.command_id
    assert not daemon.ack_dot(command, "failed", now=111)
    assert not observe(daemon, {**state, "updatedAt": 112}, 112)
    assert len(sent) == 1
    assert not daemon._send_pending_dot_if_due(112)
    assert len(sent) == 2 and sent[-1][2]["push_type"] == "background"


def test_command_read_is_latest_expiry_checked_and_budget_neutral(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    observe(daemon, snapshot(100), 100)
    observe(daemon, snapshot(110, ("first", 110)), 110)
    old_id = daemon._pending_dot.command_id
    observe(daemon, snapshot(120, ("second", 120)), 120)
    daemon._latest = {**daemon._latest, "aggregateMode": "tool_running", "updatedAt": 125}
    command = daemon.dot_command("phone", now=125)
    assert command["dot"]["commandID"] != old_id
    assert command["dot"]["aggregateMode"] == "tool_running"
    assert command["dot"]["updatedAt"] == 125
    assert len(sent) == 2
    assert daemon._pending_dot.accepted_attempts == 1
    assert daemon.dot_command("another-phone", now=125) is None
    assert daemon.dot_command(None, now=125) is None
    assert daemon.dot_command("phone", now=120 + DOT_PUSH_EXPIRY_SECONDS)["dot"] is None
    daemon._latest = snapshot(126)
    assert daemon.dot_command("phone", now=126)["dot"] is None
    assert len(sent) == 2


@pytest.fixture
def http_daemon(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch, enabled=False)
    server = daemon._build_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    def request(method, path, body=None, headers=None):
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    try:
        yield daemon, sent, request, server
    finally:
        daemon.stop()
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_preferences_are_owner_only_sparse_and_explicit(http_daemon):
    daemon, sent, request, _ = http_daemon
    assert request("POST", "/dot-availability", {"token": "other", "available": True, "dotCompletionAlertsEnabled": True})[0] == 409
    assert not daemon._dot_health()["dotCompletionAlertsEnabled"]
    assert request("POST", "/register", {"kind": "dot_device", "token": "phone", "dotCompletionAlertsEnabled": True})[0] == 200
    assert daemon._dot_health()["dotCompletionAlertsEnabled"]
    assert request("POST", "/register", {"kind": "dot_device", "token": "phone"})[0] == 200
    assert request("POST", "/dot-availability", {"token": "phone", "available": True})[0] == 200
    assert daemon._dot_health()["dotCompletionAlertsEnabled"]
    assert request("POST", "/dot-availability", {"token": "phone", "available": True, "dotCompletionAlertsEnabled": "true"})[0] == 400
    assert request("POST", "/dot-availability", {"token": "phone", "available": True, "dotCompletionAlertsEnabled": False})[0] == 200
    assert not daemon._dot_health()["dotCompletionAlertsEnabled"]
    assert sent == []


def test_http_command_requires_owner_header_and_never_sends_push(http_daemon):
    daemon, sent, request, server = http_daemon
    daemon.report_dot_completion_alerts("phone", True)
    observe(daemon, snapshot(100), 100)
    observe(daemon, snapshot(110, ("finished", 110)), 110)
    daemon._pending_dot.created_at = 10**12
    assert request("GET", "/dot-command")[0] == 403
    assert request("GET", "/dot-command?dotToken=phone")[0] == 403
    assert request("GET", "/dot-command", headers={"X-SidePulse-Dot-Token": "other"})[0] == 403
    code, body = request("GET", "/dot-command", headers={"X-SidePulse-Dot-Token": "phone"})
    assert code == 200
    assert body["dot"]["commandID"] == daemon._pending_dot.command_id
    assert "phone" not in json.dumps(body)
    assert len(sent) == 1
    stream = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        stream.request("GET", "/stream", headers={"X-SidePulse-Dot-Token": "phone"})
        response = stream.getresponse()
        assert response.status == 200
        frame = json.loads(response.readline().decode().removeprefix("data: "))
        assert frame["dotCommandID"] == daemon._pending_dot.command_id
        assert daemon._dot_owner_stream_count() == 1
    finally:
        stream.close()


def test_old_availability_and_ack_cannot_reverse_newer_opt_out(http_daemon):
    daemon, sent, request, _ = http_daemon
    daemon.report_dot_completion_alerts("phone", True)
    assert request("POST", "/dot-availability", {"token": "phone", "available": True, "reportedAt": 200, "dotCompletionAlertsEnabled": False})[0] == 200
    assert request("POST", "/dot-availability", {"token": "phone", "available": True, "reportedAt": 100, "dotCompletionAlertsEnabled": True})[0] == 200
    assert not daemon._dot_health()["dotCompletionAlertsEnabled"]
    assert request("POST", "/register", {"kind": "dot_device", "token": "phone", "reportedAt": 100, "dotCompletionAlertsEnabled": True})[0] == 200
    assert not daemon._dot_health()["dotCompletionAlertsEnabled"]
    assert request("POST", "/register", {"kind": "dot_device", "token": "phone", "dotCompletionAlertsEnabled": True})[0] == 200
    assert not daemon._dot_health()["dotCompletionAlertsEnabled"]
    daemon._queue_dot_state("working", snapshot(210), 210)
    assert request("POST", "/dot-ack", {"commandID": daemon._pending_dot.command_id, "status": "written", "dotCompletionAlertsEnabled": True})[0] == 200
    assert not daemon._dot_health()["dotCompletionAlertsEnabled"]
    assert sent == []
    assert request("POST", "/register", {"kind": "dot_device", "token": "phone", "reportedAt": 300, "dotCompletionAlertsEnabled": True})[0] == 200
    assert daemon._dot_health()["dotCompletionAlertsEnabled"]


def test_command_http_response_is_not_cacheable(http_daemon):
    _, _, _, server = http_daemon
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/dot-command", headers={"X-SidePulse-Dot-Token": "phone"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store"
        response.read()
    finally:
        connection.close()
