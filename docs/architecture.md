# Architecture

Scriptorium is a local modular monolith for one laboratory user and one Git-managed LaTeX project at a time. Codex remains the outer harness that starts and operates the tool. The CLI calls application services; services invoke deterministic Armarius workflow logic; Armarius dispatches frozen routes to sibling native Codex, Claude Code, or Antigravity runtime adapters.

The model runtimes are task executors, not a second orchestration layer. They cannot select their own route, delegate to secondary subagents, or silently switch runtime, provider, or model. Scriptorium does not add a graph engine, service process, or queue.

## Authority boundaries

- Git commit/tree SHA: manuscript content and frozen inputs.
- SQLite: run state, tasks, attempts, findings, append-only decisions, patches, verification records, and events.
- Artifact store: immutable prompts, schemas, bundles, traces, outputs, reports, and build evidence.
- Author worktree: changed only by an explicit, approved `patch apply`.

State lives under the manuscript repository:

```text
.scriptorium/
  config.toml
  state.sqlite3
  artifacts/sha256/
  runs/<run-id>/
    manifest.json
    snapshot/
    bundle/
    patched/
    sessions/<normalized-runtime-role-route-digest>/
  locks/
    <run-id>.lock
    <run-id>.providers.lock
  control/cancel/
```

`scriptorium init` adds `.scriptorium/` to `.gitignore`. SQLite uses WAL, foreign keys, a busy timeout, numbered SQL migrations, and short transactions. Git, LaTeX, and model calls happen outside transactions. An invalid structured response is preserved as a raw output artifact and accompanied by an immutable validation-report artifact; the terminal attempt transaction records both digests atomically.

### Run mutation ownership

A per-run kernel `flock` is the sole authority for whether a live process owns a run. The lock file may also contain same-inode JSON describing the apparent owner and operation, but that content is diagnostic only: it may be stale or incomplete and must never be used to override, break, or infer the absence of the kernel lock. Replacing or renaming the lock file would create a different inode and is not a valid ownership update.

The protected run mutations are `run start` after its run ID is reserved, `run resume`, `run retry`, `run cancel`, `finding decide`, `patch decide`, and `patch apply`. Each holds the same per-run lock across its state-changing operation. If another live owner holds the lock, ordinary mutations reject without changing SQLite, artifacts, or the author worktree.

`run cancel` is the cooperative control operation. It atomically publishes a durable request under `control/cancel/` without changing SQLite, then waits up to 15 seconds for the active owner to observe it. The owner cancels its workflow tasks, waits for provider cleanup, and records interrupted attempts, cancelled tasks, the cancelled run, and one `run.cancelled` event before removing the request. A request left by a crashed requester or owner is replayed idempotently by the next mutation that obtains the run lock. Read-only commands never consume it.

Every production runtime attempt executes in a foreground, per-attempt containment worker that owns a separate POSIX session and process group. It is not a background service, queue, or distributed worker. Workers hold a shared `<run-id>.providers.lock` while provider descendants may still exist. After reporting a terminal result, the worker stays alive with that barrier until its parent reaps the process group; if the parent disappears, control-channel EOF starts the same watchdog cleanup. A new owner takes the main run lock first and then waits up to 15 seconds for an exclusive pass through this provider cleanup barrier before recovering attempts or starting new work. The provider lock is not mutation authority, and persisted PID, PGID, or diagnostic owner JSON is never used to kill or reclaim work.

Containment covers the pinned SDK harnesses and descendants that inherit their POSIX session. A third-party child that deliberately starts a different session falls outside this guarantee; the supported harness versions must not detach in that way.

Recovery of leftover `running` attempts happens only after `run resume`, `run retry`, or `run cancel` acquires the per-run lock and passes the provider cleanup barrier. `run status`, `run report`, and `run gate` remain read-only, do not acquire mutation ownership, and never recover, consume cancellation requests, or rewrite state. After a driver exits unexpectedly, stale attempts therefore remain visible until one of those recovery-capable lifecycle commands safely acquires ownership and records their interruption.

