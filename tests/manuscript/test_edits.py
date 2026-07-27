from hashlib import sha256
from pathlib import Path

import pytest

from scriptorium.errors import StateError
from scriptorium.manuscript import ManuscriptManager
from scriptorium.schemas import ExactEdit

from ._support import _manuscript_repo


def test_exact_edit_and_stale_worktree_detection(tmp_path: Path) -> None:
    repo = _manuscript_repo(tmp_path)
    manager = ManuscriptManager(repo)
    revision = manager.resolve_revision("HEAD")
    snapshot = tmp_path / "snapshot"
    patched = tmp_path / "patched"
    manager.create_snapshot(revision, snapshot)
    source = snapshot / "sections" / "results.tex"
    edit = ExactEdit(
        finding_ids=["f1"],
        path="sections/results.tex",
        source_digest=sha256(source.read_bytes()).hexdigest(),
        start_line=1,
        end_line=1,
        before="The value is 1.",
        after="The value is 2.",
        rationale="Correct the reported value.",
    )

    diff, paths = manager.apply_edits(snapshot, patched, [edit])

    assert "The value is 2." in diff
    assert paths == ("sections/results.tex",)
    (repo / "sections" / "results.tex").write_text("Local change.\n", encoding="utf-8")
    with pytest.raises(StateError, match="stale"):
        manager.apply_to_worktree(snapshot, patched, paths)


def test_exact_edits_reject_mismatched_and_overlapping_replacements(tmp_path: Path) -> None:
    repo = _manuscript_repo(tmp_path)
    manager = ManuscriptManager(repo)
    snapshot = tmp_path / "snapshot"
    manager.create_snapshot(manager.resolve_revision("HEAD"), snapshot)
    source = snapshot / "sections" / "results.tex"
    common = {
        "path": "sections/results.tex",
        "source_digest": sha256(source.read_bytes()).hexdigest(),
        "start_line": 1,
        "end_line": 1,
        "rationale": "Correct the reported value.",
    }

    with pytest.raises(StateError, match="Exact replacement mismatch"):
        manager.apply_edits(
            snapshot,
            tmp_path / "mismatch",
            [ExactEdit(**common, finding_ids=["f1"], before="A different sentence.", after="Replacement.")],
        )

    with pytest.raises(StateError, match="Overlapping edits"):
        manager.apply_edits(
            snapshot,
            tmp_path / "overlap",
            [
                ExactEdit(**common, finding_ids=["f1"], before="The value is 1.", after="The value is 2."),
                ExactEdit(**common, finding_ids=["f2"], before="The value is 1.", after="The value is 3."),
            ],
        )
