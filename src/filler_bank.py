"""Pre-rendered filler audio bank. Fillers never touch the model or live TTS.

Clips are loaded into memory at boot. Playback is a buffer read onto the
outbound mix — no network, no session.say, no generate_reply.

Docs: https://docs.livekit.io/agents/multimodality/audio/customization.md
      https://docs.livekit.io/agents/multimodality/audio/background-audio.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phrase_pools import (
    RECOVERY,
    STAGE_1,
    STAGE_2,
    STAGE_3,
    STAGE_4,
    STAGE_5,
    STAGE_EMPTY,
    STAGE_ERROR,
)

logger = logging.getLogger("ava.filler_bank")

BANK_DIR = Path(__file__).resolve().parent / "audio" / "fillers"
MANIFEST_NAME = "manifest.json"
SAMPLE_RATE = 48000
NUM_CHANNELS = 1
SAMPLE_WIDTH = 2  # int16
MIN_POOL_VARIANTS = 4
VOICE = "marin"
SPEED = 0.9

FILLER_POOLS: dict[str, tuple[str, ...]] = {
    "stage_1": STAGE_1,
    "stage_2": STAGE_2,
    "stage_3": STAGE_3,
    "stage_4": STAGE_4,
    "stage_5": STAGE_5,
    "stage_error": STAGE_ERROR,
    "stage_empty": STAGE_EMPTY,
    "recovery": RECOVERY,
}


class FillerBankError(RuntimeError):
    """Boot-fatal: the filler bank is missing, incomplete, or corrupt."""


@dataclass(frozen=True)
class FillerClip:
    pool: str
    text: str
    path: Path
    pcm: bytes
    sample_rate: int
    sha256: str
    duration_s: float


def phrase_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:72] or "clip"


def clip_relpath(pool: str, text: str) -> str:
    return f"{pool}/{phrase_slug(text)}.wav"


def clip_path(pool: str, text: str, *, root: Path | None = None) -> Path:
    return (root or BANK_DIR) / clip_relpath(pool, text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def duration_for_text(text: str) -> float:
    words = max(1, len(text.split()))
    return max(0.7, min(3.8, words * 0.38 / SPEED))


def synthesize_pcm(text: str, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Deterministic speech-shaped PCM so CI can boot without OpenAI TTS."""
    n = int(sample_rate * duration_for_text(text))
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    f1 = 180.0 + (seed % 140)
    f2 = 420.0 + ((seed >> 7) % 280)
    pcm = bytearray(n * SAMPLE_WIDTH)
    for i in range(n):
        t = i / sample_rate
        env = min(1.0, t / 0.02) * min(1.0, (n - i) / (0.04 * sample_rate))
        trem = 0.85 + 0.15 * math.sin(2 * math.pi * 3.1 * t)
        sample = (
            0.55 * math.sin(2 * math.pi * f1 * t)
            + 0.28 * math.sin(2 * math.pi * f2 * t)
            + 0.06 * rng.uniform(-1.0, 1.0)
        )
        value = int(max(-32767, min(32767, sample * env * trem * 9000)))
        pcm[i * 2] = value & 0xFF
        pcm[i * 2 + 1] = (value >> 8) & 0xFF
    return bytes(pcm)


