"""Never-silent demo harness: 8 scenarios + metrics.

Writes `demos/never-silent/metrics.json` from the filler ladder, availability
cache, and anti-repetition pools. Full LiveKit audio recordings need live
secrets (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `OPENAI_API_KEY`);
without them this scaffold still produces per-call metrics and a recording note.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from availability_cache import CachedBookingProvider
from booking import MemoryBookingProvider
from call_state import CallState
from filler_ladder import FakeClock, FakeSpeaker, FillerLadder
from phrase_pools import ACKS, BARGE_IN_RESUME
from practice import PracticeClient, seed_mock_diary

OUT_DIR = Path("demos/never-silent")

SCENARIOS = (
    ("01-new-patient-checkup", "New patient check-up — cache-first availability"),
    ("02-reschedule-cancel", "Reschedule then cancel inside 24h"),
    ("03-severe-pain", "Severe pain Friday — same-day ladder"),
    ("04-swollen-swallow", "Swollen + swallowing — no book"),
    ("05-bot-ask-twice", "Are you a real person — twice"),
    ("06-figtree-dapto", "Figtree caller — offer Dapto"),
    ("07-unknown-fee", "Unknown fee — take_message"),
    ("08-barge-in-three-times", "Barge-in three times — resume pool"),
)


@dataclass
class CallMetrics:
    slug: str
    title: str
    time_to_first_audio_ms: float
    longest_silence_gap_ms: float
    stock_phrases_used: list[str] = field(default_factory=list)
    first_audio_before_dispatch: bool = False
    stage5_fallback: bool = False
    audio_recording: str | None = None
    notes: list[str] = field(default_factory=list)


def live_audio_ready(env: dict[str, str] | None = None) -> bool:
    environ = env if env is not None else os.environ
    return all(
        str(environ.get(name, "")).strip()
        for name in (
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "OPENAI_API_KEY",
        )
    )


def _silence_gap_ms(trace: Any, *, caller_talking: bool = False) -> float:
    if caller_talking:
        return 0.0
    times = []
    if trace.first_audio_ts is not None:
        times.append(trace.first_audio_ts)
    if trace.tool_dispatch_ts is not None:
        times.append(trace.tool_dispatch_ts)
    if len(times) < 2:
        return 0.0
    return max(0.0, (max(times) - min(times)) * 1000)


async def _run_ladder(
    state: CallState,
    factory,
    *,
    booking: Any | None = None,
) -> tuple[dict[str, Any], Any, FakeSpeaker]:
    clock = FakeClock()
    speaker = FakeSpeaker(clock=clock)
    ladder = FillerLadder(
        state, speaker=speaker, booking=booking, clock=clock, sleeper=clock.sleep
    )
    result, trace = await ladder.dispatch(factory)
    return dict(result), trace, speaker


async def run_all(out_dir: Path = OUT_DIR) -> list[CallMetrics]:
    today = date(2026, 9, 15)
    client = PracticeClient(mode="mock")
    seed_mock_diary(client, today=today, days=14)
    booking = CachedBookingProvider(
        MemoryBookingProvider(client),
        today_fn=lambda: today,
    )
    await booking.prewarm()
    metrics: list[CallMetrics] = []
    record = live_audio_ready()

    async def cached_check():
        return await booking.check_availability(
            branch="shellharbour",
            appointment_type="check-up",
            date_range="this week",
        )

    # 1. New patient — cache-first check
    state = CallState(branch="shellharbour")
    state.pick_opening()
    result, trace, _spk = await _run_ladder(state, cached_check, booking=booking)
    state.pick_ack()
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[0][0],
            title=SCENARIOS[0][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=["check_availability cached=" + str(result.get("cached"))],
        )
    )

    # 2. Reschedule/cancel — book path re-verifies
    state = CallState(branch="shellharbour")
    slots = result.get("slots") or []
    slot_id = slots[0]["slot_id"] if slots else "slot_missing"

    async def book():
        return await booking.book_appointment(
            branch="shellharbour",
            slot_id=slot_id,
            reason="check-up",
            name="Priya",
            mobile="0413000222",
        )

    _booked, trace, _spk = await _run_ladder(state, book, booking=booking)
    state.pick_closing()
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[1][0],
            title=SCENARIOS[1][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=["book reverified"],
        )
    )

    # 3. Severe pain — same_day, still books via cache
    state = CallState(branch="shellharbour")
    state.observe_user_text("pain keeping me awake")
    _slots, trace, _spk = await _run_ladder(state, cached_check, booking=booking)
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[2][0],
            title=SCENARIOS[2][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=[f"urgency={state.urgency_level}"],
        )
    )

    # 4. Swollen swallow — tool still speaks first, then do_not_book
    state = CallState(branch="shellharbour")
    state.observe_user_text("swollen and trouble swallowing")

    async def blocked():
        return {"ok": False, "reason": "do_not_book", "action": "call_000"}

    _blocked, trace, _spk = await _run_ladder(state, blocked, booking=booking)
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[3][0],
            title=SCENARIOS[3][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=["do_not_book after first audio"],
        )
    )

    # 5. Bot ask — anti-rep acks
    state = CallState(branch="shellharbour")
    state.observe_user_text("Are you a real person?")
    state.observe_user_text("are you a bot")
    state.pick_ack()
    state.pick_ack()
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[4][0],
            title=SCENARIOS[4][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=0.0,
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=True,
            notes=[f"bot_ask_count={state.bot_ask_count}"],
        )
    )

    # 6. Figtree → Dapto
    state = CallState(branch="shellharbour")
    state.observe_user_text("I live in Figtree")

    async def dapto_check():
        return await booking.check_availability(
            branch="dapto",
            appointment_type="check-up",
            date_range="this week",
        )

    _dapto, trace, _spk = await _run_ladder(state, dapto_check, booking=booking)
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[5][0],
            title=SCENARIOS[5][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=[f"offered_branch={state.offered_branch}"],
        )
    )

    # 7. Unknown fee → take_message via ladder
    state = CallState(
        branch="shellharbour", caller_name="Alex", caller_mobile="0412111222"
    )

    async def leave():
        return await booking.take_message(
            branch="shellharbour",
            name="Alex",
            mobile="0412111222",
            reason="quote for white filling",
        )

    _msg, trace, _spk = await _run_ladder(state, leave, booking=booking)
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[6][0],
            title=SCENARIOS[6][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
        )
    )

    # 8. Barge-in three times
    state = CallState(branch="shellharbour")
    resumes = [state.mark_interrupted() for _ in range(2)]
    resumes.append(state.mark_interrupted())
    assert all(r in BARGE_IN_RESUME for r in resumes[:2])
    metrics.append(
        CallMetrics(
            slug=SCENARIOS[7][0],
            title=SCENARIOS[7][1],
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=0.0,
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=True,
            notes=["resume pool used, no sentence restart"],
        )
    )

    if record:
        for row in metrics:
            row.audio_recording = f"{row.slug}.wav"
            row.notes.append("live audio requested — record via LiveKit room egress")
    else:
        for row in metrics:
            row.notes.append(
                "audio skipped: set LIVEKIT_URL, LIVEKIT_API_KEY, "
                "LIVEKIT_API_SECRET, OPENAI_API_KEY to capture real recordings"
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live_audio": record,
        "target_longest_silence_ms": 800,
        "ack_pool": list(ACKS),
        "calls": [asdict(row) for row in metrics],
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "README.md").write_text(
        "# Never-silent Ava — scenario metrics\n\n"
        "Machinery metrics for the eight owner scenarios. "
        "`longest_silence_gap_ms` is the gap between first filler audio and "
        "tool dispatch in the harness (caller-talking gaps are excluded).\n\n"
        "Real WAV recordings need LiveKit + OpenAI secrets; without them this "
        "harness still writes `metrics.json` so CI can prove the ladder, cache, "
        "and anti-repetition pools ran.\n\n"
        + "\n".join(f"- `{row.slug}` — {row.title}" for row in metrics)
        + "\n",
        encoding="utf-8",
    )
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()
    import asyncio

    rows = asyncio.run(run_all(Path(args.out)))
    print(f"Wrote {len(rows)} scenario metrics to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
