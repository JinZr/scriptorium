# Test layout

Tests are grouped by the Scriptorium behavior they own. Add new tests to the
nearest existing domain and create a new test module only for a distinct
behavior cluster.

Keep fake SDKs, repository builders, and data factories inside their owning
test package. Do not import test support across domains or add a root-level
support module. Live, credentialed harness checks belong under `live/`;
packaging and dependency-boundary checks belong under `contracts/`.
