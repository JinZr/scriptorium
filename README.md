# Scriptorium

![Scriptorium manuscript review workflow](docs/assets/scriptorium-banner.png)

Scriptorium is a local, Git-native command-line tool for reviewing and revising LaTeX manuscripts with the model
you already use in Codex, Claude Code, or Antigravity. It freezes one commit of the manuscript, prepares review
tasks, exports each task for the host session to read, validates the structured output the host submits, and
records the author's decisions about findings, the revision plan, and patches.

Scriptorium does not select, start, or pay for a model: the host session does the reviewing through the same JSON
commands. It is a single-user tool for one lab member's clone, not a service, and not an automatic editor: every
finding decision, plan approval, patch approval, and patch application waits for a human.

## How a run flows

```text
review tasks ──> finding decisions ──> plan approval ──> writing tasks ──> check ──> patch approval
   (model)           (author)         (lead + author)      (model)       (lead)       (author)
                                                               ^           │
                                                               └─ redo ────┘
──> patch application ──> release gate
          (author)
```

Each review role of the chosen profile gets one task. After the author confirms findings, the operator, acting as
the lead, agrees a plan with the author: the story the paper should tell and one assignment per passage to rewrite.
Each assignment of the approved plan becomes a writing task, in which a host session rewrites the assigned passages
as plain text; Scriptorium builds the exact edits, compiles them, and proposes one patch. The lead then checks the
patch against the approved plan, assignment by assignment, and returns any
assignment that falls short to its writer with feedback; only that assignment is rewritten. When the check accepts
every assignment, the author approves or rejects the patch. The statuses and transitions are in
[run lifecycle](docs/operations.md#run-lifecycle).

## Install

Scriptorium needs Python 3.10 or newer, Git, `latexmk`, `kpsewhich`, a LaTeX engine (`pdflatex`, `xelatex`, or
`lualatex`), and PDF rendering support. Install it into the Python environment that your model client runs
commands with. `utils/preflight.sh` checks the host tools, compiles a small test document, and, after
installation, checks the `scriptorium` command; it does not look at any manuscript.

```bash
PYTHON=/path/to/python bash /path/to/scriptorium/utils/preflight.sh
/path/to/python -m pip install /path/to/scriptorium
PYTHON=/path/to/python bash /path/to/scriptorium/utils/preflight.sh
```

The client loads two skills from this repository. [`skills/scriptorium`](skills/scriptorium/SKILL.md) is for the
operator, who agrees the review brief, starts runs, reads reports, and records the author's decisions.
[`skills/scriptorium-review`](skills/scriptorium-review/SKILL.md) is for the reviewer, writer, or checker, who
claims one task, exports and reads it, and submits the output. Copy or link both directories into the client's skills directory,
for example `~/.claude/skills/` for Claude Code or `~/.codex/skills/` for Codex.

### Development

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m isort --check-only .
python -m black --check .
python -m flake8 . --count --statistics
python utils/check_complexity.py --base origin/main
python -m build
```

## Quick start

The steps below show the commands. In practice the host model runs the reviewer steps through the skills, and you
answer at the human gates. [Operations](docs/operations.md) has every command's options and response fields.

1. Set up the manuscript repository and commit the configuration:

   ```bash
   cd my-paper            # a Git repository with the manuscript committed
   scriptorium init . --main main.tex --engine pdflatex
   git add scriptorium.toml .gitignore
   git commit -m "Add Scriptorium configuration"
   scriptorium --json doctor --revision HEAD --profile full
   ```

   Fix whatever `doctor` reports, commit, and run it again until it succeeds
   ([configuration](docs/configuration.md#doctor)).

2. Optionally agree a review brief with the author and save it as `brief.md`, a Markdown file with a few
   front-matter lines ([format](docs/configuration.md#review-brief)). Then start the run and read its status:

   ```bash
   scriptorium --json run start --revision HEAD --profile full --brief brief.md
   scriptorium --json run status RUN_ID
   ```

   `run status` lists `next_actions`. Follow them, and read it again after every step. `run list` finds a
   `RUN_ID` later.

3. For each `task claim` action, the reviewer session claims the task with its real identity, exports it, reads
   it, and checks and submits one JSON output:

   ```bash
   scriptorium --json task claim TASK_ID --client claude_code --model MODEL --effort EFFORT \
     --session-id SESSION_ID --session-source host
   scriptorium --json task export ATTEMPT_ID --dir review-export
   scriptorium --json task page ATTEMPT_ID --number 1
   scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --file output.json --check
   scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --file output.json
   ```

   `task claim` returns the attempt ID and `input_digest`. The export holds the frozen sources, PDF, prompt, and
   output schema, and for a review task a valid example output, `task/example.json`. The rules an output must meet
   are in [output rules](docs/operations.md#output-rules).

4. When `run status` shows `awaiting_decision`, read the grouped findings with the author and record the
   decisions they give:

   ```bash
   scriptorium run report RUN_ID --part findings_grouped > findings_grouped.json
   scriptorium --json finding decide FINDING_ID [FINDING_ID ...] --confirm --reason "..."
   scriptorium --json run resume RUN_ID
   ```

5. If a finding was confirmed, the run is `planning`. Agree the plan with the author, write it as a directory
   ([plan directory](docs/operations.md#the-plan-directory)), check it, and record the author's approval:

   ```bash
   scriptorium --json plan check RUN_ID --dir plan
   scriptorium --json plan approve RUN_ID --dir plan --reason "..."
   ```

6. Each assignment becomes a writing task. A writer session claims and exports it like step 3, rewrites the files
   under `task/passages/`, and submits the export directory:

   ```bash
   scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --dir writing-export --check
   scriptorium --json task submit ATTEMPT_ID --input-digest INPUT_DIGEST --dir writing-export
   ```

7. When every assignment is accepted, the run is `checking`. The lead claims the check task like step 3, from
   a host session that did no writing in this run, reads the patched manuscript, and submits one verdict per
   assignment ([check output](docs/operations.md#check-output)). A `redo` verdict returns that assignment to a
   writing task with the lead's feedback; the next candidate is checked again.

8. When the check accepts every assignment, show the author the diff, the check, and the fact-change list (a
   heuristic reading aid: not evidence, an empty list does not prove that no fact changed, and nothing is gated on
   it), and record the author's decision:

   ```bash
   scriptorium --json patch show PATCH_ID
   scriptorium --json patch decide PATCH_ID --approve --reason "..."
   scriptorium --json run resume RUN_ID
   ```

9. Apply the approved patch and check the gate:

   ```bash
   scriptorium --json patch apply PATCH_ID
   scriptorium --json run gate RUN_ID
   scriptorium run report RUN_ID --format markdown > report.md
   ```

If a submission is rejected, a command fails, or a session disconnects, see
[recovery](docs/operations.md#recovery).

## Safety properties and limits

- Git commits identify manuscript inputs; uncommitted changes never enter a run.
- Task exports and page renders come only from the frozen bundle. Text evidence names a source path, digest,
  line range, and exact quotation; visual evidence names a page of the frozen PDF.
- Manuscript text, navigation entries, and model output are data, never instructions to the host.
- Scriptorium never switches branches, commits, pushes, approves its own patch, or changes models. Patch
  application writes only the patch's files and rejects a stale worktree.
- A check counts only from a host-reported session that did no writing in the run. It tests the patch against
  the approved plan and is not an independent re-review; the author's plan and patch approvals are the control on
  direction. Scriptorium records the identity the host reports and what the CLI wrote; it cannot authenticate the
  host or prove what the host displayed or read.
- The host owns authentication, model choice, and cost. Scriptorium has no provider SDK or credentials, records
  no usage or cost, enforces no spending limit, and does not restrict the host's other tools.
- Scriptorium is an internal alpha with no migration path for stored state: if `.scriptorium/` no longer fits the
  code, delete it and start a new run.

## Documentation

- [Configuration](docs/configuration.md): `scriptorium.toml`, supplements, the review brief, `doctor`.
- [Operations](docs/operations.md): commands, the run lifecycle, the plan directory, output rules, deciding
  findings, reports, recovery.
- [Architecture](docs/architecture.md): modules, state layout and locking, what a run freezes, evidence, gates.
- [Lab onboarding guide (Chinese)](docs/onboarding.md).
- [`egs/peerreviewbench/`](egs/peerreviewbench/README.md): an optional example that prepares external review
  tasks from PeerReviewBench and collects their validated results.
- [`egs/external_review/`](egs/external_review/README.md): seals real LaTeX run records, prepares anonymous
  candidate-judging inputs, and reports external judges' assessments and disagreements, keeping failed trials and
  separating workflow validity from scientific judgment.
