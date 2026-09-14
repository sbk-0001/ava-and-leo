"""Dispatch metadata for outbound calls."""

from make_call import build_dispatch_metadata


def test_dispatch_metadata_asks_agent_not_to_double_dial() -> None:
    raw = build_dispatch_metadata(
        phone_number="+61412345678",
        branch_id="dapto",
        dial_from_agent=False,
    )
    assert '"dial_from_agent": false' in raw
    assert '"branch": "dapto"' in raw
    assert '"phone_number": "+61412345678"' in raw
    assert '"direction": "outbound"' in raw
