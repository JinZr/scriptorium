# Operations and recovery

Run commands from the manuscript Git repository and use `--json` for machine-readable output. The JSON envelope has `ok` and either `data` or a stable error `code` and `message`.

```bash
scriptorium --json doctor --revision COMMIT --profile full
scriptorium --json run start --revision COMMIT --profile full
scriptorium --json run list
scriptorium --json run status RUN_ID
scriptorium --json task claim TASK_ID --client codex --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
scriptorium --json task export ATTEMPT_ID --dir DIR
```

## Review brief

Before starting a run, the host agent drafts a review brief with the author. It reads `detected_template` from
`doctor`, infers the venue family, and asks only for what it cannot infer: the stage, the two or three claims to
check first, what earlier reviewers said, and what to ignore this round. The author confirms the draft, edits it,
or accepts the defaults; the agent writes `brief.json` (format in [configuration](configuration.md#review-brief)) and
starts the run with it:

```bash
scriptorium --json run start --revision COMMIT --profile full --brief brief.json
```

The brief is validated, published as a content-addressed artifact, and recorded on the run by digest. Its rendered
text (venue family, stage, and the author's lists) precedes the severity rubric in every review role's frozen prompt.
Revision and verification prompts do not include it. Every attempt's export includes the exact frozen brief as
`task/brief.json`. `run start` and `run status` show a compact summary
(`review_brief`: digest, venue family, venue, stage, and list counts), and the `run` report section includes the
brief itself. The brief cannot be changed after the run starts; to review under a
different brief, start a new run. A run without a brief exports no `task/brief.json`.

## Task command reference

Every command accepts the global `--json` flag before the command name. `scriptorium task COMMAND -h` lists each
option.

| Command | Use | Main response fields |
| --- | --- | --- |
| `task claim TASK_ID ...` | Start or reconnect to this session's attempt | `run_id`, `task`, `attempt`, `input_digest`, `bundle_digest`, `navigation_digest`, `export_command` |
| `task show ATTEMPT_ID` | Attempt overview, any status | same as claim |
| `task export ATTEMPT_ID --dir DIR` | Write the frozen bundle and task inputs (no page images) to disk | `attempt_id`, `directory`, `file_count`, `total_bytes`, `files_digest`, `task_inputs[]` with `path`, `digest` |
| `task page ATTEMPT_ID --number N` | Rendered page image, zoomed view, or text-layer file | `source_path`, `page`, `document`, `document_page`, `path`, `digest`; `view`; with `--text`, `text_layer`, `evidence: false`, `bytes`, `note` |
| `task submit ATTEMPT_ID ... --check` | Validate without recording | `valid`, `recorded: false`, `output_digest`, `validation_report` |
| `task submit ATTEMPT_ID ...` | Validate one output | `attempt`, `validation_report`, `run_status`, `next_actions` |
| `run list [--status S] [--limit N]` | This project's runs, newest first | `total`, `limit`, `returned`, `truncated`, `runs[]` with `run_id`, `status`, `commit_sha`, `profile`, `created_at`, `updated_at`, `task_counts`, and `venue_family`, `venue`, `stage` when the run has a brief |
| `run status RUN_ID` | Overview and next actions | `task_counts`, `next_actions`, `report_parts` |
| `run report RUN_ID --part P` | One whole report section | the section's JSON value |

Errors use `{"ok": false, "error": {"code", "message"}}` with `--json`. `task page` needs an active attempt;
`task show` and `task export` work for any attempt of the run and do not reactivate it.

`run list` is how to find a `RUN_ID`; do not read `.scriptorium/` by hand. It reads the database without taking a run lock and makes no model call. `--status` takes a run status value (`preparing`, `reviewing`, `awaiting_decision`, `revising`, `awaiting_patch_approval`, `verifying`, `ready_to_apply`, `completed`, `failed`, `cancelled`); `--limit` defaults to 20 and is at most 100. Bad values return `configuration_error`. `truncated` is true when more runs match than `--limit` returned; `total` counts every matching run. A run whose frozen brief cannot be read omits the brief fields; `run status` reports that failure.

`run status` returns a compact run identity and status, task counts, finding/patch counts, `has_error`, current `next_actions`, and `report_parts` commands. It omits frozen configuration and attempt history; these remain available through the `run` and `tasks` report sections. Scriptorium records no usage or cost.

The CLI responses for `run start/resume/retry/cancel` summarize the state returned by that mutation and direct the client to `run status` for current actions. They do not include the full run configuration. `run start` refuses with error code `duplicate_run`, naming the existing run, when a run on the same resolved commit is not `completed` or `cancelled` (a `failed` run can still be resumed, so it counts); inspect it with `run status`, cancel it (resume it if it failed; a failed run cannot be cancelled), or pass `--allow-duplicate` to start another deliberately. After submission, use `run status` to discover continuation or human gates; `task list` retains its detailed output for explicit inspection.

`run report RUN_ID --part PART` prints one whole report section as JSON: the value under that top-level key of the complete JSON report (under `data` with `--json`). Available parts are listed in `run status` under `report_parts`: `run`, `tasks`, `finding_ids`, `patch_ids`, `findings`, `patches`, `events`, `validation_reports`, `review_limitations`, `review_claim_checks`, `findings_grouped`, `decision_stats`, and `gate`. A section can be large, so redirect it to a file and read the file. `--part` cannot be combined with `--format`; `scriptorium run report RUN_ID --format json > report.json` or `--format markdown > report.md` writes the complete report. Report reads hold the run lock while building the report and do not append events or change workflow state. Corrupt artifacts still fail validation. Use `has_error` and the `run`, `validation_reports`, and `gate` sections to inspect failures. At a human gate, inspect findings through the report sections before asking for a decision.

`decision_stats` counts the run's findings by current human decision (`confirmed`, `rejected`, `waived`, or `pending` when undecided) overall and by producing role, category, and severity, and lists the latest rejection and waiver reasons (first 200 characters, newest first, at most 50). A finding decided more than once counts once under its latest decision; the append-only decision records are unchanged. Group rows are lists because canonical report JSON sorts object keys: roles and categories alphabetically, severities from blocker to suggestion. The counts are a calibration input for the next review and prompt maintenance, not a quality metric by themselves, and they do not affect the release gate. The complete JSON and Markdown exports include the same section.

`findings_grouped` groups all of the run's findings, across roles and attempts, for triage. Two findings share evidence when more than half of either finding's distinct anchors overlap an anchor of the other. Two line anchors overlap when they name the same frozen source path and their inclusive ranges share at least half of the shorter range. Line anchors decide whenever either finding has one, so a finding with line anchors never matches one with only PDF pages; between two findings with only PDF page anchors, a page overlaps only the same page, and the two must also share a linked claim, because a page is too coarse to identify an issue alone. Two findings also share evidence, whatever their line anchors, when they share a linked claim and both cite the same table or figure object of the run's frozen navigation index: a line anchor lies inside the float (`table`, `table*`, `sidewaystable`, `longtable`, `figure`, or `figure*`) or covers a `ref`, `cref`, `Cref`, `autoref`, `eqref`, or `pageref` whose key is a `label` inside it, so a table row, its caption, and the sentence that cites `Table~\ref{tab:main}` can group. Two findings that cite one such object also share evidence, with or without a shared claim, when a line anchor of one overlaps a line anchor of the other, so findings from different roles that quote the same rows of one table group even when exactly half of their anchors overlap. A label defined more than once, inside a float or not, as when a supplement reuses a main-paper label, is ambiguous and names no float. Because the index is line-granular, content confined to a float's `\begin` or `\end` line is treated as outside it. A run frozen without a navigation index, or without figure entries, never groups by that object. Groups do not chain: in primary order, each finding not yet grouped starts a group and gathers every later ungrouped finding that shares evidence with it, so every member shares evidence with its group's primary, and a finding that matches nothing forms its own group. The section has a `verdict` (the latest substantive output's `attempt_id`, `recommendation`, and `decisive_questions`, or null), `counts` of groups and findings per tier, and `groups`. Each group lists `group_id` (derived from its sorted member IDs), `primary_finding_id` (highest severity, then `substantive_review`, then earliest), sorted `finding_ids`, `roles` (including roles whose identical finding was recorded as a duplicate of a stored member), `max_severity`, `categories`, merged `evidence` spans and pages, `claim`, the primary finding's `consequence`, `tier`, `decision_state`, `pending_finding_ids` (the undecided members), `members` (each member's `finding_id`, `role`, `severity`, and current `status`), and one of `titles` per member in `finding_ids` order. `claim` is the most prominent linked claim (`text`, `prominence`, and the linking `finding_id`): a substantive finding links through its claim check to an inventoried claim, a detail-role finding through its `affected_claim` with null prominence; a claim check whose finding was stored under an earlier task's identical finding links to that stored finding through its duplicate record. `tier` is `headline` when a member links to a headline claim, `supporting` for any other link, `compliance` when every member is `submission_compliance`, and otherwise `other`. Groups are ordered by tier (headline, supporting, other, compliance), then severity, then primary finding ID. `decision_state` is the members' shared status (`pending`, `confirmed`, `rejected`, or `waived`), or `mixed` when they differ; read `members` for each status. Grouping is a reading aid: findings, decisions, and the release gate are unchanged. The Markdown export lists the groups before the individual findings.

A claimed attempt remains active when the CLI exits. Claim and `task show` return a compact overview; `data.input_digest` is the digest to submit and `data.export_command` names the export. `task export ATTEMPT_ID --dir DIR` writes the attempt's frozen material into `DIR`, which must not exist or must be an empty directory; otherwise the command fails with `configuration_error` and writes nothing:

- every manuscript source at the `read_path` the source map lists (under `sources/`), `manifest.json`, `navigation.json`, `source-map.json`, and `manuscript.pdf`;
- `task/prompt.md`, the attempt's frozen prompt, which includes the correction feedback on a retry; `task/schema.json`, the frozen output schema, whose required fields an output must supply; `task/brief.json` when the run has a brief; and, for a review task, `task/example.json`, a complete placeholder output built from the frozen schema that passed the task's output model and anchor checks when the task was prepared. Revision and verification tasks have no example.

Each file holds the exact frozen bytes: bundle files are checked against the bundle index and task inputs against their recorded artifact digests before anything is written. Each is written to a temporary file, renamed into place, and made read-only (`0444`). Every destination is resolved and refused if it would leave `DIR`, and a bundle path that would collide with a task input under `task/` is refused before anything is written. If any file or the event fails, every file written by that call is removed again. Rendered page images are not exported. The host owns `DIR`; Scriptorium never cleans it up. The response gives the directory, the file count, the total bytes, `files_digest` over the sorted exported listing, and the paths and digests of the task inputs; `source-map.json` and `manifest.json` list the sources. Each successful export appends one `tool.export` event bound to the attempt with the sorted paths and digests. Export works for completed, failed, and interrupted attempts too; it changes no workflow state.

Read the exported files with the host's own tools. Evidence is unchanged by an export: anchors cite the frozen `source_path`, `source_digest` from `source-map.json`, and lines, and are verified at submission; `task submit --check` verifies them without recording anything. Exports and events do not prove that anything was read.

```bash
scriptorium --json task export ATTEMPT_ID --dir review-export
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task page ATTEMPT_ID --document supplement.tex --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest DIGEST --file answer.json --check
scriptorium --json task submit ATTEMPT_ID --input-digest DIGEST --file answer.json
```

For explicitly configured independent supplements, inventory `compiled_pdf.documents` in the exported
`source-map.json`. Each record identifies a LaTeX entrypoint and its contiguous range in the assembled review PDF.
Without `--document`, `--number` is the global review-PDF page. With `--document`, it is the 1-based physical
page within that entrypoint's compiled document. The response's `source_path` and `page` are the evidence
coordinates; `document` and `document_page` describe their origin. Use the returned global `page` in findings
and claim checks, never the document-local number. The `tool.page` event records both coordinates but does
not prove the model opened the image. Missing entrypoints and out-of-range local pages are rejected.

Configure and commit independent `.tex` entrypoints using `manuscript.supplements` before starting a run.
`doctor` and run preparation compile every declared document. A supplement failure cannot produce a main-only
review task. Fix an external prerequisite and resume when possible; if the committed inputs or configuration
need changing, commit them and start a new run. Revisions and verification rebuild all the frozen entrypoints.

`navigation.json` indexes literal source locations. Each entry has a `command`, the LaTeX command or environment name (such as `section`, `ref`, `cite`, `label`, `caption`, `includegraphics`, `table`, `figure`, `equation`) or `quantity`, its `source_path`, inclusive `start_line`/`end_line`, and literal `value` (at most the first 2000 characters of a long environment body); graphics entries add candidate paths. Table, figure, and equation entries span their environment from `\begin` to `\end` in the typeset document body, and a `\[ ... \]` display is a `displaymath` entry. Quantity entries locate `siunitx` commands and numbers in document text that carry a decimal point, uncertainty, exponent, `\pm`, or percent sign, with an adjacent unit when one follows; integers, versions, labels, lengths, macro definitions, comments, and the preamble are skipped. They are heuristic locations for checking reported values, not an exhaustive or interpreted list. Navigation entries are not evidence or an interpretation of TeX; read the source range before relying on one.

`task page` returns the exact rendered image path and digest; the host must actually open the image for a visual review. `--scale S` (0.5-4.0; frozen page images use 1.5) and `--crop x0,y0,x1,y1` (fractions of the page width and height) render a zoomed view from the digest-checked frozen `manuscript.pdf` into the run's `page-views/` directory; a view larger than 40 megapixels is rejected, so lower the scale or crop a smaller region. The response keeps the frozen page `digest` and adds `view` with the scale, crop and rendered image digest; evidence still cites the global page. `--text` writes the page's PDF text layer to `page-views/DIGEST.txt` and returns its `path`, `digest`, and size in `bytes` with `evidence: false`. Each view and text file is named by its own digest and never overwritten, so a returned path keeps its bytes. A run keeps at most 512 MiB of distinct views and text files; after that, reuse an earlier one. Extraction can reorder or drop text: the text layer is a reading aid to locate material, never evidence, so quote manuscript text from frozen sources.

External review sessions have no wall-clock deadline by default. For AGY print-mode validation, use `--print-timeout 0` and wait without a subprocess timeout. A host process exit or a success message is not review completion: inspect the accepted submission receipt and `run status`, and stop at an explicit human gate. If the host disconnects, inspect the durable attempt and reconnect to the same session before deciding whether explicit abandonment is needed. Paid-call and correction allowances remain separate from elapsed time.

`task submit --file` accepts UTF-8 JSON up to 2 MB from a file or stdin. The CLI rejects larger input before decoding or passing it to the workflow.

`task submit --check` validates the same input against the attempt's frozen schema, evidence map, task context and verifier-session rule without storing it. It requires an active attempt and the attempt's input digest, records no output or validation-report artifact, findings or attempt status, and leaves the attempt claimable for submission. It appends one `tool.submit_check` event with the input and output digests, the outcome and the issue codes. `data.valid` reports that this file would currently be accepted, not that a later submission was made; workflow state can still change before the real submission, which validates again.

For a new review task, include `limitations` alongside `summary` and `findings`: a list, possibly empty, of the relevant material you could not check or could not read, including material the manuscript mentions that is absent from the frozen sources. This minimal output illustrates the shape only; a new `substantive_review` also requires `claim_checks`, `claim_inventory`, and `verdict`. The exported `task/example.json` of a review task is a complete output with a finding, its exact text anchor, and its zero-based claim-check link. The frozen `task/schema.json` takes precedence over either example:

```json
{
  "summary": "The main result was checked against the frozen sources.",
  "findings": [],
  "limitations": ["The raw data the methods section cites are not among the frozen sources."]
}
```

An accepted submission completes its review task and satisfies the required-review gate for that role; the task takes no further submission. Validation checks only the shape of `limitations`. Neither the list nor the recorded export and page events prove that the host opened or understood material.

A frozen source that is not text, such as a figure PDF or PNG listed in the source map, takes no line anchor. Inspect it as rendered where the manuscript places it: its `includegraphics` entry in `navigation.json` gives the TeX location but not the page, so locate the page from the nearby caption (the `--text` layer can help find it), open the rendered image, and use `--scale 3 --crop` for detail. Cite those global pages as evidence.

For a new `substantive_review` task, also include `claim_checks`. Each entry has `claim_index`, `evidence` (the same exact source or PDF anchors used by findings), `critical_question`, `countercheck`, `assessment` (`supported`, `unresolved`, or `finding`), and `finding_indices`. Each entry also requires `stated_scope`, `check_type` (`reporting_consistency`, `recomputation`, `design_and_analysis`, `alternative_explanation`, or `scope_and_generality`), `question_answer` (`yes`, `partly`, `no`, or `not_checkable`), and `exceptions`. A `recomputation` check, and only that type, includes a `recomputation` object with `inputs`, `calculation`, `result`, `reported`, and `outcome` (`matches` or `differs`). `supported` goes with `yes` and only with it, `yes` lists no exceptions and `partly` at least one, a differing recomputation cannot answer `yes` and a matching one cannot answer `no`, and `not_checkable` requires `unresolved`. These rules check internal consistency only; the judgments remain model declarations. New runs also require `claim_inventory`. Each entry has `claim`, `claim_anchor` (validated like evidence), `prominence` (`headline` for claims in the abstract, stated contributions, or conclusions; `supporting` for claims they depend on), and, when no claim check names it by `claim_index` (a zero-based position in `claim_inventory`), a `not_checked_reason`. Every headline claim must be checked, with `question_answer` `not_checkable` when the frozen bundle cannot decide it, and a `blocker` finding must be linked from a check of a headline claim; a supporting claim may stay unchecked with its reason. New runs also require `verdict` with `recommendation` (`accept`, `minor_revision`, `major_revision`, or `reject`) and one to three `decisive_questions`; `reject` or `major_revision` needs a `major` or `blocker` finding in the submission, and `accept` or `minor_revision` allows none. A claim check's `finding_indices` are zero-based positions in the submitted `findings` array. Use an empty index list for supported or unresolved checks; a finding check must link at least one finding, and every finding must be linked. A substantive review requires at least one claim check. The frozen schema in `task/schema.json` is authoritative.

Before submitting a substantive review, follow its frozen prompt's conclusion-consistency instructions. A
`supported` assessment needs evidence for the stated claim and critical question at the same scope; resolving one
candidate criticism does not establish a broader conclusion. Carry material counterexamples and uncertainty into
the assessment and summary, and anchor the evidence that determines the judgment. The current model performs
this step from its frozen prompt; it does not start another model or add a submission field. The CLI validates the
schema, anchors and finding links, not the scientific meaning of this comparison.

Keep work completion separate from scientific certainty. An `unresolved` claim may remain in an accepted review;
describe missing external evidence in `limitations`. That alone does not turn the unresolved question into a
finding. Existing runs keep their frozen prompts; use a new run to evaluate an updated review procedure.

Each report entry for claim checks includes `submitted_findings` in that attempt's original order and the `verdict`, which the Markdown report shows first. Its `finding_indices` refer to this list, not the report-wide findings, which may be severity-sorted or deduplicated across attempts.

After each `task submit`, use `run status RUN_ID` and follow `next_actions`. The run advances to human finding decisions once every required role has an accepted review. Repeating a superseded attempt's submission is rejected. Completed or cancelled runs cannot be reopened. If an accepted output awaits replay after a process exit, `run status` suggests `run resume`. A `next_actions` entry marked `requires_human_decision` identifies a finding decision, patch approval, or explicit patch application gate.

When output is invalid, inspect every issue in `data.validation_report.issues`. No findings, patch, or verification are accepted from that attempt. Correct the output against the same frozen schema and source map, then explicitly run `scriptorium --json run retry RUN_ID --task TASK_ID`. Claim again, export the new attempt, and submit with the **new attempt ID and input digest**; the failed attempt cannot accept a corrected output. The next prompt includes the durable diagnostics. If an active external conversation was abandoned, run `scriptorium --json run retry RUN_ID --task TASK_ID --abandon-attempt ATTEMPT_ID --reason TEXT` to interrupt only that attempt and make the task claimable again. Its late submission is rejected. Repeated identical submissions are idempotent. Different output for a terminal attempt and output for a superseded or cancelled attempt are rejected.

After review tasks complete, read `run report RUN_ID --part findings_grouped`, present the groups to the author in tier order with a one-line judgement of whether each changes how a headline claim reads, and record only the decisions the author gives, per group or per tier, with `finding decide` on the group's `pending_finding_ids` only, never IDs already decided; then `run resume`. `finding decide FINDING_ID [FINDING_ID ...] --confirm|--reject|--waive --reason TEXT` applies one decision and reason to each listed finding. All IDs must exist, belong to one run, and be decidable, or nothing is recorded; otherwise each finding receives its own decision record and event in order, in one transaction. The response lists `finding_ids`; a single ID also returns `decision` and `finding`, and several return `decisions` and `findings`. A confirmed finding prepares a revision task. After submitting revision output, inspect the patch and decide it explicitly; `run status` then suggests `run resume` to advance an approved or rejected decision. An approved patch prepares a verification task. Use a new external conversation and the host's session ID for verification; `--session-source declared` marks identity as unconfirmed and cannot pass the gate. Apply only a verified patch, then inspect `run gate` and `run report`. A failed run suggests `run resume` only when its recorded pre-failure stage is recoverable; repair the prerequisite first.

`run cancel RUN_ID --reason TEXT` waits for any current run operation to release the lock, then durably cancels pending tasks and invalidates active attempts. It does not terminate an external client conversation. A later waiver that changes an active revision or verification task interrupts that attempt; run `run resume` to prepare the replacement task. Do not edit `.scriptorium/` or its database, artifacts, snapshots, or bundles to repair a run. Fix the prerequisite and use the CLI.
