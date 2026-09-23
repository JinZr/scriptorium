from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _module():
    root = Path(__file__).resolve().parents[2] / "egs" / "peerreviewbench"
    sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location("peerreviewbench_external_run", root / "run.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(root))


def test_generated_paper_project_uses_external_tasks_without_routes(tmp_path: Path) -> None:
    benchmark = _module()
    project = tmp_path / "paper"
    benchmark.create_paper_project(project)
    benchmark.validate_paper_project(project)
    assert not (project / ".scriptorium" / "config.toml").exists()
    assert "openai-codex" not in benchmark.package_versions()
    assert "claude-agent-sdk" not in benchmark.package_versions()
    assert "google-antigravity" not in benchmark.package_versions()
