from dataclasses import replace

import pytest

from scriptorium.domain import (
    AgentRole,
    Artifact,
    Attempt,
    AttemptStatus,
    Decision,
    Event,
    Finding,
    FindingSeverity,
    Patch,
    Task,
    TaskStatus,
)
from scriptorium.storage import ConflictError, Database, NotFoundError, StorageError

from ._factories import create_completed_attempt, make_run


def _finding(run, task, attempt, fingerprint: str) -> Finding:
    return Finding(
        run_id=run.id,
        task_id=task.id,
        attempt_id=attempt.id,
        fingerprint=fingerprint,
        role=AgentRole.COPYEDIT,
        category="language",
        severity=FindingSeverity.MINOR,
        title="Typo",
        claim="A word is misspelled.",
        evidence=({"source_path": "main.tex", "start_line": 1, "end_line": 1},),
        explanation="The word is misspelled.",
        suggested_action="Fix the spelling.",
        confidence=1,
        consequence="A reader stumbles.",
    )


def _diff(database: Database, digest: str) -> Artifact:
    return database.record_artifact(Artifact(digest=digest, relative_path=f"sha256/{digest[:2]}/{digest[2:]}", size=1))


def test_one_task_per_run_stage_role_and_input_digest(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        other = database.create_run(make_run())
        task = database.create_task(Task(run_id=run.id, stage="review", role=AgentRole.COPYEDIT, input_digest="d"))

        with pytest.raises(ConflictError, match="stage, role, and input digest"):
            database.create_task(Task(run_id=run.id, stage="review", role=AgentRole.COPYEDIT, input_digest="d"))
        with pytest.raises(ConflictError, match="task already exists"):
            database.create_task(replace(task, input_digest="e"))
        database.create_task(Task(run_id=run.id, stage="review", role=AgentRole.CONSISTENCY, input_digest="d"))
        database.create_task(Task(run_id=other.id, stage="review", role=AgentRole.COPYEDIT, input_digest="d"))
        assert database.find_task(run.id, "review", AgentRole.COPYEDIT, "d") == task
        assert len(database.list_tasks(run.id)) == 2


def test_one_attempt_per_task_ordinal_and_an_attempt_names_its_task(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        task, first = create_completed_attempt(database, run)
        database.update_task_status(task.id, TaskStatus.PENDING)

        with pytest.raises(ConflictError, match="attempt ordinal must be 2"):
            database.create_attempt(Attempt(task_id=task.id, ordinal=1))
        with pytest.raises(ConflictError, match="attempt already exists"):
            database.create_attempt(Attempt(task_id=task.id, ordinal=2, id=first.id))
        with pytest.raises(NotFoundError, match="task not found"):
            database.create_attempt(Attempt(task_id="task_missing", ordinal=1))
        second = database.begin_attempt(task.id)
        with pytest.raises(ConflictError, match="already has a running attempt"):
            database.begin_attempt(task.id)
        assert [item.ordinal for item in database.list_attempts(task.id)] == [1, 2]
        assert second.ordinal == 2


def test_terminal_attempts_cannot_change_and_are_never_removed(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)
        events = database.list_events(run.id)

        with pytest.raises(ConflictError, match="already terminal"):
            database.finish_attempt(attempt.id, AttemptStatus.FAILED, error="changed")
        with pytest.raises(ConflictError, match="already terminal"):
            database.record_attempt_session(attempt.id, "thread-1")
        assert database.recover_orphaned_attempts(run.id) == 0
        database.cancel_run(run.id, "stop", "cancel-1")

        assert database.get_attempt(attempt.id) == attempt
        assert database.list_attempts(task.id) == [attempt]
        assert database.list_events(run.id)[: len(events)] == events


def test_one_finding_per_run_fingerprint_and_its_references_resolve(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)
        other = database.create_run(make_run())
        other_task, other_attempt = create_completed_attempt(database, other)
        first = database.create_finding(_finding(run, task, attempt, "f" * 64))

        with pytest.raises(ConflictError, match="finding already exists for fingerprint"):
            database.create_finding(_finding(run, task, attempt, "f" * 64))
        assert database.get_or_create_finding(_finding(run, task, attempt, "f" * 64)) == first
        with pytest.raises(StorageError, match="task is not recorded"):
            database.create_finding(_finding(run, other_task, attempt, "1" * 64))
        with pytest.raises(StorageError, match="attempt is not recorded"):
            database.create_finding(_finding(run, task, other_attempt, "2" * 64))
        assert database.create_finding(_finding(other, other_task, other_attempt, "f" * 64)).run_id == other.id
        assert [item.id for item in database.list_findings(run.id)] == [first.id]
        assert database.get_finding(first.id).confidence == 1.0


def test_one_patch_per_run_base_and_diff_and_its_references_resolve(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        _, attempt = create_completed_attempt(database, run)
        diff = _diff(database, "1" * 64)

        def patch(digest: str, attempt_id: str = attempt.id) -> Patch:
            return Patch(run.id, run.commit_sha, digest, "Fix.", (), attempt_id=attempt_id)

        first = database.create_patch(patch(diff.digest))
        with pytest.raises(ConflictError, match="base commit, and diff digest"):
            database.create_patch(patch(diff.digest))
        with pytest.raises(StorageError, match="artifact is not recorded"):
            database.create_patch(patch("2" * 64))
        with pytest.raises(StorageError, match="attempt is not recorded"):
            database.create_patch(patch(_diff(database, "3" * 64).digest, "attempt_missing"))
        assert [item.id for item in database.list_patches(run.id)] == [first.id]


def test_decisions_and_events_are_append_only(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)
        finding = database.create_finding(_finding(run, task, attempt, "f" * 64))
        event = database.append_event(Event(run.id, "test", "run", run.id, {"n": 1}))
        decision = database.append_decision(Decision("finding", finding.id, "confirm", "Real."))

        with pytest.raises(ConflictError, match="event already exists"):
            database.append_event(replace(event, payload={"n": 2}))
        with pytest.raises(ConflictError, match="decision already exists"):
            database.append_decision(replace(decision, decision="reject", reason="Changed."))
        with pytest.raises(ValueError, match="decisions target a finding or a patch"):
            database.append_decision(Decision("run", run.id, "confirm", "Unsupported."))
        assert [item for item in database.list_events(run.id) if item.id == event.id] == [event]
        assert database.list_decisions("finding", finding.id) == [decision]
        assert database.get_finding(finding.id).status.value == "confirmed"


def test_decisions_reach_findings_of_one_run_only(tmp_path) -> None:
    with Database(tmp_path) as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)
        other = database.create_run(make_run())
        other_task, other_attempt = create_completed_attempt(database, other)
        finding = database.create_finding(_finding(run, task, attempt, "f" * 64))
        foreign = database.create_finding(_finding(other, other_task, other_attempt, "f" * 64))

        with pytest.raises(ValueError, match="same run"):
            database.decide_findings([finding.id, foreign.id], "reject", "Not real.")
        with pytest.raises(NotFoundError, match="finding not found"):
            database.decide_findings([finding.id, "finding_missing"], "reject", "Not real.")
        assert database.list_decisions("finding", finding.id) == []
        assert database.get_finding(finding.id).status.value == "pending"
