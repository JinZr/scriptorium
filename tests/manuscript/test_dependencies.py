from pathlib import Path

import pytest

from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import ManuscriptManager


def test_dependency_scan_ignores_commented_pseudo_dependencies(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "main.tex").write_text(
        "\\input{kept}\n"
        "% \\input{missing-line}\n"
        "Line break\\\\% \\input{missing-inline}\n"
        "Literal \\% and \\input{also-kept}\n",
        encoding="utf-8",
    )
    (snapshot / "kept.tex").write_text("Kept.\n", encoding="utf-8")
    (snapshot / "also-kept.tex").write_text("Also kept.\n", encoding="utf-8")

    sources = ManuscriptManager(tmp_path).scan_sources(snapshot, "main.tex")

    assert {source.path for source in sources} == {"also-kept.tex", "kept.tex", "main.tex"}


def test_nested_tex_resolves_project_root_relative_dependency(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    (snapshot / "sections").mkdir(parents=True)
    (snapshot / "shared").mkdir()
    (snapshot / "main.tex").write_text("\\input{sections/results}\n", encoding="utf-8")
    (snapshot / "sections" / "results.tex").write_text("\\input{shared/methods}\n", encoding="utf-8")
    (snapshot / "shared" / "methods.tex").write_text("Methods.\n", encoding="utf-8")

    sources = ManuscriptManager(tmp_path).scan_sources(snapshot, "main.tex")

    assert {source.path for source in sources} == {
        "main.tex",
        "sections/results.tex",
        "shared/methods.tex",
    }


@pytest.mark.parametrize(
    "bibliography_command",
    ["\\bibliography{refs}", "\\addbibresource{refs.bib}"],
)
def test_missing_bibliography_uses_main_bbl(tmp_path: Path, bibliography_command: str) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "main.tex").write_text(f"{bibliography_command}\n", encoding="utf-8")
    (snapshot / "main.bbl").write_text("Compiled bibliography.\n", encoding="utf-8")

    sources = ManuscriptManager(tmp_path).scan_sources(snapshot, "main.tex")

    assert {source.path for source in sources} == {"main.bbl", "main.tex"}


def test_bibliography_source_takes_priority_over_main_bbl(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "main.tex").write_text("\\bibliography{refs}\n", encoding="utf-8")
    (snapshot / "refs.bib").write_text("@article{x, title={X}}\n", encoding="utf-8")
    (snapshot / "main.bbl").write_text("Compiled bibliography.\n", encoding="utf-8")

    sources = ManuscriptManager(tmp_path).scan_sources(snapshot, "main.tex")

    assert {source.path for source in sources} == {"main.tex", "refs.bib"}


def test_unrelated_bbl_does_not_replace_missing_bibliography(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "main.tex").write_text("\\bibliography{refs}\n", encoding="utf-8")
    (snapshot / "stray.bbl").write_text("Unrelated bibliography.\n", encoding="utf-8")

    with pytest.raises(InfrastructureError, match="Referenced manuscript file is missing: refs.bib"):
        ManuscriptManager(tmp_path).scan_sources(snapshot, "main.tex")


def test_bbl_does_not_mask_bibliography_outside_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "main.tex").write_text("\\bibliography{../refs}\n", encoding="utf-8")
    (snapshot / "main.bbl").write_text("Compiled bibliography.\n", encoding="utf-8")

    with pytest.raises(InfrastructureError, match="leaves the snapshot"):
        ManuscriptManager(tmp_path).scan_sources(snapshot, "main.tex")


def test_nested_bibliography_uses_main_document_bbl(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    (snapshot / "paper" / "sections").mkdir(parents=True)
    (snapshot / "paper" / "main.tex").write_text("\\input{sections/body}\n", encoding="utf-8")
    (snapshot / "paper" / "sections" / "body.tex").write_text(
        "\\bibliography{refs}\n",
        encoding="utf-8",
    )
    (snapshot / "paper" / "main.bbl").write_text("Compiled bibliography.\n", encoding="utf-8")

    sources = ManuscriptManager(tmp_path).scan_sources(snapshot, "paper/main.tex")

    assert {source.path for source in sources} == {
        "paper/main.bbl",
        "paper/main.tex",
        "paper/sections/body.tex",
    }
