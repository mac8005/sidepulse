from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

from sidepulse.collector import StatusMetadata, status_from_event
from sidepulse.live_activity import DeepLinkResolver
from sidepulse.models import AgentMode, AgentStatus
from sidepulse.origin import background_session_source
from sidepulse.paseo_monitor import (
    PaseoMonitor,
    agent_signature,
    hook_line_for_agent,
    paseo_agent_link,
    paseo_server_id,
)
from sidepulse.providers import parse_log_line
from sidepulse.remote_hosts import RemoteHost, _emit_envelope, qualify_remote_line
from sidepulse.session_actions import (
    SESSION_OPEN_APP,
    session_deep_link,
    session_open_action_label,
)

SERVER_ID = "srv_C27ik7se_OUO"
AGENT_ID = "50c4f41b-8460-4969-a790-c0875e88ccc5"


def agent(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": AGENT_ID,
        "provider": "codex",
        "cwd": "/Users/massimo/Git/demo",
        "model": "gpt-5.4",
        "status": "idle",
        "title": "Fix the flaky test",
        "pendingPermissions": [],
        "attentionReason": None,
        "archivedAt": None,
    }
    base.update(overrides)
    return base


def test_agent_link_matches_app_route_encoding() -> None:
    assert paseo_agent_link(SERVER_ID, AGENT_ID) == f"paseo://h/{SERVER_ID}/agent/{AGENT_ID}"
    assert paseo_agent_link("srv/1", "a b") == "paseo://h/srv%2F1/agent/a%20b"
    assert paseo_agent_link(None, AGENT_ID) is None
    assert paseo_agent_link(SERVER_ID, None) is None


def test_server_id_is_read_from_paseo_home(tmp_path: Path) -> None:
    assert paseo_server_id(tmp_path) is None
    (tmp_path / "server-id").write_text(f"{SERVER_ID}\n")
    assert paseo_server_id(tmp_path) == SERVER_ID


def test_snapshot_status_maps_to_sidepulse_modes() -> None:
    cases = [
        (agent(status="initializing"), "UserPromptSubmit", "working"),
        (agent(status="running"), "UserPromptSubmit", "working"),
        (agent(status="idle"), "SessionStart", "idle_ready"),
        (agent(status="idle", attentionReason="finished"), "Stop", "completed"),
        (agent(status="error", lastError="boom"), "PostToolUseFailure", "blocked_error"),
        (agent(status="closed"), "SessionEnd", "completed"),
        (agent(archivedAt="2026-09-04T08:00:00Z"), "SessionEnd", "completed"),
        (
            agent(
                status="running",
                pendingPermissions=[{"id": "p1", "name": "Bash", "kind": "tool", "title": "Run rm -rf build"}],
            ),
            "PermissionRequest",
            "waiting_for_input",
        ),
    ]
    for snapshot, event_name, mode in cases:
        line = hook_line_for_agent(snapshot, SERVER_ID)
        assert line is not None, snapshot
        assert line["hook_event_name"] == event_name
        assert line["sidepulse_mode"] == mode
        assert line["session_id"] == AGENT_ID
        assert line["sidepulse_deep_link"] == f"paseo://h/{SERVER_ID}/agent/{AGENT_ID}"
        assert line["agent_origin"] == "Paseo codex"
        assert line["prompt"] == "Fix the flaky test"


def test_permission_line_names_the_tool_without_its_input() -> None:
    line = hook_line_for_agent(
        agent(
            status="running",
            pendingPermissions=[
                {"id": "p1", "name": "Bash", "kind": "tool", "title": "Run tests", "description": "pytest -x"}
            ],
        ),
        SERVER_ID,
    )
    assert line is not None
    assert line["tool_name"] == "Bash"
    assert line["message"] == "Run tests"
    assert "tool_input" not in line


def test_snapshot_without_server_id_carries_no_link() -> None:
    line = hook_line_for_agent(agent(), None)
    assert line is not None
    assert "sidepulse_deep_link" not in line
    assert hook_line_for_agent({"status": "idle"}, SERVER_ID) is None


def test_line_flows_through_parser_and_status() -> None:
    line = hook_line_for_agent(agent(status="running"), SERVER_ID)
    assert line is not None
    record = parse_log_line("paseo", json.dumps(line))
    assert record is not None
    status = status_from_event(record, StatusMetadata(cwd=record.cwd, title="Fix the flaky test"))
    assert status is not None
    assert status.provider == "paseo"
    assert status.session_id == AGENT_ID
    assert status.mode.value == "working"
    assert status.deep_link == f"paseo://h/{SERVER_ID}/agent/{AGENT_ID}"


