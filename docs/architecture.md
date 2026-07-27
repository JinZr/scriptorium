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
```

`scriptorium init` adds `.scriptorium/` to `.gitignore`. SQLite uses WAL, foreign keys, a busy timeout, numbered SQL migrations, and short transactions. Git, LaTeX, and model calls happen outside transactions. A mutating command holds a per-run OS file lock; read-only status and reporting commands do not.

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
    ) -> AgentResult: ...

    async def resume_agent(
        self,
        thread_id: str,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
    ) -> AgentResult: ...
```

`AgentResult` contains normalized thread status, structured response text, token usage, JSONL trace, runtime/model metadata, duration, and error information. Native SDK objects, notifications, exceptions, and configuration do not cross this adapter boundary. The database column remains named `thread_id`, but its cross-runtime meaning is an opaque native session or conversation ID.

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

`waiting_budget`, `failed`, and `cancelled` are pause or terminal states. A task is the stable logical unit keyed by run, stage, role, route, and input digest. Every new or resumed model turn creates an immutable attempt. Completed tasks with the same input digest are reused; failed or interrupted work appends a new attempt.

Review aggregation performs schema and anchor validation, exact-fingerprint deduplication, provenance preservation, and severity ordering only. It does not ask a consensus model or perform semantic clustering. Confirmed findings are passed to the read-only Scribe, whose exact, non-overlapping edits are applied to a separate snapshot and compiled. An independent Verifier checks resolution and regression. A failed verification returns to patch approval and never starts an automatic infinite loop.

The release gate requires successful required reviews, no unresolved blocker or major finding, verified coverage of confirmed findings or a later waiver, a successful patched build, a passing Verifier, and successful patch application when changes are required.

Every new run freezes the runtime name and exact SDK version on each route together with provider, model, prompts, schemas, prices, and source digests. A historical frozen route without `runtime` is interpreted using that run's original top-level Codex runtime and is not rewritten. Package versions, SDK versions, and numbered database migrations are compatibility and recovery contracts, not product-generation labels.
