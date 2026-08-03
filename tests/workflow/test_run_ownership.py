import asyncio
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

import pytest

from scriptorium.artifacts import ArtifactStore
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
from scriptorium.runtime.contained import ContainedAgentRuntime
from scriptorium.schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    evidence_anchor_contract_content,
    evidence_anchor_contract_digest,
)
from scriptorium.service import ScriptoriumService
from scriptorium.storage import Database

from ._support import FakeAgentRuntime, PdfBuildingManuscriptManager, make_repository

_CONTAINED_BLOCKING_FACTORY = (
    f"{(Path(__file__).parents[1] / 'runtime' / '_contained_fakes.py').resolve()}:make_blocking_runtime"
)


class BlockingAgentRuntime(FakeAgentRuntime):
    def __init__(self, ready_queue, release_event):
        super().__init__()
        self.ready_queue = ready_queue
        self.release_event = release_event

    async def run_agent(self, task, role, workspace, schema, session_dir, on_session_started=None):
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


def _run_contained_start(repo_path, result_queue):
    repo = Path(repo_path)
    try:
        with ScriptoriumService(
            repo,
            runtime_factory=lambda route: ContainedAgentRuntime(
                route,
                repo,
                _worker_runtime_factory=_CONTAINED_BLOCKING_FACTORY,
            ),
            manuscript_manager=PdfBuildingManuscriptManager(repo),
        ) as service:
            view = asyncio.run(service.start_run("HEAD", "quick", None))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("ok", view["run"].id, view["run"].status.value))


def _run_contained_resume(repo_path, run_id, result_queue):
    repo = Path(repo_path)
    try:
        with ScriptoriumService(
            repo,
            runtime_factory=lambda route: ContainedAgentRuntime(
                route,
                repo,
                _worker_runtime_factory=_CONTAINED_BLOCKING_FACTORY,
            ),
            manuscript_manager=PdfBuildingManuscriptManager(repo),
        ) as service:
            view = asyncio.run(service.resume_run(run_id))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("ok", view["run"].id, view["run"].status.value))


def _run_cancel(repo_path, run_id, reason, result_queue):
    try:
        with ScriptoriumService(Path(repo_path)) as service:
            view = service.cancel_run(run_id, reason)
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("ok", view["run"].status.value))


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


