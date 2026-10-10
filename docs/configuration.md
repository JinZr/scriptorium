# Configuration

`scriptorium.toml` is committed with the manuscript. It names the LaTeX entrypoint, engine, and review roles. A run reads this file from the selected Git commit, never from uncommitted work.

```toml
[manuscript]
main = "main.tex"
engine = "pdflatex"
# Optional independent LaTeX documents, in review order:
supplements = ["supplement.tex", "appendix/main.tex"]

[profiles.quick]
roles = ["substantive_review", "copyedit"]

[profiles.full]
roles = ["substantive_review", "copyedit", "consistency", "figure_review"]
```

Supported engines are `pdflatex`, `xelatex`, and `lualatex`. Review roles are `substantive_review`, `copyedit`, `consistency`, and `figure_review`. Revision and verification tasks are prepared later by the workflow. `scriptorium init` writes a starter file and adds `.scriptorium/` to `.gitignore`.

Omit `manuscript.supplements` or use `[]` for a single document. Each listed path must be a distinct,
repository-relative `.tex` entrypoint in the same selected Git commit, compilable with `manuscript.engine`.
The main entrypoint comes first; supplements follow the configured order. Each document has its own dependency
scan and isolated build workspace. The bundle contains the union of those source closures, deduplicating shared
files, and a review PDF assembled from their compiled pages. Sources and navigation keep their original paths.
Missing inputs, unsafe paths, failed builds, or compiler inputs outside the frozen sources fail preparation.
Revision and verification rebuild all declared documents, including when only one document is edited.

This option does not discover supplements automatically, fetch external material, or accept standalone PDFs
or different engines per document. A source fragment already included by the main document does not need an
additional entry. Mentioning a supplement in prose does not freeze it. Declare independent materials before
starting the run; changing the working configuration cannot add materials to an existing frozen run.

`source-map.json` lists `compiled_pdf.documents`: each entry has an `entrypoint` (the resolved frozen source path), `start_page` in the review PDF,
and `page_count`. `task page --document supplement.tex --number 1` selects the first physical page of that
document. Its response includes the corresponding global `page` in `manuscript.pdf`; use that global page in
evidence and scope. These numbers are physical page positions, not printed page labels. Old frozen bundles
without a document index remain readable with their original global page numbers.

The selected Codex, Claude Code, or Antigravity CLI session owns model choice, effort, authentication, and spending. Supply its actual selection when claiming a task. Scriptorium stores this as external provenance; it cannot verify provider billing. `--budget-usd`, `--route`, and `.scriptorium/config.toml` belonged to the removed internal runner. Remove the old local config before starting a new run. The CLI rejects those old options or configuration with a migration error.

## Review brief

`run start --brief brief.json` freezes a short review brief agreed with the authors. The file is one UTF-8 JSON
object of at most 256 KB; it is not a manuscript source, so it does not need to be committed.

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

Only `venue_family` (`ml_conference`, `nature_family`, or `other`) and `stage` (`internal_draft`, `presubmission`,
`rebuttal_revision`, or `camera_ready`) are required. The lists default to empty and hold at most 10 non-blank items
of up to 500 characters; `venue` and `recorded_by` take up to 200 characters, `prior_reviews` 4,000, and
`severity_notes` 2,000. Omit a text field or use `null` rather than a blank string. Unknown keys are rejected.
`recorded_by` is the host session identity as declared; it is stored with the brief but not rendered into prompts.

`doctor` reports `detected_template`, the first known class or package declared by the main entrypoint or a
supplement (for example `neurips_2026`, `iclr2026_conference`, `icml2026`, `IEEEtran`, or `sn-jnl`) and the venue
family it suggests, or `unknown`. It is a default for the brief, not a venue policy.

## Doctor and frozen inputs

`doctor` checks the frozen project configuration, Git revision, dependency closure, local LaTeX tools, and compilation. It does not call a model. New runs freeze the project config, source identities, navigation, prompts, output schemas, and evidence contract. A frozen task's `input_digest` binds its prompt, schema, and bundle. A
review brief is rendered into every review role's frozen prompt, so it changes that prompt's digest and each review
task's `input_digest`; a run started without one keeps the same prompts as before briefs existed.
