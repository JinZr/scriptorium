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

Doctor checks the repository, manuscript, LaTeX tools, SQLite, selected profile, routes, and budget pricing prerequisites. It includes the revision and verification routes, then requires each referenced native SDK at the exact pinned version that a new run will freeze. Antigravity also requires `GEMINI_API_KEY`. Claude Code login or API authentication is intentionally left to an explicit live smoke test.

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

Approval allows independent verification. A rejection reason becomes feedback for a Scribe continuation. Each new patch records the attempt that generated it, so rejection resumes that attempt's exact route, runtime, provider, model, and opaque native session ID rather than guessing the latest revision thread. A historical patch without an attempt ID explicitly starts a new session on the default revision route.

Verification failure returns the run to `awaiting_patch_approval`; it does not trigger an automatic revision loop.

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

Resume marks leftover `running` attempts as `interrupted`. A completed task with an unchanged input digest is not repeated. Failed or interrupted tasks append a new attempt ordinal.

The database field named `thread_id` stores an opaque native session or conversation ID. Scriptorium resumes it only when the semantic task and the frozen runtime, exact SDK version, provider, and model all match. A same-route retry, including an explicit override naming that same frozen route, may continue the native session. When `--route` changes the task to a different named route, Scriptorium always starts a new session, even if that route selects the same model. Runtime-native state lives in a stable run directory rather than the disposable task workspace; Antigravity keeps separate `save/` and `app/` directories.

If an attempt has a recorded session ID but the corresponding native state is missing, resume fails explicitly. It never hides the loss by creating a new conversation. Runtime or model version changes are recorded on a new attempt and never rewritten into earlier history.

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

## Live native-harness smoke tests

Live tests are excluded from ordinary test runs. Each enabled test performs a small structured-output turn and a resume, checks the opaque session ID, normalized usage, and NDJSON trace, and confirms that the frozen workspace digest did not change.

Claude Code uses its native login or API configuration:

```bash
SCRIPTORIUM_LIVE_CLAUDE=1 \
SCRIPTORIUM_LIVE_CLAUDE_MODEL=MODEL_NAME \
python -m pytest -m live_harness tests/live/test_native_harnesses.py
```

Antigravity requires its supported API key:

```bash
SCRIPTORIUM_LIVE_ANTIGRAVITY=1 \
SCRIPTORIUM_LIVE_ANTIGRAVITY_MODEL=MODEL_NAME \
GEMINI_API_KEY=SECRET \
python -m pytest -m live_harness tests/live/test_native_harnesses.py
```

Setting a model name alone does not opt in. Treat these as paid, credentialed integration checks; regular CI must not set either `SCRIPTORIUM_LIVE_*` switch.
