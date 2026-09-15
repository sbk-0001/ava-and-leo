"""Process-start asserts for Thursday live: SIP host, live OpenAI key, 429 breaker."""

from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger("ava.startup")

DEFAULT_SIP_HOST = "5sft82r5337.sip.livekit.cloud"


def expected_sip_host(env: Mapping[str, str] | None = None) -> str:
    environ = env if env is not None else {}
    raw = str(environ.get("EXPECTED_SIP_HOST") or "").strip()
    return raw or DEFAULT_SIP_HOST


def _configured_sip_host(env: Mapping[str, str]) -> str:
    for key in ("LIVEKIT_SIP_HOST", "SIP_HOST", "TELNYX_SIP_HOST"):
        raw = str(env.get(key) or "").strip()
        if raw:
            return raw.replace("sip:", "").split(":")[0].rstrip("/")
    return ""


def assert_sip_host(env: Mapping[str, str] | None = None) -> str:
    """Telnyx/LiveKit SIP must be the expected host, or EXPECTED_SIP_HOST."""
    import os

    environ = dict(env if env is not None else os.environ)
    expected = expected_sip_host(environ)
    actual = _configured_sip_host(environ)
    if actual and actual != expected:
        raise RuntimeError(
            f"SIP host {actual!r} does not match expected {expected!r}. "
            "Set LIVEKIT_SIP_HOST or EXPECTED_SIP_HOST."
        )
    logger.info("sip host ok expected=%s configured=%s", expected, actual or expected)
    return expected


def assert_filler_audio_bank(env: Mapping[str, str] | None = None) -> None:
    """Boot-fatal if the pre-rendered filler bank is missing or a pool is short."""
    from filler_bank import get_filler_bank

    del env
    # Load once. Play path must never re-run synthesize_pcm / assert.
    get_filler_bank()


def assert_live_openai_key(env: Mapping[str, str] | None = None) -> None:
    """Live OPENAI_API_KEY must not be the demo/harness key when both are set."""
    import os

    environ = dict(env if env is not None else os.environ)
    live = str(
        environ.get("OPENAI_LIVE_API_KEY") or environ.get("OPENAI_API_KEY") or ""
    ).strip()
    demo = str(
        environ.get("OPENAI_DEMO_API_KEY")
        or environ.get("OPENAI_HARNESS_API_KEY")
        or environ.get("DEMO_OPENAI_API_KEY")
        or ""
    ).strip()
    if live and demo and live == demo:
        raise RuntimeError(
            "OPENAI_API_KEY for live must not be the DEMO/HARNESS key. "
            "Set OPENAI_LIVE_API_KEY on a separate OpenAI project / TPM pool."
        )
    live_only = str(environ.get("OPENAI_LIVE_API_KEY") or "").strip()
    api = str(environ.get("OPENAI_API_KEY") or "").strip()
    if live_only and api and live_only != api:
        logger.warning(
            "OPENAI_LIVE_API_KEY differs from OPENAI_API_KEY; "
            "Realtime will use OPENAI_API_KEY unless you swap them."
        )
    logger.info("openai live key distinct from demo/harness=%s", bool(demo))


class RateLimitCircuitBreaker:
    """Stub: repeated 429s open the breaker toward OVERFLOW_NUMBER."""

    def __init__(self, *, threshold: int = 3) -> None:
        self.threshold = threshold
        self.hits = 0
        self.open = False

    def record_429(self) -> bool:
        self.hits += 1
        if self.hits >= self.threshold:
            self.open = True
            logger.warning(
                "429 circuit breaker OPEN after %s hits; overflow=%s",
                self.hits,
                self.overflow_number(),
            )
        return self.open

    def overflow_number(self, env: Mapping[str, str] | None = None) -> str | None:
        import os

        environ = env if env is not None else os.environ
        raw = str(
            environ.get("OVERFLOW_NUMBER") or environ.get("AVA_OVERFLOW_NUMBER") or ""
        ).strip()
        return raw or None
