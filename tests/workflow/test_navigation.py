import asyncio

import pytest

from scriptorium.domain import AgentRole
from scriptorium.errors import InfrastructureError
from scriptorium.service import ScriptoriumService

from ._support import MANUSCRIPT, PdfBuildingManuscriptManager, claim, make_repository


def test_run_uses_frozen_navigation_after_worktree_changes(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        original = service.read_task(review["attempt"].id, "main.tex", 1, 4, 0, 8000)
        (repo / "main.tex").write_text("changed working tree\n", encoding="utf-8")
        frozen = service.read_task(review["attempt"].id, "main.tex", 1, 4, 0, 8000)
        assert frozen == original
        assert "The result is teh clear." in "".join(line["text"] for line in frozen["lines"])
        assert MANUSCRIPT != (repo / "main.tex").read_text()


def test_corrupt_frozen_navigation_blocks_claim_without_attempt(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        task = service.database.list_tasks(run.id)[0]
        navigation = repo / ".scriptorium" / "runs" / run.id / "bundle" / "navigation.json"
        navigation.write_text("{}\n", encoding="utf-8")
        with pytest.raises(InfrastructureError, match="navigation"):
            claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert service.database.list_attempts(task.id) == []
