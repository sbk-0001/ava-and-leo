<a href="https://livekit.io/">
  <img src="./.github/assets/livekit-mark.png" alt="LiveKit logo" width="100" height="100">
</a>

# Ava — Shellharbour Dentists receptionist

Ava is the phone receptionist for the Shellharbour Dentists group (Barrack Heights, Dapto, Woonona): OpenAI Realtime (`gpt-realtime`, voice **marin**), low-latency speech-to-speech. The older generic AssemblyAI/Groq/Cartesia assistant is a secondary pipeline (`AGENT_PERSONA=generic`, alias `ava-generic`). `AGENT_PERSONA=leo` still maps to Ava so old env files keep working.

| `AGENT_PERSONA` | Who | Path |
|-----------------|-----|------|
| `ava` (default on telephony and the clinic portal) | Dental receptionist callers hear | OpenAI Realtime (`gpt-realtime`, **marin**) |
| `generic` (default on console/web when unset) | Non-dental assistant | AssemblyAI STT + Groq LLM + Cartesia TTS |
| `leo` (alias) | Same as `ava` | OpenAI Realtime |
| `ava-generic` (alias) | Same as `generic` | AssemblyAI / Groq / Cartesia |

Unset `AGENT_PERSONA` defaults to **ava on telephony** (SIP inbound or outbound) and **generic** on web/console. The clinic portal always dispatches Ava (`persona: ava` in the room token).

Ava's spoken style, branch facts, fee catalogue, suburb routing, mock weekday roster, and tool rules live in [`src/persona.py`](src/persona.py). Parking, hours, and dentist names are filled from the official sites (2026-09-14). Fields marked `VERIFY` are unknown — Ava must not invent them. Say **confirmed** only after a book/reschedule/cancel tool returns `confirmed: true`.

Callers are not locked to the clinic they dialled. Ava greets as the Shellharbour Dentists group, captures the reason for the call, asks which suburb they are near, suggests the nearest clinic and dentists rostered there that day, and if a preferred dentist (for example Dr Mohit Tolani) is at another group clinic that day, she offers the farther site. The mock diary seeds slots from that weekday roster so a Monday Dapto-area caller who wants Dr Mohit is offered Shellharbour (Barrack Heights).

## Voice: marin

OpenAI Realtime has **no Australian-specific voice**. Built-in IDs (do not invent others) are `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`, `marin`, and `cedar` — see the [LiveKit OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/plugins/openai/) and [OpenAI voice options](https://developers.openai.com/docs/guides/realtime-conversations#voice-options). OpenAI recommends **marin** or **cedar** for best quality on `gpt-realtime`; marin is the feminine pair. Older female-leaning voices (`coral`, `shimmer`, `sage`) are lower quality on this model. Ava stays on **marin**; NSW/Illawarra English comes from persona instructions.

## Low-latency Realtime (not zero latency)

Ava stays on OpenAI Realtime speech-to-speech. Settings aimed at snappy, interruptible phone turns (verified against [OpenAI Realtime turn detection](https://docs.livekit.io/agents/models/realtime/plugins/openai/#turn-detection) and [interruption in realtime mode](https://docs.livekit.io/agents/logic/turns/#interruption-in-realtime-mode)):

- Server VAD with `silence_duration_ms=400` and `threshold=0.7` (telephony-friendly)
- `interrupt_response=True` so the caller can barge in
- `AgentSession` `turn_detection="realtime_llm"` with interruptions enabled
- Spoken replies kept to one or two sentences; parking/hours/listed dentists are instant facts (no tool round-trip before speaking). Today's roster and diary slots always use tools.

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
| Talk to Ava | Tap **Call Ava**. Allow the microphone. The portal mints a LiveKit token on the server (keys never go in frontend source) and dispatches `ava-and-leo`. Hang up when finished. |

Auth: empty `PORTAL_PASSWORD` is open **on localhost only**. Set `PORTAL_PASSWORD` before exposing the portal. Do not put LiveKit secrets in the browser.

You can also run the two processes yourself (they share `.data/mock_diary.json`):

```bash
AGENT_PERSONA=ava PRACTICE_SOFTWARE=mock AVA_REALTIME_VOICE=marin \
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
SIP_TRANSFER_TO=+61242169911           # optional cold-transfer destination
PRACTICE_SOFTWARE=disconnected         # production default; set mock to use the seeded diary
AGENT_PERSONA=ava                      # optional; telephony already defaults to ava
AVA_REALTIME_VOICE=marin               # female AU receptionist (OpenAI Realtime)
```

Inbound branch mapping uses the SIP participant attribute `sip.trunkPhoneNumber` (the DID the caller dialled).

`PRACTICE_SOFTWARE` unset: **mock** on local/dev/portal/console, **disconnected** on telephony and `uv run python src/agent.py start`. Mock never invents slots; it seeds the next two weeks from real dentist names and published hours.

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

On outbound, Ava waits for the callee to speak first. On inbound, Ava greets as the Shellharbour Dentists group (the dialled DID is a hint, not a lock).

## Tests

```bash
uv run pytest tests/test_persona.py tests/test_fees.py tests/test_practice.py tests/test_routing.py tests/test_sip.py tests/test_make_call.py tests/test_ava_receptionist.py tests/test_portal.py -v
uv run pytest            # includes generic-pipeline evals; needs LIVEKIT_* in CI
```

Unit tests cover persona switching, Ava naming, human VOICE_INSTRUCTIONS, suburb → clinic routing, cross-clinic preferred-dentist offers, branch facts (parking/hours/dentists), canned fees, DID mapping (including a MagicMock console participant), mock book/reschedule/cancel `confirmed=true`, roster-aware diary seeding, and the marin voice default.

## Layout

```
src/agent.py              # entrypoint: Ava Realtime or generic pipeline
src/persona.py            # Ava prompts, branches, fees (source of truth)
src/ava_receptionist.py   # AvaReceptionist + office-system tools
src/practice.py           # disconnected / mock practice software
src/portal.py             # FastAPI clinic desk (facts, diary, Call Ava token)
src/portal_static/        # portal UI
src/run_local.py          # one-command agent + portal
src/sip_utils.py          # DID map, disconnect handling
src/make_call.py          # outbound dispatch + SIP dial
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
