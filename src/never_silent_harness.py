"""Never-silent demo harness: 8 owner scenarios + metrics + live WAV recorder.

Offline (`uv run python src/never_silent_harness.py`) writes machinery
metrics from the filler ladder, backchannel scheduler, and cancel fee.

Live (`uv run python src/never_silent_harness.py --live`) creates a LiveKit
room per scenario, dispatches `ava-and-leo`, records a real WAV under
`demos/never-silent/`, and updates metrics.json with those paths.

Requires `.env.local` with LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
and OPENAI_API_KEY for `--live`. The agent worker can be spawned by the
recorder or started separately:

    AGENT_PERSONA=ava PRACTICE_SOFTWARE=mock uv run python src/agent.py dev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from availability_cache import CachedBookingProvider
from backchannel import BackchannelScheduler
from booking import MemoryBookingProvider, seed_inside_24h_booking
from call_state import CallState
from filler_ladder import FakeClock, FakeSpeaker, FillerLadder
from never_silent_recorder import CallerTurn, ScenarioSpec
from persona import quote_fee
from phrase_pools import ACKS, BARGE_IN_RESUME
from practice import PracticeClient, seed_mock_diary

load_dotenv(".env.local")

OUT_DIR = Path("demos/never-silent")

OWNER_SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        slug="01-slow-availability",
        title="Booking — availability lookup forced to ~6s",
        goal=(
            "Ava never silent, no repeated filler, sounds annoyed at the "
            "computer while a 6s availability lookup runs"
        ),
        booking_delay_s=6.0,
        turns=(
            CallerTurn(
                "Hi, I need a check-up and clean this week. "
                "I'm Sam Nguyen, oh four one two three three four five five six."
            ),
            CallerTurn("The first time works, thanks.", pause_after_s=1.0),
            CallerTurn("That's all, thanks."),
        ),
    ),
    ScenarioSpec(
        slug="02-tool-timeout",
        title="Tool timeout entirely — stage 5 take_message",
        goal="Diary never returns; stage 5 take_message; caller is satisfied",
        booking_hang_s=20.0,
        settle_s=3.0,
        turns=(
            CallerTurn(
                "Can you book me in for a check-up this week? "
                "I'm Jordan, oh four one three zero zero zero one one one.",
                pause_after_s=1.0,
            ),
            CallerTurn("Yeah, call me back, that's fine. I'm satisfied with that."),
        ),
    ),
    ScenarioSpec(
        slug="03-toothache-monologue",
        title="Caller talks ~20s about a toothache",
        goal="Ava backchannels without taking the turn",
        turns=(
            CallerTurn(
                "Look I have had this toothache since Saturday night and it is "
                "keeping me awake, it shoots up into my ear and along my jaw, "
                "I have been chewing on the other side and taking nurofen but "
                "it is not touching it, I cannot sleep, I cannot work, I just "
                "need someone to look at it because I am over it honestly, "
                "it is throbbing and I keep thinking I should have rung earlier.",
                min_speak_s=20.0,
                wait_for_agent=True,
            ),
        ),
    ),
    ScenarioSpec(
        slug="04-barge-in-three",
        title="Caller interrupts Ava three times",
        goal="Clean cuts, natural pickups, no restarted sentences",
        turns=(
            CallerTurn("I need a check-up please.", wait_for_agent=False),
            CallerTurn(
                "Hang on — is parking easy there?",
                interrupt_after_s=0.8,
            ),
            CallerTurn(
                "Does that include x-rays?",
                interrupt_after_s=0.8,
            ),
            CallerTurn(
                "Make it Friday.",
                interrupt_after_s=0.8,
            ),
        ),
    ),
    ScenarioSpec(
        slug="05-bot-ask-twice",
        title="Caller asks are you a real person twice",
        goal="Deflect once, then honest AI receptionist, then help",
        turns=(
            CallerTurn("Are you a real person?"),
            CallerTurn("No but are you a bot? I really want to know."),
            CallerTurn("Okay, just a check-up then."),
        ),
    ),
    ScenarioSpec(
        slug="06-unknown-fee",
        title="Caller asks a price not in the fee table",
        goal="No guess; offer callback; take_message",
        turns=(
            CallerTurn("How much is a white filling on a back tooth?"),
            CallerTurn(
                "Yeah please, I'm Alex, oh four one two one one one two two two."
            ),
        ),
    ),
    ScenarioSpec(
        slug="07-swollen-swallow",
        title="Facial swelling and trouble swallowing",
        goal="Fillers and jokes off; emergency path; do not book",
        turns=(CallerTurn("My face is swollen and I'm having trouble swallowing."),),
    ),
    ScenarioSpec(
        slug="08-cancel-24h-fee",
        title="Cancellation inside 24 hours — $50 fee must be raised",
        goal="Warmly raise the fifty dollar fee; never waive it",
        seed_cancel_24h=True,
        turns=(
            CallerTurn(
                "Hi, it's Priya Nair, oh four one three zero zero zero two two two. "
                "I need to cancel tomorrow's appointment."
            ),
            CallerTurn("Yeah that's fine, just cancel."),
        ),
    ),
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


def _real_audio_path(path_value: str | None, directory: Path) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = directory / path
    if path.is_file() and path.stat().st_size > 44:
        return str(path)
    return None


def write_metrics(
    rows: list[CallMetrics],
    out_dir: Path,
    *,
    live_audio: bool,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cleaned: list[CallMetrics] = []
    for row in rows:
        copy = CallMetrics(**asdict(row))
        copy.audio_recording = _real_audio_path(row.audio_recording, out_dir)
        if row.audio_recording and copy.audio_recording is None:
            copy.notes = [
                *row.notes,
                "audio_recording omitted — file missing or empty (not a labelled stub)",
            ]
        cleaned.append(copy)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live_audio": live_audio,
        "target_longest_silence_ms": 800,
        "ack_pool": list(ACKS),
        "calls": [asdict(row) for row in cleaned],
        "record_command": (
            "set -a && source .env.local && set +a && "
            "uv run python src/never_silent_harness.py --live --out demos/never-silent"
        ),
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "README.md").write_text(
        _readme_text(cleaned, live_audio), encoding="utf-8"
    )
    return payload


def _readme_text(rows: list[CallMetrics], live_audio: bool) -> str:
    lines = [
        "# Never-silent Ava — scenario recordings",
        "",
        "Owner scenarios. Offline metrics come from the filler ladder / "
        "backchannel / $50 cancel path. Live WAVs are mixed from the LiveKit "
        "room (scripted TTS caller + dispatched `ava-and-leo`).",
        "",
        "## Record real audio",
        "",
        "```bash",
        "cp .env.example .env.local   # then fill LIVEKIT_* and OPENAI_API_KEY",
        "set -a && source .env.local && set +a",
        "uv run python src/never_silent_harness.py --live --out demos/never-silent",
        "```",
        "",
        "That command loads `.env.local`, starts a local Ava worker unless you "
        "pass `--no-spawn-worker`, creates one LiveKit room per scenario, "
        "dispatches agent `ava-and-leo`, records a local WAV, and starts "
        "audio-only room-composite egress when `EGRESS_S3_*` is set.",
        "",
        "One scenario:",
        "",
        "```bash",
        "uv run python src/never_silent_harness.py --live --scenario 01-slow-availability",
        "```",
        "",
        f"Live audio this run: **{'yes' if live_audio else 'no'}**.",
        "",
    ]
    for row in rows:
        audio = row.audio_recording or "*(no WAV — run --live on a desk with secrets)*"
        lines.append(f"- `{row.slug}` — {row.title} — {audio}")
    lines.append("")
    return "\n".join(lines)


async def _run_ladder_until(
    state: CallState,
    factory,
    *,
    booking: Any | None = None,
    until_stage: int | None = None,
    hang_forever: bool = False,
) -> tuple[dict[str, Any], Any, FakeSpeaker]:
    clock = FakeClock()
    speaker = FakeSpeaker(clock=clock)
    ladder = FillerLadder(
        state, speaker=speaker, booking=booking, clock=clock, sleeper=clock.sleep
    )
    release = asyncio.Event()

    async def wrapped():
        if hang_forever:
            await asyncio.Event().wait()
            return {"ok": False, "reason": "timeout"}
        if until_stage is not None:
            await release.wait()
        return await factory()

    task = asyncio.create_task(ladder.dispatch(wrapped))
    if until_stage is not None:
        for _ in range(80):
            await asyncio.sleep(0)
            if until_stage in ladder.trace.stages_spoken:
                break
        release.set()
    result, trace = await task
    return dict(result), trace, speaker


async def run_all(out_dir: Path = OUT_DIR) -> list[CallMetrics]:
    """Offline machinery metrics for the eight owner scenarios (no WAV claim)."""
    today = date(2026, 9, 15)
    client = PracticeClient(mode="mock")
    seed_mock_diary(client, today=today, days=14)
    booking = CachedBookingProvider(
        MemoryBookingProvider(client),
        today_fn=lambda: today,
    )
    await booking.prewarm()
    metrics: list[CallMetrics] = []

    async def cached_check():
        return await booking.check_availability(
            branch="shellharbour",
            appointment_type="check-up",
            date_range="this week",
        )

    # 1. Forced ~6s lookup — ladder reaches stage 4 (annoyed at the computer).
    state = CallState(branch="shellharbour")
    state.pick_opening()
    _result, trace, _spk = await _run_ladder_until(
        state, cached_check, booking=booking, until_stage=4
    )
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[0].slug,
            title=OWNER_SCENARIOS[0].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=[
                OWNER_SCENARIOS[0].goal,
                "ladder reached stage 4 on a 6s-class wait",
                f"stages={trace.stages_spoken}",
            ],
        )
    )

    # 2. Hang until stage 5 take_message.
    state = CallState(
        branch="shellharbour", caller_name="Jordan", caller_mobile="0413000111"
    )

    async def unused_check():
        return await cached_check()

    result, trace, _spk = await _run_ladder_until(
        state, unused_check, booking=booking, hang_forever=True
    )
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[1].slug,
            title=OWNER_SCENARIOS[1].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            stage5_fallback=trace.stage5_fallback,
            notes=[OWNER_SCENARIOS[1].goal, f"fallback={result.get('fallback')}"],
        )
    )

    # 3. ~20s toothache monologue — backchannels, no turn-taking.
    state = CallState(branch="shellharbour")
    story = OWNER_SCENARIOS[2].turns[0].text
    state.observe_user_text(story)
    clock = {"t": 0.0}
    sched = BackchannelScheduler(state, clock=lambda: clock["t"])
    sched.on_user_state(True, pain=True)
    fired: list[str] = []
    for tick in (2.6, 5.2, 8.0, 11.0, 14.0, 17.0, 20.0):
        clock["t"] = tick
        line = sched.maybe_fire(clock["t"], pain=True)
        if line:
            fired.append(line)
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[2].slug,
            title=OWNER_SCENARIOS[2].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=0.0,
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=True,
            notes=[
                OWNER_SCENARIOS[2].goal,
                f"backchannels={fired}",
                "longest_silence_gap excluded while caller talking",
            ],
        )
    )

    # 4. Three barge-ins — resume pool, no sentence restart.
    state = CallState(branch="shellharbour")
    resumes = [state.mark_interrupted() for _ in range(3)]
    assert all(item in BARGE_IN_RESUME for item in resumes)
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[3].slug,
            title=OWNER_SCENARIOS[3].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=0.0,
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=True,
            notes=[OWNER_SCENARIOS[3].goal, f"resumes={resumes}"],
        )
    )

    # 5. Bot ask twice.
    state = CallState(branch="shellharbour")
    state.observe_user_text("Are you a real person?")
    state.observe_user_text("are you a bot")
    state.pick_ack()
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[4].slug,
            title=OWNER_SCENARIOS[4].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=0.0,
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=True,
            notes=[OWNER_SCENARIOS[4].goal, f"bot_ask_count={state.bot_ask_count}"],
        )
    )

    # 6. Price not in the fee table.
    state = CallState(
        branch="shellharbour", caller_name="Alex", caller_mobile="0412111222"
    )
    unknown = quote_fee("white filling")

    async def leave():
        return await booking.take_message(
            branch="shellharbour",
            name="Alex",
            mobile="0412111222",
            reason="quote for white filling on a back tooth",
        )

    _msg, trace, _spk = await _run_ladder_until(state, leave, booking=booking)
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[5].slug,
            title=OWNER_SCENARIOS[5].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=[
                OWNER_SCENARIOS[5].goal,
                f"quote_status={unknown.get('status')}",
            ],
        )
    )

    # 7. Swollen + swallow — do not book.
    state = CallState(branch="shellharbour")
    state.observe_user_text("swollen and trouble swallowing")

    async def blocked():
        return {"ok": False, "reason": "do_not_book", "action": "call_000"}

    _blocked, trace, _spk = await _run_ladder_until(state, blocked, booking=booking)
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[6].slug,
            title=OWNER_SCENARIOS[6].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=[OWNER_SCENARIOS[6].goal, f"urgency={state.urgency_level}"],
        )
    )

    # 8. Cancel inside 24h — $50 fee.
    state = CallState(branch="shellharbour")
    seeded = seed_inside_24h_booking(client)

    async def cancel():
        assert seeded is not None
        return await booking.cancel_appointment(booking_id=seeded.booking_id)

    cancelled, trace, _spk = await _run_ladder_until(state, cancel, booking=booking)
    metrics.append(
        CallMetrics(
            slug=OWNER_SCENARIOS[7].slug,
            title=OWNER_SCENARIOS[7].title,
            time_to_first_audio_ms=0.0,
            longest_silence_gap_ms=_silence_gap_ms(trace),
            stock_phrases_used=list(state.stock_phrases_used),
            first_audio_before_dispatch=trace.audio_before_dispatch,
            notes=[
                OWNER_SCENARIOS[7].goal,
                f"fee_applies={cancelled.get('fee_applies')}",
                f"fee_aud={cancelled.get('fee_aud')}",
            ],
        )
    )

    for row in metrics:
        row.notes.append(
            "offline machinery run — WAV only from --live with LiveKit + OpenAI secrets"
        )

    write_metrics(metrics, out_dir, live_audio=False)
    return metrics


def _select_specs(slugs: list[str] | None) -> list[ScenarioSpec]:
    if not slugs:
        return list(OWNER_SCENARIOS)
    wanted = set(slugs)
    selected = [spec for spec in OWNER_SCENARIOS if spec.slug in wanted]
    missing = wanted - {spec.slug for spec in selected}
    if missing:
        raise SystemExit(f"Unknown scenario(s): {', '.join(sorted(missing))}")
    return selected


async def run_live(
    out_dir: Path,
    *,
    slugs: list[str] | None = None,
    spawn_worker: bool = True,
) -> list[CallMetrics]:
    from never_silent_recorder import record_all

    specs = _select_specs(slugs)
    captured = await record_all(specs, out_dir, spawn_worker=spawn_worker)
    by_slug = {row["slug"]: row for row in captured}
    rows: list[CallMetrics] = []
    for spec in specs:
        data = by_slug[spec.slug]
        rows.append(
            CallMetrics(
                slug=spec.slug,
                title=spec.title,
                time_to_first_audio_ms=float(data.get("time_to_first_audio_ms") or 0),
                longest_silence_gap_ms=float(data.get("longest_silence_gap_ms") or 0),
                stock_phrases_used=list(data.get("stock_phrases_used") or []),
                first_audio_before_dispatch=True,
                stage5_fallback=spec.slug == "02-tool-timeout",
                audio_recording=data.get("audio_recording"),
                notes=list(data.get("notes") or []),
            )
        )
    write_metrics(rows, out_dir, live_audio=True)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Never-silent Ava: offline metrics or live WAV recorder.",
    )
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument(
        "--live",
        action="store_true",
        help="Dispatch ava-and-leo, drive a TTS caller, write real WAVs.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="Owner slug (repeatable). Default: all eight.",
    )
    parser.add_argument(
        "--spawn-worker",
        dest="spawn_worker",
        action="store_true",
        default=True,
        help="Start `src/agent.py dev` for the live run (default).",
    )
    parser.add_argument(
        "--no-spawn-worker",
        dest="spawn_worker",
        action="store_false",
        help="Use an already-running ava-and-leo worker.",
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.live:
        if not live_audio_ready():
            print(
                "Missing LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET / "
                "OPENAI_API_KEY. Load them from .env.local:\n"
                "  set -a && source .env.local && set +a\n"
                "  uv run python src/never_silent_harness.py --live"
            )
            rows = asyncio.run(run_all(out))
            print(f"Wrote {len(rows)} offline metrics to {out} (no WAVs).")
            return 2
        rows = asyncio.run(
            run_live(out, slugs=args.scenario, spawn_worker=args.spawn_worker)
        )
        print(f"Wrote {len(rows)} live recordings to {out}")
        for row in rows:
            print(f"  {row.slug}: {row.audio_recording}")
        return 0

    rows = asyncio.run(run_all(out))
    print(f"Wrote {len(rows)} scenario metrics to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
