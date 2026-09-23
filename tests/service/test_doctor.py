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
    result = subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def make_repository(tmp_path: Path) -> Path:
    repo = tmp_path / "paper"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Scriptorium Tests")
    git(repo, "config", "user.email", "scriptorium@example.invalid")
    (repo / "main.tex").write_text("paper", encoding="utf-8")
    (repo / "scriptorium.toml").write_text(
        '[manuscript]\nmain = "main.tex"\nengine = "pdflatex"\n\n' '[profiles.quick]\nroles = ["copyedit"]\n',
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


def test_doctor_uses_the_requested_frozen_revision_and_ignores_dirty_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path)
    requested_commit = git(repo, "rev-parse", "HEAD")
    (repo / "main.tex").write_text("new committed paper", encoding="utf-8")
    git(repo, "add", "main.tex")
    git(repo, "commit", "--quiet", "-m", "new manuscript")
    (repo / "main.tex").write_text(r"\input{missing}", encoding="utf-8")
    (repo / "scriptorium.toml").write_text("not valid toml =", encoding="utf-8")
    prepare_doctor(monkeypatch)
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
    repo = make_repository(tmp_path)
    (repo / "main.tex").write_text(r"\input{missing}", encoding="utf-8")
    git(repo, "add", "main.tex")
    git(repo, "commit", "--quiet", "-m", "missing dependency")
    prepare_doctor(monkeypatch)
    manager = DoctorManuscriptManager(repo)

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    sources = next(item for item in result["checks"] if item["name"] == "manuscript_sources")
    compile_check = next(item for item in result["checks"] if item["name"] == "manuscript_compile")
    assert sources["message"] == "Referenced manuscript file is missing: missing.tex"
    assert compile_check["message"] == "not run because manuscript_sources failed"
    assert manager.build_workspaces == []


def test_doctor_reports_compile_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repository(tmp_path)
    prepare_doctor(monkeypatch)
    manager = DoctorManuscriptManager(repo, InfrastructureError("LaTeX build failed:\ntest log"))

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        result = service.doctor(profile="quick")

    assert result["exit_code"] == 3
    compile_check = next(item for item in result["checks"] if item["name"] == "manuscript_compile")
    assert compile_check["message"] == "LaTeX build failed:\ntest log"
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
    repo = make_repository(tmp_path)
    system_which = shutil.which
    monkeypatch.setattr(
        "scriptorium.service.shutil.which",
        lambda command: (
            system_which("git") if command == "git" else None if command == missing_tool else f"/usr/bin/{command}"
        ),
    )
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
    repo = make_repository(tmp_path)
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
    repo = make_repository(tmp_path)
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
    repo = make_repository(tmp_path)
    prepare_doctor(monkeypatch)
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
    repo = make_repository(tmp_path)
    prepare_doctor(monkeypatch)
    manager = DoctorManuscriptManager(repo, KeyboardInterrupt())

    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        with pytest.raises(KeyboardInterrupt):
            service.doctor(profile="quick")

    assert len(manager.build_workspaces) == 1
    assert not manager.build_workspaces[0].exists()


def test_doctor_reports_compiler_source_omission(tmp_path, monkeypatch):
    from dataclasses import replace

    from scriptorium.manuscript import CompilerInput

    class UncoveredBuildManager(DoctorManuscriptManager):
        def build(self, workspace, manuscript):
            return replace(
                super().build(workspace, manuscript),
                compiler_inputs=(CompilerInput("hidden.tex", "missing", "review"),),
            )

    repo = make_repository(tmp_path)
    prepare_doctor(monkeypatch)
    with ScriptoriumService(repo, manuscript_manager=UncoveredBuildManager(repo)) as service:
        result = service.doctor(profile="quick")
        assert service.database.list_runs() == []
    assert result["exit_code"] == 3
    check = next(item for item in result["checks"] if item["name"] == "manuscript_compile")
    assert not check["ok"]
    assert "hidden.tex" in check["message"]
