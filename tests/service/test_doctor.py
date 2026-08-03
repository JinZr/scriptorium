from pathlib import Path
import shutil
import subprocess

import pytest

from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import BuildResult, ManuscriptManager
from scriptorium.service import ScriptoriumService


class DoctorManuscriptManager(ManuscriptManager):
    def __init__(self, repo: Path, build_error: Exception | None = None) -> None:
        super().__init__(repo)
        self.build_error = build_error
        self.snapshot_paths: list[Path] = []
        self.build_workspaces: list[Path] = []
        self.built_main_texts: list[str] = []

    def create_snapshot(self, revision, destination):
        super().create_snapshot(revision, destination)
        self.snapshot_paths.append(destination)

    def build(self, workspace, manuscript):
        self.build_workspaces.append(workspace)
        self.built_main_texts.append((workspace / manuscript.main).read_text(encoding="utf-8"))
        if self.build_error is not None:
            raise self.build_error
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.write_bytes(b"%PDF-1.4\n")
        return BuildResult(pdf_path=pdf_path, log="compiled")


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repository(
    tmp_path: Path,
    runtime: str,
    provider: str,
    *,
    model: str = "model",
    include_prices: bool = True,
) -> Path:
    repo = tmp_path / "paper"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Scriptorium Tests")
    git(repo, "config", "user.email", "scriptorium@example.invalid")
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
            'visual_transcription = "primary"\n'
            'revision = "primary"\n'
            'verification = "primary"\n\n'
            "[routes.primary]\n"
            f'runtime = "{runtime}"\n'
            f'model_provider = "{provider}"\n'
            f'model = "{model}"\n'
            + ("input_usd_per_million = 0\n" "output_usd_per_million = 0\n" if include_prices else "")
        ),
        encoding="utf-8",
    )
    git(repo, "add", "main.tex", "scriptorium.toml")
    git(repo, "commit", "--quiet", "-m", "initial manuscript")
    return repo


def prepare_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    system_which = shutil.which
    monkeypatch.setattr(
        "scriptorium.service.shutil.which",
        lambda command: system_which("git") if command == "git" else f"/usr/bin/{command}",
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

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor()

    assert result["ok"] is True
    assert result["profile"] == "quick"
    assert {"frozen_revision", "manuscript_sources", "manuscript_compile"} <= {
        item["name"] for item in result["checks"]
    }
    assert requested_packages == ["claude-agent-sdk"]
    assert next(item for item in result["checks"] if item["name"] == "runtime_claude_code_sdk")["ok"]


def test_doctor_ignores_unused_visual_transcription_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    local_path = repo / ".scriptorium" / "config.toml"
    local_path.write_text(
        local_path.read_text(encoding="utf-8").replace(
            'visual_transcription = "primary"',
            'visual_transcription = "visual"',
        )
        + (
            "\n[routes.visual]\n"
            'runtime = "claude_code"\n'
            'model_provider = "anthropic"\n'
            'model = "visual-model"\n'
            "input_usd_per_million = 0\n"
            "output_usd_per_million = 0\n"
        ),
        encoding="utf-8",
    )
    prepare_doctor(monkeypatch)
    requested_packages: list[str] = []

    def version(package: str) -> str:
        requested_packages.append(package)
        return {"claude-agent-sdk": "0.2.128", "openai-codex": "0.144.4"}[package]

    monkeypatch.setattr("scriptorium.service.metadata.version", version)

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor(profile="quick")

    assert result["ok"] is True
    assert requested_packages == ["openai-codex"]


def test_doctor_requires_gemini_api_key_for_antigravity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "antigravity", "gemini")
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.1.8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
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

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    check = next(item for item in result["checks"] if item["name"] == "runtime_claude_code_sdk")
    assert check == {
        "name": "runtime_claude_code_sdk",
        "ok": False,
        "message": "expected 0.2.128, found 0.2.127",
    }


def test_doctor_checks_runtime_when_model_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(
        tmp_path,
        "claude_code",
        "anthropic",
        model="USER_CONFIGURED_MODEL",
    )
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.2.128")

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 2
    assert not next(item for item in result["checks"] if item["name"] == "model_routes")["ok"]
    assert next(item for item in result["checks"] if item["name"] == "runtime_claude_code_sdk")["ok"]


def test_doctor_reports_runtime_failure_when_model_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(
        tmp_path,
        "claude_code",
        "anthropic",
        model="USER_CONFIGURED_MODEL",
    )
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.2.127")

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    assert not next(item for item in result["checks"] if item["name"] == "model_routes")["ok"]
    assert not next(item for item in result["checks"] if item["name"] == "runtime_claude_code_sdk")["ok"]


def test_doctor_checks_antigravity_auth_when_budget_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(
        tmp_path,
        "antigravity",
        "gemini",
        include_prices=False,
    )
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.1.8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor(profile="quick", budget_usd=1)

    assert result["exit_code"] == 3
    assert not next(item for item in result["checks"] if item["name"] == "model_routes")["ok"]
    assert next(item for item in result["checks"] if item["name"] == "runtime_antigravity_sdk")["ok"]
    assert not next(item for item in result["checks"] if item["name"] == "antigravity_auth")["ok"]


