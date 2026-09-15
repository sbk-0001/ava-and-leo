"""Ava — OpenAI Realtime phone receptionist. Flow lives in CallState + tools."""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Mapping
from typing import Any

from livekit import api
from livekit.agents import Agent, RunContext, function_tool, get_job_context
from livekit.plugins import openai
from openai.types.beta.realtime.session import TurnDetection

from booking import BookingProvider
from call_log import CallLog
from call_state import CallState
from filler_ladder import FillerLadder, SessionSpeaker
from persona import ava_instructions, get_branch, quote_fee, resolve_tool_branch
from phrase_pools import STAGE_1
from realtime_hygiene import maybe_trim_realtime_context
from sip_utils import find_sip_participant

logger = logging.getLogger("ava")

AVA_REALTIME_MODEL = "gpt-realtime"
AVA_DEFAULT_VOICE = "marin"
# Server VAD 450-550ms silence. Instant barge-in mid-word.
# Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection
AVA_VAD_SILENCE_MS = 500
AVA_SPEECH_SPEED = 0.9
AVA_TEMPERATURE = 0.95


def resolve_ava_voice(env: Mapping[str, str] | None = None) -> str:
    """Realtime voice. AVA_REALTIME_VOICE wins; LEO_REALTIME_VOICE is a legacy alias."""
    environ = env if env is not None else os.environ
    voice = str(
        environ.get("AVA_REALTIME_VOICE") or environ.get("LEO_REALTIME_VOICE") or ""
    ).strip()
    return voice or AVA_DEFAULT_VOICE


def ava_realtime_model() -> openai.realtime.RealtimeModel:
    """OpenAI Realtime speech-to-speech. Server VAD barge-in, slower speech.

    Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection
          https://docs.livekit.io/agents/logic/turns/#interruption-in-realtime-mode
    """
    kwargs: dict[str, Any] = {
        "model": AVA_REALTIME_MODEL,
        "voice": resolve_ava_voice(),
        "turn_detection": TurnDetection(
            type="server_vad",
            threshold=0.5,
            prefix_padding_ms=300,
            silence_duration_ms=AVA_VAD_SILENCE_MS,
            create_response=True,
            interrupt_response=True,
        ),
    }
    signature = inspect.signature(openai.realtime.RealtimeModel.__init__)
    if "speed" in signature.parameters:
        kwargs["speed"] = AVA_SPEECH_SPEED
    if "temperature" in signature.parameters:
        kwargs["temperature"] = AVA_TEMPERATURE
    return openai.realtime.RealtimeModel(**kwargs)


def transfer_destination_for_branch(
    branch_id: str,
    *,
    env: Mapping[str, str] | None = None,
    fallback: str | None = None,
) -> str | None:
    environ = env if env is not None else os.environ
    key = f"SIP_TRANSFER_{branch_id.upper()}"
    specific = str(environ.get(key, "")).strip()
    if specific:
        return specific
    mapped = str(environ.get("SIP_TRANSFER_MAP", "")).strip()
    if mapped:
        from sip_utils import parse_sip_did_map

        inverted: dict[str, str] = {}
        for did, bid in parse_sip_did_map(mapped).items():
            inverted.setdefault(bid, did)
        if branch_id in inverted:
            return inverted[branch_id]
    if fallback:
        return fallback
    return str(environ.get("SIP_TRANSFER_TO", "")).strip() or None


