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

### Local clinic portal (agent + diary + Call Ava)

One command starts the mock diary, the dental receptionist worker, and the byte voice Ava desk:

```bash
# .env.local should include LIVEKIT_*, OPENAI_API_KEY
# run_local.py defaults: AGENT_PERSONA=leo, PRACTICE_SOFTWARE=mock, LEO_REALTIME_VOICE=marin
uv run python src/run_local.py
```

Then open **http://127.0.0.1:8787**

| What to do | How |
|------------|-----|
| See a branch | Use the Barrack Heights / Dapto / Woonona tabs. Address, phone, hours, parking, and dentists are on the left. |
| Book | Pick a date, tap **Book** on an open slot, enter name + mobile, confirm. The diary only says confirmed after the mock mutation succeeds. |
| Reschedule / cancel | On a booked row, **Reschedule** (moves to an open slot that day) or **Cancel**. |
| Talk to Ava | Tap **Call Ava**. Allow the microphone. The portal mints a LiveKit token on the server (keys never go in frontend source) and dispatches `ava-and-leo` with `persona: leo` (OpenAI Realtime, marin). Hang up when finished. |

Auth: empty `PORTAL_PASSWORD` is open **on localhost only**. Set `PORTAL_PASSWORD` before exposing the portal (required on Vercel). Do not put LiveKit secrets in the browser.

You can also run the two processes yourself (they share `.data/mock_diary.json` locally):

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
uv run pytest tests/test_persona.py tests/test_fees.py tests/test_practice.py tests/test_sip.py tests/test_make_call.py tests/test_leo.py tests/test_portal.py -v
uv run pytest            # includes Ava evals; needs LIVEKIT_* in CI
```

Unit tests cover persona switching, branch facts (parking/hours/dentists), canned fees, DID mapping (including a MagicMock console participant), mock book/reschedule/cancel `confirmed=true`, and the marin voice default.

## Layout

```
src/agent.py          # entrypoint: Ava pipeline or Leo Realtime
src/persona.py        # Leo prompts, branches, fees (source of truth)
src/leo.py            # LeoReceptionist + office-system tools
src/practice.py       # disconnected / mock practice software
src/diary_store.py    # file / Redis / optional Supabase diary backends
src/portal.py         # FastAPI byte voice Ava desk
src/main.py           # Vercel FastAPI entrypoint
src/portal_static/    # portal UI
src/run_local.py      # one-command agent + portal
src/sip_utils.py      # DID map, disconnect handling
src/make_call.py      # outbound dispatch + SIP dial
Dockerfile            # LiveKit Cloud worker (`src/agent.py start`)
vercel.json           # public portal
requirements-portal.txt
```

## Docs

LiveKit Agents and telephony APIs change quickly. Use current docs:

- [OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/plugins/openai/)
- [Agent dispatch / room tokens](https://docs.livekit.io/agents/server/agent-dispatch/)
- [Outbound SIP calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/)
- [SIP participant attributes](https://docs.livekit.io/reference/telephony/sip-participant/)
- [Agent testing](https://docs.livekit.io/agents/start/testing/)

The [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/) `lk docs` subcommand (v2.15.0+) searches the same documentation.

## Share with a partner

Give the partner the **same byte voice Ava desk** and the **same Call Ava voice** as a local demo — not a tunnel to a Mac. Host two things:

1. **LiveKit Cloud agent** — the Realtime dental receptionist (`agent_name` `ava-and-leo`)
2. **Vercel portal** — public URL + `PORTAL_PASSWORD`

Both of you tap **Call Ava** against the **cloud** worker so latency matches.

### 1. Deploy the agent to LiveKit Cloud

The `Dockerfile` already starts `src/agent.py start`. Confirm you are on CLI 2.15.0+ (`lk --version`). Docs: [agent deploy quickstart](https://docs.livekit.io/deploy/agents/quickstart/), [secrets](https://docs.livekit.io/deploy/agents/secrets/), [CLI agent commands](https://docs.livekit.io/reference/developer-tools/livekit-cli/agent/).

```bash
lk cloud auth
lk project list
# Select the project whose URL is wss://shellharbour-cqvf1jsj.livekit.cloud
lk project set-default "<that-project-name>"

# First deploy only. Writes livekit.toml (gitignored). Copies secrets except LIVEKIT_*.
# Fill .env.agent from .env.agent.example first (never commit it).
lk agent create --secrets-file .env.agent

# Later deploys:
lk agent deploy
lk agent status
lk agent logs
```

Cloud injects `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`. Do not set those as agent secrets.

| Agent secret | Value |
|--------------|--------|
| `OPENAI_API_KEY` | Realtime key |
| `AGENT_PERSONA` | `leo` for the dental path (portal tokens also send `persona: leo`). `ava` is the generic STT/LLM/TTS pipeline unless the token overrides it. |
| `PRACTICE_SOFTWARE` | `mock` |
| `LEO_REALTIME_VOICE` | `marin` |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Same Redis as the portal so the desk and Call Ava share one diary |
| `DIARY_STORE` | `redis` (recommended) |

`PORTAL_*` is not required on the agent.

**Stop the local Mac worker** (`run_local.py` / `src/agent.py dev`) while the cloud agent is live. Two workers (double workers) both register as `ava-and-leo` and steal jobs from each other. To run a second agent in the same project you must change `agent_name` in source and redeploy — do not do that for this partner share.

### 2. Deploy the byte voice portal to Vercel

The desk is FastAPI (`src/portal.py`, Vercel entry `src/main.py`). Brand: void `#05060a`, surface `#0b0f1a`, cyan `#4dfff0`, violet `#8b5cff`, live magenta `#ff4d9a`.

```bash
# From the repo root. Or Import the GitHub repo in the Vercel dashboard.
npx vercel --prod
```

Set these **Vercel project env vars** (Production):

| Portal env | Notes |
|------------|--------|
| `LIVEKIT_URL` | `wss://shellharbour-cqvf1jsj.livekit.cloud` |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Same LiveKit Cloud project as the agent |
| `PORTAL_PASSWORD` | **Required** on Vercel (not localhost). Share this with the partner. |
| `PRACTICE_SOFTWARE` | `mock` |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Durable diary for two users |
| `DIARY_STORE` | `redis` |

Optional instead of Redis: run `supabase/mock_diary.sql`, set `MOCK_DIARY_TABLE=mock_diary` plus `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Local fallback without Redis/Supabase is an in-memory/file diary (file is wrong on Vercel — ephemeral).

`requirements-portal.txt` keeps the Vercel bundle off `livekit-agents`. The voice worker stays on LiveKit Cloud.

### 3. Send the partner

- The Vercel URL (for example `https://<project>.vercel.app`)
- `PORTAL_PASSWORD`

They open the desk, pick a branch, and tap **Call Ava**. No SSH tunnel, no Mac left running.

### Coordinator env checklist (do not commit values)

This repo does not deploy with production secrets. Whoever has the LiveKit / OpenAI / Upstash keys should set:

- LiveKit Cloud agent: `.env.agent` → `lk agent create` / `lk agent update-secrets`
- Vercel: dashboard env vars listed above
- Upstash Redis (or Supabase table) created once and pasted into **both** the agent and the portal

## Deploy (reference)

- Agent: [LiveKit Cloud agents](https://docs.livekit.io/deploy/agents/) via the `Dockerfile` (`CMD` → `uv run src/agent.py start`).
- Portal: Vercel FastAPI (`src/main.py`) or local `uvicorn portal:app --app-dir src`.
- Do not expose the portal without `PORTAL_PASSWORD`.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
