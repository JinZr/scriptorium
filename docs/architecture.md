# Architecture

Scriptorium is a single-user modular monolith. It has no server, queue, or model runtime. Codex, Claude Code, and Antigravity CLI operate the same JSON CLI from their own selected model sessions.

## Authority and flow

- A Git commit and tree digest identify manuscript content. A disposable snapshot is scanned and compiled; uncommitted files are excluded.
- `manuscript.py` creates the frozen source, navigation, source map, PDF, and page-image bundle. Source and page digests are checked before retrieval or result validation.
- `workflow.py` prepares review, revision, and verification tasks. It validates whole structured submissions and advances only after the required prior stage and human decision.
- `storage.py` owns SQLite migrations, tasks, attempts, decisions, and events. `artifacts.py` publishes immutable SHA-256 content.
- `service.py` provides short, per-run mutations under an OS file lock. A task claim persists across CLI processes. `cli.py` provides the shared JSON interface.

`run start` freezes inputs, compiles the base bundle, and creates review tasks. `task claim` records the external client, model, effort, and session ID on a durable attempt. The claim returns its prompt, schema, input digest, navigation digest, and source map. `task read`, `task search`, and `task page` return bounded frozen material and append access events. Read events record the source digest, exact returned line and character-offset ranges, and continuation cursor. `task submit` stores the raw result, validates it against the frozen schema and evidence map, and records a complete or failed attempt. An accepted result is replayed into findings, a candidate patch, or verification. Repeating a completed submission returns the same receipt; a different or superseded result is rejected.

A CLI process ending does not interrupt an external attempt. Only explicit abandonment, retry, or cancellation changes that claim. An invalid result creates a durable validation report; retry must be requested explicitly. Each attempt binds its own prompt, schema, and bundle digest, including correction prompts. A later finding waiver interrupts an active revision or verification task whose frozen prompt no longer matches the decision context; `run resume` prepares a new task. On recovery, completed output is read from the artifact store and validated again before domain records are materialized. Missing or corrupt frozen material is an infrastructure failure.

## Evidence and gates

Text evidence uses a bare source path, source digest, inclusive line range, and verbatim quote. PDF evidence uses `manuscript.pdf` plus a 1-based page; a page-image path is only a read location. Tool access logs report material returned, not model comprehension or exhaustive coverage.

Finding decisions, patch approval, verification, and application are separate stages. The verifier must claim from a new host-reported conversation ID, distinct from review and revision. A declared or reused ID cannot produce a passing verification. This checks recorded provenance; Scriptorium cannot authenticate the external host's claim. Patch application rechecks exact approved edits against the current worktree. Infrastructure failures cannot pass the release gate.

External model usage and cost are unknown unless the host supplies independent evidence. The tool neither invokes providers nor enforces their spending limits. Historical internal-SDK runs remain available for read-only status, reports, and gate inspection, without conversion to external tasks. Opening a legacy database for those queries does not apply the external-task migration; starting a new external run applies it only when no historical run still requires the original version.
