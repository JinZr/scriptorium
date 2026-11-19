import asyncio
import json

import pytest

from scriptorium import cli
from scriptorium.domain import AgentRole, AttemptStatus, RunStatus
from scriptorium.errors import BuildFailedError, InfrastructureError
from scriptorium.service import ScriptoriumService

from ._support import (
    MANUSCRIPT,
    PdfBuildingManuscriptManager,
    approve_plan,
    claim,
    complete_reviews,
    export_writing,
    make_repository,
    submit,
    submit_writing,
    write_plan,
)


class CompilingManager(PdfBuildingManuscriptManager):
    """A fake LaTeX build that fails like LaTeX on an undefined macro, or on both ALPHA and BETA together."""

    def __init__(self, repo):
        super().__init__(repo)
        self.missing_tool = False

    def build(self, workspace, manuscript):
        if self.missing_tool:
            raise InfrastructureError("latexmk is required but was not found")
        text = (workspace / manuscript.main).read_text(encoding="utf-8")
        if "\\undefined" in text or ("ALPHA" in text and "BETA" in text):
            log = "! Undefined control sequence.\nl.3 \\undefined\n"
            raise BuildFailedError(f"LaTeX build failed:\n{log}", log)
        return super().build(workspace, manuscript)


def _planning_run(service, *, findings=True):
    run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    complete_reviews(service, run.id, with_finding=findings)
    finding_ids = [finding.id for finding in service.list_findings(run.id)]
    if finding_ids:
        service.decide_findings(finding_ids, "confirm", "Correct the typo.")
    asyncio.run(service.resume_run(run.id))
    return run, finding_ids


def _cli(capsys, *arguments):
    code = cli.main(["--json", *arguments])
    return code, json.loads(capsys.readouterr().out)


def test_plan_check_and_approve_through_the_cli(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        run, (finding_id,) = _planning_run(service)
        assert service.store.get_run(run.id).status == RunStatus.PLANNING
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "plan approve", "run_id": run.id, "requires_human_decision": True}
        ]

        unassigned = write_plan(tmp_path / "unassigned", {"opening": (["main.tex:2-2"], None, "Open plainly.")})
        code, checked = _cli(capsys, "plan", "check", run.id, "--dir", str(unassigned))
        assert code == 0 and checked["data"]["valid"] is False
        assert [issue["code"] for issue in checked["data"]["issues"]] == ["plan.findings_unassigned"]
        code, refused = _cli(capsys, "plan", "approve", run.id, "--dir", str(unassigned), "--reason", "Approved.")
        assert code == 2 and refused["error"]["code"] == "configuration_error"
        assert "plan.findings_unassigned" in refused["error"]["message"]
        assert service.store.list_plans(run.id) == [] and service.store.get_run(run.id).status == RunStatus.PLANNING

        plan = write_plan(tmp_path / "plan", {"result": (["main.tex:2-3"], [finding_id], "Correct the typo.")})
        code, checked = _cli(capsys, "plan", "check", run.id, "--dir", str(plan))
        assert code == 0 and checked["data"]["valid"] is True and checked["data"]["issues"] == []
        assert checked["data"]["assignments"] == [
            {
                "id": "result",
                "finding_ids": [finding_id],
                "targets": [
                    {
                        "index": 1,
                        "path": "main.tex",
                        "start_line": 2,
                        "end_line": 3,
                        "first_line": "\\begin{document}",
                        "last_line": "The result is teh clear.",
                    }
                ],
            }
        ]
        assert service.store.list_plans(run.id) == []

        code, overview = _cli(capsys, "plan", "approve", run.id, "--dir", str(plan), "--reason", "The author agreed.")
        assert code == 0 and overview["data"]["run"]["status"] == "writing"
        assert overview["data"]["task_counts"] == {"completed": 2, "pending": 1}
        (recorded,) = service.store.list_plans(run.id)
        assert recorded.digest == checked["data"]["plan_digest"]
        assert recorded.assignments[0]["targets"] == [{"path": "main.tex", "start_line": 2, "end_line": 3}]
        decisions = service.store.list_decisions("plan", recorded.id)
        assert [(item.decision, item.reason) for item in decisions] == [("approve", "The author agreed.")]
        assert "plan.decided" in {event.event_type for event in service.store.list_events(run.id)}
        writing = next(task for task in service.store.list_tasks(run.id) if task.stage == "writing")
        metadata = service.store.get_external_task(writing.id)
        assert (metadata["plan_id"], metadata["assignment_id"]) == (recorded.id, "result")

        code, refused = _cli(capsys, "plan", "check", run.id, "--dir", str(plan))
        assert code == 1 and refused["error"]["code"] == "invalid_state"
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as reopened:
        assert reopened.store.list_plans(run.id) == [recorded]
        assert reopened.store.get_plan(recorded.id) == recorded


