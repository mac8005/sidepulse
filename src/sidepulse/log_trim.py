"""Keep the append-only JSONL logs bounded.

Hook logs are only ever tailed (the collector reads the newest
``HOOK_LOG_MAX_BYTES_PER_SOURCE``), so history past a generous window is dead
weight: on the mini they had grown to 2.2 GB, 100 MB a day of tool output.
Once a log passes ``LOG_MAX_BYTES`` its writer drops the oldest lines down to
``LOG_KEEP_BYTES``. The tail is copied to a sibling file and renamed into
place, so a reader sees either the old inode or the new one, never a torn
file; the daemon's tail cache re-anchors on the inode change.
"""

from __future__ import annotations

import fcntl
import os
import shutil
from pathlib import Path

LOG_MAX_BYTES = 96 * 1024 * 1024
LOG_KEEP_BYTES = 64 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


def trim_log_if_needed(
    path: Path,
    *,
    max_bytes: int | None = None,
    keep_bytes: int | None = None,
) -> bool:
    """Trim ``path`` to its newest ``keep_bytes`` once it exceeds ``max_bytes``.

    Returns True when the log was trimmed. Concurrent writers race for a lock
    file next to the log; the loser skips, the winner's result covers both.
    """
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    keep_bytes = LOG_KEEP_BYTES if keep_bytes is None else keep_bytes
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    try:
        lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return _trim_locked(path, max_bytes, keep_bytes)
    finally:
        os.close(lock_fd)  # closing the descriptor releases the flock


def _trim_locked(path: Path, max_bytes: int, keep_bytes: int) -> bool:
    temp = path.with_name(path.name + ".trim")
    try:
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            if size <= max_bytes:
                return False  # the previous lock holder already trimmed it
            start = max(0, size - keep_bytes)
            if start > 0:
                # Start on a whole line: skip the partial one unless the cut
                # happens to land right after a newline.
                source.seek(start - 1)
                if source.read(1) != b"\n":
                    source.readline()
            with temp.open("wb") as target:
                shutil.copyfileobj(source, target, _COPY_CHUNK_BYTES)
                target.flush()
                os.fsync(target.fileno())
        os.replace(temp, path)
        return True
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False
