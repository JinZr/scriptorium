from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


def test_native_runtime_extras_are_exact_and_codex_remains_in_base() -> None:
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "openai-codex==0.144.4" in project["dependencies"]
    assert all(
        not dependency.startswith(("claude-agent-sdk", "google-antigravity")) for dependency in project["dependencies"]
    )

    optional = project["optional-dependencies"]
    assert optional["claude"] == ["claude-agent-sdk==0.2.128"]
    assert optional["antigravity"] == ["google-antigravity==0.1.8"]
    assert optional["all"] == [
        "claude-agent-sdk==0.2.128",
        "google-antigravity==0.1.8",
    ]
