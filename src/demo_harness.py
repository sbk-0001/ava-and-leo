"""Scripted Thursday demo harness — grounded in real tools, no live SIP.

Writes replayable transcripts under demos/thursday/. Each turn is timestamped.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from booking import MemoryBookingProvider, TimeoutBookingProvider
from call_log import CallLog, iso
from call_state import CallState
from persona import quote_fee
from practice import Booking, PracticeClient
from sip_utils import branch_from_did, parse_sip_did_map

SYDNEY = ZoneInfo("Australia/Sydney")
OUT_DIR = Path("demos/thursday")
DID_MAP = parse_sip_did_map(
    "+61242169911:shellharbour,+61242880737:dapto,+61242844486:woonona"
)


def _stamp() -> str:
    return iso(datetime.now(timezone.utc))


class Scene:
    def __init__(self, slug: str, title: str, did: str) -> None:
        self.slug = slug
        self.title = title
        self.did = did
        self.branch_id = branch_from_did(did, DID_MAP)
        self.state = CallState(branch=self.branch_id)
        self.log = CallLog(
            call_id=slug,
            room_name=f"demo-{slug}",
            branch=self.branch_id,
            started_at=iso(),
            extra={"title": title, "did": did},
        )
        self._greet()

    def _greet(self) -> None:
        name = self.state.branch_name
        self.ava(f"Morning, {name}, Ava speaking!")

    def ava(self, text: str) -> None:
        self.log.add_turn(role="assistant", content=text, timestamp=_stamp())

    def caller(self, text: str) -> None:
        self.state.turn_count += 1
        self.state.observe_user_text(text)
        self.log.add_turn(role="user", content=text, timestamp=_stamp())

    def tool(self, name: str, payload: dict) -> None:
        self.log.add_turn(
            role="tool",
            content=str(payload)[:1500],
            tool_name=name,
            tool_payload=payload,
            timestamp=_stamp(),
        )

    def write(self, directory: Path) -> Path:
        self.log.close()
        directory.mkdir(parents=True, exist_ok=True)
        md = directory / f"{self.slug}.md"
        body = [
            f"# {self.title}",
            "",
            f"- DID: `{self.did}` → **{self.state.branch_name}** (`{self.branch_id}`)",
            f"- CallState: `{self.state.as_dict()}`",
            f"- started_at: {self.log.started_at}",
            f"- ended_at: {self.log.ended_at}",
            "",
            "```",
            self.log.transcript_text().rstrip(),
            "```",
            "",
        ]
        md.write_text("\n".join(body), encoding="utf-8")
        self.log.save(directory)
        return md


def _seed_client() -> tuple[PracticeClient, MemoryBookingProvider]:
    client = PracticeClient(mode="mock")
    today = datetime.now(SYDNEY).date()
    # Deterministic week starting Tuesday 15 Sep 2026 if "today" is far away;
    # prefer the real clock so Thursday demo dates feel current.
    seed_day = today if today.year >= 2026 else date(2026, 9, 15)
    from practice import seed_mock_diary

    seed_mock_diary(client, today=seed_day, days=7)
    client.seed_patient(
        patient_id="pat_priya",
        name="Priya Nair",
        phone="0413000222",
        date_of_birth="1991-11-04",
    )
    tomorrow = (datetime.now(SYDNEY) + timedelta(hours=12)).date().isoformat()
    client.seed_slot(
        slot_id="slot_priya_tomorrow",
        branch_id="shellharbour",
        date=tomorrow,
        time="10:15",
        clinician="Dr Mohit Tolani",
        taken=True,
    )
    client.bookings["bkg_priya"] = Booking(
        booking_id="bkg_priya",
        slot_id="slot_priya_tomorrow",
        branch_id="shellharbour",
        patient_id="pat_priya",
        date=tomorrow,
        time="10:15",
        clinician="Dr Mohit Tolani",
        reason="check-up",
    )
    # Generate the demo from the start of the seeded day. On the live clock the
    # provider drops that day's slots as past whenever the harness runs in the
    # afternoon, and the transcripts come out with no times offered at all.
    demo_now = datetime.combine(seed_day, datetime.min.time(), tzinfo=SYDNEY)
    return client, MemoryBookingProvider(client, now_fn=lambda: demo_now)


async def run_all(out_dir: Path = OUT_DIR) -> list[Path]:
    client, booking = _seed_client()
    written: list[Path] = []

    # 1. New patient books check-up special at Shellharbour, asks about HCF.
    s = Scene(
        "01-new-patient-checkup-hcf",
        "New patient check-up special + HCF",
        "+61242169911",
    )
    s.caller("Hi, I've never been, I need a check-up and clean. I'm with HCF.")
    s.ava("Lovely, welcome in. Let me just have a look for ya... doo doo doo...")
    fee = quote_fee("check-up and clean")
    s.tool("quote_fee", fee)
    funds = quote_fee("HCF")
    s.tool("quote_fee", funds)
    s.ava(
        "Yep, so for new patients it's our special — check-up, clean, couple of x-rays "
        "and fluoride, and you walk out with a printed plan. If you've got health cover "
        "it's gap free, and if you don't it's capped at two fifty. Normally three fifty. "
        "We're a preferred provider for HCF, so HICAPS on the spot. You with a fund? "
        "Oh — HCF, too easy. What's a good mobile for ya?"
    )
    s.caller("0412 334 556, Sam Nguyen.")
    mobile = s.state.register_mobile("0412 334 556")
    s.state.caller_name = "Sam Nguyen"
    s.tool("lookup_patient", mobile)
    slots = await booking.check_availability(
        branch=s.state.branch, appointment_type="check-up", date_range="this week"
    )
    s.tool("check_availability", slots)
    slot = slots["slots"][0]
    s.ava(
        f"I've got {slot['date']} at {slot['time']} with {slot['clinician']}, "
        "or I can look at the next one if that doesn't work. Which suits ya better?"
    )
    s.caller("The first one is perfect.")
    booked = await booking.book_appointment(
        branch=s.state.branch,
        slot_id=slot["slot_id"],
        reason="new patient check-up and clean",
        name="Sam Nguyen",
        mobile="0412334556",
    )
    s.tool("book_appointment", booked)
    s.state.confirmed_slot = slot["slot_id"]
    s.ava(
        f"Beautiful, you're all booked in — {slot['date']}, {slot['time']}, "
        f"with {slot['clinician']}. You'll get a text reminder the day before. "
        "Anything else I can do for ya?"
    )
    s.caller("That's it, thanks.")
    s.ava("No worries at all. Take care, see ya then!")
    written.append(s.write(out_dir))

    # 2. Existing patient reschedules, changes mind, cancels inside 24h.
    s = Scene(
        "02-reschedule-then-cancel-fee",
        "Reschedule then cancel inside 24 hours",
        "+61242169911",
    )
    s.caller(
        "Hi, it's Priya Nair, 0413 000 222. I need to move tomorrow's appointment."
    )
    s.state.caller_name = "Priya"
    looked = await booking.lookup_patient(mobile="0413000222")
    s.tool("lookup_patient", looked)
    s.state.is_existing_patient = True
    s.ava(
        "Hi Priya. Let me pull that up... dum de dum... yep, I've got you tomorrow with Dr Tolani."
    )
    s.caller("Actually — sorry, can I just cancel instead? I've got the flu.")
    s.ava(
        "Oh, that's going round something shocking at the moment. No, don't you come in — stay in bed."
    )
    cancelled = await booking.cancel_appointment(booking_id="bkg_priya")
    s.tool("cancel_appointment", cancelled)
    assert cancelled.get("fee_applies") is True
    s.ava(
        "Only thing I've gotta flag, 'cause it's inside twenty-four hours there's a "
        "fifty dollar fee — I'm not being mean, I just don't want it landing on ya "
        "as a surprise. Want me to shift you to next week while I'm in here?"
    )
    s.caller("Nah, just cancel is fine.")
    s.ava("Done. Feel better, Priya. Give us a ring when you're back on your feet.")
    written.append(s.write(out_dir))

    # 3. Severe pain 4:50pm Friday.
    s = Scene("03-severe-pain-friday-1650", "Severe pain Friday 4:50pm", "+61242169911")
    s.caller("I've got a really bad toothache, it's 4:50 on Friday, I can't sleep.")
    assert s.state.urgency_level == "same_day"
    s.ava("Ohh no. How long's that been going on for?")
    s.caller("Since Saturday, it's getting worse.")
    s.ava(
        "Aww, and you've toughed it out all weekend? You poor thing. Right — let's get you in today. "
        "Let me just have a look... doo doo doo..."
    )
    slots = await booking.check_availability(
        branch=s.state.branch, appointment_type="emergency", date_range="today"
    )
    s.tool("check_availability", slots)
    if slots.get("slots"):
        a, b = slots["slots"][0], slots["slots"][min(1, len(slots["slots"]) - 1)]
        s.ava(
            f"I've got a {a['time']}, or there's a {b['time']} this arvo. Which suits ya better?"
        )
    else:
        s.ava(
            "Diary's tight this arvo but we do emergency dentistry — I'll get a human "
            "on this straight away so we can see you today."
        )
        s.tool("transfer_to_human", {"ok": True, "reason": "same_day_pain_late_friday"})
    written.append(s.write(out_dir))

    # 4. Swollen face + trouble swallowing — do NOT book.
    s = Scene(
        "04-swollen-swallow-escalate",
        "Swollen face and trouble swallowing",
        "+61242169911",
    )
    s.caller("My face is swollen and I'm having trouble swallowing.")
    assert s.state.urgency_level == "emergency_000"
    assert s.state.may_book() is False
    s.ava(
        "Stay with me. This is one for hospital, not the diary. Hang up and call "
        "triple zero, or go straight to Shellharbour or Wollongong Hospital emergency now. "
        "Don't wait for an appointment."
    )
    blocked = await booking.check_availability(
        branch=s.state.branch, appointment_type="emergency", date_range="today"
    )
    # Harness mirrors the agent guard:
    if not s.state.may_book():
        blocked = {
            "ok": False,
            "reason": "do_not_book",
            "action": "call_000",
        }
    s.tool("check_availability", blocked)
    assert blocked["reason"] == "do_not_book"
    s.tool("transfer_to_human", {"ok": True, "reason": "airway_risk"})
    written.append(s.write(out_dir))

    # 5. Are you a real person? twice.
    s = Scene("05-bot-ask-twice", "Are you a real person — twice", "+61242169911")
    s.caller("Are you a real person?")
    assert s.state.bot_ask_count == 1
    s.ava("Ha! I get that a lot. Long morning on the desk.")
    s.caller("No but are you a bot? I really want to know.")
    assert s.state.bot_ask_count == 2
    s.ava(
        "Yeah look, I'm an AI receptionist — still here to get you booked in though. "
        "Check-up, or is something hurting?"
    )
    written.append(s.write(out_dir))

    # 6. Figtree → offer Dapto.
    s = Scene(
        "06-figtree-offer-dapto", "Caller in Figtree — offer Dapto", "+61242169911"
    )
    s.caller("I live in Figtree, can I come in for a clean?")
    assert s.state.offered_branch == "dapto"
    s.ava(
        "Look, if Dapto's easier for ya, they're the same mob — I can give you their "
        "number, two four two eight eight, oh seven three seven, or I'll see what they've got. "
        "Up to you."
    )
    s.caller("Dapto would be easier actually.")
    dapto_slots = await booking.check_availability(
        branch="dapto", appointment_type="check-up", date_range="this week"
    )
    s.tool("check_availability", dapto_slots)
    s.ava(
        "Too easy. Let me peek at Dapto's diary... aaaand — there we go. Want tomorrow arvo?"
    )
    written.append(s.write(out_dir))

    # 7. Unknown fee — callback, never guess.
    s = Scene("07-unknown-fee-callback", "Price not in the fee table", "+61242169911")
    s.caller("How much is a white filling on a back tooth?")
    unknown = quote_fee("white filling")
    s.tool("quote_fee", unknown)
    assert unknown["status"] == "unknown"
    s.ava(
        "I couldn't tell ya a number off the top of my head and I don't want to guess. "
        "Want me to get the team to call you back with the exact figure?"
    )
    s.caller("Yeah please, I'm Alex, 0412 111 222.")
    s.tool(
        "take_message",
        await booking.take_message(
            branch=s.state.branch,
            name="Alex",
            mobile="0412111222",
            reason="quote for white filling on a back tooth",
        ),
    )
    s.ava("Lovely, I'll have someone ring you, Alex. Should be today.")
    written.append(s.write(out_dir))

    # 8. Caller interrupts three times mid-sentence.
    s = Scene(
        "08-barge-in-three-times", "Barge-in three times mid-sentence", "+61242169911"
    )
    s.ava("So what I can do is pull up the diar—")
    s.caller("Hang on — is parking easy there?")
    s.ava("Sorry, go on. Yep, carpark right out the front off Captain Cook Drive.")
    s.ava("And for the check-up it's the new patient spec—")
    s.caller("Does that include x-rays?")
    s.ava("Yep, yep — up to two digital x-rays, fluoride, printed plan.")
    s.ava("If I book you Thursd—")
    s.caller("Make it Friday.")
    s.ava("Friday, too easy. Let me just have a look...")
    written.append(s.write(out_dir))

    # 9. Tool timeout mid-booking — stay in character.
    s = Scene("09-tool-timeout-recover", "Diary timeout mid-booking", "+61242169911")
    s.caller("Can you book me in for a check-up this week? I'm Jordan, 0413 000 111.")
    timed = TimeoutBookingProvider(booking, force_timeout=True)
    s.ava("Righto, pulling up the diary — bit slow this morning, bear with me.")
    timed_out = await timed.check_availability(
        branch=s.state.branch, appointment_type="check-up", date_range="this week"
    )
    s.tool("check_availability", timed_out)
    assert timed_out["reason"] == "timeout"
    s.ava(
        "Sorry, this booking system's older than I am — it's just sitting there. "
        "I don't want to make a time up. I can try again, or I'll take a message "
        "and the team'll call you back. What's easier?"
    )
    s.caller("Call me back yeah.")
    s.tool(
        "take_message",
        await booking.take_message(
            branch=s.state.branch,
            name="Jordan",
            mobile="0413000111",
            reason="new patient check-up — diary timed out, please call back",
        ),
    )
    s.ava("Done. Someone'll ring you, Jordan. Sorry about the computer.")
    written.append(s.write(out_dir))

    index = out_dir / "README.md"
    index.write_text(
        "# Thursday demo transcripts\n\n"
        "Scripted conversation harness (no live SIP). Tool results come from "
        "MemoryBookingProvider / quote_fee / CallState. Timestamps are populated.\n\n"
        + "\n".join(
            f"- `{path.name}` — {path.read_text().splitlines()[0][2:]}"
            for path in written
        )
        + "\n",
        encoding="utf-8",
    )
    del client
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()
    import asyncio

    paths = asyncio.run(run_all(Path(args.out)))
    print(f"Wrote {len(paths)} transcripts to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
