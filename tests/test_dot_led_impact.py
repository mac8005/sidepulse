import pytest

from sidepulse.live_activity import _parse_dot_display_signatures
from test_dot_completion_alerts import make_daemon, observe, snapshot


def display_profile(digest="a"):
    return {f"{state}:{read}": digest * 64
            for state in ("idle", "ask", "working", "done")
            for read in ("read", "unread")}


def test_display_profile_validation():
    profile = display_profile()
    assert _parse_dot_display_signatures({}) is None
    assert _parse_dot_display_signatures({"reportedAt": 100, "dotDisplaySignatures": profile}) == profile
    for payload in (
        {"dotDisplaySignatures": profile},
        {"reportedAt": True, "dotDisplaySignatures": profile},
        {"reportedAt": float("nan"), "dotDisplaySignatures": profile},
        {"reportedAt": 100, "dotDisplaySignatures": {}},
        {"reportedAt": 100, "dotDisplaySignatures": display_profile("z")},
    ):
        with pytest.raises(ValueError):
            _parse_dot_display_signatures(payload)


def test_only_current_owner_reports_replace_display_profile(tmp_path, monkeypatch):
    daemon, _ = make_daemon(tmp_path, monkeypatch)
    profile = display_profile()
    assert daemon.report_dot_availability("phone", True, reported_at=200, now=200, display_signatures=profile)
    assert not daemon.report_dot_availability("other", True, reported_at=300, now=300, display_signatures=display_profile("b"))
    assert daemon.report_dot_availability("phone", True, reported_at=100, now=300, display_signatures=display_profile("c"))
    assert daemon.report_dot_availability("phone", True, reported_at=300, now=300)
    assert daemon.tokens.entries("dot_device")["phone"]["dot_display_signatures"] == profile


def test_unread_completion_does_not_change_attention_led(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    before = {**snapshot(100), "aggregateMode": "waiting_for_input"}
    after = {**snapshot(110, ("finished", 110)), "aggregateMode": "waiting_for_input"}
    assert not daemon._maybe_send_dot_completion_alert("ask", before, 100)
    assert not daemon._maybe_send_dot_completion_alert("ask", after, 110)
    assert not sent


def test_disabled_overlay_uses_phone_rendered_programs(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    daemon.tokens.update_metadata("dot_device", "phone", {
        "dot_display_signatures": {"working:read": "a" * 64, "working:unread": "a" * 64},
    })
    assert not observe(daemon, snapshot(100), 100)
    assert not observe(daemon, snapshot(110, ("finished", 110)), 110)
    assert not sent


def test_already_written_led_does_not_need_visible_push(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch)
    assert not observe(daemon, snapshot(100), 100)
    after = snapshot(110, ("finished", 110))
    daemon._queue_dot_state("working", after, 110)
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=110)
    assert not observe(daemon, after, 111)
    assert not sent
