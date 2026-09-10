import json
from datetime import datetime, timedelta, timezone

import pytest

from sidepulse.collector import AgentMonitor, LiveAgentMonitor, SourceSpec
from sidepulse.models import HookEvent


TITLE_PROMPT = (
    "You are a helpful assistant. You will be presented with a user prompt, "
    "and your job is to provide a short title for a task that will be created "
    "from that prompt.\nGenerate a concise UI title of at most 36 characters.\n"
    "Do not answer the user or attempt the task.\n\n"
    "User prompt:\nRepair the marketing session."
)


def helper_events():
    now = datetime.now(timezone.utc) - timedelta(seconds=30)
    payloads = [
        ("SessionStart", {}),
        ("UserPromptSubmit", {"prompt": TITLE_PROMPT}),
        ("SidepulseSummary", {"summary": "Repair marketing session; working"}),
        ("AgentProgress", {}),
        ("Stop", {"last_assistant_message": "Repair marketing session"}),
        ("SessionEnd", {}),
    ]
    return [
        HookEvent(
            provider="codex", session_id="title-helper", cwd="/tmp/project",
            logged_at=now + timedelta(seconds=i), event_name=event,
            raw={"hook_event_name": event, "session_id": "title-helper",
                 "transcript_path": None, **extra},
        )
        for i, (event, extra) in enumerate(payloads)
    ]


def test_live_helper_stays_hidden_after_prompt_and_later_events():
    monitor = LiveAgentMonitor()
    events = helper_events()
    monitor.ingest_record(events[0])
    assert "codex:session:title-helper" in monitor.statuses_by_key
    for event in events[1:]:
        monitor.ingest_record(event)
        assert not monitor.statuses_by_key


@pytest.mark.parametrize("event_count", [2, 3, 6])
def test_replayed_helper_is_not_working_or_finished(tmp_path, event_count):
    path = tmp_path / "codex.jsonl"
    path.write_text("".join(json.dumps({
        "logged_at": event.logged_at.isoformat(), "event": event.raw,
    }) + "\n" for event in helper_events()[:event_count]))
    snapshot = AgentMonitor(sources=[SourceSpec("codex", path)]).snapshot()
    assert not snapshot.statuses
    assert not snapshot.stale_statuses
    assert snapshot.aggregate.active_count == 0


@pytest.mark.parametrize("provider,prompt,transcript", [
    ("codex", "Repair the marketing session.", None),
    ("codex", "Explain this title prompt: " + TITLE_PROMPT, None),
    ("codex", TITLE_PROMPT, "/tmp/real-session.jsonl"),
    ("claude", TITLE_PROMPT, None),
])
def test_real_sessions_remain_visible(provider, prompt, transcript):
    monitor = LiveAgentMonitor()
    monitor.ingest_record(HookEvent(
        provider=provider, session_id="real-session", cwd="/tmp/project",
        logged_at=datetime.now(timezone.utc), event_name="UserPromptSubmit",
        raw={"prompt": prompt, "transcript_path": transcript},
    ))
    assert monitor.snapshot().aggregate.active_count == 1

