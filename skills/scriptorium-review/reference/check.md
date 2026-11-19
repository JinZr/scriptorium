# Check tasks

A check task checks one candidate patch against the plan the author approved. The lead works it, from a host
session that did no writing in this run.

## Claim

Claim with `--session-source host` and the session ID from the host's own conversation state. The submission, and
`--check`, fail with `check.session_unconfirmed` when the claim is declared or when a writing attempt of this run used
the same client and session. Sessions that only reviewed or checked earlier may check. Never invent or borrow an ID.

## What the export holds

- `sources/`, `source-map.json`, `navigation.json`, and `manuscript.pdf` are the patched manuscript, compiled.
- `task/prompt.md` holds the approved plan, each assignment (instructions, targets, finding IDs, the writer's note,
  and the writing attempt the patch carries), the confirmed findings, the diff, and the fact-change list.
- `task/schema.json` is the output schema; `task/brief.md` is the author's brief, if any. There is no example.

The fact-change list compares the navigation index before and after the patch. It is a reading aid, not evidence:
an empty list does not show that no fact changed, and nothing is gated on it.

## Output

```json
{
  "summary": "Assignment a1 does what the plan asks; a2 drops the benchmark citation the plan keeps.",
  "assignments": [
    {"assignment_id": "a1", "verdict": "accept", "feedback": null},
    {"assignment_id": "a2", "verdict": "redo", "feedback": "Restore \\cite{bench} in the second sentence."}
  ]
}
```

- One entry per assignment of the plan, each once (`check.assignments_mismatch`).
- `redo` needs non-blank feedback the writer can act on: what is wrong, where, and what the text must do instead,
  within the assignment (`check.feedback_required`). `accept` may carry an optional remark.
- The output cites no evidence. Check the patch against the plan, not the paper anew.

Submit with `--file answer.json`, `--check` first. When every assignment is accepted, the patch waits for the
author's approval. Otherwise each `redo` assignment gets a redo writing task and the next candidate gets a new check.
