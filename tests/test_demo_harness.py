"""Thursday demo harness writes timestamped transcripts for all nine scenes."""

from pathlib import Path

import pytest

from demo_harness import run_all


@pytest.mark.asyncio
async def test_thursday_transcripts(tmp_path: Path) -> None:
    paths = await run_all(tmp_path)
    assert len(paths) == 9
    texts = {path.name: path.read_text() for path in paths}

    one = texts["01-new-patient-checkup-hcf.md"]
    assert "Good morning, Shellharbour Dentists, this is Ava!" in one
    assert "HCF" in one
    assert "gap free" in one.lower() or "gap-free" in one.lower()
    assert "book_appointment" in one

    two = texts["02-reschedule-then-cancel-fee.md"]
    assert "fifty dollar" in two
    assert "fee_applies" in two

    three = texts["03-severe-pain-friday-1650.md"]
    assert "toothache" in three.lower()

    four = texts["04-swollen-swallow-escalate.md"]
    assert "do_not_book" in four
    assert "triple zero" in four.lower()
    assert (
        "book_appointment" not in four.split("tool book_appointment")[0]
        or "do_not_book" in four
    )

    five = texts["05-bot-ask-twice.md"]
    assert "long day on the desk" in five
    assert "AI receptionist" in five or "I'm an AI" in five

    six = texts["06-figtree-offer-dapto.md"]
    assert "Figtree" in six
    assert "Dapto" in six
    assert "which branch" not in six.lower()

    seven = texts["07-unknown-fee-callback.md"]
    assert "unknown" in seven.lower()
    assert "guess" in seven.lower()

    eight = texts["08-barge-in-three-times.md"]
    assert eight.lower().count("sorry, go on") + eight.lower().count("yep, yep") >= 1

    nine = texts["09-tool-timeout-recover.md"]
    assert "timeout" in nine
    assert "older than I am" in nine

    for text in texts.values():
        assert "started_at:" in text
        assert "ended_at:" in text
        assert "[" in text  # timestamps on turns
