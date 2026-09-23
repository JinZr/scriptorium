from pathlib import Path


def test_model_sdks_are_absent_from_core() -> None:
    root = Path(__file__).resolve().parents[2]
    imports = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "src" / "scriptorium").rglob("*.py")
    }
    assert not list((root / "src" / "scriptorium" / "runtime").glob("*.py"))
    for source in imports.values():
        assert "openai_codex" not in source
        assert "claude_agent_sdk" not in source
        assert "google.antigravity" not in source
        assert "from openai " not in source
        assert "import openai" not in source
        assert "import anthropic" not in source
        assert "google.genai" not in source
