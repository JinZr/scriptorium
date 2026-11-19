# Plan directory

Write the plan with the author in any directory, then `plan check RUN_ID --dir DIR`. The full rules are in
`docs/operations.md` under "The plan directory".

```text
DIR/plan.md                 the story, in free Markdown
DIR/assignments/NAME.md     one per writing task; NAME is lowercase letters, digits, and hyphens
```

## `plan.md` template

Scriptorium does not read the headings; the writers do. Keep it short.

```markdown
# Story

Three sentences: what the paper shows, under which conditions, and why it matters.

# Claims and their evidence

- Claim as it should read after revision: the table, figure, or section that supports it.

# Intent per section

- Abstract: ...
- Results: ...

# Facts that must not change

- Every number in Table 2, the benchmark names, and the citations in Section 4.
```

## Assignment example

`assignments/results.md`:

```markdown
---
target: sections/results.tex:41-58
target: sections/abstract.tex:6-7
findings: finding_abc, finding_def
---
Limit the main conclusion to benchmark one. Keep the table numbers, citations, and the tab:main label.
```

- `target` is a bare `source_path` from the run's source map with an inclusive 1-based line range; repeat the line
  for more ranges. Ranges must not overlap, within or across assignments.
- `findings` lists confirmed finding IDs and may be empty (`findings:`) or absent for a change with no finding.
  Every confirmed finding appears in some assignment; waive one the author does not want changed.
- The body is the writer's instruction. Say what to change and what to keep; the writer sees `plan.md` too.
- Check each range against the echoed `first_line` and `last_line` before asking the author to approve.
- When you revise a plan, unchanged assignments keep their current text; changing `plan.md` rewrites every
  assignment.
