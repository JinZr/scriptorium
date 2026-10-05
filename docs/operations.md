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

## Task command reference

Every command accepts the global `--json` flag before the command name. `scriptorium task COMMAND -h` lists each
option. Bounded responses stay within 7,000 UTF-8 bytes; run `next_command` unchanged until it is null.

| Command | Use | Main response fields | Continuation |
| --- | --- | --- | --- |
| `task claim TASK_ID ...` | Start or reconnect to this session's attempt | `attempt`, `input_digest`, `inputs.*.command`, `source_map_command` | none |
| `task show ATTEMPT_ID` | Attempt overview | same as claim | none |
| `task show ATTEMPT_ID --part P` | Frozen prompt, schema, or source map text | `text`, `offset`, `total_chars`, `digest` | `next_command` |
| `task nav ATTEMPT_ID` | Filter navigation entries | `entries`, `total_entries`, `command_counts` | `next_cursor`, `next_command` |
| `task search ATTEMPT_ID --query Q` | Literal case-insensitive search | `matches[]` with `path`, `line`, `column`, `excerpt`, `source_digest` | `next_cursor`, `next_command` |
| `task read ATTEMPT_ID --path P` | Bounded source or metadata lines | `lines[]` with `line`, `offset`, `text`; `source_path`, `source_digest`, optional `anchor` | `next_line`, `next_offset`, `next_command` |
| `task page ATTEMPT_ID --number N` | Rendered page image, zoomed view, or text layer | `page`, `path`, `digest`, `document`, `document_page`; `view`, or `text` with `text_digest` | `next_command` with `--text` |
| `task submit ATTEMPT_ID ... --check` | Validate without recording | `valid`, `recorded: false`, `output_digest`, `validation_report` | `task submit` without `--check` |
| `task submit ATTEMPT_ID ...` | Validate one output | `attempt`, `validation_report`, `run_status`, `next_actions` | `run status` |
| `run status RUN_ID` | Overview and next actions | `task_counts`, `next_actions`, `report_parts` | none |
| `run report RUN_ID --part P` | One report section | `text`, `offset`, `digest`, `report_digest` | `next_command` |

Errors use `{"ok": false, "error": {"code", "message"}}` with `--json`. `task read/search/nav/page` need an
active attempt; `task show` also works for finished attempts.

`run status` returns a compact run identity and status, task counts, finding/patch counts, `has_error`, current `next_actions`, and `report_parts` commands. It omits frozen configuration and attempt history; these remain available through the `run` and `tasks` report sections. Costs remain unknown for external runs. Historical SDK runs return `execution: legacy_read_only` with no executable next actions.

The CLI responses for `run start/resume/retry/continue/cancel` summarize the state returned by that mutation and direct the client to `run status` for current actions. They do not include the full run configuration. After submission, use `run status` to discover continuation or human gates; `task list` retains its detailed, unbounded output for explicit inspection.

Use `run report RUN_ID --part PART` for bounded report reading. The 7,000-byte bound applies with or without `--json`, as it does for all other bounded commands: both the compact success envelope and ordinary indented output are included in the byte budget. Available parts are listed in `run status` under `report_parts`; they correspond to the top-level keys of the complete JSON report, including `run`, `tasks`, `findings`, `patches`, `events`, `validation_reports`, `review_scopes`, `review_claim_checks`, `review_tool_access`, `review_coverage_audit`, and `gate`. Each response includes `text`, character `offset`, `total_chars`, the part's SHA-256 `digest`, the whole `report_digest`, and a `next_command`. Follow it until null, concatenate without separators, and parse the JSON value. A part can split inside a record or quoted string.

Report reads hold the run lock while building each response and do not append access events or change workflow state. A nonzero `--offset` requires the previous `--report-digest`. If any report content changes, the continuation fails explicitly: restart at offset zero without that digest and discard the old fragments. To read multiple sections from the same report state, also pass that digest on their first requests. A matching digest establishes consistent report content, not scientific correctness. Corrupt artifacts still fail validation. Use `has_error` and the `run`, `validation_reports`, and `gate` sections to inspect failures.

`--part` cannot be combined with `--format`. Complete exports remain available as `scriptorium run report RUN_ID --format json > report.json` or `--format markdown > report.md`; these outputs have no byte bound. Explicit `task list`, `finding list/show`, and `patch show` output is also unbounded. At a human gate, inspect findings through the bounded report sections before asking for a decision.

A claimed attempt remains active when the CLI exits. Claim and plain `task show` return a compact overview: `data.input_digest` is the digest to submit, and `data.inputs.prompt.command` and `data.inputs.schema.command` retrieve the frozen inputs. Run each command and follow its `data.next_command` until null. Concatenate each part's `data.text` fragments in `data.offset` order; offsets count Unicode characters, not UTF-8 bytes. Parse the complete schema text as JSON and inspect its required fields before writing an output. Each part includes its immutable digest and total character count. `task show ATTEMPT_ID --part prompt|schema|source-map --offset N` also accepts an explicit continuation offset. Use `data.source_map_command` or `task show ATTEMPT_ID --part source-map` for the `sources` array of evidence paths and digests. This returns the exact frozen source-map JSON, including for completed, failed, or interrupted attempts; it does not reactivate them. `task read/search/page/nav` still require an active attempt. The attempt's input digest must accompany submission:

