"""Run mock diary + Leo worker + clinic portal together for local demo.

Usage:
    uv run python src/run_local.py

Then open http://127.0.0.1:8787
"""

from __future__ import annotations

import os
import threading

from dotenv import load_dotenv

load_dotenv(".env.local")

os.environ.setdefault("AGENT_PERSONA", "leo")
os.environ.setdefault("PRACTICE_SOFTWARE", "mock")
os.environ.setdefault("LEO_REALTIME_VOICE", "marin")


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
    thread = threading.Thread(target=server.run, name="leo-portal", daemon=True)
    thread.start()
    return f"http://{host}:{port}"


def main() -> None:
    url = _start_portal()
    password = os.getenv("PORTAL_PASSWORD", "").strip()
    print()
    print(f"Leo clinic portal: {url}")
    print("  Pick a branch, book in the mock diary, then Call Leo.")
    if password:
        print("  Auth: PORTAL_PASSWORD is set (shared gate).")
    else:
        print("  Auth: open on localhost. Set PORTAL_PASSWORD before exposing it.")
    print("  Agent worker starting in this process (AGENT_PERSONA=leo, mock diary).")
    print()

    from livekit.agents import cli

    from agent import server

    cli.run_app(server)


if __name__ == "__main__":
    main()
