from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.request import Request, urlopen

from .models import AgentMode, AgentStatus, provider_label
from .remote_hosts import RemoteHost
from .session_actions import external_session_id, remote_session_parts


REMOTE_STATE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class CanonicalUnread:
    host_name: str
    monitor_url: str
    server_id: str
    finished_at: float
    provider: str = ""
    name: str = ""
    cwd: str | None = None
    deep_link: str | None = None

    @property
    def key(self) -> tuple[str, str, float]:
        return (self.host_name, self.server_id, self.finished_at)


@dataclass(frozen=True)
class OptimisticSeen:
    row: CanonicalUnread
    epoch: int


class RemoteUnreadStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, float], CanonicalUnread] = {}
        self._epochs: dict[str, int] = {}
        self._routes: dict[str, str] = {}
        self._authoritative_routes: dict[str, str] = {}
        self._routes_initialized = False
        self._lock = threading.Lock()

    def replace_host(
        self,
        host_name: str,
        rows: Iterable[CanonicalUnread],
        *,
        monitor_url: str | None = None,
    ) -> bool:
        with self._lock:
            expected_url = self._routes.get(host_name)
            if self._routes_initialized:
                if expected_url is None:
                    return False
                if monitor_url is not None and monitor_url != expected_url:
                    return False
                accepted_url = expected_url
            else:
                accepted_url = monitor_url
            replacement = {
                row.key: row
                for row in rows
                if row.host_name == host_name
                and (
                    accepted_url is None
                    or row.monitor_url == accepted_url
                )
            }
            current = {
                key: row
                for key, row in self._rows.items()
                if key[0] == host_name
            }
            changed = current != replacement
            authority_changed = bool(
                accepted_url
                and self._authoritative_routes.get(host_name) != accepted_url
            )
            if changed:
                for key in current:
                    self._rows.pop(key, None)
                self._rows.update(replacement)
            # Even an identical successful fetch is newer canonical evidence
            # and must invalidate an older optimistic-clear rollback token.
            self._epochs[host_name] = self._epochs.get(host_name, 0) + 1
            if accepted_url:
                self._authoritative_routes[host_name] = accepted_url
            return changed or authority_changed

    def retain_routes(self, routes: Mapping[str, str]) -> bool:
        with self._lock:
            normalized_routes = dict(routes)
            changed_hosts = {
                host_name
                for host_name in self._routes.keys() | normalized_routes.keys()
                if self._routes.get(host_name) != normalized_routes.get(host_name)
            }
            removed_keys = {
                key
                for key, row in self._rows.items()
                if row.host_name in changed_hosts
            }
            self._rows = {
                key: row
                for key, row in self._rows.items()
                if key not in removed_keys
            }
            self._routes = normalized_routes
            self._routes_initialized = True
            for host_name in changed_hosts:
                self._epochs[host_name] = self._epochs.get(host_name, 0) + 1
                self._authoritative_routes.pop(host_name, None)
            return bool(removed_keys or changed_hosts)

    def is_authoritative(self, host_name: str, monitor_url: str) -> bool:
        with self._lock:
            return self._authoritative_routes.get(host_name) == monitor_url

    def match_status(
        self,
        host_name: str,
        status: AgentStatus,
        *,
        monitor_url: str | None = None,
        finished_at: float | None = None,
    ) -> CanonicalUnread | None:
        candidates = set(canonical_server_ids(status))
        with self._lock:
            matches = [
                row
                for row in self._rows.values()
                if row.host_name == host_name
                and row.server_id in candidates
                and (monitor_url is None or row.monitor_url == monitor_url)
                and (finished_at is None or row.finished_at == finished_at)
            ]
        return max(matches, key=lambda row: row.finished_at, default=None)

    def optimistically_clear(self, row: CanonicalUnread) -> OptimisticSeen | None:
        with self._lock:
            current = self._rows.get(row.key)
            if current != row:
                return None
            self._rows.pop(row.key)
            return OptimisticSeen(
                row=current,
                epoch=self._epochs.get(row.host_name, 0),
            )

    def restore(self, token: OptimisticSeen) -> bool:
        row = token.row
        with self._lock:
            if self._epochs.get(row.host_name, 0) != token.epoch:
                return False
            if (
                self._routes_initialized
                and self._routes.get(row.host_name) != row.monitor_url
            ):
                return False
            if row.key in self._rows:
                return False
            self._rows[row.key] = row
            return True

    def has_unread(self) -> bool:
        with self._lock:
            return bool(self._rows)

    def rows(self) -> tuple[CanonicalUnread, ...]:
        with self._lock:
            return tuple(self._rows.values())


