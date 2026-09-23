# Test layout

Tests live beside the behavior they own. Keep builders and fakes inside their owning test package; do not add a root-level support module.

- `manuscript/` tests Git snapshots, dependency discovery, bundle containment, navigation, exact edits, and real LaTeX helper inputs.
- `storage/` tests numbered migrations, append-only records, attempts, findings, decisions, and patches. Historical SDK rows remain decodable.
- `external/` tests the shared Codex, Claude Code, and Antigravity task contract with a real small Git and LaTeX project. It covers cross-process claims, bounded retrieval, invalid results, retry, cancellation, human gates, independent verification, and stale patch rejection. These tests require `latexmk` and `pdflatex`; the LaTeX CI job runs them.
- `contracts/` tests packaging, provider SDK absence, output schemas, and the complexity debt ratchet.
- `peerreviewbench/` tests the isolated research adapter and its evaluator without adding benchmark-specific core APIs.

The small retrieval fixture under `manuscript/fixtures/` annotates clean text, a supplementary counterexample, and a cross-section contradiction. It is an offline reachability fixture, not proof that a paid model detected those issues. Credentialed model checks require explicit opt-in and are reported separately from default CI.
