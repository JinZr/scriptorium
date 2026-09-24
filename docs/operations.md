# Operations and recovery

Run commands from the manuscript Git repository and use `--json` for machine-readable output. The JSON envelope has `ok` and either `data` or a stable error `code` and `message`.

```bash
scriptorium --json doctor --revision COMMIT --profile full
scriptorium --json run start --revision COMMIT --profile full
scriptorium --json task list RUN_ID
scriptorium --json task claim TASK_ID --client codex --model MODEL --effort EFFORT \
  --session-id SESSION_ID --session-source host
scriptorium --json task show ATTEMPT_ID
```

A claimed attempt remains active when the CLI exits. The `input_digest` returned by `task claim` or `task show` for that attempt must accompany submission:

```bash
scriptorium --json task search ATTEMPT_ID --query phrase --cursor 0 --limit 20
scriptorium --json task read ATTEMPT_ID --path main.tex --start-line 1 --max-lines 40
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest DIGEST --file answer.json
```

`task read` supports `manifest.json`, `navigation.json`, `source-map.json`, and text sources named in the source map (either `source_path` or `read_path`). It returns at most 8,000 characters per call, with `next_line` and `next_offset` when a read was truncated. `task search` searches those frozen text sources and metadata files, with `next_cursor` for further matches. `task page` returns the exact rendered image path and digest; the host must actually open the image for a visual review.

`task submit --file` accepts UTF-8 JSON up to 2 MB from a file or stdin. The CLI rejects larger input before decoding or passing it to the workflow.

For a new review task, include `scope` alongside `summary` and `findings`:

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

Use bare frozen `source_path` values. For a text source, omit both line endpoints to declare the whole file, or supply both as an inclusive range. For the compiled PDF, use `source_path: "manuscript.pdf"` and a 1-based `page`. Mark incomplete or uncertain work honestly. The report keeps this declaration separate from tool-return events; neither is proof that the host opened or understood material. Old runs keep their frozen review schema and report scope as not reported. Scope does not decide findings or the release gate.

When output is invalid, inspect the returned validation report. No findings, patch, or verification are accepted from that attempt. Explicitly run `scriptorium --json run retry RUN_ID --task TASK_ID`, then claim again; the next prompt includes the durable diagnostics and has a different input digest. If an active external conversation was abandoned, run `scriptorium --json run retry RUN_ID --task TASK_ID --abandon-attempt ATTEMPT_ID --reason TEXT` to interrupt only that attempt and make the task claimable again. Its late submission is rejected. Repeated identical submissions are idempotent. Different output for a terminal attempt and output for a superseded or cancelled attempt are rejected.

After review tasks complete, use `finding list/show/decide` for human decisions, then `run resume`. A confirmed finding prepares a revision task. After submitting revision output, inspect the patch and decide it explicitly. An approved patch prepares a verification task. Use a new external conversation and the host's session ID for verification; `--session-source declared` marks identity as unconfirmed and cannot pass the gate. Apply only a verified patch, then inspect `run gate` and `run report`.

`run cancel RUN_ID --reason TEXT` waits for any current run operation to release the lock, then durably cancels pending tasks and invalidates active attempts. It does not terminate an external client conversation. A later waiver that changes an active revision or verification task interrupts that attempt; run `run resume` to prepare the replacement task. Do not edit `.scriptorium/` or its database, artifacts, snapshots, or bundles to repair a run. Fix the prerequisite and use the CLI. Old internal-SDK runs can be inspected with status, report, and gate commands without upgrading their database. Finish any active old run with the original version before starting an external run in the same repository; that explicit start upgrades the database and the original version cannot open it afterward.
