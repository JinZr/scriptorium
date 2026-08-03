import asyncio
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from scriptorium.domain import (
    AgentRole,
    Artifact,
    AttemptStatus,
    Finding,
    FindingSeverity,
    Patch,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from scriptorium.errors import InfrastructureError, NotFoundError, StateError
from scriptorium.service import ScriptoriumService
from scriptorium.storage import Database

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository


class BlockingAgentRuntime(FakeAgentRuntime):
    def __init__(self, ready_queue, release_event):
        super().__init__()
        self.ready_queue = ready_queue
        self.release_event = release_event

    async def run_agent(self, task, role, workspace, schema, session_dir):
        self.ready_queue.put(role.value)
        while not self.release_event.is_set():
            await asyncio.sleep(0.01)
        return await super().run_agent(task, role, workspace, schema, session_dir)


def _run_blocked_start(repo_path, ready_queue, release_event, result_queue):
    repo = Path(repo_path)
    runtime = BlockingAgentRuntime(ready_queue, release_event)
    try:
        with ScriptoriumService(
            repo,
            runtime_factory=lambda route: runtime,
            manuscript_manager=PdfBuildingManuscriptManager(repo),
        ) as service:
            view = asyncio.run(service.start_run("HEAD", "quick", None))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("ok", view["run"].id, view["run"].status.value))


def _run_blocked_resume(repo_path, run_id, ready_queue, release_event, result_queue):
    repo = Path(repo_path)
    runtime = BlockingAgentRuntime(ready_queue, release_event)
    try:
        with ScriptoriumService(
            repo,
            runtime_factory=lambda route: runtime,
            manuscript_manager=PdfBuildingManuscriptManager(repo),
        ) as service:
            view = asyncio.run(service.resume_run(run_id))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("ok", view["run"].id, view["run"].status.value))


def _hold_raw_lock(path, contents, ready_event, release_event):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        handle.truncate()
        handle.write(contents)
        handle.flush()
        ready_event.set()
        release_event.wait(30)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _start_blocked_process(repo):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    release_event = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_run_blocked_start,
        args=(str(repo), ready_queue, release_event, result_queue),
    )
    process.start()
    try:
        roles = {ready_queue.get(timeout=30), ready_queue.get(timeout=30)}
        assert roles == {AgentRole.SUBSTANTIVE_REVIEW.value, AgentRole.COPYEDIT.value}
        with Database(repo / ".scriptorium" / "state.sqlite3") as database:
            run = database.list_runs()[0]
            tasks = database.list_tasks(run.id)
            assert all(database.list_attempts(task.id)[-1].status == AttemptStatus.RUNNING for task in tasks)
    except BaseException:
        release_event.set()
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(10)
        raise
    return process, release_event, result_queue, run.id, tasks


def _start_blocked_resume_process(repo, run_id):
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    release_event = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_run_blocked_resume,
        args=(str(repo), run_id, ready_queue, release_event, result_queue),
    )
    process.start()
    try:
        roles = {ready_queue.get(timeout=30), ready_queue.get(timeout=30)}
        assert roles == {AgentRole.SUBSTANTIVE_REVIEW.value, AgentRole.COPYEDIT.value}
    except BaseException:
        release_event.set()
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(10)
        raise
    return process, release_event, result_queue


def _start_raw_lock_process(lock_path, contents=b"{"):
    context = multiprocessing.get_context("spawn")
    ready_event = context.Event()
    release_event = context.Event()
    process = context.Process(
        target=_hold_raw_lock,
        args=(str(lock_path), contents, ready_event, release_event),
    )
    process.start()
    try:
        assert ready_event.wait(20)
    except BaseException:
        release_event.set()
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(10)
        raise
    return process, release_event


def _stop_process(process, release_event):
    release_event.set()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("blocked Scriptorium child did not exit")


