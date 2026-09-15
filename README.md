<a href="https://livekit.io/">
  <img src="./.github/assets/livekit-mark.png" alt="LiveKit logo" width="100" height="100">
</a>

# Ava — Shellharbour · Dapto · Woonona receptionist

Ava is the full-duplex phone receptionist for **Shellharbour Dentists**, **Dapto Dentists**, and **Woonona Dentists**: OpenAI Realtime (`gpt-realtime`, voice **marin**), Australian, Illawarra local. **The inbound DID maps the branch at session start into `CallState` before she speaks.** She answers as that clinic — "Morning, Shellharbour Dentists, Ava speaking!" — and never runs a "which clinic" menu. She only offers another site if the caller raises it or a suburb clearly suits one better (demo: Figtree → Dapto).

The older generic AssemblyAI/Groq/Cartesia assistant is a secondary pipeline (`AGENT_PERSONA=generic`, alias `ava-generic`). `AGENT_PERSONA=leo` still maps to Ava so old env files keep working.

| `AGENT_PERSONA` | Who | Path |
|-----------------|-----|------|
| `ava` (default on telephony and the clinic portal) | Dental receptionist callers hear | OpenAI Realtime (`gpt-realtime`, **marin**) |
| `generic` (default on console/web when unset) | Non-dental assistant | AssemblyAI STT + Groq LLM + Cartesia TTS |
| `leo` (alias) | Same as `ava` | OpenAI Realtime |
| `ava-generic` (alias) | Same as `generic` | AssemblyAI / Groq / Cartesia |

Unset `AGENT_PERSONA` defaults to **ava on telephony** (SIP inbound or outbound) and **generic** on web/console. The clinic portal always dispatches Ava (`persona: ava` in the room token).

Spoken identity is the **verbatim** block in [`src/instructions/ava_receptionist.md`](src/instructions/ava_receptionist.md) (`{{BRANCH_NAME}}` interpolated at session start — do not tidy it). Clinic facts and the fee table live in [`src/persona.py`](src/persona.py) and match the product brief only. Fields marked `VERIFY` are unknown — Ava must not invent them. Flow control lives in [`src/call_state.py`](src/call_state.py) (hard ask counters, urgency, suburb offers), not in the prompt. Say **confirmed** only after a book/reschedule/cancel tool returns `confirmed: true`. Quote **only** the fee table; unknown services return `status: unknown` and she offers a callback.

## Low-latency Realtime (not zero latency)

