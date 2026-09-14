"""Durable mock-diary backends for portal + cloud agent sharing.

Local default is a JSON file. Vercel/LiveKit Cloud must not rely on that
(ephemeral disk). Prefer Upstash Redis; Supabase is optional.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("diary_store")

DEFAULT_REDIS_KEY = "ava-mock-diary"
DEFAULT_SUPABASE_TABLE = "mock_diary"
DEFAULT_SUPABASE_ROW_ID = "shared"
RpcFn = Callable[[list[str]], Any]


class DiaryStore(Protocol):
    kind: str

    def load_payload(self) -> dict[str, Any] | None: ...

    def save_payload(self, payload: dict[str, Any]) -> None: ...


class DictDiaryStore:
    """Shared in-process (or test) dict. Two clients can share one bucket."""

    kind = "memory"

    def __init__(
        self, bucket: dict[str, Any] | None = None, key: str = "diary"
    ) -> None:
        self.bucket = bucket if bucket is not None else {}
        self.key = key

    def load_payload(self) -> dict[str, Any] | None:
        payload = self.bucket.get(self.key)
        if payload is None:
            return None
        return json.loads(json.dumps(payload))

    def save_payload(self, payload: dict[str, Any]) -> None:
        self.bucket[self.key] = json.loads(json.dumps(payload))


class FileDiaryStore:
    kind = "file"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load_payload(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save_payload(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class RedisDiaryStore:
    """Upstash Redis REST. Works from Vercel functions and LiveKit Cloud."""

    kind = "redis"

    def __init__(
        self,
        *,
        url: str,
        token: str,
        key: str = DEFAULT_REDIS_KEY,
        rpc: RpcFn | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.key = key
        self._rpc = rpc

    def _command(self, command: list[str]) -> Any:
        if self._rpc is not None:
            return self._rpc(command)
        body = json.dumps(command).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Upstash Redis request failed: {exc}") from exc
        return payload.get("result")

    def load_payload(self) -> dict[str, Any] | None:
        raw = self._command(["GET", self.key])
        if raw is None or raw == "":
            return None
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)

    def save_payload(self, payload: dict[str, Any]) -> None:
        self._command(["SET", self.key, json.dumps(payload)])


class SupabaseDiaryStore:
    """Optional JSON row. Enable with MOCK_DIARY_TABLE + SUPABASE_*."""

    kind = "supabase"

    def __init__(
        self,
        client: Any,
        *,
        table: str = DEFAULT_SUPABASE_TABLE,
        row_id: str = DEFAULT_SUPABASE_ROW_ID,
    ) -> None:
        self.client = client
        self.table = table
        self.row_id = row_id

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SupabaseDiaryStore:
        from supabase import create_client

        url = str(env.get("SUPABASE_URL", "")).strip()
        key = str(env.get("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
        table = str(env.get("MOCK_DIARY_TABLE", "")).strip() or DEFAULT_SUPABASE_TABLE
        row_id = (
            str(env.get("MOCK_DIARY_ROW_ID", "")).strip() or DEFAULT_SUPABASE_ROW_ID
        )
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required"
            )
        return cls(create_client(url, key), table=table, row_id=row_id)

    def load_payload(self) -> dict[str, Any] | None:
        result = (
            self.client.table(self.table)
            .select("payload")
            .eq("id", self.row_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return None
        payload = rows[0].get("payload")
        if isinstance(payload, str):
            return json.loads(payload)
        return payload

    def save_payload(self, payload: dict[str, Any]) -> None:
        self.client.table(self.table).upsert(
            {"id": self.row_id, "payload": payload}
        ).execute()


def resolve_diary_store_kind(env: Mapping[str, str]) -> str:
    raw = str(env.get("DIARY_STORE", "")).strip().lower()
    if raw in {"memory", "file", "redis", "supabase"}:
        return raw
    if (
        str(env.get("UPSTASH_REDIS_REST_URL", "")).strip()
        and str(env.get("UPSTASH_REDIS_REST_TOKEN", "")).strip()
    ):
        return "redis"
    if (
        str(env.get("MOCK_DIARY_TABLE", "")).strip()
        and str(env.get("SUPABASE_URL", "")).strip()
        and str(env.get("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
    ):
        return "supabase"
    if str(env.get("VERCEL", "")).strip():
        return "memory"
    return "file"


def diary_store_from_env(
    env: Mapping[str, str] | None = None,
    *,
    persist: bool = True,
) -> tuple[DiaryStore | None, str, Path | None]:
    """Return (store, kind, file_path). File path is only set for the file backend."""
    environ = env if env is not None else os.environ
    kind = resolve_diary_store_kind(environ)

    if kind == "redis":
        url = str(environ.get("UPSTASH_REDIS_REST_URL", "")).strip()
        token = str(environ.get("UPSTASH_REDIS_REST_TOKEN", "")).strip()
        key = str(environ.get("MOCK_DIARY_REDIS_KEY", "")).strip() or DEFAULT_REDIS_KEY
        if not url or not token:
            logger.warning(
                "DIARY_STORE=redis but Upstash URL/token missing; using memory"
            )
            return None, "memory", None
        return RedisDiaryStore(url=url, token=token, key=key), "redis", None

    if kind == "supabase":
        try:
            return SupabaseDiaryStore.from_env(environ), "supabase", None
        except Exception as exc:
            logger.warning("Supabase diary store unavailable (%s); using memory", exc)
            return None, "memory", None

    if kind == "memory" or not persist:
        if kind == "memory" and str(environ.get("VERCEL", "")).strip():
            logger.warning(
                "Vercel portal is using an in-memory mock diary. Set "
                "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN so two "
                "users share one diary."
            )
        return None, "memory", None

    raw_path = str(environ.get("MOCK_DIARY_PATH", "")).strip()
    path = Path(raw_path) if raw_path else Path(".data/mock_diary.json")
    return FileDiaryStore(path), "file", path
