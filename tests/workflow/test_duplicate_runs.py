import asyncio
import subprocess

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
