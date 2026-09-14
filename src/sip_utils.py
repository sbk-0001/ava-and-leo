"""SIP helpers: DID→branch mapping, metadata, and disconnect handling.

Inbound DID mapping uses sip.trunkPhoneNumber (the number the caller dialled).
See https://docs.livekit.io/reference/telephony/sip-participant/
Disconnect handling follows
https://docs.livekit.io/telephony/making-calls/outbound-calls/#mid-call-disconnections
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

from livekit import api, rtc
from livekit.agents import JobContext

from persona import DEFAULT_BRANCH_ID

logger = logging.getLogger("sip")

# LiveKit reasons that do not auto-close AgentSession; we must ctx.shutdown().
_SHUTDOWN_REASONS = frozenset({"USER_UNAVAILABLE", "SIP_TRUNK_FAILURE"})


def normalize_au_phone(value: str | None) -> str:
    """Normalise an Australian number to +61… E.164, or digits with + if already international."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    if digits.startswith("61") and len(digits) >= 11:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) >= 9:
        return f"+61{digits[1:]}"
    if value.strip().startswith("+"):
        return f"+{digits}"
    return f"+{digits}"


def parse_sip_did_map(raw: str | None) -> dict[str, str]:
    """Parse SIP_DID_MAP like '+61242169911:shellharbour,0242880737:dapto'."""
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for part in raw.split(","):
        item = part.strip()
        if not item or ":" not in item:
            continue
        did, branch = item.rsplit(":", 1)
        did_n = normalize_au_phone(did.strip())
        branch_id = branch.strip().lower()
        if did_n and branch_id:
            mapping[did_n] = branch_id
    return mapping


def did_map_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = env if env is not None else os.environ
    return parse_sip_did_map(environ.get("SIP_DID_MAP", ""))


def branch_from_did(
    did: str | None,
    mapping: dict[str, str] | None = None,
    *,
    default: str = DEFAULT_BRANCH_ID,
) -> str:
    mapping = mapping if mapping is not None else did_map_from_env()
    key = normalize_au_phone(did)
    if key and key in mapping:
        return mapping[key]
    return default


def branch_from_participant(
    participant: rtc.RemoteParticipant | None,
    mapping: dict[str, str] | None = None,
) -> str:
    did = None
    if participant is not None:
        did = participant.attributes.get("sip.trunkPhoneNumber")
    return branch_from_did(did, mapping)


def parse_job_metadata(raw: str | None) -> dict[str, Any]:
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def is_sip_participant(participant: rtc.Participant | None) -> bool:
    if participant is None:
        return False
    return participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP


def find_sip_participant(room: rtc.Room) -> rtc.RemoteParticipant | None:
    return next(
        (
            participant
            for participant in room.remote_participants.values()
            if is_sip_participant(participant)
        ),
        None,
    )


def disconnect_reason_name(reason: Any) -> str:
    if reason is None:
        return ""
    name_fn = getattr(rtc.DisconnectReason, "Name", None)
    if callable(name_fn):
        try:
            return str(name_fn(reason))
        except (ValueError, KeyError):
            pass
    return str(getattr(reason, "name", reason))


def should_shutdown_on_disconnect(reason: Any) -> bool:
    name = reason if isinstance(reason, str) else disconnect_reason_name(reason)
    return name in _SHUTDOWN_REASONS


def sip_call_error_types() -> tuple[type[BaseException], ...]:
    """Exceptions raised when CreateSIPParticipant wait_until_answered fails.

    Current livekit-api 1.1.1 raises TwirpError with sip_status_code in metadata.
    Newer docs describe SipCallError; catch that too when it exists.
    """
    types: list[type[BaseException]] = [api.TwirpError]
    sip_error = getattr(api, "SipCallError", None)
    if isinstance(sip_error, type) and issubclass(sip_error, BaseException):
        types.insert(0, sip_error)
    return tuple(types)


SIP_CALL_ERRORS = sip_call_error_types()


def sip_error_details(exc: BaseException) -> tuple[str, str]:
    code = getattr(exc, "sip_status_code", None)
    status = getattr(exc, "sip_status", None)
    metadata = getattr(exc, "metadata", None) or {}
    if code is None:
        code = metadata.get("sip_status_code", "")
        status = metadata.get("sip_status", "")
    return str(code or ""), str(status or "")


def register_sip_disconnect_handler(
    ctx: JobContext,
    sip_identity: str | None = None,
) -> None:
    """Log SIP disconnects and shut down on reasons RoomIO does not auto-close.

    Per LiveKit docs, CLIENT_INITIATED, ROOM_DELETED, and USER_REJECTED already
    close AgentSession. USER_UNAVAILABLE and SIP_TRUNK_FAILURE require
    ctx.shutdown() so the job is released.
    """

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        if sip_identity and participant.identity != sip_identity:
            if not is_sip_participant(participant):
                return
        elif not sip_identity and not is_sip_participant(participant):
            return

        reason = participant.disconnect_reason
        name = disconnect_reason_name(reason)
        logger.info(
            "SIP participant disconnected identity=%s reason=%s",
            participant.identity,
            name,
        )
        if name == "CLIENT_INITIATED":
            logger.info("Callee or caller hung up after the call was answered")
        elif name == "USER_REJECTED":
            logger.info("Callee rejected the call before answering")
        elif name == "USER_UNAVAILABLE":
            logger.info("Callee was unavailable")
        elif name == "SIP_TRUNK_FAILURE":
            logger.info("SIP trunk or protocol failure")

        if should_shutdown_on_disconnect(reason):
            ctx.shutdown(reason=f"sip_disconnect:{name}")