Runtime-native session state stays under the stable run session directory, outside disposable task workspaces. Antigravity keeps separate `save/` and `app/` children there. Rebuilding a task bundle therefore cannot erase resumable native state.

## Modules

- `domain`: entities, enums, invariants, and state transitions.
- `runtime/base.py`: runtime-neutral DTOs and `AgentRuntime`.
- `runtime/codex.py`: the Codex adapter.
- `runtime/claude_code.py` and `runtime/antigravity.py`: optional, lazily imported native harness adapters.
- `workflow`: the deterministic `Armarius` scheduler, budgets, recovery, approval gates, and release gate.
- `storage`: `sqlite3` migrations, transactions, and queries.
- `artifacts`: SHA-256 content addressing and atomic publication.
- `manuscript`: Git freezing, LaTeX dependency scanning and compilation, PDF rendering, source anchors, and patch construction.
- `service`: application methods shared by the CLI and any future interface.
- `cli`: argument parsing plus text or JSON rendering; no business rules.

The public service surface is `start_run`, `get_run`, `resume_run`, `retry_task`, `cancel_run`, `list_findings`, `get_finding`, `decide_finding`, `get_patch`, `decide_patch`, `apply_patch`, `render_report`, and `evaluate_gate`.

## Runtime boundary

Business code depends only on:

```python
class AgentRuntime(Protocol):
    async def run_agent(
        self,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None = None,
    ) -> AgentResult: ...

    async def resume_agent(
        self,
        thread_id: str,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None = None,
    ) -> AgentResult: ...
```

`AgentResult` contains normalized thread status, structured response text, token usage, JSONL trace, runtime/model metadata, duration, and error information. The session-start callback persists a newly allocated opaque native session ID while the attempt is still running. Runtime cancellation carries a normalized interrupted result back across the boundary so Armarius can durably finish the attempt before cancellation propagates. Native SDK objects, notifications, exceptions, and configuration do not cross this adapter boundary. The database column remains named `thread_id`, but its cross-runtime meaning is an opaque native session or conversation ID.

Each adapter is bound to one exact native harness version:

- `CodexAgentRuntime`: `openai-codex==0.144.4`; start and resume both reapply the bundle cwd, role instructions, output schema, read-only sandbox, and deny-all approval policy.
- `ClaudeCodeAgentRuntime`: `claude-agent-sdk==0.2.128`; uses the bundled Claude harness with only `Read`, `Glob`, and `Grep`. A `PreToolUse` guard rejects reads outside the bundle or through path traversal. Bash, edits, writes, web, Agent, Skill, MCP, plugins, and external settings are disabled.
- `AntigravityAgentRuntime`: `google-antigravity==0.1.8`; uses `LocalAgentConfig` with only directory listing, search, find, view, and finish. Commands, writes, web access, and subagents are disabled, and resume uses the original conversation ID in strict `RESUME` mode.

The optional SDKs are imported only inside their adapters and only when selected. Claude Code accepts `structured_output` and persists SDK messages as NDJSON. Antigravity accepts structured output and persists the current turn's incremental steps as NDJSON. Cancellation first requests native session cancellation; adapters then normalize completion, failure, or interruption.

A session can be resumed only when runtime name, exact runtime version, provider, and model match the recorded attempt. A retry that changes to a different named route always creates a new native session; naming the same frozen route may resume a compatible failed or interrupted attempt. If a recorded native session ID exists but its state has disappeared, recovery reports failure instead of creating an unrelated session.

## Frozen manuscript and agent bundle

Run preparation resolves the requested Git revision to a commit/tree SHA and creates a persistent snapshot. Starting at `main.tex`, Scriptorium follows `\input`, `\include`, bibliography, and `\includegraphics` dependencies. The build environment sees the snapshot; the agent sees only:

