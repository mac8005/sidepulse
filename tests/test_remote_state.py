from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sidepulse import cli
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.remote_hosts import RemoteHost, load_remote_hosts, save_remote_hosts
from sidepulse.remote_state import (
    CanonicalUnread,
    RemoteUnreadStore,
    canonical_server_ids,
    canonical_status_for_unread,
    monitor_route_for_status,
    parse_unread_finished,
    post_seen,
)
from sidepulse.session_actions import SESSION_OPEN_APP, session_open_target


MONITOR_URL = "http://macmini8005:8787"
SESSION_ID = "019eec66-a7de-77b0-921e-f531ea8be597"


def remote_status(*, mode: AgentMode = AgentMode.COMPLETED) -> AgentStatus:
    return AgentStatus(
        provider="codex",
        agent_id=f"codex:session:remote:macmini:{SESSION_ID}",
        display_name="SidePulse: sync read state",
        mode=mode,
        updated_at=datetime.now(timezone.utc),
        event_name="Stop",
        session_id=f"remote:macmini:{SESSION_ID}",
        cwd="/Users/massimo/Git/sidepulse",
    )


def test_remote_host_monitor_url_is_optional_validated_and_persisted(tmp_path: Path) -> None:
    config = tmp_path / "remote-hosts.json"
    host = RemoteHost(
        "macmini",
        "mini",
        monitor_url=f"{MONITOR_URL}/",
    )
    assert host.monitor_url == MONITOR_URL

    save_remote_hosts((host,), config)

    assert load_remote_hosts(config) == (host,)
    assert json.loads(config.read_text())["hosts"][0]["monitor_url"] == MONITOR_URL
    assert "monitor_url" not in RemoteHost("other", "other").to_dict()

    for invalid in (
        "macmini8005:8787",
        "ftp://macmini8005:8787",
        "http://user@macmini8005:8787",
        "http://macmini8005:8787/snapshot",
        "http://macmini8005:8787?token=x",
    ):
        with pytest.raises(ValueError):
            RemoteHost("macmini", "mini", monitor_url=invalid)


def test_remote_cli_accepts_monitor_url() -> None:
    args = cli.build_sidepulse_parser().parse_args(
        [
            "remote",
            "add",
            "macmini",
            "--ssh",
            "mini",
            "--monitor-url",
            MONITOR_URL,
        ]
    )

    assert args.monitor_url == MONITOR_URL


def test_snapshot_rows_map_from_qualified_remote_status_to_canonical_id() -> None:
    host = RemoteHost("macmini", "mini", monitor_url=MONITOR_URL)
    status = remote_status()
    rows = parse_unread_finished(
        host.name,
        MONITOR_URL,
        {
            "agents": [
                {
                    "id": f"codex:session:{SESSION_ID}",
                    "name": "SidePulse: canonical completion",
                    "mode": "completed",
                    "finishedAt": 42.5,
                    "unread": True,
                    "provider": "codex",
                    "cwd": "sidepulse",
                    "deepLink": "https://chatgpt.com/app/codex/remote/thread/example",
                },
                {
                    "id": "codex:session:seen",
                    "mode": "completed",
                    "finishedAt": 41,
                    "unread": False,
                },
                {
                    "id": "codex:session:working",
                    "mode": "working",
                    "finishedAt": 40,
                    "unread": True,
                },
            ]
        },
    )

    assert monitor_route_for_status(status, (host,)) == host
    assert canonical_server_ids(status) == (
        status.agent_id,
        f"codex:session:{SESSION_ID}",
    )
    assert len(rows) == 1
    assert rows[0].key == (host.name, f"codex:session:{SESSION_ID}", 42.5)
    assert rows[0].provider == "codex"
    assert rows[0].name == "SidePulse: canonical completion"
    assert rows[0].cwd == "sidepulse"
    assert rows[0].deep_link == "https://chatgpt.com/app/codex/remote/thread/example"

    store = RemoteUnreadStore()
    store.retain_routes({host.name: MONITOR_URL})
    assert store.replace_host(host.name, rows)
    assert store.match_status(host.name, status, monitor_url=MONITOR_URL) == rows[0]


