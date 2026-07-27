from pathlib import Path
from types import SimpleNamespace

import pytest

from scriptorium.service import ScriptoriumService


def make_repository(tmp_path: Path, runtime: str, provider: str) -> Path:
    repo = tmp_path / "paper"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "main.tex").write_text("paper", encoding="utf-8")
    (repo / "scriptorium.toml").write_text(
        (
            "[manuscript]\n"
            'main = "main.tex"\n'
            'engine = "pdflatex"\n\n'
            "[profiles.quick]\n"
            'roles = ["copyedit"]\n'
        ),
        encoding="utf-8",
    )
    state_dir = repo / ".scriptorium"
    state_dir.mkdir()
    (state_dir / "config.toml").write_text(
        (
            "[roles]\n"
            'copyedit = "primary"\n'
            'revision = "primary"\n'
            'verification = "primary"\n\n'
            "[routes.primary]\n"
            f'runtime = "{runtime}"\n'
            f'model_provider = "{provider}"\n'
            'model = "model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )
    return repo


def prepare_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scriptorium.service.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        "scriptorium.service.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="true", stderr=""),
    )


def test_doctor_checks_only_selected_native_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "claude_code", "anthropic")
    prepare_doctor(monkeypatch)
    requested_packages: list[str] = []

    def version(package: str) -> str:
        requested_packages.append(package)
        return {"claude-agent-sdk": "0.2.128"}[package]

    monkeypatch.setattr("scriptorium.service.metadata.version", version)

    with ScriptoriumService(repo) as service:
        result = service.doctor(profile="quick")

    assert result["ok"] is True
    assert requested_packages == ["claude-agent-sdk"]
    assert next(item for item in result["checks"] if item["name"] == "runtime_claude_code_sdk")["ok"]


def test_doctor_requires_gemini_api_key_for_antigravity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "antigravity", "gemini")
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.1.8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with ScriptoriumService(repo) as service:
        missing = service.doctor(profile="quick")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        configured = service.doctor(profile="quick")

    assert missing["exit_code"] == 3
    assert not next(item for item in missing["checks"] if item["name"] == "antigravity_auth")["ok"]
    assert configured["ok"] is True


def test_doctor_reports_wrong_runtime_sdk_version_as_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "claude_code", "anthropic")
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.2.127")

    with ScriptoriumService(repo) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    check = next(item for item in result["checks"] if item["name"] == "runtime_claude_code_sdk")
    assert check == {
        "name": "runtime_claude_code_sdk",
        "ok": False,
        "message": "expected 0.2.128, found 0.2.127",
    }
