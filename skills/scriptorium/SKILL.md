---
name: scriptorium
description: Operate Scriptorium review runs on a Git-managed LaTeX manuscript from Codex, Claude Code, or Antigravity. Agree a review brief with the author, start or resume runs, read bounded reports, triage findings, and record only the decisions and patch approvals the author gives.
---

# Scriptorium operator

You run the workflow and talk with the author. To work a claimed review, revision, or verification task, use the
`scriptorium-review` skill; that can be this session. Run `scriptorium --json` from the manuscript project that owns
the run. Never edit `.scriptorium/`, its SQLite database, artifacts, snapshots, bundles, or generated patches.

## Quick reference

```text
scriptorium --json doctor --revision REV --profile PROFILE          -> ok, detected_template
scriptorium --json run start --revision REV --profile PROFILE --brief brief.json [--allow-duplicate]
scriptorium --json run status RUN_ID                                -> next_actions; follow them
scriptorium --json run report RUN_ID --part PART                    -> follow next_command until null
scriptorium --json finding decide ID [ID ...] --confirm|--reject|--waive --reason TEXT
scriptorium --json patch decide PATCH_ID --approve|--reject --reason TEXT
scriptorium --json patch apply PATCH_ID
scriptorium --json run gate RUN_ID
scriptorium --json run resume RUN_ID
scriptorium --json run continue RUN_ID --task TASK_ID
scriptorium --json run retry RUN_ID --task TASK_ID [--abandon-attempt ATTEMPT_ID --reason TEXT]
scriptorium --json run cancel RUN_ID --reason TEXT
```

## Review brief

Before a new run, agree a short brief with the author. `doctor` compiles the frozen commit without calling a model;
fix what it reports until `ok` is true. Infer the venue family (`ml_conference`, `nature_family`, or
`other`) from its `detected_template`. If that is `unknown`, ask the author which family applies, and use `other`
when they do not know. `reference/venues/<family>.md` lists what to ask and the defaults. Ask only what the template
does not tell you: the stage (`internal_draft`, `presubmission`, `rebuttal_revision`, or `camera_ready`), the two or
three claims that must survive, earlier reviews, known weaknesses, and what to ignore this round. Show the draft and
let the author confirm, edit, or say "defaults". Write `brief.json` (format: `reference/brief.md`) and pass it to
`run start --brief`. The brief is frozen into every review prompt and cannot change for that run.

## Start, follow, recover

`run start` freezes and compiles the commit and prepares review tasks without calling a model. It refuses with
`duplicate_run` while a non-terminal run exists on that commit: continue that run, and pass `--allow-duplicate` only
when the author wants a second independent run. Then read `run status RUN_ID` and follow its `next_actions`. Read status
again after each change. Use IDs and digests from the CLI, never guessed values. If a run is not found, check the project directory with the author; do not search other repositories.

`task claim` actions go to a reviewer session. `run continue` reopens a task whose accepted scope is `partial` or
`unknown`. `run retry` follows a rejected submission; add `--abandon-attempt` only for a truly stuck attempt, since a
claim survives CLI exit and long work is not failure. `run resume` advances
after a decision, or recovers a failed run when status offers it; repair the external prerequisite first.
`run cancel` ends a run.

## Reports

`run status` lists `report_parts`. Read one with `run report RUN_ID --part PART`: follow every `next_command`
unchanged until null, concatenate `text` in offset order without separators, and parse the whole value. Each response
is at most 7,000 bytes. Changed reports and reading several parts from one state: `reference/fragments.md`.
`task list`, `finding list/show`, `patch show`, and full `--format` exports are unbounded; keep them out of context.

`decision_stats` counts past decisions by role, category, and severity, with recent rejection and waiver reasons.
Bring it to the next intake as calibration for `severity_notes`, not as a quality score.

## Triage

At `awaiting_decision`, read `findings_grouped`. Present the `verdict` first, then the groups in tier order:
headline, supporting, other, compliance. For each group, say in one line whether it changes how a headline claim
reads, using its `claim`, `consequence`, and the verdict, and propose confirm, reject, or waive. The author answers
per group or per tier ("waive all compliance"). Record each answer with one `finding decide` over that group's
`pending_finding_ids` only, never IDs already decided.

## Authorization

Finding decisions, patch approval, and patch application need the author's explicit instruction. Use an
authorization already given in this session; otherwise show the exact finding or patch (from the `findings` or
`patches` report part) and the proposed decision, and wait. Never infer approval from severity, tier, or your own
review, and never edit manuscript files to mimic a patch. An approved patch is verified by a separate conversation;
apply only a verified patch, then read `run gate RUN_ID`. Inconclusive or failed verification never passes the gate.

## Reporting back

Report the run, task, attempt, finding, and patch IDs; the host model and effort; retrieval gaps; validation status;
the current human gate; and the gate result. Unknown token usage or cost stays unknown.
