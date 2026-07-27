from scriptorium.domain import AgentRole, Attempt, AttemptStatus, Run, Task
from scriptorium.storage import Database


def make_run() -> Run:
    return Run(
        repository="/tmp/paper",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        profile="full",
        config_digest="c" * 64,
        frozen_config={"roles": {"copyedit": "primary"}},
        budget_usd=5.0,
    )


def create_completed_attempt(database: Database, run: Run) -> tuple[Task, Attempt]:
    task = database.create_task(
        Task(
            run_id=run.id,
            stage="review",
            role=AgentRole.COPYEDIT,
            route="primary",
            input_digest="d" * 64,
        )
    )
    attempt = database.create_attempt(
        Attempt(
            task_id=task.id,
            ordinal=1,
            runtime_name="codex",
            runtime_version="0.144.4",
            model="test-model",
            model_provider="openai",
        )
    )
    database.finish_attempt(
        attempt.id,
        AttemptStatus.COMPLETED,
        thread_id="thread-1",
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=30,
        reasoning_tokens=10,
        estimated_cost_usd=0.25,
        duration_ms=200,
    )
    return task, database.get_attempt(attempt.id)
