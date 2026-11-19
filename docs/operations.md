# Operations

The command reference, the run lifecycle, the plan directory, the rules a submitted output must meet, how findings
are grouped and decided, the report sections, and recovery. Settings are in [configuration](configuration.md); storage, freezing,
and gates are in [architecture](architecture.md).

## Conventions

- Run commands from the manuscript's Git repository, with the global `--json` flag before the command name.
  `scriptorium COMMAND -h` lists a command's options.
- Success prints `{"ok": true, "data": ...}`; failure prints `{"ok": false, "error": {"code": ..., "message": ...}}`.
- Use the IDs and digests the CLI returns rather than reading `.scriptorium/`.

| Error code | Exit | Meaning |
| --- | --- | --- |
| `invalid_arguments` | 2 | The command line did not parse |
| `configuration_error` | 2 | Bad input, configuration, or option value |
| `not_found` | 2 | No object with that ID |
| `invalid_state`, `duplicate_run` | 1 | The run or task does not allow the command now |
| `infrastructure_error` | 3 | A tool, build, or frozen file failed; the outcome is inconclusive |
| `interrupted` | 3 | The command was interrupted |

`run gate` also exits 1 when the gate does not pass, and `patch apply` exits 1 when the patch is stale.

## Commands

| Command | Use | Main response fields |
| --- | --- | --- |
| `init [PATH] [--main FILE] [--engine ENGINE]` | Write `scriptorium.toml`, ignore `.scriptorium/` | `repository`, `initialized` |
| `doctor [--revision REV] [--profile P]` | Check a commit is ready | `ok`, `profile`, `checks`, `detected_template` |
| `run start [--revision REV] [--profile P] [--brief FILE] [--allow-duplicate]` | Freeze a commit, prepare review tasks | overview, `detected_template` |
| `run list [--status S] [--limit N]` | Runs, newest first | `total`, `limit`, `returned`, `truncated`, `runs` |
| `run status RUN_ID` | Where the run stands | overview |
| `run resume RUN_ID` | Replay accepted work, prepare the next stage | overview |
| `run retry RUN_ID --task TASK_ID [--abandon-attempt ATTEMPT_ID --reason TEXT]` | Make a task claimable again | overview |
| `run cancel RUN_ID --reason TEXT` | End the run | overview |
| `run report RUN_ID [--format markdown\|json \| --part PART]` | Full report or one section | report |
| `run gate RUN_ID` | Release gate | `passed`, `conditions`, `reasons`, `errors` |
| `task list RUN_ID` | Tasks with all attempts | `run_status`, `tasks`, `next_actions` |
| `task claim TASK_ID --client C --model M --effort E --session-id S --session-source host\|declared` | Start or rejoin this session's attempt | `run_id`, `task`, `attempt`, `input_digest`, `bundle_digest`, `navigation_digest`, `export_command` |
| `task show ATTEMPT_ID` | Overview of any attempt | as `task claim` |
| `task export ATTEMPT_ID --dir DIR` | Write the frozen task to disk | `directory`, `file_count`, `total_bytes`, `files_digest`, `task_inputs` |
| `task page ATTEMPT_ID --number N [--document E] [--scale S] [--crop x0,y0,x1,y1 \| --text]` | Render a page, a view, or its text layer | `source_path`, `page`, `document`, `document_page`, `path`, `digest`, `view`; `text_layer`, `evidence`, `bytes` |
| `task submit ATTEMPT_ID --input-digest D --file F\|--dir DIR --check` | Validate without recording | `valid`, `recorded`, `output_digest`, `validation_report` |
| `task submit ATTEMPT_ID --input-digest D --file F\|--dir DIR` | Submit one output | `attempt`, `output_digest`, `validation_report`, `run_status`, `next_actions` |
| `finding list RUN_ID` | All findings | findings |
| `finding show FINDING_ID` | One finding | `finding`, `decisions` |
| `finding decide ID [ID ...] --confirm\|--reject\|--waive --reason TEXT` | Decide findings | `finding_ids`; `decision`, `finding` or `decisions`, `findings` |
| `plan check RUN_ID --dir DIR` | Validate a plan directory, recording nothing | `valid`, `issues`, `plan_digest`, `assignments` |
| `plan approve RUN_ID --dir DIR --reason TEXT` | Record the author's approval, prepare writing tasks | overview |
| `patch show PATCH_ID` | A patch | `patch`, `decisions`, `verifications`, `diff` |
| `patch decide PATCH_ID --approve\|--reject --reason TEXT` | Decide the patch | `decision`, `patch` |
| `patch apply PATCH_ID` | Write a verified patch to the worktree | the patch |

