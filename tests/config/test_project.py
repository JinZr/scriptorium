from pathlib import Path

import pytest

from scriptorium.config import (
    MODEL_PLACEHOLDER,
    ConfigurationError,
    initialize_project,
    load_local_config,
    load_project_config,
)


def test_initialize_and_load_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "main.tex").write_text("paper", encoding="utf-8")

    initialize_project(tmp_path, "main.tex", "pdflatex")

    project = load_project_config(tmp_path)
    local = load_local_config(tmp_path)
    assert project.manuscript.main == "main.tex"
    assert project.profiles["full"][-1] == "figure_review"
    assert local.max_concurrency == 2
    assert local.routes["primary"].model == MODEL_PLACEHOLDER
    assert local.routes["primary"].runtime == "codex"
    assert local.routes["primary"].runtime_version is None
    assert "visual_transcription" not in local.roles
    assert "visual" not in local.routes
    assert ".scriptorium/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_project_profiles_only_accept_review_roles(tmp_path: Path) -> None:
    (tmp_path / "scriptorium.toml").write_text(
        (
            "[manuscript]\n"
            'main = "main.tex"\n'
            'engine = "pdflatex"\n\n'
            "[profiles.invalid]\n"
            'roles = ["workflow"]\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unsupported roles"):
        load_project_config(tmp_path)
