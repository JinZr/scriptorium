# Operations and recovery

Run commands from the manuscript Git repository and use `--json` for machine-readable output. The JSON envelope has `ok` and either `data` or a stable error `code` and `message`.

```bash
scriptorium --json doctor --revision COMMIT --profile full
scriptorium --json run start --revision COMMIT --profile full
scriptorium --json run status RUN_ID
scriptorium --json task claim TASK_ID --client codex --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
scriptorium --json task show ATTEMPT_ID
```

`run status` returns a compact run identity and status, task counts, finding/patch counts, `has_error`, current `next_actions`, and `report_parts` commands. It omits frozen configuration and attempt history; these remain available through the `run` and `tasks` report sections. Costs remain unknown for external runs. Historical SDK runs return `execution: legacy_read_only` with no executable next actions.

The CLI responses for `run start/resume/retry/continue/cancel` summarize the state returned by that mutation and direct the client to `run status` for current actions. They do not include the full run configuration. After submission, use `run status` to discover continuation or human gates; `task list` retains its detailed, unbounded output for explicit inspection.

Use `run report RUN_ID --part PART` for bounded report reading. Available parts are listed in `run status` under `report_parts`; they correspond to the top-level keys of the complete JSON report, including `run`, `tasks`, `findings`, `patches`, `events`, `validation_reports`, `review_scopes`, `review_claim_checks`, `review_tool_access`, `review_coverage_audit`, and `gate`. Each response includes `text`, character `offset`, `total_chars`, the part's SHA-256 `digest`, the whole `report_digest`, and a `next_command`. Follow it until null, concatenate without separators, and parse the JSON value. A part can split inside a record or quoted string.

Report reads hold the run lock while building each response and do not append access events or change workflow state. A nonzero `--offset` requires the previous `--report-digest`. If any report content changes, the continuation fails explicitly: restart at offset zero without that digest and discard the old fragments. To read multiple sections from the same report state, also pass that digest on their first requests. A matching digest establishes consistent report content, not scientific correctness. Corrupt artifacts still fail validation. Use `has_error` and the `run`, `validation_reports`, and `gate` sections to inspect failures.

`--part` cannot be combined with `--format`. Complete exports remain available as `scriptorium run report RUN_ID --format json > report.json` or `--format markdown > report.md`; these outputs have no byte bound. Explicit `task list`, `finding list/show`, and `patch show` output is also unbounded. At a human gate, inspect findings through the bounded report sections before asking for a decision.

A claimed attempt remains active when the CLI exits. Claim and plain `task show` return a compact overview: `data.input_digest` is the digest to submit, and `data.inputs.prompt.command` and `data.inputs.schema.command` retrieve the frozen inputs. Run each command and follow its `data.next_command` until null. Concatenate each part's `data.text` fragments in `data.offset` order; offsets count Unicode characters, not UTF-8 bytes. Parse the complete schema text as JSON and inspect its required fields before writing an output. Each part includes its immutable digest and total character count. `task show ATTEMPT_ID --part prompt|schema|source-map --offset N` also accepts an explicit continuation offset. Use `data.source_map_command` or `task show ATTEMPT_ID --part source-map` for the `sources` array of evidence paths and digests. This returns the exact frozen source-map JSON, including for completed, failed, or interrupted attempts; it does not reactivate them. `task read/search/page` still require an active attempt. The attempt's input digest must accompany submission:

```bash
scriptorium --json task search ATTEMPT_ID --query phrase --cursor 0 --limit 20
scriptorium --json task read ATTEMPT_ID --path main.tex --start-line 1 --max-lines 40
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest DIGEST --file answer.json
```

`task read` and `task search --path` support `manifest.json`, `navigation.json`, `source-map.json`, and text sources named in the source map (either `source_path` or `read_path`). A bare `source_path` takes priority if it also names another source’s `read_path`; search results, continuation paths, and access events use the canonical `source_path`. `--max-chars` is a ceiling of at most 8,000 characters, not a promised chunk size. Successful JSON responses for claim, show, read, search, and page are limited to 7,000 UTF-8 bytes including JSON escaping, metadata, envelope, and newline. Metadata that cannot fit fails explicitly. This bound leaves room under the observed host truncation threshold; it cannot guarantee delivery by every host. The same bound covers `run status`, run mutation acknowledgements, and `run report --part`. Full report exports, detailed lists, and submission diagnostics are not covered by this bound.

Read returns `next_line`, `next_offset`, and a shell-quoted `next_command` when more text remains. Execute that command unchanged (or use the same arguments with your installed CLI entrypoint). A nonzero offset continues the same line; incrementing the line skips its unread tail. Search returns `total_matches`, `next_cursor`, and `next_command`; the byte limit may reduce the number of returned matches below `--limit`. Follow continuations until null when traversing an entire file or search. Access events record only the returned fragments and matches. `task page` returns the exact rendered image path and digest; the host must actually open the image for a visual review.

External review sessions have no wall-clock deadline by default. For AGY print-mode validation, use `--print-timeout 0` and wait without a subprocess timeout. A host process exit or a success message is not review completion: inspect the accepted submission receipt and `run status`, continue accepted partial scope through `run continue`, and stop at an explicit human gate. If the host disconnects, inspect the durable attempt and reconnect to the same session before deciding whether explicit abandonment is needed. Paid-call and correction allowances remain separate from elapsed time; `--help` calls are not material retrievals.

`task submit --file` accepts UTF-8 JSON up to 2 MB from a file or stdin. The CLI rejects larger input before decoding or passing it to the workflow.

