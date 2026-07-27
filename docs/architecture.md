# Architecture

Scriptorium v1 is a local modular monolith for one user and one Git-managed LaTeX project at a time. The CLI calls application services; services invoke deterministic workflow logic; the workflow uses storage, manuscript, artifact, and runtime adapters.

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
  locks/
```

`scriptorium init` adds `.scriptorium/` to `.gitignore`. SQLite uses WAL, foreign keys, a busy timeout, numbered SQL migrations, and short transactions. Git, LaTeX, and model calls happen outside transactions. A mutating command holds a per-run OS file lock; read-only status and reporting commands do not.

## Modules

- `domain`: entities, enums, invariants, and state transitions.
- `runtime`: runtime-neutral DTOs, `AgentRuntime`, and the Codex adapter.
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
    ) -> AgentResult: ...

    async def resume_agent(
        self,
        thread_id: str,
        task: str,
    ) -> AgentResult: ...
```

`AgentResult` contains normalized thread status, response text, token usage, JSONL trace, runtime/model metadata, duration, and error information. Codex SDK objects, notifications, exceptions, and configuration do not cross this adapter boundary.

The only v1 implementation is `CodexAgentRuntime`, backed by `openai-codex==0.144.4`. Each independent task starts a thread with an explicit model, provider, controlled bundle cwd, read-only sandbox, role instructions, and output schema. A persisted thread ID may be resumed for one structure/anchor correction or for human revision feedback. If a process exits before the thread ID is safely stored, recovery creates a new thread rather than guessing private SDK state.

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

The bundle becomes the Codex project root. Repository-level `.codex/`, `AGENTS.md`, source code, scripts, unrelated files, and uncommitted changes are excluded.

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
