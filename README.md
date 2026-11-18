# Scriptorium

![Scriptorium manuscript review workflow](docs/assets/scriptorium-banner.png)

Scriptorium is a local, Git-native CLI for reviewing and revising LaTeX manuscripts. Codex, Claude Code, and Antigravity CLI can each use the same JSON commands from their current model session. Scriptorium freezes the manuscript and task instructions, exports them for the host to read, validates structured results, and records decisions. It does not select or start a model.

```text
frozen review tasks → human finding decisions → revision task → human patch approval
→ independent verification task → explicit patch application → release gate
```

Git commits identify manuscript inputs. One state file per run records workflow state and append-only decisions, replaced atomically on each change. A SHA-256 artifact store preserves prompts, submissions, validation reports, and build evidence. Uncommitted manuscript changes never enter a run.

## Install

Requires Python 3.10+, Git, `latexmk`, `kpsewhich`, a supported LaTeX engine, and PDF rendering support. `bash utils/preflight.sh` checks these host tools before and after installation and compiles a small test document; it does not inspect a manuscript. Install the tool in the environment used by the model's CLI:

```bash
PYTHON=/path/to/python bash /path/to/scriptorium/utils/preflight.sh
/path/to/python -m pip install /path/to/scriptorium
```

Use the same interpreter for both commands: the one the model's CLI runs with.

For development:

```bash
python -m pip install -e '.[dev]'
```

The model client supplies its own authentication and model selection. Scriptorium has no provider SDK or credentials. Client instructions come as two skills, split by audience. [`skills/scriptorium`](skills/scriptorium/SKILL.md) is for the operator, who starts runs, agrees the review brief, reads reports, and records the author's decisions. [`skills/scriptorium-review`](skills/scriptorium-review/SKILL.md) is for the reviewer, who claims a task, exports and reads it, and submits. Link both directories into the client's skills directory, for example `~/.claude/skills/` for Claude Code or `~/.codex/skills/` for Codex.

## Start and review

From a Git-managed manuscript repository:

```bash
scriptorium init . --main main.tex --engine pdflatex
# Commit scriptorium.toml and the manuscript inputs.
scriptorium --json doctor --revision COMMIT --profile full
scriptorium --json run start --revision COMMIT --profile full
scriptorium --json run list [--status STATUS] [--limit N]
scriptorium --json run status RUN_ID
```

