"""The portal and the cloud worker must share one diary, one caller memory,
and one live desk feed - none of it on a laptop's disk.

Before this, each process held its own copy of ``.data/mock_diary.json`` (the
portal said ``booking_not_found`` for a booking Ava had just made), the caller
store on LiveKit Cloud vanished with every restart (so "remember me forever"
lasted one call), and the desk stream only worked for a browser connected to the
same process the worker happened to POST to.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from booking import MemoryBookingProvider
from caller_store import CallerStore
from practice import PracticeClient, seed_mock_diary
from state_store import (
    CALLER_STORE_KEY,
    DIARY_KEY,
    MemoryStateStore,
    StateConflictError,
    state_store_from_env,
    storage_mode,
)

REPO = Path(__file__).resolve().parents[1]


# --- the store contract ----------------------------------------------------


async def test_memory_store_versions_every_save() -> None:
    store = MemoryStateStore()
    empty = await store.load("diary")
    assert empty.payload is None and empty.version == 0

    assert await store.save("diary", {"a": 1}) == 1
    assert await store.save("diary", {"a": 2}) == 2
    snap = await store.load("diary")
    assert snap.payload == {"a": 2} and snap.version == 2

    with pytest.raises(StateConflictError):
        await store.save("diary", {"a": 3}, expected_version=1)
    assert await store.save("diary", {"a": 3}, expected_version=2) == 3


async def test_desk_events_are_append_only_with_ids() -> None:
    store = MemoryStateStore()
    assert await store.latest_desk_event_id() == 0
    first = await store.append_desk_event({"type": "transcript", "text": "hi"})
    second = await store.append_desk_event({"type": "activity", "action": "book"})
    assert (first, second) == (1, 2)
    rows = await store.desk_events_after(first)
    assert [row_id for row_id, _ in rows] == [second]
    assert rows[0][1]["action"] == "book"


def test_store_only_when_database_url_is_set(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert state_store_from_env() is None
    assert storage_mode() == "file"
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example/postgres")
    store = state_store_from_env()
    assert store is not None and storage_mode() == "postgres"
    assert state_store_from_env() is store  # one pool per process


# --- the diary --------------------------------------------------------------


async def test_two_diary_clients_share_one_store() -> None:
    """Ava books on the worker; the desk (another process) sees it at once."""
    store = MemoryStateStore()
    worker = PracticeClient(mode="mock", store=store)
    seed_mock_diary(worker, today=date(2026, 9, 14), days=3)
    await worker.persist()

    desk = PracticeClient(mode="mock", store=store)  # empty until it refreshes
    diary = await desk.list_diary(branch_id="shellharbour")
    open_slot = next(slot for slot in diary["slots"] if not slot["taken"])

    booked = await worker.book_appointment(
        branch_id="shellharbour",
        slot_id=open_slot["slot_id"],
        reason="check up",
        name="Robert Walker",
        phone="0474470332",
    )
    assert booked["confirmed"] is True

    again = await desk.list_diary(branch_id="shellharbour")
    taken = next(s for s in again["slots"] if s["slot_id"] == open_slot["slot_id"])
    assert taken["taken"] is True
    assert any(b["booking_id"] == booked["booking_id"] for b in again["bookings"])

    cancelled = await desk.cancel_appointment(booking_id=booked["booking_id"])
    assert cancelled["confirmed"] is True
    fresh = await worker.list_diary(branch_id="shellharbour")
    assert not any(b["booking_id"] == booked["booking_id"] for b in fresh["bookings"])


async def test_provider_lookup_and_cancel_see_other_process_writes() -> None:
    store = MemoryStateStore()
    desk = PracticeClient(mode="mock", store=store)
    seed_mock_diary(desk, today=date(2026, 9, 14), days=3)
    await desk.persist()
    diary = await desk.list_diary(branch_id="shellharbour")
    slot = next(s for s in diary["slots"] if not s["taken"])
    booked = await desk.book_appointment(
        branch_id="shellharbour",
        slot_id=slot["slot_id"],
        reason="clean",
        name="Jamie Cole",
        phone="0412222333",
    )

    worker = MemoryBookingProvider(PracticeClient(mode="mock", store=store))
    found = await worker.lookup_patient(mobile="0412222333")
    assert found["is_existing_patient"] is True
    assert any(b["booking_id"] == booked["booking_id"] for b in found["bookings"])

    result = await worker.cancel_appointment(booking_id=booked["booking_id"])
    assert result["ok"] is True and "fee_applies" in result


async def test_empty_store_is_seeded_once_and_local_file_migrates(tmp_path) -> None:
    """First boot against an empty database: whatever the laptop had wins."""
    path = tmp_path / "mock_diary.json"
    local = PracticeClient(mode="mock", persist_path=path)
    seed_mock_diary(local, today=date(2026, 9, 14), days=2)
    local.seed_patient(patient_id="pat_x", name="Migrated Person", phone="0400000001")
    local.save()

    store = MemoryStateStore()
    migrated = PracticeClient(mode="mock", store=store)
    migrated.load_file(path)
    await migrated.refresh()
    snap = await store.load(DIARY_KEY)
    assert snap.exists and "pat_x" in snap.payload["patients"]

    other = PracticeClient(mode="mock", store=store)
    await other.refresh()
    assert other.patients["pat_x"].name == "Migrated Person"
    assert other.store_version == snap.version


async def test_sync_mobile_correction_reaches_the_store() -> None:
    store = MemoryStateStore()
    client = PracticeClient(mode="mock", store=store)
    client.seed_patient(patient_id="pat_1", name="Robert", phone="0470470332")
    await client.persist()

    assert client.update_patient_mobile(mobile="0474470332", patient_id="pat_1")
    await client.flush()
    snap = await store.load(DIARY_KEY)
    assert snap.payload["patients"]["pat_1"]["phone"] == "0474470332"


# --- caller memory ----------------------------------------------------------


async def test_caller_memory_survives_a_new_process() -> None:
    store = MemoryStateStore()
    first_call = CallerStore(store=store)
    first_call.touch("+61474470332", name="Robert", date_of_birth="1980-08-01")
    await first_call.flush()

    next_call = CallerStore(store=store)
    await next_call.refresh()
    record = next_call.lookup("+61474470332")
    assert record is not None
    assert record.name == "Robert" and record.date_of_birth == "1980-08-01"

    snap = await store.load(CALLER_STORE_KEY)
    assert snap.exists and snap.payload["records"][0]["e164"] == "+61474470332"


async def test_caller_file_migrates_into_empty_store(tmp_path) -> None:
    path = tmp_path / "callers.json"
    CallerStore(path=path).touch("+61400000002", name="Old Laptop Record")

    store = MemoryStateStore()
    migrating = CallerStore(store=store)
    migrating.load_file(path)
    await migrating.refresh()

    fresh = CallerStore(store=store)
    await fresh.refresh()
    assert fresh.lookup("+61400000002").name == "Old Laptop Record"


# --- the desk feed through the portal ---------------------------------------


def _portal(store: MemoryStateStore) -> TestClient:
    from portal import create_app

    practice = PracticeClient(mode="mock", store=store)
    app = create_app(practice=practice, require_auth=False, state_store=store)
    return TestClient(app)


def test_portal_persists_desk_events_and_replays_after_last_event_id(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DESK_STREAM_MAX_S", "0.6")
    monkeypatch.setenv("DESK_POLL_S", "0.02")
    store = MemoryStateStore()
    http = _portal(store)

    for text in ("first", "second", "third"):
        posted = http.post(
            "/api/desk/events",
            json={"type": "transcript", "role": "assistant", "text": text},
        )
        assert posted.status_code == 200

    with http.stream(
        "GET", "/api/desk/stream", headers={"Last-Event-ID": "1"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = [chunk for chunk in body.split("\n\n") if "data:" in chunk]
    ids = [
        line
        for chunk in events
        for line in chunk.splitlines()
        if line.startswith("id:")
    ]
    texts = [
        json.loads(line[5:])["text"]
        for chunk in events
        for line in chunk.splitlines()
        if line.startswith("data:") and "text" in line
    ]
    assert texts == ["second", "third"]
    assert ids == ["id: 2", "id: 3"]


def test_portal_health_names_the_storage(monkeypatch) -> None:
    store = MemoryStateStore()
    http = _portal(store)
    body = http.get("/api/health").json()
    assert body["storage"] == "shared"
    assert body["agent"]


# --- running without the agents SDK (Vercel) --------------------------------


def test_portal_imports_without_livekit_agents() -> None:
    """The cloud portal installs only fastapi + livekit-api + asyncpg."""
    code = (
        "import sys; sys.modules['livekit.agents'] = None; "
        "sys.modules['livekit.agents.llm'] = None; "
        "import portal; print(portal.app.title)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Ava desk" in proc.stdout


def test_vercel_entrypoint_exposes_the_portal_app() -> None:
    spec = importlib.util.spec_from_file_location("vercel_app", REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.app.title.startswith("Ava desk")

    config = json.loads((REPO / "vercel.json").read_text())
    assert config["regions"] == ["syd1"]
    function = config["functions"]["app.py"]
    assert function["maxDuration"] >= 120
    assert "tests/**" in function["excludeFiles"]


def test_portal_requirements_match_base_dependencies() -> None:
    """Vercel installs requirements-portal.txt; it must cover the base deps."""
    import tomllib

    base = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    wanted = {dep.split(">")[0].split("[")[0].strip().lower() for dep in base}
    lines = (REPO / "requirements-portal.txt").read_text().splitlines()
    listed = {
        line.split(">")[0].split("[")[0].strip().lower()
        for line in lines
        if line.strip() and not line.startswith("#")
    }
    assert wanted == listed


async def test_shared_diary_placeholders_are_renamed_and_saved() -> None:
    from practice import Slot

    store = MemoryStateStore()
    await store.save(
        DIARY_KEY,
        {
            "patients": {},
            "slots": {
                "s1": Slot(
                    slot_id="s1",
                    branch_id="woonona",
                    date="2026-09-21",
                    time="08:00",
                    clinician="available dentist",
                ).__dict__
            },
            "bookings": {},
            "messages": [],
        },
    )
    client = PracticeClient(mode="mock", store=store)
    await client.refresh()
    assert client.slots["s1"].clinician == "Dr Beena Kurian"
    snap = await store.load(DIARY_KEY)
    assert snap.payload["slots"]["s1"]["clinician"] == "Dr Beena Kurian"
    assert client.store_version == snap.version


# --- speed and resilience (17 Sep: a booking took 11s from Mumbai) -----------------


class CountingStore(MemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.loads = 0
        self.full_saves = 0
        self.merges: list[dict] = []
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise OSError("database unreachable")

    async def version(self, key):
        self._check()
        return await super().version(key)

    async def load(self, key):
        self._check()
        self.loads += 1
        return await super().load(key)

    async def save(self, key, payload, *, expected_version=None):
        self._check()
        self.full_saves += 1
        return await super().save(key, payload, expected_version=expected_version)

    async def merge_sections(self, key, sections, replace=None):
        self._check()
        self.merges.append({"sections": sections, "replace": replace or {}})
        return await super().merge_sections(key, sections, replace)


async def _seeded(store) -> PracticeClient:
    client = PracticeClient(mode="mock", store=store)
    seed_mock_diary(client, today=date(2026, 9, 14), days=3)
    await client.persist()
    return client


async def test_refresh_does_not_download_an_unchanged_diary() -> None:
    store = CountingStore()
    await _seeded(store)
    worker = PracticeClient(mode="mock", store=store)
    await worker.refresh()
    assert store.loads == 1
    for _ in range(5):
        await worker.list_diary(branch_id="dapto")
    assert store.loads == 1, "same version: only the version is checked"


async def test_a_booking_uploads_only_what_changed() -> None:
    store = CountingStore()
    client = await _seeded(store)
    saves_before = store.full_saves
    slot = next(s for s in client.slots.values() if not s.taken)
    booked = await client.book_appointment(
        branch_id=slot.branch_id,
        slot_id=slot.slot_id,
        reason="check up",
        name="Robert Walker",
        phone="0474470332",
    )
    assert booked["confirmed"] is True
    assert store.full_saves == saves_before
    (merge,) = store.merges
    assert set(merge["sections"]["bookings"]) == {booked["booking_id"]}
    assert set(merge["sections"]["slots"]) == {slot.slot_id}
    assert len(merge["sections"]["patients"]) == 1


async def test_two_writers_keep_each_others_bookings() -> None:
    store = CountingStore()
    desk = await _seeded(store)
    worker = PracticeClient(mode="mock", store=store)
    await worker.refresh()
    free = [s for s in desk.slots.values() if not s.taken][:2]

    a = await desk.book_appointment(
        branch_id=free[0].branch_id,
        slot_id=free[0].slot_id,
        reason="a",
        name="Desk Person",
        phone="0400000001",
    )
    # the worker has not refreshed since; its own booking must not wipe the desk's
    worker.store = None
    b_client = worker
    b = await b_client.book_appointment(
        branch_id=free[1].branch_id,
        slot_id=free[1].slot_id,
        reason="b",
        name="Phone Person",
        phone="0400000002",
    )
    b_client.store = store
    await b_client.persist()

    snap = await store.load(DIARY_KEY)
    assert a["booking_id"] in snap.payload["bookings"]
    assert b["booking_id"] in snap.payload["bookings"]
    assert snap.payload["slots"][free[0].slot_id]["taken"] is True
    assert snap.payload["slots"][free[1].slot_id]["taken"] is True


async def test_database_outage_does_not_fail_the_call_and_catches_up() -> None:
    store = CountingStore()
    client = await _seeded(store)
    store.down = True
    diary = await client.list_diary(branch_id="woonona")  # no exception
    slot = next(s for s in diary["slots"] if not s["taken"])
    booked = await client.book_appointment(
        branch_id="woonona",
        slot_id=slot["slot_id"],
        reason="pain",
        name="Robert",
        phone="0474470332",
    )
    assert booked["confirmed"] is True
    assert client.unsynced is True

    store.down = False
    await client.refresh()  # catches up: the booking is written, not overwritten
    snap = await store.load(DIARY_KEY)
    assert booked["booking_id"] in snap.payload["bookings"]
    assert client.unsynced is False


async def test_caller_memory_survives_an_outage() -> None:
    store = CountingStore()
    callers = CallerStore(store=store)
    store.down = True
    await callers.refresh()  # no exception
    callers.touch("+61474470332", name="Robert")
    await callers.flush()
    store.down = False
    await callers.refresh()
    snap = await store.load(CALLER_STORE_KEY)
    assert snap.exists and snap.payload["records"][0]["name"] == "Robert"
