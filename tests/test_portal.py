"""Clinic portal API uses the same mock PracticeClient as Leo."""

from datetime import date

from fastapi.testclient import TestClient

from portal import create_app
from practice import PracticeClient, seed_mock_diary


def _client() -> tuple[TestClient, PracticeClient]:
    practice = PracticeClient(mode="mock")
    seed_mock_diary(practice, today=date(2026, 9, 14), days=7)
    app = create_app(practice=practice, require_auth=False)
    return TestClient(app), practice


def test_portal_lists_branch_facts() -> None:
    client, _practice = _client()
    response = client.get("/api/branches/dapto")
    assert response.status_code == 200
    body = response.json()
    assert body["trading_name"] == "Dapto Dentists"
    assert "Mall Lane" in body["parking"]
    assert any("Beena Kurian" in name for name in body["dentists"])


def test_portal_diary_and_book_reschedule_cancel() -> None:
    http, practice = _client()
    diary = http.get("/api/diary", params={"branch_id": "shellharbour"}).json()
    open_slots = [slot for slot in diary["slots"] if not slot["taken"]]
    assert open_slots
    slot = open_slots[0]

    booked = http.post(
        "/api/bookings",
        json={
            "branch_id": "shellharbour",
            "slot_id": slot["slot_id"],
            "name": "Jamie Cole",
            "phone": "0412222333",
            "reason": "check up",
        },
    )
    assert booked.status_code == 200
    body = booked.json()
    assert body["confirmed"] is True
    booking_id = body["booking_id"]

    next_slot = next(
        item
        for item in http.get("/api/diary", params={"branch_id": "shellharbour"}).json()[
            "slots"
        ]
        if not item["taken"]
    )
    moved = http.post(
        f"/api/bookings/{booking_id}/reschedule",
        json={"new_slot_id": next_slot["slot_id"]},
    )
    assert moved.json()["confirmed"] is True

    cancelled = http.post(f"/api/bookings/{booking_id}/cancel")
    assert cancelled.json()["confirmed"] is True
    assert practice.bookings[booking_id].cancelled is True


def test_portal_token_without_livekit_keys_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    http, _practice = _client()
    response = http.post("/api/token", json={"branch_id": "shellharbour"})
    assert response.status_code == 503
    assert "LIVEKIT" in response.json()["detail"]
