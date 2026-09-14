"""Vercel FastAPI entrypoint for the Strategybyte Ava desk.

Supported Vercel filename: src/main.py. Local equivalent:
    uv run uvicorn portal:app --app-dir src --host 127.0.0.1 --port 8787
"""

from portal import app

__all__ = ["app"]
