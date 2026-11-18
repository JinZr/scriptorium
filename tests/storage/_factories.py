from scriptorium.domain import AgentRole, Attempt, AttemptStatus, Run, Task
from scriptorium.storage import RunStore


def make_run() -> Run:
    return Run(
        repository="/tmp/paper",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        profile="full",
        config_digest="c" * 64,
        frozen_config={"roles": {"copyedit": "primary"}},
    )


def create_completed_attempt(store: RunStore, run: Run) -> tuple[Task, Attempt]:
    task = store.create_task(
        Task(
            run_id=run.id,
            stage="review",
            role=AgentRole.COPYEDIT,
            input_digest="d" * 64,
        )
    )
    attempt = store.create_attempt(
        Attempt(
            task_id=task.id,
            ordinal=1,
            model="test-model",
        )
    )
    store.finish_attempt(
        attempt.id,
        AttemptStatus.COMPLETED,
        thread_id="thread-1",
    )
    return task, store.get_attempt(attempt.id)
