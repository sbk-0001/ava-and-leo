# Swollen face and trouble swallowing

- DID: `+61242169911` → **Shellharbour Dentists** (`shellharbour`)
- CallState: `{'branch': 'shellharbour', 'branch_name': 'Shellharbour Dentists', 'caller_name': None, 'caller_mobile': None, 'is_existing_patient': None, 'intent': 'emergency_000', 'appointment_type': None, 'proposed_slot': None, 'confirmed_slot': None, 'urgency_level': 'emergency_000', 'escalation_flag': True, 'turn_count': 1, 'ask_counts': {}, 'offered_branch': None, 'bot_ask_count': 0, 'kill_switch': False}`
- started_at: 2026-09-15T02:03:23.005536+00:00
- ended_at: 2026-09-15T02:03:23.005585+00:00

```
call_id: 04-swollen-swallow-escalate
room: demo-04-swollen-swallow-escalate
branch: shellharbour
started_at: 2026-09-15T02:03:23.005536+00:00
ended_at: 2026-09-15T02:03:23.005585+00:00

[2026-09-15T02:03:23.005542+00:00] assistant: Good morning, Shellharbour Dentists, this is Ava!
[2026-09-15T02:03:23.005552+00:00] user: My face is swollen and I'm having trouble swallowing.
[2026-09-15T02:03:23.005555+00:00] assistant: Stay with me. This is one for hospital, not the diary. Hang up and call triple zero, or go straight to Shellharbour or Wollongong Hospital emergency now. Don't wait for an appointment.
[2026-09-15T02:03:23.005580+00:00] TOOL check_availability: {'ok': False, 'reason': 'do_not_book', 'action': 'call_000'}
[2026-09-15T02:03:23.005583+00:00] TOOL transfer_to_human: {'ok': True, 'reason': 'airway_risk'}
```