def test_concurrent_optimistic_failures_restore_independently_at_same_epoch() -> None:
    first = CanonicalUnread(
        host_name="macmini",
        monitor_url=MONITOR_URL,
        server_id=f"codex:session:{SESSION_ID}",
        finished_at=42.5,
    )
    second = CanonicalUnread(
        host_name="macmini",
        monitor_url=MONITOR_URL,
        server_id="claude:session:second",
        finished_at=43.5,
    )
    store = RemoteUnreadStore()
    store.retain_routes({first.host_name: first.monitor_url})
    store.replace_host(
        first.host_name,
        (first, second),
        monitor_url=MONITOR_URL,
    )
    first_token = store.optimistically_clear(first)
    second_token = store.optimistically_clear(second)
    assert first_token is not None
    assert second_token is not None
    assert first_token.epoch == second_token.epoch

    assert store.restore(first_token)
    assert store.restore(second_token)
    assert set(store.rows()) == {first, second}


def test_optimistic_restore_cannot_override_newer_snapshot_or_removed_route() -> None:
    row = CanonicalUnread(
        host_name="macmini",
        monitor_url=MONITOR_URL,
        server_id=f"codex:session:{SESSION_ID}",
        finished_at=42.5,
    )
    store = RemoteUnreadStore()
    store.retain_routes({row.host_name: row.monitor_url})
    store.replace_host(row.host_name, (row,), monitor_url=MONITOR_URL)
    token = store.optimistically_clear(row)
    assert token is not None

    # A successful empty snapshot is newer canonical evidence even though the
    # optimistic local state was already empty.
    assert not store.replace_host(row.host_name, (), monitor_url=MONITOR_URL)
    assert not store.restore(token)
    assert not store.has_unread()

    store.replace_host(row.host_name, (row,), monitor_url=MONITOR_URL)
    token = store.optimistically_clear(row)
    assert token is not None
    store.retain_routes({})
    assert not store.restore(token)
    assert not store.has_unread()


def test_canonical_unread_synthesizes_clickable_qualified_remote_status() -> None:
    row = CanonicalUnread(
        host_name="macmini",
        monitor_url=MONITOR_URL,
        server_id=f"codex:session:{SESSION_ID}",
        finished_at=42.5,
        provider="codex",
        name="SidePulse: canonical completion",
        cwd="sidepulse",
        deep_link="https://chatgpt.com/app/codex/remote/thread/example",
    )

    status = canonical_status_for_unread(row)

    assert status is not None
    assert status.agent_id == f"codex:session:remote:macmini:{SESSION_ID}"
    assert status.session_id == f"remote:macmini:{SESSION_ID}"
    assert status.display_name == row.name
    assert status.mode == AgentMode.COMPLETED
    assert status.cwd == row.cwd
    assert status.deep_link == row.deep_link
    assert session_open_target(status, SESSION_OPEN_APP) == (
        "url",
        f"codex://threads/{SESSION_ID}",
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="requires AppKit")
def test_exact_menu_generation_never_resolves_to_a_newer_completion() -> None:
    from sidepulse import status_bar

    host = RemoteHost("macmini", "mini", monitor_url=MONITOR_URL)
    older = CanonicalUnread(
        host.name,
        MONITOR_URL,
        f"codex:session:{SESSION_ID}",
        100.0,
    )
    newer = CanonicalUnread(
        host.name,
        MONITOR_URL,
        f"codex:session:{SESSION_ID}",
        150.0,
    )
    older_status = canonical_status_for_unread(older)
    assert older_status is not None
    store = RemoteUnreadStore()
    store.retain_routes({host.name: MONITOR_URL})
    store.replace_host(host.name, (older, newer), monitor_url=MONITOR_URL)
    target = SimpleNamespace(
        remote_monitor_hosts=(host,),
        remote_unread_store=store,
    )

    assert status_bar.StatusBarController.canonical_unread_for_status(
        target,
        older_status,
        exact_generation=True,
    ) == older
    assert status_bar.StatusBarController.canonical_unread_for_status(
        target,
        older_status,
    ) == newer