def test_a_writing_task_is_submitted_from_its_export_directory(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        run, _ = _planning_run(service)
        approve_plan(service, run.id)
        writing, export = export_writing(service, run.id)
        attempt_id, digest = writing["attempt"].id, writing["input_digest"]
        submit_args = ("task", "submit", attempt_id, "--input-digest", digest)

        passage = export / "task" / "passages" / "1.tex"
        passage.unlink()
        code, missing = _cli(capsys, *submit_args, "--dir", str(export), "--check")
        assert code == 2 and "task/passages/1.tex" in missing["error"]["message"]
        passage.write_bytes(b"\xff")
        code, undecodable = _cli(capsys, *submit_args, "--dir", str(export), "--check")
        assert code == 2 and "not UTF-8" in undecodable["error"]["message"]
        passage.write_text("The result is clear.", encoding="utf-8")
        (export / "task" / "note.md").write_text("n" * 8_001, encoding="utf-8")
        code, long_note = _cli(capsys, *submit_args, "--dir", str(export), "--check")
        assert code == 2 and "8000" in long_note["error"]["message"]
        (export / "task" / "note.md").write_text("Kept the sentence short.", encoding="utf-8")

        code, both = _cli(capsys, *submit_args, "--dir", str(export), "--file", "output.json")
        assert code == 2 and both["error"]["code"] == "invalid_arguments"
        code, checked = _cli(capsys, *submit_args, "--dir", str(export), "--check")
        assert code == 0 and checked["data"]["valid"] is True and checked["data"]["recorded"] is False
        code, receipt = _cli(capsys, *submit_args, "--dir", str(export))
        assert code == 0 and receipt["data"]["attempt"]["status"] == "completed"
        assert receipt["data"]["run_status"] == "awaiting_patch_approval"
        stored = json.loads(service.artifacts.get_bytes(receipt["data"]["output_digest"]))
        assert stored == {
            "passages": [{"index": 1, "text": "The result is clear."}],
            "note": "Kept the sentence short.",
        }
        patch = service.store.list_patches(run.id)[-1]
        assert "Note from result: Kept the sentence short." in patch.summary
        assert (
            patch.edits[0]["before"] == "The result is teh clear.\n"
            and patch.edits[0]["after"] == "The result is clear.\n"
        )
        # Resubmitting the same directory returns the same receipt.
        code, again = _cli(capsys, *submit_args, "--dir", str(export))
        assert code == 0 and again["data"]["attempt"]["id"] == attempt_id

        review = next(task for task in service.store.list_tasks(run.id) if task.stage == "review")
        review_attempt = service.store.list_attempts(review.id)[-1]
        code, refused = _cli(
            capsys, "task", "submit", review_attempt.id, "--input-digest", digest, "--dir", str(export)
        )
        assert code == 2 and "only for writing attempts" in refused["error"]["message"]


def test_an_unchanged_or_mismatched_writing_output_fails_and_the_correction_returns_to_the_writer(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, _ = _planning_run(service)
        approve_plan(service, run.id)
        writing, export = export_writing(service, run.id)
        # Dropping the final line break changes nothing: it is restored.
        receipt = submit_writing(service, writing, export, {1: "The result is teh clear."})
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert [issue["code"] for issue in receipt["validation_report"]["issues"]] == ["writing.no_change"]
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "run retry", "run_id": run.id, "task_id": writing["task"].id}
        ]
        asyncio.run(service.retry_task(run.id, writing["task"].id))
        retried, _ = export_writing(service, run.id)
        assert "Your previous passages were rejected." in retried["prompt"] and "writing.no_change" in retried["prompt"]
        mismatched = json.dumps({"passages": [{"index": 2, "text": "The result is clear."}], "note": None})
        receipt = asyncio.run(service.submit_task(retried["attempt"].id, retried["input_digest"], mismatched))
        assert [issue["code"] for issue in receipt["validation_report"]["issues"]] == ["writing.passages_mismatch"]
        assert service.store.list_patches(run.id) == []


