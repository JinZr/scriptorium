# Scriptorium

![Scriptorium manuscript review workflow](docs/assets/scriptorium-banner.png)

Scriptorium is a lab-local, single-user, Git-native research tool for reviewing, revising, and verifying LaTeX manuscripts. Git commits are the authority for manuscript content, SQLite records durable workflow state and human decisions, and a content-addressed artifact store preserves immutable inputs, model outputs, and evidence.

The workflow is:

```text
prepare → raster-page transcription when needed → review → human decision
→ revision proposal → patch approval → patched-page transcription when needed
→ verification → patch apply → completed
```

Codex is the outer, user-facing harness: it starts and operates Scriptorium as part of the laboratory workflow. Inside Scriptorium, `Armarius` is deterministic Python orchestration, not a model agent. It dispatches each frozen role/route to one of three sibling native runtimes: `codex`, `claude_code`, or `antigravity`. The workers do not choose routes, create secondary subagents, or fall back to another runtime or model.

When a manuscript PDF contains raster images, Armarius first sends the affected rendered pages to the separately routed `visual_transcription` role. PDF quotations are checked against the native text layer and then that frozen transcription. The same step runs independently for a patched PDF before verification. This uses a vision-capable model and adds model cost; Scriptorium does not use local OCR or require Tesseract.

Scriptorium intentionally has no LangGraph layer, HTTP service, or background queue.

## Requirements

- Python 3.10 or newer
- Git
- `latexmk` and a supported LaTeX engine (`pdflatex`, `xelatex`, or `lualatex`)
- PDF rendering support for review bundles
- Credentials or native login for every runtime referenced by the selected routes

The base package includes the pinned Codex runtime:

```bash
python -m pip install .
```

Install native harnesses only when their routes are used:

```bash
python -m pip install '.[claude]'       # claude-agent-sdk==0.2.128
python -m pip install '.[antigravity]'  # google-antigravity==0.1.8
python -m pip install '.[all]'          # both optional runtimes
```

Install for development:

```bash
python -m pip install -e '.[dev,all]'
```

`requirements.txt` and `requirements-dev.txt` retain the base Codex installation. Choose an extra explicitly when exercising a native Claude Code or Antigravity route.

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
- A runtime receives only a generated manuscript bundle, never the repository's `.codex/`, `AGENTS.md`, scripts, unrelated files, or uncommitted content.
- Model roles are read-only. Codex runs with a read-only sandbox and deny-all approvals; Claude Code is limited to `Read`, `Glob`, and `Grep`; Antigravity is limited to directory listing, search, find, view, and finish.
- Bash, write/edit tools, web access, MCP, plugins, skills, external settings, and native subagents are unavailable to native workers.
- Scriptorium accepts only structured output and never silently falls back between runtimes, models, providers, or routes.
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

Live native-harness smoke tests are skipped by default. They require an explicit per-runtime environment switch, a model name, and working native authentication; see [Operations and recovery](docs/operations.md#live-native-harness-smoke-tests).
