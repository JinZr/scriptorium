from pathlib import Path

from scriptorium.manuscript import ManuscriptManager

from ._support import _manuscript_repo


def test_snapshot_uses_committed_revision_and_scans_dependencies(tmp_path: Path) -> None:
    repo = _manuscript_repo(tmp_path)
    manager = ManuscriptManager(repo)
    revision = manager.resolve_revision("HEAD")
    (repo / "sections" / "results.tex").write_text("Uncommitted.\n", encoding="utf-8")

    snapshot = tmp_path / "snapshot"
    manager.create_snapshot(revision, snapshot)
    sources = manager.scan_sources(snapshot, "main.tex")

    assert (snapshot / "sections" / "results.tex").read_text(encoding="utf-8") == "The value is 1.\n"
    assert {source.path for source in sources} == {
        "figures/result.png",
        "main.tex",
        "refs.bib",
        "sections/results.tex",
    }
