#!/usr/bin/env python3
"""Generate the pre-rendered filler bank.

Uses OpenAI TTS (gpt-4o-mini-tts, voice marin, speed 0.9) when OPENAI_API_KEY
is set — the same voice Ava uses on Realtime. Falls back to an Australian
neural TTS so tool waits are never silent placeholders.

Checksums live in src/audio/fillers/manifest.json and are verified at process
start by filler_bank.assert_filler_bank().

  uv run python scripts/generate_filler_bank.py
  uv run python scripts/generate_filler_bank.py --synthetic
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from filler_bank import (  # noqa: E402
    BANK_DIR,
    REAL_TTS_SOURCE,
    SPEED,
    SYNTHETIC_SOURCE,
    VOICE,
    generate_bank,
)

EDGE_VOICE = "en-AU-NatashaNeural"
EDGE_SOURCE = "edge-tts-en-AU-NatashaNeural"


def _all_lines() -> list[str]:
    from filler_bank import FILLER_POOLS as POOLS

    lines: list[str] = []
    for pool in POOLS.values():
        lines.extend(pool)
    return lines


async def _openai_wav() -> dict[str, bytes]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    rendered: dict[str, bytes] = {}
    for text in _all_lines():
        response = await client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=VOICE,
            input=text,
            speed=SPEED,
            response_format="wav",
        )
        rendered[text] = response.content
        print(f"openai-tts {text!r} ({len(response.content)} bytes)")
    return rendered


async def _edge_wav() -> dict[str, bytes]:
    """Australian female neural TTS. Audible speech, not marin."""
    import edge_tts

    # -10% is about 0.9x speaking rate.
    rendered: dict[str, bytes] = {}
    for text in _all_lines():
        communicate = edge_tts.Communicate(text, EDGE_VOICE, rate="-10%")
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        blob = b"".join(chunks)
        if not blob:
            raise RuntimeError(f"edge-tts returned no audio for {text!r}")
        rendered[text] = _mp3_to_wav_pcm(blob)
        print(f"edge-tts {text!r} ({len(rendered[text])} bytes pcm)")
    return rendered


def _mp3_to_wav_pcm(blob: bytes) -> bytes:
    """Decode mp3/ogg via PyAV (already a LiveKit dependency) to 48 kHz PCM."""
    import io

    import av

    from filler_bank import SAMPLE_RATE, wav_bytes_to_pcm

    container = av.open(io.BytesIO(blob))
    try:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=SAMPLE_RATE
        )
        pcm = bytearray()
        for frame in container.decode(stream):
            resampled = resampler.resample(frame)
            frames = resampled if isinstance(resampled, list) else [resampled]
            for out in frames:
                pcm.extend(bytes(out.planes[0]))
        flushed = resampler.resample(None)
        for out in flushed if isinstance(flushed, list) else [flushed]:
            if out is not None:
                pcm.extend(bytes(out.planes[0]))
    finally:
        container.close()
    if not pcm:
        return wav_bytes_to_pcm(blob)
    return bytes(pcm)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Ava filler WAV bank")
    parser.add_argument("--synthetic", action="store_true", help="Skip neural TTS")
    parser.add_argument("--root", type=Path, default=BANK_DIR)
    args = parser.parse_args()
    source = SYNTHETIC_SOURCE
    tts_pcm = None
    if not args.synthetic:
        if os.getenv("OPENAI_API_KEY"):
            try:
                tts_pcm = asyncio.run(_openai_wav())
                source = REAL_TTS_SOURCE
            except Exception as exc:
                print(f"OpenAI TTS failed ({exc}); trying edge-tts")
        if tts_pcm is None:
            try:
                import edge_tts  # noqa: F401
            except ImportError:
                print("installing edge-tts for audible filler clips...")
                import subprocess

                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "edge-tts"],
                    stdout=sys.stdout,
                )
            try:
                tts_pcm = asyncio.run(_edge_wav())
                source = EDGE_SOURCE
            except Exception as exc:
                print(f"edge-tts failed ({exc})")
                if args.root == BANK_DIR:
                    print(
                        "refusing to overwrite the committed bank with "
                        "synthetic-placeholder"
                    )
                    return 1
        if not tts_pcm:
            print("OPENAI_API_KEY unset and edge-tts failed: writing placeholder PCM")
            if args.root == BANK_DIR:
                return 1
    else:
        print("--synthetic: writing placeholder PCM")
    generate_bank(
        root=args.root,
        source=source,
        tts_pcm=tts_pcm,
        voice=VOICE
        if source == REAL_TTS_SOURCE
        else (EDGE_VOICE if source == EDGE_SOURCE else VOICE),
    )
    print(f"wrote filler bank to {args.root} source={source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
