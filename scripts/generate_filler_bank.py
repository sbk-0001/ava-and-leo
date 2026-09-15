#!/usr/bin/env python3
"""Generate the pre-rendered filler bank.

Uses OpenAI TTS (gpt-4o-mini-tts, voice marin, speed 0.9) when OPENAI_API_KEY
is set — the same voice Ava uses on Realtime. Otherwise writes deterministic
synthetic PCM so CI and boot asserts still pass.

Checksums live in src/audio/fillers/manifest.json and are verified at process
start by filler_bank.assert_filler_bank().

  uv run python scripts/generate_filler_bank.py
  uv run python scripts/generate_filler_bank.py --synthetic
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from filler_bank import BANK_DIR, SPEED, VOICE, generate_bank  # noqa: E402


def _all_lines() -> list[str]:
    from filler_bank import FILLER_POOLS as POOLS

    lines: list[str] = []
    for pool in POOLS.values():
        lines.extend(pool)
    return lines


async def _openai_pcm() -> dict[str, bytes]:
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
        print(f"tts {text!r} ({len(response.content)} bytes)")
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Ava filler WAV bank")
    parser.add_argument("--synthetic", action="store_true", help="Skip OpenAI TTS")
    parser.add_argument("--root", type=Path, default=BANK_DIR)
    args = parser.parse_args()
    source = "synthetic-placeholder"
    tts_pcm = None
    if not args.synthetic and os.getenv("OPENAI_API_KEY"):
        import asyncio
        import io
        import wave

        raw = asyncio.run(_openai_pcm())
        decoded: dict[str, bytes] = {}
        for text, blob in raw.items():
            if blob[:4] == b"RIFF":
                with wave.open(io.BytesIO(blob), "rb") as wav:
                    decoded[text] = wav.readframes(wav.getnframes())
            else:
                decoded[text] = blob
        tts_pcm = decoded
        source = "openai-tts-gpt-4o-mini-tts"
    else:
        print("OPENAI_API_KEY unset or --synthetic: writing placeholder PCM")
    generate_bank(root=args.root, source=source, tts_pcm=tts_pcm)
    print(f"wrote filler bank to {args.root} source={source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
