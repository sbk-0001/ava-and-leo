# Never-silent Ava — scenario metrics

Machinery metrics for the eight owner scenarios. `longest_silence_gap_ms` is the gap between first filler audio and tool dispatch in the harness (caller-talking gaps are excluded).

Real WAV recordings need LiveKit + OpenAI secrets; without them this harness still writes `metrics.json` so CI can prove the ladder, cache, and anti-repetition pools ran.

- `01-new-patient-checkup` — New patient check-up — cache-first availability
- `02-reschedule-cancel` — Reschedule then cancel inside 24h
- `03-severe-pain` — Severe pain Friday — same-day ladder
- `04-swollen-swallow` — Swollen + swallowing — no book
- `05-bot-ask-twice` — Are you a real person — twice
- `06-figtree-dapto` — Figtree caller — offer Dapto
- `07-unknown-fee` — Unknown fee — take_message
- `08-barge-in-three-times` — Barge-in three times — resume pool
