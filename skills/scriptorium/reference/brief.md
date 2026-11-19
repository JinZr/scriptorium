# Review brief format

`run start --brief brief.md` reads a Markdown file: a few front-matter lines, then a free body in the author's words.
It is a regular UTF-8 file of at most 64 KB, is not a manuscript source, and need not be committed.

## Template

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

## Front matter

The `key: value` lines between the first line `---` and the next `---`, each key at most once:

- `venue_family` (required): `ml_conference`, `nature_family`, or `other`.
- `stage` (required): `internal_draft`, `presubmission`, `rebuttal_revision`, or `camera_ready`.
- `venue` (optional): the venue's name, non-blank, up to 200 characters. Leave the line out when there is none.
- `recorded_by` (optional): this host session's identity as declared, up to 200 characters. It is kept with the
  frozen file but not rendered into prompts.

Any other key, a repeated key, or a line without a colon makes the brief invalid; `run start` then fails with
`configuration_error`, names every problem, and starts nothing. Fix the file and start again.

## Body

Free Markdown, and it may be empty. Scriptorium looks for no headings; the template's headings are a recommendation.
Write each section in the author's words and leave out a section the author has nothing for.

- **Claims the authors most want checked**: the two or three claims that must survive review, stated as the paper
  states them.
- **Weaknesses the authors already know**: limits the author accepts, such as a single seed or a missing baseline,
  so reviewers weigh them rather than rediscover them.
- **Out of scope this round**: what reviewers should leave alone. A reviewer still reports such an item if it meets
  the blocker definition.
- **Earlier reviewer comments**: what internal or external reviewers asked for, summarized by the author.
- **Severity notes**: how the author's target venue weighs particular problems, to calibrate severity. They do not
  change the severity rules.

The body is the author's context for reviewers, not evidence. It is rendered verbatim, after the venue family,
stage, and venue, into every review role's frozen prompt; the file's digest joins every task's input digest.
Reviewers find the file as `task/brief.md` in their `task export` directory. A run cannot change its brief; start a
new run instead.

`doctor` reports `detected_template`: the first known class or package declared by the main entrypoint or a
supplement, and the venue family it suggests, or `unknown`. It is a default for the brief, not a venue policy.
