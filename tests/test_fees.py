"""Canned-fee quoting must never invent a price."""

from persona import VERIFY, quote_fee


def test_unverified_fee_is_not_quoted() -> None:
    result = quote_fee("check_up", "shellharbour")
    assert result["ok"] is False
    assert result["reason"] == "fee_not_verified"
    assert result.get("confirmed") is not True
    assert "amount_aud" not in result or result["amount_aud"] == VERIFY


def test_unknown_item_is_not_invented() -> None:
    result = quote_fee("mystery_implant_package", "shellharbour")
    assert result["ok"] is False
    assert result["reason"] in {"fee_not_verified", "unknown_item"}
    assert result.get("confirmed") is not True
    assert "amount_aud" not in result


def test_known_canned_fee_is_quoted_only_from_table() -> None:
    table = {
        "shellharbour": {"check_up": "185.00"},
        "dapto": {"check_up": VERIFY},
        "woonona": {"check_up": VERIFY},
    }
    quoted = quote_fee("check-up and clean", "shellharbour", table=table)
    assert quoted == {
        "ok": True,
        "item": "check_up",
        "label": "Check-up and clean",
        "amount_aud": "185.00",
        "currency": "AUD",
        "gst": "included",
        "branch_id": "shellharbour",
    }

    unverified = quote_fee("check_up", "dapto", table=table)
    assert unverified["ok"] is False
    assert unverified["reason"] == "fee_not_verified"
    assert unverified.get("confirmed") is not True


def test_default_fee_table_is_all_verify() -> None:
    for branch_id in ("shellharbour", "dapto", "woonona"):
        for item in ("check_up", "exam", "extraction", "whitening"):
            result = quote_fee(item, branch_id)
            assert result["ok"] is False, (branch_id, item, result)
            assert result.get("confirmed") is not True