def _run_rows(repo, run_id):
    connection = sqlite3.connect(repo / ".scriptorium" / "state.sqlite3")
    try:
        return {
            "run": connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchall(),
            "tasks": connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall(),
            "attempts": connection.execute(
                """
                SELECT attempts.* FROM attempts
                JOIN tasks ON tasks.id = attempts.task_id
                WHERE tasks.run_id = ? ORDER BY attempts.id
                """,
                (run_id,),
            ).fetchall(),
            "events": connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall(),
        }
    finally:
        connection.close()


def _create_orphaned_run(repo):
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        run = database.create_run(
            Run(
                repository=str(repo),
                commit_sha="a" * 40,
                tree_sha="b" * 40,
                profile="quick",
                config_digest="c" * 64,
                frozen_config={"profile_roles": [AgentRole.CONSISTENCY.value]},
            )
        )
        database.update_run(run.id, RunStatus.REVIEWING)
        task = database.create_task(
            Task(
                run_id=run.id,
                stage="review",
                role=AgentRole.CONSISTENCY,
                route="primary",
                input_digest="d" * 64,
            )
        )
        attempt = database.begin_attempt(task.id)
    return run, task, attempt


def test_live_start_owner_rejects_resume_retry_and_cancel_without_state_changes(tmp_path):
    repo = make_repository(tmp_path)
    process, release_event, result_queue, run_id, tasks = _start_blocked_process(repo)
    owner = json.loads((repo / ".scriptorium" / "locks" / f"{run_id}.lock").read_text(encoding="utf-8"))
    assert set(owner) == {"operation", "pid", "hostname", "acquired_at"}
    assert owner["operation"] == "run start"
    assert owner["pid"] == process.pid
    before = _run_rows(repo, run_id)

    try:
        with ScriptoriumService(
            repo,
            runtime_factory=lambda route: FakeAgentRuntime(),
            manuscript_manager=PdfBuildingManuscriptManager(repo),
        ) as service:
            operations = (
                lambda: asyncio.run(service.resume_run(run_id)),
                lambda: asyncio.run(service.retry_task(run_id, tasks[0].id)),
                lambda: service.cancel_run(run_id, "stop"),
            )
            for operation in operations:
                with pytest.raises(StateError) as caught:
                    operation()
                message = str(caught.value)
                assert caught.value.code == "invalid_state"
                assert f"run {run_id} is already being changed by run start" in message
                assert "pid " in message
                assert "host " in message
                assert "acquired_at " in message
                assert "age " in message

        assert _run_rows(repo, run_id) == before
    finally:
        _stop_process(process, release_event)

    assert process.exitcode == 0
    assert result_queue.get(timeout=5) == ("ok", run_id, RunStatus.AWAITING_DECISION.value)
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        attempts = [attempt for task in database.list_tasks(run_id) for attempt in database.list_attempts(task.id)]
        assert all(attempt.status == AttemptStatus.COMPLETED for attempt in attempts)
        assert all(attempt.error is None for attempt in attempts)


def test_killed_owner_releases_lock_and_resume_recovers_each_attempt_once(tmp_path):
    repo = make_repository(tmp_path)
    process, release_event, _, run_id, tasks = _start_blocked_process(repo)
    old_attempt_ids = set()
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        for task in tasks:
            old_attempt_ids.add(database.list_attempts(task.id)[-1].id)

    process.terminate()
    process.join(20)
    if process.is_alive():
        _stop_process(process, release_event)
        pytest.fail("owner process did not terminate")

    resume_process, resume_release, result_queue = _start_blocked_resume_process(repo, run_id)
    after_recovery = _run_rows(repo, run_id)
    try:
        with ScriptoriumService(
            repo,
            runtime_factory=lambda route: FakeAgentRuntime(),
            manuscript_manager=PdfBuildingManuscriptManager(repo),
        ) as service:
            with pytest.raises(StateError, match=f"run {run_id} is already being changed by run resume"):
                asyncio.run(service.resume_run(run_id))
        assert _run_rows(repo, run_id) == after_recovery
    finally:
        _stop_process(resume_process, resume_release)

    assert resume_process.exitcode == 0
    assert result_queue.get(timeout=5) == ("ok", run_id, RunStatus.AWAITING_DECISION.value)
    with ScriptoriumService(repo) as service:
        resumed = service.get_run(run_id)

    assert resumed["run"].status == RunStatus.AWAITING_DECISION
    for item in resumed["tasks"]:
        assert [attempt.status for attempt in item["attempts"]] == [
            AttemptStatus.INTERRUPTED,
            AttemptStatus.COMPLETED,
        ]
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        interruptions = [event for event in database.list_events(run_id) if event.event_type == "attempt.interrupted"]
        assert {event.entity_id for event in interruptions} == old_attempt_ids
        assert len(interruptions) == len(old_attempt_ids)


