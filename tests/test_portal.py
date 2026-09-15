"""Clinic portal API uses the same mock PracticeClient as Ava."""

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from portal import _cookie_digest, create_app
from practice import PracticeClient, seed_mock_diary

STATIC = Path(__file__).resolve().parents[1] / "src" / "portal_static"


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
    assert body["parking"] == "VERIFY"
    assert body["dentists"] == []
    assert "35 Baan Baan Street" in body["address"]


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
    assert "Ava" in response.json()["detail"]


def test_portal_config_persona_is_ava() -> None:
    http, _practice = _client()
    body = http.get("/api/config").json()
    assert body["persona"] == "ava"
    assert body["voice"] == "marin"


def test_portal_health_is_ava_desk() -> None:
    http, _practice = _client()
    body = http.get("/api/health").json()
    assert body["ok"] == "true"
    assert "ava" in body["service"]
    assert "leo" not in body["service"]


def test_portal_ui_says_call_ava() -> None:
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    assert "Call Ava" in html
    assert "Call Leo" not in html
    assert "Ava desk" in html
    assert "Leo desk" not in html
    assert "Connecting to Ava" in js
    assert "Connected to Ava" in js
    assert "Leo" not in js


def test_portal_phone_brand_is_ava_desk() -> None:
    """Portal is a staff desk, not the inbound greeting brand."""
    html = (STATIC / "index.html").read_text()
    assert "Ava desk" in html
    assert "Call Ava" in html


def test_portal_has_live_call_panel() -> None:
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    assert "live-call" in html
    assert "inbound phone" in html.lower()
    assert "EventSource" in js
    assert "/api/desk/stream" in js
    assert "?token=" in js
    assert "refresh_diary" in js
    assert "live-transcript" in js
    assert "live-activity" in js
    assert "call-+" not in js or "sip" in js.lower()
    assert "subscribeDeskStream" in js


def test_desk_http_bus_posts_reach_subscribers() -> None:
    http, _practice = _client()
    queue = http.app.state.desk_bus.subscribe()
    packet = {
        "type": "activity",
        "action": "book_appointment",
        "label": "Booked appointment",
        "payload": {
            "name": "Jamie Cole",
            "time": "09:30",
            "doctor": "Dr Mohit Tolani",
            "booking_id": "bkg_1",
        },
        "refresh_diary": True,
        "id": "desk_test_1",
        "room": "call-+61412345678",
        "channel": "sip",
    }
    posted = http.post("/api/desk/events", json=packet)
    assert posted.status_code == 200
    assert posted.json()["ok"] is True
    assert queue.get_nowait() == packet


def test_desk_http_bus_rejects_junk() -> None:
    http, _practice = _client()
    assert http.post("/api/desk/events", json={"nope": True}).status_code == 400


def test_desk_stream_requires_sign_in() -> None:
    practice = PracticeClient(mode="mock")
    app = create_app(practice=practice, require_auth=True, portal_password="secret")
    http = TestClient(app)
    assert http.get("/api/desk/stream").status_code == 401
    assert (
        http.post(
            "/api/desk/events",
            json={"type": "activity", "action": "book_appointment"},
        ).status_code
        == 403
    )


def test_desk_stream_accepts_query_token() -> None:
    """SSE uses the same _auth as the rest of the desk: cookie or ?token=."""
    practice = PracticeClient(mode="mock")
    app = create_app(practice=practice, require_auth=True, portal_password="secret")
    http = TestClient(app)
    digest = _cookie_digest("secret")
    assert http.get("/api/desk/stream").status_code == 401
    assert http.get("/api/branches").status_code == 401
    assert http.get(f"/api/branches?token={digest}").status_code == 200
    assert http.get("/api/branches?token=secret").status_code == 200
    assert http.get("/api/desk/stream?token=wrong").status_code == 401
