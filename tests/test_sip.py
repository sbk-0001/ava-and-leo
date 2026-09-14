"""Inbound DID mapping and SIP disconnect shutdown rules."""

from unittest.mock import MagicMock

from sip_utils import (
    branch_from_did,
    branch_from_participant,
    normalize_au_phone,
    parse_job_metadata,
    parse_sip_did_map,
    should_shutdown_on_disconnect,
    sip_error_details,
)


def test_normalize_au_phone_variants() -> None:
    assert normalize_au_phone("02 4216 9911") == "+61242169911"
    assert normalize_au_phone("(02) 4288-0737") == "+61242880737"
    assert normalize_au_phone("+61 2 4284 4486") == "+61242844486"
    assert normalize_au_phone("0242169911") == "+61242169911"
    assert normalize_au_phone("61242169911") == "+61242169911"
    assert normalize_au_phone("+61242169911") == "+61242169911"
    assert normalize_au_phone("0412 345 678") == "+61412345678"


def test_did_map_routes_group_numbers() -> None:
    raw = "+61242169911:shellharbour,02 4288 0737:dapto,+61 2 4284 4486:woonona"
    mapping = parse_sip_did_map(raw)
    assert mapping[normalize_au_phone("+61242169911")] == "shellharbour"
    assert branch_from_did("02 4216 9911", mapping) == "shellharbour"
    assert branch_from_did("0242880737", mapping) == "dapto"
    assert branch_from_did("+61242844486", mapping) == "woonona"


def test_unknown_did_falls_back_to_default_branch() -> None:
    mapping = parse_sip_did_map("+61242169911:shellharbour")
    assert branch_from_did("+61040000000", mapping) == "shellharbour"
    assert branch_from_did("", mapping) == "shellharbour"
    assert branch_from_did(None, mapping) == "shellharbour"


def test_empty_did_map_is_safe() -> None:
    assert parse_sip_did_map("") == {}
    assert parse_sip_did_map(None) == {}


def test_parse_job_metadata() -> None:
    assert parse_job_metadata(None) == {}
    assert parse_job_metadata("") == {}
    assert parse_job_metadata("not-json") == {}
    assert parse_job_metadata('{"phone_number": "+61400000000"}') == {
        "phone_number": "+61400000000"
    }


def test_sip_error_details_from_twirp_metadata() -> None:
    from livekit.api import TwirpError

    exc = TwirpError(
        "unavailable",
        "callee busy",
        status=486,
        metadata={"sip_status_code": "486", "sip_status": "Busy Here"},
    )
    assert sip_error_details(exc) == ("486", "Busy Here")


def test_shutdown_on_disconnect_reasons() -> None:
    # LiveKit docs: USER_UNAVAILABLE and SIP_TRUNK_FAILURE do not auto-close.
    assert should_shutdown_on_disconnect("USER_UNAVAILABLE") is True
    assert should_shutdown_on_disconnect("SIP_TRUNK_FAILURE") is True
    assert should_shutdown_on_disconnect("CLIENT_INITIATED") is False
    assert should_shutdown_on_disconnect("USER_REJECTED") is False
    assert should_shutdown_on_disconnect("ROOM_DELETED") is False


def test_magicmock_participant_did_does_not_crash() -> None:
    """Console / tests may pass a MagicMock participant with no real SIP DID."""
    participant = MagicMock()
    assert branch_from_participant(participant) == "shellharbour"
    assert normalize_au_phone(MagicMock()) == ""
    assert normalize_au_phone(None) == ""
