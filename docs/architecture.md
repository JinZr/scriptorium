# Architecture

Scriptorium is a single-user modular monolith: one Python package behind one CLI, with no server, queue, or model
runtime. This page describes which module owns what, how state is stored, what a run freezes, the evidence
contract, and the gates. Commands and output rules are in [operations](operations.md).

## Module ownership

| Module | Owns |
| --- | --- |
| `cli.py` | Argument parsing and text or JSON presentation; no business rules |
| `service.py` | Operations shared by entrypoints, the per-run operation lock, exports, page rendering, reports, the release gate |
| `workflow.py` | Task preparation, output validation, replay and recovery, stage advancement, approval checks |
| `storage.py` | `RunStore`: the per-run state files, their locking and atomic replacement, and the rules a relational schema would enforce |
| `artifacts.py` | Atomic, SHA-256-addressed artifact publication |
| `manuscript.py` | Git snapshots, LaTeX dependency scanning and builds, bundles, navigation, source anchors, exact edits, patch application |
| `plan.py` | Reading and validating the plan directory; turning a writer's returned passages into exact edits |
| `fact_changes.py` | The fact-change list: comparing two navigation indexes and two document indexes |
| `domain.py` | Entities, enums, invariants, and state transitions |
| `schemas.py`, `prompts/` | Output schemas, the evidence contract, the severity rubric, the brief model, and role prompts |
| `config.py` | `scriptorium.toml` loading and `init` |
| `finding_groups.py`, `decision_stats.py`, `tool_output.py`, `examples.py` | Report grouping, decision counts, compact CLI views, task example outputs |

## State layout

`.scriptorium/` sits in the manuscript repository, ignored by Git, and holds everything Scriptorium writes:

```text
.scriptorium/
  runs/RUN_ID/
    state.json            run, tasks, attempts, findings, decisions, plans, patches, checks, events
    state.lock            the store's per-run lock
    snapshot/             the frozen commit's files
    bundle/               the frozen review bundle a task reads from
    build/                disposable LaTeX builds, including each writing submission's compile check
    manifest.json         the frozen inputs and the bundle digest
    patched/PATCH_ID/     the snapshot with a patch applied
    patches/PATCH_ID.json the patch's edits and changed paths
    checks/PATCH_ID/bundle/          the patched bundle a check reads
    page-views/           zoomed page views and text layers, named by digest
  artifacts/sha256/AB/CDEF...        immutable content, with CDEF....json metadata beside it
  locks/RUN_ID.lock       the service's per-run operation lock
```

A run exists exactly when its `state.json` exists. Decisions and events are append-only within it, and so are
approved plans: the latest is the current one, and a patch records the plan it was built from. The artifact store
keeps prompts, schemas, briefs, approved plan and assignment files, bundle indexes, submitted outputs, validation
reports, examples, diffs, fact-change lists, and build evidence under their SHA-256 digest.

## Atomicity and locking

- Every change to a run takes `state.lock`, loads `state.json`, changes it in memory, and replaces the file with a
  complete, fsynced copy. A reader or a crash sees the old state or the new one, never a mix. A command that
  changes several records, such as a multi-ID `finding decide`, does so in one replacement.
- Artifacts are written to a temporary file, fsynced, and renamed into place under their digest; publishing the
  same content again verifies the existing file, so published content never changes.
- `service.py` serializes each run's operations under `locks/RUN_ID.lock`, which records the holder's operation,
  process, host, and time. Claims, submissions, decisions, retries, resumes, `run status`, and
  `run report --part` take it without waiting; `run cancel`, `task export`, `task page`, and `task submit --check`
  wait for it. Concurrent `run start` calls on one commit wait on a per-commit lock, so the duplicate check sees
  the earlier run.
- The store's lock is always taken inside the service lock, never the other way round, so writers that skip the
  service lock still serialize.
- Export and page rendering hold the run lock through their event, so a cancellation or submission cannot change
  the attempt between the checks and the written files.
- Other reads (`run list`, `run gate`, `run report` without `--part`, `task list`, `task show`, and the `finding`
  and `patch` reads) take no lock; they read complete state files. Report reads write no events or artifacts.

## Freezing a run

`run start` resolves the revision and copies the commit into a disposable snapshot; uncommitted files never enter
it. From the snapshot it reads `scriptorium.toml`, scans each entrypoint's LaTeX dependency closure, builds the
navigation index, compiles, and creates the bundle: the sources at `sources/...`, `manifest.json`,
`source-map.json`, `navigation.json`, `manuscript.pdf`, and one PNG per page.

