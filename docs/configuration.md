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

`run start` and `doctor` use the profile named by `--profile`. Writing and verification tasks are not configured:
the workflow prepares one writing task per assignment of an approved plan, and a verification task when a patch is
approved.

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
4. Write `brief.json` and pass it to `run start --brief brief.json`.

The brief is one UTF-8 JSON object of at most 256 KB. It is not a manuscript source and need not be committed.

```json
{
  "venue_family": "ml_conference",
  "venue": "NeurIPS 2026",
  "stage": "presubmission",
  "priority_claims": ["The method improves accuracy on all three benchmarks."],
  "known_weaknesses": ["The ablation uses a single seed."],
  "prior_reviews": "An internal reviewer asked for a stronger baseline.",
  "ignore": ["Checklist answers"],
  "severity_notes": "Checklist items were waived last time.",
  "recorded_by": "claude_code SESSION_ID"
}
```

| Field | Rule |
| --- | --- |
| `venue_family` | Required: `ml_conference`, `nature_family`, or `other` |
| `stage` | Required: `internal_draft`, `presubmission`, `rebuttal_revision`, or `camera_ready` |
| `priority_claims`, `known_weaknesses`, `ignore` | Default empty; at most 10 non-blank items of up to 500 characters |
| `venue`, `recorded_by` | Up to 200 characters |
| `prior_reviews` | Up to 4,000 characters |
| `severity_notes` | Up to 2,000 characters |

Omit an optional text field or use `null`; a blank string is rejected, and so are unknown keys. `recorded_by` is
the host session identity as declared; it is stored with the brief but not rendered into prompts. The brief cannot
change for the run; to review under a different brief, start a new run. How the brief is frozen is in
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
