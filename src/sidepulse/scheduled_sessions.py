from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from functools import lru_cache
from pathlib import Path


def scheduled_session_ids() -> frozenset[str]:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return _read_scheduled_session_ids(str(home), int(time.monotonic() // 5))


@lru_cache(maxsize=2)
def _read_scheduled_session_ids(home: str, interval: int) -> frozenset[str]:
    # Only source metadata is needed. Never read prompts or modify the app DB.
    paths = sorted(
        (path for path in Path(home).glob("state_*.sqlite")
         if path.stem.removeprefix("state_").isdigit()),
        key=lambda path: int(path.stem.removeprefix("state_")), reverse=True,
    )
    for path in paths:
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
                return frozenset(row[0] for row in db.execute(
                    "SELECT id FROM threads WHERE thread_source = 'automation'"
                ))
        except (sqlite3.Error, OSError, ValueError):
            continue
    # Missing/older metadata must not hide an ordinary session.
    return frozenset()


def is_scheduled_session(provider: str, session_id: str | None, reported: bool = False) -> bool:
    if provider != "codex" or not isinstance(session_id, str) or not session_id:
        return False
    if reported:
        return True
    if session_id.startswith("remote:"):
        return False
    return session_id in scheduled_session_ids()


def hide_scheduled_session(
    provider: str, session_id: str | None, mode: str, reported: bool = False,
) -> bool:
    return mode not in {"waiting_for_input", "blocked_error", "unknown"} and is_scheduled_session(
        provider, session_id, reported
    )
