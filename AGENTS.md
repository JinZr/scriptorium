# Scriptorium Agent Guide

This file governs work on the Scriptorium repository. It is not part of the
manuscript bundle shown to review runtimes.

## Project scope

Scriptorium is a lab-local, single-user, Git-native CLI for durable LaTeX
manuscript review, revision, approval, verification, and release gating. Keep
it a modular monolith. Do not add a GUI, HTTP service, background queue, graph
orchestration layer, or distributed worker system unless the user explicitly
requests that expansion.

Before changing behavior, read the relevant contracts in `README.md` and
`docs/architecture.md`. Use `docs/configuration.md` for route and budget work
and `docs/operations.md` for lifecycle, recovery, or CLI work.

## Authority and safety contracts

- Git commits and tree digests are the authority for manuscript content and
  frozen run inputs. Uncommitted manuscript changes must not enter a run.
- SQLite owns durable workflow state and append-only decisions/events. The
  content-addressed artifact store owns immutable prompts, bundles, outputs,
  traces, and build evidence.
- Never repair a run by editing `.scriptorium/`, SQLite, artifacts, snapshots,
  bundles, or generated patches manually. Repair the external prerequisite and
  resume or retry through the CLI.
- Review runtimes are read-only task executors. They must not modify files,
  choose routes, create subagents, use unrelated repository content, or fall
  back silently to another runtime, provider, model, or route.
- A run freezes its Git revision, source digests, role-to-route mapping,
  runtime and SDK version, provider/model settings, prompts, schemas, pricing,
  retry policy, and budget. Frozen metadata must describe the native invocation
  that actually runs; reject unsupported settings instead of merely recording
  or ignoring them.
- Findings, human decisions, patch approval, verification, application, and
  the release gate are separate stages. Do not infer approval from severity or
  bypass an explicit human gate.
- Exact edits must match the approved base, path, digest, line range, and
  `before` text. Reject stale worktrees; do not add automatic three-way merging.
- Infrastructure failures are inconclusive, not successful verification. They
  must remain visible and must not allow the release gate to pass.
- Scriptorium does not switch branches, merge, commit, or push manuscript work.
  Patch application may touch only explicitly approved files.

## Code boundaries

Preserve the dependency direction and existing ownership:

- `domain.py`: entities, enums, invariants, and state transitions.
- `runtime/base.py`: runtime-neutral contracts and normalized results.
- `runtime/*.py`: native SDK details, security restrictions, start/resume, and
  normalization. SDK objects and exceptions do not cross this boundary.
- `workflow.py`: deterministic Armarius scheduling, recovery, budgets, and
  approval/release gates.
- `storage.py`: SQLite migrations, transactions, and persistence queries. Add a
  numbered migration for a schema change; do not rewrite historical migrations.
- `artifacts.py`: atomic, SHA-256-addressed artifact publication.
- `manuscript.py`: Git snapshots, LaTeX dependency scanning/building, bundles,
  source anchors, and exact patch construction.
- `service.py`: application operations shared by entrypoints.
- `cli.py`: argument parsing and text/JSON presentation, not business rules.
- `schemas.py` and `prompts/`: structured runtime I/O contracts and role prompts.

Keep optional runtime SDKs lazily imported by their adapters. Treat pinned SDK
versions and resume compatibility as recovery contracts. When adding or
changing a frozen route field, trace it through to the native SDK call and add
a regression test using a non-default value.

Reimplement behavior independently. Do not introduce reference-project names,
imports, dependencies, schemas, prompts, compatibility layers, filenames, or
copied implementation into the product.

## Evaluation boundary

Keep PeerReviewBench-specific code and dependencies under
`egs/peerreviewbench/`, with tests under `tests/peerreviewbench/`; do not widen
Scriptorium core APIs for the benchmark unless explicitly required.

Treat reviewer recall, precision, and F1 as one capability channel, not as
end-to-end acceptance of revision, approval, compilation, verification,
resume, or release-gate behavior. Offline fixtures and dry runs are not evidence
of a live paid benchmark run. Preserve upstream revisions, hashes, dependency
versions, and run provenance without adding product-style schema or generation
version fields to the research dataset.

## Change discipline

- Make the smallest change that satisfies the request. Avoid unrelated
  refactors, speculative abstractions, compatibility shims, or new policy.
- Inspect `git status` and the relevant diff before editing. Existing changes
  belong to the user; preserve them and avoid repo-wide rewrites in a dirty
  worktree.
- Put tests beside the behavior they own, following `tests/README.md`. Keep
  fakes and builders inside their owning test package.
- Keep `pyproject.toml` authoritative for dependencies and tool configuration.
- Update documentation when a public CLI, configuration, lifecycle, safety, or
  recovery contract changes.
- Never place credentials in tracked files, manuscript bundles, SQLite, or
  artifacts. Live provider checks require explicit opt-in and working native
  authentication.

## Validation

Use the project virtual environment when available. Run the narrowest relevant
tests first, then validate in proportion to the change. The standard checks are:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m isort --check-only .
.venv/bin/python -m black --check .
.venv/bin/python -m flake8 . --count --statistics
```

Run `.venv/bin/python -m build` when packaging, dependencies, package data, or
entrypoints change. `bash utils/style_check.sh` mutates files, so do not run it
blindly over unrelated user changes. Live native-harness tests are paid,
credentialed integration checks and must never be enabled without explicit
authorization.

Do not commit, push, rebase, amend, discard changes, or change branches unless
the user explicitly authorizes it. When a commit is authorized, stage only the
current task's paths or hunks, inspect the staged diff, and keep implementation
with its directly related tests in the same logical commit.
