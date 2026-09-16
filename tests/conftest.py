"""Keep the test suite away from the live practice.

Several modules call load_dotenv(".env.local") on import. On a machine that runs
the real desk, that file holds the shared database, the desk address and the SMS
key, so a plain `pytest` run posted fake "Booked appointment" / "Hello?" events to
the live desk and wrote test callers into Ava's real memory (17 Sep 2026).

load_dotenv never overrides a variable that is already set, so blanking these
here - before any test module imports the app - keeps every run offline.
"""

from __future__ import annotations

import os
import tempfile

LIVE_SETTINGS = (
    "DATABASE_URL",
    "DESK_EVENTS_TOKEN",
    "SMS_PROVIDER",
    "BREVO_API_KEY",
    "TELNYX_API_KEY",
    "SMS_FROM",
    "TELNYX_MESSAGING_PROFILE_ID",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "PORTAL_PASSWORD",
)
for _name in LIVE_SETTINGS:
    os.environ[_name] = ""

# Port 9 (discard) refuses at once: desk posts fail fast instead of reaching a
# desk running on this machine.
os.environ["DESK_EVENTS_URL"] = "http://127.0.0.1:9"
os.environ["PORTAL_URL"] = "http://127.0.0.1:9"

_SANDBOX = tempfile.mkdtemp(prefix="ava-tests-")
os.environ["CALLER_STORE_PATH"] = os.path.join(_SANDBOX, "caller_store.json")
os.environ["MOCK_DIARY_PATH"] = os.path.join(_SANDBOX, "mock_diary.json")
os.environ["AVA_CALL_LOG_DIR"] = os.path.join(_SANDBOX, "call_logs")
