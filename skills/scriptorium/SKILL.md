---
name: scriptorium
description: Operate the Scriptorium CLI for auditable review, revision, recovery, verification, reporting, and patch application in Git-managed LaTeX manuscript repositories. Use when a user asks to start or inspect a Scriptorium run, review findings, record human decisions, inspect or approve a proposed patch, resume or retry interrupted work, evaluate the release gate, or apply a verified patch.
---

# Scriptorium

Use Scriptorium only through its installed CLI. Keep the CLI as the authority for workflow state and never edit `.scriptorium/`, SQLite, artifacts, snapshots, bundles, or generated patches directly.

## Inspect before acting

Run the following from the Git-managed LaTeX repository:

```bash
scriptorium --json doctor
```

For an existing run, inspect it before any other run operation:

```bash
scriptorium --json run status RUN_ID
```

Use JSON output for every command. Read `ok`, the stable error `code`, and returned IDs instead of parsing prose. If `doctor` fails, report the exact error and stop rather than bypassing the check.

## Follow the workflow

Use only the command appropriate for the run's current state:

```text
run start
→ finding list/show/decide
→ run resume
→ patch show/decide
→ run resume
→ patch apply
→ run gate
→ run report
```

Useful read-only commands:

```bash
scriptorium --json finding list RUN_ID
scriptorium --json finding show FINDING_ID
scriptorium --json patch show PATCH_ID
scriptorium --json run report RUN_ID --format json
scriptorium --json run gate RUN_ID
```

For recovery, inspect status first, then use the CLI-reported task and run IDs:

```bash
scriptorium --json run resume RUN_ID
scriptorium --json run retry RUN_ID --task TASK_ID
```

Never invent or infer an ID. Always return every relevant run, task, finding, and patch ID to the user.

## Enforce authorization gates

Obtain explicit user authorization immediately before:

- starting, resuming, or retrying work that may invoke a paid route;
- recording any finding decision;
- approving or rejecting a patch;
- applying a patch.

Do not treat a request to review, inspect, summarize, or resume as authorization for one of these actions. State the exact command, target ID, decision, reason, route/profile, and budget as applicable. After authorization, run only that command.

Before requesting authorization for a paid start, validate the proposed profile and budget with a read-only preflight:

```bash
scriptorium --json doctor --profile PROFILE --budget-usd N
```

After authorization, run exactly one requested command:

```bash
scriptorium --json run start --revision REVISION --profile PROFILE --budget-usd N
scriptorium --json run resume RUN_ID
scriptorium --json run retry RUN_ID --task TASK_ID
scriptorium --json finding decide FINDING_ID --confirm --reason "TEXT"
scriptorium --json finding decide FINDING_ID --reject --reason "TEXT"
scriptorium --json finding decide FINDING_ID --waive --reason "TEXT"
scriptorium --json patch decide PATCH_ID --approve --reason "TEXT"
scriptorium --json patch decide PATCH_ID --reject --reason "TEXT"
scriptorium --json patch apply PATCH_ID
```

Do not choose a finding outcome, invent a reason, approve a patch, or apply changes on the user's behalf. Do not edit manuscript files directly to reproduce a Scriptorium patch.

## Report outcomes

After each command, report:

- whether it succeeded;
- the current run state;
- the relevant IDs;
- the next human gate or safe read-only command;
- any stable error code and message.

Do not claim that a provider-level spending limit was enforced. Scriptorium's budget is a local scheduling and audit estimate.
