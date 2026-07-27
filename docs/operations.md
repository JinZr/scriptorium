# Operations and recovery

Run Scriptorium from the root of the Git-managed LaTeX repository. Use `--json` for automation and the Codex Skill.

## Initialize and diagnose

```bash
scriptorium init . --main main.tex --engine pdflatex
```

Initialization creates the project-local `.scriptorium/` state directory and ignores it in Git. After tracking `scriptorium.toml` and configuring local routes, run:

```bash
scriptorium --json doctor --profile full --budget-usd 10
```

Doctor checks the repository, manuscript, LaTeX tools, Codex SDK, SQLite, selected profile, routes, and budget pricing prerequisites.

## Start and inspect a run

```bash
scriptorium --json run start --revision HEAD --profile full --budget-usd 10
scriptorium --json run status RUN_ID
scriptorium --json finding list RUN_ID
scriptorium --json run report RUN_ID --format json
```

The revision is resolved and frozen before review. Uncommitted changes do not enter the snapshot or agent bundle. Independent review tasks run concurrently up to `max_concurrency`; every stage result is persisted before scheduling the next stage.

## Record finding decisions

Every finding requires one decision and a non-empty human reason:

```bash
scriptorium --json finding show FINDING_ID
scriptorium --json finding decide FINDING_ID --confirm --reason "TEXT"
scriptorium --json finding decide FINDING_ID --reject --reason "TEXT"
scriptorium --json finding decide FINDING_ID --waive --reason "TEXT"
```

Unresolved blocker or major findings prevent revision and gate passage. Decisions are append-only audit records.

After all findings have decisions, advance the run:

```bash
scriptorium --json run resume RUN_ID
```

## Review and decide a patch

Confirmed findings are sent to the Scribe. Scriptorium validates source membership and digests, exact `before` text, line ranges, and non-overlap, then creates and compiles a patched snapshot without changing the author worktree.

```bash
scriptorium --json patch show PATCH_ID
scriptorium --json patch decide PATCH_ID --approve --reason "TEXT"
scriptorium --json patch decide PATCH_ID --reject --reason "TEXT"
```

Approval allows independent verification. A rejection reason becomes feedback for a Scribe continuation. Verification failure returns the run to `awaiting_patch_approval`; it does not trigger an automatic revision loop.

Process either patch decision explicitly:

```bash
scriptorium --json run resume RUN_ID
```

## Resume, retry, and cancel

```bash
scriptorium --json run status RUN_ID
scriptorium --json run resume RUN_ID
scriptorium --json run retry RUN_ID --task TASK_ID
scriptorium --json run retry RUN_ID --task TASK_ID --route ROUTE
scriptorium --json run cancel RUN_ID --reason "TEXT"
```

Resume marks leftover `running` attempts as `interrupted`. A completed task with an unchanged input digest is not repeated. Failed or interrupted tasks append a new attempt ordinal. Scriptorium resumes a thread only when a persisted thread ID belongs to the same semantic task; otherwise it starts a new thread. Runtime or model version changes are recorded on the new attempt and never rewritten into earlier history.

If the budget is exhausted, the run pauses in `waiting_budget`. Inspect the report, then either retry the task with an explicitly selected frozen zero-cost route or start a new run with a new budget. Scriptorium never changes the frozen budget or selects a fallback model.

## Apply and evaluate the gate

```bash
scriptorium --json patch apply PATCH_ID
scriptorium --json run gate RUN_ID
scriptorium --json run report RUN_ID --format json
```

After verification, `patch apply` compares the current worktree digests of affected files with the frozen source digests. A mismatch marks the patch stale and stops. There is no automatic three-way merge. `run gate` exits `0` when the release gate passes and `1` when a valid domain condition remains unmet; for runs requiring changes, the gate includes successful patch application and the resulting completed run state.

Patch application modifies only the approved files. It does not switch branches, commit, or push.

## Infrastructure failures

Exit code `3` identifies Git, LaTeX, SQLite, or AgentRuntime infrastructure failure. Preserve `.scriptorium/` and inspect the JSON error plus run report. Do not manually edit the database, artifacts, snapshots, bundles, or patches. Repair the external prerequisite, then use `run resume` or an explicit task retry.
