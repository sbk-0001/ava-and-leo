"""Unit tests for Ava persona, branch facts, and AGENT_PERSONA switching."""

from persona import (
    BACKEND_INSTRUCTIONS,
    BRANCHES,
    DEFAULT_TELEPHONY_PERSONA,
    DEFAULT_WEB_PERSONA,
    GROUP_NAME,
    VERIFY,
    VOICE_INSTRUCTIONS,
    ava_instructions,
    format_branch_block,
    format_group_clinics,
    get_branch,
    resolve_persona,
    resolve_tool_branch,
)


def test_group_public_name_is_illawarra_dentists() -> None:
    """Inbound phone brand is Illawarra Dentists, not a single clinic."""
    assert GROUP_NAME == "Illawarra Dentists"


def _assert_no_retired_group_names(text: str) -> None:
    leftover = text.replace(GROUP_NAME, "")
    assert "Illawarra Dental" not in leftover
    assert "Illawarra Group" not in leftover
    assert "Illawarra group" not in leftover


def test_branch_ids_cover_the_group() -> None:
    assert set(BRANCHES) == {"shellharbour", "dapto", "woonona"}
    group = format_group_clinics()
    assert "Shellharbour Dentists" in group
    assert "Barrack Heights" in group
    assert "Dapto Dentists" in group
    assert "Woonona Dentists" in group
    assert "Captain Cook Drive" in group
    assert "35 Baan Baan Street" in group
    assert "379 Princes Highway" in group
    assert "Debto" not in group
    assert "Winona" not in group


def test_resolve_tool_branch_uses_chosen_clinic() -> None:
    assert resolve_tool_branch("dapto", "shellharbour") == "dapto"
    assert resolve_tool_branch("woonona", "shellharbour") == "woonona"
    assert resolve_tool_branch(None, "dapto") == "dapto"
    assert resolve_tool_branch("", "shellharbour") == "shellharbour"
    assert resolve_tool_branch("unknown", "dapto") == "shellharbour"


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


def test_voice_instructions_name_ava_not_leo() -> None:
    """Caller-facing identity is Ava. Leo must not remain in spoken copy."""
    spoken = VOICE_INSTRUCTIONS
    lowered = spoken.lower()
    assert "ava" in lowered
    assert "leo" not in lowered
    assert "keep the name ava" in lowered


def test_voice_instructions_answer_as_illawarra_dentists() -> None:
    """First identity is the group number, not Shellharbour Dentists."""
    spoken = VOICE_INSTRUCTIONS
    assert GROUP_NAME in spoken
    assert "phone receptionist for Illawarra Dentists" in spoken
    assert "phone receptionist for the Shellharbour Dentists" not in spoken
    assert "Shellharbour Dentists" in spoken
    assert "Dapto Dentists" in spoken
    assert "Woonona Dentists" in spoken
    assert "Barrack Heights" in spoken
    assert "Debto" not in spoken
    assert "Winona" not in spoken
    _assert_no_retired_group_names(spoken)


def test_voice_instructions_include_human_affect() -> None:
    """Spoken style must stay warm and human, not a stiff script.

    Snapshot-style keyword checks so humour, emotion, and laughter do not
    silently drop out of VOICE_INSTRUCTIONS.
    """
    spoken = VOICE_INSTRUCTIONS.lower()
    assert "humour" in spoken or "humor" in spoken
    assert "laugh" in spoken
    assert "warm" in spoken or "warmth" in spoken
    assert "concern" in spoken or "emotion" in spoken
    assert "relief" in spoken
    assert "pain" in spoken or "emergency" in spoken
    assert "mm-hmm" in spoken
    assert "no worries" in spoken
    assert "illawarra" in spoken
    assert "american" in spoken
    assert "g'day" in spoken
    assert "ai" in spoken
    assert "one question" in spoken
    assert "one or two sentences" in spoken or "one to two" in spoken
    assert "one idea" in spoken
    assert "oh right" in spoken
    assert "lovely" in spoken
    assert "softer" in spoken or "slower" in spoken
    assert "post-op" in spoken or "post op" in spoken
    assert (
        "list dump" in spoken or "robotic list" in spoken or "not a robotic" in spoken
    )
    assert "ssml" in spoken
    assert "[laughs]" in spoken or "stage direction" in spoken
    assert "acknowledg" in spoken