```text
manifest.json
sources/
manuscript.pdf
pages/page-*.png
source-map.json
task.md
```

The bundle becomes the selected runtime's workspace. Repository-level `.codex/`, `AGENTS.md`, source code, scripts, unrelated files, and uncommitted changes are excluded.

If the PDF contains raster images, Armarius identifies those pages and runs one separately routed `visual_transcription` task before starting reviewers. That task's workspace contains only the target page PNGs and a digest request manifest, not manuscript sources, the PDF, or unrelated pages. Its output is an immutable, digest-bound transcription artifact used only for evidence validation; it is not copied into reviewer workspaces. PDF quotations are matched first against the page's native text and then against the frozen transcription, with whitespace normalization only. Patched PDFs receive an independent transcription before verification. Scriptorium does not perform local OCR or depend on Tesseract.

## Durable workflow

```text
preparing
→ reviewing
→ awaiting_decision
→ revising
→ awaiting_patch_approval
→ verifying
→ ready_to_apply
→ completed
```

`waiting_budget`, `failed`, and `cancelled` are pause or terminal states. A task is the stable logical unit keyed by run, stage, role, route, and base-input digest. Correction diagnostics change only the attempt prompt digest, not task identity. Every new or resumed model turn creates an immutable attempt. A resume attempt records its known session ID when it begins; a new session is filled exactly once with compare-and-set semantics and cannot be replaced at completion. Completed tasks with the same input digest are reused; failed or interrupted work appends a new attempt. Ctrl+C finishes active attempts as `interrupted` while leaving the run at its resumable workflow stage; it does not imply durable run cancellation.

Structured outputs are accepted atomically. JSON syntax is checked first, then all Pydantic schema errors are normalized, and semantic anchor or workflow validation runs only after the schema is complete. Semantic validation accumulates every independently decidable issue while skipping checks whose prerequisites are absent. Any issue rejects the whole output, so no finding, patch, verification, or visual transcription is partially materialized. Runtime provenance, artifact access, PDF I/O, SQLite, and internal state failures remain infrastructure errors rather than model-correctable diagnostics.

Each validation report binds the output artifact, frozen schema digest, and attempt bundle digest. Reports contain stable error codes, RFC 6901 JSON Pointers, bounded expected and actual values, and bounded diffs where an exact target exists. They contain no attempt-specific timestamp or random field, so identical invalid output under the same bindings has the same artifact digest. Hidden visual-transcription text may be used to check a PDF quote but is never copied into reviewer diagnostics.

An initial base turn may receive one automatic same-session correction. If that correction also fails, the mutation stops. A later same-route resume or retry loads the latest durable report and adds exactly one correction attempt without replaying the base prompt. An interrupted correction reuses its exact recorded prompt artifact. A different named route creates a new task and session with the full base prompt plus the compatible prior report. Reports are never recomputed during recovery; missing, corrupt, wrongly typed, or mismatched report artifacts are infrastructure failures.

Review aggregation performs schema and anchor validation, exact-fingerprint deduplication, provenance preservation, and severity ordering only. It does not ask a consensus model or perform semantic clustering. The visual transcriber supplies page text but cannot submit findings or validate its own output. Confirmed findings are passed to the read-only Scribe, whose exact, non-overlapping edits are applied to a separate snapshot and compiled. An independent Verifier checks resolution and regression. A failed verification returns to patch approval and never starts an automatic infinite loop.

The release gate requires successful required reviews, no unresolved blocker or major finding, verified coverage of confirmed findings or a later waiver, a successful patched build, a passing Verifier, and successful patch application when changes are required.

Every new run freezes the runtime name and exact SDK version on each route together with provider, model, prompts, schemas, prices, and source digests. A historical frozen route without `runtime` is interpreted using that run's original top-level Codex runtime and is not rewritten. Package versions, SDK versions, and numbered database migrations are compatibility and recovery contracts, not product-generation labels.
