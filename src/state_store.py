"""One place for the state the portal and the worker share.

On a laptop the diary, the caller memory, and the live desk feed lived in
``.data/*.json`` and in the memory of the one process that owned them. In the
cloud the portal runs as short-lived functions (Vercel) and the worker runs in
another region (LiveKit Cloud), so nothing in memory or on disk survives or is
visible to the other side. Everything durable goes through a ``StateStore``:

* ``ava_state``        - whole-document rows (``diary``, ``caller_store``) with
                         a version counter, so a reader can tell "changed since
                         I last looked" from one cheap query.
* ``ava_desk_events``  - the live transcript / activity feed, append-only, so
                         the desk can poll "everything after id N".

``MemoryStateStore`` is the same contract in memory for tests and local runs.
``PostgresStateStore`` talks to Supabase through its connection pooler.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("state_store")

DIARY_KEY = "diary"
CALLER_STORE_KEY = "caller_store"
DEFAULT_EVENT_LIMIT = 200

SCHEMA_SQL = """
create table if not exists ava_state (
    key text primary key,
    payload jsonb not null,
    version bigint not null default 1,
    updated_at timestamptz not null default now()
);
create table if not exists ava_desk_events (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    packet jsonb not null
);
create index if not exists ava_desk_events_created_at_idx
    on ava_desk_events (created_at);
