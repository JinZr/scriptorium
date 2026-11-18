import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit

LIMITATIONS = ["The supplement cited in Section 2 is not among the frozen sources."]


def test_accepted_review_limitations_are_reported_in_json_and_markdown(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review", "copyedit"))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        substantive = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "limitations" in substantive["schema"]["required"]
        assert "scope" not in substantive["schema"]["properties"]
        receipt = submit(service, substantive, {"summary": "Reviewed.", "findings": [], "limitations": LIMITATIONS})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        copyedit = claim(service, run.id, AgentRole.COPYEDIT)
        assert submit(service, copyedit, {"summary": "Reviewed.", "findings": []})["attempt"].status == (
            AttemptStatus.COMPLETED
        )

        report = service.render_report(run.id, "json")
        assert report["review_limitations"] == [
            {
                "task_id": substantive["task"].id,
                "attempt_id": substantive["attempt"].id,
                "role": "substantive_review",
                "limitations": LIMITATIONS,
            },
            {
                "task_id": copyedit["task"].id,
                "attempt_id": copyedit["attempt"].id,
                "role": "copyedit",
                "limitations": [],
            },
        ]
        assert {"review_scopes", "review_tool_access", "review_coverage_audit"}.isdisjoint(report)
        markdown = service.render_report(run.id, "markdown")
        assert "## Reviewer limitations" in markdown
        assert f"- `{substantive['attempt'].id}` / substantive_review:\n  - {LIMITATIONS[0]}\n" in markdown
        assert f"- `{copyedit['attempt'].id}` / copyedit: none\n" in markdown
        assert "declared scope" not in markdown.lower()
        assert "task-tool returns" not in markdown


def test_rejected_review_reports_no_limitations(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        rejected = asyncio.run(
            service.submit_task(
                review["attempt"].id,
                review["input_digest"],
                json.dumps({"summary": "Missing limitations.", "findings": []}),
            )
        )
        assert rejected["attempt"].status == AttemptStatus.FAILED
        assert service.render_report(run.id, "json")["review_limitations"] == []


@pytest.mark.parametrize("artifact_damage", ["missing", "corrupt"])
def test_accepted_review_completes_the_task_and_the_required_review_gate(tmp_path, artifact_damage):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        receipt = submit(service, review, {"summary": "Reviewed.", "findings": [], "limitations": LIMITATIONS})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert service.database.get_run(run.id).status == RunStatus.AWAITING_DECISION
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
        assert service.evaluate_gate(run.id)["conditions"]["required_reviews_completed"]

        asyncio.run(service.resume_run(run.id))
        assert service.database.get_run(run.id).status == RunStatus.COMPLETED
        assert service.evaluate_gate(run.id)["passed"]

        output = service.artifacts.path_for(receipt["output_digest"])
        if artifact_damage == "missing":
            output.unlink()
        else:
            output.write_text("corrupt", encoding="utf-8")
        gate = service.evaluate_gate(run.id)
        assert not gate["passed"]
        assert not gate["conditions"]["review_artifacts_valid"]
        assert review["attempt"].id in gate["errors"][0]
