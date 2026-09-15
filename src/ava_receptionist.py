"""Ava — Illawarra Dentists OpenAI Realtime receptionist."""

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
    GROUP_NAME,
    ava_instructions,
    get_branch,
    quote_fee,
    resolve_tool_branch,
)
from practice import PracticeClient
from sip_utils import find_sip_participant

logger = logging.getLogger("ava")

AVA_REALTIME_MODEL = "gpt-realtime"
# OpenAI Realtime has no AU-specific voice. marin is the recommended feminine
# quality voice; cedar is more masculine.
# Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/
AVA_DEFAULT_VOICE = "marin"


def resolve_ava_voice(env: Mapping[str, str] | None = None) -> str:
    """Realtime voice. AVA_REALTIME_VOICE wins; LEO_REALTIME_VOICE is a legacy alias."""
    environ = env if env is not None else os.environ
    voice = str(
        environ.get("AVA_REALTIME_VOICE") or environ.get("LEO_REALTIME_VOICE") or ""
    ).strip()
    return voice or AVA_DEFAULT_VOICE


def ava_realtime_model() -> openai.realtime.RealtimeModel:
    """OpenAI Realtime speech-to-speech model for Ava.

    Semantic VAD (documented default) so turns close when the caller has
    finished speaking, not after a tight silence window. interrupt_response
    stays on so they can barge in. Temperature a little above the 0.8 default
    for more natural variation. Do not claim zero latency.
    Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection
          https://docs.livekit.io/agents/logic/turns/#interruption-in-realtime-mode
    """
    return openai.realtime.RealtimeModel(
        model=AVA_REALTIME_MODEL,
        voice=resolve_ava_voice(),
        temperature=0.9,
        turn_detection=TurnDetection(
            type="semantic_vad",
            eagerness="medium",
            create_response=True,
            interrupt_response=True,
        ),
    )


class AvaReceptionist(Agent):
    """Australian-English phone receptionist for Illawarra Dentists."""

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

    def _select_branch(self, branch_id: str | None) -> str:
        selected = resolve_tool_branch(branch_id, self.branch.id)
        if selected != self.branch.id:
            self.branch = get_branch(selected)
        return self.branch.id

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
    async def get_availability(
        self,
        context: RunContext,
        date: str,
        clinician: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Check diary availability. Only returns real slots; never invent times.

        Args:
            date: Requested date in YYYY-MM-DD.
            clinician: Optional dentist name to filter by.
            branch_id: Clinic to check: shellharbour, dapto, or woonona. Use the
                clinic the caller chose. Defaults to the current clinic.
        """
        clinic_id = self._select_branch(branch_id)
        logger.info(
            "get_availability date=%s clinician=%s branch=%s",
            date,
            clinician,
            clinic_id,
        )
        return await self.practice.get_availability(
            branch_id=clinic_id, date=date, clinician=clinician
        )

    @function_tool()
    async def book_appointment(
        self,
        context: RunContext,
        slot_id: str,
        reason: str,
        patient_id: str | None = None,
        name: str | None = None,
        phone: str | None = None,
        date_of_birth: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Book a diary slot returned by get_availability. Say confirmed only if confirmed is true.

        Args:
            slot_id: Slot id from get_availability.
            reason: Short reason for the visit.
            patient_id: Patient id from find_patient, if known.
            name: Full name for a new patient when no patient_id exists.
            phone: Mobile for a new patient.
            date_of_birth: Date of birth if given, preferably YYYY-MM-DD.
            branch_id: Clinic to book at: shellharbour, dapto, or woonona. Must
                match the slot. Defaults to the current clinic.
        """
        clinic_id = self._select_branch(branch_id)
        logger.info(
            "book_appointment slot=%s patient=%s name=%s branch=%s",
            slot_id,
            patient_id,
            name,
            clinic_id,
        )
        return await self.practice.book_appointment(
            branch_id=clinic_id,
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
    async def quote_fee(self, context: RunContext, item: str) -> dict[str, Any]:
        """Quote a canned fee only. If unverified, do not invent a price.

        Args:
            item: Treatment or item the caller asked about, e.g. check-up, filling, emergency consult.
        """
        logger.info("quote_fee item=%s", item)
        return quote_fee(item, self.branch.id)

    @function_tool()
    async def leave_message(
        self,
        context: RunContext,
        caller_name: str,
        phone: str,
        body: str,
    ) -> dict[str, Any]:
        """Leave a message for the practice team.

        Args:
            caller_name: Caller's name.
            phone: Call-back number.
            body: Message for the team.
        """
        logger.info("leave_message name=%s", caller_name)
        return await self.practice.leave_message(
            branch_id=self.branch.id,
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
    del branch_id  # DID/portal hint is not the inbound brand.
    return (
        "Sound like a real receptionist just picking up — slight natural energy, "
        "not a script or a menu. Greet the caller as Ava at "
        f"{GROUP_NAME}. They reached the Illawarra Dentists group number, not "
        "one clinic. Do not greet as Shellharbour Dentists, Dapto Dentists, or "
        "Woonona Dentists. One warm short sentence plus one question. Do not "
        "list all three clinics in the opening. Offer to help, then ask what "
        "they need or where they are so you can choose Shellharbour, Dapto, or "
        "Woonona conversationally. Do not say G'day. Plain speech only — no "
        "lists, no SSML, no stage directions."
    )