The overview has `run` (`id`, `status`, `commit_sha`), `task_counts`, `finding_count`, `patch_count`, `has_error`,
`next_actions`, `report_parts` (one `run report --part` command per section), and, for a run with a brief,
`review_brief`: its `digest`, `venue_family`, `venue`, `stage`, list counts, and whether `prior_reviews` and
`severity_notes` are set. It leaves out the frozen configuration and attempt history, which the `run` and `tasks`
report sections hold. `task list` gives the detail; the normal loop uses `run status`.

### Starting and listing runs

- `run start` freezes the commit and prepares one review task per role of the profile (default `full`). It calls
  no model.
- It refuses with `duplicate_run`, naming the other run, while a run on the same resolved commit is neither
  `completed` nor `cancelled`; a `failed` run counts because it can be resumed. Inspect that run, resume it if it
  failed or cancel it otherwise, or pass `--allow-duplicate` to start a second run on purpose.
- `run list` finds a `RUN_ID`. `--status` takes a run status, `--limit` defaults to 20 and is at most 100, and other
  values return `configuration_error`. `total` counts every match; `truncated` is true when more match than were
  returned. Each entry has `run_id`, `status`, `commit_sha`, `profile`, `created_at`, `updated_at` (the last
  status change or finished attempt), `task_counts`, and `venue_family`, `venue`, `stage` for a briefed run. A run
  whose brief cannot be read is listed without those fields; `run status` reports the failure.

### Next actions

`run status` returns the current `next_actions`; responses to `run start`, `resume`, `retry`, and `cancel` point
to it. Reread it after every change. Each entry names a command and its IDs:

| Command | Offered when |
| --- | --- |
| `task claim` | A task of the current stage is pending |
| `task show` | An attempt is running |
| `run retry` | A task failed or was interrupted |
| `run resume` | The run can advance or recover |
| `finding list` | Findings await a decision |
| `plan approve` | The run is `planning` and a finding is confirmed |
| `patch show` | A patch awaits approval, or, while `planning`, the latest patch was rejected (its reason is in `decisions`) |
| `patch apply` | A verified patch is ready |

Entries marked `requires_human_decision` are human gates: stop and hand them to the author.

## Run lifecycle

| Status | Meaning | Next step |
| --- | --- | --- |
| `preparing` | Snapshot, build, and bundle in progress | `run resume` if it stopped |
| `reviewing` | Review tasks are open | Work each task |
| `awaiting_decision` | Every profile role has an accepted review | `finding decide`, `run resume` |
| `planning` | Findings are confirmed; the lead and the author agree a plan | `plan check`, `plan approve` |
| `writing` | One writing task per assignment of the approved plan | Work each task |
| `awaiting_patch_approval` | A patch was proposed, or its verification failed | `patch show`, `patch decide`, `run resume` |
| `verifying` | A verification task is open | Work it from a new conversation |
| `ready_to_apply` | Verification passed | `patch apply` |
| `completed` | The patch was applied, or nothing was confirmed | `run gate`, `run report` |
| `failed` | An operation failed; `has_error` is true | Repair, then `run resume` if offered |
| `cancelled` | Ended by `run cancel` | None |

After the decisions, `run resume` moves the run to `planning` if any finding is confirmed and completes it if none
is. `plan approve` moves it to `writing`. When every assignment has an accepted writing output, Scriptorium applies
all their edits to one copy of the snapshot, compiles it, and proposes the patch (`awaiting_patch_approval`). After
the patch decision, `run resume` prepares a verification task for an approved patch, or returns a rejected one to
`planning`, where the lead reads the reason with `patch show` and changes the plan with the author; no task is
prepared until a new plan is approved. A failing verification returns the run to `awaiting_patch_approval`; reject
the patch there to plan again. In `planning` with no confirmed finding left, `run resume` completes the run.
`completed` and `cancelled` runs cannot be reopened.

