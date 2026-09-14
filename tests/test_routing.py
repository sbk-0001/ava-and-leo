"""Location → nearby clinic and cross-clinic preferred-dentist routing."""

from datetime import date

from persona import (
    BRANCHES,
    WEEKDAY_ROSTER,
    lookup_clinician,
    rostered_clinicians,
    suggest_clinics_for_location,
)
from practice import PracticeClient, seed_mock_diary

MONDAY = date(2026, 9, 14)  # Canonical demo day: Mohit at Shellharbour, not Dapto.
TUESDAY = date(2026, 9, 15)


def test_dapto_and_nearby_suburbs_rank_dapto_first() -> None:
    for query in ("Dapto", "I'm in Dapto", "near Koonawarra", "Horsley"):
        result = suggest_clinics_for_location(query, on_date=MONDAY)
        assert result["ok"] is True
        assert result["matched"] is True
        assert result["nearest_branch_id"] == "dapto"
        assert result["branch_ids"][0] == "dapto"
        names = [clinic["trading_name"] for clinic in result["clinics"]]
        assert names[0] == "Dapto Dentists"


def test_warilla_and_barrack_heights_rank_shellharbour_first() -> None:
    for query in ("Warilla", "Barrack Heights", "Shellharbour", "Oak Flats"):
        result = suggest_clinics_for_location(query, on_date=MONDAY)
        assert result["ok"] is True, query
        assert result["matched"] is True, query
        assert result["nearest_branch_id"] == "shellharbour", query


def test_woonona_and_north_coast_rank_woonona_first() -> None:
    for query in ("Woonona", "Bulli", "Thirroul", "Corrimal", "Fairy Meadow"):
        result = suggest_clinics_for_location(query, on_date=MONDAY)
        assert result["ok"] is True, query
        assert result["nearest_branch_id"] == "woonona", query


def test_unknown_suburb_does_not_guess_a_nearest_clinic() -> None:
    result = suggest_clinics_for_location("Bondi", on_date=MONDAY)
    assert result["ok"] is True
    assert result["matched"] is False
    assert result.get("nearest_branch_id") in (None, "")
    assert "do not guess" in result["note"].lower()
    assert {clinic["branch_id"] for clinic in result["clinics"]} == set(BRANCHES)


def test_nearby_clinics_include_todays_roster_not_the_full_site_list() -> None:
    result = suggest_clinics_for_location("Dapto", on_date=MONDAY)
    dapto = next(
        clinic for clinic in result["clinics"] if clinic["branch_id"] == "dapto"
    )
    assert "Dr Beena Kurian" in dapto["rostered_today"]
    assert "Dr Mohit Tolani" not in dapto["rostered_today"]
    shellharbour = next(
        clinic for clinic in result["clinics"] if clinic["branch_id"] == "shellharbour"
    )
    assert "Dr Mohit Tolani" in shellharbour["rostered_today"]


def test_roster_only_uses_dentists_listed_on_that_branch_site() -> None:
    for branch_id, by_weekday in WEEKDAY_ROSTER.items():
        listed = set(BRANCHES[branch_id].dentists)
        for weekday, names in by_weekday.items():
            assert 0 <= weekday <= 5
            for name in names:
                assert name in listed, (branch_id, weekday, name)


def test_monday_mohit_is_at_shellharbour_not_dapto() -> None:
    assert "Dr Mohit Tolani" in rostered_clinicians("shellharbour", MONDAY)
    assert "Dr Mohit Tolani" not in rostered_clinicians("dapto", MONDAY)
    assert "Dr Mohit Tolani" not in rostered_clinicians("woonona", MONDAY)


def test_preferred_dentist_offers_farther_group_clinic() -> None:
    """Dapto-area caller wants Dr Mohit on Monday → offer Shellharbour."""
    result = lookup_clinician(
        "Dr Mohit Tolani",
        on_date=MONDAY,
        near_branch_id="dapto",
    )
    assert result["ok"] is True
    assert result["clinician"] == "Dr Mohit Tolani"
    assert result["available_at_nearest"] is False
    assert "dapto" not in result["rostered_at"]
    assert "shellharbour" in result["rostered_at"]
    offer = result["offer_other_clinic"]
    assert offer is not None
    assert offer["branch_id"] == "shellharbour"
    assert "Shellharbour Dentists" in offer["trading_name"]
    assert "Barrack Heights" in offer["suburb"]
    note = offer["note"].lower()
    assert "not rostered" in note or "not at" in note
    assert "shellharbour" in note or "barrack heights" in note


def test_preferred_dentist_short_name_resolves() -> None:
    result = lookup_clinician("Mohit", on_date=MONDAY, near_branch_id="dapto")
    assert result["ok"] is True
    assert result["clinician"] == "Dr Mohit Tolani"
    assert result["offer_other_clinic"]["branch_id"] == "shellharbour"


def test_preferred_dentist_at_nearest_clinic_does_not_redirect() -> None:
    result = lookup_clinician(
        "Dr Mohit Tolani",
        on_date=TUESDAY,
        near_branch_id="dapto",
    )
    assert result["ok"] is True
    assert result["available_at_nearest"] is True
    assert result["offer_other_clinic"] is None
    assert "dapto" in result["rostered_at"]


def test_unknown_dentist_is_not_invented() -> None:
    result = lookup_clinician("Dr Nobody Smith", on_date=MONDAY)
    assert result["ok"] is False
    assert result["reason"] == "clinician_not_found"
    assert "invent" in result["note"].lower()


def test_seeded_diary_follows_monday_roster_split() -> None:
    client = PracticeClient(mode="mock")
    seed_mock_diary(client, today=MONDAY, days=7)
    monday = MONDAY.isoformat()
    dapto_names = {
        slot.clinician
        for slot in client.slots.values()
        if slot.branch_id == "dapto" and slot.date == monday
    }
    sh_names = {
        slot.clinician
        for slot in client.slots.values()
        if slot.branch_id == "shellharbour" and slot.date == monday
    }
    assert dapto_names
    assert sh_names
    assert "Dr Mohit Tolani" not in dapto_names
    assert "Dr Mohit Tolani" in sh_names
    assert "Dr Beena Kurian" in dapto_names
