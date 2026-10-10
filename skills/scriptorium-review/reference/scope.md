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

## Continuations

After `run continue`, the new attempt includes the prior scope, summary, output digest, and recorded findings.
Report cumulative checked and remaining areas in the new scope. Earlier findings and decisions stay recorded.

Validation codes `scope.path_unknown`, `scope.line_out_of_range`, and `scope.page_out_of_range` name the area that
broke these rules.
