"""run_local.py must start the LiveKit worker in dev without a manual subcommand."""

import sys

from run_local import ensure_worker_argv, main


def test_ensure_worker_argv_injects_dev_when_missing() -> None:
    assert ensure_worker_argv(["src/run_local.py"]) == ["src/run_local.py", "dev"]
    assert ensure_worker_argv(["/workspace/src/run_local.py"])[-1] == "dev"


def test_ensure_worker_argv_keeps_explicit_subcommand() -> None:
    assert ensure_worker_argv(["src/run_local.py", "dev"]) == [
        "src/run_local.py",
        "dev",
    ]
    assert ensure_worker_argv(["src/run_local.py", "console"]) == [
        "src/run_local.py",
        "console",
    ]
    assert ensure_worker_argv(["src/run_local.py", "start"]) == [
        "src/run_local.py",
        "start",
    ]


def test_ensure_worker_argv_appends_dev_after_flags() -> None:
    assert ensure_worker_argv(["src/run_local.py", "--reload"]) == [
        "src/run_local.py",
        "--reload",
        "dev",
    ]


def test_main_starts_worker_in_dev_without_subcommand(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["src/run_local.py"])
    monkeypatch.setattr("run_local._start_portal", lambda: "http://127.0.0.1:8787")
    captured: dict[str, list[str]] = {}

    def fake_run_app(_server: object) -> None:
        captured["argv"] = list(sys.argv)

    monkeypatch.setattr("livekit.agents.cli.run_app", fake_run_app)
    main()
    assert captured["argv"][0] == "src/run_local.py"
    assert captured["argv"][-1] == "dev"
    assert "console" not in captured["argv"]
