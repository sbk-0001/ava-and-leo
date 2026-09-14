"""Clinic portal API uses the same mock PracticeClient as Leo."""

import base64
import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from portal import create_app
from practice import PracticeClient, seed_mock_diary

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "portal_static"
STRATEGYBYTE_COLORS = ("#091736", "#FFC605", "#0061FF", "#FFEFD7")


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


def _jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_portal_token_dispatches_dental_realtime_receptionist(monkeypatch) -> None:
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secretsecretsecretsecretsecret12")
    http, _practice = _client()
    response = http.post("/api/token", json={"branch_id": "dapto"})
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "wss://example.livekit.cloud"
    claims = _jwt_payload(body["token"])
    room_config = claims.get("roomConfig") or claims.get("room_config") or {}
    agents = room_config.get("agents") or []
    assert agents, claims
    metadata_raw = agents[0].get("metadata") or ""
    metadata = json.loads(metadata_raw)
    assert metadata["persona"] == "leo"
    assert metadata["branch"] == "dapto"
    assert metadata["source"] == "portal"
    agent_name = agents[0].get("agentName") or agents[0].get("agent_name")
    assert agent_name == "ava-and-leo"


def test_portal_static_is_strategybyte_branded() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    branded = html + css
    for color in STRATEGYBYTE_COLORS:
        assert color in branded, f"missing Strategybyte color {color}"
    assert "Strategybyte" in html
    assert "Ava desk" in html
    assert "Call Ava" in html
    assert 'id="call-ava"' in html
    assert "Call Leo" not in html


def test_portal_js_plays_remote_audio_and_keeps_call_ava() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "Call Ava" in html
    assert "call-ava" in js
    assert "LivekitClient" in js
    assert "autoplay" in js
    assert "playsInline" in js or "playsinline" in js
    assert ".play(" in js
    assert "remoteParticipants" in js
    assert "startAudio" in js
    assert "TrackSubscribed" in js


def test_portal_http_serves_strategybyte_static() -> None:
    http, _practice = _client()
    page = http.get("/")
    css = http.get("/static/styles.css")
    js = http.get("/static/app.js")
    assert page.status_code == 200
    assert css.status_code == 200
    assert js.status_code == 200
    assert "Call Ava" in page.text
    assert "Strategybyte" in page.text
    assert "Ava desk" in page.text
    for color in STRATEGYBYTE_COLORS:
        assert color in css.text
    assert "call-ava" in js.text
    assert "remoteParticipants" in js.text