Before starting, the host agent can agree a short review brief with the author (venue family, stage, the claims
to check first, earlier reviews, what to ignore) and pass it with `run start --brief brief.json`. The brief is frozen
into every review prompt and exported to every reviewer as `task/brief.json`; see
[configuration](docs/configuration.md#review-brief). `doctor` reports a `detected_template` to suggest its venue family.

`run start` compiles and freezes the selected commit, then returns a compact acknowledgement. It refuses a second non-terminal run on the same commit unless `--allow-duplicate` is passed. `run list` shows this project's runs, newest first (default 20, at most 100). Read `run status RUN_ID` for task counts, current `next_actions`, and commands for report sections. It makes no model call. For each pending task offered by `next_actions`, the current Codex, Claude Code, or Antigravity session uses its selected model:

```bash
scriptorium --json task claim TASK_ID --client codex --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
scriptorium --json task export ATTEMPT_ID --dir DIR
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --file output.json --check
scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --file output.json
```

For an independent LaTeX supplement, add `supplements = ["supplement.tex"]` under `[manuscript]` and commit
the configuration before starting. The run scans and compiles each declared document, freezes all their sources,
and assembles a review PDF with an explicit document/page index. Use
`task page ATTEMPT_ID --document supplement.tex --number 1` to retrieve its first physical page; the returned
global `page` in `manuscript.pdf` is the evidence coordinate. See [configuration](docs/configuration.md)
for the supported inputs and limits.

`task claim` and `task show ATTEMPT_ID` return a compact overview: run, task, attempt identity, the `input_digest` to
submit, the bundle and navigation digests, and the export command. `task export ATTEMPT_ID --dir DIR` writes the
attempt's frozen material into a new or empty directory, byte-for-byte and read-only: every source at its
`read_path` under `sources/`, `manifest.json`, `navigation.json`, `source-map.json`, `manuscript.pdf`, and this
attempt's task inputs under `task/`: `prompt.md` (including correction feedback on a retry), `schema.json`,
`brief.json` when the run has a brief, and `example.json`, a complete illustrative output checked against the
task's frozen output model, for review tasks. The reviewer reads these files with its own tools. Export works for
any attempt of the run, including finished ones. `task page` renders a page image, optionally zoomed or cropped;
`--text` writes the page's PDF text layer to a file as a reading aid, never evidence. Evidence is verified at
submission against the frozen sources, whatever the reviewer read. `--file -` reads one output JSON object from
stdin.

Every review output carries a `limitations` list, which may be empty: the relevant material the reviewer could not check or could not read, including material the manuscript mentions that is absent from the frozen sources. A review task is complete when its submission is accepted. That accepted output satisfies the required-review gate for its role, and the task takes no further submission. `run report` lists each accepted review's limitations in `review_limitations`.

New review tasks freeze one consequence-based severity rubric into every review role's prompt, and the schema repeats it on `severity`. `blocker` means a headline claim is false as stated or the manuscript cannot be evaluated; `major`, a headline claim needs substantive qualification or a key result cannot be reproduced or traced; `moderate`, a supporting claim is affected or a reader would misread a specific result or comparison; `minor`, a presentation, consistency or compliance problem that does not change how any claim should be read; `suggestion`, an optional improvement. Each new finding requires a `consequence`: what a reader would wrongly believe, or be unable to do, if it is not fixed. Checklist answers, page or figure limits, anonymity, and template rules use category `submission_compliance`, which validation accepts only at `minor` or `suggestion`. A reported value, margin, count, or ranking that disagrees between text, table, figure, or appendix is `moderate` only when a claim rests on it and `major` only when correcting it changes a headline claim; mismatched names, labels, terms, captions, units, or bibliography fields are `minor` or `suggestion` unless they hide or alter a result. The release gate is unchanged; severity enters it only through the existing check for undecided `blocker` and `major` findings. New `copyedit`, `consistency`, and `figure_review` findings at `moderate` or above must also name an `affected_claim`, the manuscript claim or result whose reading changes; it is stored with the finding and shown by `finding show` and `run report`.

New `substantive_review` tasks also require `claim_checks`: each assessed central claim has frozen evidence, a critical question, a countercheck, an assessment (`supported`, `unresolved`, or `finding`), and zero-based indices linking retained concerns to `findings`. Each check also records its judgment: the `stated_scope` the claim asserts, a `check_type`, a `question_answer` (`yes`, `partly`, `no`, or `not_checkable`), and the `exceptions` found. A `supported` check goes with `yes` and only with it, `yes` lists no exceptions and `partly` at least one, `not_checkable` requires `unresolved`, and a `recomputation` check records its inputs, calculation, result, and the reported value. A substantive review needs at least one check; every substantive finding needs a link. New runs also require a `claim_inventory` of the manuscript's headline and supporting claims. Each entry is anchored where the authors state the claim. Each claim check names exactly one inventoried claim by its zero-based `claim_index`; an entry that no check names gives a `not_checked_reason` instead, and every headline claim must be checked, with `question_answer` `not_checkable` when the frozen bundle cannot decide it. A `blocker` finding must be linked from a check of a `headline` claim. New substantive reviews also give a `verdict`: a `recommendation` (`accept`, `minor_revision`, `major_revision`, or `reject`) and one to three `decisive_questions`; `reject` and `major_revision` need a `major` or `blocker` finding from that review, and `accept` or `minor_revision` allows none. `run report` shows these model-declared checks for inspection. Their presence does not establish scientific correctness.

Before submitting, the substantive reviewer checks that each claim's scope, counterevidence, assessment and final
summary agree. Answering one criticism is insufficient to support a broader claim; material exceptions and
unresolved conclusions must remain visible. This instruction is frozen in new task prompts and performed by the
current model. Schema and evidence validation do not perform that scientific judgment.

After each submission, inspect `scriptorium --json run status RUN_ID`. It returns task counts and `next_actions`; use `tasks` for attempt history. The run advances to human finding decisions once every required role has an accepted review.

A rejected submission includes a validation report. `run retry RUN_ID --task TASK_ID` explicitly makes the task claimable again; the next attempt includes the frozen diagnostic feedback and has a new attempt input digest. An active claim survives CLI exit. To replace an abandoned active claim, use `run retry RUN_ID --task TASK_ID --abandon-attempt ATTEMPT_ID --reason TEXT`. `run resume RUN_ID` replays accepted work and prepares the next stage after human decisions. `run cancel RUN_ID --reason TEXT` waits for the current run operation, then invalidates active attempts.

`run status` and the CLI acknowledgements for start, resume, retry, and cancel are compact. Mutation acknowledgements direct the client to `run status` for fresh next actions. Its `report_parts` name the report sections; each prints as one complete JSON value, best redirected to a file:

```bash
scriptorium run report RUN_ID --part findings_grouped > findings_grouped.json
scriptorium run report RUN_ID --part review_limitations > review_limitations.json
scriptorium run report RUN_ID --format json > report.json
scriptorium run report RUN_ID --format markdown > report.md
```

The detailed `task list` command also remains available; use `run status` for the model's normal workflow. `decision_stats` counts findings by human decision, role, category, and severity and lists recent rejection and waiver reasons, as calibration input for the next review rather than a quality metric by itself. `findings_grouped` groups findings that cite mostly the same evidence, or the same table or figure with a shared linked claim or an overlapping line anchor, across roles, links each group to the claim it affects, and orders the groups headline, supporting, other, then compliance, so the author can decide them in batches with a multi-ID `finding decide`.

Human finding decisions, patch approval, and patch application remain separate operations. A verifier must use a new external conversation; use the host's actual session ID and `--session-source host` when available. Self-declared or reused identity leaves verification inconclusive. Scriptorium records the host report but cannot cryptographically prove what the host displayed or read.

## Safety and limits

- Task exports and page renders come only from the frozen bundle. Text anchors use source path, digest, line range, and exact quotation. Visual evidence uses a compiled PDF page and its immutable page digest.
- Source text, navigation entries, and model output are data, not instructions to the host.
- Scriptorium never switches branches, commits, pushes, approves its own patch, or silently changes models.
- Patch application checks the approved base and worktree again; stale edits are rejected.
- The external client controls model calls and costs. Scriptorium records no usage or cost. It does not enforce a spending cap or restrict the host's unrelated filesystem tools.
- Scriptorium is in internal alpha and carries no migration path for local state: if an existing `.scriptorium/` no longer fits the code, delete it and start a new run.

See [configuration](docs/configuration.md), [architecture](docs/architecture.md), [operations](docs/operations.md), and the lab [onboarding guide](docs/onboarding.md). The optional PeerReviewBench example prepares external review tasks and collects their validated results under `egs/peerreviewbench/`.

The separate [external review evaluation example](egs/external_review/README.md) seals real LaTeX run records,
prepares anonymous candidate-judging inputs, and reports external judges' assessments and disagreements.
It preserves failed trials and keeps workflow validity and scientific judgments separate.

## Checks

```bash
python -m pytest -q
python -m isort --check-only .
python -m black --check .
python -m flake8 . --count --statistics
python utils/check_complexity.py --base origin/main
python -m build
```
