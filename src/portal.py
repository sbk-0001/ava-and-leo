"""Clinic-staff web portal for Ava (Shellharbour / Dapto / Woonona).

Serves branch facts, the mock diary, booking mutations, and a LiveKit token
so the browser can talk to the running Ava worker. Secrets stay on the server.

Docs: https://docs.livekit.io/agents/server/agent-dispatch/
      https://docs.livekit.io/frontends/build/authentication/
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from desk_events import DeskBus
from persona import BRANCHES, branch_as_dict, get_branch
from practice import PracticeClient, get_shared_practice

load_dotenv(".env.local")

AGENT_NAME = "ava-and-leo"
STATIC_DIR = Path(__file__).parent / "portal_static"
COOKIE_NAME = "ava_portal"


class LoginBody(BaseModel):
    password: str


class BookBody(BaseModel):
    branch_id: str
    slot_id: str
    reason: str = "appointment"
    name: str | None = None
    phone: str | None = None
    patient_id: str | None = None
    date_of_birth: str | None = None


class RescheduleBody(BaseModel):
    new_slot_id: str


class TokenBody(BaseModel):
    branch_id: str = "shellharbour"
    identity: str | None = None


class FindPatientBody(BaseModel):
    name: str
    phone: str | None = None
    date_of_birth: str | None = None


def _cookie_digest(password: str) -> str:
    return hashlib.sha256(f"ava-portal:{password}".encode()).hexdigest()


def _secrets_match(provided: str, expected: str) -> bool:
    if not provided or not expected or len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    if request.headers.get("x-forwarded-for"):
        return False
    return host in {"127.0.0.1", "::1", "localhost"}


def create_app(
    *,
    practice: PracticeClient | None = None,
    require_auth: bool | None = None,
    portal_password: str | None = None,
) -> FastAPI:
    """Build the portal app. Tests pass an in-memory PracticeClient."""
    app = FastAPI(title="Ava desk — Shellharbour · Dapto · Woonona", docs_url=None)
    app.state.practice = practice
    app.state.desk_bus = DeskBus()
    app.state.portal_password = (
        portal_password
        if portal_password is not None
        else os.getenv("PORTAL_PASSWORD", "").strip()
    )
    app.state.require_auth = require_auth

    def _practice() -> PracticeClient:
        return app.state.practice or get_shared_practice(is_telephony=False)

    def _auth(request: Request) -> None:
        password = app.state.portal_password
        forced = app.state.require_auth
        if forced is False:
            return
        if not password:
            if _is_loopback(request) and forced is not True:
                return
            raise HTTPException(
                status_code=401,
                detail=(
                    "This portal is not public. Set PORTAL_PASSWORD, or open it "
                    "on localhost for a demo."
                ),
            )
        cookie = request.cookies.get(COOKIE_NAME, "")
        token = request.query_params.get("token", "")
        expected = _cookie_digest(password)
        if _secrets_match(cookie, expected):
            return
        if _secrets_match(token, expected) or _secrets_match(token, password):
            return
        raise HTTPException(
            status_code=401,
            detail="Sign in with PORTAL_PASSWORD.",
        )

    def _worker_auth(request: Request) -> None:
        """Accept desk events from the local Ava worker, not the public internet."""
        token = os.getenv("DESK_EVENTS_TOKEN", "").strip()
        header = request.headers.get("x-desk-token", "")
        if token and _secrets_match(header, token):
            return
        if app.state.require_auth is False:
            return
        if _is_loopback(request):
            return
        raise HTTPException(
            status_code=403,
            detail="Desk events are only accepted from the local worker.",
        )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"ok": "true", "service": "ava-portal"}

    @app.get("/api/config")
    async def config(request: Request) -> dict[str, Any]:
        password = app.state.portal_password
        signed_in = False
        if password:
            cookie = request.cookies.get(COOKIE_NAME, "")
            signed_in = bool(cookie) and hmac.compare_digest(
                cookie, _cookie_digest(password)
            )
        loopback = _is_loopback(request)
        auth_required = bool(password) or (
            app.state.require_auth is not False and not loopback
        )
        return {
            "auth_required": auth_required and not signed_in,
            "signed_in": signed_in or not auth_required,
            "loopback": loopback,
            "auth_note": (
                "Open on localhost for demo, or set PORTAL_PASSWORD."
                if not password
                else "Shared password gate is enabled."
            ),
            "persona": "ava",
            "voice": os.getenv("AVA_REALTIME_VOICE")
            or os.getenv("LEO_REALTIME_VOICE", "marin")
            or "marin",
            "practice_mode": _practice().mode,
        }

    @app.post("/api/login")
    async def login(body: LoginBody, response: Response) -> dict[str, bool]:
        password = app.state.portal_password
        if not password or not hmac.compare_digest(body.password, password):
            raise HTTPException(status_code=401, detail="Wrong password.")
        response.set_cookie(
            COOKIE_NAME,
            _cookie_digest(password),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 12,
        )
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME)
        return {"ok": True}

    @app.get("/api/branches")
    async def branches(request: Request) -> dict[str, Any]:
        _auth(request)
        return {"branches": [branch_as_dict(branch) for branch in BRANCHES.values()]}

    @app.get("/api/branches/{branch_id}")
    async def branch(branch_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        if branch_id not in BRANCHES:
            raise HTTPException(status_code=404, detail="Unknown branch.")
        return branch_as_dict(get_branch(branch_id))

    @app.get("/api/diary")
    async def diary(
        request: Request,
        branch_id: str = "shellharbour",
        date_from: str | None = None,
        date_to: str | None = None,
        clinician: str | None = None,
    ) -> dict[str, Any]:
        _auth(request)
        return await _practice().list_diary(
            branch_id=get_branch(branch_id).id,
            date_from=date_from,
            date_to=date_to,
            clinician=clinician,
        )

    @app.get("/api/availability")
    async def availability(
        request: Request,
        branch_id: str,
        date: str,
        clinician: str | None = None,
    ) -> dict[str, Any]:
        _auth(request)
        return await _practice().get_availability(
            branch_id=get_branch(branch_id).id,
            date=date,
            clinician=clinician,
        )

    @app.post("/api/patients/find")
    async def find_patient(body: FindPatientBody, request: Request) -> dict[str, Any]:
        _auth(request)
        return await _practice().find_patient(
            name=body.name, phone=body.phone, date_of_birth=body.date_of_birth
        )

    @app.post("/api/bookings")
    async def book(body: BookBody, request: Request) -> dict[str, Any]:
        _auth(request)
        result = await _practice().book_appointment(
            branch_id=get_branch(body.branch_id).id,
            slot_id=body.slot_id,
            reason=body.reason,
            patient_id=body.patient_id,
            name=body.name,
            phone=body.phone,
            date_of_birth=body.date_of_birth,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/api/bookings/{booking_id}/reschedule")
    async def reschedule(
        booking_id: str, body: RescheduleBody, request: Request
    ) -> dict[str, Any]:
        _auth(request)
        result = await _practice().reschedule_appointment(
            booking_id=booking_id, new_slot_id=body.new_slot_id
        )
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/api/bookings/{booking_id}/cancel")
    async def cancel(booking_id: str, request: Request) -> dict[str, Any]:
        _auth(request)
        result = await _practice().cancel_appointment(booking_id=booking_id)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.post("/api/token")
    async def token(body: TokenBody, request: Request) -> dict[str, str]:
        """Mint a LiveKit room token that dispatches Ava.

        Docs: https://docs.livekit.io/agents/server/agent-dispatch/
        """
        _auth(request)
        url = os.getenv("LIVEKIT_URL", "").strip()
        api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
        api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
        if not url or not api_key or not api_secret:
            raise HTTPException(
                status_code=503,
                detail=(
                    "LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET are "
                    "required to call Ava from the browser."
                ),
            )

        from livekit.api import (
            AccessToken,
            RoomAgentDispatch,
            RoomConfiguration,
            VideoGrants,
        )

        branch = get_branch(body.branch_id)
        room = f"ava-portal-{branch.id}-{uuid.uuid4().hex[:8]}"
        identity = body.identity or f"staff-{uuid.uuid4().hex[:6]}"
        metadata = json.dumps(
            {
                "persona": "ava",
                "branch": branch.id,
                "source": "portal",
            }
        )
        jwt = (
            AccessToken(api_key, api_secret)
            .with_identity(identity)
            .with_name("Clinic staff")
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
            .with_room_config(
                RoomConfiguration(
                    agents=[RoomAgentDispatch(agent_name=AGENT_NAME, metadata=metadata)]
                )
            )
            .to_jwt()
        )
        return {"token": jwt, "url": url, "room": room, "branch_id": branch.id}

    @app.post("/api/desk/events")
    async def desk_events(request: Request) -> dict[str, bool]:
        """Local worker publishes live transcript/activity for the desk SSE bus."""
        _worker_auth(request)
        try:
            packet = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON.") from exc
        if not isinstance(packet, dict) or packet.get("type") not in {
            "transcript",
            "activity",
        }:
            raise HTTPException(
                status_code=400,
                detail="Expected a desk transcript or activity packet.",
            )
        app.state.desk_bus.publish(packet)
        return {"ok": True}

    @app.get("/api/desk/stream")
    async def desk_stream(request: Request) -> StreamingResponse:
        """Signed-in staff subscribe to live desk events (SSE). Cookie or ?token=."""
        _auth(request)
        queue = app.state.desk_bus.subscribe()

        async def generate():
            try:
                yield "event: ready\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        packet = await asyncio.wait_for(queue.get(), timeout=20)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(packet, default=str)}\n\n"
            finally:
                app.state.desk_bus.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
