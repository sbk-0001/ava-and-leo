"""Live Ava desk feed: transcript + booking activity for web and SIP rooms.

Docs: https://docs.livekit.io/reference/agents/events/#conversation_item_added
      https://docs.livekit.io/transport/data/packets/
"""

from types import SimpleNamespace

import pytest
from livekit.agents.llm import ChatMessage

from desk_events import (
    DESK_TOPIC,
    DIARY_MUTATIONS,
    activity_packet_from_result,
    desk_events_url,
    encode_desk_packet,
    post_desk_event_http,
    publish_desk_packet,
    stamp_packet,
    transcript_packet,
)


def test_desk_topic_is_namespaced() -> None:
    assert DESK_TOPIC == "ava.desk"


def test_transcript_packet_from_user_and_ava_turns() -> None:
    user = ChatMessage(role="user", content=["I'd like a check-up on Tuesday"])
    ava = ChatMessage(role="assistant", content=["I can look that up for you."])

    user_packet = transcript_packet(user)
    ava_packet = transcript_packet(ava)

    assert user_packet == {
        "type": "transcript",
        "role": "user",
        "text": "I'd like a check-up on Tuesday",
    }
    assert ava_packet == {
        "type": "transcript",
        "role": "assistant",
        "text": "I can look that up for you.",
    }


def test_transcript_packet_duck_types_realtime_turns() -> None:
    """Realtime items often are not ChatMessage — role + text_content is enough."""
    realtime = SimpleNamespace(
        role="assistant",
        text_content="I've got half past two with Dr Mohit.",
    )
    packet = transcript_packet(realtime)
    assert packet == {
        "type": "transcript",
        "role": "assistant",
        "text": "I've got half past two with Dr Mohit.",
    }
    caller = SimpleNamespace(role="user", text_content="Broken tooth, this week.")
    assert transcript_packet(caller)["role"] == "user"
    assert (
        transcript_packet(SimpleNamespace(role="system", text_content="nope")) is None
    )
    assert transcript_packet(SimpleNamespace(role="user", text_content="  ")) is None
    assert transcript_packet(SimpleNamespace(role="user")) is None


def test_transcript_packet_skips_empty_system() -> None:
    assert transcript_packet(ChatMessage(role="user", content=["  "])) is None
    assert transcript_packet(ChatMessage(role="system", content=["ignore"])) is None


def test_activity_packet_from_successful_book() -> None:
    packet = activity_packet_from_result(
        "book_appointment",
        {
            "slot_id": "slot_shellharbour_2026-09-15_0930_dr-mohit-tolani",
            "reason": "check-up",
            "name": "Jamie Cole",
            "mobile": "0412222333",
        },
        {
            "ok": True,
            "confirmed": True,
            "booking_id": "bkg_abc",
            "date": "2026-09-15",
            "time": "09:30",
            "clinician": "Dr Mohit Tolani",
            "branch_id": "shellharbour",
        },
    )
    assert packet is not None
    assert packet["type"] == "activity"
    assert packet["action"] == "book_appointment"
    assert packet["label"] == "Booked appointment"
    assert packet["refresh_diary"] is True
    payload = packet["payload"]
    assert payload["name"] == "Jamie Cole"
    assert payload["time"] == "09:30"
    assert payload["doctor"] == "Dr Mohit Tolani"
    assert payload["branch"] == "shellharbour"
    assert payload["reason"] == "check-up"
    assert payload["booking_id"] == "bkg_abc"
    assert payload["date"] == "2026-09-15"
    assert payload["phone"] == "0412222333"