def test_monitor_emits_only_on_signature_change() -> None:
    emitted: list[tuple[str, dict[str, object]]] = []
    monitor = PaseoMonitor(server_id=SERVER_ID, emit=lambda p, l: emitted.append((p, l)), log=lambda _: None)

    def session(message: dict[str, object]) -> str:
        return json.dumps({"type": "session", "message": message})

    monitor.handle_message(
        session(
            {
                "type": "fetch_agents_response",
                "payload": {"requestId": "r1", "entries": [{"agent": agent(status="running")}]},
            }
        )
    )
    monitor.handle_message(
        session({"type": "agent_update", "payload": {"kind": "upsert", "agent": agent(status="running", updatedAt="x")}})
    )
    monitor.handle_message(
        session({"type": "agent_update", "payload": {"kind": "upsert", "agent": agent(status="idle", attentionReason="finished")}})
    )
    monitor.handle_message(session({"type": "agent_update", "payload": {"kind": "remove", "agentId": AGENT_ID}}))
    monitor.handle_message(session({"type": "agent_update", "payload": {"kind": "remove", "agentId": AGENT_ID}}))
    # Noise that must not raise or emit.
    monitor.handle_message('{"type":"pong"}')
    monitor.handle_message("not json")
    monitor.handle_message(session({"type": "agent_stream", "payload": {"agentId": AGENT_ID, "event": {}}}))

    assert [(p, l["hook_event_name"]) for p, l in emitted] == [
        ("paseo", "UserPromptSubmit"),
        ("paseo", "Stop"),
        ("paseo", "SessionEnd"),
    ]
    assert emitted[2][1]["sidepulse_deep_link"] == f"paseo://h/{SERVER_ID}/agent/{AGENT_ID}"


def test_monitor_closes_agents_missing_from_a_fresh_directory() -> None:
    emitted: list[dict[str, object]] = []
    monitor = PaseoMonitor(server_id=SERVER_ID, emit=lambda _p, l: emitted.append(l), log=lambda _: None)
    directory = {"type": "fetch_agents_response", "payload": {"requestId": "r", "entries": [{"agent": agent()}]}}
    monitor.handle_message(json.dumps({"type": "session", "message": directory}))
    monitor.handle_message(
        json.dumps({"type": "session", "message": {"type": "fetch_agents_response", "payload": {"requestId": "r2", "entries": []}}})
    )
    assert [l["hook_event_name"] for l in emitted] == ["SessionStart", "SessionEnd"]
    assert monitor.signatures == {}


def test_monitor_learns_server_id_from_handshake() -> None:
    monitor = PaseoMonitor(emit=lambda *_: None, log=lambda _: None)
    monitor.handle_message(
        json.dumps(
            {
                "type": "session",
                "message": {"type": "status", "payload": {"status": "server_info", "serverId": SERVER_ID}},
            }
        )
    )
    assert monitor.server_id == SERVER_ID


def test_signature_ignores_stream_noise() -> None:
    assert agent_signature(agent(updatedAt="1")) == agent_signature(agent(updatedAt="2"))
    assert agent_signature(agent(status="idle")) != agent_signature(agent(status="running"))


def test_resolver_builds_paseo_links_from_server_id(monkeypatch) -> None:
    monkeypatch.setattr("sidepulse.live_activity.paseo_server_id", lambda: SERVER_ID)
    resolver = DeepLinkResolver()
    assert resolver.link_for("paseo", AGENT_ID) == f"paseo://h/{SERVER_ID}/agent/{AGENT_ID}"
    assert resolver.link_for("paseo", f"remote:mini:{AGENT_ID}") is None


def test_emit_envelope_carries_paseo_link_to_remote_status(monkeypatch) -> None:
    monkeypatch.setattr("sidepulse.live_activity.paseo_server_id", lambda: SERVER_ID)
    monkeypatch.setattr("sidepulse.remote_hosts._REMOTE_DEEP_LINKS", None)
    output = io.StringIO()
    line = hook_line_for_agent(agent(status="running"), None)
    assert line is not None

    _emit_envelope("paseo", json.dumps(line), output)

    envelope = json.loads(output.getvalue())
    qualified = qualify_remote_line("paseo", envelope["line"], "macmini")
    record = parse_log_line("paseo", json.dumps(qualified))
    assert record is not None
    status = status_from_event(record, StatusMetadata())
    assert status is not None
    assert status.session_id == f"remote:macmini:{AGENT_ID}"
    assert status.deep_link == f"paseo://h/{SERVER_ID}/agent/{AGENT_ID}"


def test_remote_host_accepts_paseo_provider() -> None:
    assert RemoteHost("mini", "mini", ("codex", "claude", "paseo")).providers == ("codex", "claude", "paseo")


def paseo_status(session_id: str, deep_link: str | None = None) -> AgentStatus:
    return AgentStatus(
        provider="paseo",
        agent_id=f"paseo:{session_id}",
        display_name="Paseo",
        mode=AgentMode.WORKING,
        updated_at=datetime.now(timezone.utc),
        event_name="UserPromptSubmit",
        session_id=session_id,
        deep_link=deep_link,
    )


def test_session_actions_open_paseo_agent_in_app(monkeypatch) -> None:
    link = f"paseo://h/{SERVER_ID}/agent/{AGENT_ID}"
    remote = paseo_status(f"remote:macmini:{AGENT_ID}", link)
    assert session_deep_link(remote) == link
    assert session_open_action_label(remote, SESSION_OPEN_APP) == "Open in Paseo"
    assert session_deep_link(paseo_status(f"remote:macmini:{AGENT_ID}")) == "paseo://"

    monkeypatch.setattr("sidepulse.session_actions.paseo_server_id", lambda: SERVER_ID)
    assert session_deep_link(paseo_status(AGENT_ID)) == link


def test_hosted_agent_hooks_are_background_sessions() -> None:
    assert background_session_source({"PASEO_AGENT_ID": AGENT_ID}) == "env:PASEO_AGENT_ID"
    assert background_session_source({}) is None
