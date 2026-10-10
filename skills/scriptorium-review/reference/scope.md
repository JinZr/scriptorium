# Review scope

New review outputs require `scope`. Older frozen schemas may lack it, in which case follow the schema `task show`
returns.

```json
{
  "completion": "partial",
  "checked": [
    {"source_path": "main.tex", "start_line": 1, "end_line": 120},
    {"source_path": "supplement.tex"}
  ],
  "outstanding": [{"source_path": "manuscript.pdf", "page": 4}],
  "limitations": ["Figure 3 was not inspected at full resolution."]
}
```

## Area shapes

- A text source area is a `source_path` from the source map, with both `start_line` and `end_line` or neither.
  Neither means the whole source. `end_line` cannot precede `start_line`.
- A rendered page area is `source_path: "manuscript.pdf"` with only a 1-based global `page`.
- A source area cannot carry `page`, and a page area cannot carry line fields.
- Every path must be a frozen source or `manuscript.pdf`. Describe a supplement that is mentioned but absent from
  the frozen manifest in `limitations` instead (or in `summary` for a revision or verification output).

## Completion

- `complete` requires an empty `outstanding` list.
- Use `partial` when material areas remain unchecked and `unknown` when you cannot tell. Explain unresolved links in
  `limitations`.
- Scope is your declaration, not proof of what the host displayed. `run report RUN_ID --part review_coverage_audit`
  compares declared checked areas with what the task tools returned. Read any listed gaps, or narrow the next
  `checked` to areas actually checked. Direct host reads are invisible to the audit, so describe them separately.

## What the audit sees

The audit counts `task read`, `task page`, and `task export` returns recorded for the task's own attempts up to the
latest accepted one. An export belongs to the attempt that made it; a different task of the run (copyedit,
consistency, figure) does not inherit it.

- Declare `checked` only from what you read through `task read`, `task page`, or your own `task export` in this task.
- Reading another task's export, or any file outside the task tools, is invisible to the audit. State it in
  `limitations`; never count it in `checked`. Otherwise the range shows as `declared_without_access` and `complete`
  looks unsupported.
- `declared_without_task_read` lists exported ranges too. An exported-only range is accessed but not read; the audit
  also lists it under `exported`.
- `task page --text` is a reading aid and is not a page render: the page stays in `declared_without_task_page`.
  Open the image from `task page` without `--text`.

## Graphics sources

A frozen source that is not text, such as `figures/fig1.pdf` or `fig2.png`, cannot be returned by `task read` or
`task page`, and takes no line fields.

- Inspect it as rendered where the manuscript places it. `task nav --command graphics` locates the placements; render
  those pages with `task page`, and use `--scale 3 --crop` for detail.
- Declare those global pages in `checked` as `manuscript.pdf` page areas.
- Never list the graphics file in `outstanding`: no tool can clear it, so the run could never reach `complete`.
  Validation does accept a graphics path as a whole-file area (the audit reports it under `not_comparable`), but a
  line range on it fails `scope.line_out_of_range`.
- State in `limitations` that the standalone file was inspected only as rendered on the manuscript pages.

## Continuations

After `run continue`, the new attempt includes the prior scope, summary, output digest, and recorded findings.
Report cumulative checked and remaining areas in the new scope. Earlier findings and decisions stay recorded.

Validation codes `scope.path_unknown`, `scope.line_out_of_range`, and `scope.page_out_of_range` name the area that
broke these rules.