## Working a task

### Claiming

```bash
scriptorium --json task claim TASK_ID --client claude_code --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
```

The host owns model choice, effort, authentication, and spending. Claim with the model and effort the host actually
selected; Scriptorium records them with the client and session as reported provenance and cannot verify them. Use
`--session-source host` only for an ID from the host's own state, otherwise `declared`. The same client, model,
effort, and session claiming again get the same running attempt; another session cannot take it. The response
gives `input_digest` to submit with and `export_command`.

### Export

`task export ATTEMPT_ID --dir DIR` writes into a directory that does not exist or is empty; otherwise it fails with
`configuration_error` and writes nothing.

| Path | Content |
| --- | --- |
| `sources/...` | Every frozen source at its `read_path` |
| `source-map.json` | Per source: `source_path`, `read_path`, `source_digest`, `line_count`, `text_anchorable`; the PDF's pages and `compiled_pdf.documents` |
| `manifest.json` | Commit, tree, sources with digests and line counts, PDF and page digests, documents |
| `navigation.json` | An index of literal source locations (below) |
| `manuscript.pdf` | The review PDF |
| `task/prompt.md` | This attempt's prompt, with correction feedback after a retry |
| `task/schema.json` | Review and verification tasks: the output schema |
| `task/brief.json` | Review and verification tasks: the review brief, when the run has one |
| `task/example.json` | Review tasks: a complete output that passed this task's checks |
| `task/plan.md` | Writing tasks: the approved plan |
| `task/assignment.md` | Writing tasks: this task's assignment file |
| `task/passages/N.EXT` | Writing tasks: the exact frozen bytes of target `N`'s line range (`N` is the 1-based target index, `.EXT` the source's suffix, such as `1.tex`), final line terminator included |

- Files hold the exact frozen bytes, checked against the bundle index or their recorded digest, and are written
  atomically and read-only (`0444`), except the passage files, which are writable (`0644`). A path that would leave `DIR` or collide with `task/` is refused before
  anything is written, and a failure removes everything the call wrote.
- Page images are not exported; use `task page`. The host owns `DIR`; Scriptorium never cleans it up.
- Export works for any attempt of the run, including finished ones, changes no workflow state, and appends one
  `tool.export` event with the sorted paths and digests.

Read the files with the host's own tools. A search hit can be a comment or inactive text; check that it belongs to
the compiled manuscript.

`navigation.json` entries have a `command` (such as `section`, `ref`, `cite`, `label`, `caption`,
`includegraphics`, `table`, `figure`, `equation`, `displaymath` for `\[ ... \]`, or `quantity`), `source_path`,
inclusive `start_line` and `end_line`, and the literal `value` (at most 2000 characters); graphics entries add
candidate paths. Environments span `\begin` to `\end` in the document body. Quantity entries mark `siunitx`
commands and numbers with a decimal point, uncertainty, exponent, `\pm`, or percent sign, with an adjacent unit;
integers, versions, labels, lengths, macro definitions, comments, and the preamble are skipped. Entries are
heuristic locations, not evidence: read the source range before relying on one.

### Pages

`task page` needs an active attempt and returns the path and digest of a rendered page. Open the image; a path is
not an inspection.

- `--number` is the 1-based global page of `manuscript.pdf`. With `--document ENTRYPOINT` it is the physical page
  within that document (`compiled_pdf.documents` gives each `entrypoint`, `start_page`, and `page_count`). Unknown
  entrypoints and out-of-range pages are rejected.
- `source_path` and `page` in the response are the evidence coordinates; `document` and `document_page` say where
  the page came from. Cite the global page, never a local number or a printed page label.
- `--scale` (0.5 to 4.0; frozen pages use 1.5) and `--crop x0,y0,x1,y1` (page fractions) render a view from the
  frozen PDF. The response keeps the frozen page `digest` and adds `view`. Views over 40 megapixels are rejected.