"""


_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


class StateConflictError(RuntimeError):
    """Someone else saved this document since we loaded it."""


@dataclass
class Snapshot:
    payload: dict[str, Any] | None
    version: int

    @property
    def exists(self) -> bool:
        return self.payload is not None


class StateStore(Protocol):
    async def version(self, key: str) -> int: ...

    async def load(self, key: str) -> Snapshot: ...

    async def merge_sections(
        self,
        key: str,
        sections: Mapping[str, Mapping[str, Any]],
        replace: Mapping[str, Any] | None = None,
    ) -> int: ...

    async def save(
        self,
        key: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> int: ...

    async def append_desk_event(self, packet: Mapping[str, Any]) -> int: ...

    async def desk_events_after(
        self, after_id: int, *, limit: int = DEFAULT_EVENT_LIMIT
    ) -> list[tuple[int, dict[str, Any]]]: ...

    async def latest_desk_event_id(self) -> int: ...


class MemoryStateStore:
    """In-process store with the same semantics as Postgres (for tests/local)."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[dict[str, Any], int]] = {}
        self._events: list[dict[str, Any]] = []

    async def load(self, key: str) -> Snapshot:
        row = self._rows.get(key)
        if row is None:
            return Snapshot(None, 0)
        return Snapshot(deepcopy(row[0]), row[1])

    async def save(
        self,
        key: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> int:
        current = self._rows.get(key, (None, 0))[1]
        if expected_version is not None and expected_version != current:
            raise StateConflictError(
                f"{key}: expected version {expected_version}, store has {current}"
            )
        version = current + 1
        self._rows[key] = (deepcopy(dict(payload)), version)
        return version

    async def version(self, key: str) -> int:
        return self._rows.get(key, (None, 0))[1]

    async def merge_sections(
        self,
        key: str,
        sections: Mapping[str, Mapping[str, Any]],
        replace: Mapping[str, Any] | None = None,
    ) -> int:
        payload, current = self._rows.get(key, ({}, 0))
        merged = deepcopy(payload)
        for name, entries in sections.items():
            merged.setdefault(name, {}).update(deepcopy(dict(entries)))
        for name, value in (replace or {}).items():
            merged[name] = deepcopy(value)
        self._rows[key] = (merged, current + 1)
        return current + 1

    async def append_desk_event(self, packet: Mapping[str, Any]) -> int:
        self._events.append(deepcopy(dict(packet)))
        return len(self._events)

    async def desk_events_after(
        self, after_id: int, *, limit: int = DEFAULT_EVENT_LIMIT
    ) -> list[tuple[int, dict[str, Any]]]:
        rows = [
            (index + 1, deepcopy(packet))
            for index, packet in enumerate(self._events)
            if index + 1 > after_id
        ]
        return rows[:limit]

    async def latest_desk_event_id(self) -> int:
        return len(self._events)


class PostgresStateStore:
    """Supabase Postgres via the transaction pooler (Supavisor, port 6543).

    A pool is created lazily per event loop: serverless hosts give every
    invocation a fresh loop, and asyncpg pools are bound to the loop that made
    them. ``statement_cache_size=0`` is required in transaction pooling mode.
    """

    def __init__(self, dsn: str, *, max_size: int = 4) -> None:
        self.dsn = dsn
        self.max_size = max_size
        self._pools: dict[int, Any] = {}

    async def _pool(self) -> Any:
        import asyncpg

        loop = asyncio.get_running_loop()
        pool = self._pools.get(id(loop))
        if pool is None or getattr(pool, "_closed", False):
            pool = await asyncpg.create_pool(
                self.dsn,
                min_size=0,
                max_size=self.max_size,
                statement_cache_size=0,
                ssl="require",
                timeout=15,
                command_timeout=20,
            )
            self._pools[id(loop)] = pool
        return pool

    async def close(self) -> None:
        for pool in list(self._pools.values()):
            with contextlib.suppress(Exception):
                await pool.close()
        self._pools.clear()

    async def ensure_schema(self) -> None:
        """Create the tables unless they exist (the app role may not own DDL)."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            present = await conn.fetchval(
                "select to_regclass('public.ava_state') is not null"
                " and to_regclass('public.ava_desk_events') is not null"
            )
            if not present:
                await conn.execute(SCHEMA_SQL)

    async def version(self, key: str) -> int:
        started = time.perf_counter()
        pool = await self._pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "select version from ava_state where key = $1", key
            )
        logger.debug("state version %s %.0fms", key, _ms(started))
        return int(value or 0)

    async def merge_sections(
        self,
        key: str,
        sections: Mapping[str, Mapping[str, Any]],
        replace: Mapping[str, Any] | None = None,
    ) -> int:
        """Write only changed entries: entry-level merge inside each section.

        Two writers touching different bookings both survive; the same entry
        is last-writer-wins.
        """
        started = time.perf_counter()
        names = [n for n in sections if _IDENT_RE.match(n)]
        replaced = [n for n in (replace or {}) if _IDENT_RE.match(n)]
        patch = {n: sections[n] for n in names}
        patch.update({n: (replace or {})[n] for n in replaced})
        merge_expr = "ava_state.payload"
        if names:
            pairs = ", ".join(
                f"'{n}', coalesce(ava_state.payload->'{n}', '{{}}'::jsonb) "
                f"|| coalesce(excluded.payload->'{n}', '{{}}'::jsonb)"
                for n in names
            )
            merge_expr += f" || jsonb_build_object({pairs})"
        if replaced:
            pairs = ", ".join(f"'{n}', excluded.payload->'{n}'" for n in replaced)
            merge_expr += f" || jsonb_build_object({pairs})"
        pool = await self._pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                f"""
                insert into ava_state (key, payload) values ($1, $2::jsonb)
                on conflict (key) do update
                    set payload = {merge_expr},
                        version = ava_state.version + 1,
                        updated_at = now()
                returning version
                """,
                key,
                json.dumps(patch, default=str),
            )
        logger.info(
            "state merge %s %s bytes %.0fms",
            key,
            len(json.dumps(patch, default=str)),
            _ms(started),
        )
        return int(value)

    async def load(self, key: str) -> Snapshot:
        started = time.perf_counter()
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "select payload::text as payload, version from ava_state where key = $1",
                key,
            )
        logger.info("state load %s %.0fms", key, _ms(started))
        if row is None:
            return Snapshot(None, 0)
        return Snapshot(json.loads(row["payload"]), int(row["version"]))

    async def save(
        self,
        key: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> int:
        body = json.dumps(dict(payload), default=str)
        pool = await self._pool()
        async with pool.acquire() as conn:
            if expected_version is None:
                row = await conn.fetchrow(
                    """
                    insert into ava_state (key, payload) values ($1, $2::jsonb)
                    on conflict (key) do update
                        set payload = excluded.payload,
                            version = ava_state.version + 1,
                            updated_at = now()
                    returning version
                    """,
                    key,
                    body,
                )
            elif expected_version == 0:
                row = await conn.fetchrow(
                    """
                    insert into ava_state (key, payload) values ($1, $2::jsonb)
                    on conflict (key) do nothing
                    returning version
                    """,
                    key,
                    body,
                )
            else:
                row = await conn.fetchrow(
                    """
                    update ava_state
                       set payload = $2::jsonb, version = version + 1, updated_at = now()
                     where key = $1 and version = $3
                    returning version
                    """,
                    key,
                    body,
                    expected_version,
                )
        if row is None:
            raise StateConflictError(f"{key}: expected version {expected_version}")
        return int(row["version"])

    async def append_desk_event(self, packet: Mapping[str, Any]) -> int:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "insert into ava_desk_events (packet) values ($1::jsonb) returning id",
                json.dumps(dict(packet), default=str),
            )
        return int(row["id"])

    async def desk_events_after(
        self, after_id: int, *, limit: int = DEFAULT_EVENT_LIMIT
    ) -> list[tuple[int, dict[str, Any]]]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select id, packet::text as packet from ava_desk_events
                 where id > $1 order by id limit $2
                """,
                int(after_id),
                int(limit),
            )
        return [(int(row["id"]), json.loads(row["packet"])) for row in rows]

    async def latest_desk_event_id(self) -> int:
        pool = await self._pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "select coalesce(max(id), 0) from ava_desk_events"
            )
        return int(value or 0)

    async def prune_desk_events(self, *, keep_hours: int = 72) -> int:
        pool = await self._pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "delete from ava_desk_events where created_at < now() - ($1 || ' hours')::interval",
                str(int(keep_hours)),
            )
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except ValueError:
            return 0


_SHARED: PostgresStateStore | None = None


def database_url(env: Mapping[str, str] | None = None) -> str:
    environ = env if env is not None else os.environ
    return str(environ.get("DATABASE_URL") or "").strip()


def state_store_from_env(env: Mapping[str, str] | None = None) -> StateStore | None:
    """A shared Postgres store when DATABASE_URL is set, otherwise None (files)."""
    global _SHARED
    dsn = database_url(env)
    if not dsn:
        return None
    if _SHARED is None or _SHARED.dsn != dsn:
        _SHARED = PostgresStateStore(dsn)
    return _SHARED


def reset_shared_state_store() -> None:
    global _SHARED
    _SHARED = None


def storage_mode(env: Mapping[str, str] | None = None) -> str:
    return "postgres" if database_url(env) else "file"
