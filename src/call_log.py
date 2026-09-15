"""Replayable call logging. Timestamps are always populated — never NULL."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("call_log")

DEFAULT_LOG_DIR = Path(".data/call_logs")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None = None) -> str:
    stamp = ts or utcnow()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat()


@dataclass
class TurnLog:
    turn_index: int
    role: str
    content: str
    timestamp: str
    tool_name: str | None = None
    tool_payload: dict[str, Any] | None = None


@dataclass
class CallLog:
    call_id: str
    room_name: str
    branch: str
    started_at: str
    ended_at: str | None = None
    turns: list[TurnLog] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def add_turn(
        self,
        *,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_payload: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> TurnLog:
        turn = TurnLog(
            turn_index=len(self.turns),
            role=role,
            content=content,
            timestamp=timestamp or iso(),
            tool_name=tool_name,
            tool_payload=tool_payload,
        )
        if not turn.timestamp:
            turn.timestamp = iso()
        self.turns.append(turn)
        return turn

    def close(self) -> None:
        self.ended_at = iso()

    def transcript_text(self) -> str:
        lines = [
            f"call_id: {self.call_id}",
            f"room: {self.room_name}",
            f"branch: {self.branch}",
            f"started_at: {self.started_at}",
            f"ended_at: {self.ended_at or iso()}",
            "",
        ]
        for turn in self.turns:
            who = turn.role
            stamp = turn.timestamp
            if turn.tool_name:
                lines.append(f"[{stamp}] TOOL {turn.tool_name}: {turn.content}")
            else:
                lines.append(f"[{stamp}] {who}: {turn.content}")
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if not payload.get("ended_at"):
            payload["ended_at"] = iso()
        for turn in payload["turns"]:
            if not turn.get("timestamp"):
                turn["timestamp"] = iso()
        return payload

    def save(self, directory: Path | None = None) -> Path:
        path_dir = directory or DEFAULT_LOG_DIR
        path_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re_sub(self.call_id)
        json_path = path_dir / f"{safe_id}.json"
        txt_path = path_dir / f"{safe_id}.txt"
        json_path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        txt_path.write_text(self.transcript_text(), encoding="utf-8")
        logger.info("saved call log %s", json_path)
        return json_path


def re_sub(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)[:80]


def log_dir_from_env(env: Mapping[str, str] | None = None) -> Path:
    environ = env if env is not None else os.environ
    raw = str(environ.get("AVA_CALL_LOG_DIR", "")).strip()
    return Path(raw) if raw else DEFAULT_LOG_DIR


def supabase_turn_row(
    *,
    call_id: Any,
    turn: TurnLog,
) -> dict[str, Any]:
    """One ava_transcript_turns row. created_at is always set."""
    return {
        "call_id": call_id,
        "turn_index": turn.turn_index,
        "role": "user" if turn.role == "user" else "agent",
        "content": turn.content,
        "created_at": turn.timestamp or iso(),
        "tool_name": turn.tool_name,
    }
