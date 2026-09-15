# Ava filler audio bank

Pre-rendered clips for stages 1–5, error/empty paths, and grounding recovery.
Fillers are a **buffer read** onto the outbound mix — they must never call
`session.say` or `generate_reply`.

| Field | Value |
| --- | --- |
| Voice | `marin` (same as live Ava Realtime) |
| Speed | `0.9` |
| Sample rate | 48 kHz mono PCM WAV |

## Regenerate

```bash
# Placeholder PCM (deterministic, no network) — what CI boots against
uv run python scripts/generate_filler_bank.py --synthetic

# Live Ava voice (needs OPENAI_API_KEY). Uses gpt-4o-mini-tts / marin / 0.9
uv run python scripts/generate_filler_bank.py
```

`manifest.json` stores a SHA-256 per file. `assert_filler_bank()` runs at
process start and **fails boot** if a pool is missing or has fewer than 4 clips.
