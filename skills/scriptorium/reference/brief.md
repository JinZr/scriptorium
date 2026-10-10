# Review brief format

`run start --brief brief.json` reads one UTF-8 JSON object of at most 256 KB. The brief is not a manuscript source and
need not be committed.

```json
{
  "venue_family": "ml_conference",
  "venue": "NeurIPS 2026",
  "stage": "presubmission",
  "priority_claims": ["The method improves accuracy on all three benchmarks."],
  "known_weaknesses": ["The ablation uses a single seed."],
  "prior_reviews": "An internal reviewer asked for a stronger baseline.",
  "ignore": ["Checklist answers"],
  "severity_notes": "Checklist items were waived last time.",
  "recorded_by": "claude_code SESSION_ID"
}
```

- Required: `venue_family` (`ml_conference`, `nature_family`, or `other`) and `stage` (`internal_draft`,
  `presubmission`, `rebuttal_revision`, or `camera_ready`).
- `priority_claims`, `known_weaknesses`, and `ignore` default to empty and hold at most 10 non-blank items of up to
  500 characters each.
- `venue` and `recorded_by` take up to 200 characters, `prior_reviews` 4,000, and `severity_notes` 2,000. Omit a text
  field or use `null`; a blank string is rejected.
- Unknown keys are rejected.
- `recorded_by` is this host session's identity as declared. It is stored with the brief but not rendered into
  prompts.

The brief is rendered into every review role's frozen prompt and joins every task's input digest. Reviewers read it
with `task show ATTEMPT_ID --part brief`. A run cannot change its brief; start a new run instead.

`doctor` reports `detected_template`: the first known class or package declared by the main entrypoint or a
supplement, and the venue family it suggests, or `unknown`. It is a default for the brief, not a venue policy.