```bash
scriptorium --json task search ATTEMPT_ID --query phrase --cursor 0 --limit 20
scriptorium --json task read ATTEMPT_ID --path main.tex --start-line 1 --max-lines 40
scriptorium --json task nav ATTEMPT_ID --command heading
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task page ATTEMPT_ID --document supplement.tex --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest DIGEST --file answer.json
```

For explicitly configured independent supplements, inventory `compiled_pdf.documents` in the reconstructed
source map. Each record identifies a LaTeX entrypoint and its contiguous range in the assembled review PDF.
Without `--document`, `--number` is the global review-PDF page. With `--document`, it is the 1-based physical
page within that entrypoint's compiled document. The response's `source_path` and `page` are the evidence
coordinates; `document` and `document_page` describe their origin. Use the returned global `page` in findings,
claim checks and scope, never the document-local number. A page access event records both coordinates but does
not prove the model opened the image. Missing entrypoints and out-of-range local pages are rejected.

Configure and commit independent `.tex` entrypoints using `manuscript.supplements` before starting a run.
`doctor` and run preparation compile every declared document. A supplement failure cannot produce a main-only
review task. Fix an external prerequisite and resume when possible; if the committed inputs or configuration
need changing, commit them and start a new run. Revisions and verification rebuild all the frozen entrypoints.

`task nav` filters the frozen navigation index without reading it as text. Repeat `--command` with a group (`heading`, `reference`, `citation`, `label`, `caption`, `graphics`) or an exact LaTeX command name (duplicates collapse); `--query` matches a case-insensitive substring of the literal value and `--path` limits entries to one frozen source; as in `task read`, a read path takes priority over a colliding source path. Each entry keeps its `command`, `source_path`, inclusive `start_line`/`end_line`, literal `value` (cut to 500 characters with `value_truncated`), and graphics candidates. At most 10 `candidate_paths` are listed, fewer when their encoded paths would not fit the response bound; when the list is cut, `candidate_count` gives the total and `candidate_paths_truncated` is true. If long paths still leave no room for a lone entry, it omits the `source_path` that `--path` already names (`source_path_omitted`), then its `target_path` (`target_path_omitted`; the candidate count and the source map still identify the target), then cuts `value` by encoded size. Without `--path`, a source path that still does not fit finally gives way to `source_index`, its position in the source map's `sources` (with `source_path_omitted`). When more entries remain, a `--path` too long to repeat in the bounded continuation, such as one full of characters that need shell quoting, is rejected; run `task nav` without `--path` and match entries by `source_path`. The response adds `total_entries`, `command_counts` for the filtered set, and a cursor continuation. Navigation entries are literal source locations, not evidence or an interpretation of TeX; read the source range before relying on one. Runs frozen without a navigation index reject the command. Report tool-return counts include `nav`.

`task read` and `task search --path` support `manifest.json`, `navigation.json`, `source-map.json`, and text sources named in the source map (either `source_path` or `read_path`). Bare metadata names select the generated bundle metadata; use the source map's `read_path` for a same-named manuscript source. Among sources, `read_path` takes priority over a colliding `source_path`, so every source remains addressable through its frozen read path. Search matches use the canonical `source_path`; continuations retain a read path whenever shortening it would select a different source or metadata. Read responses and access events include the resolved `source_path` (null for generated metadata), separately from the requested `path`. `--max-chars` is a ceiling of at most 8,000 characters, not a promised chunk size. Successful JSON responses for claim, show, read, search, nav, and page are limited to 7,000 UTF-8 bytes including JSON escaping, metadata, envelope, and newline. Metadata that cannot fit fails explicitly. This bound leaves room under the observed host truncation threshold; it cannot guarantee delivery by every host. The same bound covers `run status`, run mutation acknowledgements, and `run report --part`. Full report exports, detailed lists, and submission diagnostics are not covered by this bound.

