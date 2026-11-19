import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, PatchStatus, RunStatus, TaskStatus
from scriptorium.errors import StateError
from scriptorium.service import ScriptoriumService

from ._support import (
    MANUSCRIPT,
    PdfBuildingManuscriptManager,
    approve_plan,
    check_output,
    check_task,
    claim,
    claim_check,
    complete_reviews,
    export_writing,
    make_repository,
    prepare_patch,
    review_finding,
    run_check,
    submit,
    submit_check,
    submit_writing,
    writing_task,
)

OPENING = "\\begin{document}\nThis note states one result."
RESULT = "The result is clear."


def _two_assignment_run(service):
    """A run whose two confirmed findings go to two assignments: opening (line 2) and result (line 3)."""
    run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
    first = review_finding(review)
    second = {**first, "title": "Second issue", "claim": "The opening needs a sentence."}
    submit(service, review, {"summary": "Two issues.", "findings": [first, second]})
    findings = [finding.id for finding in service.list_findings(run.id)]
    service.decide_findings(findings, "confirm", "Both need writing.")
    asyncio.run(service.resume_run(run.id))
    return run, findings


def _assignments(findings, *, result="Correct the typo."):
    return {
        "opening": (["main.tex:2-2"], [findings[1]], "Open with one sentence that states the topic."),
        "result": (["main.tex:3-3"], [findings[0]], result),
    }


def _write(service, run_id, assignment, text, *, session=None):
    writing, export = export_writing(service, run_id, assignment, session=session or f"writer-{assignment}")
    receipt = submit_writing(service, writing, export, {1: text})
    assert receipt["attempt"].status == AttemptStatus.COMPLETED, receipt["validation_report"]
    return writing["attempt"], receipt


def _pending_writing(service, run_id):
    return [
        service.store.get_external_task(task.id)["assignment_id"]
        for task in service.store.list_tasks(run_id)
        if task.stage == "writing" and task.status == TaskStatus.PENDING
    ]


def _checked_two_assignment_run(service):
    run, findings = _two_assignment_run(service)
    approve_plan(service, run.id, _assignments(findings))
    opening, _ = _write(service, run.id, "opening", OPENING)
    result, receipt = _write(service, run.id, "result", RESULT)
    assert receipt["run_status"] == RunStatus.CHECKING
    return run, findings, opening, result