def test_doctor_uses_the_requested_frozen_revision_and_ignores_dirty_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    requested_commit = git(repo, "rev-parse", "HEAD")
    (repo / "main.tex").write_text("new committed paper", encoding="utf-8")
    git(repo, "add", "main.tex")
    git(repo, "commit", "--quiet", "-m", "new manuscript")
    (repo / "main.tex").write_text(r"\input{missing}", encoding="utf-8")
    (repo / "scriptorium.toml").write_text("not valid toml =", encoding="utf-8")
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.144.4")
    manager = DoctorManuscriptManager(repo)

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        result = service.doctor(profile="quick", revision=requested_commit)

    assert result["ok"] is True
    assert manager.built_main_texts == ["paper"]
    assert manager.build_workspaces[0] != manager.snapshot_paths[0]
    revision_check = next(item for item in result["checks"] if item["name"] == "frozen_revision")
    assert revision_check["message"] == f"{requested_commit} -> {requested_commit}"


def test_doctor_reports_missing_source_without_starting_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    (repo / "main.tex").write_text(r"\input{missing}", encoding="utf-8")
    git(repo, "add", "main.tex")
    git(repo, "commit", "--quiet", "-m", "missing dependency")
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.144.4")
    manager = DoctorManuscriptManager(repo)

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    sources = next(item for item in result["checks"] if item["name"] == "manuscript_sources")
    compile_check = next(item for item in result["checks"] if item["name"] == "manuscript_compile")
    assert sources["message"] == "Referenced manuscript file is missing: missing.tex"
    assert compile_check["message"] == "not run because manuscript_sources failed"
    assert manager.build_workspaces == []


def test_doctor_reports_compile_failure_and_continues_runtime_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "claude_code", "anthropic")
    prepare_doctor(monkeypatch)
    requested_packages: list[str] = []

    def version(package: str) -> str:
        requested_packages.append(package)
        return "0.2.128"

    monkeypatch.setattr("scriptorium.service.metadata.version", version)
    manager = DoctorManuscriptManager(repo, InfrastructureError("LaTeX build failed:\ntest log"))

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    compile_check = next(item for item in result["checks"] if item["name"] == "manuscript_compile")
    assert compile_check["message"] == "LaTeX build failed:\ntest log"
    assert requested_packages == ["claude-agent-sdk"]
    assert not manager.build_workspaces[0].exists()


@pytest.mark.parametrize(
    ("missing_tool", "failed_check"),
    (("latexmk", "latexmk"), ("pdflatex", "latex_engine")),
)
def test_doctor_skips_compile_when_latex_tool_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_tool: str,
    failed_check: str,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    system_which = shutil.which
    monkeypatch.setattr(
        "scriptorium.service.shutil.which",
        lambda command: (
            system_which("git") if command == "git" else None if command == missing_tool else f"/usr/bin/{command}"
        ),
    )
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.144.4")
    manager = DoctorManuscriptManager(repo)

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    assert next(item for item in result["checks"] if item["name"] == "manuscript_sources")["ok"]
    compile_check = next(item for item in result["checks"] if item["name"] == "manuscript_compile")
    assert compile_check["message"] == f"not run because {failed_check} failed"
    assert manager.build_workspaces == []


def test_doctor_reports_frozen_project_configuration_errors_without_loading_the_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    (repo / "scriptorium.toml").write_text("not valid toml =", encoding="utf-8")
    git(repo, "add", "scriptorium.toml")
    git(repo, "commit", "--quiet", "-m", "invalid project config")
    prepare_doctor(monkeypatch)

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 2
    config_check = next(item for item in result["checks"] if item["name"] == "tracked_project_config")
    assert config_check["ok"] is False
    assert "Cannot read" in config_check["message"]
    assert next(item for item in result["checks"] if item["name"] == "manuscript_compile")["message"] == (
        "not run because manuscript_sources failed"
    )


def test_doctor_reports_an_unknown_revision_as_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    prepare_doctor(monkeypatch)

    with ScriptoriumService(repo, manuscript_manager=DoctorManuscriptManager(repo)) as service:
        result = service.doctor(profile="quick", revision="does-not-exist")

    assert result["exit_code"] == 3
    revision_check = next(item for item in result["checks"] if item["name"] == "frozen_revision")
    assert revision_check["ok"] is False
    assert next(item for item in result["checks"] if item["name"] == "manuscript_sources")["message"] == (
        "not run because tracked_project_config failed"
    )


def test_doctor_rebuilds_without_persisting_workflow_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    prepare_doctor(monkeypatch)
    monkeypatch.setattr("scriptorium.service.metadata.version", lambda package: "0.144.4")
    manager = DoctorManuscriptManager(repo)

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        before_artifacts = sorted(path.relative_to(service.state_dir) for path in service.artifacts.root.rglob("*"))
        first = service.doctor(profile="quick")
        second = service.doctor(profile="quick")
        counts = {
            table: service.database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "tasks", "attempts", "events", "artifacts")
        }
        after_artifacts = sorted(path.relative_to(service.state_dir) for path in service.artifacts.root.rglob("*"))

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(manager.build_workspaces) == 2
    assert all(not workspace.exists() for workspace in manager.build_workspaces)
    assert counts == {"runs": 0, "tasks": 0, "attempts": 0, "events": 0, "artifacts": 0}
    assert after_artifacts == before_artifacts


def test_doctor_cleans_temporary_build_after_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path, "codex", "openai")
    prepare_doctor(monkeypatch)
    manager = DoctorManuscriptManager(repo, KeyboardInterrupt())

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        with pytest.raises(KeyboardInterrupt):
            service.doctor(profile="quick")

    assert len(manager.build_workspaces) == 1
    assert not manager.build_workspaces[0].exists()
