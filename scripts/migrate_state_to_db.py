"""Move the laptop's diary and caller memory into the shared database.

    DATABASE_URL=postgresql://... uv run python scripts/migrate_state_to_db.py
    uv run python scripts/migrate_state_to_db.py --force   # overwrite what is there

Creates the tables if needed, then copies ``.data/mock_diary.json`` and
``.data/caller_store.json`` (or MOCK_DIARY_PATH / CALLER_STORE_PATH) into
``ava_state``. Without --force an existing row is left alone.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

load_dotenv(".env.local")

from state_store import (  # noqa: E402
    CALLER_STORE_KEY,
    DIARY_KEY,
    PostgresStateStore,
    database_url,
)


async def main(force: bool) -> int:
    dsn = database_url()
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    store = PostgresStateStore(dsn)
    await store.ensure_schema()
    sources = {
        DIARY_KEY: Path(os.getenv("MOCK_DIARY_PATH") or ".data/mock_diary.json"),
        CALLER_STORE_KEY: Path(
            os.getenv("CALLER_STORE_PATH") or ".data/caller_store.json"
        ),
    }
    for key, path in sources.items():
        if not path.is_file():
            print(f"{key}: no local file at {path} - skipped")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        current = await store.load(key)
        if current.exists and not force:
            print(f"{key}: already in the database (v{current.version}) - kept")
            continue
        version = await store.save(key, payload)
        size = len(payload.get("records", payload.get("bookings", payload)))
        print(f"{key}: written from {path} ({size} items) -> v{version}")
    print("latest desk event id:", await store.latest_desk_event_id())
    await store.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--force" in sys.argv[1:])))
