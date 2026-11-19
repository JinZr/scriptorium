# Configuration

This page covers the project file `scriptorium.toml`, independent supplements, the review brief, and `doctor`.

## scriptorium.toml

`scriptorium.toml` is committed with the manuscript. It names the main LaTeX entrypoint, the engine, and the review
profiles. A run reads it from the selected commit, never from uncommitted work. `scriptorium init` writes this
starter file (without the comment lines) and adds `.scriptorium/` to `.gitignore`:

```toml
[manuscript]
main = "main.tex"
engine = "pdflatex"
# Optional independent LaTeX documents, in review order:
# supplements = ["supplement.tex", "appendix/main.tex"]

[profiles.quick]
roles = ["substantive_review", "copyedit"]

[profiles.full]
roles = ["substantive_review", "copyedit", "consistency", "figure_review"]
```

| Key | Values |
| --- | --- |
| `manuscript.main` | A repository-relative `.tex` file |
| `manuscript.engine` | `pdflatex`, `xelatex`, or `lualatex` |
| `manuscript.supplements` | Optional list of further repository-relative `.tex` entrypoints |
| `profiles.NAME.roles` | One or more distinct roles from `substantive_review`, `copyedit`, `consistency`, `figure_review` |

`run start` and `doctor` use the profile named by `--profile`. Writing and check tasks are not configured: the
workflow prepares one writing task per assignment of an approved plan, and one check task per candidate patch.

## Supplements

Declare an independent LaTeX document, such as a supplement compiled on its own, in `manuscript.supplements` and
commit the configuration before starting the run. Omit the key, or use `[]`, for a single document.

- Each path must be a distinct, repository-relative `.tex` entrypoint in the selected commit that compiles with
  `manuscript.engine`.
- The main entrypoint comes first and supplements follow in the listed order. Each document gets its own
  dependency scan and build; the bundle holds the union of their sources (shared files once), and the review PDF
  concatenates their pages. Sources and navigation keep their original paths.
- `doctor` and run preparation compile every declared document. Missing inputs, unsafe paths, failed builds, or
  compiler inputs outside the frozen sources fail preparation; a supplement failure never produces a main-only
  review.
- Supplements are not discovered automatically, fetched, or accepted as standalone PDFs, and every document uses
  the same engine. A file already included by the main document needs no entry, and mentioning a supplement in
  prose does not freeze it.
- A started run cannot gain documents; commit the change and start a new run.

How reviewers address supplement pages is in [operations](operations.md#pages).

## Review brief

Before a run, the host agent agrees a short brief with the author:

1. Run `doctor` and read `detected_template`, which suggests the venue family.
2. Ask only what that does not answer: the stage, the two or three claims to check first, what earlier reviewers
   said, known weaknesses, and what to ignore this round.
3. Show the draft. The author confirms it, edits it, or accepts the defaults.
4. Write `brief.md` and pass it to `run start --brief brief.md`.

The brief is a Markdown file: flat front matter, then a free body. It is not a manuscript source and need not be
committed.

```markdown
---
venue_family: ml_conference
venue: NeurIPS 2026
stage: presubmission
recorded_by: claude_code SESSION_ID
---
## Claims the authors most want checked
- The gain on benchmark one holds across seeds.

## Weaknesses the authors already know
- Only two benchmarks.

## Out of scope this round
- Related work.

## Earlier reviewer comments
Reviewer 2 asked for an ablation.

## Severity notes
Treat missing seeds as major.
```

- The file is a regular UTF-8 file, not a symbolic link, of at most 64 KB.
- The front matter is the `key: value` lines between a first line `---` and the next line `---`, each key at most
  once. Any other key, a repeated key, or a line without a colon is invalid.

| Key | Rule |
| --- | --- |
| `venue_family` | Required: `ml_conference`, `nature_family`, or `other` |
| `stage` | Required: `internal_draft`, `presubmission`, `rebuttal_revision`, or `camera_ready` |
| `venue` | Optional: non-blank, up to 200 characters |
| `recorded_by` | Optional: non-blank, up to 200 characters; the host session identity as declared |

The body is free Markdown and may be blank; Scriptorium looks for no headings in it. The headings above are the
recommended template from the operator skill. An invalid brief fails `run start` with `configuration_error`,
naming every problem, and starts nothing.

Every review prompt renders the brief once, before the severity rubric: a fixed lead-in, the venue family and stage,
the venue when given, a fixed rule that something the brief puts out of scope is reported only if it meets the
blocker definition, and the body verbatim when it is not blank, marked as the authors' context rather than
evidence. `recorded_by` is not rendered. Writing and check prompts do not include the brief. The brief cannot change
for the run; to review under a different brief, start a new run. How the brief is frozen is in
[architecture](architecture.md#freezing-a-run).

`detected_template` is the first known class or package declared by the main entrypoint or a supplement (for
example `neurips_2026`, `iclr2026_conference`, `icml2026`, `IEEEtran`, or `sn-jnl`) with the venue family it
suggests, or `unknown`. `run start` reports it too. It is a default for the brief, not a venue policy.

## Doctor

`scriptorium --json doctor --revision REV --profile P` checks that a commit is ready to review. It compiles the
committed revision in a temporary copy and calls no model. `--revision` defaults to `HEAD`; without `--profile` it
checks `full`, or the first profile when there is no `full`.

| Check | Verifies |
| --- | --- |
| `git_repository` | The directory is a Git work tree |
| `frozen_revision` | The revision resolves and can be snapshotted |
| `tracked_project_config` | `scriptorium.toml` is committed in that revision and valid |
| `manuscript_main` | The main entrypoint is in that revision |
| `latexmk`, `latex_engine` | The tools are on `PATH` |
| `manuscript_sources` | The dependency closure of every entrypoint resolves |
| `manuscript_compile` | Every document compiles from the frozen sources |
| `review_profile` | The profile exists |
| `run_state` | The run state directory |

When every check passes, the response has `ok: true`, the `checks`, and `detected_template`. Otherwise the command
fails with `configuration_error` (exit 2) or `infrastructure_error` (exit 3) and a message listing the failed checks.
Common causes are a wrong `main` path, figures or bibliography files that were never committed, and an engine that
is not installed: fix them, commit, and run `doctor` again.
