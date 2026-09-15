"""Publish live transcript and booking activity to the clinic desk.

Two delivery paths (both kept):
1. LiveKit room data packets on topic ``ava.desk``.
2. A local HTTP bus: the worker POSTs ``/api/desk/events`` and the signed-in
   desk reads ``GET /api/desk/stream`` (SSE). Phone/SIP rooms such as
   ``call-+61…`` have no portal participant, so the HTTP bus is required.

Realtime conversation items are not always ``ChatMessage`` instances — packet
builders duck-type ``role`` + ``text_content`` so those turns still publish.

Docs: https://docs.livekit.io/reference/agents/events/#conversation_item_added
      https://docs.livekit.io/transport/data/packets/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from livekit.agents.llm import FunctionCall, FunctionCallOutput

logger = logging.getLogger("desk")

DESK_TOPIC = "ava.desk"
_PENDING_PUBLISHES: set[asyncio.Task[None]] = set()

DESK_TOOLS = frozenset(
    {
        "lookup_patient",
        "find_patient",
        "check_availability",
        "get_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
        "take_message",
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

BOOKING_FAILURE_REASONS = frozenset({"slot_gone", "invalid_slot_id"})

_ACTIVITY_LABELS = {
    "lookup_patient": "Looked up patient",
    "find_patient": "Looked up patient",
    "check_availability": "Checked availability",
    "get_availability": "Checked availability",
    "book_appointment": "Booked appointment",
    "reschedule_appointment": "Rescheduled appointment",
    "cancel_appointment": "Cancelled appointment",
    "take_message": "Took a message",
    "leave_message": "Took a message",
}

_FAILURE_LABELS = {
    "slot_gone": "Booking failed (slot gone)",
    "invalid_slot_id": "Booking failed (invalid slot)",
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


def _item_text(item: Any) -> str:
    text = getattr(item, "text_content", None)
    if text not in (None, ""):
        return str(text)
    content = getattr(item, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            else:
                nested = getattr(part, "text", None) or getattr(part, "content", None)
                if nested:
                    parts.append(str(nested))
        return "".join(parts)
    fallback = getattr(item, "text", None)
    return str(fallback or "")


def transcript_packet(item: Any) -> dict[str, Any] | None:
    """Build a desk packet from a committed conversation turn.

    Duck-type ``role`` + ``text_content`` so OpenAI Realtime items publish even
    when they are not ``livekit.agents.llm.ChatMessage`` instances.
    """
    role = getattr(item, "role", None)
    if role not in {"user", "assistant"}:
        return None
    text = _item_text(item).strip()
    if not text:
        return None
    return {"type": "transcript", "role": role, "text": text}


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
        "branch": _first(
            result.get("branch_id"),
            args.get("branch_id"),
            args.get("branch"),
        ),
        "reason": _first(args.get("reason"), args.get("body"), result.get("reason")),
        "booking_id": _first(result.get("booking_id"), args.get("booking_id")),
        "slot_id": _first(
            result.get("slot_id"), args.get("slot_id"), args.get("new_slot_id")
        ),
        "date": _first(
            result.get("date"),
            args.get("date"),
            args.get("date_range"),
            first_slot.get("date"),
        ),
        "phone": _first(
            args.get("phone"),
            args.get("mobile"),
            result.get("phone"),
            result.get("mobile"),
            first_patient.get("phone"),
            first_patient.get("mobile"),
        ),
        "ok": bool(result.get("ok")),
        "confirmed": bool(result.get("confirmed")),
    }
    failure = str(result.get("reason") or "")
    if failure and not result.get("ok"):
        payload["failure_reason"] = failure
    if action in {"check_availability", "get_availability"}:
        payload["open_slots"] = len(slots)
    if action in {"lookup_patient", "find_patient"}:
        payload["matches"] = len(patients)
    return {key: value for key, value in payload.items() if value not in (None, "")}


def activity_packet_from_result(
    action: str,
    arguments: Any,
    result: Any,
) -> dict[str, Any] | None:
    """Build a desk activity packet from a practice-tool result.

    Successful tools always publish. Booking failures with ``slot_gone`` or
    ``invalid_slot_id`` also publish so the desk can see a near-miss book.
    """
    if action not in DESK_TOOLS:
        return None
    parsed = _parse_jsonish(result)
    ok = bool(parsed.get("ok"))
    reason = str(parsed.get("reason") or "")
    is_booking_failure = action in DIARY_MUTATIONS and reason in BOOKING_FAILURE_REASONS
    if not ok and not is_booking_failure:
        return None
    payload = activity_payload(action, arguments, result)
    label = _ACTIVITY_LABELS.get(action, action)
    if not ok:
        label = _FAILURE_LABELS.get(reason, f"{label} failed")
    return {
        "type": "activity",
        "action": action,
        "label": label,
        "payload": payload,
        "refresh_diary": action in DIARY_MUTATIONS and bool(parsed.get("confirmed")),
    }


def activity_packet(
    call: FunctionCall,
    output: FunctionCallOutput,
) -> dict[str, Any] | None:
    """Build a desk activity packet for one successful practice tool."""
    if output.is_error:
        return None
    return activity_packet_from_result(
        call.name or output.name, call.arguments, output.output
    )


def stamp_packet(packet: dict[str, Any], *, room: Any = None) -> dict[str, Any]:
    stamped = dict(packet)
    stamped.setdefault("id", f"desk_{uuid.uuid4().hex[:12]}")
    room_name = getattr(room, "name", None) or stamped.get("room")
    if room_name:
        stamped.setdefault("room", room_name)
        name = str(room_name)
        if name.startswith(("call-", "sip-")):
            stamped.setdefault("channel", "sip")
        elif name.startswith("ava-portal-"):
            stamped.setdefault("channel", "portal")
    return stamped


def encode_desk_packet(packet: dict[str, Any]) -> bytes:
    return json.dumps(packet, separators=(",", ":"), default=str).encode("utf-8")


def desk_events_url(env: dict[str, str] | None = None) -> str:
    """Worker POST target. Prefer DESK_EVENTS_URL, then PORTAL_URL, then host:port."""
    environ = env if env is not None else os.environ
    raw = str(environ.get("DESK_EVENTS_URL") or environ.get("PORTAL_URL") or "").strip()
    if raw:
        base = raw.rstrip("/")
        if base.endswith("/api/desk/events"):
            return base
        return f"{base}/api/desk/events"
    host = str(environ.get("PORTAL_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = str(environ.get("PORTAL_PORT") or "8787").strip() or "8787"
    return f"http://{host}:{port}/api/desk/events"


def _post_desk_event_sync(packet: dict[str, Any]) -> None:
    url = desk_events_url()
    headers = {"Content-Type": "application/json"}
    token = os.getenv("DESK_EVENTS_TOKEN", "").strip()
    if token:
        headers["X-Desk-Token"] = token
    request = Request(
        url,
        data=encode_desk_packet(packet),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        response.read()


async def post_desk_event_http(packet: dict[str, Any]) -> None:
    """POST one packet to the local portal bus. Never raise to the session."""
    try:
        await asyncio.to_thread(_post_desk_event_sync, packet)
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("desk http post failed url=%s error=%s", desk_events_url(), exc)
    except Exception:
        logger.exception("desk http post failed")


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


async def emit_desk_event(packet: dict[str, Any], *, room: Any = None) -> None:
    """Send one packet on LiveKit data *and* the local HTTP bus."""
    stamped = stamp_packet(packet, room=room)
    await publish_desk_packet(room, stamped)
    await post_desk_event_http(stamped)


def schedule_desk_publish(room: Any, packet: dict[str, Any] | None) -> None:
    if not packet:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(emit_desk_event(packet, room=room))
    _PENDING_PUBLISHES.add(task)
    task.add_done_callback(_PENDING_PUBLISHES.discard)


class DeskBus:
    """In-process fan-out for portal SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, packet: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(packet)
            except asyncio.QueueFull:
                logger.warning("desk bus subscriber is full; dropping a packet")

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