def test_activity_packet_publishes_booking_failures() -> None:
    gone = activity_packet_from_result(
        "book_appointment",
        {"slot_id": "slot_shellharbour_2026-09-15_1430_dr-mohit-tolani"},
        {
            "ok": False,
            "confirmed": False,
            "reason": "slot_gone",
            "slot_id": "slot_shellharbour_2026-09-15_1430_dr-mohit-tolani",
        },
    )
    assert gone is not None
    assert gone["action"] == "book_appointment"
    assert "slot gone" in gone["label"].lower()
    assert gone["refresh_diary"] is False
    assert gone["payload"]["failure_reason"] == "slot_gone"
    assert gone["payload"]["ok"] is False
    assert gone["payload"]["confirmed"] is False

    invented = activity_packet_from_result(
        "book_appointment",
        {"slot_id": "slot-half-past-two"},
        {"ok": False, "confirmed": False, "reason": "invalid_slot_id"},
    )
    assert invented is not None
    assert invented["payload"]["failure_reason"] == "invalid_slot_id"
    assert "invalid" in invented["label"].lower()


def test_activity_packets_cover_lookup_availability_reschedule_cancel_message() -> None:
    lookup = activity_packet_from_result(
        "lookup_patient",
        {"mobile": "0413000222"},
        {"ok": True, "patients": [{"name": "Priya Nair", "patient_id": "pat_1"}]},
    )
    available = activity_packet_from_result(
        "check_availability",
        {"date_range": "next week", "clinician": "Dr Beena Kurian", "branch": "dapto"},
        {
            "ok": True,
            "date": "2026-09-22",
            "branch_id": "dapto",
            "slots": [{"time": "10:00", "clinician": "Dr Beena Kurian"}],
        },
    )
    moved = activity_packet_from_result(
        "reschedule_appointment",
        {"booking_id": "bkg_old", "new_slot_id": "slot_dapto_2026-09-16_1000_dr"},
        {
            "ok": True,
            "confirmed": True,
            "booking_id": "bkg_old",
            "date": "2026-09-16",
            "time": "10:00",
            "clinician": "Dr Beena Kurian",
            "branch_id": "dapto",
        },
    )
    cancelled = activity_packet_from_result(
        "cancel_appointment",
        {"booking_id": "bkg_old"},
        {"ok": True, "confirmed": True, "booking_id": "bkg_old"},
    )
    message = activity_packet_from_result(
        "take_message",
        {
            "name": "Sam Lee",
            "mobile": "0412000000",
            "reason": "Call back about a filling",
        },
        {"ok": True, "confirmed": True, "message_id": "msg_1", "branch_id": "dapto"},
    )
    assert lookup is not None
    assert lookup["payload"]["name"] == "Priya Nair"
    assert lookup["payload"]["matches"] == 1
    assert available is not None
    assert available["payload"]["open_slots"] == 1
    assert available["payload"]["doctor"] == "Dr Beena Kurian"
    assert moved is not None and moved["refresh_diary"] is True
    assert cancelled is not None and cancelled["refresh_diary"] is True
    assert message is not None
    assert message["refresh_diary"] is False
    assert message["payload"]["name"] == "Sam Lee"
    assert message["payload"]["reason"] == "Call back about a filling"


def test_activity_packet_skips_unknown_or_unrelated_failures() -> None:
    assert activity_packet_from_result("book_appointment", {}, {"ok": False}) is None
    assert activity_packet_from_result("quote_fee", {}, {"ok": True}) is None
    assert activity_packet_from_result("end_call", {}, {"ok": True}) is None
    assert activity_packet_from_result("lookup_patient", {}, {"ok": False}) is None
    assert (
        activity_packet_from_result(
            "reschedule_appointment", {}, {"ok": False, "reason": "not_found"}
        )
        is None
    )


def test_diary_mutations_are_book_reschedule_cancel() -> None:
    assert set(DIARY_MUTATIONS) == {
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    }


def test_desk_events_url_defaults_and_overrides() -> None:
    assert desk_events_url(env={}) == "http://127.0.0.1:8787/api/desk/events"
    assert (
        desk_events_url(env={"PORTAL_URL": "http://127.0.0.1:8787"})
        == "http://127.0.0.1:8787/api/desk/events"
    )
    assert (
        desk_events_url(env={"DESK_EVENTS_URL": "http://127.0.0.1:8787"})
        == "http://127.0.0.1:8787/api/desk/events"
    )
    assert (
        desk_events_url(
            env={"DESK_EVENTS_URL": "http://127.0.0.1:8787/api/desk/events"}
        )
        == "http://127.0.0.1:8787/api/desk/events"
    )


