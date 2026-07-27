from pathlib import Path


def test_model_sdk_dependency_boundary() -> None:
    root = Path(__file__).parents[1]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"anthropic' not in project
    assert '"google-genai' not in project
    assert '"openai"' not in project

    imports = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in (root / "src" / "scriptorium").glob("*.py")
    }
    for relative, source in imports.items():
        if relative == "src/scriptorium/runtime.py":
            continue
        assert "openai_codex" not in source
        assert "from openai " not in source
        assert "import openai" not in source
        assert "import anthropic" not in source
        assert "google.genai" not in source