@pytest.mark.skipif(sys.platform != "darwin", reason="requires AppKit")
def test_canonical_completion_survives_missing_ssh_row_and_replaces_old_collision() -> None:
    from sidepulse import status_bar
    from sidepulse.settings import AgentMonitorSettings

    canonical = canonical_status_for_unread(
        CanonicalUnread(
            "macmini",
            MONITOR_URL,
            f"codex:session:{SESSION_ID}",
            100.0,
            name="Canonical latest state",
        )
    )
    assert canonical is not None
    canonical_newest = canonical_status_for_unread(
        CanonicalUnread(
            "macmini",
            MONITOR_URL,
            f"codex:session:{SESSION_ID}",
            150.0,
            name="Canonical newest generation",
        )
    )
    assert canonical_newest is not None
    now = datetime.fromtimestamp(200.0, timezone.utc)
    old_ssh = AgentStatus(
        provider="codex",
        agent_id=f"codex:session:remote:macmini:{SESSION_ID}",
        display_name="Old SSH state",
        mode=AgentMode.COMPLETED,
        updated_at=now,
        event_name="Stop",
        session_id=f"remote:macmini:{SESSION_ID}",
    )
    empty_snapshot = SimpleNamespace(
        statuses=(),
        stale_statuses=(),
        collected_at=now,
    )
    collision_snapshot = SimpleNamespace(
        statuses=(old_ssh,),
        stale_statuses=(),
        collected_at=now,
    )
    settings = AgentMonitorSettings()

    without_ssh = status_bar.recent_statuses(
        empty_snapshot,
        settings,
        canonical_statuses=(canonical, canonical_newest),
    )
    collision = status_bar.recent_statuses(
        collision_snapshot,
        settings,
        canonical_statuses=(canonical, canonical_newest),
    )

    assert without_ssh == [canonical_newest]
    assert collision == [canonical_newest]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires AppKit")
def test_local_unread_fallback_remains_until_first_successful_canonical_fetch() -> None:
    from sidepulse import status_bar
    from sidepulse.settings import AgentMonitorSettings

    host = RemoteHost("macmini", "mini", monitor_url=MONITOR_URL)
    store = RemoteUnreadStore()
    store.retain_routes({host.name: MONITOR_URL})
    target = SimpleNamespace(
        settings=AgentMonitorSettings().with_show_finished(True),
        finished_tracking_initialized=False,
        observed_agent_modes={},
        unread_finished_agent_ids=set(),
        remote_monitor_hosts=(host,),
        remote_unread_store=store,
        last_snapshot=None,
    )
    working = remote_status(mode=AgentMode.WORKING)
    completed = remote_status()
    working_snapshot = SimpleNamespace(
        statuses=(working,),
        stale_statuses=(),
        collected_at=datetime.now(timezone.utc),
    )
    completed_snapshot = SimpleNamespace(
        statuses=(completed,),
        stale_statuses=(),
        collected_at=datetime.now(timezone.utc),
    )

    status_bar.StatusBarController.observe_finished_sessions(target, working_snapshot)
    target.last_snapshot = completed_snapshot
    status_bar.StatusBarController.observe_finished_sessions(target, completed_snapshot)
    assert target.unread_finished_agent_ids == {completed.agent_id}
    assert status_bar.StatusBarController.is_status_unread(target, completed)
    assert status_bar.StatusBarController.should_show_finished_on_leds(
        target,
        AgentMode.WORKING,
    )

    assert store.replace_host(host.name, (), monitor_url=MONITOR_URL)
    assert store.is_authoritative(host.name, MONITOR_URL)
    assert not store.replace_host(host.name, (), monitor_url=MONITOR_URL)
    assert not status_bar.StatusBarController.is_status_unread(target, completed)
    assert not status_bar.StatusBarController.should_show_finished_on_leds(
        target,
        AgentMode.WORKING,
    )


def test_post_seen_sends_exact_generation_and_accepts_idempotent_ack(monkeypatch) -> None:
    row = CanonicalUnread(
        host_name="macmini",
        monitor_url=MONITOR_URL,
        server_id=f"codex:session:{SESSION_ID}",
        finished_at=42.5,
    )
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true,"marked":false}'

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("sidepulse.remote_state.urlopen", fake_urlopen)

    assert post_seen(row)
    request = captured["request"]
    assert request.full_url == f"{MONITOR_URL}/seen"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "id": row.server_id,
        "finishedAt": row.finished_at,
    }


