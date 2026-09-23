from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


def test_package_has_no_internal_model_sdk_dependencies() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert all(
        not dependency.startswith(("openai-codex", "claude-agent-sdk", "google-antigravity"))
        for dependency in project["dependencies"]
    )

    optional = project["optional-dependencies"]
    assert set(optional) == {"dev"}
