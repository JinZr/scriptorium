import asyncio

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, make_repository, prepare_patch, submit


def test_rejected_patch_creates_external_revision_with_human_feedback(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first_patch, finding_id = prepare_patch(service, run.id)
        service.decide_patch(first_patch.id, "reject", "Use more precise wording.")
        resumed = asyncio.run(service.resume_run(run.id))
        assert resumed["run"].status == RunStatus.REVISING
        tasks = [task for task in service.database.list_tasks(run.id) if task.role == AgentRole.REVISION]
        assert len(tasks) == 2
        second = service.claim_task(tasks[-1].id, "claude_code", "sonnet", "max", "new-revision-session", "host")
        assert "Use more precise wording." in second["prompt"]
        source = next(item for item in second["source_map"]["sources"] if item["source_path"] == "main.tex")
        receipt = submit(
            service,
            second,
            {
                "summary": "Made the wording precise.",
                "edits": [
                    {
                        "finding_ids": [finding_id],
                        "path": "main.tex",
                        "source_digest": source["source_digest"],
                        "start_line": 3,
                        "end_line": 3,
                        "before": "The result is teh clear.",
                        "after": "The result is clear and precise.",
                        "rationale": "Addresses the feedback.",
                    }
                ],
            },
        )
        assert receipt["run_status"] == RunStatus.AWAITING_PATCH_APPROVAL
        patches = service.database.list_patches(run.id)
        assert len(patches) == 2
        assert patches[0].id == first_patch.id
        assert patches[1].id != first_patch.id
        assert (repo / "main.tex").read_text().find("teh clear") >= 0
