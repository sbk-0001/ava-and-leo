"""Verbatim session instructions — do not paraphrase the WHO YOU ARE block."""

from pathlib import Path

from persona import (
    INSTRUCTIONS_PATH,
    ava_instructions,
    load_session_instructions,
)

CALIBRATION_END = "Hahaha! Yeah look, someone's gotta balance out the drill."


def test_instructions_file_is_verbatim() -> None:
    text = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    assert text.startswith("# WHO YOU ARE\n")
    assert "You are Ava, receptionist at {{BRANCH_NAME}}." in text
    assert CALIBRATION_END in text
    assert text.rstrip().endswith(CALIBRATION_END)
    assert text.count("{{BRANCH_NAME}}") == 5
    assert "which branch they\nwant" in text or "which branch they want" in text


def test_branch_name_is_interpolated_not_rewritten() -> None:
    loaded = load_session_instructions("Shellharbour Dentists")
    assert "{{BRANCH_NAME}}" not in loaded
    assert "Good morning, Shellharbour Dentists, this is Ava!" in loaded
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
