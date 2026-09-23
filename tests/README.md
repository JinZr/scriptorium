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
