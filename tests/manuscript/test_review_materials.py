from pathlib import Path
import shutil

import fitz
import pytest

from scriptorium.config import ManuscriptConfig
from scriptorium.errors import InfrastructureError
from scriptorium.manuscript import BuildResult, FrozenRevision, ManuscriptManager
from scriptorium.schemas import DEFAULT_EVIDENCE_ANCHOR_CONTRACT
from scriptorium.workflow import Armarius


def make_sources(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for name, body in {
        "main.tex": "Main result.\\input{shared}\n",
        "supplement.tex": "\\section{Counterexample}\nSupplement.\\input{shared}\\input{counterexample}\n",
        "shared.tex": "Shared definition.\n",
        "counterexample.tex": "The result applies only to adults.\n",
        "unrelated.tex": "Unrelated private text.\n",
    }.items():
        (snapshot / name).write_text(body)
    return snapshot


class DocumentBuilder(ManuscriptManager):
    def build(self, workspace, manuscript):
        assert not (workspace / "compiler-marker.tmp").exists()
        (workspace / "compiler-marker.tmp").touch()
        pdf = workspace / Path(manuscript.main).with_suffix(".pdf")
        with fitz.open() as document:
            for number in range(2 if manuscript.main == "main.tex" else 1):
                document.new_page().insert_text((40, 40), f"{manuscript.main} page {number + 1}")
            document.save(pdf)
        return BuildResult(pdf, f"compiled {manuscript.main}")


def test_declared_supplement_closure_is_searchable_without_collecting_unrelated_files(tmp_path):
    snapshot = make_sources(tmp_path)
    manager = DocumentBuilder(tmp_path)
    primary = manager.scan_project_sources(snapshot, ManuscriptConfig("main.tex", "pdflatex"))
    config = ManuscriptConfig("main.tex", "pdflatex", ("supplement.tex",))
    sources = manager.scan_project_sources(snapshot, config)
    assert {item.path for item in primary} == {"main.tex", "shared.tex"}
    assert [item.path for item in sources] == ["counterexample.tex", "main.tex", "shared.tex", "supplement.tex"]
    build = manager.build_project(snapshot, tmp_path / "build", config)
    assert not (snapshot / "compiler-marker.tmp").exists()
    assert not (snapshot / "main.pdf").exists()
    assert "compiled main.tex" in build.log and "compiled supplement.tex" in build.log
    bundle = manager.create_bundle(
        snapshot,
        tmp_path / "bundle",
        FrozenRevision("commit", "tree"),
        sources,
        build.pdf_path,
        DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
        manager.create_navigation(snapshot, sources),
        build.documents,
    )
    assert bundle.pdf_pages == 3
    assert [item.model_dump() for item in bundle.anchor_map.compiled_pdf.documents] == [
        {"entrypoint": "main.tex", "start_page": 1, "page_count": 2},
        {"entrypoint": "supplement.tex", "start_page": 3, "page_count": 1},
    ]
    with fitz.open(bundle.workspace / "manuscript.pdf") as document:
        assert "main.tex page 2" in document[1].get_text()
        assert "supplement.tex page 1" in document[2].get_text()
    assert "Counterexample" in (bundle.workspace / "navigation.json").read_text()
    assert Armarius._load_bundle(bundle.workspace, DEFAULT_EVIDENCE_ANCHOR_CONTRACT).anchor_map == bundle.anchor_map


@pytest.mark.parametrize("entrypoint", ["missing.tex", ".codex/instructions.tex", "outside.tex"])
def test_unsafe_or_missing_supplement_is_not_omitted(tmp_path, entrypoint):
    snapshot = make_sources(tmp_path)
    (snapshot / ".codex").mkdir()
    (snapshot / ".codex/instructions.tex").write_text("Instructions.")
    (tmp_path / "outside.tex").write_text("Outside.")
    (snapshot / "outside.tex").symlink_to(tmp_path / "outside.tex")
    with pytest.raises(InfrastructureError):
        ManuscriptManager(tmp_path).scan_project_sources(
            snapshot, ManuscriptConfig("main.tex", "pdflatex", (entrypoint,))
        )


def test_document_index_uses_the_frozen_source_identity_for_an_internal_link(tmp_path):
    snapshot = make_sources(tmp_path)
    (snapshot / "alias.tex").symlink_to("main.tex")
    manager = DocumentBuilder(tmp_path)
    config = ManuscriptConfig("alias.tex", "pdflatex")
    sources = manager.scan_project_sources(snapshot, config)
    build = manager.build_project(snapshot, tmp_path / "build", config)
    assert build.documents[0].entrypoint == "main.tex"
    bundle = manager.create_bundle(
        snapshot,
        tmp_path / "bundle",
        FrozenRevision("commit", "tree"),
        sources,
        build.pdf_path,
        DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
        documents=build.documents,
    )
    assert bundle.anchor_map.compiled_pdf.documents[0].entrypoint == "main.tex"


def test_supplement_cannot_repeat_the_main_document_through_a_link(tmp_path):
    snapshot = make_sources(tmp_path)
    (snapshot / "alias.tex").symlink_to("main.tex")
    with pytest.raises(InfrastructureError, match="distinct"):
        ManuscriptManager(tmp_path).scan_project_sources(
            snapshot, ManuscriptConfig("main.tex", "pdflatex", ("alias.tex",))
        )


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("latexmk", "pdflatex", "kpsewhich")), reason="LaTeX tools required"
)
def test_real_independent_documents_keep_separate_page_numbering(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for name, body in {
        "main.tex": "Main result.",
        "supplement.tex": "Supplement first page.\\newpage Supplement counterexample.",
    }.items():
        (snapshot / name).write_text("\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n")
    config = ManuscriptConfig("main.tex", "pdflatex", ("supplement.tex",))
    manager = ManuscriptManager(tmp_path)
    sources = manager.scan_project_sources(snapshot, config)
    build = manager.build_project(snapshot, tmp_path / "build", config)
    manager.validate_build_sources(build, sources)
    assert [(item.entrypoint, item.start_page, item.page_count) for item in build.documents] == [
        ("main.tex", 1, 1),
        ("supplement.tex", 2, 2),
    ]
    assert {item.path for item in build.compiler_inputs if item.kind == "review"} == {"main.tex", "supplement.tex"}
    with fitz.open(build.pdf_path) as document:
        assert len(document) == 3
        assert "Supplement first page" in document[1].get_text()
        assert document[1].get_text().strip().endswith("1")
        assert "Supplement counterexample" in document[2].get_text()
    assert sorted(path.name for path in snapshot.iterdir()) == ["main.tex", "supplement.tex"]
