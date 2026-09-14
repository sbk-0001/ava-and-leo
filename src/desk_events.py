"""Publish live transcript and booking activity to the clinic desk.

Two delivery paths (both kept):
1. LiveKit room data packets on topic ``ava.desk``.
2. A local HTTP bus: the worker POSTs ``/api/desk/events`` and the signed-in
   desk reads ``GET /api/desk/stream`` (SSE). Realtime tool-result events are
   not always reliable, so practice tools also emit activity themselves.

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


def activity_packet_from_result(
    action: str,
    arguments: Any,
    result: Any,
) -> dict[str, Any] | None:
    """Build a desk activity packet from a successful practice-tool result."""
    if action not in DESK_TOOLS:
        return None
    parsed = _parse_jsonish(result)
    if not parsed.get("ok"):
        return None
    payload = activity_payload(action, arguments, result)
    return {
        "type": "activity",
        "action": action,
        "label": _ACTIVITY_LABELS.get(action, action),
        "payload": payload,
        "refresh_diary": action in DIARY_MUTATIONS
        and bool(parsed.get("confirmed") or parsed.get("ok")),
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


def activity_packets_from_tools(
    event: FunctionToolsExecutedEvent,
) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for call, output in event.zipped():
        packet = activity_packet(call, output)
        if packet:
            packets.append(packet)
    return packets


def stamp_packet(packet: dict[str, Any]) -> dict[str, Any]:
    stamped = dict(packet)
    stamped.setdefault("id", f"desk_{uuid.uuid4().hex[:12]}")
    return stamped


def encode_desk_packet(packet: dict[str, Any]) -> bytes:
    return json.dumps(packet, separators=(",", ":"), default=str).encode("utf-8")


def desk_events_url(env: dict[str, str] | None = None) -> str:
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
    except (URLError, TimeoutError, OSError):
        logger.debug("desk http post skipped url=%s", desk_events_url())
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
    stamped = stamp_packet(packet)
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
