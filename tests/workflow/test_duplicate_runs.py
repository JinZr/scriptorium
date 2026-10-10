import asyncio
import subprocess
import threading

import pytest

from scriptorium.domain import RunStatus
from scriptorium.errors import DuplicateRunError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, make_repository


def _service(repo):
    return ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo))


def test_second_start_on_the_same_commit_is_refused_and_names_the_active_run(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        first = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        with pytest.raises(DuplicateRunError) as caught:
            asyncio.run(service.start_run("HEAD", "quick"))
        assert caught.value.code == "duplicate_run"
        assert first.id in str(caught.value)
        assert f"run status {first.id}" in str(caught.value)
        assert "--allow-duplicate" in str(caught.value)
        assert [run.id for run in service.database.list_runs()] == [first.id]


def test_allow_duplicate_bypasses_the_guard(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        first = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        second = asyncio.run(service.start_run("HEAD", "quick", allow_duplicate=True))["run"]
        assert second.id != first.id
        assert second.commit_sha == first.commit_sha


def test_start_is_permitted_once_the_earlier_run_is_terminal(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        first = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        service.cancel_run(first.id, "Superseded")
        assert service.database.get_run(first.id).status == RunStatus.CANCELLED
        second = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        assert second.id != first.id


def test_a_run_on_a_different_commit_does_not_block_start(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        asyncio.run(service.start_run("HEAD", "quick"))
        manuscript = repo / "main.tex"
        manuscript.write_text(manuscript.read_text(encoding="utf-8") + "% later\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "commit", "-qam", "Later revision"], check=True)
        later = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        assert later.status == RunStatus.REVIEWING


class _BlockingSnapshotManager(PdfBuildingManuscriptManager):
    def __init__(self, repo, entered, release):
        super().__init__(repo)
        self.entered = entered
        self.release = release

    def create_snapshot(self, revision, destination):
        self.entered.set()
        assert self.release.wait(timeout=30)
        super().create_snapshot(revision, destination)


def test_overlapping_starts_on_one_commit_cannot_both_create_a_run(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    entered, release = threading.Event(), threading.Event()
    outcomes = {}

    def first():
        with ScriptoriumService(repo, manuscript_manager=_BlockingSnapshotManager(repo, entered, release)) as service:
            outcomes["first"] = asyncio.run(service.start_run("HEAD", "quick"))["run"]

    def second():
        with _service(repo) as service:
            try:
                asyncio.run(service.start_run("HEAD", "quick"))
            except DuplicateRunError as exc:
                outcomes["second"] = exc

    starter = threading.Thread(target=first)
    starter.start()
    assert entered.wait(timeout=30)
    contender = threading.Thread(target=second)
    contender.start()
    contender.join(timeout=1)
    assert contender.is_alive(), "the second start must wait for the first to finish freezing"
    release.set()
    starter.join(timeout=60)
    contender.join(timeout=60)
    assert isinstance(outcomes["second"], DuplicateRunError)
    assert outcomes["first"].id in str(outcomes["second"])
    with _service(repo) as service:
        assert [run.id for run in service.database.list_runs()] == [outcomes["first"].id]


def test_a_resumable_failed_run_still_blocks_a_replacement(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        first = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        service.database.update_run(first.id, RunStatus.FAILED, "host crashed")
        with pytest.raises(DuplicateRunError, match=first.id) as caught:
            asyncio.run(service.start_run("HEAD", "quick"))
        assert "resume it" in str(caught.value)
