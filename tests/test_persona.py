"""Unit tests for Leo persona, branch facts, and AGENT_PERSONA switching."""

from persona import (
    BRANCHES,
    DEFAULT_TELEPHONY_PERSONA,
    DEFAULT_WEB_PERSONA,
    VERIFY,
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
    assert "9 to 25 Captain Cook Drive" in branch.address
    assert "Centre Health Complex" in branch.address
    assert branch.phone == "02 4216 9911"
    assert "Captain Cook Drive" in branch.parking
    assert "Monday" in branch.hours or "Mon" in branch.hours
    assert "Dr Mohit Tolani" in branch.dentists
    assert "Dr Pat Pandey" in branch.dentists
    assert "Dr Maryam Kalo" in branch.dentists
    assert "Dr Rick Wasef" in branch.dentists
    assert VERIFY not in (branch.parking, branch.hours)
    assert VERIFY not in branch.dentists


def test_dapto_and_woonona_unverified_fields_are_not_invented() -> None:
    dapto = get_branch("dapto")
    woonona = get_branch("woonona")

    assert dapto.trading_name == "Dapto Dentists"
    assert dapto.address == "35 Baan Baan Street, Dapto"
    assert dapto.phone == "02 4288 0737"
    assert dapto.parking == VERIFY
    assert dapto.hours == VERIFY
    assert dapto.dentists == (VERIFY,)

    assert woonona.trading_name == "Woonona Dentists"
    assert woonona.address == "379 Princes Highway, Woonona"
    assert woonona.phone == "02 4284 4486"
    assert woonona.parking == VERIFY
    assert woonona.hours == VERIFY
    assert woonona.dentists == (VERIFY,)


def test_unknown_branch_falls_back_to_shellharbour() -> None:
    assert get_branch("unknown").id == "shellharbour"
    assert get_branch(None).id == "shellharbour"


def test_branch_block_marks_verify_fields() -> None:
    block = format_branch_block(get_branch("dapto"))
    assert "VERIFY" in block
    assert "Dapto Dentists" in block
    assert "35 Baan Baan Street" in block
    assert "do not invent" in block.lower()


def test_leo_instructions_are_au_voice_with_tool_rules() -> None:
    text = leo_instructions("shellharbour")
    lowered = text.lower()
    assert "australian" in lowered
    assert "leo" in lowered
    assert "confirmed" in lowered
    assert "000" in text
    assert "VERIFY" in text or "verify" in lowered
    assert "Shellharbour Dentists" in text
    assert "never invent" in lowered or "do not invent" in lowered


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
