from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sidepulse.collector import AgentMonitor, LiveAgentMonitor, agent_status_from_dict
from sidepulse.live_activity import LiveActivityConfig, LiveActivityDaemon, TokenStore, status_row
from sidepulse.models import AgentMode, AgentStatus, HookEvent
from sidepulse.remote_hosts import _emit_envelope, qualify_remote_line
from sidepulse.providers import parse_log_line
from sidepulse.scheduled_sessions import _read_scheduled_session_ids, is_scheduled_session, scheduled_session_ids


class ScheduledSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        _read_scheduled_session_ids.cache_clear()
        self.addCleanup(_read_scheduled_session_ids.cache_clear)
        links = patch("sidepulse.live_activity._DEEP_LINKS", None)
        links.start()
        self.addCleanup(links.stop)
        with sqlite3.connect(self.root / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, thread_source TEXT)")
            db.executemany("INSERT INTO threads VALUES (?, ?)", [
                ("scheduled", "automation"), ("interactive", "app"),
            ])
        self.now = datetime.now(timezone.utc)

    def status(self, mode=AgentMode.WORKING, session="scheduled", **kwargs):
        return AgentStatus(
            provider="codex", agent_id=f"codex:session:{session}",
            session_id=session, display_name="Kleido Android spend guard",
            mode=mode, event_name="AgentProgress",
            updated_at=self.now - timedelta(seconds=30), **kwargs,
        )

    def snapshot(self, statuses, live=False):
        monitor = LiveAgentMonitor() if live else AgentMonitor(sources=())
        if live:
            monitor.statuses_by_key = {s.agent_id: s for s in statuses}
            return monitor.snapshot(include_stale=True)
        with patch.object(monitor, "_latest_statuses", return_value={s.agent_id: s for s in statuses}):
            return monitor.snapshot(include_stale=True)

    def test_both_monitors_hide_healthy_modes_but_keep_attention(self):
        for live in (False, True):
            for mode in AgentMode:
                with self.subTest(live=live, mode=mode):
                    snapshot = self.snapshot([self.status(mode)], live)
                    visible = snapshot.statuses + snapshot.stale_statuses
                    expected = mode in {AgentMode.WAITING_FOR_INPUT, AgentMode.BLOCKED_ERROR, AgentMode.UNKNOWN}
                    self.assertEqual(bool(visible), expected)
                    if not expected:
                        self.assertEqual(snapshot.aggregate.active_count, 0)
                        self.assertEqual(snapshot.aggregate.mode, AgentMode.IDLE_READY)

    def test_interactive_with_same_title_and_other_provider_stay_visible(self):
        interactive = self.status(session="interactive")
        claude = replace(self.status(), provider="claude", agent_id="claude:session:scheduled")
        snapshot = self.snapshot([interactive, claude])
        self.assertEqual(len(snapshot.statuses), 2)

    def test_recoverable_tool_error_stays_hidden(self):
        status = replace(self.status(AgentMode.BLOCKED_ERROR), event_name="PostToolUseFailure")
        self.assertFalse(self.snapshot([status]).statuses)

    def test_remote_requires_source_host_marker_and_survives_serialization(self):
        session = "remote:mini:scheduled"
        unmarked = self.status(session=session)
        self.assertTrue(self.snapshot([unmarked]).statuses)
        marked = replace(unmarked, scheduled=True)
        restored = agent_status_from_dict(marked.to_dict())
        self.assertTrue(restored.scheduled)
        self.assertFalse(self.snapshot([restored], live=True).statuses)
        waiting = replace(restored, mode=AgentMode.WAITING_FOR_INPUT)
        self.assertTrue(self.snapshot([waiting], live=True).statuses)

    def test_remote_stream_attaches_classification_and_collector_retains_it(self):
        output = io.StringIO()
        line = {"logged_at": self.now.isoformat(), "event": {
            "session_id": "scheduled", "hook_event_name": "UserPromptSubmit", "prompt": "Check guard",
        }}
        with patch("sidepulse.remote_hosts.remote_session_web_link", return_value=None):
            _emit_envelope("codex", json.dumps(line), output)
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["line"]["event"]["sidepulse_scheduled_session"])
        qualified = qualify_remote_line("codex", envelope["line"], "mini")
        monitor = LiveAgentMonitor()
        monitor.ingest_record(parse_log_line("codex", json.dumps(qualified)))
        self.assertFalse(monitor.snapshot().statuses)
        session = "remote:mini:scheduled"
        for event, visible in (("PermissionRequest", True), ("UserPromptSubmit", False), ("Stop", False)):
            monitor.ingest_record(HookEvent(
                provider="codex", logged_at=self.now, event_name=event,
                session_id=session, raw={"hook_event_name": event},
            ))
            self.assertEqual(bool(monitor.snapshot().statuses), visible)

    def test_late_classification_rechecks_without_new_event(self):
        status = self.status(session="late")
        self.assertTrue(self.snapshot([status]).statuses)
        with sqlite3.connect(self.root / "state_5.sqlite") as db:
            db.execute("INSERT INTO threads VALUES ('late', 'automation')")
        _read_scheduled_session_ids.cache_clear()
        self.assertFalse(self.snapshot([status]).statuses)

    def test_missing_schema_keeps_sessions_and_does_not_create_database(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "missing")}):
            self.assertEqual(scheduled_session_ids(), frozenset())
            self.assertFalse((self.root / "missing").exists())
        with sqlite3.connect(self.root / "state_5.sqlite") as db:
            db.execute("DROP TABLE threads")
        _read_scheduled_session_ids.cache_clear()
        self.assertTrue(self.snapshot([self.status()]).statuses)

    def test_malformed_session_identity_does_not_break_remote_stream(self):
        for session in (None, "", 1, {}, []):
            with self.subTest(session=session):
                self.assertFalse(is_scheduled_session("codex", session))

    def test_daemon_removes_saved_and_recovered_scheduled_completions(self):
        with patch("sidepulse.live_activity.default_state_dir", return_value=self.root):
            daemon = LiveActivityDaemon(
                LiveActivityConfig(apns_key_path=self.root / "unused", apns_key_id="unused",
                                   apns_team_id="unused", summaries_enabled=False),
                TokenStore(self.root / "tokens.json"),
            )
        local = self.status(AgentMode.COMPLETED)
        remote = self.status(AgentMode.BLOCKED_ERROR, "remote:mini:scheduled", scheduled=True)
        ordinary = self.status(AgentMode.COMPLETED, "interactive")
        daemon._recent_finished = {
            s.agent_id: {**status_row(s), "finishedAt": self.now.timestamp(), "unread": True}
            for s in (local, ordinary)
        }
        daemon._agent_modes = {remote.agent_id: "blocked_error"}
        daemon._last_rows = {remote.agent_id: status_row(remote)}
        daemon._remember_finished([], self.now.timestamp())
        self.assertEqual(set(daemon._recent_finished), {ordinary.agent_id})
        saved = json.loads(daemon._recent_finished_path.read_text())
        self.assertEqual(set(saved), {ordinary.agent_id})


if __name__ == "__main__":
    unittest.main()
