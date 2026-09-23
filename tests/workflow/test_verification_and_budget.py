import asyncio

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import MANUSCRIPT, PdfBuildingManuscriptManager, claim, make_repository, prepare_verification, submit


def test_failed_verification_returns_to_human_gate_without_revision_loop(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        patch, _ = prepare_verification(service, run.id)
        verifier = claim(service, run.id, AgentRole.VERIFICATION, session="independent-session")
        receipt = submit(
            service,
            verifier,
            {
                "verdict": "fail",
                "summary": "The patch needs another human-directed revision.",
                "resolved_finding_ids": [],
                "issues": [
                    {
                        "title": "Finding remains unresolved",
                        "explanation": "The proposed wording does not fully resolve the finding.",
                        "evidence": [],
                    }
                ],
            },
        )
        assert receipt["run_status"] == RunStatus.AWAITING_PATCH_APPROVAL
        assert service.get_patch(patch.id)["verifications"][0].result.value == "fail"
        assert len([task for task in service.database.list_tasks(run.id) if task.role == AgentRole.REVISION]) == 1
        assert (repo / "main.tex").read_text() == MANUSCRIPT
        assert not service.evaluate_gate(run.id)["passed"]