def test_a_latex_error_in_a_passage_returns_to_the_writer_and_the_run_stays_usable(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=CompilingManager(repo)) as service:
        run, _ = _planning_run(service)
        approve_plan(service, run.id)
        writing, export = export_writing(service, run.id)
        checked = submit_writing(service, writing, export, {1: "The result is \\undefined clear."}, check=True)
        (issue,) = checked["validation_report"]["issues"]
        assert issue["code"] == "writing.build_failed" and "Undefined control sequence" in issue["actual"]["log_tail"]
        assert service.store.get_attempt(writing["attempt"].id).status == AttemptStatus.RUNNING
        receipt = submit_writing(service, writing, export, {1: "The result is \\undefined clear."})
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert receipt["run_status"] == RunStatus.WRITING
        assert not list((service.repo / ".scriptorium" / "runs" / run.id / "build").glob("writing-check-*"))
        asyncio.run(service.retry_task(run.id, writing["task"].id))
        retried, export = export_writing(service, run.id)
        assert "writing.build_failed" in retried["prompt"]
        receipt = submit_writing(service, retried, export, {1: "The result is clear."})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert receipt["run_status"] == RunStatus.AWAITING_PATCH_APPROVAL


def test_a_missing_tool_while_compiling_a_submission_is_inconclusive(tmp_path):
    repo = make_repository(tmp_path)
    manager = CompilingManager(repo)
    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        run, _ = _planning_run(service)
        approve_plan(service, run.id)
        writing, export = export_writing(service, run.id)
        manager.missing_tool = True
        with pytest.raises(InfrastructureError, match="latexmk"):
            submit_writing(service, writing, export, {1: "The result is clear."})
        assert service.store.get_attempt(writing["attempt"].id).status == AttemptStatus.RUNNING
        assert service.store.get_run(run.id).status == RunStatus.WRITING
        manager.missing_tool = False
        receipt = submit_writing(service, writing, export, {1: "The result is clear."})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED


def test_two_assignments_one_without_findings_reach_a_passing_gate(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, (finding_id,) = _planning_run(service)
        approve_plan(
            service,
            run.id,
            {
                "result": (["main.tex:3-3"], [finding_id], "Correct the typo."),
                "opening": (["main.tex:2-2"], None, "Open with one sentence that states the topic."),
            },
        )
        assert [task.status.value for task in service.store.list_tasks(run.id) if task.stage == "writing"] == [
            "pending",
            "pending",
        ]
        writing, export = export_writing(service, run.id, "opening", session="writer-opening")
        receipt = submit_writing(service, writing, export, {1: "\\begin{document}\nThis note states one result."})
        assert receipt["run_status"] == RunStatus.WRITING
        writing, export = export_writing(service, run.id, "result", session="writer-result")
        receipt = submit_writing(service, writing, export, {1: "The result is clear."})
        assert receipt["run_status"] == RunStatus.AWAITING_PATCH_APPROVAL

        patch = service.store.list_patches(run.id)[-1]
        plan = service.store.list_plans(run.id)[-1]
        assert patch.plan_id == plan.id
        assert sorted((edit["start_line"], tuple(edit["finding_ids"])) for edit in patch.edits) == [
            (2, ()),
            (3, (finding_id,)),
        ]
        service.decide_patch(patch.id, "approve", "The author approved the patch.")
        asyncio.run(service.resume_run(run.id))
        reused = claim(service, run.id, AgentRole.VERIFICATION, session="writer-result")
        rejected = submit(
            service,
            reused,
            {"verdict": "pass", "summary": "Checked.", "resolved_finding_ids": [finding_id], "issues": []},
        )
        assert "verification.session_unconfirmed" in {
            issue["code"] for issue in rejected["validation_report"]["issues"]
        }
        asyncio.run(service.retry_task(run.id, reused["task"].id))
        verifier = claim(service, run.id, AgentRole.VERIFICATION, session="independent-verifier")
        receipt = submit(
            service,
            verifier,
            {"verdict": "pass", "summary": "Checked.", "resolved_finding_ids": [finding_id], "issues": []},
        )
        assert receipt["run_status"] == RunStatus.READY_TO_APPLY
        service.apply_patch(patch.id)
        assert (repo / "main.tex").read_text() == MANUSCRIPT.replace(
            "\\begin{document}\n", "\\begin{document}\nThis note states one result.\n"
        ).replace("teh clear", "clear")
        assert service.evaluate_gate(run.id)["passed"]

        report = service.render_report(run.id, "json")
        (item,) = report["plans"]
        assert item["plan"]["id"] == plan.id and item["plan"]["digest"] == plan.digest
        assert [assignment["id"] for assignment in item["plan"]["assignments"]] == ["opening", "result"]
        assert item["decisions"][0]["reason"] == "The author approved this plan."
        assert sorted(task["assignment_id"] for task in report["tasks"] if "assignment_id" in task) == [
            "opening",
            "result",
        ]
        markdown = service.render_report(run.id, "markdown")
        assert "## Plans" in markdown and f"`{plan.id}`" in markdown
        assert "assignment `opening`" in markdown and f"(plan `{plan.id}`)" in markdown
        assert service.read_report(run.id, "plans") == report["plans"]


def test_assignments_that_compile_alone_but_not_together_return_the_run_to_planning(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=CompilingManager(repo)) as service:
        run, (finding_id,) = _planning_run(service)
        approve_plan(
            service,
            run.id,
            {
                "result": (["main.tex:3-3"], [finding_id], "Correct the typo."),
                "opening": (["main.tex:1-1"], None, "Keep the class."),
            },
        )
        writing, export = export_writing(service, run.id, "opening", session="writer-1")
        submit_writing(service, writing, export, {1: "\\documentclass{article} % ALPHA"})
        writing, export = export_writing(service, run.id, "result", session="writer-2")
        receipt = submit_writing(service, writing, export, {1: "The result is BETA clear."})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert receipt["run_status"] == RunStatus.PLANNING
        run_now = service.store.get_run(run.id)
        assert "compile alone but not together" in run_now.error
        assert service.store.list_patches(run.id) == []
        (event,) = [item for item in service.store.list_events(run.id) if item.event_type == "plan.build_failed"]
        assert "Undefined control sequence" in service.artifacts.get_bytes(event.payload["log_digest"]).decode()
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "plan approve", "run_id": run.id, "requires_human_decision": True}
        ]
        events = len(service.store.list_events(run.id))
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.PLANNING
        assert len(service.store.list_events(run.id)) == events
        approve_plan(service, run.id, {"result": (["main.tex:3-3"], [finding_id], "Correct the typo only.")})
        assert service.store.get_run(run.id).error is None
        writing, export = export_writing(service, run.id, "result", session="writer-3")
        receipt = submit_writing(service, writing, export, {1: "The result is clear."})
        assert receipt["run_status"] == RunStatus.AWAITING_PATCH_APPROVAL
