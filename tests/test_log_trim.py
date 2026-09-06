from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from sidepulse import log_trim
from sidepulse.audit import append_status_audit_record
from sidepulse.collector import (
    SourceSpec,
    _tool_response_looks_failed,
    append_hook_log_cache,
    load_hook_log_cache,
)
from sidepulse.hook import compact_hook_payload, format_hook_payload, write_hook_line
from sidepulse.log_trim import trim_log_if_needed
from sidepulse.providers import parse_log_line


def _line(index: int) -> bytes:
    body = json.dumps(
        {"hook_event_name": "Stop", "session_id": "s", "logged_at": "2026-09-06T08:00:00Z", "seq": index}
    )
    return body.ljust(99).encode() + b"\n"  # exactly 100 bytes per line


def _write_lines(path: Path, count: int) -> None:
    with path.open("wb") as handle:
        for index in range(count):
            handle.write(_line(index))


def test_trim_keeps_the_newest_whole_lines(tmp_path: Path) -> None:
    log = tmp_path / "claude.jsonl"
    _write_lines(log, 200)  # 20 000 bytes

    assert trim_log_if_needed(log, max_bytes=10_000, keep_bytes=5_050) is True

    kept = log.read_bytes()
    lines = [line for line in kept.split(b"\n") if line]
    assert kept.endswith(b"\n")
    assert 0 < len(kept) <= 5_050
    # The cut fell inside a line, which is dropped; everything kept is whole.
    assert [json.loads(line)["seq"] for line in lines] == list(range(150, 200))
    # Within bounds now: nothing to do, and no leftovers.
    assert trim_log_if_needed(log, max_bytes=10_000, keep_bytes=5_050) is False
    assert not log.with_name("claude.jsonl.trim").exists()
    assert trim_log_if_needed(tmp_path / "missing.jsonl") is False


def test_trim_lands_cleanly_on_a_line_boundary(tmp_path: Path) -> None:
    log = tmp_path / "codex.jsonl"
    _write_lines(log, 200)
    assert trim_log_if_needed(log, max_bytes=10_000, keep_bytes=5_000) is True
    lines = [line for line in log.read_bytes().split(b"\n") if line]
    assert [json.loads(line)["seq"] for line in lines] == list(range(150, 200))


def test_trim_skips_while_another_writer_holds_the_lock(tmp_path: Path) -> None:
    log = tmp_path / "claude.jsonl"
    _write_lines(log, 200)
    lock = os.open(log.with_name("claude.jsonl.lock"), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert trim_log_if_needed(log, max_bytes=10_000, keep_bytes=5_000) is False
        assert log.stat().st_size == 20_000
    finally:
        os.close(lock)
    assert trim_log_if_needed(log, max_bytes=10_000, keep_bytes=5_000) is True


def test_write_hook_line_keeps_the_log_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(log_trim, "LOG_MAX_BYTES", 4_000)
    monkeypatch.setattr(log_trim, "LOG_KEEP_BYTES", 2_000)
    log = tmp_path / "claude.jsonl"
    for index in range(300):
        write_hook_line(log, {"hook_event_name": "Stop", "session_id": "s", "seq": index})

    assert log.stat().st_size <= 4_000
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["seq"] for line in lines] == list(range(lines[0]["seq"], 300))


def test_collector_tail_cache_reanchors_after_a_trim(tmp_path: Path) -> None:
    log = tmp_path / "claude.jsonl"
    _write_lines(log, 200)
    source = SourceSpec("claude", log)
    cache = load_hook_log_cache(source, max_lines=1000, max_bytes=1 << 20)
    assert cache is not None and cache.offset == 20_000

    assert trim_log_if_needed(log, max_bytes=10_000, keep_bytes=5_000) is True
    with log.open("ab") as handle:
        handle.write(_line(200))

    # Same path, new inode: the incremental read refuses and a reload starts over.
    assert append_hook_log_cache(cache, source, log.stat(), max_lines=1000, max_bytes=1 << 20) is False
    fresh = load_hook_log_cache(source, max_lines=1000, max_bytes=1 << 20)
    assert fresh is not None
    assert fresh.offset == log.stat().st_size == 5_100
    assert [record.raw["seq"] for record in fresh.records][-3:] == [198, 199, 200]


def test_compaction_keeps_head_and_tail_and_names_dropped_failure_markers() -> None:
    head = "stdout line\n" * 200
    middle = "x" * 5000 + "\nTraceback (most recent call last):\n" + "y" * 5000
    tail = "z" * 3000
    text = head + middle + tail

    compacted = compact_hook_payload({"tool_response": text, "prompt": "p" * 10_000})

    out = compacted["tool_response"]
    assert len(out) < 4_000
    assert out.startswith(text[:1024]) and out.endswith(text[-2048:])
    assert "chars trimmed: traceback]" in out
    assert _tool_response_looks_failed(out)
    assert compacted["prompt"] == "p" * 10_000  # other fields are not touched


def test_compaction_leaves_short_and_clean_output_alone() -> None:
    payload = {"tool_response": {"stdout": "ok", "interrupted": False}, "tool_input": {"command": "ls"}}
    assert compact_hook_payload(payload) is payload

    clean = compact_hook_payload({"tool_response": "a" * 20_000})["tool_response"]
    assert clean.count("chars trimmed]") == 1
    assert not _tool_response_looks_failed(clean)


def test_compaction_recurses_into_structured_tool_fields() -> None:
    payload = {
        "tool_response": {"stdout": "o" * 20_000, "stderr": "", "interrupted": True},
        "tool_input": {"content": ["c" * 20_000, "short"]},
    }
    compacted = compact_hook_payload(payload)

    assert compacted is not payload
    assert compacted["tool_response"]["interrupted"] is True
    assert compacted["tool_response"]["stderr"] == ""
    assert len(compacted["tool_response"]["stdout"]) < 4_000
    assert compacted["tool_input"]["content"][1] == "short"
    assert len(compacted["tool_input"]["content"][0]) < 4_000
    assert _tool_response_looks_failed(compacted["tool_response"])


def test_codex_lines_are_compacted_inside_the_event_wrapper() -> None:
    payload = json.dumps(
        {"hook_event_name": "PostToolUse", "session_id": "s", "tool_response": "r" * 30_000, "cwd": "/tmp"}
    )
    codex = format_hook_payload("codex", payload, logged_at="2026-09-06T08:00:00Z")
    assert len(codex["event"]["tool_response"]) < 4_000
    assert codex["event"]["hook_event_name"] == "PostToolUse"

    claude = format_hook_payload("claude", payload, logged_at="2026-09-06T08:00:00Z")
    assert len(claude["tool_response"]) < 4_000
    assert claude["logged_at"] == "2026-09-06T08:00:00Z"


def test_audit_log_is_trimmed_like_the_hook_logs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(log_trim, "LOG_MAX_BYTES", 6_000)
    monkeypatch.setattr(log_trim, "LOG_KEEP_BYTES", 3_000)
    record = parse_log_line("claude", _line(1).decode())
    assert record is not None
    audit = tmp_path / "event-status.jsonl"
    for _ in range(100):
        append_status_audit_record(record, None, path=audit)

    assert 0 < audit.stat().st_size <= 6_000
    assert all(json.loads(line) for line in audit.read_text().splitlines())