def test_corrupt_busy_metadata_falls_back_and_stale_file_does_not_block_cancel(tmp_path):
    repo = make_repository(tmp_path)
    run, task, attempt = _create_orphaned_run(repo)
    other_run, _, _ = _create_orphaned_run(repo)
    lock_path = repo / ".scriptorium" / "locks" / f"{run.id}.lock"
    process, release_event = _start_raw_lock_process(lock_path)
    before = _run_rows(repo, run.id)

    try:
        with ScriptoriumService(repo) as service:
            with pytest.raises(
                StateError,
                match=rf"run {run.id} is already being changed \(owner details unavailable\)",
            ):
                service.cancel_run(run.id, "stop")
            assert _run_rows(repo, run.id) == before
            assert (
                service.cancel_run(other_run.id, "other run remains independent")["run"].status == RunStatus.CANCELLED
            )
    finally:
        _stop_process(process, release_event)

    assert process.exitcode == 0
    with ScriptoriumService(repo) as service:
        cancelled = service.cancel_run(run.id, "owner exited")

    assert cancelled["run"].status == RunStatus.CANCELLED
    cancelled_task = next(item for item in cancelled["tasks"] if item["task"].id == task.id)
    assert cancelled_task["task"].status == TaskStatus.CANCELLED
    assert cancelled_task["attempts"][0].id == attempt.id
    assert cancelled_task["attempts"][0].status == AttemptStatus.INTERRUPTED