For a new review task, include `scope` alongside `summary` and `findings`. This minimal partial output illustrates `scope` only; a new `substantive_review` also requires `claim_checks`. See the [shared skill's full JSON example](../skills/scriptorium/SKILL.md#example-substantive-review-output) for a finding, its exact text anchor, and its zero-based claim-check link. The task's reconstructed frozen schema takes precedence over either example:

```json
{
  "summary": "The main result was checked; visual review remains open.",
  "findings": [],
  "scope": {
    "completion": "partial",
    "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 40}],
    "outstanding": [{"source_path": "manuscript.pdf", "page": 2}],
    "limitations": ["Page 2 was returned but not visually opened."]
  }
}
```

Use bare frozen `source_path` values. For a text source, omit both line endpoints to declare the whole file, or supply both as an inclusive range. For the compiled PDF, use `source_path: "manuscript.pdf"` and a 1-based `page`. `complete` cannot include outstanding areas. Mark incomplete or uncertain work honestly. The report keeps this declaration separate from successful task-tool returns, including returns from failed review attempts. Its coverage audit compares the latest accepted scope with returns through that attempt; search excerpts do not count as full reads, line summaries may represent partial fragments, and missing task-tool returns may reflect direct host access. Neither declaration nor tool returns prove that the host opened or understood material. Old runs keep their frozen review schema and report scope as not reported. Historical SDK runs have unknown tool-access counts. For scoped reviews, partial or unknown scope does not satisfy the required-review gate.

After a partial submission, read `run report RUN_ID --part review_coverage_audit`, following every continuation. Use the reconstructed audit to find checked ranges or pages without matching task-tool returns; in the next continuation, read the missing ranges or correct the checked declaration to include only areas actually checked across attempts. Record a separate explanation for direct host access that the audit cannot observe. Mentioned material absent from the frozen source map belongs in `scope.limitations`, not in `scope.outstanding`.

For a new `substantive_review` task, also include `claim_checks`. Each entry has `claim`, `evidence` (the same exact source or PDF anchors used by findings), `critical_question`, `countercheck`, `assessment` (`supported`, `unresolved`, or `finding`), and `finding_indices`. The indices are zero-based positions in the submitted `findings` array. Use an empty index list for supported or unresolved checks; a finding check must link at least one finding, and every finding must be linked. A complete review requires at least one claim check; a partial review may submit an empty list. The frozen schema returned by `task show` is authoritative.

Each report entry for claim checks includes `submitted_findings` in that attempt's original order. Its `finding_indices` refer to this list, not the report-wide findings, which may be severity-sorted or deduplicated across attempts.

After each `task submit`, use `run status RUN_ID` and follow `next_actions`. An accepted scoped review with `partial` or `unknown` is a durable checkpoint: its findings are retained, but the role is not finished and the run remains in `reviewing`. Continue that role explicitly:

```bash
scriptorium --json run continue RUN_ID --task TASK_ID
scriptorium --json task claim TASK_ID --client CLIENT --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
```

Before reopening, Scriptorium replays the accepted output so a crash after submission cannot lose findings. The new claim includes the previous accepted scope, summary, output digest, and recorded finding details in its frozen prompt. Its input digest differs from the previous attempt. Continue searching and reading outstanding relevant material, then report cumulative checked and outstanding areas. Prior accepted outputs, findings, and human decisions stay recorded. Submit only newly discovered findings, except when a new claim check needs to link an existing concern: include that finding in the current output and link its index. Repeated findings with the same category, severity, title, claim, and evidence retain their original identity even if the explanation, suggested action, or confidence changes. Repeating a superseded attempt's submission is rejected. An invalid continuation uses the ordinary explicit retry and receives both prior scope and validation diagnostics. If an older run already reached `awaiting_decision` with an incomplete scoped review, `run continue` returns it to `reviewing`; `run resume` does not advance it into revision. Completed or cancelled runs cannot be reopened. If an accepted output awaits replay after a process exit, `run status` suggests `run resume`. A `next_actions` entry marked `requires_human_decision` identifies a finding decision, patch approval, or explicit patch application gate.

When output is invalid, inspect every issue in `data.validation_report.issues`. No findings, patch, or verification are accepted from that attempt. Correct the output against the same frozen schema and source map, then explicitly run `scriptorium --json run retry RUN_ID --task TASK_ID`. Claim again, read the new `task show`, and submit with the **new attempt ID and input digest**; the failed attempt cannot accept a corrected output. The next prompt includes the durable diagnostics. If an active external conversation was abandoned, run `scriptorium --json run retry RUN_ID --task TASK_ID --abandon-attempt ATTEMPT_ID --reason TEXT` to interrupt only that attempt and make the task claimable again. Its late submission is rejected. Repeated identical submissions are idempotent. Different output for a terminal attempt and output for a superseded or cancelled attempt are rejected.

After review tasks complete, use `finding list/show/decide` for human decisions, then `run resume`. A confirmed finding prepares a revision task. After submitting revision output, inspect the patch and decide it explicitly; `run status` then suggests `run resume` to advance an approved or rejected decision. An approved patch prepares a verification task. Use a new external conversation and the host's session ID for verification; `--session-source declared` marks identity as unconfirmed and cannot pass the gate. Apply only a verified patch, then inspect `run gate` and `run report`. A failed run suggests `run resume` only when its recorded pre-failure stage is recoverable; repair the prerequisite first.

`run cancel RUN_ID --reason TEXT` waits for any current run operation to release the lock, then durably cancels pending tasks and invalidates active attempts. It does not terminate an external client conversation. A later waiver that changes an active revision or verification task interrupts that attempt; run `run resume` to prepare the replacement task. Do not edit `.scriptorium/` or its database, artifacts, snapshots, or bundles to repair a run. Fix the prerequisite and use the CLI. Old internal-SDK runs can be inspected with status, report, and gate commands without upgrading their database. Finish any active old run with the original version before starting an external run in the same repository; that explicit start upgrades the database and the original version cannot open it afterward.
