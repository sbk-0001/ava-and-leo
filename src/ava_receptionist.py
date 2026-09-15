"""Ava — OpenAI Realtime phone receptionist. Flow lives in CallState + tools."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from typing import Any

from livekit import api
from livekit.agents import (
    Agent,
    ModelSettings,
    RunContext,
    function_tool,
    get_job_context,
)
from livekit.plugins import openai
from openai.types.beta.realtime.session import TurnDetection

from booking import (
    BookingProvider,
    empty_tool_args_result,
    invalid_slot_id_result,
    is_canonical_slot_id,
)
from call_log import CallLog
from call_state import CallState
from caller_store import CallerStore, get_shared_caller_store, upsert_from_booking
from date_context import resolve_date_phrase as resolve_date_phrase_fn
from dead_air import DeadAirMonitor
from desk_events import activity_packet_from_result, schedule_desk_publish
from filler_ladder import FillerLadder, SessionSpeaker, speak_scripted
from filler_player import FillerPlayer
from grounding import (
    grounded_realtime_transcription,
    grounding_corrective_note,
    grounding_violation_packet,
)
from persona import ava_instructions, get_branch, quote_fee, resolve_tool_branch
from phrase_pools import RECOVERY, STAGE_1
from realtime_hygiene import maybe_trim_realtime_context
from sip_utils import find_sip_participant

try:
    from livekit.agents import StopResponse
except ImportError:  # pragma: no cover
    StopResponse = None  # type: ignore[misc, assignment]

logger = logging.getLogger("ava")

AVA_REALTIME_MODEL = "gpt-realtime"
AVA_DEFAULT_VOICE = "marin"
# Server VAD 450-550ms silence. Instant barge-in mid-word.
# Docs: https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection
AVA_VAD_SILENCE_MS = 500
AVA_SPEECH_SPEED = 0.9
AVA_TEMPERATURE = 0.95

EMERGENCY_000_SCRIPT = (
    "This sounds like it needs emergency care. Please hang up and call triple zero, "
    "or get straight to Shellharbour or Wollongong Hospital emergency. "
    "I can't book this one."
)

DeskNotify = Callable[[dict[str, Any]], Awaitable[None] | None]


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
        on_desk_event: DeskNotify | None = None,
        caller_store: CallerStore | None = None,
        filler_player: FillerPlayer | None = None,
        dead_air: DeadAirMonitor | None = None,
    ) -> None:
        self.state = state
        self.booking = booking
        self.transfer_to = transfer_to
        self.call_log = call_log
        self.ambient = ambient
        self.on_desk_event = on_desk_event
        self.caller_store = (
            caller_store if caller_store is not None else get_shared_caller_store()
        )
        self._desk_tasks: set[asyncio.Task[Any]] = set()
        self._speech_tasks: set[asyncio.Task[Any]] = set()
        self._active_ladder: FillerLadder | None = None
        self._scripted_speech = False
        self._speech_session: Any | None = None
        self.filler_player = filler_player or FillerPlayer(ambient=ambient)
        self.dead_air = dead_air or DeadAirMonitor(branch=state.branch)
        self.filler_player.on_audio = self.dead_air.note_audio
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

    def _job_room(self) -> Any:
        try:
            return get_job_context().room
        except Exception:
            return None

    def _notify_desk(
        self, action: str, arguments: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Push booking activity as soon as the practice tool returns.

        Works for web Call Ava and inbound SIP. HTTP bus reaches the desk
        when the browser is not in the LiveKit room.
        """
        packet = activity_packet_from_result(action, arguments, result)
        if packet is None:
            return
        if self.on_desk_event is not None:
            maybe = self.on_desk_event(packet)
            if inspect.isawaitable(maybe):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return
                task = loop.create_task(maybe)
                self._desk_tasks.add(task)
                task.add_done_callback(self._desk_tasks.discard)
            return
        schedule_desk_publish(self._job_room(), packet)

    def _log_tool(
        self,
        name: str,
        payload: dict[str, Any],
        arguments: dict[str, Any] | None = None,
    ) -> None:
        logger.info("tool %s %s", name, {k: payload.get(k) for k in list(payload)[:8]})
        if self.call_log is not None:
            self.call_log.add_turn(
                role="tool",
                content=str(payload)[:2000],
                tool_name=name,
                tool_payload=payload,
            )
        self._notify_desk(name, arguments or {}, payload)

    def _gate_speech(self, text: str) -> str:
        gated = self.state.gate_speech(text)
        if gated.suppressed:
            logger.warning("%s", gated.log_line)
            packet = grounding_violation_packet(
                gated,
                count=self.state.grounding_violations,
                branch=self.state.branch,
            )
            if self.on_desk_event is not None:
                maybe = self.on_desk_event(packet)
                if inspect.isawaitable(maybe):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        return gated.spoken
                    task = loop.create_task(maybe)
                    self._desk_tasks.add(task)
                    task.add_done_callback(self._desk_tasks.discard)
            else:
                schedule_desk_publish(self._job_room(), packet)
            self._kick_recovery(gated.original, gated.violations)
        return gated.spoken

    def _voice_session(self) -> Any:
        if self._speech_session is not None:
            return self._speech_session
        return self.session

    def _on_ungrounded_rewrite(self, spoken: str) -> None:
        session = self._voice_session()
        interrupt = getattr(session, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:
                logger.exception("grounding interrupt failed")
        # Recovery is kicked from _gate_speech via commit(); captions use spoken.

    def _kick_recovery(self, original: str, kinds: list[str]) -> None:
        """Play a bank recovery clip. Never generate_reply a substitute sentence."""
        if self._scripted_speech:
            return
        self._scripted_speech = True
        line = self.state.pick_phrase("recovery", RECOVERY)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._scripted_speech = False
            return

        async def _run() -> None:
            try:
                self.filler_player.session = self._voice_session()
                await self.filler_player.play(line)
                note = grounding_corrective_note(original, kinds)
                self.state.pending_grounding_note = note
                try:
                    await self.update_instructions(
                        ava_instructions(
                            self.state.branch,
                            self.state.prompt_block() + "\n" + note,
                        )
                    )
                except Exception:
                    logger.exception("grounding corrective note failed")
            except Exception:
                logger.exception("grounding recovery clip failed")
            finally:
                self._scripted_speech = False

        task = loop.create_task(_run())
        self._speech_tasks.add(task)
        task.add_done_callback(self._speech_tasks.discard)

    def _kick_substitute_speech(self, spoken: str) -> None:
        self._kick_recovery(spoken, ["confirm"])

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[Any]:
        """Pipeline pre-TTS intercept. Docs: https://docs.livekit.io/agents/logic/nodes/"""

        async def _gated() -> AsyncIterable[str]:
            chunks: list[str] = []
            async for chunk in text:
                chunks.append(chunk if isinstance(chunk, str) else str(chunk))
            yield self._gate_speech("".join(chunks))

        async for frame in Agent.default.tts_node(self, _gated(), model_settings):
            yield frame

    async def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        """Rewrite Realtime captions; interrupt ungrounded audio; play bank recovery.

        Yielding the recovery caption updates the published transcript. OpenAI
        Realtime audio is S2S — do not generate_reply a substitute sentence.

        Docs: https://docs.livekit.io/agents/logic/nodes/
              https://docs.livekit.io/agents/multimodality/audio/background-audio.md
        """
        del model_settings
        if self._scripted_speech:
            async for chunk in text:
                yield chunk if isinstance(chunk, str) else str(chunk)
            return
        async for chunk in grounded_realtime_transcription(
            text,
            facts=self.state.speakable,
            commit=self._gate_speech,
            on_rewrite=self._on_ungrounded_rewrite,
        ):
            yield chunk

    async def _cover(self, context: RunContext) -> None:
        """Stage-1 filler only. Prefer _dispatch_with_ladder so audio precedes the network."""
        try:
            speaker = SessionSpeaker(context.session, player=self.filler_player)
            line = self.state.pick_phrase("stage_1", STAGE_1)
            await speaker.utter(line)
        except Exception:
            logger.exception("filler speech failed; continuing tool")

    async def _dispatch_with_ladder(
        self,
        context: RunContext,
        factory: Any,
        *,
        in_flight: str = "tool",
    ) -> dict[str, Any]:
        """Speak stage-1 audio, then run the tool. Ladder covers the wait."""
        self.filler_player.session = context.session
        self.filler_player.ambient = self.ambient
        speaker = SessionSpeaker(context.session, player=self.filler_player)
        ladder = FillerLadder(self.state, speaker=speaker, booking=self.booking)
        self._active_ladder = ladder
        self.dead_air.room = self._job_room()
        self.dead_air.set_in_flight(in_flight, pending=True)

        async def _watch_dead_air() -> None:
            while True:
                await asyncio.sleep(0.1)
                self.dead_air.check()

        watcher = asyncio.create_task(_watch_dead_air(), name="ava-dead-air")
        if self.ambient is not None:
            try:
                self.ambient.play_keyboard()
            except Exception:
                logger.exception("keyboard clatter failed")
        try:
            result, trace = await ladder.dispatch(factory)
            self.dead_air.check()
            logger.info(
                "first-audio-ts=%s tool-dispatch-ts=%s audio_before_dispatch=%s",
                trace.first_audio_ts,
                trace.tool_dispatch_ts,
                trace.audio_before_dispatch,
            )
            return dict(result)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
            self._active_ladder = None
            self.dead_air.set_in_flight(None, pending=False)
            if self.ambient is not None:
                try:
                    self.ambient.stop_keyboard()
                except Exception:
                    logger.exception("keyboard stop failed")

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        self.state.turn_count += 1
        self.state.refresh_dates()
        text = (getattr(new_message, "text_content", None) or "").strip()
        self.state.observe_user_text(text)
        if self.call_log is not None and text:
            self.call_log.add_turn(role="user", content=text)
        if self._active_ladder is not None:
            await self._active_ladder.on_caller_speech()
        if self.state.urgency_level == "emergency_000":
            try:
                await speak_scripted(
                    self._voice_session(),
                    EMERGENCY_000_SCRIPT,
                    allow_interruptions=False,
                    kind="script",
                )
            except Exception:
                logger.exception("emergency 000 script failed")
            if StopResponse is not None:
                raise StopResponse()
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
        if self.state.kill_switch:
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
            return
        if not self.state.greet_on_enter:
            return
        try:
            await self.session.generate_reply(
                instructions=inbound_greeting_instructions(
                    self.state.branch, state=self.state
                )
            )
        except Exception:
            logger.exception("session-start greeting failed")

    @function_tool()
    async def resolve_date_phrase(
        self, context: RunContext, phrase: str
    ) -> dict[str, Any]:
        """Resolve a spoken date in Australia/Sydney. Never do date maths yourself.

        Call this before check_availability when they name a day. If it comes
        back ambiguous, ask them which day they mean.

        Args:
            phrase: What they said — next Tuesday, this week, tomorrow, 2026-09-22.
        """
        del context
        if not (phrase or "").strip():
            result = empty_tool_args_result("phrase")
            self._log_tool("resolve_date_phrase", result, {"phrase": phrase})
            return result
        self.state.refresh_dates()
        result = resolve_date_phrase_fn(phrase, today=self.state.today)
        self.state.apply_date_resolution(result)
        self._log_tool("resolve_date_phrase", result, {"phrase": phrase})
        return result

    @function_tool()
    async def check_availability(
        self,
        context: RunContext,
        appointment_type: str,
        date_range: str,
        branch: str | None = None,
        clinician: str | None = None,
    ) -> dict[str, Any]:
        """Check real diary availability before offering times. Never invent times.

        Must check before offering any time. Book only an exact slot_id from slots.

        Args:
            branch: shellharbour, dapto, or woonona. Default is the caller's branch.
            appointment_type: check-up, emergency, existing, whitening, etc.
            date_range: Prefer phrases the diary understands — next week, next tuesday
                / next <weekday>, this week, today, tomorrow — or an explicit ISO
                range YYYY-MM-DD or YYYY-MM-DD/YYYY-MM-DD. Unknown strings fall
                back to this week, so pass the caller's phrase or ISO dates.
            clinician: Optional preferred dentist (e.g. Dr Mohit). Filters slots
                that name a clinician. Empty if none match — do not invent a time.
        """

        async def _run() -> dict[str, Any]:
            if not (appointment_type or "").strip() or not (date_range or "").strip():
                return empty_tool_args_result("appointment_type", "date_range")
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
            chosen = clinician or self.state.preferred_clinician
            if clinician:
                self.state.preferred_clinician = clinician
            result = await self.booking.check_availability(
                branch=clinic_id,
                appointment_type=appointment_type,
                date_range=date_range,
                clinician=chosen,
            )
            self.state.remember_availability(result)
            if str(result.get("status") or "") == "UNKNOWN":
                result = dict(result)
                result.setdefault(
                    "note",
                    (
                        "Diary status is UNKNOWN. Do not say chockers or packed. "
                        "Do not invent a time."
                    ),
                )
            elif result.get("ok") and not result.get("slots"):
                result = dict(result)
                if result.get("may_say_chockers"):
                    result.setdefault(
                        "note",
                        (
                            "No diary slots in this range (OK). You may say chockers. "
                            "Do not invent a time."
                        ),
                    )
                else:
                    result.setdefault(
                        "note",
                        (
                            "No diary slots to offer. Do not invent a time. "
                            "Do not say half past two or any clock time."
                        ),
                    )
            return result

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool(
            "check_availability",
            result,
            {
                "appointment_type": appointment_type,
                "date_range": date_range,
                "branch": branch,
                "clinician": clinician or self.state.preferred_clinician,
            },
        )
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
        """Book only an exact slot_id from check_availability. Never invent ids.

        Call check_availability first. Say confirmed / you're all set only if
        the result has ok true and confirmed true. Otherwise say it is not locked.

        Args:
            branch: Clinic id: shellharbour, dapto, or woonona.
            slot_id: Exact slot_id from the slots list. Never reconstruct one.
            reason: Short reason for the visit.
            name: Caller's name for a new patient.
            mobile: Australian mobile.
            patient_id: Patient id from lookup_patient, if known.
            date_of_birth: Date of birth if given, preferably YYYY-MM-DD.
        """

        async def _run() -> dict[str, Any]:
            if not (slot_id or "").strip() or not (reason or "").strip():
                return empty_tool_args_result("slot_id", "reason")
            if not self.state.may_book():
                return {
                    "ok": False,
                    "reason": "do_not_book",
                    "action": "call_000",
                    "note": "Do not book this caller. Escalate. Triple zero if needed.",
                }
            if not is_canonical_slot_id(slot_id):
                return invalid_slot_id_result(slot_id)
            selected = self.state.booking_flow.select_slot(slot_id)
            if not selected.get("ok"):
                return selected
            held = self.state.booking_flow.hold(slot_id)
            if not held.get("ok"):
                return held
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
            booked = dict(booked)
            booked.setdefault("slot_id", slot_id)
            return booked

        result = self.state.record_book_result(
            await self._dispatch_with_ladder(
                context, _run, in_flight="book_appointment"
            )
        )
        if result.get("ok") and result.get("confirmed"):
            try:
                upsert_from_booking(self.caller_store, self.state, result)
            except Exception:
                logger.exception("caller store write failed")
        self._log_tool(
            "book_appointment",
            result,
            {
                "slot_id": slot_id,
                "reason": reason,
                "branch": branch,
                "name": name,
                "mobile": mobile,
                "patient_id": patient_id,
            },
        )
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
            gated = self.state.require_dob_for_existing()
            if not gated.get("ok"):
                return gated
            moved = await self.booking.reschedule_appointment(
                booking_id=booking_id, new_slot_id=new_slot_id
            )
            if moved.get("confirmed"):
                self.state.confirmed_slot = new_slot_id
            return moved

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool(
            "reschedule_appointment",
            result,
            {"booking_id": booking_id, "new_slot_id": new_slot_id},
        )
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
            gated = self.state.require_dob_for_existing()
            if not gated.get("ok"):
                return gated
            cancelled = await self.booking.cancel_appointment(booking_id=booking_id)
            if cancelled.get("fee_applies"):
                cancelled["say"] = (
                    "There is a fifty dollar fee for inside twenty-four hours, "
                    "just so you're not surprised by it. You cannot waive it."
                )
            return cancelled

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("cancel_appointment", result, {"booking_id": booking_id})
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
            self.state.pms_record = looked if isinstance(looked, dict) else None
            if looked.get("is_existing_patient"):
                patients = looked.get("patients") or []
                if patients and not self.state.caller_name:
                    self.state.caller_name = patients[0].get("name")
            public = dict(looked)
            if not self.state.dob_verified:
                public["is_existing_patient"] = None
                public["patients"] = []
                public["bookings"] = []
                public["identity_verified"] = False
                public["note"] = (
                    "Do not confirm or deny that they are a patient. "
                    "Do not mention existing appointments. "
                    "A new booking does not need date of birth."
                )
            elif looked.get("is_existing_patient"):
                self.state.is_existing_patient = True
            elif looked.get("ok"):
                self.state.is_existing_patient = False
            return public

        result = await self._dispatch_with_ladder(context, _run)
        self._log_tool("lookup_patient", result, {"mobile": mobile})
        return result

    @function_tool()
    async def ask_for_field(self, context: RunContext, field: str) -> dict[str, Any]:
        """Ask the caller for a field only if it is not already known.

        Hard-rejects a re-ask. If the field is populated, returns
        already known: <value> and you must not ask again.

        Args:
            field: mobile, name, or date_of_birth.
        """
        del context
        result = self.state.ask_for(field)
        self._log_tool("ask_for_field", result, {"field": field})
        return result

    @function_tool()
    async def verify_date_of_birth(
        self, context: RunContext, date_of_birth: str
    ) -> dict[str, Any]:
        """Verify DOB before discussing, moving, or cancelling an existing appointment.

        New bookings do not need this. Failed verification: offer a callback.
        Do not say the date of birth was wrong. Do not confirm or deny a record.

        Args:
            date_of_birth: Date of birth as spoken, preferably YYYY-MM-DD.
        """
        del context
        result = self.state.verify_dob(date_of_birth)
        self._log_tool("verify_date_of_birth", result, {"date_of_birth": "given"})
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
        self._log_tool(
            "take_message",
            result,
            {"name": name, "mobile": mobile, "reason": reason, "branch": branch},
        )
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
        blocked = self.state.booking_flow.end_call_guard()
        if not blocked.get("ok"):
            self._log_tool("end_call", blocked, {"reason": reason})
            try:
                await context.session.generate_reply(
                    instructions=(
                        "Do not hang up. The booking is not locked yet. "
                        "Say you'll finish it or offer another time. Warm, one sentence."
                    )
                )
            except Exception:
                logger.exception("end_call blocked speech failed")
            return blocked
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


def inbound_greeting_instructions(
    branch_id: str, *, state: CallState | None = None
) -> str:
    branch = get_branch(branch_id)
    name = branch.trading_name
    if state is not None and state.known_caller and state.caller_first_name:
        first = state.caller_first_name
        return (
            f"Known caller. Sound warm. Answer as {name}. "
            f"Greet {first} by first name. Do not ask for their number. "
            "You may light-confirm: 'Is this still the best number for ya?' "
            "Do not mention existing appointments, dentist, or treatment "
            "until date of birth is verified on this call. "
            "Never confirm or deny that they are a patient. "
            "One warm short sentence, then stop and listen. Do not say G'day."
        )
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
