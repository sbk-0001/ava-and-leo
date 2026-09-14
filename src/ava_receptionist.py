"""Ava — Shellharbour Dentists OpenAI Realtime receptionist."""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Mapping
from typing import Any

from livekit import api
from livekit.agents import Agent, RunContext, function_tool, get_job_context
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import openai
from openai.types.beta.realtime.session import TurnDetection

from persona import (
    ava_instructions,
    get_branch,
    quote_fee,
    suggest_clinics_for_location,
)
from persona import (
    lookup_clinician as lookup_clinician_across_group,
)
from practice import PracticeClient
from sip_utils import find_sip_participant

logger = logging.getLogger("ava")

AVA_REALTIME_MODEL = "gpt-realtime"
# OpenAI Realtime built-in voices (do not invent IDs): alloy, ash, ballad,
# coral, echo, sage, shimmer, verse, marin, cedar.
# LiveKit plugin: https://docs.livekit.io/agents/models/realtime/plugins/openai/
# OpenAI voice list: https://developers.openai.com/docs/guides/realtime-conversations#voice-options
# There is no AU-specific Realtime voice. OpenAI recommends marin or cedar for
# best quality on gpt-realtime; marin is the feminine pair, cedar the masculine.
# Older female-leaning voices (coral, shimmer, sage) are lower quality on this
# model. Keep marin for a soft, warm, energetic female receptionist; Australian
# English comes from persona instructions, not a different voice ID.
AVA_DEFAULT_VOICE = "marin"


def resolve_ava_voice(env: Mapping[str, str] | None = None) -> str:
    """Realtime voice. AVA_REALTIME_VOICE wins; LEO_REALTIME_VOICE is a legacy alias."""
    environ = env if env is not None else os.environ
    voice = str(
        environ.get("AVA_REALTIME_VOICE") or environ.get("LEO_REALTIME_VOICE") or ""
    ).strip()
    return voice or AVA_DEFAULT_VOICE


def ava_realtime_model() -> openai.realtime.RealtimeModel:
    """OpenAI Realtime speech-to-speech model for Ava telephony.

    Low-latency path: server VAD with a tighter silence window (telephony-friendly)
    and interrupt_response so the caller can barge in. Do not claim zero latency.
    Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection
          https://docs.livekit.io/agents/logic/turns/#interruption-in-realtime-mode
    """
    return openai.realtime.RealtimeModel(
        model=AVA_REALTIME_MODEL,
        voice=resolve_ava_voice(),
        turn_detection=TurnDetection(
            type="server_vad",
            threshold=0.7,
            prefix_padding_ms=300,
            silence_duration_ms=400,
            create_response=True,
            interrupt_response=True,
        ),
    )


