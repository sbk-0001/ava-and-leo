"""run_local defaults to the `dev` worker subcommand.

`cli.run_app()` needs a LiveKit Agents subcommand. Without one it prints the
CLI help and exits, which also tears down the daemon portal thread — so the
documented `uv run python src/run_local.py` started nothing at all.
"""

from run_local import _ensure_worker_subcommand


def test_bare_invocation_gets_dev_appended() -> None:
    """The form the module docstring and README document must actually work."""
    assert _ensure_worker_subcommand(["src/run_local.py"]) == [
        "src/run_local.py",
        "dev",
    ]


def test_explicit_subcommand_is_left_alone() -> None:
    for sub in ("dev", "start", "console", "connect", "download-files"):
        argv = ["src/run_local.py", sub]
        assert _ensure_worker_subcommand(argv) == argv


def test_subcommand_after_flags_is_still_detected() -> None:
    argv = ["src/run_local.py", "--log-level", "DEBUG", "start"]
    assert _ensure_worker_subcommand(argv) == argv


def test_help_and_completion_are_not_hijacked() -> None:
    """Appending `dev` to --help would run a worker instead of printing help."""
    for flag in ("--help", "-h", "--install-completion", "--show-completion"):
        argv = ["src/run_local.py", flag]
        assert _ensure_worker_subcommand(argv) == argv


def test_does_not_mutate_the_caller_list() -> None:
    argv = ["src/run_local.py"]
    _ensure_worker_subcommand(argv)
    assert argv == ["src/run_local.py"]
