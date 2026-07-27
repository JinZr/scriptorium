from pathlib import Path

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
