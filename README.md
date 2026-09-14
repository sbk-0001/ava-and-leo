<a href="https://livekit.io/">
  <img src="./.github/assets/livekit-mark.png" alt="LiveKit logo" width="100" height="100">
</a>

# Ava and Leo — Shellharbour Dentists caller

LiveKit Agents project with two personas:

| `AGENT_PERSONA` | Who | Path |
|-----------------|-----|------|
| `ava` | Generic voice assistant | AssemblyAI STT + Groq LLM + Cartesia TTS |
| `leo` | Phone receptionist for Shellharbour Dentists (Barrack Heights, Dapto, Woonona) | OpenAI Realtime (`gpt-realtime` via `livekit.plugins.openai.realtime.RealtimeModel`) |

Unset `AGENT_PERSONA` defaults to **leo on telephony** (SIP inbound or outbound) and **ava** on web/console.

Leo's spoken style, branch facts, fee catalogue, and tool rules live in [`src/persona.py`](src/persona.py) (product-owner source of truth). Do not invent parking, hours, dentists, prices, or diary slots. Fields marked `VERIFY` are unknown until the owner fills them in. Say **confirmed** only after a book/reschedule/cancel tool returns `confirmed: true`.

## Dev setup

This project uses [`uv`](https://docs.astral.sh/uv/). Copy `.env.example` to `.env.local` (never commit secrets).

```bash
cp .env.example .env.local
uv sync
```

### Required credentials

| Variable | Used by |
|----------|---------|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Agent worker, outbound `make_call.py`, evals |
| `OPENAI_API_KEY` | Leo (OpenAI Realtime) |
| `ASSEMBLYAI_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY` | Ava pipeline only |

Load LiveKit Cloud credentials with the [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/):

```bash
lk cloud auth
lk app env --write --destination .env.local
```

### SIP / telephony (Leo)

1. Create inbound and outbound trunks in LiveKit Cloud. See [SIP trunk setup](https://docs.livekit.io/telephony/start/sip-trunk-setup/) and [outbound calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/).
2. Point dispatch rules at this agent (`agent_name` is `ava-and-leo`).
3. Set:

```bash
SIP_OUTBOUND_TRUNK_ID=ST_xxxx          # lk sip outbound list
SIP_DID_MAP=+61242169911:shellharbour,+61242880737:dapto,+61242844486:woonona
SIP_TRANSFER_TO=+61242169911           # optional cold-transfer destination
PRACTICE_SOFTWARE=disconnected         # or mock — never invents diary slots
AGENT_PERSONA=leo                      # optional; telephony already defaults to leo
```

Inbound branch mapping uses the SIP participant attribute `sip.trunkPhoneNumber` (the DID the caller dialled).

Practice software (`PRACTICE_SOFTWARE=disconnected` by default) refuses availability/booking rather than inventing times. `mock` only returns slots you seed in tests.

## Run the agent

Console (Ava by default):

```bash
uv run python src/agent.py console
```

Leo in console (Realtime, no SIP):

```bash
AGENT_PERSONA=leo uv run python src/agent.py console
```

Worker for frontend or telephony:

```bash
uv run python src/agent.py dev     # development
uv run python src/agent.py start   # production
```

## Outbound calls

Start the worker, then:

```bash
uv run python src/make_call.py --to +61400000000
uv run python src/make_call.py --to +61400000000 --branch dapto
```

The script [dispatches](https://docs.livekit.io/agents/server/agent-dispatch/) agent `ava-and-leo` and calls [`CreateSIPParticipant`](https://docs.livekit.io/telephony/making-calls/outbound-calls/) with `wait_until_answered=True`. Failed dials raise `SipCallError` (busy, no answer, trunk failure). Mid-call hangups are handled per [SIP disconnect docs](https://docs.livekit.io/telephony/making-calls/outbound-calls/#mid-call-disconnections): `USER_UNAVAILABLE` and `SIP_TRUNK_FAILURE` explicitly shut down the job.

On outbound, Leo waits for the callee to speak first. On inbound, Leo greets as the mapped branch.

## Tests

```bash
uv run pytest tests/test_persona.py tests/test_fees.py tests/test_practice.py tests/test_sip.py tests/test_make_call.py -v
uv run pytest            # includes Ava evals; needs LIVEKIT_* in CI
```

Unit tests cover persona switching, branch facts, canned fees (`VERIFY` never becomes a made-up price), DID mapping, and the mock diary (no invented slots).

## Layout

```
src/agent.py       # entrypoint: Ava pipeline or Leo Realtime
src/persona.py     # Leo prompts, branches, fees (source of truth)
src/leo.py         # LeoReceptionist + office-system tools
src/practice.py    # disconnected / mock practice software
src/sip_utils.py   # DID map, disconnect handling
src/make_call.py   # outbound dispatch + SIP dial
```

## Docs

LiveKit Agents and telephony APIs change quickly. Use current docs:

- [OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/plugins/openai/)
- [Outbound SIP calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/)
- [SIP participant attributes](https://docs.livekit.io/reference/telephony/sip-participant/)
- [Agent testing](https://docs.livekit.io/agents/start/testing/)

The [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/) `lk docs` subcommand (v2.15.0+) searches the same documentation.

## Deploy

The `Dockerfile` is ready for [LiveKit Cloud agents](https://docs.livekit.io/deploy/agents/). Set `AGENT_PERSONA` / `OPENAI_API_KEY` / SIP vars as secrets on the agent.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
