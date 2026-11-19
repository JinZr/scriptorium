from scriptorium.domain import AgentRole, Artifact, Attempt, AttemptStatus, Plan, Run, RunStatus, Task
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


def approve_plan(store: RunStore, run: Run, digest: str = "7" * 64) -> Plan:
    """Bring the run to planning and record an approved one-assignment plan, which moves it to writing."""
    for status in (RunStatus.REVIEWING, RunStatus.AWAITING_DECISION, RunStatus.PLANNING):
        if store.get_run(run.id).status != RunStatus.PLANNING:
            store.update_run(run.id, status)
    artifact = store.add_artifact(
        Artifact(digest=digest, relative_path=f"sha256/{digest[:2]}/{digest[2:]}", size=1, media_type="text/markdown")
    )
    plan = Plan(
        run_id=run.id,
        digest=digest,
        plan_artifact_digest=artifact.digest,
        assignments=(
            {
                "id": "results",
                "artifact_digest": artifact.digest,
                "targets": [{"path": "main.tex", "start_line": 1, "end_line": 2}],
                "finding_ids": [],
            },
        ),
    )
    store.approve_plan(plan, "The author approved the plan.")
    return plan
