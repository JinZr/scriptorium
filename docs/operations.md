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

Doctor checks the repository, manuscript, LaTeX tools, SQLite, selected profile, routes, and budget pricing prerequisites. It includes the visual-transcription, revision, and verification routes, then requires each referenced native SDK at the exact pinned version that a new run will freeze. Antigravity also requires `GEMINI_API_KEY`. Claude Code login or API authentication is intentionally left to an explicit live smoke test.

## Start and inspect a run

```bash
scriptorium --json run start --revision HEAD --profile full --budget-usd 10
scriptorium --json run status RUN_ID
scriptorium --json finding list RUN_ID
scriptorium --json run report RUN_ID --format json
```

The revision is resolved and frozen before review. Uncommitted changes do not enter the snapshot or agent bundle. If the PDF contains raster images, one separately routed `visual_transcription` task transcribes every affected rendered page before any reviewer starts. A patched PDF is checked independently before the Verifier starts. These tasks use the configured model budget and provenance machinery; text-only PDFs skip them entirely. No local OCR or Tesseract installation is used. Independent review tasks then run concurrently up to `max_concurrency`; every stage result is persisted before scheduling the next stage.

Once `run start` reserves its run ID, it owns that run through the same kernel lock used by later mutations. `run status`, `run report`, and `run gate` remain available as read-only observations while a mutation is active; they do not take ownership or alter attempts.

### Use evidence anchors

Treat `source-map.json` as the exact path map. For source-line evidence, read `sources/sections/methods.tex` but return `source_path="sections/methods.tex"` together with `start_line`, `end_line`, `source_digest`, and `quoted_text`; do not include `page`. Revision edits use the same bare path and may target only entries marked `text_anchorable=true`.

For compiled-PDF evidence, read the page image at the map's exact path, such as `pages/page-0003.png`, but return `source_path="manuscript.pdf"`, `page=3`, and `quoted_text`; do not include source line or digest fields. Paths such as `sources/main.tex`, `pages/page-0003.png`, or a source graphic such as `sources/Fig5.pdf` are never accepted as output aliases. The Verifier uses the patched workspace's current source map and digests for any new issue; evidence attached to an earlier finding is historical context only.

### Diagnose structured-output failures

`run status` exposes a nullable `validation_report_artifact_digest` on each attempt. `run report --format json` adds the corresponding full reports in stable attempt order; the Markdown report shows only the issue count, first code and JSON Pointer, and report digest. The short attempt error is intentionally only a one-line index into this durable report.

Scriptorium validates one complete replacement object at a time. The first invalid base turn may receive one automatic same-session correction. If that correction is still invalid, the command stops; each later same-route `run resume` or `run retry` adds exactly one correction attempt using the latest report. Selecting a different named route starts a new session with the full base task and compatible diagnostics. Correction attempts continue to count their actual usage and cost, and a later mutation must pass the ordinary budget gate.

Do not edit or regenerate a validation report. A missing, corrupt, wrongly typed, or provenance-mismatched report is an infrastructure failure and blocks a new attempt. Repair artifact storage rather than manually changing SQLite. Reports may say that a PDF quotation failed both native and visual checks, but hidden visual-transcription text is deliberately not included. Raster-only pages may therefore still require the reviewer to reread the exact page-image `read_path` from `source-map.json` or use source-line evidence; this release does not relax strict PDF evidence rules or expose the transcription as a suggested quotation.

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

The per-run kernel `flock`, not PID data or lock-file contents, determines whether a live owner exists. Owner JSON stored on the same lock-file inode is only a diagnostic aid and may be stale after a crash. Never edit it, delete it, replace the lock file, or use it as permission to recover a run.

If a live owner holds the lock, ordinary protected mutations reject safely. This includes `run resume`, `run retry`, `finding decide`, `patch decide`, and `patch apply`; `run start` joins the same ownership contract after reserving its run ID.