def test_a_check_returns_one_of_two_assignments_and_only_that_one_is_rewritten(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, findings, opening, result = _checked_two_assignment_run(service)
        first_patch = service.store.list_patches(run.id)[-1]
        assert first_patch.sources == (
            {"assignment_id": "opening", "attempt_id": opening.id},
            {"assignment_id": "result", "attempt_id": result.id},
        )
        lead = claim_check(service, run.id)
        assert lead["task"].role == AgentRole.CHECK
        for text in ("This note states one result.", RESULT, opening.id, result.id, "Fact-change list", findings[0]):
            assert text in lead["prompt"]
        feedback = "Say what is clear about the result, as the plan asks."
        output = check_output(service, run.id, {"result": feedback})
        output["assignments"][0]["feedback"] = "The opening reads well."
        receipt = submit_check(service, lead, output)
        assert receipt["run_status"] == RunStatus.WRITING

        (check,) = service.store.list_checks(run.id)
        assert check.result.value == "fail" and check.patch_id == first_patch.id
        assert check.assignments == (
            {
                "assignment_id": "opening",
                "writing_attempt_id": opening.id,
                "verdict": "accept",
                "feedback": "The opening reads well.",
            },
            {"assignment_id": "result", "writing_attempt_id": result.id, "verdict": "redo", "feedback": feedback},
        )
        assert service.store.get_patch(first_patch.id).status == PatchStatus.RETURNED
        # Only the returned assignment gets a task; the accepted one keeps its output.
        assert _pending_writing(service, run.id) == ["result"]
        redo = writing_task(service, run.id, "result")
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "task claim", "task_id": redo.id}]
        metadata = service.store.get_external_task(redo.id)
        assert (metadata["check_id"], metadata["returned_attempt_id"]) == (check.id, result.id)
        with pytest.raises(StateError, match="cannot be decided while run is writing"):
            service.decide_patch(first_patch.id, "approve", "Not checked.")

        writing, export = export_writing(service, run.id, "result", session="writer-result-2")
        # The passage holds the returned text, after the line-break rule, and the prompt carries the feedback.
        assert (export / "task" / "passages" / "1.tex").read_text(encoding="utf-8") == RESULT + "\n"
        assert (export / "sources" / "main.tex").read_text(encoding="utf-8") == MANUSCRIPT
        assert (
            feedback in writing["prompt"] and result.id in writing["prompt"] and "This is a redo" in writing["prompt"]
        )
        unchanged = submit_writing(service, writing, export, {1: RESULT})
        assert [issue["code"] for issue in unchanged["validation_report"]["issues"]] == [
            "writing.unchanged_after_return"
        ]
        asyncio.run(service.retry_task(run.id, redo.id))
        writing, export = export_writing(service, run.id, "result", session="writer-result-2")
        assert "writing.unchanged_after_return" in writing["prompt"]
        receipt = submit_writing(service, writing, export, {1: "The result is clear on every benchmark."})
        assert receipt["run_status"] == RunStatus.CHECKING

        patch = service.store.list_patches(run.id)[-1]
        assert patch.id != first_patch.id
        assert patch.sources == (
            {"assignment_id": "opening", "attempt_id": opening.id},
            {"assignment_id": "result", "attempt_id": writing["attempt"].id},
        )
        run_check(service, run.id)
        assert service.store.get_patch(patch.id).status == PatchStatus.CHECKED
        shown = service.get_patch(patch.id)
        assert set(shown) == {"patch", "decisions", "checks", "diff", "facts"}
        assert [item.result.value for item in shown["checks"]] == ["pass"]
        assert "+The result is clear on every benchmark." in shown["diff"]
        assert shown["facts"]["removed"] == [] and shown["facts"]["added"] == []
        assert shown["facts"]["pages"] == [{"document": "main.tex", "before": 1, "after": 1}]

        service.decide_patch(patch.id, "approve", "The author approved the checked patch.")
        asyncio.run(service.resume_run(run.id))
        service.apply_patch(patch.id)
        assert "This note states one result.\nThe result is clear on every benchmark.\n" in (
            repo / "main.tex"
        ).read_text(encoding="utf-8")
        gate = service.evaluate_gate(run.id)
        assert gate["passed"] and gate["conditions"]["check_passed"]

        report = service.render_report(run.id, "json")
        assert [item["patch"]["status"] for item in report["patches"]] == ["returned", "applied"]
        returned = report["patches"][0]
        assert returned["checks"][0]["assignments"][1]["verdict"] == "redo"
        assert returned["facts"]["note"] == shown["facts"]["note"]
        assert service.read_report(run.id, "patches") == report["patches"]
        markdown = service.render_report(run.id, "markdown")
        assert f"check `{check.id}`: fail" in markdown
        assert f"`result` (writing attempt `{result.id}`): redo: {feedback}" in markdown
        assert "fact changes (A reading aid" in markdown and "pages of `main.tex`: 1 before, 1 after" in markdown


def test_every_check_code_and_the_session_rule(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        prepare_patch(service, run.id)
        valid = check_output(service, run.id)
        task_id = check_task(service, run.id).id

        def codes(claim_data, output):
            checked = submit_check(service, claim_data, output, check=True)
            return [] if checked["valid"] else [issue["code"] for issue in checked["validation_report"]["issues"]]

        # The writer's session wrote in this run; a declared session is not host-reported.
        writer = claim_check(service, run.id, session="writing-session", client="codex")
        assert codes(writer, valid) == ["check.session_unconfirmed"]
        asyncio.run(service.retry_task(run.id, task_id, writer["attempt"].id, "The writer cannot check."))
        declared = claim_check(service, run.id, session="declared-lead", source="declared")
        assert codes(declared, valid) == ["check.session_unconfirmed"]
        asyncio.run(service.retry_task(run.id, task_id, declared["attempt"].id, "Unconfirmed session."))

        # The reviewer's session did no writing, so it may check.
        lead = claim_check(service, run.id, session="session-substantive_review", client="codex")
        assert codes(lead, valid) == []
        verdict = valid["assignments"][0]
        for assignments in ([], [verdict, verdict], [{**verdict, "assignment_id": "other"}]):
            assert codes(lead, {**valid, "assignments": assignments}) == ["check.assignments_mismatch"]
        for feedback in (None, " \n"):
            redo = {**verdict, "verdict": "redo", "feedback": feedback}
            assert codes(lead, {**valid, "assignments": [redo]}) == ["check.feedback_required"]
        assert codes(lead, {**valid, "assignments": [], "summary": " "}) == [
            "schema.cross_field",
        ]
        assert codes(lead, {**valid, "assignments": [{**verdict, "verdict": "maybe"}]}) == ["schema.constraint"]
        receipt = submit_check(
            service, lead, {**valid, "assignments": [{**verdict, "verdict": "redo", "feedback": ""}]}
        )
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert [issue["code"] for issue in receipt["validation_report"]["issues"]] == ["check.feedback_required"]
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "run retry", "run_id": run.id, "task_id": task_id}
        ]
        asyncio.run(service.retry_task(run.id, task_id))
        lead = claim_check(service, run.id, session="session-substantive_review", client="codex")
        assert "Your previous CheckOutput was rejected." in lead["prompt"]
        accepted = submit_check(service, lead, {**valid, "assignments": [{**verdict, "feedback": "Fine as written."}]})
        assert accepted["run_status"] == RunStatus.AWAITING_PATCH_APPROVAL


