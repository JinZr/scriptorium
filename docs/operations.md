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

A claimed attempt remains active when the CLI exits. The task's `input_digest` must accompany submission:

```bash
scriptorium --json task search ATTEMPT_ID --query phrase --cursor 0 --limit 20
scriptorium --json task read ATTEMPT_ID --path main.tex --start-line 1 --max-lines 40
scriptorium --json task page ATTEMPT_ID --number 1
scriptorium --json task submit ATTEMPT_ID --input-digest DIGEST --file answer.json
```

`task read` supports `navigation.json`, `source-map.json`, and text sources named in the source map. It returns `next_line` and `next_offset` when a read was truncated. `task search` searches frozen text sources and navigation, with `next_cursor` for further matches. `task page` returns the exact rendered image path and digest; the host must actually open the image for a visual review.

When output is invalid, inspect the returned validation report. No findings, patch, or verification are accepted from that attempt. Explicitly run `scriptorium --json run retry RUN_ID --task TASK_ID`, then claim again; the next prompt includes the durable diagnostics. Repeated identical submissions are idempotent. Different output for a terminal attempt and output for a superseded or cancelled attempt are rejected.

After review tasks complete, use `finding list/show/decide` for human decisions, then `run resume`. A confirmed finding prepares a revision task. After submitting revision output, inspect the patch and decide it explicitly. An approved patch prepares a verification task. Use a new external conversation and the host's session ID for verification; `--session-source declared` marks identity as unconfirmed and cannot pass the gate. Apply only a verified patch, then inspect `run gate` and `run report`.

`run cancel RUN_ID --reason TEXT` durably cancels pending tasks and invalidates active attempts. It does not terminate an external client conversation. Do not edit `.scriptorium/` or its database, artifacts, snapshots, or bundles to repair a run. Fix the prerequisite and use the CLI. Old internal-SDK runs can be inspected with status, report, and gate commands but cannot be resumed by this version.
