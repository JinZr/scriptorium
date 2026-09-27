# External manuscript review evaluation

This repository-only example seals external Scriptorium review runs, builds an anonymous candidate-judging
workspace, and reports each judge's assessments separately. It uses the installed Scriptorium JSON CLI;
it never launches a model, submits a review, decides a finding, or advances a manuscript workflow.
It is separate from the PeerReviewBench Markdown adapter and its recall/precision metrics.

Run these commands from the Scriptorium checkout with its development environment installed. There are no
additional dependencies. Use absolute manuscript and output paths. Keep outputs outside `.scriptorium/`;
each command publishes a new directory and refuses to overwrite an existing one.

## 1. Fix the trial before reviewing

Choose the committed manuscript revision, review profile, client, exact model and supported effort. Prepare
isolated manuscript projects with the same committed configuration. The normal core CLI prepares tasks:

```bash
cd /absolute/manuscript-project
scriptorium --json doctor --revision COMMIT --profile trial
scriptorium --json run start --revision COMMIT --profile trial
```

Return to the Scriptorium checkout and seal a baseline **before any task is claimed**:

```bash
python -m egs.external_review.collect \
  --project /absolute/manuscript-project --run RUN_ID \
  --case paper-01 --trial configuration-a \
  --output /absolute/evaluation/a-before
```

Record the planned host/model/effort, tool versions, allowed corrections and existing paid budget externally
before launching the host. A fresh conversation uses the shared Scriptorium skill to claim, retrieve and submit.
An accepted partial/unknown review requires an explicit continuation; a process exit or valid submission is
not by itself completion. Use `run status` to determine the next action. Stop at human finding decisions.
There is no time limit or automatic retry in this example. Paid host calls require explicit authorization.

Repeat with independent runs for other configurations or papers. Never change a run's frozen artifacts to
manufacture a comparison. Source, PDF and rendered-page equality are separate checks: matching text alone
does not establish identical visual material.

## 2. Collect every outcome

After the host stops, seal another snapshot of the prepared run, including partial, failed or cancelled reviews:

```bash
python -m egs.external_review.collect \
  --project /absolute/manuscript-project --run RUN_ID \
  --case paper-01 --trial configuration-a \
  --baseline /absolute/evaluation/a-before \
  --host-record /absolute/sanitized-host-tool-events.jsonl \
  --output /absolute/evaluation/a-after
```

`--host-record` is optional and repeatable. Supply only sanitized, task-specific records; they are copied
verbatim into the private collection and are not parsed as proof of tool use, image viewing, identity or billing.
Do not include credentials. Existing historical runs can be collected without `--baseline`; they are explicitly
marked as not prospectively locked by this example.

A preparation failure without a frozen bundle cannot enter this collector. Keep that trial in the operator's
preflight ledger as infrastructure-blocked; do not count it as a zero-finding review or silently remove it from
the planned manuscript set.

The collector reconstructs all report sections through bounded `run report --part` calls, executes returned
continuation commands unchanged, and checks offsets, section hashes and the shared report digest. It checks
the digest again after copying, so a concurrently advancing run requires a fresh collection. The full bundle
must match its content-addressed file index. Every available attempt prompt, schema, bundle index, raw output,
trace and validation artifact is copied with its digest checked, including invalid raw submissions. Corruption
or changing input leaves no published collection. Baselines must precede all attempts and match case/trial,
run identity and frozen input bytes exactly.

Each collection contains:

- `collection.json`: manuscript commit/tree, frozen configuration and material identities, baseline binding,
  collection-time code/dependency hashes, and attached host-record paths. Collection-time code is not evidence
  of which tool build an earlier reviewer used.
- `report.json`, `inspection-calls.json`, `artifacts/`, `bundle/`: the complete report, actual collector CLI
  returns, all available attempt artifacts and frozen material. Collector calls are explicitly separate from
  the reviewer's tool events in the report.
- `summary.json`: attempts and their reported client/model/effort/session provenance, retry/continuation
  counts, latest declared scope, successful tool returns and the core coverage audit. Page returns leave
  `image_views_verified` unknown. Unknown costs remain null.
- `seal.json`: exact file inventory and SHA-256 digests, checked by downstream steps.

File seals detect changes; they are not provider signatures. Host/caller-reported identities remain claims.
Use native host records for any stronger assertion about direct model tool use. The example does not parse
arbitrary shell transcripts, infer comprehension from reading intervals, or treat accepted JSON as scientific truth.

## 3. Prepare anonymous candidate judgments

Collect every trial before revealing judge feedback. Include failed and zero-finding trials in the ledger:

```bash
python -m egs.external_review.judge prepare \
  --collection /absolute/evaluation/a-after \
  --collection /absolute/evaluation/b-after \
  --seed 17 --output /absolute/evaluation/judging
```

Only give **`judging/public/`** to a fresh external judge conversation. It contains neutral material IDs,
shuffled candidate IDs, frozen manuscript copies, `prompt.md`, `schema.json` and a file seal. Its input omits
reviewer client/model/effort/session, original severity/confidence, workflow validity and human decisions.
It also omits original review summaries and claim checks, which can disclose trial grouping or identities.
This is a candidate-level assessment, not a new exhaustive review or an audit of the reviewer's entire argument.

`judging/mapping.json` stays private. It maps each candidate to its original finding/attempt/trial and retains
all trial statuses. The same run cannot masquerade as multiple trials. Exact duplicate candidate content on
identical material is combined while preserving every origin; semantic duplicates are left for judges to mark.
The seed and mapping preserve ordering provenance. Free-text style or self-identification may still reveal a
generator; structural blinding cannot guarantee anonymity. The host must restrict the judge's workspace:
instructions do not isolate arbitrary host filesystem tools.

The operator selects the judge and obtains any needed paid authorization. Supply its actual selected model
and effort; never silently substitute one. Judges return one JSON object under `schema.json`, with
`packet_digest` copied from the public seal. Keep their result files outside the sealed packet. Each judge
uses a new session and sees the same packet, without other judgments or human reference labels.

## 4. Keep judgments and disagreement separate

```bash
python -m egs.external_review.judge summarize \
  --packet /absolute/evaluation/judging \
  --judgment /absolute/evaluation/judge-a.json \
  --judgment /absolute/evaluation/judge-b.json \
  --output /absolute/evaluation/results
```

Omit `--judgment` to inspect an unjudged ledger. Result validation binds the entire public packet, requires
exactly one assessment per candidate, and rejects unknown/missing/duplicate IDs, invalid duplicate references,
known reused review sessions and duplicate judge sessions. Declared session provenance still cannot establish
independent context. Invalid judge results publish no aggregate. Accepted judge JSON bytes are preserved.

`results/summary.json` reports each trial's workflow and tool facts, each judge's supported/unsupported/uncertain
assessments and supported consequential counts, and candidate-level disagreements without majority voting.
Input comparisons list differing commits, sources, prompts, schemas, PDF and page hashes within each case.
Matching these does not establish equal host capabilities, effort, cost or independent model-family biases.

There are no gold labels, recall/precision/F1 scores, human decisions or release claims here. Model-supported
concerns remain candidates. Local LaTeX candidates with unresolved provenance, build fidelity, review mapping
or rights remain exploratory; this example does not clear those prerequisites. Synthetic tests and collection
of prior paid-run artifacts are not new paid reviewer or judge runs.
