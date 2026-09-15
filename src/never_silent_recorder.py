"""Live LiveKit recorder for the eight never-silent owner scenarios.

Creates a room, dispatches `ava-and-leo`, records a local mixed WAV by
subscribing to tracks, and starts audio-only room-composite egress when
S3 (or compatible) credentials are present so the file can be downloaded.

Docs: https://docs.livekit.io/agents/server/agent-dispatch/
      https://docs.livekit.io/transport/media/ingress-egress/egress/composite-recording/
      https://docs.livekit.io/transport/media/publish/
      https://docs.livekit.io/transport/media/raw-tracks/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
import time
import uuid
import wave
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(".env.local")

logger = logging.getLogger("never_silent_recorder")

AGENT_NAME = "ava-and-leo"
SAMPLE_RATE = 24000
NUM_CHANNELS = 1
FRAME_SAMPLES = 480  # 20 ms at 24 kHz
RMS_THRESHOLD = 400.0


@dataclass
class CallerTurn:
    text: str
    wait_for_agent: bool = True
    interrupt_after_s: float | None = None
    min_speak_s: float | None = None
    pause_after_s: float = 0.4


@dataclass
class ScenarioSpec:
    slug: str
    title: str
    goal: str
    turns: tuple[CallerTurn, ...]
    booking_delay_s: float = 0.0
    booking_hang_s: float = 0.0
    seed_cancel_24h: bool = False
    settle_s: float = 2.0
    greeting_timeout_s: float = 12.0


@dataclass
class PcmTape:
    sample_rate: int = SAMPLE_RATE
    chunks: list[tuple[float, bytes]] = field(default_factory=list)
    started: float | None = None

    def add(self, pcm: bytes, ts: float | None = None) -> None:
        if not pcm:
            return
        stamp = ts if ts is not None else time.perf_counter()
        if self.started is None:
            self.started = stamp
        self.chunks.append((stamp, pcm))

    def to_pcm(self, end: float | None = None) -> bytes:
        if not self.chunks or self.started is None:
            return b""
        finished = end if end is not None else self.chunks[-1][0]
        duration = max(0.0, finished - self.started)
        total = int(duration * self.sample_rate) + FRAME_SAMPLES
        buf = bytearray(total * 2)
        for stamp, pcm in self.chunks:
            offset = int(max(0.0, stamp - self.started) * self.sample_rate)
            start = offset * 2
            end_i = start + len(pcm)
            if end_i > len(buf):
                buf.extend(b"\x00" * (end_i - len(buf) + 2))
            buf[start:end_i] = pcm[: len(buf) - start]
        return bytes(buf)


def write_wav(
    path: Path,
    pcm: bytes,
    *,
    sample_rate: int = SAMPLE_RATE,
    channels: int = NUM_CHANNELS,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return path


def _rms_window(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return (sum(sample * sample for sample in samples) / n) ** 0.5


def analyze_pcm(
    agent_pcm: bytes,
    caller_pcm: bytes,
    *,
    sample_rate: int = SAMPLE_RATE,
    threshold: float = RMS_THRESHOLD,
    window_ms: int = 20,
) -> tuple[float, float]:
    """Return (time_to_first_audio_ms, longest_silence_gap_ms).

    Gaps while the caller is talking are excluded, matching the owner brief.
    Leading silence before the first agent audio is time_to_first_audio, not a gap.
    """
    window = max(1, int(sample_rate * window_ms / 1000))
    bytes_per = window * 2
    length = max(len(agent_pcm), len(caller_pcm))
    agent = agent_pcm + b"\x00" * (length - len(agent_pcm))
    caller = caller_pcm + b"\x00" * (length - len(caller_pcm))

    first_audio_win: int | None = None
    gap = 0
    longest = 0
    after_first = False
    for index in range(0, length, bytes_per):
        agent_rms = _rms_window(agent[index : index + bytes_per])
        caller_rms = _rms_window(caller[index : index + bytes_per])
        agent_on = agent_rms >= threshold
        caller_on = caller_rms >= threshold
        if agent_on and first_audio_win is None:
            first_audio_win = index // bytes_per
            after_first = True
            gap = 0
            continue
        if not after_first:
            continue
        if caller_on:
            gap = 0
            continue
        if agent_on:
            longest = max(longest, gap)
            gap = 0
        else:
            gap += 1
    longest = max(longest, gap)
    ttfa = (first_audio_win or 0) * window_ms
    return float(ttfa), float(longest * window_ms)


def stock_phrases_from_texts(texts: Iterable[str]) -> list[str]:
    from phrase_pools import STOCK_POOLS

    blob = " ".join(texts).lower()
    found: list[str] = []
    for pool in STOCK_POOLS.values():
        for line in pool:
            needle = line.lower().split("{")[0].strip()
            if needle and needle in blob and line not in found:
                found.append(line)
    return found


def egress_s3_from_env(env: Mapping[str, str] | None = None) -> Any | None:
    from livekit import api

    environ = env if env is not None else os.environ
    bucket = str(environ.get("EGRESS_S3_BUCKET", "")).strip()
    access = str(
        environ.get("EGRESS_S3_ACCESS_KEY") or environ.get("AWS_ACCESS_KEY_ID") or ""
    ).strip()
    secret = str(
        environ.get("EGRESS_S3_SECRET") or environ.get("AWS_SECRET_ACCESS_KEY") or ""
    ).strip()
    if not bucket or not access or not secret:
        return None
    kwargs: dict[str, Any] = {
        "access_key": access,
        "secret": secret,
        "bucket": bucket,
        "region": str(environ.get("EGRESS_S3_REGION", "ap-southeast-2")).strip(),
    }
    endpoint = str(environ.get("EGRESS_S3_ENDPOINT", "")).strip()
    if endpoint:
        kwargs["endpoint"] = endpoint
    force = str(environ.get("EGRESS_S3_FORCE_PATH_STYLE", "")).strip().lower()
    if force in {"1", "true", "yes"}:
        kwargs["force_path_style"] = True
    return api.S3Upload(**kwargs)


def build_room_composite_request(
    *,
    room_name: str,
    filepath: str,
    s3: Any | None = None,
) -> Any:
    """Audio-only room composite. Leave layout empty for the audio billing path.

    Docs: https://docs.livekit.io/transport/media/ingress-egress/egress/composite-recording/
    """
    from livekit import api

    file_kwargs: dict[str, Any] = {"filepath": filepath}
    file_type = getattr(api.EncodedFileType, "OGG", 2)
    file_kwargs["file_type"] = file_type
    if s3 is not None:
        file_kwargs["s3"] = s3
    req_kwargs: dict[str, Any] = {
        "room_name": room_name,
        "audio_only": True,
        "file_outputs": [api.EncodedFileOutput(**file_kwargs)],
    }
    mixing = getattr(api, "AudioMixing", None)
    if mixing is not None:
        req_kwargs["audio_mixing"] = mixing.DUAL_CHANNEL_AGENT
    return api.RoomCompositeEgressRequest(**req_kwargs)


def build_dispatch_metadata(spec: ScenarioSpec) -> str:
    payload: dict[str, Any] = {
        "persona": "ava",
        "branch": "shellharbour",
        "source": "never-silent-recorder",
        "direction": "inbound",
    }
    if spec.booking_delay_s:
        payload["booking_delay_s"] = spec.booking_delay_s
    if spec.booking_hang_s:
        payload["booking_hang_s"] = spec.booking_hang_s
    if spec.seed_cancel_24h:
        payload["seed_cancel_24h"] = True
    return json.dumps(payload)


def build_caller_token(
    *,
    url: str,
    api_key: str,
    api_secret: str,
    room: str,
    identity: str,
) -> str:
    del url
    from livekit.api import AccessToken, VideoGrants

    return (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name("Scripted caller")
        .with_ttl(timedelta(hours=1))
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )


def _required_live_env() -> dict[str, str]:
    names = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY")
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing "
            + ", ".join(missing)
            + ". Copy .env.example to .env.local and fill LiveKit + OpenAI keys."
        )
    return values


async def synthesize_pcm(text: str) -> bytes:
    """OpenAI TTS → 24 kHz 16-bit mono PCM for the scripted caller."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    model = (
        os.getenv("CALLER_TTS_MODEL", "gpt-4o-mini-tts").strip() or "gpt-4o-mini-tts"
    )
    voice = os.getenv("CALLER_TTS_VOICE", "alloy").strip() or "alloy"
    try:
        response = await client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
            response_format="pcm",
        )
        pcm = response.content
        if pcm:
            return pcm
    except Exception:
        logger.exception("gpt-4o-mini-tts pcm failed; trying tts-1 wav")
    response = await client.audio.speech.create(
        model="tts-1",
        voice=voice
        if voice in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
        else "alloy",
        input=text,
        response_format="wav",
    )
    return _wav_to_pcm(response.content)


