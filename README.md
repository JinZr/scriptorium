# Scriptorium

Scriptorium is a local, single-user, Git-native workflow for reviewing, revising, and verifying LaTeX manuscripts with Codex. Git commits are the authority for manuscript content, SQLite records workflow state and human decisions, and a content-addressed artifact store preserves immutable inputs, model outputs, and evidence.

The v1 workflow is:

```text
prepare → review → human decision → revision proposal
→ patch approval → verification → patch apply → completed
```

`Armarius` is deterministic Python orchestration, not a model agent. Model work crosses one narrow `AgentRuntime` boundary and is implemented by the pinned Codex Python SDK. Scriptorium does not use LangGraph, an HTTP service, background workers, or native OpenAI, Anthropic, or Google model clients.

## Requirements

- Python 3.10 or newer
- Git
- `latexmk` and a supported LaTeX engine (`pdflatex`, `xelatex`, or `lualatex`)
- PDF rendering support for review bundles
- Codex provider credentials and endpoint configuration outside the manuscript repository

Install for development:

```bash
python -m pip install -r requirements-dev.txt
```

For runtime dependencies only, use `python -m pip install -r requirements.txt`.

## Quick start

From a Git-managed LaTeX manuscript repository:

```bash
scriptorium init . --main main.tex --engine pdflatex
```

Commit `scriptorium.toml`, then configure local role routing in the ignored `.scriptorium/config.toml`. Start a run from an explicit Git revision:

```bash
scriptorium doctor --profile full --budget-usd 10
scriptorium run start --revision HEAD --profile full --budget-usd 10
scriptorium run status RUN_ID
scriptorium finding list RUN_ID
```

The run reads a persistent snapshot of the resolved commit. Uncommitted work is excluded. Review each finding and record `confirm`, `reject`, or `waive` with a reason. Scriptorium generates and compiles a patched snapshot without modifying the author's worktree. Only an approved, independently verified patch becomes eligible for explicit application.

Use top-level `--json` for machine-readable output:

```bash
scriptorium --json run status RUN_ID
```

Errors have stable codes:

```json
{
  "ok": false,
  "error": {
    "code": "stable_machine_code",
    "message": "human readable message"
  }
}
```

Exit codes are `0` for command success or a passing gate, `1` for a valid domain condition that did not pass, `2` for argument or configuration errors, and `3` for Git, LaTeX, SQLite, or runtime infrastructure failures.

## Safety and authority

- `.scriptorium/` is project-local and Git-ignored.
- Codex receives only a generated manuscript bundle, never the repository's `.codex/`, `AGENTS.md`, scripts, unrelated files, or uncommitted content.
- All v1 model roles are read-only. The Scribe returns structured edits; it cannot edit the manuscript.
- Scriptorium does not silently fall back between models or routes.
- Applying a patch rechecks source digests and stops if the worktree is stale.
- Scriptorium never switches branches, merges, commits, pushes, or applies an unapproved patch.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Operations and recovery](docs/operations.md)

## Development checks

Use the sleep2vec-style helper to format and lint the repository with the active Python environment:

```bash
bash utils/style_check.sh
```

Use the non-mutating checks for CI and final verification:

```bash
python -m isort --check-only .
python -m black --check .
python -m flake8 . --count --statistics
python -m pytest -q
python -m build
```
