from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore


def make_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr("sidepulse.live_activity.default_state_dir", lambda: tmp_path)
    daemon = LiveActivityDaemon(
        LiveActivityConfig(tmp_path / "unused.p8", "unused", "unused", summaries_enabled=False, port=0),
        token_store=TokenStore(tmp_path / "tokens.json"),
    )
    daemon.tokens.register("dot_device", "phone", {})
    return daemon


def state(*, unread=False, mode="working", updated_at=100.0):
    return {
        "aggregateMode": mode,
        "activeCount": 1 if mode == "working" else 0,
        "agents": [{"id": "finished", "mode": "completed", "unread": unread}],
        "updatedAt": updated_at,
    }


def test_foreground_receipt_prevents_redundant_push_after_backgrounding(tmp_path, monkeypatch):
    daemon = make_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(daemon.apns, "send", lambda *args, **kw: (sent.append(args) or (200, "")))
    current = state(unread=True)
    daemon._queue_dot_state("working", current, now=100)
    daemon._dot_stream_connected("phone")

    frame = daemon._dot_stream_snapshot(current, "phone")
    assert frame["dotCommandID"] == daemon._pending_dot.command_id
    assert "dotCommandID" not in current
    assert daemon.ack_dot(frame["dotCommandID"], "written", now=101)

    daemon._dot_stream_disconnected("phone")
    assert not daemon._send_pending_dot_if_due(102)
    assert sent == []
    assert daemon._last_dot_has_unread_finished is True


def test_only_the_dot_owner_receives_the_write_receipt(tmp_path, monkeypatch):
    daemon = make_daemon(tmp_path, monkeypatch)
    current = state(unread=True)
    daemon._queue_dot_state("working", current, now=100)
    for token in (None, "another-phone"):
        assert "dotCommandID" not in daemon._dot_stream_snapshot(current, token)


def test_receipt_never_attaches_to_a_different_or_older_snapshot(tmp_path, monkeypatch):
    daemon = make_daemon(tmp_path, monkeypatch)
    current = state(unread=True, updated_at=200)
    daemon._queue_dot_state("working", current, now=200)
    for other in (
        state(unread=False, updated_at=201),
        state(unread=True, mode="completed", updated_at=201),
        state(unread=True, updated_at=199),
    ):
        assert "dotCommandID" not in daemon._dot_stream_snapshot(other, "phone")


def test_all_read_receipt_uses_the_same_off_normalization_as_pushes(tmp_path, monkeypatch):
    daemon = make_daemon(tmp_path, monkeypatch)
    current = state(mode="completed")
    daemon._queue_dot_state("idle", {**current, "aggregateMode": "idle_ready"}, now=100)
    frame = daemon._dot_stream_snapshot(current, "phone")
    assert daemon.ack_dot(frame["dotCommandID"], "written", now=101)
    assert daemon._last_dot_state == "idle"


def test_unread_change_still_pushes_immediately_after_foreground_receipt(tmp_path, monkeypatch):
    daemon = make_daemon(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(daemon.apns, "send", lambda *args, **kw: (sent.append(args) or (200, "")))
    baseline = state()
    daemon._observe_dot_state("working", baseline, now=90)
    daemon._observe_dot_state("working", baseline, now=100)
    frame = daemon._dot_stream_snapshot(baseline, "phone")
    assert daemon.ack_dot(frame["dotCommandID"], "written", now=101)
    assert daemon._observe_dot_state("working", state(unread=True, updated_at=102), now=102)
    assert daemon._send_pending_dot_if_due(102)
    assert sent[-1][1]["dot"]["hasUnreadFinished"] is True


def test_old_stream_receipt_cannot_acknowledge_newer_unread_state(tmp_path, monkeypatch):
    daemon = make_daemon(tmp_path, monkeypatch)
    baseline = state()
    daemon._queue_dot_state("working", baseline, now=100)
    old = daemon._dot_stream_snapshot(baseline, "phone")["dotCommandID"]
    current = state(unread=True, updated_at=102)
    daemon._queue_dot_state("working", current, now=102)
    assert not daemon.ack_dot(old, "written", now=103)
    assert daemon._pending_dot.has_unread_finished is True
    newer_frame = daemon._dot_stream_snapshot({**current, "updatedAt": 104}, "phone")
    assert newer_frame["dotCommandID"] == daemon._pending_dot.command_id


@pytest.mark.parametrize("queue_during_frame", [False, True])
def test_queued_receipt_wakes_unchanged_sse_and_acks_without_apns(tmp_path, monkeypatch, queue_during_frame):
    daemon = make_daemon(tmp_path, monkeypatch)
    daemon._latest = state(unread=True)
    waiting = threading.Event()
    original_wait = daemon._condition.wait

    def observe_wait(timeout=None):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(daemon._condition, "wait", observe_wait)
    if queue_during_frame:
        original_frame = daemon._dot_stream_snapshot
        queued = False

        def queue_after_frame_preparation(snapshot, token):
            nonlocal queued
            frame = original_frame(snapshot, token)
            if not queued:
                queued = True
                daemon._queue_dot_state("working", snapshot, now=100)
            return frame

        monkeypatch.setattr(daemon, "_dot_stream_snapshot", queue_after_frame_preparation)
    server = daemon._build_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stream = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    ack = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        stream.request("GET", "/stream?dotToken=phone")
        response = stream.getresponse()
        assert response.status == 200
        first = json.loads(response.readline().decode().removeprefix("data: "))
        assert "dotCommandID" not in first
        assert response.readline() == b"\n"
        if not queue_during_frame:
            assert waiting.wait(timeout=2)
            daemon._queue_dot_state("working", daemon._latest, now=100)
        frame = json.loads(response.readline().decode().removeprefix("data: "))
        command_id = frame["dotCommandID"]
        ack.request("POST", "/dot-ack", body=json.dumps({"commandID": command_id, "status": "written"}), headers={"Content-Type": "application/json"})
        acknowledged = ack.getresponse()
        assert acknowledged.status == 200
        assert json.loads(acknowledged.read())["acknowledged"] is True
        assert daemon._pending_dot is None
        assert daemon._last_dot_has_unread_finished is True
    finally:
        daemon.stop()
        stream.close()
        ack.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
