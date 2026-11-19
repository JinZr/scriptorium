---
name: scriptorium
description: Operate Scriptorium review runs on a Git-managed LaTeX manuscript from Codex, Claude Code, or Antigravity. Agree a review brief with the author, start or resume runs, read reports, triage findings, lead the revision plan with the author, and record only the decisions, plan approvals, and patch approvals the author gives.
---

# Scriptorium operator

You run the workflow, talk with the author, and lead the revision plan. To work a claimed review or verification
task, use the `scriptorium-review` skill; that can be this session. Run `scriptorium --json` from the manuscript
project that owns the run. Never edit `.scriptorium/`, its run state files, artifacts, snapshots, bundles, or generated patches.

## Quick reference

```text
scriptorium --json doctor --revision REV --profile PROFILE          -> ok, detected_template
scriptorium --json run start --revision REV --profile PROFILE --brief brief.json [--allow-duplicate]
scriptorium --json run status RUN_ID                                -> next_actions; follow them
scriptorium --json run list [--status STATUS] [--limit N]           -> newest first; find RUN_ID
scriptorium --json run report RUN_ID --part PART > PART.json       -> one whole report section
scriptorium --json finding decide ID [ID ...] --confirm|--reject|--waive --reason TEXT
scriptorium --json plan check RUN_ID --dir DIR                      -> valid, issues, echoed ranges
scriptorium --json plan approve RUN_ID --dir DIR --reason TEXT
scriptorium --json patch decide PATCH_ID --approve|--reject --reason TEXT
scriptorium --json patch apply PATCH_ID
scriptorium --json run gate RUN_ID
scriptorium --json run resume RUN_ID
scriptorium --json run retry RUN_ID --task TASK_ID [--abandon-attempt ATTEMPT_ID --reason TEXT]
scriptorium --json run cancel RUN_ID --reason TEXT
```

## Review brief

Before a new run, agree a short brief with the author. `doctor` compiles the frozen commit without calling a model;
fix what it reports until `ok` is true. Infer the venue family (`ml_conference`, `nature_family`, or
`other`) from its `detected_template`. If that is `unknown`, ask the author which family applies, and use `other`
when they do not know. `reference/venues/<family>.md` lists what to ask and the defaults. Ask only what the template
does not tell you: the stage (`internal_draft`, `presubmission`, `rebuttal_revision`, or `camera_ready`), the claims
that must survive, earlier reviews, known weaknesses, and what to ignore. Show the draft; the author confirms or edits it. Write `brief.json` (format: `reference/brief.md`) and pass it to
`run start --brief`. The brief is frozen into every review prompt and cannot change for that run.

## Start, follow, recover

`run start` freezes and compiles the commit and prepares review tasks, calling no model. It refuses with
`duplicate_run` while a non-terminal run exists on that commit: continue it, or pass `--allow-duplicate` only when the
author wants a second run. Then read `run status RUN_ID` and follow its `next_actions`, rereading after each change. Use IDs and digests from the
CLI, never guesses. If a run ID is missing or not found, read `run list`; do not search other repositories.

`task claim` actions go to a reviewer or writer session. `run retry` follows a rejected submission; add
`--abandon-attempt` only for a truly stuck attempt, since a claim survives CLI exit. `run resume` advances after a
decision, or recovers a failed run when status offers it; repair the external prerequisite first.

## Reports

`run status` lists `report_parts`. `run report RUN_ID --part PART > PART.json` writes one whole section as JSON;
read the file. `--format json|markdown` writes the full report. Send `task list`, `finding list/show`, and
`patch show` to files too. If `has_error` is true, read the `run`, `validation_reports`, and `gate` sections.

`decision_stats` counts past decisions with recent rejection and waiver reasons; use it at the next intake to
calibrate `severity_notes`, not as a quality score.

## Triage

At `awaiting_decision`, read `findings_grouped`. Present the `verdict` first, then the groups in tier order:
headline, supporting, other, compliance. For each group, say in one line whether it changes how a headline claim
reads, using its `claim`, `consequence`, and the verdict, and propose confirm, reject, or waive. The author answers
per group or per tier ("waive all compliance"). Record each answer with one `finding decide` over that group's
`pending_finding_ids` only, never IDs already decided.

## Planning

At `planning` you are the lead. Agree the story with the author: what the paper claims after revision, what each
confirmed finding changes, and which findings to waive instead. Draft the plan directory (format and template:
`reference/plan.md`), reading each target's lines before naming its range. Run `plan check` until `valid` is true,
show the author the plan, each assignment, and the echoed `first_line` and `last_line` of every range, and run
`plan approve` only on the author's word. After a rejected patch, read the reason with `patch show` and change the
plan with the author. Hand the writing tasks to a writer session, such as another host like Antigravity, using
`scriptorium-review`.

## Authorization

Finding decisions, plan approval, patch approval, and patch application need the author's explicit instruction. Use an
authorization already given in this session; otherwise show the exact finding, plan, or patch (from the `findings`,
`plans`, or `patches` report part) and the proposed decision, and wait. Never record an approval the author did not
give or infer one from severity, tier, or your own review; never edit manuscript files to mimic a patch. A separate
conversation verifies an approved patch; apply only a verified patch, then read `run gate RUN_ID`. Inconclusive or
failed verification never passes the gate.

## Reporting back

Report the run, task, attempt, finding, plan, and patch IDs; host model and effort; validation status; the current
human gate; the gate result. Scriptorium records no usage or cost.
