"""Forever fix: one voice, identity, cancel-by-ANI, DOB readback, junk ignore."""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from booking import MemoryBookingProvider
from call_state import CallState
from caller_store import CallerStore, apply_record_to_state
from filler_bank import get_filler_bank
from filler_player import FillerPlayer
from phrase_pools import STAGE_1
from practice import Booking, PracticeClient
from turn_filter import classify_user_turn

SYDNEY = ZoneInfo("Australia/Sydney")

# Frozen Sydney clock. MemoryBookingProvider defaults now_fn to the live clock,
# so an unpinned provider drops the seeded slots as past once real time moves
# beyond them and these tests fail by calendar date, not by behaviour.
SYDNEY_NOW = datetime(2026, 9, 15, 7, 0, tzinfo=SYDNEY)


class _QuietPlayer:
    last_first_audio_ts = 0.0
    session = None
    ambient = None
    on_audio = None

    def __init__(self) -> None:
        self.played: list[str] = []

    async def play(self, text: str, **_kwargs: object) -> None:
        self.played.append(text)

    def notify_model_audio(self) -> None:
        return None

    def notify_model_audio_ended(self) -> None:
        return None


@pytest.mark.asyncio
async def test_play_must_not_dual_route_when_live_play_returns_true() -> None:
    captures: list[int] = []

    class Handle:
        def stop(self) -> None:
            return None

    class AmbientPlayer:
        def __init__(self) -> None:
            self.plays: list[object] = []

        def play(self, *args: object, **kwargs: object) -> Handle:
            self.plays.append((args, kwargs))
            return Handle()

    class AudioOut:
        def capture_frame(self, *_args: object, **_kwargs: object) -> None:
            captures.append(1)

    ambient = SimpleNamespace(player=AmbientPlayer())
    session = SimpleNamespace(output=SimpleNamespace(audio=AudioOut()))
    player = FillerPlayer(get_filler_bank(), ambient=ambient, session=session)
    await player.play(STAGE_1[0])
    await asyncio.sleep(0.05)
    assert player.route == "live"
    assert player.live_plays == 1
    assert player.mix_plays == 0
    assert player._task is None
    assert captures == []
    assert len(ambient.player.plays) == 1
    player.notify_model_audio()
    assert player._live_handle is None
    await player.play(STAGE_1[1] if len(STAGE_1) > 1 else STAGE_1[0])
    assert player.route == "skipped_model_speaking"