def _pcm_to_16bit(frames: bytes, width: int) -> bytes:
    """Convert unsigned-8 / 16 / 24 / 32-bit PCM to signed 16-bit little-endian."""
    if width == 2:
        return frames
    if width == 1:
        samples = [((byte - 128) << 8) for byte in frames]
        return struct.pack(f"<{len(samples)}h", *samples)
    if width == 3:
        count = len(frames) // 3
        samples = []
        for index in range(count):
            raw = frames[index * 3 : index * 3 + 3]
            value = int.from_bytes(raw, "little", signed=True)
            samples.append(max(-32768, min(32767, value >> 8)))
        return struct.pack(f"<{count}h", *samples)
    if width == 4:
        count = len(frames) // 4
        samples = struct.unpack(f"<{count}i", frames[: count * 4])
        clipped = [max(-32768, min(32767, sample >> 16)) for sample in samples]
        return struct.pack(f"<{count}h", *clipped)
    raise ValueError(f"unsupported sample width: {width}")


def _to_mono(frames: bytes, channels: int) -> bytes:
    if channels <= 1:
        return frames
    frame_count = len(frames) // (2 * channels)
    packed = frames[: frame_count * channels * 2]
    samples = struct.unpack(f"<{frame_count * channels}h", packed)
    mono = []
    for index in range(frame_count):
        total = sum(samples[index * channels + channel] for channel in range(channels))
        mono.append(int(max(-32768, min(32767, total // channels))))
    return struct.pack(f"<{frame_count}h", *mono)


def _resample_mono16(frames: bytes, in_rate: int, out_rate: int) -> bytes:
    if in_rate == out_rate:
        return frames
    incoming = len(frames) // 2
    if incoming == 0:
        return b""
    samples = struct.unpack(f"<{incoming}h", frames)
    outgoing = max(1, round(incoming * out_rate / in_rate))
    if incoming == 1:
        return struct.pack(f"<{outgoing}h", *([samples[0]] * outgoing))
    converted: list[int] = []
    scale = (incoming - 1) / (outgoing - 1)
    for index in range(outgoing):
        source = index * scale
        low = int(source)
        high = min(low + 1, incoming - 1)
        frac = source - low
        value = samples[low] * (1.0 - frac) + samples[high] * frac
        converted.append(int(max(-32768, min(32767, round(value)))))
    return struct.pack(f"<{outgoing}h", *converted)


def _wav_to_pcm(blob: bytes) -> bytes:
    """Decode a WAV blob to 24 kHz 16-bit mono PCM without stdlib audioop.

    ``audioop`` was removed in Python 3.13, and CI runs 3.14.
    """
    import io

    with wave.open(io.BytesIO(blob), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    frames = _pcm_to_16bit(frames, width)
    frames = _to_mono(frames, channels)
    return _resample_mono16(frames, rate, SAMPLE_RATE)


async def _publish_pcm(source: Any, pcm: bytes, tape: PcmTape) -> None:
    from livekit import rtc

    padded = pcm
    rem = len(padded) % (FRAME_SAMPLES * 2)
    if rem:
        padded += b"\x00" * (FRAME_SAMPLES * 2 - rem)
    for index in range(0, len(padded), FRAME_SAMPLES * 2):
        chunk = padded[index : index + FRAME_SAMPLES * 2]
        frame = rtc.AudioFrame(
            data=chunk,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            samples_per_channel=FRAME_SAMPLES,
        )
        tape.add(chunk)
        await source.capture_frame(frame)


async def _wait_energy(
    tape: PcmTape,
    *,
    timeout_s: float,
    threshold: float = RMS_THRESHOLD,
    want_on: bool = True,
) -> bool:
    deadline = time.perf_counter() + timeout_s
    seen = 0
    while time.perf_counter() < deadline:
        if tape.chunks:
            _rms = _rms_window(tape.chunks[-1][1])
            active = _rms >= threshold
            if want_on and active:
                seen += 1
                if seen >= 2:
                    return True
            elif not want_on and not active:
                seen += 1
                if seen >= 8:
                    return True
            else:
                seen = 0
        await asyncio.sleep(0.04)
    return False


async def _subscribe_remote_audio(room: Any, tape: PcmTape) -> list[asyncio.Task[None]]:
    from livekit import rtc

    tasks: list[asyncio.Task[None]] = []

    async def _pump(track: Any) -> None:
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS
        )
        try:
            async for event in stream:
                frame = event.frame
                tape.add(bytes(frame.data))
        except Exception:
            logger.exception("remote audio stream ended")
        finally:
            with_context = getattr(stream, "aclose", None)
            if callable(with_context):
                await with_context()

    def _maybe(track: Any, participant: Any) -> None:
        kind = getattr(track, "kind", None)
        if "AUDIO" not in str(kind).upper():
            return
        identity = getattr(participant, "identity", "") or ""
        if identity == room.local_participant.identity:
            return
        tasks.append(asyncio.create_task(_pump(track)))

    @room.on("track_subscribed")
    def _on_sub(track: Any, publication: Any, participant: Any) -> None:
        del publication
        _maybe(track, participant)

    for participant in room.remote_participants.values():
        for publication in participant.track_publications.values():
            track = getattr(publication, "track", None)
            if track is not None:
                _maybe(track, participant)
    return tasks


async def _wait_for_agent(room: Any, timeout_s: float = 45.0) -> Any:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        for participant in room.remote_participants.values():
            kind = str(getattr(participant, "kind", ""))
            identity = (getattr(participant, "identity", "") or "").lower()
            if "AGENT" in kind.upper() or "agent" in identity or "ava" in identity:
                return participant
        await asyncio.sleep(0.2)
    raise TimeoutError(
        "Agent ava-and-leo did not join. Start the worker with "
        "`AGENT_PERSONA=ava PRACTICE_SOFTWARE=mock uv run python src/agent.py dev` "
        "or pass --spawn-worker."
    )


async def spawn_agent_worker() -> asyncio.subprocess.Process:
    env = os.environ.copy()
    env.setdefault("AGENT_PERSONA", "ava")
    env.setdefault("PRACTICE_SOFTWARE", "mock")
    env.setdefault("BOOKING_PROVIDER", "memory")
    env.setdefault("AVA_REALTIME_VOICE", "marin")
    root = Path(__file__).resolve().parent.parent
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(root / "src" / "agent.py"),
        "dev",
        cwd=str(root),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    started = time.perf_counter()
    buf = b""
    while time.perf_counter() - started < 45:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=45)
        if not line:
            break
        buf += line
        text = line.decode("utf-8", errors="replace").lower()
        logger.info("worker: %s", line.decode("utf-8", errors="replace").rstrip())
        if (
            "registered" in text
            or "worker started" in text
            or "starting worker" in text
        ):
            await asyncio.sleep(1.5)
            return proc
        if proc.returncode is not None:
            break
    raise RuntimeError(
        "Agent worker did not register. Output:\n"
        + buf.decode("utf-8", errors="replace")
    )


async def _drive_turns(
    *,
    source: Any,
    turns: Sequence[CallerTurn],
    agent_tape: PcmTape,
    caller_tape: PcmTape,
    greeting_timeout_s: float,
) -> list[str]:
    spoken: list[str] = []
    await _wait_energy(agent_tape, timeout_s=greeting_timeout_s, want_on=True)
    await _wait_energy(agent_tape, timeout_s=8.0, want_on=False)
    for turn in turns:
        if turn.interrupt_after_s is not None:
            await _wait_energy(agent_tape, timeout_s=10.0, want_on=True)
            await asyncio.sleep(turn.interrupt_after_s)
        pcm = await synthesize_pcm(turn.text)
        spoken.append(turn.text)
        started = time.perf_counter()
        await _publish_pcm(source, pcm, caller_tape)
        elapsed = time.perf_counter() - started
        if turn.min_speak_s and elapsed < turn.min_speak_s:
            extra = turn.text
            while time.perf_counter() - started < turn.min_speak_s:
                more = await synthesize_pcm(extra)
                await _publish_pcm(source, more, caller_tape)
        if turn.wait_for_agent and turn.interrupt_after_s is None:
            await _wait_energy(agent_tape, timeout_s=16.0, want_on=True)
            await _wait_energy(agent_tape, timeout_s=8.0, want_on=False)
        if turn.pause_after_s:
            await asyncio.sleep(turn.pause_after_s)
    return spoken


async def record_scenario(
    spec: ScenarioSpec,
    out_dir: Path,
    *,
    spawn_worker: bool = False,
) -> dict[str, Any]:
    """Run one owner scenario against a live Ava worker and write `{slug}.wav`."""
    from livekit import api, rtc

    creds = _required_live_env()
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{spec.slug}.wav"
    room_name = f"ava-ns-{spec.slug}-{uuid.uuid4().hex[:8]}"
    identity = f"caller-{spec.slug}"
    worker: asyncio.subprocess.Process | None = None
    egress_id: str | None = None
    notes: list[str] = [spec.goal]
    transcripts: list[str] = []

    if spawn_worker:
        worker = await spawn_agent_worker()
        notes.append("spawned local ava-and-leo worker")

    token = build_caller_token(
        url=creds["LIVEKIT_URL"],
        api_key=creds["LIVEKIT_API_KEY"],
        api_secret=creds["LIVEKIT_API_SECRET"],
        room=room_name,
        identity=identity,
    )
    room = rtc.Room()
    agent_tape = PcmTape()
    caller_tape = PcmTape()
    stream_tasks: list[asyncio.Task[None]] = []

    async with api.LiveKitAPI() as lkapi:
        await lkapi.room.create_room(
            api.CreateRoomRequest(name=room_name, empty_timeout=300)
        )
        s3 = egress_s3_from_env()
        try:
            info = await lkapi.egress.start_room_composite_egress(
                build_room_composite_request(
                    room_name=room_name,
                    filepath=f"never-silent/{spec.slug}.ogg",
                    s3=s3,
                )
            )
            egress_id = info.egress_id
            notes.append(
                "started audio-only room composite egress"
                + (" to S3" if s3 is not None else " (project default storage)")
            )
        except Exception as exc:
            logger.warning("room composite egress unavailable: %s", exc)
            notes.append(
                "egress skipped — local WAV mix is the recording "
                f"({type(exc).__name__})"
            )

        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room_name,
                metadata=build_dispatch_metadata(spec),
            )
        )

        await room.connect(creds["LIVEKIT_URL"], token)
        stream_tasks = await _subscribe_remote_audio(room, agent_tape)
        source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track("caller", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)

        try:
            await _wait_for_agent(room)
            spoken = await _drive_turns(
                source=source,
                turns=spec.turns,
                agent_tape=agent_tape,
                caller_tape=caller_tape,
                greeting_timeout_s=spec.greeting_timeout_s,
            )
            transcripts.extend(spoken)
            await asyncio.sleep(spec.settle_s)
        finally:
            with_context = getattr(room, "disconnect", None)
            if callable(with_context):
                await room.disconnect()
            if egress_id:
                try:
                    await lkapi.egress.stop_egress(
                        api.StopEgressRequest(egress_id=egress_id)
                    )
                except Exception:
                    logger.exception("stop egress failed")
            try:
                await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except Exception:
                logger.exception("delete room failed")

    for task in stream_tasks:
        task.cancel()
    if worker is not None:
        worker.terminate()
        try:
            await asyncio.wait_for(worker.wait(), timeout=8)
        except TimeoutError:
            worker.kill()

    agent_pcm = agent_tape.to_pcm()
    caller_pcm = caller_tape.to_pcm()
    mixed = _mix_mono(agent_pcm, caller_pcm)
    if len(mixed) <= 44:
        raise RuntimeError(f"no audio captured for {spec.slug}")
    write_wav(wav_path, mixed)
    ttfa, gap = analyze_pcm(agent_pcm, caller_pcm)
    phrases = stock_phrases_from_texts(transcripts)
    return {
        "slug": spec.slug,
        "title": spec.title,
        "audio_recording": str(wav_path),
        "time_to_first_audio_ms": ttfa,
        "longest_silence_gap_ms": gap,
        "stock_phrases_used": phrases,
        "notes": notes,
        "room": room_name,
        "egress_id": egress_id,
    }


def _mix_mono(left: bytes, right: bytes) -> bytes:
    length = max(len(left), len(right))
    a = left + b"\x00" * (length - len(left))
    b = right + b"\x00" * (length - len(right))
    n = length // 2
    mixed = []
    samples_a = struct.unpack(f"<{n}h", a[: n * 2])
    samples_b = struct.unpack(f"<{n}h", b[: n * 2])
    for sa, sb in zip(samples_a, samples_b, strict=True):
        mixed.append(int(max(-32768, min(32767, (sa + sb) // 2))))
    return struct.pack(f"<{n}h", *mixed)


async def record_all(
    specs: Sequence[ScenarioSpec],
    out_dir: Path,
    *,
    spawn_worker: bool = True,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    worker: asyncio.subprocess.Process | None = None
    if spawn_worker:
        worker = await spawn_agent_worker()
    try:
        for spec in specs:
            logger.info("recording %s", spec.slug)
            results.append(await record_scenario(spec, out_dir, spawn_worker=False))
    finally:
        if worker is not None:
            worker.terminate()
            try:
                await asyncio.wait_for(worker.wait(), timeout=8)
            except TimeoutError:
                worker.kill()
    return results