Read returns `next_line`, `next_offset`, and a shell-quoted `next_command` when more text remains. `--end-line N` reads an inclusive range: continuations keep it and stop after line N rather than at the end of the file. `--anchor` adds an `anchor` object with `source_path`, `start_line`, `end_line`, `source_digest`, and `quoted_text` for the completely returned lines of a text source, in the exact evidence shape; it is null for metadata and for a response holding only part of a line. It is also null when the run's frozen evidence contract numbers that source differently from retrieval: only a run frozen before the contract named its line terminators, reading a source with form feeds or Unicode line separators. Its quotation repeats the returned text, so fewer lines fit in each bounded response. Any verbatim substring of `quoted_text` remains a valid quotation for the same range. An unknown path error lists the closest frozen text read paths. Execute that command unchanged (or use the same arguments with your installed CLI entrypoint). Continuations preserve the current output mode: `--json` keeps the success envelope, while ordinary output keeps top-level fields. Overview commands for starting other inputs or report sections explicitly request JSON. A nonzero offset continues the same line; incrementing the line skips its unread tail. Search returns `total_matches`, `next_cursor`, and `next_command`; the byte limit may reduce the number of returned matches below `--limit`. Without `--path`, search covers the frozen text sources only; add `--include-metadata` to also search `manifest.json`, `navigation.json`, and `source-map.json`, which repeat every label, citation and path. `--context N` (0-3) adds `before` and `after` lists of neighbouring lines, each cut to 200 characters. Context is search output: the access event records its line span, and the coverage audit does not count it as a read. Follow continuations until null when traversing an entire file or search. Access events record only the returned fragments and matches. `task page` returns the exact rendered image path and digest; the host must actually open the image for a visual review. `--scale S` (0.5-4.0; frozen page images use 1.5) and `--crop x0,y0,x1,y1` (fractions of the page width and height) render a zoomed view from the digest-checked frozen `manuscript.pdf` into the run's `page-views/` directory; a view larger than 40 megapixels is rejected, so lower the scale or crop a smaller region. Each view file is named by its own digest and never overwritten, so a returned path keeps its bytes. A run keeps at most 512 MiB of distinct views; after that, reuse an earlier view. The response keeps the frozen page `digest` and adds `view` with the scale, crop and rendered image digest; evidence and scope still cite the global page. `--text` returns the page's PDF text layer in bounded fragments with `evidence: false` and a `text_digest` of the extraction. Continuations pass `--offset` with that `--text-digest`; if the extraction no longer matches, for example after a PyMuPDF upgrade, the continuation is rejected and reading restarts at offset 0. Extraction can reorder or drop text, so quote manuscript text from frozen sources, never from the text layer. Text-layer returns are recorded as `tool.page_text`, counted as `page_text`, and do not satisfy the coverage audit's page check.

External review sessions have no wall-clock deadline by default. For AGY print-mode validation, use `--print-timeout 0` and wait without a subprocess timeout. A host process exit or a success message is not review completion: inspect the accepted submission receipt and `run status`, continue accepted partial scope through `run continue`, and stop at an explicit human gate. If the host disconnects, inspect the durable attempt and reconnect to the same session before deciding whether explicit abandonment is needed. Paid-call and correction allowances remain separate from elapsed time; `--help` calls are not material retrievals.

`task submit --file` accepts UTF-8 JSON up to 2 MB from a file or stdin. The CLI rejects larger input before decoding or passing it to the workflow.

`task submit --check` validates the same input against the attempt's frozen schema, evidence map, task context and verifier-session rule without storing it. It requires an active attempt and the attempt's input digest, records no output or validation-report artifact, findings or attempt status, and leaves the attempt claimable for submission. It appends one `tool.submit_check` event with the input and output digests, the outcome and the issue codes. `data.valid` reports that this file would currently be accepted, not that a later submission was made; workflow state can still change before the real submission, which validates again. Coverage audits and tool-return counts do not count checks.

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

For a new `substantive_review` task, also include `claim_checks`. Each entry has `claim`, `evidence` (the same exact source or PDF anchors used by findings), `critical_question`, `countercheck`, `assessment` (`supported`, `unresolved`, or `finding`), and `finding_indices`. New runs also require the judgment fields: `claim_anchor` (an exact anchor where the manuscript states the claim, validated like evidence), `stated_scope`, `check_type` (`reporting_consistency`, `recomputation`, `design_and_analysis`, `alternative_explanation`, or `scope_and_generality`), `question_answer` (`yes`, `partly`, `no`, or `not_checkable`), and `exceptions`. A `recomputation` check, and only that type, includes a `recomputation` object with `inputs`, `calculation`, `result`, `reported`, and `outcome` (`matches` or `differs`). `supported` goes with `yes` and only with it, `yes` lists no exceptions and `partly` at least one, a differing recomputation cannot answer `yes` and a matching one cannot answer `no`, and `not_checkable` requires `unresolved`. These rules check internal consistency only; the judgments remain model declarations. Runs frozen before these fields keep their earlier claim-check shape. The indices are zero-based positions in the submitted `findings` array. Use an empty index list for supported or unresolved checks; a finding check must link at least one finding, and every finding must be linked. A complete review requires at least one claim check; a partial review may submit an empty list. The frozen schema returned by `task show` is authoritative.

Before submitting a substantive review, follow its frozen prompt's conclusion-consistency instructions. A
`supported` assessment needs evidence for the stated claim and critical question at the same scope; resolving one
candidate criticism does not establish a broader conclusion. Carry material counterexamples and uncertainty into
the assessment and summary, and anchor the evidence that determines the judgment. The shared skill routes the
current model through this step; it does not start another model or add a submission field. The CLI validates the
schema, anchors and finding links, not the scientific meaning of this comparison.

Keep work completion separate from scientific certainty. Available relevant sources that remain unexamined belong
in `scope.outstanding` and prevent a `complete` declaration. An `unresolved` claim may remain after the available
material has been assessed; describe missing external evidence in `scope.limitations`. That alone does not require
continuation or turn the unresolved question into a finding. Existing runs keep their frozen prompts; use a new run
to evaluate an updated review procedure.

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