def monitor_route_for_status(
    status: AgentStatus,
    hosts: Iterable[RemoteHost],
) -> RemoteHost | None:
    remote = remote_session_parts(status.session_id)
    if remote is None:
        return None
    host_name, _session_id = remote
    return next(
        (
            host
            for host in hosts
            if host.name == host_name and host.monitor_url
        ),
        None,
    )


def canonical_server_ids(status: AgentStatus) -> tuple[str, ...]:
    candidates: list[str] = []
    if status.agent_id:
        candidates.append(status.agent_id)
    session_id = external_session_id(status)
    if session_id:
        canonical = f"{status.provider.lower()}:session:{session_id}"
        if canonical not in candidates:
            candidates.append(canonical)
    return tuple(candidates)


def canonical_status_for_unread(row: CanonicalUnread) -> AgentStatus | None:
    id_provider, separator, server_session_id = row.server_id.partition(":session:")
    if not separator or not id_provider or not server_session_id:
        return None
    remote = remote_session_parts(server_session_id)
    external_id = remote[1] if remote is not None else server_session_id
    qualified_session_id = f"remote:{row.host_name}:{external_id}"
    provider = (row.provider or id_provider).lower()
    try:
        updated_at = datetime.fromtimestamp(row.finished_at, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return AgentStatus(
        provider=provider,
        agent_id=f"{provider}:session:{qualified_session_id}",
        display_name=(
            row.name.strip()
            or f"{provider_label(provider)} session {external_id[:8]}"
        ),
        mode=AgentMode.COMPLETED,
        updated_at=updated_at,
        event_name="CanonicalFinished",
        session_id=qualified_session_id,
        cwd=row.cwd,
        origin=f"{provider_label(provider)} on {row.host_name}",
        deep_link=row.deep_link,
        stale=True,
    )


def parse_unread_finished(
    host_name: str,
    monitor_url: str,
    payload: Any,
) -> tuple[CanonicalUnread, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("agents"), list):
        raise ValueError("Snapshot must contain an agents list.")

    rows: dict[tuple[str, str, float], CanonicalUnread] = {}
    for item in payload["agents"]:
        if (
            not isinstance(item, dict)
            or item.get("mode") != "completed"
            or item.get("unread") is not True
        ):
            continue
        server_id = item.get("id")
        finished_at = item.get("finishedAt")
        if (
            not isinstance(server_id, str)
            or not server_id
            or isinstance(finished_at, bool)
            or not isinstance(finished_at, (int, float))
        ):
            continue
        finished_at = float(finished_at)
        if not math.isfinite(finished_at):
            continue
        row = CanonicalUnread(
            host_name=host_name,
            monitor_url=monitor_url,
            server_id=server_id,
            finished_at=finished_at,
            provider=(
                item.get("provider").strip().lower()
                if isinstance(item.get("provider"), str)
                else server_id.partition(":")[0].lower()
            ),
            name=(
                item.get("name").strip()
                if isinstance(item.get("name"), str)
                else ""
            ),
            cwd=item.get("cwd") if isinstance(item.get("cwd"), str) else None,
            deep_link=(
                item.get("deepLink")
                if isinstance(item.get("deepLink"), str)
                else None
            ),
        )
        rows[row.key] = row
    return tuple(rows.values())


def fetch_unread_finished(
    host_name: str,
    monitor_url: str,
    *,
    timeout: float = REMOTE_STATE_TIMEOUT_SECONDS,
) -> tuple[CanonicalUnread, ...]:
    request = Request(
        f"{monitor_url}/snapshot",
        headers={"Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return parse_unread_finished(host_name, monitor_url, payload)


def post_seen(
    row: CanonicalUnread,
    *,
    timeout: float = REMOTE_STATE_TIMEOUT_SECONDS,
) -> bool:
    data = json.dumps(
        {"id": row.server_id, "finishedAt": row.finished_at},
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"{row.monitor_url}/seen",
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if not 200 <= int(status) < 300:
                return False
            result = json.loads(response.read())
    except (OSError, TypeError, ValueError):
        return False
    return isinstance(result, dict) and result.get("ok") is True