def _create_orphaned_run(repo, *, legacy=False):
    prompt = "frozen prompt"
    prompt_record = {
        "digest": ArtifactStore.digest_bytes(prompt.encode("utf-8")),
        "content": prompt,
    }
    frozen_config = {"profile_roles": [AgentRole.CONSISTENCY.value]}
    if not legacy:
        frozen_config["evidence_anchor_contract"] = {
            "digest": evidence_anchor_contract_digest(DEFAULT_EVIDENCE_ANCHOR_CONTRACT),
            "content": evidence_anchor_contract_content(DEFAULT_EVIDENCE_ANCHOR_CONTRACT),
            "prompt_templates": {
                AgentRole.CONSISTENCY.value: prompt_record,
                AgentRole.REVISION.value: prompt_record,
                AgentRole.VERIFICATION.value: prompt_record,
            },
        }
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        run = database.create_run(
            Run(
                repository=str(repo),
                commit_sha="a" * 40,
                tree_sha="b" * 40,
                profile="quick",
                config_digest="c" * 64,
                frozen_config=frozen_config,
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


def test_legacy_run_rejects_resume_and_retry_before_orphan_recovery(tmp_path):
    repo = make_repository(tmp_path)
    run, task, attempt = _create_orphaned_run(repo, legacy=True)
    runtime = FakeAgentRuntime()
    before = _run_rows(repo, run.id)

    with ScriptoriumService(
        repo,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PdfBuildingManuscriptManager(repo),
    ) as service:
        pending = service._publish_cancel_request(run.id, "pending legacy cancel")
        control_dir = repo / ".scriptorium" / "control" / "cancel"
        control_before = {path.name: path.read_bytes() for path in control_dir.glob(f"{run.id}.*.json")}
        artifacts_before = sorted(
            path.relative_to(service.artifacts.root).as_posix()
            for path in service.artifacts.root.rglob("*")
            if path.is_file()
        )
        assert service.get_run(run.id)["run"].status == RunStatus.REVIEWING
        assert service.evaluate_gate(run.id)["passed"] is False
        assert "reviewing" in service.render_report(run.id, "markdown")
        for operation in (
            lambda: asyncio.run(service.resume_run(run.id)),
            lambda: asyncio.run(service.retry_task(run.id, task.id)),
        ):
            with pytest.raises(
                InfrastructureError,
                match="predates the frozen evidence anchor contract; start a new run",
            ):
                operation()
            assert _run_rows(repo, run.id) == before
            assert runtime.run_calls == {}
            assert runtime.resume_calls == []
            assert {path.name: path.read_bytes() for path in control_dir.glob(f"{run.id}.*.json")} == control_before
            assert (
                sorted(
                    path.relative_to(service.artifacts.root).as_posix()
                    for path in service.artifacts.root.rglob("*")
                    if path.is_file()
                )
                == artifacts_before
            )

        cancelled = service.cancel_run(run.id, "stop legacy run")
        assert cancelled["run"].status == RunStatus.CANCELLED
        assert service.database.get_attempt(attempt.id).status == AttemptStatus.INTERRUPTED
        cancellation = next(
            event for event in service.database.list_events(run.id) if event.event_type == "run.cancelled"
        )
        assert cancellation.payload["request_id"] == pending["request_id"]
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.CANCELLED
        with pytest.raises(StateError, match="review tasks cannot be retried while run is cancelled"):
            asyncio.run(service.retry_task(run.id, task.id))


def test_live_start_owner_rejects_other_work_but_honors_durable_cancel(tmp_path):
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

            cancelled = service.cancel_run(run_id, "stop")
            assert cancelled["run"].status == RunStatus.CANCELLED
    finally:
        _stop_process(process, release_event)

    assert process.exitcode == 0
    result = result_queue.get(timeout=5)
    assert result[:2] == ("error", "CancelledError")
    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        attempts = [attempt for task in database.list_tasks(run_id) for attempt in database.list_attempts(task.id)]
        assert all(attempt.status == AttemptStatus.INTERRUPTED for attempt in attempts)
        cancellations = [event for event in database.list_events(run_id) if event.event_type == "run.cancelled"]
        assert len(cancellations) == 1
        assert cancellations[0].payload["reason"] == "stop"
        assert cancellations[0].payload["request_id"].startswith("cancel_")


def test_sigint_interrupts_parallel_contained_attempts_and_resume_appends_ordinals(tmp_path):
    repo = make_repository(tmp_path)
    context = multiprocessing.get_context("spawn")

    def wait_for_running_ordinal(expected_ordinal, run_id=None):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with Database(repo / ".scriptorium" / "state.sqlite3") as database:
                runs = database.list_runs()
                if runs:
                    current_run_id = run_id or runs[0].id
                    attempts = [
                        database.list_attempts(task.id)[-1]
                        for task in database.list_tasks(current_run_id)
                        if database.list_attempts(task.id)
                    ]
                    if len(attempts) == 2 and all(
                        attempt.ordinal == expected_ordinal and attempt.status == AttemptStatus.RUNNING
                        for attempt in attempts
                    ):
                        return current_run_id
            time.sleep(0.02)
        pytest.fail(f"contained attempts did not reach running ordinal {expected_ordinal}")

    def wait_for_worker_pids(previous=frozenset()):
        deadline = time.monotonic() + 10
        sessions = repo / ".scriptorium" / "runs" / run_id / "sessions"
        while time.monotonic() < deadline:
            states = list(sessions.glob("*/worker-state.json"))
            if len(states) == 2:
                pids = {json.loads(path.read_text(encoding="utf-8"))["pid"] for path in states}
                if len(pids) == 2 and pids.isdisjoint(previous):
                    return pids
            time.sleep(0.02)
        pytest.fail("contained workers did not start their native runtimes")

    first_results = context.Queue()
    first = context.Process(target=_run_contained_start, args=(str(repo), first_results))
    first.start()
    try:
        run_id = wait_for_running_ordinal(1)
        first_worker_pids = wait_for_worker_pids()
        os.kill(first.pid, signal.SIGINT)
        first.join(30)
        assert not first.is_alive() and first.exitcode == 0
        assert first_results.get(timeout=5)[0:2] in {
            ("error", "KeyboardInterrupt"),
            ("error", "CancelledError"),
        }
    finally:
        if first.is_alive():
            first.kill()
            first.join(10)

    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        assert database.get_run(run_id).status == RunStatus.REVIEWING
        first_attempts = [database.list_attempts(task.id) for task in database.list_tasks(run_id)]
        assert all(
            [attempt.status for attempt in attempts] == [AttemptStatus.INTERRUPTED] for attempts in first_attempts
        )
        assert [event for event in database.list_events(run_id) if event.event_type == "run.cancelled"] == []
    cancelled_markers = list((repo / ".scriptorium" / "runs" / run_id / "sessions").glob("*/cancelled.json"))
    assert len(cancelled_markers) == 2

    resumed_results = context.Queue()
    resumed = context.Process(target=_run_contained_resume, args=(str(repo), run_id, resumed_results))
    resumed.start()
    try:
        wait_for_running_ordinal(2, run_id)
        wait_for_worker_pids(first_worker_pids)
        os.kill(resumed.pid, signal.SIGINT)
        resumed.join(30)
        assert not resumed.is_alive() and resumed.exitcode == 0
        assert resumed_results.get(timeout=5)[0:2] in {
            ("error", "KeyboardInterrupt"),
            ("error", "CancelledError"),
        }
    finally:
        if resumed.is_alive():
            resumed.kill()
            resumed.join(10)

    with Database(repo / ".scriptorium" / "state.sqlite3") as database:
        assert database.get_run(run_id).status == RunStatus.REVIEWING
        for task in database.list_tasks(run_id):
            attempts = database.list_attempts(task.id)
            assert [attempt.ordinal for attempt in attempts] == [1, 2]
            assert [attempt.status for attempt in attempts] == [
                AttemptStatus.INTERRUPTED,
                AttemptStatus.INTERRUPTED,
            ]


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


def test_corrupt_busy_metadata_times_out_with_a_durable_cancel_request(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, task, attempt = _create_orphaned_run(repo)
    other_run, _, _ = _create_orphaned_run(repo)
    lock_path = repo / ".scriptorium" / "locks" / f"{run.id}.lock"
    process, release_event = _start_raw_lock_process(lock_path)
    before = _run_rows(repo, run.id)
    monkeypatch.setattr("scriptorium.service._CANCEL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr("scriptorium.service._CANCEL_POLL_SECONDS", 0.01)

    try:
        with ScriptoriumService(repo) as service:
            with pytest.raises(StateError, match=rf"cancellation request cancel_.* remains pending for run {run.id}"):
                service.cancel_run(run.id, "stop")
            assert _run_rows(repo, run.id) == before
            requests = list((repo / ".scriptorium" / "control" / "cancel").glob(f"{run.id}.*.json"))
            assert len(requests) == 1
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


def test_cancel_deadline_includes_main_lock_and_provider_cleanup_wait(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    run_lock = repo / ".scriptorium" / "locks" / f"{run.id}.lock"
    process, release_event = _start_raw_lock_process(run_lock)
    provider_lock = repo / ".scriptorium" / "locks" / f"{run.id}.providers.lock"
    provider_descriptor = os.open(provider_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(provider_descriptor, fcntl.LOCK_SH)
    monkeypatch.setattr("scriptorium.service._CANCEL_WAIT_SECONDS", 0.15)
    monkeypatch.setattr("scriptorium.service._CANCEL_POLL_SECONDS", 0.01)
    release_timer = threading.Timer(0.08, release_event.set)
    release_timer.start()
    started = time.monotonic()

    try:
        with ScriptoriumService(repo) as service:
            with pytest.raises(StateError, match=rf"cancellation request cancel_.* remains pending for run {run.id}"):
                service.cancel_run(run.id, "stop")
        elapsed = time.monotonic() - started

        assert 0.12 <= elapsed < 0.4
        requests = list((repo / ".scriptorium" / "control" / "cancel").glob(f"{run.id}.*.json"))
        assert len(requests) == 1
    finally:
        release_timer.cancel()
        os.close(provider_descriptor)
        _stop_process(process, release_event)


def test_concurrent_cancel_requesters_share_one_terminal_result(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    run_lock = repo / ".scriptorium" / "locks" / f"{run.id}.lock"
    owner, release_owner = _start_raw_lock_process(run_lock)
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    requesters = [
        context.Process(
            target=_run_cancel,
            args=(str(repo), run.id, reason, result_queue),
        )
        for reason in ("first requester", "second requester")
    ]
    for requester in requesters:
        requester.start()

    try:
        cancel_dir = repo / ".scriptorium" / "control" / "cancel"
        deadline = time.monotonic() + 10
        while len(list(cancel_dir.glob(f"{run.id}.*.json"))) < 2:
            if time.monotonic() >= deadline:
                pytest.fail("concurrent cancellation requests were not published")
            time.sleep(0.02)
        release_owner.set()
        results = [result_queue.get(timeout=20), result_queue.get(timeout=20)]
        for requester in requesters:
            requester.join(20)

        assert results == [("ok", RunStatus.CANCELLED.value), ("ok", RunStatus.CANCELLED.value)]
        assert all(not requester.is_alive() and requester.exitcode == 0 for requester in requesters)
        with Database(repo / ".scriptorium" / "state.sqlite3") as database:
            cancellations = [event for event in database.list_events(run.id) if event.event_type == "run.cancelled"]
            assert len(cancellations) == 1
        assert list(cancel_dir.glob(f"{run.id}.*.json")) == []
    finally:
        release_owner.set()
        owner.join(20)
        if owner.is_alive():
            owner.terminate()
            owner.join(10)
        for requester in requesters:
            if requester.is_alive():
                requester.terminate()
                requester.join(10)


def test_terminal_run_wins_when_cancel_watcher_observes_a_late_request(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, _, attempt = _create_orphaned_run(repo)

    with ScriptoriumService(repo) as service:
        terminal_committed = asyncio.Event()

        async def operation():
            service.database.finish_attempt(attempt.id, AttemptStatus.FAILED, error="provider failed")
            service.database.update_run(run.id, RunStatus.FAILED, "provider failed")
            terminal_committed.set()
            await asyncio.Event().wait()

        async def wait_for_terminal_request(run_id):
            await terminal_committed.wait()
            return service._publish_cancel_request(run_id, "too late")

        monkeypatch.setattr(service, "_wait_for_cancel_request", wait_for_terminal_request)

        async def exercise():
            with service._run_operation(run.id, "run resume"):
                return await service._run_with_cancel_watcher(run.id, operation())

        view = asyncio.run(exercise())

        assert view["run"].status == RunStatus.FAILED
        assert [event for event in service.database.list_events(run.id) if event.event_type == "run.cancelled"] == []
    assert list((repo / ".scriptorium" / "control" / "cancel").glob(f"{run.id}.*.json")) == []


def test_resume_clears_a_terminal_cancel_request_without_claiming_the_run_was_cancelled(tmp_path):
    repo = make_repository(tmp_path)
    run, _, attempt = _create_orphaned_run(repo)

    with ScriptoriumService(repo) as service:
        service.database.finish_attempt(attempt.id, AttemptStatus.FAILED, error="provider failed")
        service.database.update_run(run.id, RunStatus.FAILED, "provider failed")
        request = service._publish_cancel_request(run.id, "too late")

        with pytest.raises(
            StateError,
            match=rf"cannot be resumed while failed; cancellation request {request['request_id']} was cleared",
        ):
            asyncio.run(service.resume_run(run.id))

        assert service.database.get_run(run.id).status == RunStatus.FAILED
        assert [event for event in service.database.list_events(run.id) if event.event_type == "run.cancelled"] == []
    assert list((repo / ".scriptorium" / "control" / "cancel").glob(f"{run.id}.*.json")) == []


def test_cancel_timeout_returns_success_if_the_owner_already_committed_cancellation(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    results = []
    monkeypatch.setattr("scriptorium.service._CANCEL_WAIT_SECONDS", 0.1)
    monkeypatch.setattr("scriptorium.service._CANCEL_POLL_SECONDS", 0.01)

    def request_cancel():
        with ScriptoriumService(repo) as requester:
            results.append(requester.cancel_run(run.id, "stop"))

    requester_thread = threading.Thread(target=request_cancel)
    with ScriptoriumService(repo) as owner:
        with owner._run_operation(run.id, "run resume"):
            requester_thread.start()
            cancel_dir = repo / ".scriptorium" / "control" / "cancel"
            deadline = time.monotonic() + 5
            while not list(cancel_dir.glob(f"{run.id}.*.json")):
                if time.monotonic() >= deadline:
                    pytest.fail("cancellation request was not published")
                time.sleep(0.01)
            request = owner._cancel_requests(run.id)[0]
            owner.database.cancel_run(run.id, request["reason"], request["request_id"])
            owner._remove_cancel_requests(run.id)
            time.sleep(0.15)
        requester_thread.join(5)

        assert not requester_thread.is_alive()
        assert len(results) == 1
        assert results[0]["run"].status == RunStatus.CANCELLED


def test_cancel_timeout_keeps_a_request_pending_until_terminal_cleanup_is_serialized(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    errors = []
    monkeypatch.setattr("scriptorium.service._CANCEL_WAIT_SECONDS", 0.1)
    monkeypatch.setattr("scriptorium.service._CANCEL_POLL_SECONDS", 0.01)

    def request_cancel():
        with ScriptoriumService(repo) as requester:
            try:
                requester.cancel_run(run.id, "stop")
            except Exception as exc:
                errors.append(exc)

    requester_thread = threading.Thread(target=request_cancel)
    with ScriptoriumService(repo) as owner:
        with owner._run_operation(run.id, "run resume"):
            requester_thread.start()
            cancel_dir = repo / ".scriptorium" / "control" / "cancel"
            deadline = time.monotonic() + 5
            while not list(cancel_dir.glob(f"{run.id}.*.json")):
                if time.monotonic() >= deadline:
                    pytest.fail("cancellation request was not published")
                time.sleep(0.01)
            request = owner._cancel_requests(run.id)[0]
            owner.database.cancel_run(run.id, request["reason"], request["request_id"])
            time.sleep(0.15)
        requester_thread.join(5)

        assert not requester_thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], StateError)
        assert request["request_id"] in str(errors[0])
        assert "remains pending" in str(errors[0])
        assert (cancel_dir / request["_name"]).is_file()


def test_cancelled_operation_cleanup_error_does_not_override_interrupted_semantics(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)

    with ScriptoriumService(repo) as service:
        started = asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("operation cleanup failed")

        async def wait_for_request(run_id):
            await started.wait()
            return service._publish_cancel_request(run_id, "stop")

        monkeypatch.setattr(service, "_wait_for_cancel_request", wait_for_request)

        async def exercise():
            with service._run_operation(run.id, "run resume"):
                await service._run_with_cancel_watcher(run.id, operation())

        with pytest.raises(asyncio.CancelledError) as caught:
            asyncio.run(exercise())

        assert isinstance(caught.value.__cause__, RuntimeError)
        assert service.database.get_run(run.id).status == RunStatus.CANCELLED
        cancellations = [event for event in service.database.list_events(run.id) if event.event_type == "run.cancelled"]
        assert len(cancellations) == 1


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


def test_read_only_views_leave_a_durable_cancel_request_pending(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)

    with ScriptoriumService(repo) as service:
        request = service._publish_cancel_request(run.id, "stop later")
        before = _run_rows(repo, run.id)

        assert service.get_run(run.id)["run"].status == RunStatus.REVIEWING
        assert service.render_report(run.id, "json")["run"]["status"] == RunStatus.REVIEWING.value
        assert service.evaluate_gate(run.id)["run_id"] == run.id

    assert _run_rows(repo, run.id) == before
    request_path = repo / ".scriptorium" / "control" / "cancel" / f"{run.id}.{request['request_id']}.json"
    assert request_path.is_file()


def test_cancel_request_scan_tolerates_concurrent_owner_cleanup(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)

    with ScriptoriumService(repo) as service:
        request = service._publish_cancel_request(run.id, "stop")
        original_open = os.open
        removed = False

        def open_after_owner_cleanup(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal removed
            if path == f"{run.id}.{request['request_id']}.json" and not removed:
                os.unlink(path, dir_fd=dir_fd)
                removed = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr("scriptorium.service.os.open", open_after_owner_cleanup)

        assert service._cancel_requests(run.id) == []
        assert removed is True


def test_next_mutation_replays_pending_cancel_once_and_removes_all_requests(tmp_path):
    repo = make_repository(tmp_path)
    run, task, attempt = _create_orphaned_run(repo)

    with ScriptoriumService(repo) as service:
        first = service._publish_cancel_request(run.id, "first reason")
        service._publish_cancel_request(run.id, "second reason")

        with pytest.raises(StateError, match="was cancelled by request"):
            asyncio.run(service.resume_run(run.id))

        view = service.get_run(run.id)
        assert view["run"].status == RunStatus.CANCELLED
        assert view["tasks"][0]["task"].status == TaskStatus.CANCELLED
        assert view["tasks"][0]["attempts"][0].id == attempt.id
        assert view["tasks"][0]["attempts"][0].status == AttemptStatus.INTERRUPTED
        cancellations = [event for event in service.database.list_events(run.id) if event.event_type == "run.cancelled"]
        assert len(cancellations) == 1
        assert cancellations[0].payload == {
            "reason": "first reason",
            "request_id": first["request_id"],
        }

    assert list((repo / ".scriptorium" / "control" / "cancel").glob(f"{run.id}.*.json")) == []
    assert task.id == view["tasks"][0]["task"].id


def test_cancel_replays_request_left_after_the_cancel_commit(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)

    with ScriptoriumService(repo) as service:
        request = service._publish_cancel_request(run.id, "stop")
        service.database.cancel_run(run.id, "stop", request["request_id"])

        replayed = service.cancel_run(run.id, "caller retry")

        assert replayed["run"].status == RunStatus.CANCELLED
        cancellations = [event for event in service.database.list_events(run.id) if event.event_type == "run.cancelled"]
        assert len(cancellations) == 1

    assert list((repo / ".scriptorium" / "control" / "cancel").glob(f"{run.id}.*.json")) == []


def test_cancel_request_directory_rejects_a_symlink(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    external = tmp_path / "external-control"
    external.mkdir()
    (repo / ".scriptorium" / "control").symlink_to(external, target_is_directory=True)

    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError, match="cancellation request directory"):
            service.cancel_run(run.id, "stop")

    assert list(external.iterdir()) == []


def test_pending_cancel_rejects_a_hard_linked_request_file(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    request_id = "cancel_linked"
    request = {
        "version": 1,
        "request_id": request_id,
        "run_id": run.id,
        "reason": "stop",
        "requested_at": "2026-08-03T00:00:00+00:00",
    }
    control = repo / ".scriptorium" / "control" / "cancel"
    control.mkdir(parents=True)
    target = tmp_path / "linked-request.json"
    target.write_text(json.dumps(request), encoding="utf-8")
    os.link(target, control / f"{run.id}.{request_id}.json")
    before = _run_rows(repo, run.id)

    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError, match="unsafe cancellation request file"):
            asyncio.run(service.resume_run(run.id))

    assert _run_rows(repo, run.id) == before


def test_cancel_watcher_failure_stops_the_owned_operation_before_propagating(tmp_path, monkeypatch):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    control = repo / ".scriptorium" / "control" / "cancel"
    control.mkdir(parents=True)
    (control / f"{run.id}.cancel_broken.json").write_text("{", encoding="utf-8")

    with ScriptoriumService(repo) as service:
        started = asyncio.Event()
        stopped = asyncio.Event()
        original_watcher = service._wait_for_cancel_request

        async def wait_after_operation_starts(run_id):
            await started.wait()
            return await original_watcher(run_id)

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(service, "_wait_for_cancel_request", wait_after_operation_starts)

        async def exercise():
            with pytest.raises(InfrastructureError, match="invalid cancellation request"):
                await service._run_with_cancel_watcher(run.id, operation())
            assert stopped.is_set()

        asyncio.run(exercise())


def test_overlong_cancel_reason_is_rejected_without_publishing_a_request(tmp_path):
    repo = make_repository(tmp_path)
    run, _, _ = _create_orphaned_run(repo)
    before = _run_rows(repo, run.id)

    with ScriptoriumService(repo) as service:
        with pytest.raises(StateError, match="cancellation reason is too long"):
            service.cancel_run(run.id, "x" * 70000)

    cancel_dir = repo / ".scriptorium" / "control" / "cancel"
    assert not cancel_dir.exists() or list(cancel_dir.iterdir()) == []
    assert _run_rows(repo, run.id) == before


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
