from pathlib import Path

import pytest

from scriptorium.config import ConfigurationError, initialize_project, load_project_config, reject_legacy_local_config


def test_initialize_and_load_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "main.tex").write_text("paper", encoding="utf-8")

    initialize_project(tmp_path, "main.tex", "pdflatex")

    project = load_project_config(tmp_path)
    assert project.manuscript.main == "main.tex"
    assert project.profiles["full"][-1] == "figure_review"
    assert not (tmp_path / ".scriptorium" / "config.toml").exists()
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


def test_legacy_local_route_config_requires_migration(tmp_path: Path) -> None:
    state = tmp_path / ".scriptorium"
    state.mkdir()
    (state / "config.toml").write_text('[routes.primary]\nmodel = "old-model"\n', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="removed internal model routes"):
        reject_legacy_local_config(tmp_path)
