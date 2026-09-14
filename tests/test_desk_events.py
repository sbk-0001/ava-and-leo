"""Live Ava desk feed: transcript + booking activity packets.

Docs: https://docs.livekit.io/reference/agents/events/#conversation_item_added
      https://docs.livekit.io/reference/agents/events/#function_tools_executed
      https://docs.livekit.io/transport/data/packets/
"""

from types import SimpleNamespace

import pytest
from livekit.agents import FunctionToolsExecutedEvent
from livekit.agents.llm import ChatMessage, FunctionCall, FunctionCallOutput

from desk_events import (
    DESK_TOPIC,
    DIARY_MUTATIONS,
    activity_packets_from_tools,
    encode_desk_packet,
    publish_desk_packet,
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


def test_transcript_packet_skips_empty_system_and_non_messages() -> None:
    assert transcript_packet(ChatMessage(role="user", content=["  "])) is None
    assert transcript_packet(ChatMessage(role="system", content=["ignore"])) is None
    assert transcript_packet(SimpleNamespace(role="user", text_content="hi")) is None


def test_activity_packet_from_successful_book() -> None:
    event = FunctionToolsExecutedEvent(
        function_calls=[
            FunctionCall(
                call_id="c1",
                name="book_appointment",
                arguments=(
                    '{"slot_id":"slot-am","reason":"check-up",'
                    '"name":"Jamie Cole","phone":"0412222333"}'
                ),
            )
        ],
        function_call_outputs=[
            FunctionCallOutput(
                call_id="c1",
                name="book_appointment",
                is_error=False,
                output=(
                    '{"ok":true,"confirmed":true,"booking_id":"bkg_abc",'
                    '"date":"2026-09-15","time":"09:30",'
                    '"clinician":"Dr Mohit Tolani","branch_id":"shellharbour"}'
                ),
            )
        ],
    )

    packets = activity_packets_from_tools(event)
    assert len(packets) == 1
    packet = packets[0]
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


def test_activity_packets_cover_lookup_availability_reschedule_cancel_message() -> None:
    event = FunctionToolsExecutedEvent(
        function_calls=[
            FunctionCall(
                call_id="c1",
                name="find_patient",
                arguments='{"name":"Priya Nair","phone":"0413000222"}',
            ),
            FunctionCall(
                call_id="c2",
                name="get_availability",
                arguments='{"date":"2026-09-16","clinician":"Dr Beena Kurian"}',
            ),
            FunctionCall(
                call_id="c3",
                name="reschedule_appointment",
                arguments='{"booking_id":"bkg_old","new_slot_id":"slot-pm"}',
            ),
            FunctionCall(
                call_id="c4",
                name="cancel_appointment",
                arguments='{"booking_id":"bkg_old"}',
            ),
            FunctionCall(
                call_id="c5",
                name="leave_message",
                arguments=(
                    '{"caller_name":"Sam Lee","phone":"0412000000",'
                    '"body":"Call back about a filling"}'
                ),
            ),
        ],
        function_call_outputs=[
            FunctionCallOutput(
                call_id="c1",
                name="find_patient",
                is_error=False,
                output='{"ok":true,"patients":[{"name":"Priya Nair","patient_id":"pat_1"}]}',
            ),
            FunctionCallOutput(
                call_id="c2",
                name="get_availability",
                is_error=False,
                output=(
                    '{"ok":true,"date":"2026-09-16","branch_id":"dapto",'
                    '"slots":[{"time":"10:00","clinician":"Dr Beena Kurian"}]}'
                ),
            ),
            FunctionCallOutput(
                call_id="c3",
                name="reschedule_appointment",
                is_error=False,
                output=(
                    '{"ok":true,"confirmed":true,"booking_id":"bkg_old",'
                    '"date":"2026-09-16","time":"10:00",'
                    '"clinician":"Dr Beena Kurian","branch_id":"dapto"}'
                ),
            ),
            FunctionCallOutput(
                call_id="c4",
                name="cancel_appointment",
                is_error=False,
                output='{"ok":true,"confirmed":true,"booking_id":"bkg_old"}',
            ),
            FunctionCallOutput(
                call_id="c5",
                name="leave_message",
                is_error=False,
                output='{"ok":true,"confirmed":true,"message_id":"msg_1","branch_id":"dapto"}',
            ),
        ],
    )

    packets = activity_packets_from_tools(event)
    actions = [packet["action"] for packet in packets]
    assert actions == [
        "find_patient",
        "get_availability",
        "reschedule_appointment",
        "cancel_appointment",
        "leave_message",
    ]
    assert packets[0]["payload"]["name"] == "Priya Nair"
    assert packets[1]["payload"]["date"] == "2026-09-16"
    assert packets[1]["payload"]["doctor"] == "Dr Beena Kurian"
    assert packets[1]["payload"]["open_slots"] == 1
    assert packets[2]["refresh_diary"] is True
    assert packets[3]["refresh_diary"] is True
    assert packets[4]["refresh_diary"] is False
    assert packets[4]["payload"]["name"] == "Sam Lee"
    assert packets[4]["payload"]["reason"] == "Call back about a filling"


def test_activity_packet_skips_failed_or_unknown_tools() -> None:
    event = FunctionToolsExecutedEvent(
        function_calls=[
            FunctionCall(
                call_id="c1",
                name="book_appointment",
                arguments='{"slot_id":"missing","reason":"check-up","name":"Jamie"}',
            ),
            FunctionCall(
                call_id="c2", name="quote_fee", arguments='{"item":"check-up"}'
            ),
            FunctionCall(call_id="c3", name="end_call", arguments="{}"),
        ],
        function_call_outputs=[
            FunctionCallOutput(
                call_id="c1",
                name="book_appointment",
                is_error=False,
                output='{"ok":false,"reason":"slot_unavailable"}',
            ),
            FunctionCallOutput(
                call_id="c2",
                name="quote_fee",
                is_error=False,
                output='{"ok":true,"item":"check-up"}',
            ),
            FunctionCallOutput(
                call_id="c3",
                name="end_call",
                is_error=True,
                output="boom",
            ),
        ],
    )
    assert activity_packets_from_tools(event) == []


def test_diary_mutations_are_book_reschedule_cancel() -> None:
    expected = {
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    }
    assert set(DIARY_MUTATIONS) == expected


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
