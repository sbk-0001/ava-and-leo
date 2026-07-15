"""Tests for the Supabase persistence path.

These guard the fix for the worker killing job processes: the transcript flush
must be fully async (never the synchronous supabase client on the event loop),
a row must be opened at session START, and it must be finalized on shutdown.
No network — a fake async Supabase client records the queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

import agent

# ---- Fakes -----------------------------------------------------------------


class _FakeQuery:
    """Records one table operation; `execute()` is a coroutine (async client)."""

    def __init__(self, log: list, table: str, op: str, payload=None):
        self._log = log
        self._entry = {"table": table, "op": op, "payload": payload, "filters": {}}

    def insert(self, payload):
        self._entry["op"] = "insert"
        self._entry["payload"] = payload
        return self

    def update(self, payload):
        self._entry["op"] = "update"
        self._entry["payload"] = payload
        return self

    def eq(self, column, value):
        self._entry["filters"][column] = value
        return self

    async def execute(self):
        self._log.append(self._entry)
        # Mimic postgrest returning the inserted row with an id.
        return type("Result", (), {"data": [{"id": "call-123"}]})()


class _FakeSupabase:
    def __init__(self, log: list):
        self._log = log

    def table(self, name: str):
        return _FakeQuery(self._log, name, "select")


@dataclass
class _FakeItem:
    type: str
    role: str
    text_content: str


class _FakeHistory:
    def __init__(self, items):
        self.items = items


class _FakeSession:
    def __init__(self, items):
        self.history = _FakeHistory(items)


class _FakeJob:
    id = "job-1"


class _FakeParticipant:
    identity = "caller-7"


class _FakeRoom:
    name = "room-abc"

    def __init__(self):
        self.remote_participants = {"p": _FakeParticipant()}


class _FakeCtx:
    room = _FakeRoom()
    job = _FakeJob()


@pytest.fixture
def query_log(monkeypatch):
    log: list = []

    async def _fake_client():
        return _FakeSupabase(log)

    monkeypatch.setattr(agent, "_supabase_client", _fake_client)
    return log


# ---- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_call_start_opens_in_progress_row(query_log):
    started = datetime(2026, 7, 15, tzinfo=timezone.utc)
    call_id = await agent._insert_call_start(_FakeCtx(), "ava", started)

    assert call_id == "call-123"
    assert len(query_log) == 1
    entry = query_log[0]
    assert entry["table"] == "ava_calls"
    assert entry["op"] == "insert"
    assert entry["payload"]["status"] == "in_progress"
    assert entry["payload"]["agent_name"] == "ava"
    assert entry["payload"]["participant_identity"] == "caller-7"
    # started row must NOT carry an end time yet
    assert "ended_at" not in entry["payload"]


@pytest.mark.asyncio
async def test_finalize_updates_existing_row_and_writes_turns(query_log):
    started = datetime(2026, 7, 15, tzinfo=timezone.utc)
    session = _FakeSession(
        [
            _FakeItem("message", "user", "Hi there"),
            _FakeItem("message", "assistant", "Hello! How can I help?"),
            _FakeItem("function_call", "assistant", "ignored"),  # non-message skipped
            _FakeItem("message", "user", "   "),  # blank skipped
        ]
    )

    await agent._finalize_call(session, _FakeCtx(), "ava", started, call_id="call-123")

    ops = [(e["table"], e["op"]) for e in query_log]
    # Updates the opened row (not a second insert), then inserts turn rows.
    assert ("ava_calls", "update") in ops
    assert ("ava_transcript_turns", "insert") in ops
    assert not any(t == "ava_calls" and o == "insert" for t, o in ops)

    update = next(e for e in query_log if e["table"] == "ava_calls")
    assert update["filters"] == {"id": "call-123"}
    assert update["payload"]["status"] == "completed"
    assert "User: Hi there" in update["payload"]["full_transcript"]
    assert "Ava: Hello! How can I help?" in update["payload"]["full_transcript"]

    turns = next(e for e in query_log if e["table"] == "ava_transcript_turns")
    assert [r["role"] for r in turns["payload"]] == ["user", "agent"]


@pytest.mark.asyncio
async def test_finalize_inserts_fallback_row_when_start_missing(query_log):
    started = datetime(2026, 7, 15, tzinfo=timezone.utc)
    session = _FakeSession([_FakeItem("message", "user", "Only turn")])

    # call_id is None -> the start insert had failed; finalize must still persist.
    await agent._finalize_call(session, _FakeCtx(), "leo", started, call_id=None)

    calls = [e for e in query_log if e["table"] == "ava_calls"]
    assert len(calls) == 1
    assert calls[0]["op"] == "insert"
    assert calls[0]["payload"]["status"] == "completed"
    assert calls[0]["payload"]["ended_at"] is not None


@pytest.mark.asyncio
async def test_persistence_helpers_are_async(query_log):
    import inspect

    # The whole point of the fix: these run on the event loop via await,
    # never as blocking synchronous calls.
    assert inspect.iscoroutinefunction(agent._insert_call_start)
    assert inspect.iscoroutinefunction(agent._finalize_call)
    assert inspect.iscoroutinefunction(agent._supabase_client)