def write_wav(path: Path, pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(NUM_CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def read_wav_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != SAMPLE_WIDTH:
            raise FillerBankError(f"{path} is not 16-bit PCM")
        pcm = wav.readframes(wav.getnframes())
        return pcm, wav.getframerate()


def build_manifest(
    *,
    source: str,
    root: Path | None = None,
) -> dict[str, Any]:
    root = root or BANK_DIR
    pools: dict[str, list[dict[str, str]]] = {}
    for pool, lines in FILLER_POOLS.items():
        entries: list[dict[str, str]] = []
        for text in lines:
            rel = clip_relpath(pool, text)
            path = root / rel
            digest = sha256_bytes(path.read_bytes()) if path.is_file() else ""
            entries.append({"text": text, "file": rel, "sha256": digest})
        pools[pool] = entries
    return {
        "voice": VOICE,
        "speed": SPEED,
        "sample_rate": SAMPLE_RATE,
        "source": source,
        "pools": pools,
    }


def write_manifest(payload: dict[str, Any], *, root: Path | None = None) -> Path:
    root = root or BANK_DIR
    path = root / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def generate_bank(
    *,
    root: Path | None = None,
    source: str = "synthetic-placeholder",
    tts_pcm: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Write one WAV per pool line. OpenAI PCM may be supplied via tts_pcm."""
    root = root or BANK_DIR
    rendered = tts_pcm or {}
    for pool, lines in FILLER_POOLS.items():
        for text in lines:
            path = clip_path(pool, text, root=root)
            pcm = rendered.get(text) or synthesize_pcm(text)
            write_wav(path, pcm)
    manifest = build_manifest(source=source, root=root)
    write_manifest(manifest, root=root)
    return manifest


def load_manifest(*, root: Path | None = None) -> dict[str, Any]:
    root = root or BANK_DIR
    path = root / MANIFEST_NAME
    if not path.is_file():
        raise FillerBankError(
            f"Filler bank manifest missing at {path}. "
            "Run `uv run python scripts/generate_filler_bank.py` "
            "(OpenAI TTS when OPENAI_API_KEY is set; else synthetic PCM)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def assert_filler_bank(*, root: Path | None = None) -> dict[str, list[FillerClip]]:
    """Fail the process if any pool is missing or has fewer than 4 clips."""
    root = root or BANK_DIR
    manifest = load_manifest(root=root)
    loaded: dict[str, list[FillerClip]] = {}
    problems: list[str] = []
    for pool, lines in FILLER_POOLS.items():
        entries = {
            item["text"]: item for item in (manifest.get("pools") or {}).get(pool, [])
        }
        clips: list[FillerClip] = []
        for text in lines:
            rel = clip_relpath(pool, text)
            path = root / rel
            if not path.is_file():
                problems.append(f"missing clip {rel}")
                continue
            raw = path.read_bytes()
            expected = (entries.get(text) or {}).get("sha256") or ""
            digest = sha256_bytes(raw)
            if expected and digest != expected:
                problems.append(f"checksum mismatch {rel}")
                continue
            try:
                pcm, rate = read_wav_pcm(path)
            except Exception as exc:
                problems.append(f"unreadable {rel}: {exc}")
                continue
            duration = (len(pcm) / SAMPLE_WIDTH / max(1, rate)) if pcm else 0.0
            if duration < 0.2:
                problems.append(f"clip too short {rel}")
                continue
            clips.append(
                FillerClip(
                    pool=pool,
                    text=text,
                    path=path,
                    pcm=pcm,
                    sample_rate=rate,
                    sha256=digest,
                    duration_s=duration,
                )
            )
        unused = len(clips)
        if unused < MIN_POOL_VARIANTS:
            problems.append(
                f"pool {pool!r} has {unused} clips; need ≥{MIN_POOL_VARIANTS}"
            )
        loaded[pool] = clips
    extra_pools = set(manifest.get("pools") or {}) - set(FILLER_POOLS)
    if extra_pools:
        logger.info("filler bank has extra pools %s (ignored)", sorted(extra_pools))
    if problems:
        raise FillerBankError("Filler bank failed boot assert: " + "; ".join(problems))
    logger.info(
        "filler bank ok pools=%s clips=%s voice=%s source=%s",
        list(loaded),
        sum(len(v) for v in loaded.values()),
        manifest.get("voice"),
        manifest.get("source"),
    )
    return loaded


class FillerBank:
    def __init__(self, clips: dict[str, list[FillerClip]]) -> None:
        self.clips = clips
        self.by_text: dict[str, FillerClip] = {
            clip.text: clip for group in clips.values() for clip in group
        }

    @classmethod
    def load(cls, *, root: Path | None = None) -> FillerBank:
        return cls(assert_filler_bank(root=root))

    def get(self, text: str) -> FillerClip:
        clip = self.by_text.get(text)
        if clip is None:
            raise FillerBankError(f"no filler clip for {text!r}")
        return clip

    def pool_clips(self, pool: str) -> list[FillerClip]:
        return list(self.clips.get(pool) or [])


_BANK: FillerBank | None = None


def get_filler_bank(*, root: Path | None = None) -> FillerBank:
    global _BANK
    if _BANK is None or root is not None:
        _BANK = FillerBank.load(root=root)
    return _BANK


def reset_filler_bank_cache() -> None:
    global _BANK
    _BANK = None