def test_stamp_packet_marks_sip_and_portal_rooms() -> None:
    sip = stamp_packet(
        {"type": "transcript", "role": "user", "text": "hi"},
        room=SimpleNamespace(name="call-+61412345678"),
    )
    assert sip["id"].startswith("desk_")
    assert sip["room"] == "call-+61412345678"
    assert sip["channel"] == "sip"
    portal = stamp_packet(
        {"type": "activity", "action": "book_appointment"},
        room=SimpleNamespace(name="ava-portal-shellharbour-ab12cd34"),
    )
    assert portal["channel"] == "portal"
    assert stamp_packet({"id": "keep", "type": "activity"})["id"] == "keep"


def test_encode_desk_packet_is_json_bytes() -> None:
    raw = encode_desk_packet({"type": "transcript", "role": "user", "text": "hi"})
    assert raw == b'{"type":"transcript","role":"user","text":"hi"}'


@pytest.mark.asyncio
async def test_publish_desk_packet_uses_reliable_topic() -> None:
    published: dict[str, object] = {}

    class Participant:
        async def publish_data(self, payload, *, reliable, topic):
            published["payload"] = payload
            published["reliable"] = reliable
            published["topic"] = topic

    await publish_desk_packet(
        SimpleNamespace(local_participant=Participant()),
        {"type": "transcript", "role": "user", "text": "hi"},
    )
    assert published["reliable"] is True
    assert published["topic"] == DESK_TOPIC
    assert published["payload"] == b'{"type":"transcript","role":"user","text":"hi"}'


@pytest.mark.asyncio
async def test_desk_http_post_failures_log_warning(monkeypatch, caplog) -> None:
    def _boom(_packet):
        raise TimeoutError("timed out")

    monkeypatch.setattr("desk_events._post_desk_event_sync", _boom)
    with caplog.at_level("WARNING", logger="desk"):
        await post_desk_event_http({"type": "transcript", "role": "user", "text": "hi"})
    assert any("desk http post failed" in rec.message for rec in caplog.records)
    assert not any(rec.levelname == "DEBUG" for rec in caplog.records)


def test_agent_publishes_desk_feed_for_every_ava_room() -> None:
    """Web Call Ava and inbound SIP (call-+61…) share the same desk wiring."""
    import inspect

    from agent import _register_desk_feed, my_agent
    from ava_receptionist import AvaReceptionist

    feed = inspect.getsource(_register_desk_feed)
    entry = inspect.getsource(my_agent)
    assert "conversation_item_added" in feed
    assert "schedule_desk_publish" in feed
    assert "transcript_packet" in feed
    assert "ava-portal" not in feed
    assert "_register_desk_feed(session, ctx.room)" in entry
    assert 'persona_key == "ava"' in entry
    assert "_notify_desk" in inspect.getsource(AvaReceptionist)
    assert "schedule_desk_publish" in inspect.getsource(AvaReceptionist)
    assert "grounding_violation" in inspect.getsource(AvaReceptionist)


def test_grounding_violation_packet_is_typed() -> None:
    from grounding import GateResult, grounding_violation_packet

    packet = grounding_violation_packet(
        GateResult(
            original="I've got half past two",
            spoken="Hang on, let me check that properly — I don't want to give you the wrong time.",
            suppressed=True,
            violations=["time"],
        ),
        count=2,
        branch="shellharbour",
    )
    assert packet["type"] == "grounding_violation"
    assert packet["label"] == "GROUNDING_VIOLATION"
    assert packet["payload"]["count"] == 2
    assert packet["payload"]["violations"] == ["time"]
