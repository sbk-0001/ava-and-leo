"""Canned-fee quoting must never invent a price."""

from persona import VERIFY, quote_fee


def test_unverified_fee_is_not_quoted() -> None:
    result = quote_fee("filling", "shellharbour")
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
        "shellharbour": {"filling": "185.00"},
        "dapto": {"filling": VERIFY},
        "woonona": {"filling": VERIFY},
    }
    quoted = quote_fee("filling", "shellharbour", table=table)
    assert quoted["ok"] is True
    assert quoted["amount_aud"] == "185.00"
    assert quoted["item"] == "filling"

    unverified = quote_fee("filling", "dapto", table=table)
    assert unverified["ok"] is False
    assert unverified["reason"] == "fee_not_verified"
    assert unverified.get("confirmed") is not True


def test_shellharbour_check_up_does_not_pick_one_gospel_amount() -> None:
    result = quote_fee("check-up and clean", "shellharbour")
    amount = str(result.get("amount_aud") or "")
    specials = " ".join(result.get("published_specials") or [])
    note = str(result.get("note") or "")
    blob = f"{amount} {specials} {note}".lower()
    assert "250" in blob
    assert "150" in blob
    assert (
        result.get("amount_aud") in (None, VERIFY, "") or result.get("gospel") is False
    )


def test_shellharbour_published_specials_may_be_quoted() -> None:
    whitening = quote_fee("whitening", "shellharbour")
    assert whitening["ok"] is True
    assert "650" in str(whitening.get("amount_aud") or "") or "650" in str(
        whitening.get("note") or ""
    )

    implant = quote_fee("implant", "shellharbour")
    assert implant["ok"] is True
    assert "5000" in str(implant.get("amount_aud") or implant.get("note") or "")

    veneers = quote_fee("veneers", "shellharbour")
    assert veneers["ok"] is True
    assert "1300" in str(veneers.get("amount_aud") or veneers.get("note") or "")


def test_dapto_and_woonona_unpublished_items_stay_verify() -> None:
    for branch_id in ("dapto", "woonona"):
        for item in ("filling", "extraction", "exam"):
            result = quote_fee(item, branch_id)
            assert result["ok"] is False, (branch_id, item, result)
            assert result.get("confirmed") is not True
