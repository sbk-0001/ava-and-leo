#!/usr/bin/env python3
"""Purge ByteVoice caller-store records older than 12 months from last contact."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from caller_store import RETENTION, caller_store_from_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", default=None, help="ISO timestamp override")
    args = parser.parse_args()
    store = caller_store_from_env()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    removed = store.purge_older_than(now=now, retention=RETENTION)
    print(f"purged {len(removed)} caller records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
