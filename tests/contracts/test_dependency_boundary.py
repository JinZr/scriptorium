from pathlib import Path


def test_model_sdk_dependency_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    imports = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "src" / "scriptorium").rglob("*.py")
    }
    allowed_modules = {
        "openai_codex": "src/scriptorium/runtime/codex.py",
        "claude_agent_sdk": "src/scriptorium/runtime/claude_code.py",
        "google.antigravity": "src/scriptorium/runtime/antigravity.py",
    }
    for sdk_name, allowed_module in allowed_modules.items():
        assert sdk_name in imports[allowed_module]

    for relative, source in imports.items():
        for sdk_name, allowed_module in allowed_modules.items():
            if relative != allowed_module:
                assert sdk_name not in source

        assert "from openai " not in source
        assert "import openai" not in source
        assert "import anthropic" not in source
        assert "google.genai" not in source
