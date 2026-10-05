import asyncio
from hashlib import sha256
import json

import pytest

from egs.external_review import collect as collector
from egs.external_review.collect import collect
from egs.external_review.files import file_records, read_json, seal, verify_seal
from scriptorium.service import ScriptoriumService

from ._support import project, start, submit


def test_collect_preserves_prepared_inputs_failures_continuations_and_unknowns(tmp_path):
    root = project(tmp_path)
    run_id, task_id = start(root)
    baseline = collect(root, run_id, tmp_path / "before", "paper-a", "trial-a", host_computation="denied")
    with ScriptoriumService(root) as service:
        context = service.claim_task(task_id, "codex", "model-a", "low", "review-a", "host")
        rejected = asyncio.run(service.submit_task(context["attempt"].id, context["input_digest"], "{broken"))
        assert rejected["attempt"].status.value == "failed"
        asyncio.run(service.retry_task(run_id, task_id))
        submit(service, task_id, completion="partial")
        asyncio.run(service.continue_review(run_id, task_id))
        submit(service, task_id)
        before = service.render_report(run_id, "json")
    artifacts = file_records(root / ".scriptorium/artifacts")
    host = tmp_path / "host.json"
    host.write_text('{"native_tool_event": "independent record"}')
    output = collect(root, run_id, tmp_path / "after", "paper-a", "trial-a", baseline, [host], "denied")
    verify_seal(output)
    summary = read_json(output / "summary.json")
    assert summary["attempt_status_counts"] == {"failed": 1, "completed": 2}
    assert summary["continuations"] == summary["retries"] == 1
    assert summary["required_reviews_completed"]
    assert summary["accepted_findings"] == 1
    assert summary["cost_usd"] is None
    assert summary["image_views_verified"] is None
    assert summary["scientific_correctness"] == "not_evaluated"
    assert summary["attempts"][0]["thread_id"] == "review-a"
    metadata = read_json(output / "collection.json")
    assert metadata["baseline_digest"] == verify_seal(baseline)
    assert metadata["prepared_before_review"]
    assert metadata["host_conditions"] == {"computation": "denied"}
    assert (output / "host-records/001.bin").read_bytes() == host.read_bytes()
    assert (output / "artifacts" / rejected["attempt"].output_artifact_digest).read_text() == "{broken"
    for call in read_json(output / "inspection-calls.json"):
        assert call["arguments"][1:3] in (["run", "status"], ["run", "report"])
        assert len(call["stdout"].encode()) <= 7000
    with ScriptoriumService(root) as service:
        assert service.render_report(run_id, "json") == before
    assert file_records(root / ".scriptorium/artifacts") == artifacts


def test_collector_keeps_partial_and_failed_trials_incomplete(tmp_path):
    root = project(tmp_path)
    run_id, task_id = start(root)
    with ScriptoriumService(root) as service:
        submit(service, task_id, completion="partial")
    output = collect(root, run_id, tmp_path / "partial", "case", "trial")
    summary = read_json(output / "summary.json")
    assert not summary["required_reviews_completed"]
    assert summary["run_status"] == "reviewing"
    assert summary["tasks"][0]["scope"]["completion"] == "partial"
    assert summary["accepted_findings"] == 1
    assert not read_json(output / "collection.json")["prepared_before_review"]


@pytest.mark.parametrize("damage", ["source", "output", "index"])
def test_collector_rejects_corruption_without_publishing_partial_package(tmp_path, damage):
    root = project(tmp_path)
    run_id, task_id = start(root)
    with ScriptoriumService(root) as service:
        context = submit(service, task_id)
        attempt = service.database.list_attempts(task_id)[-1]
        targets = {
            "source": root / ".scriptorium/runs" / run_id / "bundle/sources/main.tex",
            "output": service.artifacts.path_for(attempt.output_artifact_digest),
            "index": service.artifacts.path_for(context["bundle_digest"]),
        }
        targets[damage].write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        collect(root, run_id, tmp_path / "invalid", "case", "trial")
    assert not (tmp_path / "invalid").exists()