def test_voice_instructions_forbid_ssml_tags() -> None:
    """Realtime marin cannot render SSML or [laughs] tags — instruct affect in prose."""
    spoken = VOICE_INSTRUCTIONS.lower()
    assert "plain speech" in spoken
    assert "never" in spoken and "ssml" in spoken
    assert "stage direction" in spoken or "stage-direction" in spoken


def test_backend_instructions_policy_intact() -> None:
    """Clinic policy lives in BACKEND_INSTRUCTIONS and must not be rewritten away."""
    text = BACKEND_INSTRUCTIONS
    lowered = text.lower()
    assert "CURRENT BRANCH:" in text
    assert "INSTANT FACTS versus TOOLS:" in text
    assert "parking" in lowered
    assert "hours" in lowered
    assert "dentist" in lowered
    assert "VERIFY" in text
    assert "Never invent diary slots" in text
    assert 'Use the word "confirmed" only after' in text
    assert "triple zero, 000" in text
    assert "Do not fill VERIFY gaps." in text
    assert "Quote only canned fees" in text
    assert "no tool" in lowered
    assert "speak before" in lowered or "immediately" in lowered
    assert GROUP_NAME in text
    assert "Shellharbour Dentists" in text
    assert "Dapto Dentists" in text
    assert "Woonona Dentists" in text
    assert "Barrack Heights" in text
    assert "location" in lowered
    assert "availability" in lowered
    assert "Never say Debto or Winona" in text
    assert "group number" in lowered or "not a single clinic" in lowered
    _assert_no_retired_group_names(text)


def test_ava_instructions_are_female_au_voice_with_tool_rules() -> None:
    text = ava_instructions("shellharbour")
    lowered = text.lower()
    spoken = VOICE_INSTRUCTIONS.lower()
    assert "australian" in spoken
    assert "woman" in spoken or "female" in spoken
    assert "nsw" in spoken or "illawarra" in spoken
    assert "american" in spoken
    assert "ava" in lowered
    assert "leo" not in lowered
    assert "confirmed" in lowered
    assert "000" in text
    assert GROUP_NAME in text
    assert "Shellharbour Dentists" in text
    assert "Dapto Dentists" in text
    assert "Woonona Dentists" in text
    assert "never invent" in lowered or "do not invent" in lowered
    assert "Captain Cook Drive" in text
    assert "35 Baan Baan Street" in text
    assert "379 Princes Highway" in text
    assert VOICE_INSTRUCTIONS in text
    assert "Never invent diary slots" in text
    assert "get_availability" in text or "availability" in lowered
    assert "book" in lowered
    _assert_no_retired_group_names(text)


def test_resolve_persona_explicit_env() -> None:
    assert resolve_persona(is_telephony=True, env={"AGENT_PERSONA": "ava"}) == "ava"
    assert (
        resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "generic"})
        == "generic"
    )
    assert resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "AVA"}) == "ava"


def test_resolve_persona_aliases() -> None:
    """Historical names still resolve: leo → ava (dental), ava-generic → generic."""
    assert resolve_persona(is_telephony=True, env={"AGENT_PERSONA": "leo"}) == "ava"
    assert resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "LEO"}) == "ava"
    assert (
        resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "ava-generic"})
        == "generic"
    )


def test_resolve_persona_defaults() -> None:
    assert (
        resolve_persona(is_telephony=True, env={}) == DEFAULT_TELEPHONY_PERSONA == "ava"
    )
    assert (
        resolve_persona(is_telephony=False, env={}) == DEFAULT_WEB_PERSONA == "generic"
    )
    assert resolve_persona(is_telephony=True, env={"AGENT_PERSONA": "nope"}) == "ava"
    assert (
        resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "nope"}) == "generic"
    )
