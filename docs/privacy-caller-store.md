# Caller store — privacy and data flow

ByteVoice keeps a **minimum** caller record so Ava can stop asking for facts she
already has. It is **not** the practice management system (PMS / Zavy 360).

## What we store

Keyed on **E.164** phone number:

| Field | Purpose |
| --- | --- |
| name | Greet by first name |
| preferred_branch | Skip "which clinic" |
| dentist_preference | Filter diary offers after identity is verified |
| booking_history (ids/dates only) | Avoid re-collecting; **never volunteer** |
| last_contacted | 12-month retention clock |
| mobile | Same as the key, national format in CallState |

No clinical notes, no treatment plans, no DOB in this store. DOB is checked
against the PMS for the current call and is not retained here.

## How it is used

- **Avoid asking** — if the mobile is known, the ask-tool returns
  `already known: <value>` and Ava must not ask again.
- **Never volunteer** existing appointment, dentist, or treatment detail
  until the caller has verified date of birth on this call.
- Greet by first name is allowed without DOB.
- New booking: no DOB. Discuss / move / cancel an existing appointment: DOB
  required.
- Failed DOB: offer a callback. Do not say the date of birth was wrong. Do
  not confirm or deny that a patient record exists.
- Web: no device fingerprinting. Ask once for the number and store against it
  if they give it.

## Retention

12 months from **last contact**. `uv run python scripts/purge_caller_store.py`
deletes older rows. If this store moves to Postgres/Supabase, enable RLS so
a workspace can only read its own `e164` rows:

```sql
-- stub: enable when the store is a table, not the local JSON file
alter table caller_store enable row level security;
create policy caller_store_workspace on caller_store
  using (workspace_id = auth.jwt()->>'workspace_id');
```

## Telephony vs web

Phone: ANI is normalised to E.164 before speech; lookup hydrates CallState.
Web portal: no ANI; Ava asks once and stores against the number they give.
