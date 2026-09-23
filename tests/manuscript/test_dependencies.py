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


@pytest.mark.parametrize("command", [r"\input kept.tex", r"\input kept", "\\input\nkept.tex", r"\input kept.tex\relax"])
def test_unbraced_input(tmp_path: Path, command: str) -> None:
    (tmp_path / "main.tex").write_text(command)
    (tmp_path / "kept.tex").write_text("Kept.")
    sources = ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")
    assert {source.path for source in sources} == {"main.tex", "kept.tex"}


def test_input_command_boundary(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text(r"\inputting ignored.tex")
    assert len(ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")) == 1


def test_root_dependency_wins_over_including_directory(tmp_path: Path) -> None:
    (tmp_path / "sections").mkdir()
    (tmp_path / "main.tex").write_text(r"\input{sections/body}")
    (tmp_path / "sections/body.tex").write_text(r"\input{methods}")
    (tmp_path / "methods.tex").write_text("Actual methods.")
    (tmp_path / "sections/methods.tex").write_text("Wrong methods.")
    sources = ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")
    assert {source.path for source in sources} == {"main.tex", "sections/body.tex", "methods.tex"}


def test_graphicspath_follows_include_and_declaration_order(tmp_path: Path) -> None:
    for directory in ("first", "second"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "figure.pdf").write_bytes(b"graphic")
    (tmp_path / "preamble.tex").write_text(r"\graphicspath{{missing/}{first/}}")
    (tmp_path / "body.tex").write_text(r"\includegraphics* [width=1cm]{figure}")
    (tmp_path / "main.tex").write_text(
        "\\input{preamble}\n"
        "% \\graphicspath{{ignored/}}\n"
        "\\input{body}\n"
        "\\graphicspath{{second/}}\n"
        "\\includegraphics{figure.pdf}\n"
    )
    sources = ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")
    assert {source.path for source in sources} == {
        "main.tex",
        "preamble.tex",
        "body.tex",
        "first/figure.pdf",
        "second/figure.pdf",
    }


def test_graphicspath_empty_declaration_clears_paths(tmp_path: Path) -> None:
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures/figure.pdf").write_bytes(b"graphic")
    (tmp_path / "main.tex").write_text(r"\graphicspath{{figures/}}\graphicspath{}\includegraphics{figure}")
    with pytest.raises(InfrastructureError, match="Referenced manuscript file is missing"):
        ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")


@pytest.mark.parametrize("command", [r"\input ../outside.tex", r"\graphicspath{{../outside/}}"])
def test_new_dependency_syntax_rejects_traversal(tmp_path: Path, command: str) -> None:
    (tmp_path / "main.tex").write_text(command)
    with pytest.raises(InfrastructureError, match="leaves the snapshot"):
        ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")


@pytest.mark.parametrize(
    "command", [r"\input .codex/instructions", r"\includegraphics*{AGENTS.md}", r"\graphicspath{{.codex/}}"]
)
def test_new_dependency_syntax_rejects_forbidden_paths(tmp_path: Path, command: str) -> None:
    (tmp_path / "main.tex").write_text(command)
    with pytest.raises(InfrastructureError, match="not allowed in an agent bundle"):
        ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")


@pytest.mark.parametrize("declaration", [r"\graphicspath{\paths}", r"\graphicspath{{\figdir/}}"])
def test_macro_graphicspath_is_explicitly_unsupported(tmp_path: Path, declaration: str) -> None:
    (tmp_path / "main.tex").write_text(declaration)
    with pytest.raises(InfrastructureError, match="Only literal"):
        ManuscriptManager(tmp_path).scan_sources(tmp_path, "main.tex")
