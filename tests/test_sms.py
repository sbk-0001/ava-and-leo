"""Confirmation texts after a booking, a move, or a cancellation.

The practice is Illawarra Dentists; the text names it first, then the clinic
the appointment is at, the dentist, the time, and how to change it. Texts go
only to a confirmed Australian mobile, and only when a provider is set up.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from sms import (
    BrevoSms,
    MemorySms,
    TelnyxSms,
    au_mobile_e164,
    booking_text,
    sms_sender_from_env,
)

SYDNEY = ZoneInfo("Australia/Sydney")
BOOKED = {
    "ok": True,
    "confirmed": True,
    "booking_id": "bkg_1",
    "branch_id": "dapto",
    "date": "2026-09-22",
    "time": "09:30",
    "clinician": "Dr Amy Min",
}


def test_only_australian_mobiles_get_a_text() -> None:
    assert au_mobile_e164("0474 470 332") == "+61474470332"
    assert au_mobile_e164("+61 474 470 332") == "+61474470332"
    assert au_mobile_e164("61474470332") == "+61474470332"
    assert au_mobile_e164("(02) 4288 0737") is None  # a landline cannot take it
    assert au_mobile_e164("0474 470") is None
    assert au_mobile_e164("") is None
    assert au_mobile_e164(None) is None


def test_booking_text_names_the_practice_first_then_the_clinic() -> None:
    text = booking_text("booked", BOOKED, first_name="Robert")
    assert text.startswith("Illawarra Dentists: Hi Robert,")
    assert "Dapto Dentists" in text
    assert "35 Baan Baan Street" in text
    assert "Dr Amy Min" in text
    assert "Tue 22 Sep at 9:30am" in text
    assert "(02) 4288 0737" in text
    assert "$50" in text and "24 hours" in text
    assert len(text) <= 306  # two SMS segments at most


def test_text_never_names_a_placeholder_dentist() -> None:
    text = booking_text("booked", {**BOOKED, "clinician": "available dentist"})
    assert "available dentist" not in text
    assert "with Dr" not in text
    assert text.startswith("Illawarra Dentists: Hi,")


def test_moved_and_cancelled_texts() -> None:
    moved = booking_text(
        "moved",
        {
            **BOOKED,
            "branch_id": "woonona",
            "time": "14:00",
            "clinician": "Dr Chin Valsan",
        },
        first_name="Robert",
    )
    assert "moved" in moved
    assert "Woonona Dentists" in moved and "2:00pm" in moved
    assert "Dr Chin Valsan" in moved

    cancelled = booking_text(
        "cancelled", {"booking_id": "bkg_1", "branch_id": "dapto"}, first_name="Robert"
    )
    assert cancelled.startswith("Illawarra Dentists: Hi Robert,")
    assert "cancelled" in cancelled
    assert "(02) 4288 0737" in cancelled
    assert "$50" not in cancelled


def test_sender_is_only_built_when_configured() -> None:
    assert sms_sender_from_env({}) is None
    assert sms_sender_from_env({"SMS_PROVIDER": "telnyx"}) is None  # no key
    telnyx = sms_sender_from_env(
        {"SMS_PROVIDER": "telnyx", "TELNYX_API_KEY": "k", "SMS_FROM": "IllawarraDn"}
    )
    assert isinstance(telnyx, TelnyxSms)
    assert telnyx.from_ == "IllawarraDn"
    assert isinstance(sms_sender_from_env({"SMS_PROVIDER": "memory"}), MemorySms)


async def test_telnyx_posts_the_message(monkeypatch) -> None:
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"data": {"id": "msg_123"}}).encode()

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.data)
        return _Resp()

    monkeypatch.setattr("sms.urlopen", fake_urlopen)
    sender = TelnyxSms(api_key="KEY", from_="+61400000000", profile_id="prof")
    result = await sender.send("+61474470332", "hello")
    assert result == {"ok": True, "id": "msg_123"}
    assert seen["url"] == "https://api.telnyx.com/v2/messages"
    assert seen["auth"] == "Bearer KEY"
    assert seen["body"] == {
        "from": "+61400000000",
        "to": "+61474470332",
        "text": "hello",
        "messaging_profile_id": "prof",
    }


async def test_telnyx_failure_is_reported_not_raised(monkeypatch) -> None:
    def boom(request, timeout):
        raise OSError("network down")

    monkeypatch.setattr("sms.urlopen", boom)
    result = await TelnyxSms(api_key="k", from_="x").send("+61474470332", "hi")
    assert result["ok"] is False
    assert "network down" in result["error"]


def test_brevo_is_the_default_sms_provider_when_keyed() -> None:
    """Strategybyte's prepaid SMS credits are on Brevo."""
    sender = sms_sender_from_env({"SMS_PROVIDER": "brevo", "BREVO_API_KEY": "k"})
    assert isinstance(sender, BrevoSms)
    assert sender.sender == "Illawarra"  # 11-letter limit; body names the practice
    assert sms_sender_from_env({"SMS_PROVIDER": "brevo"}) is None
    custom = sms_sender_from_env(
        {"SMS_PROVIDER": "brevo", "BREVO_API_KEY": "k", "SMS_FROM": "IllawarraDC"}
    )
    assert custom.sender == "IllawarraDC"
    with pytest.raises(ValueError):
        BrevoSms(api_key="k", sender="Illawarra Dentists")