class AvaReceptionist(Agent):
    """Australian-English phone receptionist for Shellharbour Dentists."""

    def __init__(
        self,
        *,
        branch_id: str,
        practice: PracticeClient,
        transfer_to: str | None = None,
    ) -> None:
        self.branch = get_branch(branch_id)
        self.practice = practice
        self.transfer_to = transfer_to
        end_call_kwargs: dict[str, Any] = {
            "extra_description": (
                "End the call only after the caller is finished. Confirm they do "
                "not need anything else, then say goodbye in Australian English."
            ),
            "delete_room": True,
            "end_instructions": (
                "Thank them briefly in warm Australian English and say goodbye. "
                "Keep it to one short sentence. Sound human, not scripted."
            ),
        }
        # Hide end_call during greeting. Older SDKs omit this kwarg; passing it
        # blindly TypeErrors and crashes console. Docs:
        # https://docs.livekit.io/agents/prebuilt/tools/end-call-tool/
        if "ignore_on_enter" in inspect.signature(EndCallTool.__init__).parameters:
            end_call_kwargs["ignore_on_enter"] = True
        end_call = EndCallTool(**end_call_kwargs)
        super().__init__(
            instructions=ava_instructions(self.branch.id),
            llm=ava_realtime_model(),
            tools=end_call.tools,
        )

    @function_tool()
    async def find_patient(
        self,
        context: RunContext,
        name: str,
        phone: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        """Look up a patient in the practice software. Never invent a record.

        Args:
            name: Caller's full name as they said it.
            phone: Mobile or home number if given.
            date_of_birth: Date of birth if given, preferably YYYY-MM-DD.
        """
        logger.info("find_patient name=%s", name)
        return await self.practice.find_patient(
            name=name, phone=phone, date_of_birth=date_of_birth
        )

    @function_tool()
    async def lookup_nearby_clinics(
        self,
        context: RunContext,
        location: str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Rank group clinics for the caller's suburb and list dentists rostered that day.

        Args:
            location: Suburb or area they said, e.g. Dapto, Warilla, Woonona.
            date: Optional YYYY-MM-DD. Defaults to today in Australia/Sydney.
        """
        logger.info("lookup_nearby_clinics location=%s date=%s", location, date)
        return suggest_clinics_for_location(location, on_date=date)

    @function_tool()
    async def lookup_clinician(
        self,
        context: RunContext,
        name: str,
        date: str | None = None,
        near_branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Find where a preferred dentist is listed and rostered today.

        If they are not at the nearest clinic today but are at another group
        clinic, offer_other_clinic explains that farther site. Never invent a dentist.

        Args:
            name: Dentist name as the caller said it, e.g. Dr Mohit Tolani.
            date: Optional YYYY-MM-DD. Defaults to today in Australia/Sydney.
            near_branch_id: Nearest clinic id from lookup_nearby_clinics, if known.
        """
        logger.info(
            "lookup_clinician name=%s date=%s near=%s", name, date, near_branch_id
        )
        return lookup_clinician_across_group(
            name, on_date=date, near_branch_id=near_branch_id
        )

    @function_tool()
    async def get_availability(
        self,
        context: RunContext,
        date: str,
        branch_id: str | None = None,
        clinician: str | None = None,
    ) -> dict[str, Any]:
        """Check diary availability. Only returns real slots; never invent times.

        Args:
            date: Requested date in YYYY-MM-DD.
            branch_id: Clinic id (shellharbour, dapto, woonona). Omit to search the group.
            clinician: Optional dentist name to filter by.
        """
        logger.info(
            "get_availability date=%s branch=%s clinician=%s",
            date,
            branch_id,
            clinician,
        )
        return await self.practice.get_availability(
            branch_id=branch_id, date=date, clinician=clinician
        )

    @function_tool()
    async def book_appointment(
        self,
        context: RunContext,
        slot_id: str,
        reason: str,
        branch_id: str | None = None,
        patient_id: str | None = None,
        name: str | None = None,
        phone: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        """Book a diary slot returned by get_availability. Say confirmed only if confirmed is true.

        Args:
            slot_id: Slot id from get_availability.
            reason: Short reason for the visit.
            branch_id: Clinic id for that slot. Optional if the slot already has a branch.
            patient_id: Patient id from find_patient, if known.
            name: Full name for a new patient when no patient_id exists.
            phone: Mobile for a new patient.
            date_of_birth: Date of birth if given, preferably YYYY-MM-DD.
        """
        logger.info(
            "book_appointment slot=%s branch=%s patient=%s name=%s",
            slot_id,
            branch_id,
            patient_id,
            name,
        )
        return await self.practice.book_appointment(
            branch_id=branch_id,
            slot_id=slot_id,
            reason=reason,
            patient_id=patient_id,
            name=name,
            phone=phone,
            date_of_birth=date_of_birth,
        )

    @function_tool()
    async def reschedule_appointment(
        self,
        context: RunContext,
        booking_id: str,
        new_slot_id: str,
    ) -> dict[str, Any]:
        """Move an existing booking to a new slot from get_availability.

        Args:
            booking_id: Existing booking id.
            new_slot_id: New slot id from get_availability.
        """
        logger.info(
            "reschedule_appointment booking=%s slot=%s", booking_id, new_slot_id
        )
        return await self.practice.reschedule_appointment(
            booking_id=booking_id, new_slot_id=new_slot_id
        )

    @function_tool()
    async def cancel_appointment(
        self,
        context: RunContext,
        booking_id: str,
    ) -> dict[str, Any]:
        """Cancel an existing booking. Say confirmed only if confirmed is true.

        Args:
            booking_id: Existing booking id.
        """
        logger.info("cancel_appointment booking=%s", booking_id)
        return await self.practice.cancel_appointment(booking_id=booking_id)

    @function_tool()
    async def quote_fee(
        self,
        context: RunContext,
        item: str,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Quote a canned fee only. If unverified, do not invent a price.

        Args:
            item: Treatment or item the caller asked about, e.g. check-up, filling, emergency consult.
            branch_id: Clinic id if known; defaults to the dialled hint branch.
        """
        logger.info("quote_fee item=%s branch=%s", item, branch_id)
        return quote_fee(item, branch_id or self.branch.id)

    @function_tool()
    async def leave_message(
        self,
        context: RunContext,
        caller_name: str,
        phone: str,
        body: str,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Leave a message for the practice team.

        Args:
            caller_name: Caller's name.
            phone: Call-back number.
            body: Message for the team.
            branch_id: Clinic id if known; defaults to the dialled hint branch.
        """
        logger.info("leave_message name=%s", caller_name)
        return await self.practice.leave_message(
            branch_id=branch_id or self.branch.id,
            caller_name=caller_name,
            phone=phone,
            body=body,
        )

    @function_tool()
    async def emergency(
        self,
        context: RunContext,
        symptoms: str,
        breathing_affected: bool = False,
        uncontrolled_bleeding: bool = False,
        unconscious: bool = False,
    ) -> dict[str, Any]:
        """Triage a dental or medical emergency. Does not book a slot and does not diagnose.

        Args:
            symptoms: What the caller describes.
            breathing_affected: True if swelling or injury affects breathing or airway.
            uncontrolled_bleeding: True if bleeding will not stop.
            unconscious: True if the person is unresponsive.
        """
        logger.info(
            "emergency breathing=%s bleeding=%s", breathing_affected, unconscious
        )
        if breathing_affected or uncontrolled_bleeding or unconscious:
            return {
                "ok": True,
                "severity": "life_threatening",
                "action": "call_000",
                "say": (
                    "This sounds like an emergency. Hang up and call triple zero, "
                    "000, now. Do not wait for an appointment."
                ),
            }
        return {
            "ok": True,
            "severity": "dental_emergency",
            "action": "urgent_care_or_transfer",
            "say": (
                "Stay calm. Do not diagnose. Check the diary for an urgent slot "
                "or offer to transfer or take a message. Do not invent a time."
            ),
            "symptoms": symptoms,
        }

    @function_tool()
    async def transfer_call(self, context: RunContext) -> dict[str, Any]:
        """Cold-transfer the SIP caller to the practice team. Confirm they want a person first."""
        transfer_to = self.transfer_to or os.getenv("SIP_TRANSFER_TO", "").strip()
        if not transfer_to:
            return {
                "ok": False,
                "reason": "transfer_not_configured",
                "note": "No transfer number is configured. Offer to take a message instead.",
            }

        job_ctx = get_job_context()
        sip_participant = find_sip_participant(job_ctx.room)
        if sip_participant is None:
            return {"ok": False, "reason": "no_sip_caller"}

        await context.session.generate_reply(
            instructions=(
                "Tell the caller you are putting them through to the team now. "
                "Warm and brief."
            )
        )

        destination = transfer_to if ":" in transfer_to else f"tel:{transfer_to}"
        try:
            await job_ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=job_ctx.room.name,
                    participant_identity=sip_participant.identity,
                    transfer_to=destination,
                )
            )
        except Exception:
            logger.exception("transfer_call failed")
            return {"ok": False, "reason": "transfer_failed"}

        logger.info("transferred call to %s", destination)
        return {"ok": True, "confirmed": True, "transfer_to": destination}


def inbound_greeting_instructions(branch_id: str) -> str:
    branch = get_branch(branch_id)
    return (
        "Sound warm, soft, and human, like a real receptionist picking up — not a script. "
        "Greet the caller as Ava for the Shellharbour Dentists group. Mention that you "
        "cover Barrack Heights, Dapto, and Woonona. Do not lock them to one clinic first. "
        f"They may have dialled {branch.trading_name} — treat that as a hint only. "
        "One warm short sentence plus one question about how you can help. Do not say "
        "byte voice or G'day."
    )
