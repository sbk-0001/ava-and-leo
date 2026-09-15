"""Replayable transcripts always carry timestamps."""

from datetime import datetime

from call_log import CallLog, iso, supabase_turn_row


def test_turns_never_have_null_timestamps(tmp_path) -> None:
    log = CallLog(
        call_id="demo-1",
        room_name="room-a",
        branch="shellharbour",
        started_at=iso(),
    )
    log.add_turn(
        role="assistant", content="Good morning, Shellharbour Dentists, this is Ava!"
    )
    log.add_turn(role="user", content="Hi, I'd like a check-up")
    log.close()
    assert log.ended_at
    for turn in log.turns:
        assert turn.timestamp
        datetime.fromisoformat(turn.timestamp)
    path = log.save(tmp_path)
    assert path.exists()
    text = (tmp_path / "demo-1.txt").read_text()
    assert "Good morning, Shellharbour Dentists" in text
    assert "[" in text

    row = supabase_turn_row(call_id="abc", turn=log.turns[0])
    assert row["created_at"]
    assert row["created_at"] != "None"


def test_iso_always_timezone_aware() -> None:
    stamp = iso(datetime(2026, 9, 16, 1, 0))
    assert "+00:00" in stamp or stamp.endswith("Z") or "+00:00" in iso()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
