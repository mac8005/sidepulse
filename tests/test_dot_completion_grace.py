from test_dot_completion_alerts import make_daemon, observe, snapshot


def test_stable_completion_alerts_once_after_ten_seconds(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch, completion_delay=10)
    assert not observe(daemon, snapshot(100), 100)
    done = snapshot(110, ("session", 110))
    assert not observe(daemon, done, 110)
    assert not daemon._send_pending_dot_if_due(110)
    assert not observe(daemon, done, 119)
    assert not sent
    assert observe(daemon, done, 120)
    assert not observe(daemon, done, 130)
    assert len(sent) == 1
    assert sent[0][1]["aps"]["alert"]["title"] == "Session finished"


def test_five_second_completion_resume_sends_neither_notice(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch, completion_delay=10)
    assert not observe(daemon, snapshot(100), 100)
    assert not observe(daemon, snapshot(110), 110)
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=110)
    assert not observe(daemon, snapshot(120, ("session", 120)), 120)
    assert not daemon._send_pending_dot_if_due(120)
    resumed = {**snapshot(125), "agents": [{"id": "session", "mode": "working"}]}
    assert not observe(daemon, resumed, 125)
    assert not observe(daemon, resumed, 135)
    assert daemon._dot_completion_candidate is None
    assert not sent


def test_unsent_completion_needs_no_resume_notice_without_prior_ack(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch, completion_delay=10)
    assert not observe(daemon, snapshot(100), 100)
    assert not observe(daemon, snapshot(110, ("session", 110)), 110)
    assert not daemon._send_pending_dot_if_due(110)
    resumed = {**snapshot(115), "agents": [{"id": "session", "mode": "working"}]}
    assert not observe(daemon, resumed, 115)
    assert not observe(daemon, resumed, 130)
    assert not sent


def test_silent_write_during_grace_does_not_need_a_banner(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch, completion_delay=10)
    assert not observe(daemon, snapshot(100), 100)
    done = snapshot(110, ("session", 110))
    assert not observe(daemon, done, 110)
    assert daemon.ack_dot(daemon._pending_dot.command_id, "written", now=115)
    assert not observe(daemon, done, 120)
    assert daemon._dot_completion_candidate is None
    assert not sent


def test_dnd_during_grace_cancels_without_replay(tmp_path, monkeypatch):
    daemon, sent = make_daemon(tmp_path, monkeypatch, completion_delay=10)
    assert not observe(daemon, snapshot(100), 100)
    done = snapshot(110, ("session", 110))
    assert not observe(daemon, done, 110)
    daemon.report_dot_availability("phone", False, "dnd", 60, reported_at=115, now=115)
    assert not observe(daemon, done, 115)
    daemon.report_dot_availability("phone", True, reported_at=120, now=120)
    assert not observe(daemon, done, 130)
    assert not sent
