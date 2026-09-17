"""Unit tests for Ava persona switching and brief-only clinic facts."""

from persona import (
    BRANCHES,
    DEFAULT_TELEPHONY_PERSONA,
    DEFAULT_WEB_PERSONA,
    INSTRUCTIONS_PATH,
    VERIFY,
    VOICE_INSTRUCTIONS,
    ava_instructions,
    format_branch_block,
    format_group_clinics,
    get_branch,
    resolve_persona,
    resolve_tool_branch,
)


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


def test_resolve_tool_branch_uses_chosen_clinic() -> None:
    assert resolve_tool_branch("dapto", "shellharbour") == "dapto"
    assert resolve_tool_branch("woonona", "shellharbour") == "woonona"
    assert resolve_tool_branch(None, "dapto") == "dapto"
    assert resolve_tool_branch("", "shellharbour") == "shellharbour"
    assert resolve_tool_branch("unknown", "dapto") == "shellharbour"


def test_shellharbour_facts_match_brief() -> None:
    branch = get_branch("shellharbour")
    assert branch.trading_name == "Shellharbour Dentists"
    assert branch.suburb == "Barrack Heights"
    assert "Suite 7" in branch.address
    assert "Captain Cook Drive" in branch.address
    assert "2528" in branch.address
    assert "Centre Health Complex" in branch.address
    assert "4216 9911" in branch.phone
    assert "Captain Cook Drive" in branch.parking
    assert VERIFY not in (branch.parking, branch.hours)
    assert "8:00" in branch.hours or "8:00am" in branch.hours
    names = " ".join(branch.dentists)
    for dentist in (
        "Dr Mohit Tolani",
        "Dr Pat Pandey",
        "Dr Maryam Kalo",
        "Dr Rick Wasef",
    ):
        assert dentist in names
    assert "Amy Min" not in names
    assert "Hindi" in branch.languages
    assert "Sindhi" in branch.languages
    assert "Spanish" in branch.languages
    assert "English" in branch.languages
    assert "Arabic" not in branch.languages
    assert "50" in branch.cancellation
    assert "2024" in branch.notes
    assert "Centre Health Dental" in branch.notes


DAPTO_DENTISTS = (
    "Dr Beena Kurian",
    "Dr Irena Stojkovski",
    "Dr Pat Pandey",
    "Dr Ayesha Panta",
    "Dr Mohit Tolani",
    "Dr Amy Min",
    "Dr Omar Ahsan",
)
WOONONA_DENTISTS = (
    "Dr Beena Kurian",
    "Dr Natasha Khushalani",
    "Dr Abha Verma",
    "Dr Ayesha Panta",
    "Dr Chin Valsan",
)


def test_dapto_and_woonona_name_their_dentists() -> None:
    """The desk showed no dentists for Dapto or Woonona and the diary said
    "available dentist". Both official sites name them (checked 17 Sep 2026:
    daptodentists.com.au and woononadentists.com.au, home + about-us)."""
    dapto = get_branch("dapto")
    assert dapto.trading_name == "Dapto Dentists"
    assert "35 Baan Baan Street" in dapto.address
    assert "4288 0737" in dapto.phone
    assert dapto.dentists == DAPTO_DENTISTS
    assert "rear of the building" in dapto.parking
    assert "Mall Lane" not in dapto.parking
    # The Dapto site gives two different weekday closing times - not spoken.
    assert dapto.hours == VERIFY

    woonona = get_branch("woonona")
    assert "379 Princes Highway" in woonona.address
    assert "4284 4486" in woonona.phone
    assert woonona.dentists == WOONONA_DENTISTS
    assert "Haddon Lane" in woonona.parking
    assert "IGA" in woonona.parking
    assert "6:00pm" in woonona.hours and "5:00pm" in woonona.hours

    for branch in (dapto, woonona):
        principal = [c for c in branch.clinicians if "Principal" in c.role]
        assert [c.name for c in principal] == ["Dr Beena Kurian"]
        assert {c.name for c in branch.clinicians} == set(branch.dentists)
    # Reviewers' dentists are not staff.
    everyone = " ".join((*DAPTO_DENTISTS, *WOONONA_DENTISTS))
    for outsider in ("Ivan Young", "Paul Blatch", "McGovern"):
        assert outsider not in everyone


def test_unknown_branch_falls_back_to_shellharbour() -> None:
    assert get_branch("unknown").id == "shellharbour"
    assert get_branch(None).id == "shellharbour"


def test_branch_block_marks_unknown_fields() -> None:
    block = format_branch_block(get_branch("dapto"))
    assert "Dapto Dentists" in block
    assert "35 Baan Baan Street" in block
    assert VERIFY in block
    assert "do not invent" in block.lower() or "VERIFY" in block


def test_voice_instructions_are_the_verbatim_file() -> None:
    spoken = VOICE_INSTRUCTIONS
    lowered = spoken.lower()
    assert spoken.startswith("# WHO YOU ARE")
    assert "ava" in lowered
    assert "leo" not in lowered
    assert "{{BRANCH_NAME}}" in spoken
    assert "Hahaha! Yeah look, someone's gotta balance out the drill." in spoken
    assert "Morning, {{BRANCH_NAME}}, Ava speaking!" in spoken
    assert "how ya going" in spoken
    assert "what can I do for ya" in spoken
    assert INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip() == spoken


def test_ava_instructions_answer_as_mapped_branch() -> None:
    text = ava_instructions("shellharbour")
    # Every number answers as the practice; the clinic is chosen with the caller.
    assert "Morning, Illawarra Dentists, Ava speaking!" in text
    assert "front desk at Illawarra Dentists" in text
    assert "Morning, Shellharbour Dentists" not in text
    assert "you never ask which clinic" not in text.lower()
    assert "which of our clinics suits you best" in text.lower()
    assert "Captain Cook Drive" in text
    assert "35 Baan Baan Street" in text
    assert "379 Princes Highway" in text
    assert "Illawarra Dentists group number" not in text


def test_resolve_persona_explicit_env() -> None:
    assert resolve_persona(is_telephony=True, env={"AGENT_PERSONA": "ava"}) == "ava"
    assert (
        resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "generic"})
        == "generic"
    )
    assert resolve_persona(is_telephony=False, env={"AGENT_PERSONA": "AVA"}) == "ava"


def test_resolve_persona_aliases() -> None:
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


def test_clinic_facts_say_who_works_where_and_when_to_offer_another_clinic() -> None:
    """Illawarra Dentists answers; the three clinics are offered by the dentist
    the caller wants, where they live, what they prefer, and availability."""
    from persona import format_clinic_facts

    facts = format_clinic_facts("shellharbour")
    assert "WHO WORKS WHERE" in facts
    assert "Dr Beena Kurian: Dapto, Woonona" in facts
    assert "Dr Mohit Tolani: Shellharbour, Dapto" in facts
    assert "Dr Chin Valsan: Woonona" in facts
    assert "Offer another site only if" not in facts
    lower = facts.lower()
    for reason in ("dentist", "live", "prefer", "suitable soon", "ask which clinic"):
        assert reason in lower
    assert "illawarra dentists" in lower
