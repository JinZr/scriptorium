import json

import pytest

from scriptorium.artifacts import ArtifactStore
from scriptorium.domain import AgentRole, Artifact, Run, RunStatus, Task
from scriptorium.storage import ConflictError, NotFoundError, RunStore, StorageError

from ._factories import create_completed_attempt, make_run


def test_a_run_is_one_state_file_that_reopens_unchanged(tmp_path) -> None:
    run = make_run()
    with RunStore(tmp_path) as store:
        store.create_run(run)
        task, attempt = create_completed_attempt(store, run)
        stored = store.get_run(run.id)
        task = store.get_task(task.id)
        events = store.list_events(run.id)

    state_file = tmp_path / "runs" / run.id / "state.json"
    document = json.loads(state_file.read_bytes())
    assert list(document) == sorted(
        [
            "run",
            "tasks",
            "external_tasks",
            "attempts",
            "findings",
            "decisions",
            "plans",
            "patches",
            "verifications",
            "events",
        ]
    )
    assert document["run"]["id"] == run.id
    assert [item["id"] for item in document["tasks"]] == [task.id]
    assert sorted(path.name for path in state_file.parent.iterdir()) == ["state.json", "state.lock"]

    with RunStore(tmp_path) as reopened:
        assert reopened.get_run(run.id) == stored
        assert reopened.get_task(task.id) == task
        assert reopened.get_attempt(attempt.id) == attempt
        assert reopened.list_events(run.id) == events


def test_lookups_by_id_search_every_run(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        records = []
        for _ in range(3):
            run = store.create_run(make_run())
            records.append((run, *create_completed_attempt(store, run)))

        for run, task, attempt in records:
            assert store.get_task(task.id).run_id == run.id
            assert store.get_attempt(attempt.id).task_id == task.id
            assert [item.id for item in store.list_attempts(task.id)] == [attempt.id]
            assert store.get_run(run.id).id == run.id
        with pytest.raises(NotFoundError, match="task not found"):
            store.get_task("task_missing")
        with pytest.raises(NotFoundError, match="run not found"):
            store.get_run("run_missing")
        assert store.list_attempts("task_missing") == []
        assert store.list_decisions("finding", "finding_missing") == []


def test_a_run_directory_without_a_state_file_is_not_a_run(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        (tmp_path / "runs" / "run_unfinished" / "snapshot").mkdir(parents=True)

        assert [item.id for item in store.list_runs()] == [run.id]
        assert store.list_run_summaries(None, 10) == (1, [(store.get_run(run.id), {})])
        assert store.list_active_runs(run.commit_sha) == [store.get_run(run.id)]
        assert store.list_tasks("run_unfinished") == []
        with pytest.raises(NotFoundError, match="run not found"):
            store.get_run("run_unfinished")
        with pytest.raises(NotFoundError, match="run not found"):
            store.update_run("run_unfinished", RunStatus.REVIEWING)
        assert not (tmp_path / "runs" / "run_unfinished" / "state.lock").exists()


def test_an_unreadable_state_file_fails_with_its_path(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        state_file = tmp_path / "runs" / run.id / "state.json"
        state_file.write_text('{"run": ', encoding="utf-8")

        for read in (lambda: store.get_run(run.id), store.list_runs):
            with pytest.raises(StorageError, match="run state is unreadable") as caught:
                read()
            assert str(state_file) in str(caught.value)
        with pytest.raises(StorageError, match="run state is unreadable"):
            store.update_run(run.id, RunStatus.REVIEWING)


def test_a_state_file_that_does_not_fit_the_records_fails(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        state_file = tmp_path / "runs" / run.id / "state.json"
        document = json.loads(state_file.read_bytes())

        state_file.write_text(json.dumps({**document, "extra": []}), encoding="utf-8")
        with pytest.raises(StorageError, match="unexpected top-level keys"):
            store.get_run(run.id)
        state_file.write_text(json.dumps({**document, "run": {**document["run"], "unknown": 1}}), encoding="utf-8")
        with pytest.raises(StorageError, match="run state is unreadable"):
            store.get_run(run.id)
        other = tmp_path / "runs" / "run_copied"
        other.mkdir()
        (other / "state.json").write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(StorageError, match="does not match its directory"):
            store.get_run("run_copied")


def test_run_ids_are_single_safe_path_components(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        with pytest.raises(StorageError, match="unsafe run id"):
            store.create_run(Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {}, id="../escape"))
        with pytest.raises(NotFoundError, match="run not found"):
            store.get_run("../../outside")
        assert not (tmp_path / "escape").exists()


def test_a_run_is_created_once(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        run = store.create_run(make_run())
        with pytest.raises(ConflictError, match="run already exists"):
            store.create_run(run)
        assert [event.event_type for event in store.list_events(run.id)] == ["run.created"]


def test_a_run_records_its_brief_artifact_digest(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        artifact = ArtifactStore(tmp_path / "artifacts").put_text("{}", "application/json")
        store.record_artifact(artifact)
        run = Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {}, brief_digest=artifact.digest)
        store.create_run(run)
        plain = Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {})
        store.create_run(plain)

        assert store.get_run(run.id).brief_digest == artifact.digest
        assert store.get_run(plain.id).brief_digest is None
        unrecorded = Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {}, brief_digest="e" * 64)
        with pytest.raises(StorageError, match="artifact is not recorded"):
            store.create_run(unrecorded)
        assert not (tmp_path / "runs" / unrecorded.id / "state.json").exists()


def test_artifact_records_are_immutable_and_shared_by_runs(tmp_path) -> None:
    digest = "e" * 64
    artifact = Artifact(digest=digest, relative_path=f"sha256/ee/{'e' * 62}", size=3, media_type="text/plain")
    with RunStore(tmp_path) as store:
        first = store.record_artifact(artifact)
        again = store.record_artifact(Artifact(digest, artifact.relative_path, 3, "application/json"))

        assert again == first
        assert store.get_artifact(digest) == first
        with pytest.raises(ConflictError, match="artifact metadata conflicts"):
            store.record_artifact(Artifact(digest, artifact.relative_path, 4))
        with pytest.raises(NotFoundError, match="artifact not found"):
            store.get_artifact("f" * 64)
        with pytest.raises(NotFoundError, match="artifact not found"):
            store.get_artifact("not-a-digest")
    assert (tmp_path / "artifacts" / "sha256" / "ee" / f"{'e' * 62}.json").is_file()
    with RunStore(tmp_path) as reopened:
        assert reopened.get_artifact(digest) == first


def test_a_task_names_an_existing_run(tmp_path) -> None:
    with RunStore(tmp_path) as store:
        with pytest.raises(NotFoundError, match="run not found"):
            store.create_task(Task(run_id="run_missing", stage="review", role=AgentRole.COPYEDIT, input_digest="d"))
        assert store.list_runs() == []
