"""LiveKit Cloud + Vercel partner-share deploy artifacts stay in sync."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_starts_src_agent_py() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "src/agent.py" in dockerfile
    assert '"start"' in dockerfile
    assert "CMD" in dockerfile
    assert "uv" in dockerfile
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env.*" in dockerignore


def test_agent_dispatch_name_is_ava_and_leo() -> None:
    source = (ROOT / "src" / "agent.py").read_text(encoding="utf-8")
    assert 'agent_name="ava-and-leo"' in source
    portal = (ROOT / "src" / "portal.py").read_text(encoding="utf-8")
    assert 'AGENT_NAME = "ava-and-leo"' in portal


def test_vercel_fastapi_entrypoint_is_portal() -> None:
    vercel = (ROOT / "vercel.json").read_text(encoding="utf-8")
    assert "src/main.py" in vercel
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "src.main:app" in pyproject or "src/main.py" in pyproject
    requirements = (ROOT / "requirements-portal.txt").read_text(encoding="utf-8")
    assert "fastapi" in requirements.lower()
    assert "livekit-api" in requirements
    assert "livekit-agents" not in requirements
    main = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    assert "from portal import app" in main


def test_livekit_cloud_example_targets_shellharbour_project() -> None:
    example = (ROOT / "livekit.toml.example").read_text(encoding="utf-8")
    assert "shellharbour-cqvf1jsj" in example
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Share with a partner" in readme
    assert "lk agent create" in readme
    assert "lk agent deploy" in readme
    assert "wss://shellharbour-cqvf1jsj.livekit.cloud" in readme
    assert "PORTAL_PASSWORD" in readme
    assert "double worker" in readme.lower() or "double workers" in readme.lower()
    assert "vercel" in readme.lower()
