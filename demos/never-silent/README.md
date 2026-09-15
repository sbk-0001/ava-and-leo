# Never-silent Ava — scenario recordings

Owner scenarios. Offline metrics come from the filler ladder / backchannel / $50 cancel path. Live WAVs are mixed from the LiveKit room (scripted TTS caller + dispatched `ava-and-leo`).

## Record real audio

```bash
cp .env.example .env.local   # then fill LIVEKIT_* and OPENAI_API_KEY
set -a && source .env.local && set +a
uv run python src/never_silent_harness.py --live --out demos/never-silent
```

That command loads `.env.local`, starts a local Ava worker unless you pass `--no-spawn-worker`, creates one LiveKit room per scenario, dispatches agent `ava-and-leo`, records a local WAV, and starts audio-only room-composite egress when `EGRESS_S3_*` is set.

One scenario:

```bash
uv run python src/never_silent_harness.py --live --scenario 01-slow-availability
```

Live audio this run: **no**.

- `01-slow-availability` — Booking — availability lookup forced to ~6s — *(no WAV — run --live on a desk with secrets)*
- `02-tool-timeout` — Tool timeout entirely — stage 5 take_message — *(no WAV — run --live on a desk with secrets)*
- `03-toothache-monologue` — Caller talks ~20s about a toothache — *(no WAV — run --live on a desk with secrets)*
- `04-barge-in-three` — Caller interrupts Ava three times — *(no WAV — run --live on a desk with secrets)*
- `05-bot-ask-twice` — Caller asks are you a real person twice — *(no WAV — run --live on a desk with secrets)*
- `06-unknown-fee` — Caller asks a price not in the fee table — *(no WAV — run --live on a desk with secrets)*
- `07-swollen-swallow` — Facial swelling and trouble swallowing — *(no WAV — run --live on a desk with secrets)*
- `08-cancel-24h-fee` — Cancellation inside 24 hours — $50 fee must be raised — *(no WAV — run --live on a desk with secrets)*