@pytest.mark.asyncio
async def test_play_mix_only_when_live_play_fails() -> None:
    captures: list[int] = []

    class AmbientPlayer:
        def play(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("BackgroundAudio is not started")

    class AudioOut:
        def capture_frame(self, *_args: object, **_kwargs: object) -> None:
            captures.append(1)

    ambient = SimpleNamespace(player=AmbientPlayer())
    session = SimpleNamespace(output=SimpleNamespace(audio=AudioOut()))
    player = FillerPlayer(get_filler_bank(), ambient=ambient, session=session)
    await player.play(STAGE_1[0])
    await asyncio.sleep(0.05)
    assert player.route == "mix"
    assert player.live_plays == 0
    assert player.mix_plays == 1
    assert player._live_handle is None


@pytest.mark.asyncio
async def test_play_skips_when_model_is_speaking() -> None:
    player = FillerPlayer(get_filler_bank())
    player.notify_model_audio()
    await player.play(STAGE_1[0])
    assert player.route == "skipped_model_speaking"
    assert player.played == []
    assert player.skipped == [STAGE_1[0]]


@pytest.mark.asyncio
async def test_filler_play_does_not_resynthesize_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bank = get_filler_bank()

    def _boom(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("synthesize_pcm must not run on the play hot path")

    monkeypatch.setattr("filler_bank.synthesize_pcm", _boom)
    player = FillerPlayer(bank)
    await player.play(STAGE_1[0])
    assert player.played == [STAGE_1[0]]


def test_get_filler_bank_is_process_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    bank = get_filler_bank()

    def _boom_synth(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("synthesize_pcm must not run after boot cache")

    def _boom_assert(**_kwargs: object) -> object:
        raise AssertionError("assert_filler_bank must not re-run on get_filler_bank")

    monkeypatch.setattr("filler_bank.synthesize_pcm", _boom_synth)
    monkeypatch.setattr("filler_bank.assert_filler_bank", _boom_assert)
    assert get_filler_bank() is bank


def test_apply_record_does_not_restore_store_name_after_correction() -> None:
    store = CallerStore(path=None)
    store.touch("+61449004305", name="Sam Smith")
    state = CallState(branch="shellharbour")
    record = store.lookup("+61449004305")
    assert record is not None
    apply_record_to_state(state, record)
    state.correct_caller_name("Johnson")
    apply_record_to_state(state, record)
    assert state.caller_name == "Johnson"
    assert state.name_corrected is True


def test_name_correction_overwrites_state_store_and_known_facts() -> None:
    store = CallerStore(path=None)
    store.touch("+61449004305", name="Sam Smith")
    state = CallState(branch="shellharbour")
    record = store.lookup("+61449004305")
    assert record is not None
    apply_record_to_state(state, record)
    assert state.caller_name == "Sam Smith"
    state.observe_user_text("My name is Johnson")
    assert state.name_corrected is True
    assert state.caller_name == "Johnson"
    store.touch("+61449004305", name=state.caller_name)
    assert store.lookup("+61449004305").name == "Johnson"  # type: ignore[union-attr]
    facts = state.known_facts_block()
    assert "Johnson" in facts
    assert "name_corrected: True" in facts
    assert "Sam Smith" not in facts


def test_junk_does_not_clear_goal_or_name() -> None:
    state = CallState(branch="shellharbour", caller_name="Johnson")
    state.correct_caller_name("Johnson")
    state.lock_goal("cancel", "Cancel the caller's existing appointment")
    for text in ("Ne güzel", "puedes buscar el mundo", "Iya", "Mm", "Load what?"):
        verdict = classify_user_turn(
            text, tool_in_flight=True, recent_fillers=["mm, c'moooon, load"]
        )
        assert verdict.ignore is True
    assert state.caller_name == "Johnson"
    assert state.goal_kind == "cancel"
    assert "Cancel" in (state.active_goal or "")


@pytest.mark.asyncio
async def test_cancel_resolves_by_ani_history_after_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_receptionist import AvaReceptionist

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = PracticeClient(mode="mock")
    client.seed_patient(
        patient_id="pat_sam",
        name="Sam Smith",
        phone="0449004305",
        date_of_birth="1988-03-12",
    )
    client.seed_slot(
        slot_id="slot_shellharbour_2026-09-21_1000_dr-mohit-tolani",
        branch_id="shellharbour",
        date="2026-09-21",
        time="10:00",
        clinician="Dr Mohit Tolani",
        taken=True,
    )
    client.bookings["bkg_127e73a5fa"] = Booking(
        booking_id="bkg_127e73a5fa",
        slot_id="slot_shellharbour_2026-09-21_1000_dr-mohit-tolani",
        branch_id="shellharbour",
        patient_id="pat_sam",
        date="2026-09-21",
        time="10:00",
        clinician="Dr Mohit Tolani",
        reason="check-up",
    )
    store = CallerStore(path=None)
    store.touch(
        "+61449004305",
        name="Sam Smith",
        booking={
            "booking_id": "bkg_127e73a5fa",
            "date": "2026-09-21",
            "time": "10:00",
            "clinician": "Dr Mohit Tolani",
        },
    )
    state = CallState(
        branch="shellharbour",
        today=date(2026, 9, 15),
        ani="+61449004305",
        caller_mobile="0449004305",
    )
    record = store.lookup("+61449004305")
    assert record is not None
    apply_record_to_state(state, record)
    ava = AvaReceptionist(
        state=state,
        booking=MemoryBookingProvider(client, now_fn=lambda: SYDNEY_NOW),
        caller_store=store,
        filler_player=_QuietPlayer(),
    )
    looked = await MemoryBookingProvider(
        client, now_fn=lambda: SYDNEY_NOW
    ).lookup_patient(mobile="0449004305")
    ava.state.pms_record = looked
    verified = ava.state.verify_dob("1988-03-12")
    assert verified["ok"] is True
    ava.state.observe_user_text("My name is Johnson")
    ava._persist_caller_name()
    assert store.lookup("+61449004305").name == "Johnson"  # type: ignore[union-attr]

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    cancelled = await ava.cancel_appointment(SimpleNamespace(), booking_id="bkg_wrong")
    assert cancelled.get("confirmed") is True
    assert cancelled.get("booking_id") == "bkg_127e73a5fa"
    assert client.bookings["bkg_127e73a5fa"].cancelled is True


@pytest.mark.asyncio
async def test_cancel_ambiguous_lists_candidates_never_fakes_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_receptionist import AvaReceptionist

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    store = CallerStore(path=None)
    store.touch(
        "+61449004305",
        name="Sam Smith",
        booking={"booking_id": "bkg_one", "date": "2026-09-21", "time": "10:00"},
    )
    store.touch(
        "+61449004305",
        name="Sam Smith",
        booking={"booking_id": "bkg_two", "date": "2026-09-22", "time": "11:00"},
    )
    state = CallState(
        branch="shellharbour",
        ani="+61449004305",
        caller_mobile="0449004305",
        dob_verified=True,
    )
    ava = AvaReceptionist(
        state=state,
        booking=MemoryBookingProvider(
            PracticeClient(mode="mock"), now_fn=lambda: SYDNEY_NOW
        ),
        caller_store=store,
        filler_player=_QuietPlayer(),
    )

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    result = await ava.cancel_appointment(SimpleNamespace(), booking_id="")
    assert result.get("confirmed") is not True
    assert result["reason"] == "ambiguous_booking"
    assert len(result["candidates"]) == 2


@pytest.mark.asyncio
async def test_verified_dob_readback_allowed_unverified_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_receptionist import AvaReceptionist

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state = CallState(branch="shellharbour")
    state.pms_record = {
        "ok": True,
        "is_existing_patient": True,
        "patients": [{"date_of_birth": "1990-01-15", "name": "Sam"}],
    }
    ava = AvaReceptionist(
        state=state,
        booking=MemoryBookingProvider(
            PracticeClient(mode="mock"), now_fn=lambda: SYDNEY_NOW
        ),
        filler_player=_QuietPlayer(),
    )
    refused = await ava.read_date_of_birth(SimpleNamespace())
    assert refused["ok"] is False
    assert refused["reason"] == "not_verified"
    verified = ava.state.verify_dob("1990-01-15")
    assert verified["ok"] is True
    facts = ava.state.known_facts_block()
    assert "1990-01-15" in facts
    allowed = await ava.read_date_of_birth(SimpleNamespace())
    assert allowed["ok"] is True
    assert allowed["date_of_birth"] == "1990-01-15"
    assert "January" in (allowed.get("spoken") or "")


@pytest.mark.asyncio
async def test_fillers_before_cancel_and_book_confirm_kicked_under_one_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava_receptionist import AvaReceptionist

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = PracticeClient(mode="mock")
    slot_id = "slot_shellharbour_2026-09-16_0930_dr-mohit-tolani"
    client.seed_slot(
        slot_id=slot_id,
        branch_id="shellharbour",
        date="2026-09-16",
        time="09:30",
        clinician="Dr Mohit Tolani",
    )
    inner = MemoryBookingProvider(client, now_fn=lambda: SYDNEY_NOW)

    class Slow(MemoryBookingProvider):
        def __init__(self) -> None:
            super().__init__(client, now_fn=lambda: SYDNEY_NOW)
            self.tool_ok_at: float | None = None

        async def book_appointment(self, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.05)
            result = await inner.book_appointment(**kwargs)
            self.tool_ok_at = time.perf_counter()
            return result

        async def cancel_appointment(self, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.05)
            return await inner.cancel_appointment(**kwargs)

    slow = Slow()
    player = _QuietPlayer()
    replies: list[dict] = []

    class Session:
        tts = None
        llm = SimpleNamespace(capabilities=SimpleNamespace(supports_say=False))

        def generate_reply(self, **kwargs: object) -> SimpleNamespace:
            replies.append(dict(kwargs))
            return SimpleNamespace()

        def say(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("say should not be used")

    state = CallState(
        branch="shellharbour",
        today=date(2026, 9, 15),
        caller_name="Johnson",
        caller_mobile="0449004305",
    )
    ava = AvaReceptionist(
        state=state,
        booking=slow,
        caller_store=CallerStore(path=None),
        filler_player=player,
    )
    ctx = SimpleNamespace(session=Session())
    state.booking_flow.offer_slots([{"slot_id": slot_id}])
    booked = await ava.book_appointment(
        ctx, slot_id=slot_id, reason="check-up", name="Johnson", mobile="0449004305"
    )
    assert booked.get("confirmed") is True
    assert player.played, "filler must utter before book await"
    assert ava.state.book_confirm_kicked_at is not None
    assert slow.tool_ok_at is not None
    assert ava.state.book_confirm_kicked_at - slow.tool_ok_at < 1.0
    assert replies, "confirm path must kick generate_reply"
    assert "_dispatch_with_ladder" in inspect.getsource(
        AvaReceptionist.cancel_appointment
    )
    assert 'in_flight="cancel_appointment"' in inspect.getsource(
        AvaReceptionist.cancel_appointment
    ) or "in_flight='cancel_appointment'" in inspect.getsource(
        AvaReceptionist.cancel_appointment
    )
