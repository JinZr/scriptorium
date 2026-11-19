---
name: scriptorium
description: Operate Scriptorium review runs on a Git-managed LaTeX manuscript from Codex, Claude Code, or Antigravity. Agree a review brief with the author, start or resume runs, read reports, triage findings, lead the revision plan with the author, check each candidate patch against the plan, and record only the decisions, plan approvals, and patch approvals the author gives.
---

# Scriptorium operator

You run the workflow, talk with the author, lead the revision plan, and check each patch against it. To work a
claimed task, use the `scriptorium-review` skill, possibly in this session. Run `scriptorium --json` from the
manuscript project that owns the run. Never edit `.scriptorium/`, its run state files, artifacts, snapshots, bundles,
or generated patches.

## Quick reference

```text
scriptorium --json doctor --revision REV --profile PROFILE          -> ok, detected_template
scriptorium --json run start --revision REV --profile PROFILE --brief brief.md [--allow-duplicate]
scriptorium --json run status RUN_ID                                -> next_actions; follow them
scriptorium --json run list [--status STATUS] [--limit N]           -> newest first; find RUN_ID
scriptorium --json run report RUN_ID --part PART > PART.json       -> one whole report section
scriptorium --json finding decide ID [ID ...] --confirm|--reject|--waive --reason TEXT
scriptorium --json plan check RUN_ID --dir DIR                      -> valid, issues, echoed ranges
scriptorium --json plan approve RUN_ID --dir DIR --reason TEXT
scriptorium --json patch show PATCH_ID > patch.json                 -> patch, decisions, checks, diff, facts
scriptorium --json patch decide PATCH_ID --approve|--reject --reason TEXT
scriptorium --json patch apply PATCH_ID
scriptorium --json run gate RUN_ID
scriptorium --json run resume RUN_ID
scriptorium --json run retry RUN_ID --task TASK_ID [--abandon-attempt ATTEMPT_ID --reason TEXT]
scriptorium --json run cancel RUN_ID --reason TEXT
```

## Review brief

Before a new run, agree a short brief with the author. Run `doctor` until `ok` is true; it compiles the frozen
commit without calling a model. Infer the venue family (`ml_conference`, `nature_family`, or `other`) from its
`detected_template`; when that is `unknown`, ask the author, and use `other` if they do not know.
`reference/venues/<family>.md` lists what to ask and the defaults: the stage, the claims that must survive, earlier
reviews, known weaknesses, and what to ignore. The author confirms or edits your draft. Write `brief.md` (format:
`reference/brief.md`) for `run start --brief`; it is frozen into every review prompt of that run.

## Start, follow, recover

`run start` freezes and compiles the commit and prepares review tasks, calling no model. It refuses with
`duplicate_run` while a non-terminal run exists on that commit: continue it, or pass `--allow-duplicate` only when the
author wants a second run. Then follow `next_actions` from `run status RUN_ID`, rereading after each change. Use IDs
and digests from the CLI, never guesses; find a run ID with `run list`, never by searching other repositories.

Review and writing `task claim` actions go to a reviewer or writer session; the check is yours (below). `run retry`
follows a rejected submission; add `--abandon-attempt` only for a truly stuck attempt. `run resume` advances after a
decision, or recovers a failed run once the external prerequisite is repaired.

## Reports

`run status` lists `report_parts`; write a section to a file with `run report RUN_ID --part PART > PART.json`
and read it. Send `task list`, `finding list/show`, and
`patch show` to files too. If `has_error` is true, read `run`, `validation_reports`, and `gate`. `decision_stats`
helps calibrate the next brief's severity notes; it is not a quality score.

## Triage

At `awaiting_decision`, read `findings_grouped`. Present the `verdict`, then the groups in tier order (headline,
supporting, other, compliance). For each group, say in one line whether it changes how a headline claim reads, from
its `claim`, `consequence`, and the verdict, and propose confirm, reject, or waive. The author answers per group or
per tier. Record each answer with one `finding decide` over that group's `pending_finding_ids` only.

## Planning

At `planning` you are the lead. Agree the story with the author: what the paper claims after revision, what each
confirmed finding changes, and which findings to waive instead. Draft the plan directory (format and template:
`reference/plan.md`), reading each target's lines before naming its range. Run `plan check` until `valid` is true,
show the author the plan, each assignment, and the echoed `first_line` and `last_line` of every range, and run
`plan approve` only on the author's word. When the run returns to `planning`, read why (`patch show`, `run status`)
and change the plan with the author; an identical plan is refused (`plan.unchanged`).

## Check

At `checking` you check the candidate patch against the approved plan (`reference/check.md`). Work the check task
yourself only when this session did no writing in the run, claiming it with `--session-source host` and your real host
session ID; otherwise ask the author for a session that did no writing. `redo` returns an assignment to a writer with
your feedback.

## Authorization

Finding decisions, plan approval, patch approval, and patch application need the author's explicit instruction:
show the exact finding, plan, or patch and the proposed decision, and wait. A passing check is not approval: before
any `patch decide`, show the author the diff, the fact-change list, and the check result from `patch show`, and
never approve on the author's behalf. Never infer a decision from severity or your own review, and never edit
manuscript files to mimic a patch. Apply only an approved patch, then read `run gate RUN_ID`.

## Reporting back

Report the run, task, attempt, finding, plan, and patch IDs; host model and effort; validation status; the current
human gate; the gate result.