`run cancel` instead publishes a durable local request and waits up to 15 seconds. The active owner observes it, requests native cancellation for its provider turns, waits for their contained process groups to exit, records attempts as `interrupted`, and commits the run as `cancelled`. The original owner command exits with the existing `interrupted` error and code `3`; the cancel command returns the cancelled run with code `0`. If confirmation takes longer than 15 seconds, cancel returns `invalid_state` and code `1` with its request ID; the request remains pending and must not be deleted manually.

Pressing Ctrl+C is different from `run cancel`: the first interrupt requests native cancellation and cleans up the contained provider process groups, but leaves the run in its current resumable stage. The active attempts are durably `interrupted`, and the command exits with code `3`. Use `run resume`, `run retry`, or an explicit `run cancel` afterward. A second interrupt or forced driver termination may stop the driver immediately; the worker control-channel EOF still starts the cleanup watchdog. The watchdog gives native interruption, terminal drain, and SDK close 10 seconds, then sends process-group `SIGTERM` and escalates to `SIGKILL` after 2 more seconds.

Only a command that has acquired the kernel lock and passed the provider cleanup barrier may recover leftover `running` attempts. The next `run resume`, `run retry`, or `run cancel` marks stale attempts as `interrupted` before continuing. A completed task with an unchanged input digest is not repeated, and failed or interrupted tasks append a new attempt ordinal. Read-only `run status`, `run report`, and `run gate` never perform this recovery or consume pending cancellation requests, so observing stale state is not itself a state change.

The permanent `<run-id>.providers.lock` file is a cleanup barrier, not a second ownership record. Its contents and existence do not indicate a live provider. Do not delete or replace it. A worker keeps its shared lock after reporting a result until the parent has reaped the process group; parent death instead leaves EOF-triggered watchdog cleanup in charge. If cleanup does not finish within 15 seconds, the lifecycle mutation reports an infrastructure error rather than starting overlapping provider work or killing a PID read from stale metadata.

The database field named `thread_id` stores an opaque native session or conversation ID. Scriptorium resumes it only when the semantic task and the frozen runtime, exact SDK version, provider, and model all match. A same-route retry, including an explicit override naming that same frozen route, may continue the native session. When `--route` changes the task to a different named route, Scriptorium always starts a new session, even if that route selects the same model. Runtime-native state lives in a stable run directory rather than the disposable task workspace; Antigravity keeps separate `save/` and `app/` directories.

If an attempt has a recorded session ID but the corresponding native state is missing, resume fails explicitly. It never hides the loss by creating a new conversation. Runtime or model version changes are recorded on a new attempt and never rewritten into earlier history.

If the budget is exhausted, the run pauses in `waiting_budget`. Inspect the report, then either retry the task with an explicitly selected frozen zero-cost route or start a new run with a new budget. Scriptorium never changes the frozen budget or selects a fallback model.

Historical reports may reflect the page-anchor behavior frozen before visual transcription was introduced. Read-only inspection preserves that history; Scriptorium never injects a newer model, route, or anchor rule into frozen inputs.

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

## Upgrading with existing runs

Before upgrading Scriptorium, reinstalling it from a different checkout, or switching the code used by a driver, stop every old driver and its child processes and confirm that their kernel locks and provider cleanup barriers have been released. Old and new drivers must never operate on the same `.scriptorium/` state concurrently. Same-inode owner JSON can help identify a process but is not proof that ownership remains or has ended.

After the old processes have stopped, perform recovery through `run resume`, `run retry`, or `run cancel`. That command must acquire the kernel lock before it marks stale attempts as interrupted. Do not make `run status`, `run report`, or `run gate` repair state, and do not edit SQLite, artifacts, session data, or lock files to force an upgrade through.

Runs created before the frozen evidence-anchor contract require an explicit boundary. `run status`, `run report`, and `run gate` remain read-only, existing finding and patch decisions and `patch apply` retain their behavior, and `run cancel` remains available. Terminal-run reads and no-ops are unchanged. For a nonterminal legacy run, however, `run resume` and `run retry` fail before attempt recovery or provider invocation; start a new run instead. Do not backfill the contract or infer it from old prompts or bundles.

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
