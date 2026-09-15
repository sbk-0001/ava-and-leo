# Ava filler audio bank

Pre-rendered clips for stages 1–5, error/empty paths, and grounding recovery.
Fillers are a **buffer read** onto the outbound mix — they must never call
`session.say` or `generate_reply`.

| Field | Value |
| --- | --- |
| Voice | `marin` (same as live Ava Realtime) |
| Source | `openai-tts-gpt-4o-mini-tts` |
| Speed | `0.9` |
| Sample rate | 48 kHz mono PCM WAV |

Production boot **fails** if `manifest.json` still says `source=synthetic-placeholder`
(or a clip's PCM matches the sine-wave placeholder). Set `FILLER_REQUIRE_REAL=0`
or `FILLER_ALLOW_SYNTHETIC=1` only for local placeholder experiments.

## Regenerate

```bash
# Live Ava voice (needs OPENAI_API_KEY). Uses gpt-4o-mini-tts / marin / 0.9
uv run python scripts/generate_filler_bank.py

# If OpenAI is unset, the script falls back to Australian neural TTS
# (en-AU-NatashaNeural at −10% rate) so waits are still audible speech.

# Placeholder PCM (deterministic, no network) — tests only, never production
uv run python scripts/generate_filler_bank.py --synthetic --root /tmp/fillers
```

`manifest.json` stores a SHA-256 per file. `assert_filler_bank()` runs at
process start and **fails boot** if a pool is missing, a clip is short, or
production still has synthetic placeholders.
