# Diary timeout mid-booking

- DID: `+61242169911` → **Shellharbour Dentists** (`shellharbour`)
- CallState: `{'branch': 'shellharbour', 'branch_name': 'Shellharbour Dentists', 'caller_name': None, 'caller_mobile': None, 'is_existing_patient': None, 'intent': None, 'appointment_type': None, 'proposed_slot': None, 'confirmed_slot': None, 'urgency_level': 'routine', 'escalation_flag': False, 'turn_count': 2, 'ask_counts': {}, 'offered_branch': None, 'bot_ask_count': 0, 'kill_switch': False}`
- started_at: 2026-09-15T02:03:23.006896+00:00
- ended_at: 2026-09-15T02:03:23.006954+00:00

```
call_id: 09-tool-timeout-recover
room: demo-09-tool-timeout-recover
branch: shellharbour
started_at: 2026-09-15T02:03:23.006896+00:00
ended_at: 2026-09-15T02:03:23.006954+00:00

[2026-09-15T02:03:23.006901+00:00] assistant: Good morning, Shellharbour Dentists, this is Ava!
[2026-09-15T02:03:23.006914+00:00] user: Can you book me in for a check-up this week? I'm Jordan, 0413 000 111.
[2026-09-15T02:03:23.006919+00:00] assistant: Righto, pulling up the diary — bit slow this morning, bear with me.
[2026-09-15T02:03:23.006926+00:00] TOOL check_availability: {'ok': False, 'reason': 'timeout', 'action': 'check_availability', 'note': 'The diary timed out. Stay in character. Do not invent a slot. Offer to try again, take a message, or transfer.'}
[2026-09-15T02:03:23.006929+00:00] assistant: Sorry, this booking system's older than I am — it's just sitting there. I don't want to make a time up. I can try again, or I'll take a message and the team'll call you back. What's easier?
[2026-09-15T02:03:23.006936+00:00] user: Call me back yeah.
[2026-09-15T02:03:23.006950+00:00] TOOL take_message: {'ok': True, 'confirmed': True, 'message_id': 'msg_68800a509f', 'branch_id': 'shellharbour'}
[2026-09-15T02:03:23.006952+00:00] assistant: Done. Someone'll ring you, Jordan. Sorry about the computer.
```
