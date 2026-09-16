"""Run mock diary + Ava worker + clinic portal together for local demo.

Usage:
    uv run python src/run_local.py

Then open http://127.0.0.1:8787
"""

from __future__ import annotations

import os
import sys
import threading

from dotenv import load_dotenv

load_dotenv(".env.local")

os.environ.setdefault("AGENT_PERSONA", "ava")
os.environ.setdefault("PRACTICE_SOFTWARE", "mock")
os.environ.setdefault("AVA_REALTIME_VOICE", "marin")


def _portal_bind() -> tuple[str, int]:
    host = os.getenv("PORTAL_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("PORTAL_PORT", "8787") or "8787")
    return host, port


def _start_portal() -> str:
    import uvicorn

    from portal import create_app
    from practice import get_shared_practice

    get_shared_practice(is_telephony=False)
    host, port = _portal_bind()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(),
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="ava-portal", daemon=True)
    thread.start()
    return f"http://{host}:{port}"


# LiveKit Agents subcommands that already start a worker. `cli.run_app()` needs
# one of these; given none it prints the CLI help and exits, which also tears
# down the daemon portal thread started above.
WORKER_SUBCOMMANDS = frozenset({"dev", "start", "console", "connect", "download-files"})
PASSTHROUGH_FLAGS = frozenset(
    {"--help", "-h", "--install-completion", "--show-completion"}
)


def _ensure_worker_subcommand(argv: list[str]) -> list[str]:
    """Default a bare invocation to `dev` so the documented usage works.

    `uv run python src/run_local.py` is what the module docstring and the README
    tell you to run, so it has to start the worker rather than print help.
    Explicit subcommands and help/completion flags pass through untouched.
    """
    args = argv[1:]
    if any(arg in WORKER_SUBCOMMANDS for arg in args):
        return argv
    if any(arg in PASSTHROUGH_FLAGS for arg in args):
        return argv
    return [*argv, "dev"]


def main() -> None:
    url = _start_portal()
    os.environ.setdefault("PORTAL_URL", url)
    os.environ.setdefault("DESK_EVENTS_URL", url)
    password = os.getenv("PORTAL_PASSWORD", "").strip()
    print()
    print(f"Ava clinic portal: {url}")
    print("  Pick a branch, book in the mock diary, then Call Ava.")
    print("  Live call panel shows web + inbound phone transcript and bookings.")
    print("  Desk bus: DESK_EVENTS_URL / PORTAL_URL → POST /api/desk/events")
    if password:
        print(
            "  Auth: PORTAL_PASSWORD is set (shared gate). SSE accepts cookie or ?token=."
        )
    else:
        print("  Auth: open on localhost. Set PORTAL_PASSWORD before exposing it.")
    print("  Agent worker starting in this process (AGENT_PERSONA=ava, mock diary).")
    print()

    from livekit.agents import cli

    from agent import server

    sys.argv = _ensure_worker_subcommand(sys.argv)
    cli.run_app(server)


if __name__ == "__main__":
    main()
