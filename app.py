"""Vercel entrypoint: the clinic desk portal as one ASGI function.

Vercel looks for ``app`` in a root-level ``app.py``. The portal's modules use
flat imports (``from practice import ...``) with ``src/`` on the path, exactly
as ``uv run uvicorn portal:app --app-dir src`` does locally.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from portal import app

__all__ = ["app"]
