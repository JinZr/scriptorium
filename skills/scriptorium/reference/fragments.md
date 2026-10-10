# Reading report parts

`run status RUN_ID` lists the available parts under `report_parts`. They are the top-level keys of the full JSON
report, such as `run`, `tasks`, `findings`, `findings_grouped`, `decision_stats`, `patches`, `review_scopes`,
`review_coverage_audit`, and `gate`. `run report --help` lists the parts this installation accepts.

## Traversal

- Each `run report RUN_ID --part PART` response carries `text`, a character `offset`, `total_chars`, the part's
  `digest`, the whole report's `report_digest`, and a `next_command`.
- Follow `next_command` unchanged until it is null. Concatenate `text` in offset order without separators, then
  parse the complete JSON value. A fragment can end inside a record or a quoted string.
- Offsets count Unicode characters, not UTF-8 bytes. If you use a wrapper, replace only the executable in the
  returned command.
- Continuations carry `--report-digest`; do not remove it. A changed report is rejected rather than mixed with
  earlier fragments. Discard the old fragments and restart at offset zero without the digest.
- To read several parts from one report state, pass the first part's `report_digest` on the first request for each
  other part as well.
- `run status`, run mutation acknowledgements, and report-part responses are at most 7,000 UTF-8 bytes.

## Unbounded output

Full exports have no byte bound and are meant for files or human reading:

```bash
scriptorium run report RUN_ID --format json > report.json
scriptorium run report RUN_ID --format markdown > report.md
```

`--part` cannot be combined with `--format`. `task list`, `finding list`, `finding show`, and `patch show` are also
unbounded; read the bounded `tasks`, `findings`, or `patches` parts instead.

## Run errors

`run status` sets `has_error` when the run recorded an error. Read the `run` part for its full text and the
`validation_reports` or `gate` parts for diagnostics. Repair the external cause, such as an uncommitted file or a
missing LaTeX tool, then use the action `run status` offers. Never repair a run by editing `.scriptorium/`.
