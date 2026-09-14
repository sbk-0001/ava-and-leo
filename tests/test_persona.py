"""Unit tests for Leo persona, branch facts, and AGENT_PERSONA switching."""

from persona import (
    BRANCHES,
    DEFAULT_TELEPHONY_PERSONA,
    DEFAULT_WEB_PERSONA,
    VERIFY,
    VOICE_INSTRUCTIONS,
    format_branch_block,
    get_branch,
    leo_instructions,
    resolve_persona,
)


def test_branch_ids_cover_the_group() -> None:
    assert set(BRANCHES) == {"shellharbour", "dapto", "woonona"}


def test_shellharbour_facts_are_known() -> None:
    branch = get_branch("shellharbour")
    assert branch.trading_name == "Shellharbour Dentists"
    assert branch.suburb == "Barrack Heights"
    assert "Suite 7" in branch.address
    assert "Captain Cook Drive" in branch.address
    assert "2528" in branch.address
    assert "Centre Health Complex" in branch.address
    assert "02 4216 9911" in branch.phone
    assert "Captain Cook Drive" in branch.parking
    assert "Centre Health Complex" in branch.parking
    assert VERIFY not in (branch.parking, branch.hours)
    assert "Monday" in branch.hours or "Mon" in branch.hours
    assert "8" in branch.hours and "5" in branch.hours
    assert "Saturday" in branch.hours or "Saturday" in branch.hours.lower()
    names = " ".join(branch.dentists)
    for dentist in (
        "Dr Mohit Tolani",
        "Dr Amy Min",
        "Dr Pat Pandey",
        "Dr Maryam Kalo",
        "Dr Rick Wasef",
    ):
        assert dentist in names
    assert VERIFY not in branch.dentists
    assert "Spanish" in branch.languages
    assert "Hindi" in branch.languages
    assert "Arabic" in branch.languages
    assert "50" in branch.cancellation


def test_dapto_facts_are_known() -> None:
    dapto = get_branch("dapto")
    assert dapto.trading_name == "Dapto Dentists"
    assert "35 Baan Baan Street" in dapto.address
    assert "2530" in dapto.address
    assert "02 4288 0737" in dapto.phone
    assert VERIFY not in (dapto.parking, dapto.hours)
    assert "Mall Lane" in dapto.parking
    assert "rear" in dapto.parking.lower()
    assert "6" in dapto.hours
    assert "Saturday" in dapto.hours
    names = " ".join(dapto.dentists)
    for dentist in (
        "Dr Beena Kurian",
        "Dr Irena K. Stojkovski",
        "Dr Pat Pandey",
        "Dr Ayesha Panta",
        "Dr Mohit Tolani",
        "Dr Amy Min",
        "Dr Omar Ahsan",
    ):
        assert dentist in names
    assert VERIFY not in dapto.dentists
    assert "Malayalam" in dapto.languages
    assert "Macedonian" in dapto.languages


def test_woonona_facts_are_known() -> None:
    woonona = get_branch("woonona")
    assert woonona.trading_name == "Woonona Dentists"
    assert "379 Princes Highway" in woonona.address
    assert "2517" in woonona.address
    assert "02 4284 4486" in woonona.phone
    assert VERIFY not in (woonona.parking, woonona.hours)
    assert "Haddon" in woonona.parking
    assert "IGA" in woonona.parking
    assert "6" in woonona.hours
    assert "Saturday" in woonona.hours
    names = " ".join(woonona.dentists)
    for dentist in (
        "Dr Beena Kurian",
        "Dr Natasha Khushalani",
        "Dr Abha Verma",
        "Dr Ayesha Panta",
        "Dr Chin Valsan",
    ):
        assert dentist in names
    assert VERIFY not in woonona.dentists


def test_unknown_branch_falls_back_to_shellharbour() -> None:
    assert get_branch("unknown").id == "shellharbour"
    assert get_branch(None).id == "shellharbour"


def test_branch_block_includes_instant_facts() -> None:
    block = format_branch_block(get_branch("dapto"))
    assert "Dapto Dentists" in block
    assert "35 Baan Baan Street" in block
    assert "Mall Lane" in block
    assert "Dr Beena Kurian" in block
    assert "do not invent" in block.lower() or "VERIFY" in block


def test_leo_instructions_are_female_au_voice_with_tool_rules() -> None:
    text = leo_instructions("shellharbour")
    lowered = text.lower()
    spoken = VOICE_INSTRUCTIONS.lower()
    assert "australian" in spoken
    assert "woman" in spoken or "female" in spoken
    assert "nsw" in spoken or "illawarra" in spoken
    assert "american" in spoken
    assert "leo" in lowered
    assert "confirmed" in lowered
    assert "000" in text
    assert "Shellharbour Dentists" in text
    assert "never invent" in lowered or "do not invent" in lowered
    assert "Captain Cook Drive" in text


def test_resolve_persona_explicit_env() -> None:
    assert resolve_persona(is_telephony=True, env={"AGENT_PERSONA": "ava"}) == "ava"
    assert resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "leo"}) == "leo"
    assert resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "LEO"}) == "leo"


def test_resolve_persona_defaults() -> None:
    assert (
        resolve_persona(is_telephony=True, env={}) == DEFAULT_TELEPHONY_PERSONA == "leo"
    )
    assert resolve_persona(is_telephony=False, env={}) == DEFAULT_WEB_PERSONA == "ava"
    assert resolve_persona(is_telephony=True, env={"AGENT_PERSONA": "nope"}) == "leo"
    assert resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "nope"}) == "ava"
