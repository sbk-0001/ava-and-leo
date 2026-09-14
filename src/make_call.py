"""Place an outbound call: dispatch the agent, then CreateSIPParticipant.

Usage:
    uv run python src/make_call.py --to +61400000000
    uv run python src/make_call.py --to +61400000000 --branch dapto

Requires LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, and
SIP_OUTBOUND_TRUNK_ID. The agent worker must already be running
(`uv run python src/agent.py dev` or `start`).

Docs: https://docs.livekit.io/telephony/making-calls/outbound-calls/
      https://docs.livekit.io/agents/server/agent-dispatch/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from livekit import api

from persona import DEFAULT_BRANCH_ID, get_branch
from sip_utils import (
    SIP_CALL_ERRORS,
    normalize_au_phone,
    sip_error_details,
)

load_dotenv(".env.local")

logger = logging.getLogger("make_call")

AGENT_NAME = "ava-and-leo"


def build_dispatch_metadata(
    *,
    phone_number: str,
    branch_id: str,
    dial_from_agent: bool = False,
) -> str:
    return json.dumps(
        {
            "phone_number": phone_number,
            "branch": branch_id,
            "dial_from_agent": dial_from_agent,
            "direction": "outbound",
        }
    )


async def place_outbound_call(
    *,
    phone_number: str,
    branch_id: str = DEFAULT_BRANCH_ID,
    room_name: str | None = None,
    trunk_id: str | None = None,
    wait_until_answered: bool = True,
) -> api.SIPParticipantInfo:
    """Dispatch Ava, then dial with CreateSIPParticipant(wait_until_answered)."""
    phone = normalize_au_phone(phone_number) or phone_number
    branch = get_branch(branch_id)
    trunk = trunk_id or os.getenv("SIP_OUTBOUND_TRUNK_ID", "").strip()
    if not trunk:
        raise RuntimeError(
            "SIP_OUTBOUND_TRUNK_ID is not set. Create an outbound trunk and put "
            "its ID in .env.local (lk sip outbound list)."
        )

    room = room_name or (
        f"ava-{branch.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    metadata = build_dispatch_metadata(
        phone_number=phone,
        branch_id=branch.id,
        dial_from_agent=False,
    )

    async with api.LiveKitAPI() as lkapi:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room,
                metadata=metadata,
            )
        )
        logger.info("Dispatched %s to room %s", AGENT_NAME, room)

        request = api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk,
            sip_call_to=phone,
            room_name=room,
            participant_identity=phone,
            participant_name="Ava callee",
            wait_until_answered=wait_until_answered,
            play_dialtone=True,
        )
        sip_number = os.getenv("SIP_OUTBOUND_NUMBER", "").strip()
        if sip_number:
            request.sip_number = sip_number

        try:
            participant = await lkapi.sip.create_sip_participant(request)
        except SIP_CALL_ERRORS as exc:
            code, status = sip_error_details(exc)
            logger.error("SIP call failed: %s %s", code, status)
            raise

        logger.info("Call answered: %s", participant)
        return participant


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dispatch the Ava agent and place an outbound SIP call.",
    )
    parser.add_argument(
        "--to", required=True, help="Phone number to dial (E.164 preferred)."
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH_ID,
        choices=("shellharbour", "dapto", "woonona"),
        help="Practice branch for this call (default: shellharbour).",
    )
    parser.add_argument("--room", default=None, help="LiveKit room name (optional).")
    parser.add_argument(
        "--trunk-id",
        default=None,
        help="Outbound SIP trunk ID (default: SIP_OUTBOUND_TRUNK_ID).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args(argv)
    try:
        asyncio.run(
            place_outbound_call(
                phone_number=args.to,
                branch_id=args.branch,
                room_name=args.room,
                trunk_id=args.trunk_id,
            )
        )
    except SIP_CALL_ERRORS as exc:
        code, status = sip_error_details(exc)
        print(f"Call failed: {code} {status}".strip() or exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Call failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
