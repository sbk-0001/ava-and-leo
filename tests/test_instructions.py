"""Verbatim session instructions — do not paraphrase the WHO YOU ARE block."""

from pathlib import Path

from persona import (
    INSTRUCTIONS_PATH,
    ava_instructions,
    load_session_instructions,
)

CALIBRATION_END = "That any good\n     to ya?"


def test_instructions_file_is_verbatim() -> None:
    text = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    assert text.startswith("# WHO YOU ARE\n")
    assert "You're Ava. You work the front desk at {{BRANCH_NAME}}." in text
    assert CALIBRATION_END in text
    assert text.rstrip().endswith(CALIBRATION_END)
    assert text.count("{{BRANCH_NAME}}") == 6
    assert "which clinic they want" in text
    assert "a sentence or two" in text.lower()
    assert "Morning, {{BRANCH_NAME}}, Ava speaking!" in text
    assert "how ya going" in text
    assert "what can I do for ya" in text


def test_branch_name_is_interpolated_not_rewritten() -> None:
    loaded = load_session_instructions("Shellharbour Dentists")
    assert "{{BRANCH_NAME}}" not in loaded
    assert "Morning, Shellharbour Dentists, Ava speaking!" in loaded
    assert "Shellharbour Dentists, this is Ava — how ya going?" in loaded
    assert "Shellharbour Dentists, Ava — what can I do for ya?" in loaded
    assert loaded.startswith("# WHO YOU ARE")
    assert CALIBRATION_END in loaded
    # Original file unchanged
    source = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    assert "{{BRANCH_NAME}}" in source


def test_ava_instructions_include_verbatim_plus_facts() -> None:
    text = ava_instructions("dapto")
    assert text.startswith("# WHO YOU ARE")
    assert "Dapto Dentists" in text
    assert "35 Baan Baan Street" in text
    assert "(02) 4288 0737" in text
    assert CALIBRATION_END in text
    assert Path(INSTRUCTIONS_PATH).is_file()
