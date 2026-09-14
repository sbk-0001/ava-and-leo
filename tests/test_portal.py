"""Clinic portal API uses the same mock PracticeClient as Leo."""

import base64
import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from diary_store import DictDiaryStore
from portal import COOKIE_NAME, create_app
from practice import PracticeClient, seed_mock_diary

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "portal_static"
BYTE_VOICE_COLORS = (
    "#05060a",
    "#0b0f1a",
    "#f4f7ff",
    "#8b93a7",
    "#4dfff0",
    "#8b5cff",
    "#ff4d9a",
)
LEGACY_STRATEGYBYTE_COLORS = ("#091736", "#FFC605", "#FFEFD7")
REQUIRED_PORTAL_IDS = (
    "login-gate",
    "login-form",
    "login-password",
    "login-error",
    "auth-note",
    "call-ava",
    "hang-up",
    "call-status",
    "remote-audio",
    "branch-tabs",
    "facts",
    "fact-suburb",
    "fact-name",
    "diary-date",
    "diary-empty",
    "slot-list",
    "book-dialog",
    "book-form",
    "book-slot-id",
    "book-submit",
)


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
    monkeypatch.setenv("LIVEKIT_URL", "wss://shellharbour-cqvf1jsj.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secretsecretsecretsecretsecret12")
    http, _practice = _client()
    response = http.post("/api/token", json={"branch_id": "dapto"})
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "wss://shellharbour-cqvf1jsj.livekit.cloud"
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


def test_portal_static_is_byte_voice_branded() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    branded = html + css
    branded_lower = branded.lower()
    for color in BYTE_VOICE_COLORS:
        assert color in branded_lower, f"missing byte voice color {color}"
    for color in LEGACY_STRATEGYBYTE_COLORS:
        assert color.lower() not in branded_lower
    assert "byte voice" in html
    assert "Strategybyte" not in html
    assert "Strategybyte" not in css
    assert "Ava desk" in html
    assert "Call Ava" in html
    assert 'id="call-ava"' in html
    assert "Call Leo" not in html
    assert "family=Syne" in html or "family=syne" in html
    assert "family=Manrope" in html or "family=manrope" in html
    assert "prefers-reduced-motion" in css
    assert "@keyframes" in css
    assert ".call-btn.live" in css
    assert ".slot:hover" in css or ".slot.open:hover" in css
    for element_id in REQUIRED_PORTAL_IDS:
        assert f'id="{element_id}"' in html, f"missing portal id {element_id}"


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
    assert "Australia/Sydney" in js


def test_portal_http_serves_byte_voice_static() -> None:
    http, _practice = _client()
    page = http.get("/")
    css = http.get("/static/styles.css")
    js = http.get("/static/app.js")
    assert page.status_code == 200
    assert css.status_code == 200
    assert js.status_code == 200
    assert "Call Ava" in page.text
    assert "byte voice" in page.text
    assert "Strategybyte" not in page.text
    assert "Ava desk" in page.text
    for color in BYTE_VOICE_COLORS:
        assert color in css.text.lower()
    assert "call-ava" in js.text
    assert "remoteParticipants" in js.text
    assert "--i" in js.text


def test_portal_config_brand_is_byte_voice() -> None:
    http, _practice = _client()
    body = http.get("/api/config").json()
    assert body["brand"] == "byte voice"
    assert body["product"] == "Ava desk"
    assert body["call_label"] == "Call Ava"


def test_portal_requires_password_when_not_localhost() -> None:
    practice = PracticeClient(mode="mock")
    app = create_app(practice=practice, portal_password="")
    http = TestClient(app)
    forwarded = {"x-forwarded-for": "203.0.113.10"}
    denied = http.get("/api/branches", headers=forwarded)
    assert denied.status_code == 401
    assert "PORTAL_PASSWORD" in denied.json()["detail"]
    config = http.get("/api/config", headers=forwarded)
    assert config.status_code == 200
    assert config.json()["auth_required"] is True
    assert config.json()["loopback"] is False


def test_portal_login_cookie_is_secure_behind_proxy() -> None:
    practice = PracticeClient(mode="mock")
    app = create_app(practice=practice, portal_password="partner-demo")
    http = TestClient(app)
    response = http.post(
        "/api/login",
        json={"password": "partner-demo"},
        headers={"x-forwarded-for": "203.0.113.10"},
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie

    http.cookies.set(COOKIE_NAME, response.cookies.get(COOKIE_NAME))
    gated = http.get("/api/branches", headers={"x-forwarded-for": "203.0.113.10"})
    assert gated.status_code == 200


def test_two_portal_sessions_share_durable_diary() -> None:
    bucket: dict = {}
    store = DictDiaryStore(bucket)
    practice_a = PracticeClient(mode="mock", store=store)
    seed_mock_diary(practice_a, today=date(2026, 9, 14), days=3)
    practice_a.save()

    practice_b = PracticeClient(mode="mock", store=store)
    practice_b.load()
    app_a = create_app(practice=practice_a, require_auth=False)
    app_b = create_app(practice=practice_b, require_auth=False)
    staff = TestClient(app_a)
    partner = TestClient(app_b)

    diary = staff.get("/api/diary", params={"branch_id": "shellharbour"}).json()
    slot = next(item for item in diary["slots"] if not item["taken"])
    booked = staff.post(
        "/api/bookings",
        json={
            "branch_id": "shellharbour",
            "slot_id": slot["slot_id"],
            "name": "Partner Demo",
            "phone": "0412000999",
            "reason": "check up",
        },
    )
    assert booked.json()["confirmed"] is True

    partner_diary = partner.get(
        "/api/diary", params={"branch_id": "shellharbour"}
    ).json()
    taken = next(
        item for item in partner_diary["slots"] if item["slot_id"] == slot["slot_id"]
    )
    assert taken["taken"] is True
    names = [item["patient_name"] for item in partner_diary["bookings"]]
    assert "Partner Demo" in names


def test_health_reports_ava_desk() -> None:
    http, _practice = _client()
    body = http.get("/api/health").json()
    assert body["ok"] == "true"
    assert body["service"] == "ava-desk"
    assert "diary_store" in body