With supplements, each entrypoint's closure is scanned separately, so its graphics and bibliography context is
kept, and the bundle freezes their union. Each document compiles in its own disposable copy, so generated files
cannot leak between documents. The PDFs are concatenated in configured order into `manuscript.pdf`, and the
manifest and source map freeze a document index (entrypoint, first global page, page count) whose ranges partition
the PDF and name distinct text sources. A writing submission's compile check and the combined candidate rebuild
every document, and the check's patched bundle comes from the candidate's build; any failed build blocks that
stage.

The run freezes:

- the project configuration and `scriptorium.toml` text;
- the source identities (path, digest, line count) and the navigation index;
- one prompt template per role: for review roles the role prompt, the shared export-reading instructions, the
  evidence contract, the limitations instruction, and the severity rubric, added once by the workflow (the role
  prompts do not repeat it); for the writing role the writer prompt and its passage rules, with slots for the plan,
  the assignment, its targets, its findings, and, for a redo task, what the check returned; for the check role the
  check prompt, with slots for the plan, the assignments with the writers' notes and attempts, the confirmed
  findings, the diff, and the fact-change list. Slots are filled when a task is prepared;
- the output schemas, which repeat the severity levels on `severity`;
- the evidence contract;
- the review brief, when one is given: validated against a strict model, published as an artifact, recorded by
  digest on the run, and rendered deterministically before the severity rubric in every review prompt template.
  Writing and check prompts do not include it.

A run keeps the prompts it froze; a changed prompt applies only to new runs.

Each task records its prompt, schema, and bundle digests and a content-addressed index of its bundle files. Its
input digest is the digest of those three digests and, for a run with a brief, the brief's artifact digest, so
brief fields that are not rendered are bound too. Every attempt binds its own prompt; a correction prompt after a
rejected submission is a new prompt with a new input digest. A review task also freezes an example output, built
from its schema and the first non-blank line of a text source (an entrypoint first) whose bundle and snapshot
copies both match its digest, and kept only if it passes the task's output model and anchor checks. Its digest is
stored outside the input digest. Writing and check tasks have no example. A writing task records the digests of
the plan text and the assignment file it was prepared from and the assignment ID, not a plan record, and a redo task
also the check and the returned attempt; its prompt holds exactly that content, so a later plan with the same plan
text and assignment file finds the same tasks and keeps the assignment's current output
([reuse rule](operations.md#the-plan-directory)). A writing task is current while the latest plan is current and
holds that content; a check task is current while it matches the latest patch, still `proposed`, of that plan. A cancelled task does not count toward the one-task-per-input rule, so an
identical task can be prepared again after a waiver cancelled it.

Export and page rendering check every file they use against the bundle index and the document mapping;
submission validates the whole bundle. Accepted outputs are replayed into findings, a candidate patch, or a check
record; a writing output is validated again on replay but compiled only when it is submitted. Each assignment's
current output is derived on replay from stored state alone: a check records, per assignment, the writing attempt
it accepted or returned.

## Evidence contract

- A text anchor names a bare source path, the source digest, an inclusive 1-based line range, and a verbatim quote
  from that range.
- A line ends only at `\n`, `\r\n`, or `\r`; form feeds and Unicode line separators stay inside their line. The
  contract freezes this as `line_terminators`, and the same rule counts lines in the source map, validates
  anchors, applies exact edits, and writes patch diffs.
- The source map's `line_count` bounds anchors; the manifest's per-source `lines` is informational.
- A PDF anchor names `manuscript.pdf` and a 1-based global page. A page image path is only a read location; the
  frozen page digest binds what the page shows.
- Global pages keep separately compiled documents from being confused. The document index is bundle metadata; it
  does not change the output schema or the run state.
- Page views and text layers are rendered from the digest-checked PDF and stored outside the bundle; they are
  never evidence.

## Gates

Finding decisions, plan approval, writing, the check, patch approval, patch application, and the release gate are
separate stages, each with its own command ([plan](operations.md#the-plan-directory),
[procedure](operations.md#patches-the-check-and-application)). Scriptorium never infers approval from severity or
from a passing check.

- On replay, a stored output is read from the artifact store and validated again before its records are created.
  Missing or corrupt frozen material is an infrastructure failure: it fails the operation visibly and never counts
  as success.

`run gate` passes only when every condition holds:

| Condition | Holds when |
| --- | --- |
| `required_reviews_completed` | Every profile role has an accepted review |
| `review_artifacts_valid` | Every accepted review output is readable and valid |
| `all_findings_decided` | No finding is pending |
| `no_unhandled_blocker_or_major` | No pending finding is `blocker` or `major` |
| `confirmed_findings_covered` | The applied patch covers every confirmed finding |
| `patched_build_succeeded` | The applied patch built, when a patch is required |
| `check_passed` | The applied patch has a passing check, when a patch is required |
| `patch_applied_when_required` | A patch is applied whenever a finding is confirmed |
| `run_completed` | The run is `completed` |
