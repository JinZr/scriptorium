# Test layout

Tests live beside the behavior they own. Keep builders and fakes inside their owning test package; do not add a root-level support module.

- `manuscript/` tests Git snapshots, dependency discovery, bundle containment, navigation, exact edits, and real LaTeX helper inputs.
- `plan/` tests reading and validating a plan directory and turning returned passages into exact edits, without a run.
- `fact_changes/` tests the fact-change list compared from two navigation indexes, without a run.
- `storage/` tests the per-run state files: reopening, uniqueness and reference rules, append-only records, atomic replacement, crash and cross-process safety, and lookups across runs.
- `external/` tests the shared Codex, Claude Code, and Antigravity task contract with a real small Git and LaTeX project. It covers cross-process claims, task exports, invalid results, retry, cancellation, human gates, a real LaTeX error in a writing submission, the check's session rule, and stale patch rejection. These tests require `latexmk` and `pdflatex`; the LaTeX CI job runs them.
- `contracts/` tests packaging, provider SDK absence, output schemas, the complexity debt ratchet, and the skill files' front matter, word budgets, and quick-reference commands.
- `peerreviewbench/` tests the isolated research adapter and its evaluator without adding benchmark-specific core APIs.
- `external_review/` tests sealed CLI report collection, fixed-input provenance, anonymous candidate packaging and external judge validation. Synthetic judgments are contract fixtures, not paid model or scientific-quality evidence.

The small retrieval fixture under `manuscript/fixtures/` annotates clean text, a supplementary counterexample, and a cross-section contradiction. It is an offline reachability fixture, not proof that a paid model detected those issues. Credentialed model checks require explicit opt-in and are reported separately from default CI.

`manuscript/test_review_materials.py` covers declared independent supplements, isolated compilation and frozen
document/page ranges, including a real two-document LaTeX build in the LaTeX CI job.
`workflow/test_review_materials.py` covers cross-process document retrieval, corrupt supplemental artifacts,
failed builds and recovery, and supplementary edits through the check, approval, and application gates.
