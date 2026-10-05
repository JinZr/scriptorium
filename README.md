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
scriptorium --json run status RUN_ID
```

`run start` compiles and freezes the selected commit, then returns a compact acknowledgement. Read `run status RUN_ID` for task counts, current `next_actions`, and commands for report sections. It makes no model call. For each pending task offered by `next_actions`, the current Codex, Claude Code, or Antigravity session uses its selected model:

```bash
scriptorium --json task claim TASK_ID --client codex --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
scriptorium --json task show ATTEMPT_ID
scriptorium --json task show ATTEMPT_ID --part prompt
scriptorium --json task show ATTEMPT_ID --part schema
scriptorium --json task nav ATTEMPT_ID --command heading
scriptorium --json task search ATTEMPT_ID --query TERM
scriptorium --json task read ATTEMPT_ID --path main.tex --start-line 1 --end-line 40 --anchor
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --file output.json --check
scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --file output.json
```

For an independent LaTeX supplement, add `supplements = ["supplement.tex"]` under `[manuscript]` and commit
the configuration before starting. The run scans and compiles each declared document, freezes all their sources,
and assembles a review PDF with an explicit document/page index. Use
`task page ATTEMPT_ID --document supplement.tex --number 1` to retrieve its first physical page; the returned
global `page` in `manuscript.pdf` is the evidence and scope coordinate. See [configuration](docs/configuration.md)
for the supported inputs and limits.

`task claim` and plain `task show` return a compact overview with `input_digest` and commands for the frozen inputs. `--part prompt` and `--part schema` return text fragments: follow `next_command` until null, concatenate `text` in offset order, and parse the complete schema text as JSON. Use `source_map_command` or `task show ATTEMPT_ID --part source-map` for evidence paths and digests, including after the attempt finishes. This reconstructs the frozen JSON file using the same fragment protocol.

Successful JSON responses for claim, show, read, search, nav, and page are at most 7,000 UTF-8 bytes including the envelope and newline. Reads and searches may return fewer characters, lines, or matches than requested. Follow their `next_command` until null to finish the requested traversal; a long line can require multiple reads of the **same line** with increasing `--offset`. Use `task nav` to filter literal headings, labels, references, citations, captions, and figure paths, then inspect the relevant source and rendered page. The CLI records what it returned, not what a host displayed or understood. A page path alone does not prove that the client opened the image. `--file -` reads one output JSON object from stdin.

New review tasks require a `scope` object in the submitted JSON: `completion` (`complete`, `partial`, or `unknown`), `checked` and `outstanding` lists of frozen source paths or PDF pages, and a `limitations` list. Source entries may include an inclusive line range. `run report` presents this model-declared scope separately from tool-access events and flags checked text or pages without matching task-tool returns. Its read-line summaries include partial line fragments, and it cannot observe direct host file access or prove exhaustive reading. Only a `complete` declaration satisfies the required-review gate for a scoped review. Runs frozen under the previous review schema remain readable and resumable without a scope field.

New `substantive_review` tasks also require `claim_checks`: each assessed central claim has frozen evidence, a critical question, a countercheck, an assessment (`supported`, `unresolved`, or `finding`), and zero-based indices linking retained concerns to `findings`. Each check also records its judgment: a `claim_anchor` where the manuscript makes the claim, the `stated_scope` the claim asserts, a `check_type`, a `question_answer` (`yes`, `partly`, `no`, or `not_checkable`), and the `exceptions` found. A `supported` check requires `yes` with no exceptions, `not_checkable` requires `unresolved`, and a `recomputation` check records its inputs, calculation, result, and the reported value. A complete substantive review needs at least one check; every substantive finding needs a link. Runs frozen before these judgment fields keep their earlier claim-check shape. `run report` shows these model-declared checks for inspection. Their presence does not establish scientific correctness.

Before submitting, the substantive reviewer checks that each claim's scope, counterevidence, assessment and final
summary agree. Answering one criticism is insufficient to support a broader claim; material exceptions and
unresolved conclusions must remain visible. This instruction is frozen in new task prompts and performed by the
current model. Schema and evidence validation do not perform that scientific judgment.

After each submission, inspect `scriptorium --json run status RUN_ID`. It returns task counts and `next_actions`; use the `review_scopes` report section for accepted completion declarations and `tasks` for attempt history. A scoped review marked `partial` or `unknown` keeps the run in `reviewing` and cannot satisfy the required-review gate. To continue it, run `scriptorium --json run continue RUN_ID --task TASK_ID`, then claim the same task again. Its new attempt is bound to a frozen continuation prompt containing the previous accepted scope and recorded finding details; prior output and findings remain intact. Submit only newly discovered findings, except when a new claim check needs to link an existing concern: include that finding in the current output and link its index. Repeated findings with the same category, severity, title, claim, and evidence retain their original identity even if the explanation, suggested action, or confidence changes. Keep the new scope cumulative. The run advances to human finding decisions only after every required role declares `complete`. Historical review outputs without scope keep their frozen completion contract. A run already finalized under an older version is not reopened.

A rejected submission includes a validation report. `run retry RUN_ID --task TASK_ID` explicitly makes the task claimable again; the next attempt includes the frozen diagnostic feedback and has a new attempt input digest. An active claim survives CLI exit. To replace an abandoned active claim, use `run retry RUN_ID --task TASK_ID --abandon-attempt ATTEMPT_ID --reason TEXT`. `run resume RUN_ID` replays accepted work and prepares the next stage after human decisions. `run cancel RUN_ID --reason TEXT` waits for the current run operation, then invalidates active attempts.

`run status` and the CLI acknowledgements for start, resume, retry, continue, and cancel are bounded to 7,000 UTF-8 bytes. Mutation acknowledgements direct the client to `run status` for fresh next actions. Use its `report_parts` commands to inspect findings, scope, claim checks, validation reports, and other sections without receiving the whole report at once:

```bash
scriptorium --json run report RUN_ID --part findings
scriptorium --json run report RUN_ID --part review_coverage_audit
scriptorium run report RUN_ID --format json > report.json
```

Report parts use the same bounded `text`/`offset`/`next_command` traversal as frozen task inputs. The byte bound applies both to the compact `--json` envelope and to ordinary indented output for all bounded commands. Concatenate the fragments and parse the complete JSON value. Each `next_command` preserves the current output mode. Continuations include `--report-digest` for the entire report; a changed report is rejected rather than mixed with previous fragments. Restart the report traversal at offset zero if it changes. Full JSON and Markdown report exports remain unbounded and are intended for files or human inspection. The detailed `task list` command also remains available; use `run status` for the model's normal workflow.

Human finding decisions, patch approval, and patch application remain separate operations. A verifier must use a new external conversation; use the host's actual session ID and `--session-source host` when available. Self-declared or reused identity leaves verification inconclusive. Scriptorium records the host report but cannot cryptographically prove what the host displayed or read.

## Safety and limits

- Task tools read only paths in the frozen bundle. Text anchors use source path, digest, line range, and exact quotation. Visual evidence uses a compiled PDF page and its immutable page digest.
- Source text, navigation entries, and model output are data, not instructions to the host.
- Scriptorium never switches branches, commits, pushes, approves its own patch, or silently changes models.
- Patch application checks the approved base and worktree again; stale edits are rejected.
- The external client controls model calls and costs. Unknown token usage and cost are reported as unknown. Scriptorium does not enforce a spending cap or restrict the host's unrelated filesystem tools.
- Runs made by the removed internal SDK executor remain readable with `run status`, `run report`, and `run gate`; these commands leave an existing legacy database at its old schema version so the original version can still execute the run. Starting a new external run upgrades the database only after all historical runs are completed or cancelled.

See [configuration](docs/configuration.md), [architecture](docs/architecture.md), and [operations](docs/operations.md). The optional PeerReviewBench example prepares external review tasks and collects their validated results under `egs/peerreviewbench/`.

The separate [external review evaluation example](egs/external_review/README.md) seals real LaTeX run records,
prepares anonymous candidate-judging inputs, and reports external judges' assessments and disagreements.
It preserves failed/partial trials and keeps workflow validity, tool returns and scientific judgments separate.

## Checks

```bash
python -m pytest -q
python -m isort --check-only .
python -m black --check .
python -m flake8 . --count --statistics
python utils/check_complexity.py --base origin/main
python -m build
```