- `--text` writes the page's PDF text layer to a file and returns `path`, `digest`, `bytes`, and `evidence: false`.
  Extraction can reorder or drop text: use it to find material, and quote from the sources.
- Views and text files are kept in the run's `page-views/`, named by digest and never overwritten, up to 512 MiB
  per run; after that, reuse an earlier one.
- Each call appends a `tool.page` event with both coordinates. It shows what was rendered, not what was opened.

A non-text source, such as a figure PDF or PNG, takes no line anchor. Its `includegraphics` entry gives the TeX
location, not the page: find the page from the caption (the text layer helps), open it, zoom with
`--scale 3 --crop ...`, and cite the global page.

### Submitting

- `--file` takes one UTF-8 JSON object of at most 2 MB, or `-` for stdin; larger input is rejected before it is
  decoded.
- `--dir` takes a writing attempt's export directory, and only a writing attempt's. It reads the passage file of
  each target and `task/note.md` if present, and packs them into the stored output
  ([writing output](#writing-output)). A missing or non-UTF-8 passage file, or a note over 8,000 characters, is a
  `configuration_error` naming the file. `--file` with the packed JSON is equivalent.
- `--check` runs the full validation (schema, evidence, task context, a writing task's compile check, the
  verification session rule) under the run lock, for an active attempt and its input digest. It stores nothing but one `tool.submit_check` event (input and
  output digests, outcome, issue codes) and leaves the attempt claimable. `valid: true` means the file would be
  accepted now; it is not a receipt, and the submission validates again.
- A submission stores the raw output and validates it. A valid output completes the attempt and its task; an
  invalid one fails the attempt with a validation report, and nothing is taken from it.
- An accepted review completes its task and satisfies the review condition for its role; the task takes no
  further submission.
- Resubmitting the same output to the latest completed or failed attempt returns the same receipt. Other output
  for a finished attempt, and any output for a superseded, interrupted, or cancelled one, is rejected.

## Output rules

For review and verification tasks `task/schema.json` is authoritative: supply every required field and nothing
else. `task/example.json` shows the shape of a complete review output; do not copy its content. A writing task
returns passages, not JSON ([writing output](#writing-output)).

### Evidence anchors

| Kind | Fields | Rule |
| --- | --- | --- |
| Source text | `source_path`, `start_line`, `end_line`, `source_digest`, `quoted_text` | The bare `source_path` from `source-map.json` (not the `read_path`) of a `text_anchorable` source, its digest, an inclusive range within `line_count`, and a quote found verbatim in that range |
| PDF page | `source_path: "manuscript.pdf"`, `page` | A global page; no line, digest, or quote fields |

Lines are counted by the [evidence contract](architecture.md#evidence-contract). Use page anchors only for what
text cannot show: layout, graphics, colours, markings, rendering. Anchors are checked at submission against the
frozen sources, whatever the reviewer read.

### Review output

Every review role returns `summary`, `findings` (possibly empty), and `limitations`:

```json
{
  "summary": "The main result was checked against the frozen sources.",
  "findings": [],
  "limitations": ["The raw data the methods section cites are not among the frozen sources."]
}
```

- `limitations` lists relevant material the reviewer could not check or read, including material the manuscript
  mentions that is absent from the frozen sources; it may be empty. Only its shape is validated.
- A finding has `category`, `severity`, `title`, `claim`, `evidence` (at least one anchor), `explanation`,
  `suggested_action`, `confidence` (0 to 1), and `consequence`: what a reader would wrongly believe, or be unable
  to do, if it is not fixed.
- `copyedit`, `consistency`, and `figure_review` findings at `moderate` or above name an `affected_claim`, the claim
  or result whose reading changes. It is stored outside the finding's identity, so a later duplicate can fill a
  missing value; the first stays. `finding show` and `run report` show it. Substantive findings link claims
  through claim checks instead.

### Severity

| Level | Meaning |
| --- | --- |
| `blocker` | A headline claim is false as stated, or the manuscript cannot be evaluated |
| `major` | A headline claim needs substantive qualification, or a key result cannot be reproduced or traced |
| `moderate` | A supporting claim is affected, or a reader would misread a specific result or comparison |
| `minor` | A presentation, consistency, or compliance problem that does not change how any claim should be read |
| `suggestion` | An optional improvement |

- A value, margin, count, or ranking that disagrees between text, table, figure, or appendix is `moderate` only
  when a claim rests on it and `major` only when correcting it changes a headline claim. Mismatched names, labels,
  terms, captions, units, or bibliography fields are `minor` or `suggestion` unless they hide or alter a result.
  The consistency, figure-review, and copyedit prompts restate this; the copyedit and substantive prompts send
  value disagreements and presentation items to the roles that own them.
- Checklist answers, page or figure limits, anonymity, and template rules use category `submission_compliance`,
  accepted only at `minor` or `suggestion`.
- Severity is the model's declaration. Validation enforces the compliance ceiling and the required fields, not
  whether a level fits.

### Substantive review output

A `substantive_review` output adds three fields:

| Field | Content |
| --- | --- |
| `claim_inventory` | Each central claim: `claim`, `claim_anchor` (where the authors state it), `prominence` (`headline` for the abstract, contributions, or conclusions; `supporting` for claims they rest on), and `not_checked_reason` exactly when no check names it |
| `claim_checks` | `claim_index` (zero-based in `claim_inventory`), `evidence`, `critical_question`, `countercheck`, `stated_scope`, `check_type`, `question_answer`, `exceptions`, `recomputation`, `assessment`, `finding_indices` |
| `verdict` | `recommendation` (`accept`, `minor_revision`, `major_revision`, `reject`) and one to three `decisive_questions` |

`check_type` is `reporting_consistency`, `recomputation`, `design_and_analysis`, `alternative_explanation`, or
`scope_and_generality`; a `recomputation` object (`inputs`, `calculation`, `result`, `reported`, `outcome`
`matches` or `differs`) appears exactly for that type. Validation requires:

- at least one claim check, and a check of every headline claim (`question_answer` `not_checkable` when the bundle
  cannot decide it); a supporting claim may stay unchecked with its reason;
- `supported` exactly with `yes`; no `exceptions` with `yes` and at least one with `partly`; `unresolved` with
  `not_checkable`; no `yes` after a differing recomputation and no `no` after a matching one;
- `finding_indices` as zero-based positions in this output's `findings`: a `finding` check links at least one,
  `supported` and `unresolved` checks link none, and every finding is linked;
- every `blocker` linked from a check of a headline claim;
- `reject` or `major_revision` with at least one `major` or `blocker` finding of this output, and `accept` or
  `minor_revision` with none (`verdict.inconsistent`).

Every anchor, including claim anchors, and every link is validated before any finding is accepted. The prompt also
asks the reviewer to keep each claim's scope, counterevidence, assessment, and summary consistent: answering one
criticism does not support a broader claim, and exceptions and open questions stay visible. That judgment is the
model's; validation does not perform it. An `unresolved` claim may stand in an accepted review, with missing
evidence in `limitations`; that alone is not a finding. Claim checks are declarations shown for inspection, not
proof of scientific correctness.

### The plan directory

The lead writes the plan with the author in a directory anywhere on disk:

```text
DIR/plan.md                 free Markdown: the story the paper should tell; no required headings
DIR/assignments/NAME.md     one file per writing task
```

`plan.md` is a regular, non-blank UTF-8 file of at most 64 KB; Scriptorium treats it as opaque text. `assignments/`
holds 1 to 50 regular `NAME.md` files of at most 32 KB each, `NAME` matching `[a-z0-9][a-z0-9-]{0,62}`; `NAME` is the
assignment ID, and assignments are ordered by it. Any other entry in `assignments/` is an error; other entries in
`DIR` are ignored. Symbolic links are refused. An assignment starts with flat front matter, then non-blank Markdown
instructions:

```markdown
---
target: sections/results.tex:41-58
target: sections/abstract.tex:6-7
findings: finding_abc, finding_def
---
Limit the main conclusion to benchmark one. Keep the table numbers, citations, and the tab:main label.
```

- The front matter is the `key: value` lines between a first line `---` and the next `---`. `target` (one or more)
  is `PATH:START-END`, split at the last colon: the bare `source_path` of a `text_anchorable` source and a 1-based
  inclusive line range within its `line_count`. `findings` (at most once) lists finding IDs separated by commas and
  may be empty: an assignment may change the narrative without a finding.
- Every confirmed finding must be listed by an assignment. Waive a finding the author does not want changed
  (`finding decide --waive`).
- Targets on one path must not overlap, within or across assignments.

`plan check` reports these codes; `plan approve` refuses an invalid plan with the same issues in its error and records
nothing: `plan.missing` (`plan.md` is missing, blank, not UTF-8, or too large), `plan.no_assignments`,
`plan.too_many_assignments`, `plan.assignment_name_invalid`, `plan.front_matter_invalid` (also an assignment file
that is not UTF-8 or too large), `plan.assignment_empty`, `plan.target_path_unknown`, `plan.target_not_editable`,
`plan.target_out_of_bounds`, `plan.targets_overlap`, `plan.unknown_finding_ids` (not a confirmed finding of this
run), `plan.findings_unassigned`, and `plan.unchanged` (the plan's digest equals the latest approved plan's; the
run is planning again because that plan's patch was rejected, its outputs failed to compile together, or a waiver
voided it, and approving the same plan would reproduce that result). `plan check` echoes each assignment's `id`, `finding_ids`, and `targets` with
`index`, `path`, `start_line`, `end_line`, and the frozen text of the range's `first_line` and `last_line`, so the
author can confirm each range.

`plan approve` is the author's approval, a human gate. It publishes `plan.md` and every assignment file as
artifacts, records the plan (its `digest` covers the plan text, every assignment file, its targets, and its finding
IDs) with a `plan` decision, and prepares the plan's writing tasks. The run's latest plan is its current one, and it
stays current only while every finding it lists is still confirmed.

A writing task is identified by its content: the `plan.md` text, the assignment ID, and the assignment file. When a
later plan has a byte-identical `plan.md` and an assignment with the same name and a byte-identical file, that
assignment keeps its accepted writing output and gets no new task; only changed and new assignments are rewritten. A
changed `plan.md` rewrites every assignment. The candidate patch combines the kept and new outputs and records the
latest plan. Pending and claimed tasks of assignments the latest plan no longer holds are cancelled or interrupted.

### Writing output

The writer rewrites the exported passage files: each file's whole content becomes the new text of its target's line
range, an unchanged file keeps the range, and an empty file deletes those lines. It may add `task/note.md` for the
author, such as where an assignment asks for something the manuscript's evidence does not show. `task submit --dir`
stores this as `{"passages": [{"index": 1, "text": "..."}], "note": ...}`.

- `writing.passages_mismatch`: the indexes must be exactly the assignment's targets, each once.
- When a target's frozen text ends with a line terminator and the submitted text is not empty and ends with none,
  the frozen terminator is kept.
- `writing.no_change`: at least one target must change.
- `writing.build_failed`: at submission, and for `--check`, the assignment's edits alone are applied to a copy of
  the snapshot and compiled; a LaTeX error rejects the output with the end of the build log, so `run retry` returns
  the task to a writer with the log in its correction prompt. A missing tool or other infrastructure failure leaves
  the attempt running and is inconclusive.

Scriptorium builds one exact edit per changed target, with the assignment's finding IDs, the frozen text as
`before`, and the replacement as `after`; a writer never supplies line numbers or a diff. When every assignment is
accepted, all edits are applied together and compiled. If the combined copy fails to compile although each
assignment compiled alone, the run returns to `planning` with the error in `run status` and a `plan.build_failed`
event naming the build log artifact; change the plan.

### Verification output

`verdict` (`pass` or `fail`), `summary`, `resolved_finding_ids`, and `issues` (`title`, `explanation`, optional
`evidence` against the patched sources). A `pass` resolves every confirmed finding and has no issues. The attempt
must be claimed with `--session-source host` from a session that did no review or writing work in this run;
otherwise the submission is rejected with `verification.session_unconfirmed`.

### Validation report

`validation_report.issues` lists each problem with a `code`, a JSON-pointer `path`, a `message`, often `expected`
and `actual`, and a `diff` for a `before` mismatch. Code prefixes: `json.` (invalid JSON, often an unescaped LaTeX
backslash), `schema.` (shape), `evidence.` (for example `evidence.source_quote_mismatch`,
`evidence.source_digest_mismatch`, `evidence.range_out_of_bounds`, `evidence.page_out_of_bounds`), `verdict.`,
`writing.`, and `verification.`.

## Deciding findings

At `awaiting_decision`:

1. Write `run report RUN_ID --part findings_grouped` to a file and read it.
2. Present the `verdict`, then the groups in tier order. For each, say in one line whether it changes how a headline
   claim reads, using its `claim` and `consequence`, and propose confirm, reject, or waive.
3. Record only the author's decisions, per group or per tier, with one `finding decide` over the group's
   `pending_finding_ids`, never IDs already decided. Then `run resume`.

Confirm sends a finding to the plan, reject dismisses it, and waive accepts it without a change. Never infer a
decision from severity or tier. `finding decide` applies one decision and reason to each listed finding: all IDs
must exist, without repeats, and belong to one run that accepts the decision, or nothing is recorded; otherwise
each gets its own decision record and event, in one replacement of the run state. Outside `awaiting_decision` only
a waiver is accepted, and nothing once the run is completed, failed, or cancelled.

### How findings are grouped

`findings_grouped` groups the run's findings across roles and attempts. Two findings share evidence when:

- both have line anchors and more than half of either one's distinct line anchors overlap a line anchor of the
  other (same source, inclusive ranges sharing at least half of the shorter); when either has a line anchor, line
  anchors decide;
- both have only page anchors, share a linked claim, and more than half of either one's pages appear in the other;
- they share a linked claim and cite the same table or figure; or
- they cite the same table or figure and a line anchor of one overlaps a line anchor of the other.

A finding cites a table or figure when a line anchor lies inside a `table`, `table*`, `sidewaystable`,
`longtable`, `figure`, or `figure*` environment, or covers a `ref`, `cref`, `Cref`, `autoref`, `eqref`, or
`pageref` to a `label` inside one. A label defined more than once names nothing, and text only on an environment's
`\begin` or `\end` line counts as outside it.

Groups do not chain. In primary order (highest severity, then `substantive_review`, then earliest), each ungrouped
finding starts a group and takes every later ungrouped finding that shares evidence with it. Linked claims come
from claim checks (with prominence) and from `affected_claim` (without); a finding repeated by a later task
resolves to the stored one through its duplicate record.

The section has `verdict` (the latest substantive `attempt_id`, `recommendation`, and `decisive_questions`, or
null), `counts` of groups and findings per tier, and `groups`. Each group has `group_id` (from its sorted member
IDs), `primary_finding_id`, sorted `finding_ids`, `members` (`finding_id`, `role`, `severity`, `status`),
`titles`, `roles` (including roles whose identical finding was stored as a duplicate), `max_severity`,
`categories`, merged `evidence`, `claim` (the most prominent linked claim: `text`, `prominence`, `finding_id`), the
primary's `consequence`, `tier`, `decision_state` (the shared status, or `mixed`), and `pending_finding_ids`.

`tier` is `headline` when a member links to a headline claim, `supporting` for any other link, `compliance` when
every member is `submission_compliance`, and otherwise `other`. Groups are ordered by tier (headline, supporting,
other, compliance), then severity, then primary ID. Grouping is a reading aid and changes no finding, decision, or
gate.

### Decision statistics

`decision_stats` counts findings by current decision (`confirmed`, `rejected`, `waived`, `pending`) in total and by
role, category, severity, and role with severity, and lists the latest rejection and waiver reasons (first 200
characters, newest first, at most 50). A finding decided twice counts once, under its latest decision. Rows are
lists, sorted by name and from `blocker` to `suggestion`. Use it to calibrate the next brief's `severity_notes`; it
is not a quality metric and does not affect the gate.

## Patches, verification, and application

1. After the writing tasks, read the patch with `patch show` (its `plan_id` names the plan it came from, and its
   `summary` carries the writers' notes), record the author's decision with `patch decide` (only the latest patch
   can be decided), and `run resume`.
2. Work the verification task from a new host session ([verification output](#verification-output)).
3. Apply the verified patch with `patch apply`. It writes only the files the patch edits, after checking that each
   still matches the frozen revision; if one does not, the patch becomes `stale`, the run fails, and nothing is
   written.
4. Read `run gate` ([conditions](architecture.md#gates)) and `run report`.

## Report sections

`run report RUN_ID --part PART` prints the value under that key of the JSON report (inside `data` with `--json`);
it cannot be combined with `--format`, which prints the whole report as `markdown` (default) or `json`. Redirect
large output to a file:

```bash
scriptorium run report RUN_ID --part findings_grouped > findings_grouped.json
scriptorium run report RUN_ID --format markdown > report.md
```

| Part | Content |
| --- | --- |
| `run` | The run record: frozen configuration, error, and the full brief |
| `tasks` | Each task with its attempts; a writing task also names its `assignment_id` |
| `finding_ids`, `patch_ids` | IDs in order |
| `findings` | Each finding with its decisions; status follows the latest decision |
| `findings_grouped` | [Groups](#how-findings-are-grouped) |
| `decision_stats` | [Decision counts](#decision-statistics) |
| `plans` | Each approved plan (digests, assignments with targets and finding IDs) with its approval decision |
| `patches` | Each patch, with the plan it came from, its decisions, and verifications |
| `events` | The append-only event log, including `tool.*` events |
| `validation_reports` | Every validation report, oldest first |
| `review_limitations` | Each accepted review's `limitations`, with task, attempt, and role |
| `review_claim_checks` | Each accepted substantive output's `submitted_findings` (in submitted order, which `finding_indices` refer to), `claim_checks`, `claim_inventory`, `verdict` |
| `gate` | The release gate result |

The Markdown report has the run header, tasks, reviewer limitations, claim checks (verdict first), validation
failures, findings by group, findings, decision statistics, plans, patches, and open gate conditions. Reports validate
stored outputs again, so a corrupt artifact fails the report. When `has_error` is true, read `run`,
`validation_reports`, and `gate`.

## Recovery

- **Rejected submission.** Read every issue. Fix the output, run `run retry RUN_ID --task TASK_ID`, claim again,
  export the new attempt, and submit with its new ID and input digest; its prompt carries the frozen diagnostics.
  A failed attempt never accepts a correction. Retries are explicit and unlimited.
- **Long or lost sessions.** A claim survives CLI exit and has no time limit. A host exiting or printing success is
  not completion: check the receipt and `run status`. After a disconnect, read the attempt with `task show` and
  rejoin from the same session before abandoning it.
- **Abandoned attempt.** `run retry RUN_ID --task TASK_ID --abandon-attempt ATTEMPT_ID --reason TEXT` interrupts
  only the running attempt and reopens the task; a late submission to it is rejected. Only abandonment, retry, or
  cancellation ends a claim.
- **Accepted work not replayed.** If a process stopped after accepting an output, `run status` offers `run resume`.
- **Waiver after confirmation.** Waiving a finding the current plan lists while the run is `writing` interrupts or
  cancels the open writing tasks and returns the run to `planning`; approve a plan without it. Later, a waiver
  interrupts an open verification task whose prompt no longer matches, and `run resume` prepares its replacement.
- **Rejected patch or a plan that does not compile.** The run is `planning`: read the reason (`patch show`, or the
  `run status` error and the `plan.build_failed` event), change the plan with the author, `plan check`, and
  `plan approve`.
- **Failed run.** Repair the cause, such as a missing tool or a failed build. `run status` offers `run resume` only
  when the stage before the failure can be recovered. A failed run cannot be cancelled. If committed inputs or
  configuration must change, commit them and start a new run.
- **Busy run.** A command needing the run lock while another holds it fails with `invalid_state` naming the holder;
  `run cancel`, `task export`, `task page`, and `task submit --check` wait instead.
- **Cancel.** `run cancel` waits for the current operation, cancels pending tasks, and interrupts active attempts.
  It does not stop the host conversation.

Never repair a run by editing `.scriptorium/`; fix the prerequisite and use the CLI.
