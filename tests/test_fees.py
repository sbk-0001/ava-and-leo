"""Fee quoting must only use the product-brief table — never guess."""

from persona import quote_fee


def test_unknown_item_is_unknown() -> None:
    result = quote_fee("filling", "shellharbour")
    assert result["ok"] is False
    assert result["status"] == "unknown"
    assert result["reason"] == "unknown_item"
    assert "amount_aud" not in result
    assert "callback" in result["note"].lower() or "message" in result["note"].lower()


def test_mystery_package_is_not_invented() -> None:
    result = quote_fee("mystery_implant_package", "shellharbour")
    assert result["ok"] is False
    assert result["status"] == "unknown"
    assert "amount_aud" not in result


def test_new_patient_check_up_special() -> None:
    result = quote_fee("check-up and clean", "shellharbour")
    assert result["ok"] is True
    speak = result["speak"].lower()
    assert "gap free" in speak or "gap-free" in speak
    assert "250" in speak
    assert "350" in speak
    assert "not combinable" in speak


def test_published_fees_match_brief() -> None:
    whitening = quote_fee("whitening", "dapto")
    assert whitening["ok"] is True
    assert "650" in whitening["speak"]
    assert "850" in whitening["speak"]

    implant = quote_fee("implant with crown", "woonona")
    assert implant["ok"] is True
    assert "4,500" in implant["speak"] or "4500" in str(implant.get("amount_aud"))

    veneers = quote_fee("veneers", "shellharbour")
    assert veneers["ok"] is True
    assert "1,300" in veneers["speak"] or "1300" in str(veneers.get("amount_aud"))

    wisdom = quote_fee("wisdom tooth removal", "shellharbour")
    assert wisdom["ok"] is True
    assert "350" in wisdom["speak"] and "500" in wisdom["speak"]

    hcf = quote_fee("HCF", "shellharbour")
    assert hcf["ok"] is True
    assert "HCF" in hcf["speak"]
    assert "HICAPS" in hcf["speak"]


def test_fees_are_group_wide() -> None:
    for branch_id in ("shellharbour", "dapto", "woonona"):
        result = quote_fee("check-up", branch_id)
        assert result["ok"] is True, (branch_id, result)