def test_post_seen_reports_transport_or_invalid_ack_failure(monkeypatch) -> None:
    row = CanonicalUnread("macmini", MONITOR_URL, "codex:session:x", 1.0)

    def fail_urlopen(_request, *, timeout):
        raise OSError("offline")

    monkeypatch.setattr("sidepulse.remote_state.urlopen", fail_urlopen)
    assert not post_seen(row)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires AppKit")
def test_native_menu_item_prefixes_unread_completed_session() -> None:
    from sidepulse import status_bar
    from sidepulse.settings import AgentMonitorSettings

    status = remote_status()
    item = status_bar.build_session_menu_item(
        status,
        datetime.now(timezone.utc),
        SimpleNamespace(
            settings=AgentMonitorSettings(),
            is_status_unread=lambda candidate: candidate == status,
        ),
    )

    assert item.title().startswith("NEW — ")
    assert item.title() == status_bar.native_session_menu_title(status, unread=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires AppKit")
def test_failed_seen_post_restores_generation_and_refetches() -> None:
    from sidepulse import status_bar

    row = CanonicalUnread("macmini", MONITOR_URL, "codex:session:x", 1.0)
    store = RemoteUnreadStore()
    store.retain_routes({row.host_name: row.monitor_url})
    store.replace_host(row.host_name, (row,))
    token = store.optimistically_clear(row)
    assert token is not None

    changes = []
    errors = []
    target = SimpleNamespace(
        remote_unread_store=store,
        remote_unread_network_lock=threading.Lock(),
        schedule_remote_unread_changed=lambda: changes.append(True),
        record_remote_unread_error=lambda host, error: errors.append((host, error)),
    )
    with (
        patch.object(status_bar, "post_seen", return_value=False),
        patch.object(
            status_bar,
            "fetch_unread_finished",
            side_effect=OSError("offline"),
        ) as fetch,
    ):
        status_bar.StatusBarController._post_seen_worker(target, token)

    assert store.has_unread()
    fetch.assert_called_once_with(row.host_name, row.monitor_url)
    assert changes == [True]
    assert errors == [(row.host_name, "offline")]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires AppKit")
def test_poll_and_seen_confirmation_share_one_serial_network_lane() -> None:
    from sidepulse import status_bar

    host = RemoteHost("macmini", "mini", monitor_url=MONITOR_URL)
    row = CanonicalUnread("macmini", MONITOR_URL, "codex:session:x", 1.0)
    store = RemoteUnreadStore()
    store.retain_routes({host.name: MONITOR_URL})
    store.replace_host(host.name, (row,), monitor_url=MONITOR_URL)
    token = store.optimistically_clear(row)
    assert token is not None

    poll_started = threading.Event()
    release_poll = threading.Event()
    post_started = threading.Event()
    fetch_count = 0

    def fetch(_host_name, _monitor_url):
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            poll_started.set()
            assert release_poll.wait(1.0)
            return (row,)
        return ()

    def seen(_row):
        post_started.set()
        return True

    target = SimpleNamespace(
        remote_unread_store=store,
        remote_unread_network_lock=threading.Lock(),
        remote_unread_poll_lock=threading.Lock(),
        remote_unread_poll_in_flight=True,
        schedule_remote_unread_changed=lambda: None,
        record_remote_unread_error=lambda _host, _error: None,
    )
    with (
        patch.object(status_bar, "fetch_unread_finished", side_effect=fetch),
        patch.object(status_bar, "post_seen", side_effect=seen),
    ):
        poll_thread = threading.Thread(
            target=status_bar.StatusBarController._poll_remote_unread_worker,
            args=(target, (host,)),
        )
        post_thread = threading.Thread(
            target=status_bar.StatusBarController._post_seen_worker,
            args=(target, token),
        )
        poll_thread.start()
        assert poll_started.wait(1.0)
        post_thread.start()
        assert not post_started.wait(0.05)
        release_poll.set()
        poll_thread.join(1.0)
        post_thread.join(1.0)

    assert not poll_thread.is_alive()
    assert not post_thread.is_alive()
    assert post_started.is_set()
    assert fetch_count == 2
    assert not store.has_unread()