def test_two_rounds_of_return_and_a_returned_patch_proposed_again(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first_patch, _ = prepare_patch(service, run.id)
        first = service.store.list_attempts(writing_task_id(service, run.id, 0))[-1]
        run_check(service, run.id, {"result": "Name the benchmark."})
        second, _ = _write(service, run.id, "result", "The result is clear on benchmark one.")
        run_check(service, run.id, {"result": "Go back to the short sentence; the benchmark is named later."})
        redo = writing_task(service, run.id, "result")
        writing, export = export_writing(service, run.id, "result", session="writer-result-3")
        # The second redo revises the second draft, with the second check's feedback.
        assert (export / "task" / "passages" / "1.tex").read_text(encoding="utf-8") == (
            "The result is clear on benchmark one.\n"
        )
        assert "Go back to the short sentence" in writing["prompt"] and second.id in writing["prompt"]
        assert service.store.get_external_task(redo.id)["returned_attempt_id"] == second.id
        receipt = submit_writing(service, writing, export, {1: RESULT})
        assert receipt["run_status"] == RunStatus.CHECKING

        # The third draft equals the first, so the returned first patch is proposed again with the new attempt.
        patches = service.store.list_patches(run.id)
        assert len(patches) == 2 and patches[-1].id == first_patch.id
        assert patches[-1].status == PatchStatus.PROPOSED
        assert patches[-1].sources == ({"assignment_id": "result", "attempt_id": writing["attempt"].id},)
        assert writing["attempt"].id != first.id
        reproposed = [item for item in service.store.list_events(run.id) if item.event_type == "patch.reproposed"]
        assert reproposed[-1].payload["from"] == "returned"
        lead = claim_check(service, run.id)
        assert writing["attempt"].id in lead["prompt"] and first.id not in lead["prompt"]
        submit_check(service, lead, check_output(service, run.id))
        assert [check.result.value for check in service.store.list_checks(run.id)] == ["fail", "fail", "pass"]
        assert service.store.get_patch(first_patch.id).status == PatchStatus.CHECKED


def writing_task_id(service, run_id, index):
    return [task.id for task in service.store.list_tasks(run_id) if task.stage == "writing"][index]


def test_an_unchanged_assignment_after_a_rejection_reuses_the_last_output_of_its_chain(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, findings, _, result = _checked_two_assignment_run(service)
        run_check(service, run.id, {"opening": "Name the topic in the opening sentence."})
        opening, _ = _write(service, run.id, "opening", "\\begin{document}\nThis note states one result on parsing.")
        run_check(service, run.id)
        patch = service.store.list_patches(run.id)[-1]
        service.decide_patch(patch.id, "reject", "The result sentence is too vague.")
        asyncio.run(service.resume_run(run.id))
        assert service.store.get_run(run.id).status == RunStatus.PLANNING

        tasks = len(service.store.list_tasks(run.id))
        approve_plan(service, run.id, _assignments(findings, result="State what is clear."))
        # The unchanged opening keeps the last output of its chain; only the changed result is rewritten.
        assert _pending_writing(service, run.id) == ["result"]
        assert len(service.store.list_tasks(run.id)) == tasks + 1
        rewritten, receipt = _write(service, run.id, "result", "The result is clear on every benchmark.")
        assert receipt["run_status"] == RunStatus.CHECKING
        latest = service.store.list_patches(run.id)[-1]
        assert latest.sources == (
            {"assignment_id": "opening", "attempt_id": opening.id},
            {"assignment_id": "result", "attempt_id": rewritten.id},
        )
        assert result.id != rewritten.id
        opening_edit = next(edit for edit in latest.edits if edit["start_line"] == 2)
        assert opening_edit["after"] == "\\begin{document}\nThis note states one result on parsing.\n"


def test_a_waiver_during_the_check_returns_the_run_to_planning(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, findings, opening, _ = _checked_two_assignment_run(service)
        pending = check_task(service, run.id)
        service.decide_finding(findings[0], "waive", "The author keeps the result sentence.")
        assert service.store.get_task(pending.id).status == TaskStatus.CANCELLED
        assert service.store.get_run(run.id).status == RunStatus.PLANNING
        assert service.list_tasks(run.id)["next_actions"] == [
            {"command": "plan approve", "run_id": run.id, "requires_human_decision": True}
        ]
        approve_plan(service, run.id, {"opening": _assignments(findings)["opening"]})
        # The unchanged opening keeps its output, so the new candidate goes straight to its check.
        assert service.store.get_run(run.id).status == RunStatus.CHECKING
        assert service.store.list_patches(run.id)[-1].sources == (
            {"assignment_id": "opening", "attempt_id": opening.id},
        )
        assert check_task(service, run.id).id != pending.id


def test_a_waiver_after_the_check_changes_nothing_in_the_flow(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run, findings, _, _ = _checked_two_assignment_run(service)
        run_check(service, run.id)
        patch = service.store.list_patches(run.id)[-1]
        service.decide_finding(findings[0], "waive", "The author accepts the result sentence as it was.")
        assert service.store.get_run(run.id).status == RunStatus.AWAITING_PATCH_APPROVAL
        assert service.store.get_patch(patch.id).status == PatchStatus.CHECKED
        service.decide_patch(patch.id, "approve", "Apply it anyway.")
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.READY_TO_APPLY
        service.decide_finding(findings[1], "waive", "Also fine as it was.")
        assert service.store.get_run(run.id).status == RunStatus.READY_TO_APPLY


def test_resume_replays_a_return_into_redo_tasks_and_the_redo_into_a_new_check(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        prepare_patch(service, run.id)
        lead = claim_check(service, run.id)
        output = json.dumps(check_output(service, run.id, {"result": "Say what is clear."}))
        # Accepted without advancing the run, as when a process stops after accepting an output.
        service.armarius.submit_task(lead["attempt"].id, lead["input_digest"], output)
        assert service.store.get_run(run.id).status == RunStatus.CHECKING
        assert service.store.list_checks(run.id) == []
        assert service.list_tasks(run.id)["next_actions"] == [{"command": "run resume", "run_id": run.id}]
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.WRITING
        assert _pending_writing(service, run.id) == ["result"]
        tasks, events = len(service.store.list_tasks(run.id)), len(service.store.list_events(run.id))
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.WRITING
        assert (len(service.store.list_tasks(run.id)), len(service.store.list_events(run.id))) == (tasks, events)

        writing, export = export_writing(service, run.id, "result", session="writer-2")
        (export / "task" / "passages" / "1.tex").write_text("The result is clear on benchmark one.", encoding="utf-8")
        packed = service.pack_writing_output(writing["attempt"].id, export)
        service.armarius.submit_task(writing["attempt"].id, writing["input_digest"], packed)
        assert service.store.get_run(run.id).status == RunStatus.WRITING
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.CHECKING
        assert len(service.store.list_patches(run.id)) == 2
        run_check(service, run.id)
        assert service.store.get_run(run.id).status == RunStatus.AWAITING_PATCH_APPROVAL


def test_a_check_cannot_be_claimed_from_an_earlier_patch_after_a_return(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        complete_reviews(service, run.id)
        finding_id = service.list_findings(run.id)[0].id
        service.decide_finding(finding_id, "confirm", "Correct the typo.")
        asyncio.run(service.resume_run(run.id))
        approve_plan(service, run.id)
        _write(service, run.id, "result", RESULT, session="writing-session")
        first = check_task(service, run.id)
        run_check(service, run.id, {"result": "Say what is clear."})
        with pytest.raises(StateError, match="cannot be claimed while run is writing"):
            service.claim_task(first.id, "claude_code", "test-model", "max", "lead-session", "host")