async def test_brevo_posts_a_transactional_sms(monkeypatch) -> None:
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"messageId": 1511882900176220}).encode()

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["key"] = request.headers["Api-key"]
        seen["body"] = json.loads(request.data)
        return _Resp()

    monkeypatch.setattr("sms.urlopen", fake_urlopen)
    result = await BrevoSms(api_key="KEY").send("+61474470332", "hello")
    assert result == {"ok": True, "id": "1511882900176220"}
    assert seen["url"] == "https://api.brevo.com/v3/transactionalSMS/send"
    assert seen["key"] == "KEY"
    assert seen["body"] == {
        "sender": "Illawarra",
        "recipient": "61474470332",
        "content": "hello",
        "type": "transactional",
        "tag": "ava-booking",
    }


async def test_brevo_failure_is_reported_not_raised(monkeypatch) -> None:
    def boom(request, timeout):
        raise OSError("402 not enough credits")

    monkeypatch.setattr("sms.urlopen", boom)
    result = await BrevoSms(api_key="k").send("+61474470332", "hi")
    assert result["ok"] is False
    assert "credits" in result["error"]


# --- Ava sends it --------------------------------------------------------------


def _ava(monkeypatch, sms):
    from ava_receptionist import AvaReceptionist
    from booking import MemoryBookingProvider
    from call_state import CallState
    from caller_store import CallerStore
    from practice import PracticeClient

    practice = PracticeClient(mode="mock")
    for hhmm in ("0830", "0900"):
        practice.seed_slot(
            slot_id=f"slot_dapto_2026-09-17_{hhmm}_dr-amy-min",
            branch_id="dapto",
            date="2026-09-17",
            time=f"{hhmm[:2]}:{hhmm[2:]}",
            clinician="Dr Amy Min",
        )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state = CallState(branch="dapto", now=datetime(2026, 9, 16, 20, 0, tzinfo=SYDNEY))
    ava = AvaReceptionist(
        state=state,
        booking=MemoryBookingProvider(practice, now_fn=lambda: state.now),
        caller_store=CallerStore(),
        sms=sms,
    )

    async def _run_only(self, context, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(AvaReceptionist, "_dispatch_with_ladder", _run_only)
    monkeypatch.setattr(AvaReceptionist, "_kick_book_confirm", lambda *a, **k: None)
    monkeypatch.setattr(AvaReceptionist, "_kick_cancel_confirm", lambda *a, **k: None)
    return ava, practice


async def _book(ava, mobile="0474 470 332"):
    ctx = SimpleNamespace()
    ava.state.observe_user_text("I want to book")
    ava.state.register_mobile(mobile)
    ava.state.confirm_mobile(correct=True)
    await ava.check_availability(
        ctx, appointment_type="check up", date_range="2026-09-17"
    )
    return await ava.book_appointment(
        ctx,
        slot_id="slot_dapto_2026-09-17_0830_dr-amy-min",
        reason="check up",
        name="Robert Walker",
    )


async def test_confirmed_booking_texts_the_caller(monkeypatch) -> None:
    sms = MemorySms()
    ava, _ = _ava(monkeypatch, sms)
    booked = await _book(ava)
    assert booked["confirmed"] is True
    assert booked["confirmation_text"] == "queued"
    await ava.flush_texts()
    assert len(sms.sent) == 1
    to, text = sms.sent[0]
    assert to == "+61474470332"
    assert text.startswith("Illawarra Dentists: Hi Robert,")
    assert "Dapto Dentists" in text and "Dr Amy Min" in text


async def test_confirm_facts_mention_the_text_only_when_queued() -> None:
    from ava_receptionist import book_confirm_facts

    queued = book_confirm_facts("Robert", {**BOOKED, "confirmation_text": "queued"})
    assert "text with the details is on its way" in queued
    assert "Do not promise a text" not in queued

    none = book_confirm_facts("Robert", {**BOOKED, "confirmation_text": "unavailable"})
    assert "Do not promise a text" in none


async def test_no_provider_means_no_text_promised(monkeypatch) -> None:
    ava, _ = _ava(monkeypatch, None)
    booked = await _book(ava)
    assert booked["confirmed"] is True
    assert booked["confirmation_text"] == "unavailable"


async def test_landline_caller_gets_no_text(monkeypatch) -> None:
    sms = MemorySms()
    ava, _ = _ava(monkeypatch, sms)
    ava.state.caller_mobile = "0242880737"
    ava.state.mobile_confirmed = True
    ctx = SimpleNamespace()
    ava.state.observe_user_text("I want to book")
    await ava.check_availability(
        ctx, appointment_type="check up", date_range="2026-09-17"
    )
    booked = await ava.book_appointment(
        ctx,
        slot_id="slot_dapto_2026-09-17_0830_dr-amy-min",
        reason="check up",
        name="Robert Walker",
        mobile="0242880737",
    )
    if booked.get("confirmed"):
        assert booked["confirmation_text"] == "no_mobile"
    await ava.flush_texts()
    assert sms.sent == []


async def test_failed_booking_sends_nothing(monkeypatch) -> None:
    sms = MemorySms()
    ava, practice = _ava(monkeypatch, sms)
    for slot in practice.slots.values():
        slot.taken = True
    booked = await _book(ava)
    assert booked.get("confirmed") is not True
    assert "confirmation_text" not in booked
    await ava.flush_texts()
    assert sms.sent == []


async def test_cancel_texts_the_caller(monkeypatch) -> None:
    sms = MemorySms()
    ava, _ = _ava(monkeypatch, sms)
    booked = await _book(ava)
    await ava.flush_texts()
    ava.state.dob_verified = True
    monkeypatch.setattr(
        type(ava.state), "require_dob_for_existing", lambda self: {"ok": True}
    )
    cancelled = await ava.cancel_appointment(
        SimpleNamespace(), booking_id=booked["booking_id"]
    )
    assert cancelled["confirmed"] is True
    assert cancelled["confirmation_text"] == "queued"
    await ava.flush_texts()
    assert len(sms.sent) == 2
    assert "cancelled" in sms.sent[1][1]


@pytest.mark.parametrize("kind", ["booked", "moved", "cancelled"])
def test_every_text_starts_with_the_practice(kind) -> None:
    assert booking_text(kind, BOOKED).startswith("Illawarra Dentists:")