class AvaReceptionist(Agent):
    """Australian-English phone receptionist. Branch is already on CallState."""

    def __init__(
        self,
        *,
        state: CallState,
        booking: BookingProvider,
        transfer_to: str | None = None,
        call_log: CallLog | None = None,
        ambient: Any | None = None,
    ) -> None:
        self.state = state
        self.booking = booking
        self.transfer_to = transfer_to
        self.call_log = call_log
        self.ambient = ambient
        self._active_ladder: FillerLadder | None = None
        self.branch = get_branch(state.branch)
        super().__init__(
            instructions=ava_instructions(self.branch.id, state.prompt_block()),
            llm=ava_realtime_model(),
        )

    def _select_branch(self, branch_id: str | None) -> str:
        selected = resolve_tool_branch(branch_id, self.state.branch)
        if selected != self.branch.id:
            self.branch = get_branch(selected)
        return selected

    def _log_tool(self, name: str, payload: dict[str, Any]) -> None:
        logger.info("tool %s %s", name, {k: payload.get(k) for k in list(payload)[:8]})
        if self.call_log is not None:
            self.call_log.add_turn(
                role="tool",
                content=str(payload)[:2000],
                tool_name=name,
                tool_payload=payload,
            )

    async def _cover(self, context: RunContext) -> None:
        """Stage-1 filler only. Prefer _dispatch_with_ladder so audio precedes the network."""
        try:
            speaker = SessionSpeaker(context.session)
            line = self.state.pick_phrase("stage_1", STAGE_1)
            await speaker.utter(line)
        except Exception:
            logger.exception("filler speech failed; continuing tool")

    async def _dispatch_with_ladder(
        self,
        context: RunContext,
        factory: Any,
    ) -> dict[str, Any]:
        """Speak stage-1 audio, then run the tool. Ladder covers the wait."""
        speaker = SessionSpeaker(context.session)
        ladder = FillerLadder(self.state, speaker=speaker, booking=self.booking)
        self._active_ladder = ladder
        if self.ambient is not None:
            try:
                self.ambient.play_keyboard()
            except Exception:
                logger.exception("keyboard clatter failed")
        try:
            result, trace = await ladder.dispatch(factory)
            logger.info(
                "first-audio-ts=%s tool-dispatch-ts=%s audio_before_dispatch=%s",
                trace.first_audio_ts,
                trace.tool_dispatch_ts,
                trace.audio_before_dispatch,
            )
            return dict(result)
        finally:
            self._active_ladder = None
            if self.ambient is not None:
                try:
                    self.ambient.stop_keyboard()
                except Exception:
                    logger.exception("keyboard stop failed")

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        self.state.turn_count += 1
        text = (getattr(new_message, "text_content", None) or "").strip()
        self.state.observe_user_text(text)
        if self.call_log is not None and text:
            self.call_log.add_turn(role="user", content=text)
        if self._active_ladder is not None:
            await self._active_ladder.on_caller_speech()
        try:
            await self.update_instructions(
                ava_instructions(self.state.branch, self.state.prompt_block())
            )
        except Exception:
            logger.exception("failed to refresh CallState instructions")
        try:
            await maybe_trim_realtime_context(self)
        except Exception:
            logger.exception("realtime context trim failed; CallState still intact")

    async def _do_transfer(self, branch_id: str, reason: str) -> dict[str, Any]:
        transfer_to = transfer_destination_for_branch(
            branch_id, fallback=self.transfer_to
        )
        if not transfer_to:
            return {
                "ok": False,
                "reason": "transfer_not_configured",
                "note": (
                    "No transfer number is configured. Offer to take a message instead."
                ),
            }

        job_ctx = get_job_context()
        sip_participant = find_sip_participant(job_ctx.room)
        if sip_participant is None:
            return {
                "ok": False,
                "reason": "no_sip_caller",
                "note": (
                    "Not a SIP caller. Say you're putting them through, then end "
                    "the web session if this is a demo kill-switch."
                ),
                "transfer_to": transfer_to,
            }

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
            logger.exception("transfer_to_human failed")
            return {"ok": False, "reason": "transfer_failed"}

        logger.info("transferred call to %s reason=%s", destination, reason)
        return {
            "ok": True,
            "confirmed": True,
            "transfer_to": destination,
            "reason": reason,
        }

    async def on_enter(self) -> None:
        if not self.state.kill_switch:
            return
        self.state.escalation_flag = True
        self.state.intent = "kill_switch"
        try:
            await self.session.generate_reply(
                instructions=(
                    f"Kill-switch is on. Warm one sentence: you're putting them "
                    f"through to the team at {self.state.branch_name} now. Then stop."
                )
            )
        except Exception:
            logger.exception("kill-switch greeting failed")
        result = await self._do_transfer(self.state.branch, "kill_switch")
        if not result.get("ok"):
            try:
                await self.session.generate_reply(
                    instructions=(
                        "The transfer didn't go through. Stay warm. Offer to take "
                        "a message and a callback. Do not mention kill-switch."
                    )
                )
            except Exception:
                logger.exception("kill-switch fallback speech failed")

    @function_tool()
    async def check_availability(
        self,
        context: RunContext,
        appointment_type: str,
        date_range: str,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Check real diary availability. Never invent times.

        Args:
            branch: shellharbour, dapto, or woonona. Default is the caller's branch.
            appointment_type: check-up, emergency, existing, whitening, etc.
            date_range: YYYY-MM-DD, YYYY-MM-DD/YYYY-MM-DD, today, tomorrow, or this week.
        """

        async def _run() -> dict[str, Any]:
            clinic_id = self._select_branch(branch)
            if not self.state.may_book():
                return {
                    "ok": False,
                    "reason": "do_not_book",
                    "action": "call_000",
                    "note": (
                        "Life-threatening presentation. Do not book. Tell them to call "
                        "triple zero or go to Shellharbour or Wollongong Hospital emergency."
                    ),
                }
            self.state.appointment_type = appointment_type
            result = await self.booking.check_availability(
                branch=clinic_id,
                appointment_type=appointment_type,
                date_range=date_range,
            )
            if result.get("ok") and result.get("slots"):
                first = result["slots"][0]
                self.state.proposed_slot = first.get("slot_id")
            return result

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("check_availability", result)
        return result

    @function_tool()
    async def book_appointment(
        self,
        context: RunContext,
        slot_id: str,
        reason: str,
        branch: str | None = None,
        name: str | None = None,
        mobile: str | None = None,
        patient_id: str | None = None,
        date_of_birth: str | None = None,
    ) -> dict[str, Any]:
        """Book a slot returned by check_availability. Say confirmed only if confirmed is true.

        Args:
            branch: Clinic id: shellharbour, dapto, or woonona.
            slot_id: Slot id from check_availability.
            reason: Short reason for the visit.
            name: Caller's name for a new patient.
            mobile: Australian mobile.
            patient_id: Patient id from lookup_patient, if known.
            date_of_birth: Date of birth if given, preferably YYYY-MM-DD.
        """

        async def _run() -> dict[str, Any]:
            if not self.state.may_book():
                return {
                    "ok": False,
                    "reason": "do_not_book",
                    "action": "call_000",
                    "note": "Do not book this caller. Escalate. Triple zero if needed.",
                }
            if name:
                self.state.caller_name = name
            mobile_result = self.state.register_mobile(
                mobile or self.state.caller_mobile
            )
            if not mobile_result.get("ok") and not patient_id:
                return mobile_result
            clinic_id = self._select_branch(branch)
            booked = await self.booking.book_appointment(
                branch=clinic_id,
                slot_id=slot_id,
                reason=reason,
                patient_id=patient_id,
                name=name or self.state.caller_name,
                mobile=self.state.caller_mobile,
                date_of_birth=date_of_birth,
            )
            if booked.get("confirmed"):
                self.state.confirmed_slot = slot_id
                self.state.intent = "booked"
            return booked

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("book_appointment", result)
        return result

    @function_tool()
    async def reschedule_appointment(
        self,
        context: RunContext,
        booking_id: str,
        new_slot_id: str,
    ) -> dict[str, Any]:
        """Move an existing booking to a new slot from check_availability.

        Args:
            booking_id: Existing booking id.
            new_slot_id: New slot id from check_availability.
        """

        async def _run() -> dict[str, Any]:
            self.state.intent = "reschedule"
            moved = await self.booking.reschedule_appointment(
                booking_id=booking_id, new_slot_id=new_slot_id
            )
            if moved.get("confirmed"):
                self.state.confirmed_slot = new_slot_id
            return moved

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("reschedule_appointment", result)
        return result

    @function_tool()
    async def cancel_appointment(
        self,
        context: RunContext,
        booking_id: str,
    ) -> dict[str, Any]:
        """Cancel an existing booking. Returns whether the $50 policy applies.

        Args:
            booking_id: Existing booking id.
        """

        async def _run() -> dict[str, Any]:
            self.state.intent = "cancel"
            cancelled = await self.booking.cancel_appointment(booking_id=booking_id)
            if cancelled.get("fee_applies"):
                cancelled["say"] = (
                    "There is a fifty dollar fee for inside twenty-four hours, "
                    "just so you're not surprised by it. You cannot waive it."
                )
            return cancelled

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("cancel_appointment", result)
        return result

    @function_tool()
    async def lookup_patient(self, context: RunContext, mobile: str) -> dict[str, Any]:
        """Look up a patient by mobile. Never invent a record.

        Args:
            mobile: Australian mobile number as spoken.
        """

        async def _run() -> dict[str, Any]:
            mobile_result = self.state.register_mobile(mobile)
            if not mobile_result.get("ok"):
                return mobile_result
            looked = await self.booking.lookup_patient(
                mobile=self.state.caller_mobile or mobile
            )
            if looked.get("is_existing_patient"):
                self.state.is_existing_patient = True
                patients = looked.get("patients") or []
                if patients and not self.state.caller_name:
                    self.state.caller_name = patients[0].get("name")
            elif looked.get("ok"):
                self.state.is_existing_patient = False
            return looked

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("lookup_patient", result)
        return result

    @function_tool()
    async def quote_fee(self, context: RunContext, service: str) -> dict[str, Any]:
        """Quote only the published fee table. Unknown means unknown — offer a callback.

        Args:
            service: Treatment or item the caller asked about.
        """

        async def _run() -> dict[str, Any]:
            quoted = quote_fee(service, self.state.branch)
            if quoted.get("status") == "unknown" or not quoted.get("ok"):
                quoted["note"] = (
                    "Fee unknown. Do not guess. Offer to have the team call back."
                )
            return quoted

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("quote_fee", result)
        return result

    @function_tool()
    async def take_message(
        self,
        context: RunContext,
        name: str,
        mobile: str,
        reason: str,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Leave a message for the practice team.

        Args:
            branch: Clinic id.
            name: Caller's name.
            mobile: Call-back number.
            reason: Why they rang.
        """

        async def _run() -> dict[str, Any]:
            if name:
                self.state.caller_name = name
            mobile_result = self.state.register_mobile(mobile)
            stored_mobile = self.state.caller_mobile or mobile or ""
            if mobile_result.get("stop_asking") and not self.state.caller_mobile:
                stored_mobile = mobile or ""
            clinic_id = self._select_branch(branch)
            left = await self.booking.take_message(
                branch=clinic_id,
                name=name,
                mobile=stored_mobile,
                reason=reason,
            )
            self.state.intent = "message"
            return left

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("take_message", result)
        return result

    @function_tool()
    async def transfer_to_human(
        self,
        context: RunContext,
        reason: str,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Cold-transfer the SIP caller to the practice team. Genuinely wired.

        Args:
            branch: Clinic to put them through to.
            reason: Why they need a person.
        """
        self.state.escalation_flag = True
        self.state.intent = "transfer"
        clinic_id = self._select_branch(branch)
        dest_branch = get_branch(clinic_id)
        try:
            await context.session.generate_reply(
                instructions=(
                    f"Tell the caller you are putting them through to the team at "
                    f"{dest_branch.trading_name} now. Warm and brief. Never a surprise transfer."
                )
            )
        except Exception:
            logger.exception("transfer cover speech failed")
        result = await self._do_transfer(clinic_id, reason)
        self._log_tool("transfer_to_human", result)
        return result

    @function_tool()
    async def end_call(self, context: RunContext, reason: str) -> dict[str, Any]:
        """End the call after a warm goodbye. Genuinely wired — deletes the room.

        Args:
            reason: Why the call is ending, e.g. done, goodbye, kill_switch.
        """
        self.state.intent = "end"
        try:
            await context.session.generate_reply(
                instructions=(
                    "Thank them briefly in warm Australian English and say goodbye. "
                    "One short sentence. Sound human, not scripted."
                )
            )
        except Exception:
            logger.exception("end_call goodbye failed")
        job_ctx = get_job_context()
        try:
            await job_ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=job_ctx.room.name)
            )
            ended = True
            end_reason = "deleted_room"
        except Exception:
            logger.exception("end_call delete_room failed; shutting down job")
            try:
                job_ctx.shutdown(reason=f"end_call:{reason}")
                ended = True
                end_reason = "shutdown"
            except Exception:
                logger.exception("end_call shutdown failed")
                ended = False
                end_reason = "end_failed"
        result = {"ok": ended, "ended": ended, "reason": reason, "how": end_reason}
        self._log_tool("end_call", result)
        return result


def inbound_greeting_instructions(branch_id: str) -> str:
    branch = get_branch(branch_id)
    name = branch.trading_name
    return (
        "Sound warm and human, like a real receptionist picking up — not a script. "
        f"Answer as {name}. They rang this branch; you already know. "
        "Never ask which clinic they want. Never greet as a group menu. "
        "Use one of your opening lines with this branch name, for example: "
        f'"Morning, {name}, Ava speaking!" '
        f'or "{name}, this is Ava — how ya going?" '
        f'or "{name}, Ava — what can I do for ya?" '
        "One warm short sentence, then stop and listen. Do not say G'day."
    )
