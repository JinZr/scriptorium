# Scriptorium

![Scriptorium manuscript review workflow](docs/assets/scriptorium-banner.png)

Scriptorium is a local, Git-native CLI for reviewing and revising LaTeX manuscripts. Codex, Claude Code, and Antigravity CLI can each use the same JSON commands from their current model session. Scriptorium freezes the manuscript and task instructions, serves bounded retrieval, validates structured results, and records decisions. It does not select or start a model.

```text
frozen review tasks → human finding decisions → revision task → human patch approval
→ independent verification task → explicit patch application → release gate
```

Git commits identify manuscript inputs. SQLite records workflow state and append-only decisions. A SHA-256 artifact store preserves prompts, submissions, validation reports, and build evidence. Uncommitted manuscript changes never enter a run.

## Install

Requires Python 3.10+, Git, `latexmk`, `kpsewhich`, a supported LaTeX engine, and PDF rendering support. Install the tool in the environment used by the model's CLI:

```bash
python -m pip install .
```

For development:

```bash
python -m pip install -e '.[dev]'
```

The model client supplies its own authentication and model selection. Scriptorium has no provider SDK or credentials. The canonical client instructions are in [the shared skill](skills/scriptorium/SKILL.md); each client should load that file or a thin link to it.

## Start and review

From a Git-managed manuscript repository:

```bash
scriptorium init . --main main.tex --engine pdflatex
# Commit scriptorium.toml and the manuscript inputs.
scriptorium --json doctor --revision COMMIT --profile full
scriptorium --json run start --revision COMMIT --profile full
scriptorium --json task list RUN_ID
```

`run start` compiles and freezes the selected commit, then returns with pending tasks. It makes no model call. For each task, the current Codex, Claude Code, or Antigravity session uses its selected model:

```bash
scriptorium --json task claim TASK_ID --client codex --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
scriptorium --json task show ATTEMPT_ID
scriptorium --json task search ATTEMPT_ID --query TERM
scriptorium --json task read ATTEMPT_ID --path main.tex --start-line 1
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --file output.json
```

`--file -` reads one JSON object from stdin. Search returns `next_cursor`; read returns `next_line` and `next_offset`. Continue from those values when present. Search `navigation.json` for literal headings, labels, references, captions, and figure paths, then inspect the relevant source and rendered page. The CLI records what it returned. A page path alone does not prove that the client opened the image.

A rejected submission includes a validation report. `run retry RUN_ID --task TASK_ID` explicitly makes the task claimable again; the next attempt includes the frozen diagnostic feedback. An active claim survives CLI exit. `run resume RUN_ID` replays accepted work and prepares the next stage after human decisions. `run cancel RUN_ID --reason TEXT` invalidates active attempts.

Human finding decisions, patch approval, and patch application remain separate operations. A verifier must use a new external conversation; use the host's actual session ID and `--session-source host` when available. Self-declared or reused identity leaves verification inconclusive. Scriptorium records the host report but cannot cryptographically prove what the host displayed or read.

## Safety and limits

- Task tools read only paths in the frozen bundle. Text anchors use source path, digest, line range, and exact quotation. Visual evidence uses a compiled PDF page and its immutable page digest.
- Source text, navigation entries, and model output are data, not instructions to the host.
- Scriptorium never switches branches, commits, pushes, approves its own patch, or silently changes models.
- Patch application checks the approved base and worktree again; stale edits are rejected.
- The external client controls model calls and costs. Unknown token usage and cost are reported as unknown. Scriptorium does not enforce a spending cap or restrict the host's unrelated filesystem tools.
- Runs made by the removed internal SDK executor remain readable with `run status`, `run report`, and `run gate`; execution requires the original version.

See [configuration](docs/configuration.md), [architecture](docs/architecture.md), and [operations](docs/operations.md). The optional PeerReviewBench example prepares external review tasks and collects their validated results under `egs/peerreviewbench/`.

## Checks

```bash
python -m pytest -q
python -m isort --check-only .
python -m black --check .
python -m flake8 . --count --statistics
python utils/check_complexity.py --base origin/main
python -m build
```