def test_retry_rejects_a_task_from_another_run_before_orphan_recovery(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    other_run, other_task, _ = _create_orphaned_run(repo)
    before = {
        run.id: _run_rows(repo, run.id),
        other_run.id: _run_rows(repo, other_run.id),
    }

    with ScriptoriumService(repo) as service:
        with pytest.raises(StateError, match=f"task {other_task.id} does not belong to run {run.id}"):
            asyncio.run(service.retry_task(run.id, other_task.id))

    assert _run_rows(repo, run.id) == before[run.id]
    assert _run_rows(repo, other_run.id) == before[other_run.id]


def test_read_only_run_views_do_not_recover_orphaned_attempts(tmp_path):
    repo = make_repository(tmp_path)
    run, task, attempt = _create_orphaned_run(repo)
    before = _run_rows(repo, run.id)

    with ScriptoriumService(repo) as service:
        status = service.get_run(run.id)
        report = service.render_report(run.id, "json")
        gate = service.evaluate_gate(run.id)

    assert status["tasks"][0]["attempts"][0].id == attempt.id
    assert status["tasks"][0]["attempts"][0].status == AttemptStatus.RUNNING
    assert report["run"]["id"] == run.id
    assert gate["run_id"] == run.id
    assert _run_rows(repo, run.id) == before
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        assert database.get_task(task.id).status == TaskStatus.RUNNING


def test_finding_and_patch_mutations_reject_a_live_owner_before_changing_state(tmp_path):
    repo = make_repository(tmp_path)
    run, task, attempt = _create_orphaned_run(repo)
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        finding = database.create_finding(
            Finding(
                run_id=run.id,
                task_id=task.id,
                attempt_id=attempt.id,
                fingerprint="f" * 64,
                role=task.role,
                category="clarity",
                severity=FindingSeverity.MINOR,
                title="Finding",
                claim="Claim",
                evidence=(),
                explanation="Explanation",
                suggested_action="Action",
                confidence=0.9,
            )
        )
        artifact = database.record_artifact(
            Artifact(
                digest="e" * 64,
                relative_path="sha256/ee/" + "e" * 62,
                size=0,
            )
        )
        patch = database.create_patch(
            Patch(
                run_id=run.id,
                base_commit=run.commit_sha,
                diff_digest=artifact.digest,
                summary="Patch",
                edits=(),
                attempt_id=attempt.id,
            )
        )
    lock_path = repo / ".scriptorium" / "locks" / f"{run.id}.lock"
    process, release_event = _start_raw_lock_process(lock_path)
    before = _run_rows(repo, run.id)
    main_before = (repo / "main.tex").read_bytes()

    try:
        with ScriptoriumService(repo) as service:
            operations = (
                lambda: service.decide_finding(finding.id, "reject", "not actionable"),
                lambda: service.decide_patch(patch.id, "reject", "not suitable"),
                lambda: service.apply_patch(patch.id),
            )
            for operation in operations:
                with pytest.raises(StateError, match="owner details unavailable"):
                    operation()
        assert _run_rows(repo, run.id) == before
        assert (repo / "main.tex").read_bytes() == main_before
        with Database(repo / ".scriptorium" / "state.sqlite3") as database:
            assert database.get_finding(finding.id) == finding
            assert database.get_patch(patch.id) == patch
            assert database.list_decisions("finding", finding.id) == []
            assert database.list_decisions("patch", patch.id) == []
    finally:
        _stop_process(process, release_event)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_run_lock_rejects_links_before_writing_owner_metadata(tmp_path, link_kind):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    lock_path = repo / ".scriptorium" / "locks" / f"{run.id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / f"{link_kind}-target.txt"
    target.write_text("must remain intact", encoding="utf-8")
    if link_kind == "symlink":
        lock_path.symlink_to(target)
    else:
        os.link(target, lock_path)
    before = _run_rows(repo, run.id)

    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError):
            service.cancel_run(run.id, "stop")

    assert target.read_text(encoding="utf-8") == "must remain intact"
    assert _run_rows(repo, run.id) == before


def test_run_lock_rejects_a_linked_lock_directory(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    external_locks = tmp_path / "external-locks"
    external_locks.mkdir()
    (repo / ".scriptorium" / "locks").symlink_to(external_locks, target_is_directory=True)
    before = _run_rows(repo, run.id)

    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError):
            service.cancel_run(run.id, "stop")

    assert list(external_locks.iterdir()) == []
    assert _run_rows(repo, run.id) == before


@pytest.mark.parametrize("kind", ["relative", "absolute"])
def test_untrusted_run_id_is_resolved_before_any_lock_path_is_created(tmp_path, kind):
    repo = make_repository(tmp_path)
    value = "../escaped" if kind == "relative" else str(tmp_path / "scriptorium-escaped-run")
    escaped = repo / ".scriptorium" / "escaped.lock"
    absolute = Path(f"{value}.lock")

    with ScriptoriumService(repo) as service:
        with pytest.raises(NotFoundError, match="run not found"):
            asyncio.run(service.resume_run(value))

    assert not escaped.exists()
    if absolute.is_absolute():
        assert not absolute.exists()


@pytest.mark.parametrize("exception", [RuntimeError("failed"), asyncio.CancelledError(), KeyboardInterrupt()])
def test_run_operation_releases_lock_for_all_exit_paths(tmp_path, exception):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo) as service:
        with pytest.raises(type(exception)):
            with service._run_operation("run_test", "run resume"):
                raise exception
        with service._run_operation("run_test", "run retry"):
            pass
