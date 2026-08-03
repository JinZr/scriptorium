from pathlib import Path
import subprocess

import pytest

from scriptorium.errors import InfrastructureError
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


def test_revision_uses_verified_single_object_ids(tmp_path: Path) -> None:
    repo = _manuscript_repo(tmp_path)
    revision = ManuscriptManager(repo).resolve_revision("HEAD")

    expected_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected_tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{expected_commit}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert revision.commit_sha == expected_commit
    assert revision.tree_sha == expected_tree


def test_revision_verifies_both_git_queries(tmp_path: Path, monkeypatch) -> None:
    manager = ManuscriptManager(tmp_path)
    commit = "a" * 40
    tree = "b" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str:
        calls.append(args)
        return f"{commit}\n" if len(calls) == 1 else f"{tree}\n"

    monkeypatch.setattr(manager, "_git", fake_git)

    assert manager.resolve_revision("HEAD").tree_sha == tree
    assert calls == [
        ("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
        ("rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}"),
    ]


@pytest.mark.parametrize("output", ["", "--end-of-options\n" + "a" * 40 + "\n", "A" * 40 + "\n"])
def test_revision_rejects_invalid_git_object_output(output: str) -> None:
    with pytest.raises(InfrastructureError, match="invalid object ID"):
        ManuscriptManager._git_object_id(output)
