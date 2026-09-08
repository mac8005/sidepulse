from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from functools import lru_cache
from pathlib import Path


def goal_states() -> dict[str, str]:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return _read_goal_states(str(home / "goals_1.sqlite"), int(time.monotonic()))


@lru_cache(maxsize=2)
def _read_goal_states(path: str, second: int) -> dict[str, str]:
    # Read only lifecycle metadata, never objectives or transcripts. A short
    # cache bounds database work when multiple LEDs and clients poll together.
    try:
        with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
            return dict(db.execute("SELECT thread_id, status FROM thread_goals"))
    except (sqlite3.Error, OSError, ValueError):
        return {}
