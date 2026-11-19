# Checking a patch

When every assignment has an accepted writing output, Scriptorium builds and compiles one candidate patch and the
run becomes `checking`. You, the lead, check it against the plan the author approved. The rules an output must meet
are in `docs/operations.md` under "Check output".

## Who checks

A check counts only from a host-reported session that did no writing in this run. Claim the check task with
`--session-source host` and the session ID from the host's own state. A session that reviewed or checked earlier in
the run may check; a session that wrote may not, and a declared session cannot pass (`check.session_unconfirmed`).
If this session wrote, ask the author to open another one; never invent or borrow an ID.

## How to check

1. Claim, export, and read the task with the `scriptorium-review` skill. The export holds the patched manuscript;
   the prompt holds the plan, each assignment with the writer's note and attempt, the confirmed findings, the diff,
   and the fact-change list.
2. For each assignment, accept it only when its new text does what the assignment asks, keeps what the plan says
   must not change, and resolves every finding it lists. Otherwise return it (`redo`) with feedback the writer can
   act on: what is wrong, where, and what the text must do instead, within the assignment.
3. This is a check of the patch against the plan, not a new review of the paper. Raise nothing the plan does not
   ask for; if the plan itself is wrong, accept or return the assignments as written and tell the author.
4. Use the fact-change list as a starting point: for each removed or added number, citation, reference, label, or
   graphic, confirm the assignment allows it. It comes from the heuristic navigation index, so it is not evidence,
   an empty list does not prove that no fact changed, and nothing is gated on it.

A returned assignment gets a redo task whose passages hold the returned text; hand it to a writer session. Accepted
assignments keep their outputs. Each new candidate gets a new check.

## After a passing check

The run is `awaiting_patch_approval`. Write `patch show PATCH_ID` to a file and show the author the diff, the
fact-change list (`facts`), and the check (`checks`: summary and each assignment's verdict and remark). Record
`patch decide` only on the author's word; a passing check is not approval.
