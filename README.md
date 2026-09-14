<a href="https://livekit.io/">
  <img src="./.github/assets/livekit-mark.png" alt="LiveKit logo" width="100" height="100">
</a>

# Ava and Leo — Shellharbour Dentists

LiveKit Agents project with two personas:

| `AGENT_PERSONA` | Who | Path |
|-----------------|-----|------|
| `ava` | Generic voice assistant | AssemblyAI STT + Groq LLM + Cartesia TTS |
| `leo` | Phone receptionist for Shellharbour Dentists (Barrack Heights, Dapto, Woonona) | OpenAI Realtime (`gpt-realtime`, voice **marin**) |

Unset `AGENT_PERSONA` defaults to **leo on telephony** (SIP inbound or outbound) and **ava** on web/console. The clinic portal always asks for Leo (`persona: leo` in the room token).

Leo's spoken style, branch facts, fee catalogue, and tool rules live in [`src/persona.py`](src/persona.py). Parking, hours, and dentist names are filled from the official sites (2026-09-14). Fields marked `VERIFY` are unknown — Leo must not invent them. Say **confirmed** only after a book/reschedule/cancel tool returns `confirmed: true`.

## Dev setup

This project uses [`uv`](https://docs.astral.sh/uv/). Copy `.env.example` to `.env.local` (never commit secrets).

```bash
cp .env.example .env.local
uv sync
```

### Required credentials

| Variable | Used by |
|----------|---------|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Agent worker, portal “Call Leo”, outbound `make_call.py`, evals |
| `OPENAI_API_KEY` | Leo (OpenAI Realtime) |
| `ASSEMBLYAI_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY` | Ava pipeline only |

Load LiveKit Cloud credentials with the [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/):

```bash
lk cloud auth
lk app env --write --destination .env.local
```

### Local clinic portal (agent + diary + Call Leo)

One command starts the mock diary, the Leo worker in **dev** mode, and the staff portal:

```bash
# .env.local should include LIVEKIT_*, OPENAI_API_KEY
# run_local.py defaults: AGENT_PERSONA=leo, PRACTICE_SOFTWARE=mock, LEO_REALTIME_VOICE=marin
# The worker subcommand is `dev` (injected automatically — do not pass it yourself).
uv run python src/run_local.py
```

Then open **http://127.0.0.1:8787**

| What to do | How |
|------------|-----|
| See a branch | Use the Barrack Heights / Dapto / Woonona tabs. Address, phone, hours, parking, and dentists are on the left. |
| Book | Pick a date, tap **Book** on an open slot, enter name + mobile, confirm. The diary only says confirmed after the mock mutation succeeds. |
| Reschedule / cancel | On a booked row, **Reschedule** (moves to an open slot that day) or **Cancel**. |
| Talk to Leo | Tap **Call Leo**. Allow the microphone. The portal mints a LiveKit token on the server (keys never go in frontend source) and dispatches `ava-and-leo`. Hang up when finished. |

Auth: empty `PORTAL_PASSWORD` is open **on localhost only**. Set `PORTAL_PASSWORD` before exposing the portal. Do not put LiveKit secrets in the browser.

You can also run the two processes yourself (they share `.data/mock_diary.json`):

```bash
AGENT_PERSONA=leo PRACTICE_SOFTWARE=mock LEO_REALTIME_VOICE=marin \
  uv run python src/agent.py dev
# other terminal
AGENT_PERSONA=leo PRACTICE_SOFTWARE=mock \
  uv run uvicorn portal:app --app-dir src --host 127.0.0.1 --port 8787
```

### SIP / telephony (Leo)

1. Create inbound and outbound trunks in LiveKit Cloud. See [SIP trunk setup](https://docs.livekit.io/telephony/start/sip-trunk-setup/) and [outbound calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/).
2. Point dispatch rules at this agent (`agent_name` is `ava-and-leo`).
3. Set:

```bash
SIP_OUTBOUND_TRUNK_ID=ST_xxxx          # lk sip outbound list
SIP_DID_MAP=+61242169911:shellharbour,+61242880737:dapto,+61242844486:woonona
SIP_TRANSFER_TO=+61242169911           # optional cold-transfer destination
PRACTICE_SOFTWARE=disconnected         # production default; set mock to use the seeded diary
AGENT_PERSONA=leo                      # optional; telephony already defaults to leo
LEO_REALTIME_VOICE=marin               # female AU receptionist (OpenAI Realtime)
```

Inbound branch mapping uses the SIP participant attribute `sip.trunkPhoneNumber` (the DID the caller dialled).

`PRACTICE_SOFTWARE` unset: **mock** on local/dev/portal/console, **disconnected** on telephony and `uv run python src/agent.py start`. Mock never invents slots; it seeds the next two weeks from real dentist names and published hours.

## Run the agent only

Console (Ava by default):

```bash
uv run python src/agent.py console
```

Leo in console (Realtime, female marin voice, no SIP):

```bash
AGENT_PERSONA=leo uv run python src/agent.py console
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

On outbound, Leo waits for the callee to speak first. On inbound, Leo greets as the mapped branch.

## Tests

```bash
uv run pytest tests/test_persona.py tests/test_fees.py tests/test_practice.py tests/test_sip.py tests/test_make_call.py tests/test_leo.py tests/test_portal.py tests/test_run_local.py -v
uv run pytest            # includes Ava evals; needs LIVEKIT_* in CI
```

Unit tests cover persona switching, branch facts (parking/hours/dentists), canned fees, DID mapping (including a MagicMock console participant), mock book/reschedule/cancel `confirmed=true`, and the marin voice default.

## Layout

```
src/agent.py          # entrypoint: Ava pipeline or Leo Realtime
src/persona.py        # Leo prompts, branches, fees (source of truth)
src/leo.py            # LeoReceptionist + office-system tools
src/practice.py       # disconnected / mock practice software
src/portal.py         # FastAPI clinic desk (facts, diary, Call Leo token)
src/portal_static/    # portal UI
src/run_local.py      # one-command agent + portal
src/sip_utils.py      # DID map, disconnect handling
src/make_call.py      # outbound dispatch + SIP dial
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
