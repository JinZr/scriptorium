from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys

from egs.peerreviewbench.prepare import file_digest
from egs.peerreviewbench.run import PeerReviewBenchManuscriptManager, _run_paper, _validate_completed_paper
from scriptorium.service import ScriptoriumService


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


def test_benchmark_prepares_external_tasks_and_collects_validated_reviews(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared" / "paper1"
    preprint = prepared / "preprint"
    preprint.mkdir(parents=True)
    paper = preprint / "preprint.md"
    paper.write_text("# A paper\n\nThe result needs review.\n", encoding="utf-8")
    (prepared / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "test/dataset",
                "dataset_revision": "a" * 40,
                "paper_id": 1,
                "paper_title": "A paper",
                "files": [
                    {
                        "path": "preprint.md",
                        "content_hash": file_digest(paper),
                        "size_bytes": paper.stat().st_size,
                        "is_text": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "runs" / "one"
    run_dir.mkdir(parents=True)
    entry = {"paper_id": 1, "project_dir": "papers/paper1", "status": "pending"}

    entry = asyncio.run(_run_paper(entry, run_dir, prepared))
    assert entry["status"] == "awaiting_review"
    project = run_dir / entry["project_dir"]
    with ScriptoriumService(project, manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared)) as service:
        tasks = service.list_tasks(entry["scriptorium_run_id"])["tasks"]
        assert len(tasks) == 4
        assert all(item["task"].status.value == "pending" for item in tasks)
        for item in tasks:
            claim = service.claim_task(item["task"].id, "codex", "selected-model", "max", "session-1", "host")
            asyncio.run(
                service.submit_task(
                    claim["attempt"].id,
                    claim["input_digest"],
                    json.dumps(
                        {
                            "summary": "Reviewed the frozen paper.",
                            "findings": [],
                            "scope": {"completion": "unknown", "checked": [], "outstanding": [], "limitations": []},
                        }
                    ),
                )
            )

    entry = asyncio.run(_run_paper(entry, run_dir, prepared))
    assert entry["status"] == "complete"
    assert entry["scriptorium"]["finding_count"] == 0
    assert entry["scriptorium"]["estimated_cost_usd"] is None
    _validate_completed_paper(entry, run_dir, prepared)
