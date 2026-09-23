# Test layout

Tests are grouped by the Scriptorium behavior they own. Add new tests to the
nearest existing domain and create a new test module only for a distinct
behavior cluster.

Keep fake SDKs, repository builders, and data factories inside their owning
test package. Do not import test support across domains or add a root-level
support module. Live, credentialed harness checks belong under `live/`;
packaging and dependency-boundary checks belong under `contracts/`.

`manuscript/test_dependencies_compile.py` compares supported dependency syntax
with real `pdflatex -recorder` inputs. Local runs skip these tests when `pdflatex`
is unavailable; the `latex-dependencies` CI job installs the engine and verifies
it is present before running them. These tests require no provider credentials.

`manuscript/test_compiler_inputs.py` exercises fresh recorder validation and
compiler input coverage, including real `latexmk` builds. The LaTeX CI job also
installs and verifies `latexmk`; workflow and doctor tests cover the shared
coverage gate without provider calls.

`manuscript/test_build_helpers.py` covers pre-build link containment and native
BibTeX/Biber/EPS helper evidence. The LaTeX CI job installs bibliography tools
and EPS conversion dependencies and runs these cases on Python 3.10.

`runtime/codex/test_preflight.py` includes a real, uncredentialed startup/config
probe of the pinned bundled binary. It does not invoke a model and runs in the
ordinary test suite. The same package owns process cleanup, output redaction,
and synthetic native protocol fixtures. Paid retrieval tests remain under `live/`.

`live/test_retrieval_contract.py` runs offline: it checks randomized retrieval
fixtures, native access evidence, and harness reporting without credentials.
`live/test_native_harnesses.py` owns the opt-in, paid Codex, Claude Code, and
Antigravity retrieval/resume checks. Default skips are not capability passes;
see `docs/operations.md` for opt-in switches and evidence retention.