def test_collector_rejects_changed_baseline_and_existing_output(tmp_path):
    root = project(tmp_path)
    run_id, _ = start(root)
    baseline = collect(root, run_id, tmp_path / "before", "case", "trial")
    with pytest.raises(ValueError, match="already exists"):
        collect(root, run_id, baseline, "case", "trial")
    with pytest.raises(ValueError, match="outside sealed input"):
        collect(root, run_id, baseline / "nested", "case", "trial", baseline)
    verify_seal(baseline)
    with pytest.raises(ValueError, match="identity changed"):
        collect(root, run_id, tmp_path / "different", "case", "other", baseline)
    with pytest.raises(ValueError, match="same planned host conditions"):
        collect(root, run_id, tmp_path / "computed", "case", "trial", baseline, host_computation="allowed")
    with pytest.raises(ValueError, match="Host computation must be one of"):
        collect(root, run_id, tmp_path / "invalid", "case", "trial", host_computation="sometimes")
    (baseline / "report.json").write_text("{}")
    with pytest.raises(ValueError, match="Collection has changed"):
        collect(root, run_id, tmp_path / "damaged", "case", "trial", baseline)


def test_collection_seal_detects_raw_artifact_and_file_set_changes(tmp_path):
    root = tmp_path / "sealed"
    root.mkdir()
    raw = b"not JSON: invalid submissions must remain raw"
    (root / sha256(raw).hexdigest()).write_bytes(raw)
    seal(root)
    verify_seal(root)
    (root / "extra.json").write_text(json.dumps({"unexpected": True}))
    with pytest.raises(ValueError, match="Collection has changed"):
        verify_seal(root)


@pytest.mark.parametrize("reseal", [False, True])
def test_baseline_cannot_hide_prior_attempts_during_collection(tmp_path, monkeypatch, reseal):
    root = project(tmp_path)
    run_id, task_id = start(root)
    with ScriptoriumService(root) as service:
        submit(service, task_id)
    baseline = collect(root, run_id, tmp_path / "baseline", "case", "trial")
    original = collector.read_json

    def changing_read(path):
        value = original(path)
        if path == baseline / "report.json":
            for item in value["tasks"]:
                item["attempts"] = []
            path.write_text(json.dumps(value))
            if reseal:
                (baseline / "seal.json").unlink()
                seal(baseline)
        return value

    monkeypatch.setattr(collector, "read_json", changing_read)
    with pytest.raises(ValueError, match="Collection has changed"):
        collect(root, run_id, tmp_path / "after", "case", "trial", baseline)
    assert not (tmp_path / "after").exists()


@pytest.mark.parametrize("shadow", ["scriptorium.py", "scriptorium/__init__.py", "pydantic.py"])
def test_manuscript_cannot_shadow_the_collector_cli_or_dependencies(tmp_path, monkeypatch, shadow):
    root = project(tmp_path)
    run_id, _ = start(root)
    module = root / shadow
    module.parent.mkdir(exist_ok=True)
    module.write_text(
        "from pathlib import Path\n"
        "Path('shadow-imported').write_text('untrusted code ran')\n"
        "raise RuntimeError('project module shadowed CLI')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(root))
    output = collect(root, run_id, tmp_path / "collection", "case", "trial")
    assert read_json(output / "report.json")["run"]["id"] == run_id
    assert not (root / "shadow-imported").exists()


def test_run_change_during_collection_rejects_mixed_report(tmp_path, monkeypatch):
    root = project(tmp_path)
    run_id, task_id = start(root)
    original_cli = collector.cli
    changed = False

    def advancing_cli(project_path, arguments, records):
        nonlocal changed
        result = original_cli(project_path, arguments, records)
        if "--part" in arguments and not changed:
            with ScriptoriumService(root) as service:
                service.claim_task(task_id, "codex", "model", "low", "session", "host")
            changed = True
        return result

    monkeypatch.setattr(collector, "cli", advancing_cli)
    with pytest.raises(ValueError, match="report changed"):
        collect(root, run_id, tmp_path / "mixed", "paper", "trial")
    assert not (tmp_path / "mixed").exists()
