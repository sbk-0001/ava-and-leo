"""Publish live transcript and booking activity to the clinic desk.

Ava and the staff browser share one LiveKit room during Call Ava. This module
turns AgentSession events into small JSON data packets on topic ``ava.desk``.

Why data packets instead of only ``lk.transcription`` text streams:
conversation_item_added is already the source of the post-call transcript and
fires for both user and assistant ChatMessages, including OpenAI Realtime.
Booking activity has no built-in frontend event, so the same room data channel
carries both.

Docs: https://docs.livekit.io/reference/agents/events/#conversation_item_added
      https://docs.livekit.io/reference/agents/events/#function_tools_executed
      https://docs.livekit.io/transport/data/packets/
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from livekit.agents import FunctionToolsExecutedEvent
from livekit.agents.llm import ChatMessage, FunctionCall, FunctionCallOutput

logger = logging.getLogger("desk")

DESK_TOPIC = "ava.desk"
_PENDING_PUBLISHES: set[asyncio.Task[None]] = set()

DESK_TOOLS = frozenset(
    {
        "find_patient",
        "get_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "leave_message",
    }
)

DIARY_MUTATIONS = frozenset(
    {
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    }
)

_ACTIVITY_LABELS = {
    "find_patient": "Looked up patient",
    "get_availability": "Checked availability",
    "book_appointment": "Booked appointment",
    "reschedule_appointment": "Rescheduled appointment",
    "cancel_appointment": "Cancelled appointment",
    "leave_message": "Took a message",
}


def _parse_jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def transcript_packet(item: Any) -> dict[str, Any] | None:
    """Build a desk packet from a committed conversation turn."""
    if not isinstance(item, ChatMessage):
        return None
    if item.role not in {"user", "assistant"}:
        return None
    text = (item.text_content or "").strip()
    if not text:
        return None
    return {"type": "transcript", "role": item.role, "text": text}


def activity_payload(action: str, arguments: Any, output: Any) -> dict[str, Any]:
    """Flatten tool args + result into the fields the desk shows."""
    args = _parse_jsonish(arguments)
    result = _parse_jsonish(output)
    patients = (
        result.get("patients") if isinstance(result.get("patients"), list) else []
    )
    slots = result.get("slots") if isinstance(result.get("slots"), list) else []
    first_patient = patients[0] if patients and isinstance(patients[0], dict) else {}
    first_slot = slots[0] if slots and isinstance(slots[0], dict) else {}

    payload: dict[str, Any] = {
        "name": _first(
            args.get("name"),
            args.get("caller_name"),
            result.get("name"),
            first_patient.get("name"),
        ),
        "time": _first(result.get("time"), args.get("time"), first_slot.get("time")),
        "doctor": _first(
            result.get("clinician"),
            args.get("clinician"),
            first_slot.get("clinician"),
        ),
        "branch": _first(result.get("branch_id"), args.get("branch_id")),
        "reason": _first(args.get("reason"), args.get("body"), result.get("reason")),
        "booking_id": _first(result.get("booking_id"), args.get("booking_id")),
        "date": _first(result.get("date"), args.get("date"), first_slot.get("date")),
        "phone": _first(
            args.get("phone"),
            result.get("phone"),
            first_patient.get("phone"),
        ),
        "ok": bool(result.get("ok")),
        "confirmed": bool(result.get("confirmed")),
    }
    if action == "get_availability":
        payload["open_slots"] = len(slots)
    if action == "find_patient":
        payload["matches"] = len(patients)
    return {key: value for key, value in payload.items() if value not in (None, "")}


def activity_packet(
    call: FunctionCall,
    output: FunctionCallOutput,
) -> dict[str, Any] | None:
    """Build a desk activity packet for one successful practice tool."""
    name = call.name or output.name
    if name not in DESK_TOOLS or output.is_error:
        return None
    result = _parse_jsonish(output.output)
    if not result.get("ok"):
        return None
    payload = activity_payload(name, call.arguments, output.output)
    return {
        "type": "activity",
        "action": name,
        "label": _ACTIVITY_LABELS.get(name, name),
        "payload": payload,
        "refresh_diary": name in DIARY_MUTATIONS
        and bool(result.get("confirmed") or result.get("ok")),
    }


def activity_packets_from_tools(
    event: FunctionToolsExecutedEvent,
) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for call, output in event.zipped():
        packet = activity_packet(call, output)
        if packet:
            packets.append(packet)
    return packets


def encode_desk_packet(packet: dict[str, Any]) -> bytes:
    return json.dumps(packet, separators=(",", ":"), default=str).encode("utf-8")


async def publish_desk_packet(room: Any, packet: dict[str, Any]) -> None:
    """Send one JSON packet on the room data channel. Never raise to the session."""
    participant = getattr(room, "local_participant", None)
    publish = getattr(participant, "publish_data", None)
    if publish is None:
        return
    try:
        await publish(encode_desk_packet(packet), reliable=True, topic=DESK_TOPIC)
    except Exception:
        logger.exception("desk publish failed action=%s", packet.get("type"))


def schedule_desk_publish(room: Any, packet: dict[str, Any] | None) -> None:
    if not packet:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(publish_desk_packet(room, packet))
    _PENDING_PUBLISHES.add(task)
    task.add_done_callback(_PENDING_PUBLISHES.discard)