Ava stays on OpenAI Realtime speech-to-speech (`marin`). Settings aimed at human, interruptible phone turns (verified against [OpenAI Realtime turn detection](https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection) and [interruption in realtime mode](https://docs.livekit.io/agents/logic/turns/#interruption-in-realtime-mode)):

- **Server VAD** with `silence_duration_ms=500` (450–550ms window) and `interrupt_response=True` so the caller can cut her off mid-word (target: Ava stops in under 100ms). Resume with a pooled "sorry, go on" / "yep yep, sorry" — never restart the cut-off sentence
- Speech rate `speed=0.9`; temperature `0.95` (0.9–1.0)
- `AgentSession` `turn_detection="realtime_llm"` with interruptions enabled
- **Never-silent tools:** stage-1 filler audio starts *before* the diary network call (`first-audio-ts` < `tool-dispatch-ts`). A code timer then ladders more fillers at 1.2s / 3s / 6s / 12s. Stage 5 is a real `take_message` fallback, not just a line
- 14-day availability cache per branch, refreshed every 2–3 minutes; `check_availability` is cache-first; books re-verify live
- Quiet office bed at about −47.5 dBFS plus keyboard clatter while a tool runs; backchannels ("mm", "yep") on a side channel while the caller talks
- After ~20 Realtime conversation items, older turns are truncated (`ChatContext.truncate` + `update_chat_ctx`). **`CallState` (branch, name, mobile, booking) is not wiped** — it is re-injected as instructions and as a compact recap
- On `rate_limit_exceeded`, Ava covers ("just a sec"), trims, and retries with backoff instead of hanging silent
- Target: under 800ms to first audio each turn

## Noise cancellation (Call Ava and SIP)

Default is LiveKit Cloud **Krisp BVC** on web participants and **BVCTelephony** on SIP. That is the [documented RoomOptions path](https://docs.livekit.io/transport/media/noise-cancellation/) and does not use the ai-coustics enhancer.

Do **not** enable ai-coustics unless you have a license. `ai_coustics.audio_enhancement` without valid enhancer auth previously hung `session.start` with **0 published audio tracks** (silent Ava on the portal). Missing auth now fails soft: Ava still publishes audio.

| `AVA_NOISE_CANCELLATION` | Effect |
|--------------------------|--------|
| unset / `1` / `krisp` | Krisp BVC (web) / BVCTelephony (SIP). Safe default. Import or runtime failure → empty `RoomOptions()` + warning. |
| `0` / `off` | Empty `RoomOptions()` — no filter. |
| `ai_coustics` | QUAIL_VF_S **only** when `AI_COUSTICS_LICENSE_KEY` is set. Otherwise falls back to Krisp. Any failure → empty `RoomOptions()`. |

Requires `livekit-plugins-noise-cancellation` (already in this project). LiveKit Cloud bills Krisp NC on the agent input path.

## Dev setup

This project uses [`uv`](https://docs.astral.sh/uv/). Copy `.env.example` to `.env.local` (never commit secrets).

```bash
cp .env.example .env.local
uv sync
```

### Required credentials

| Variable | Used by |
|----------|---------|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Agent worker, portal “Call Ava”, outbound `make_call.py`, evals |
| `OPENAI_API_KEY` | Ava (OpenAI Realtime) |
| `OPENAI_LIVE_API_KEY` | Optional. Live project key; must differ from `OPENAI_DEMO_API_KEY` / `OPENAI_HARNESS_API_KEY` |
| `LIVEKIT_SIP_HOST` / `EXPECTED_SIP_HOST` | Telnyx SIP host (`5sft82r5337.sip.livekit.cloud`) |
| `OVERFLOW_NUMBER` | Optional. 429 circuit-breaker overflow destination |
| `ASSEMBLYAI_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY` | Generic pipeline only |

Load LiveKit Cloud credentials with the [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/):

```bash
lk cloud auth
lk app env --write --destination .env.local
```

### Local clinic portal (agent + diary + Call Ava)

One command starts the mock diary, the Ava worker, and the staff portal:

```bash
# .env.local should include LIVEKIT_*, OPENAI_API_KEY
# run_local.py defaults: AGENT_PERSONA=ava, PRACTICE_SOFTWARE=mock, AVA_REALTIME_VOICE=marin
uv run python src/run_local.py
```

Then open **http://127.0.0.1:8787**

| What to do | How |
|------------|-----|
| See a branch | Use the Barrack Heights / Dapto / Woonona tabs. Address, phone, hours, parking, and dentists are on the left. |
| Book | Pick a date, tap **Book** on an open slot, enter name + mobile, confirm. The diary only says confirmed after the mock mutation succeeds. |
| Reschedule / cancel | On a booked row, **Reschedule** (moves to an open slot that day) or **Cancel**. |
| Talk to Ava | Tap **Call Ava**. Allow the microphone. The portal mints a LiveKit token on the server (keys never go in frontend source) and dispatches `ava-and-leo`. The **Live call** panel shows the transcript and booking activity for **web and inbound SIP/phone** (LiveKit `ava.desk` packets plus a local HTTP/SSE bus). Hang up when finished. |

Auth: empty `PORTAL_PASSWORD` is open **on localhost only**. Set `PORTAL_PASSWORD` before exposing the portal. Do not put LiveKit secrets in the browser. SSE (`/api/desk/stream`) uses the same cookie, or `?token=` (cookie digest or `PORTAL_PASSWORD`).

When the agent worker and portal run as two processes (or the desk is opened through a tunnel), the worker must POST desk events to the **local** portal:

```bash
DESK_EVENTS_URL=http://127.0.0.1:8787
# or PORTAL_URL=http://127.0.0.1:8787
```

`run_local.py` sets both. Inbound SIP rooms (`call-+61…`) are not in the browser Call Ava room — the HTTP bus is what puts those turns on the desk.

You can also run the two processes yourself (they share `.data/mock_diary.json`):

```bash
AGENT_PERSONA=ava PRACTICE_SOFTWARE=mock AVA_REALTIME_VOICE=marin \
  DESK_EVENTS_URL=http://127.0.0.1:8787 \
  uv run python src/agent.py dev
# other terminal
AGENT_PERSONA=ava PRACTICE_SOFTWARE=mock \
  uv run uvicorn portal:app --app-dir src --host 127.0.0.1 --port 8787
```

### SIP / telephony (Ava)

1. Create inbound and outbound trunks in LiveKit Cloud. See [SIP trunk setup](https://docs.livekit.io/telephony/start/sip-trunk-setup/) and [outbound calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/).
2. Point dispatch rules at this agent (`agent_name` is `ava-and-leo`).
3. Set:

```bash
SIP_OUTBOUND_TRUNK_ID=ST_xxxx          # lk sip outbound list
SIP_DID_MAP=+61242169911:shellharbour,+61242880737:dapto,+61242844486:woonona
SIP_TRANSFER_TO=+61242169911           # default cold-transfer; per-branch SIP_TRANSFER_DAPTO etc. win
AVA_KILL_SWITCH=0                      # 1 = skip Ava and transfer_to_human immediately
BOOKING_PROVIDER=memory                # zavy360 = stub (see docs/zavy360.md)
PRACTICE_SOFTWARE=mock                 # local demo diary; production telephony defaults disconnected
AGENT_PERSONA=ava
AVA_REALTIME_VOICE=marin
```

Inbound DID mapping uses `sip.trunkPhoneNumber` (the number the caller dialled). That id is written to `CallState.branch` **before** the greeting. Unknown DIDs fall back to Shellharbour.

`BOOKING_PROVIDER=memory` seeds a week of availability for all three branches (Shellharbour dentists by name; Dapto/Woonona as "available dentist" because the brief does not name those clinicians). `BOOKING_PROVIDER=zavy360` is stubbed and never invents slots — notes in [`docs/zavy360.md`](docs/zavy360.md).

Kill-switch: `AVA_KILL_SWITCH=1` makes `transfer_to_human` fire on enter. `end_call` deletes the LiveKit room (or shuts the job down). Both are wired, not stubs.

Call logs: every turn gets a UTC timestamp (never NULL) under `AVA_CALL_LOG_DIR` (default `.data/call_logs`) and, if configured, Supabase `ava_transcript_turns.created_at`. The supabase client is imported in the agent **prewarm** so shutdown does not block the event loop on a cold import.

## OpenAI Realtime token limits (long demos)

A chatty 10–15 minute call can hit the OpenAI Realtime **tokens-per-minute (TPM)** budget. Observed on a live call: late replies jumped to 12–15s with:

```
RealtimeError: response failed: [tokens] rate_limit_exceeded
```

That is usage-tier TPM, not a bug in barge-in. This agent reduces pressure by trimming older Realtime items and keeping booking facts in `CallState`. For long live demos, **raise the OpenAI Realtime TPM limit** on the project (platform.openai.com → Limits / usage tier). There is no client-side TPM override; `OPENAI_REALTIME_TPM` in `.env.example` is a reminder only.

## Run the agent only

Console (generic pipeline by default):

```bash
uv run python src/agent.py console
```

Ava in console (Realtime, female marin voice, no SIP):

```bash
AGENT_PERSONA=ava uv run python src/agent.py console
```

Worker for frontend or telephony:

```bash
uv run python src/agent.py dev     # development (mock diary unless telephony)
uv run python src/agent.py start   # production (disconnected diary unless PRACTICE_SOFTWARE=mock)
```

## Outbound calls

Start the worker, then:

```bash
uv run python src/make_call.py --to +61400000000
uv run python src/make_call.py --to +61400000000 --branch dapto
```

The script [dispatches](https://docs.livekit.io/agents/server/agent-dispatch/) agent `ava-and-leo` and calls [`CreateSIPParticipant`](https://docs.livekit.io/telephony/making-calls/outbound-calls/) with `wait_until_answered=True`. Failed dials raise `TwirpError` / `SipCallError` (busy, no answer, trunk failure). Mid-call hangups are handled per [SIP disconnect docs](https://docs.livekit.io/telephony/making-calls/outbound-calls/#mid-call-disconnections): `USER_UNAVAILABLE` and `SIP_TRUNK_FAILURE` explicitly shut down the job.

On outbound, Ava waits for the callee to speak first. On inbound, Ava greets as the **mapped branch**.

## Tests

```bash
uv run pytest tests/test_persona.py tests/test_fees.py tests/test_practice.py tests/test_sip.py tests/test_make_call.py tests/test_ava_receptionist.py tests/test_portal.py tests/test_room_options.py tests/test_call_state.py tests/test_booking.py tests/test_instructions.py tests/test_call_log.py tests/test_demo_harness.py tests/test_realtime_hygiene.py tests/test_filler_ladder.py tests/test_availability_cache.py tests/test_phrase_pools.py tests/test_never_silent.py tests/test_never_silent_harness.py tests/test_never_silent_recorder.py -v
uv run pytest            # includes generic-pipeline evals; needs LIVEKIT_* in CI
uv run python src/demo_harness.py   # writes demos/thursday/*.md
uv run python src/never_silent_harness.py          # offline metrics for 8 owner scenarios
# Real WAVs (LiveKit room + dispatched ava-and-leo + TTS caller):
set -a && source .env.local && set +a
uv run python src/never_silent_harness.py --live --out demos/never-silent
```

Thursday demo transcripts (scripted harness, tools are real): [`demos/thursday/`](demos/thursday/).

Never-silent owner recordings (real WAV, not transcripts):

```bash
# .env.local must include LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, OPENAI_API_KEY
set -a && source .env.local && set +a
uv run python src/never_silent_harness.py --live --out demos/never-silent
# one scenario: --scenario 01-slow-availability
# worker already running: --no-spawn-worker
```

The recorder creates a LiveKit room, dispatches `ava-and-leo`, drives a TTS caller through the eight owner scripts, mixes a local WAV, and starts audio-only room-composite egress when `EGRESS_S3_*` is set. Without secrets it still writes `demos/never-silent/metrics.json` (no fake `{slug}.wav` labels).

## Layout

```
src/agent.py                         # entrypoint: DID → CallState before speech
src/instructions/ava_receptionist.md # verbatim session instructions
src/call_state.py                    # branch, asks, urgency, kill-switch, phrase pools
src/booking.py                       # BookingProvider + memory + Zavy360 stub
src/availability_cache.py            # 14-day cache-first availability
src/filler_ladder.py                 # speak-before-dispatch + 0/1.2/3/6/12s fillers
src/ambient.py                       # office bed + keyboard
src/backchannel.py                   # mm/yep while the caller talks
src/persona.py                       # brief-only facts + fee table + instruction loader
src/ava_receptionist.py              # tools (availability, book, cancel+$50, transfer, end_call)
src/realtime_hygiene.py              # Realtime context trim + TPM rate-limit recovery
src/call_log.py                      # timestamped replayable transcripts
src/practice.py                      # in-memory diary used by memory provider + portal
src/demo_harness.py                  # Thursday scenarios without live SIP
src/never_silent_harness.py          # 8 owner scenarios: offline metrics or --live WAVs
src/never_silent_recorder.py         # LiveKit room + egress + scripted TTS caller
src/portal.py / portal_static/       # staff desk + SSE live feed
src/desk_events.py                   # transcript/activity packets → LiveKit + HTTP bus
src/run_local.py                     # one-command agent + portal
src/sip_utils.py                     # DID map, disconnect handling
src/make_call.py                     # outbound dispatch + SIP dial
docs/zavy360.md                      # API investigation notes
demos/thursday/                      # scenario transcripts
demos/never-silent/                  # never-silent metrics + recording scaffold
```

## Docs

LiveKit Agents and telephony APIs change quickly. Use current docs:

- [OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/plugins/openai/)
- [Agent dispatch / room tokens](https://docs.livekit.io/agents/server/agent-dispatch/)
- [Outbound SIP calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/)
- [SIP participant attributes](https://docs.livekit.io/reference/telephony/sip-participant/)
- [Agent testing](https://docs.livekit.io/agents/start/testing/)

The [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/) `lk docs` subcommand (v2.15.0+) searches the same documentation.

## Deploy

The `Dockerfile` is ready for [LiveKit Cloud agents](https://docs.livekit.io/deploy/agents/). Set `AGENT_PERSONA` / `OPENAI_API_KEY` / SIP vars as secrets on the agent. The clinic portal is for local/dev (`run_local.py`); do not expose it without `PORTAL_PASSWORD`.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
