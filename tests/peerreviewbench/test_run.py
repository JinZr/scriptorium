import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil

import fitz
import pytest

from egs.peerreviewbench.prepare import BenchmarkError, file_digest, load_lock
import egs.peerreviewbench.run as benchmark_run
from egs.peerreviewbench.run import PeerReviewBenchManuscriptManager, create_paper_project, run_benchmark
from scriptorium.config import load_project_config
from scriptorium.domain import AgentRole, RunStatus, TaskStatus, canonical_json
from scriptorium.errors import InfrastructureError
from scriptorium.schemas import DEFAULT_EVIDENCE_ANCHOR_CONTRACT
from scriptorium.service import ScriptoriumService

ROLES = {
    AgentRole.SUBSTANTIVE_REVIEW,
    AgentRole.COPYEDIT,
    AgentRole.CONSISTENCY,
    AgentRole.FIGURE_REVIEW,
}
MARKDOWN = (
    "# A benchmark paper\n" "\n" "The reported result needs review.\n" "\n" "![Result figure](figures/result.png)\n"
)


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    try:
        page = document.new_page(width=100, height=80)
        page.insert_text((10, 30), "result")
        page.get_pixmap(alpha=False).save(path)
    finally:
        document.close()


def _prepared_paper(root: Path, *, paper_id: int, dataset_id: str, dataset_revision: str) -> Path:
    paper = root / f"paper{paper_id}"
    preprint = paper / "preprint"
    (preprint / "supplement").mkdir(parents=True)
    (preprint / "code").mkdir()
    (preprint / "preprint.md").write_text(MARKDOWN, encoding="utf-8")
    (preprint / "supplement" / "notes.md").write_text("Supporting note.\n", encoding="utf-8")
    (preprint / "code" / "check.py").write_text("print('checked')\n", encoding="utf-8")
    _write_png(preprint / "figures" / "result.png")

    files = []
    for path in sorted(preprint.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(preprint).as_posix()
        files.append(
            {
                "path": relative,
                "content_hash": file_digest(path),
                "size_bytes": path.stat().st_size,
                "is_text": path.suffix.lower() != ".png",
            }
        )
    (paper / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "dataset_revision": dataset_revision,
                "paper_id": paper_id,
                "paper_title": "A benchmark paper",
                "files": files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return paper


def _project(tmp_path: Path, prepared: Path) -> tuple[Path, PeerReviewBenchManuscriptManager]:
    project = tmp_path / "project"
    create_paper_project(project)
    (project / "AGENTS.md").write_text("This project file must not enter the benchmark bundle.\n", encoding="utf-8")
    return project, PeerReviewBenchManuscriptManager(project, prepared)


def _complete_reviews(service: ScriptoriumService, run_id: str, *, skip: AgentRole | None = None) -> None:
    for item in service.list_tasks(run_id)["tasks"]:
        task = item["task"]
        if task.role == skip or task.status == TaskStatus.COMPLETED:
            continue
        claim = service.claim_task(task.id, "codex", "test-model", "max", f"session-{task.role.value}", "host")
        source = next(
            record for record in claim["source_map"]["sources"] if record["source_path"] == "preprint/preprint.md"
        )
        output = {
            "summary": f"Reviewed the frozen {task.role.value} material.",
            "scope": {"completion": "complete", "checked": [], "outstanding": [], "limitations": []},
            "findings": [
                {
                    "category": task.role.value,
                    "severity": "moderate",
                    "title": f"{task.role.value} finding",
                    "claim": f"The {task.role.value} reviewer found an issue.",
                    "evidence": [
                        {
                            "source_path": "preprint/preprint.md",
                            "start_line": 3,
                            "end_line": 3,
                            "source_digest": source["source_digest"],
                            "quoted_text": "The reported result needs review.",
                        }
                    ],
                    "explanation": "The finding is grounded in the frozen Markdown source.",
                    "suggested_action": "Inspect and clarify the reported result.",
                    "confidence": 0.9,
                }
            ],
        }
        receipt = asyncio.run(service.submit_task(claim["attempt"].id, claim["input_digest"], json.dumps(output)))
        assert receipt["attempt"].status.value == "completed"


def test_markdown_manager_builds_standard_bundle_without_project_sentinel(tmp_path: Path) -> None:
    prepared = _prepared_paper(
        tmp_path / "prepared",
        paper_id=7,
        dataset_id="test/peerreview-bench",
        dataset_revision="locked-revision",
    )
    project, manager = _project(tmp_path, prepared)
    revision = manager.resolve_revision("ignored")
    assert manager.resolve_revision("also-ignored") == revision

    snapshot = tmp_path / "snapshot"
    manager.create_snapshot(revision, snapshot)
    sources = manager.scan_sources(snapshot, "benchmark.tex")

    assert not (snapshot / "AGENTS.md").exists()
    assert {source.path for source in sources} == {
        "preprint/code/check.py",
        "preprint/figures/result.png",
        "preprint/preprint.md",
        "preprint/supplement/notes.md",
    }
    assert "benchmark.tex" not in {source.path for source in sources}
    assert next(source for source in sources if source.path == "preprint/preprint.md").lines == 5

    workspace = tmp_path / "build"
    shutil.copytree(snapshot, workspace)
    build = manager.build(workspace, load_project_config(snapshot).manuscript)
    with fitz.open(build.pdf_path) as document:
        assert document.page_count == 2
    assert "appended 1 referenced figure(s)" in build.log
    repeat_workspace = tmp_path / "repeat-build"
    shutil.copytree(snapshot, repeat_workspace)
    repeated = manager.build(repeat_workspace, load_project_config(snapshot).manuscript)
    assert file_digest(repeated.pdf_path) == file_digest(build.pdf_path)

    bundle = manager.create_bundle(
        snapshot,
        tmp_path / "bundle",
        revision,
        sources,
        build.pdf_path,
        DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    )

    assert bundle.pdf_pages == 2
    assert (bundle.workspace / "manuscript.pdf").is_file()
    assert len(list((bundle.workspace / "pages").glob("page-*.png"))) == 2
    assert not (bundle.workspace / "sources" / "benchmark.tex").exists()
    assert not (bundle.workspace / "AGENTS.md").exists()


def test_image_discovery_ignores_only_locked_conversion_placeholders(tmp_path: Path) -> None:
    preprint = tmp_path / "preprint"
    preprint.mkdir()
    _write_png(preprint / "images" / "figure1.png")
    markdown = "![crop](page_1012_172_388_388.png)\n"

    images, skipped = benchmark_run._referenced_images(preprint, markdown)

    assert images == ((preprint / "images" / "figure1.png").resolve(),)
    assert skipped == 1

    with pytest.raises(benchmark_run.InfrastructureError, match="Referenced image is missing"):
        benchmark_run._referenced_images(preprint, markdown + "![missing](missing.png)\n")
    with pytest.raises(benchmark_run.InfrastructureError, match="Referenced image is missing"):
        benchmark_run._referenced_images(
            preprint,
            "![nested](figures/page_1012_172_388_388.png)\n",
        )
    with pytest.raises(benchmark_run.InfrastructureError, match="Referenced image is missing"):
        benchmark_run._referenced_images(preprint, "![near](page_1012_172_388.png)\n")

    existing_placeholder = preprint / "page_1012_172_388_388.png"
    _write_png(existing_placeholder)
    images, skipped = benchmark_run._referenced_images(preprint, markdown)
    assert existing_placeholder.resolve() in images
    assert skipped == 0


def test_image_list_entries_remain_authoritative(tmp_path: Path) -> None:
    preprint = tmp_path / "preprint"
    supplement = preprint / "supplement"
    supplement.mkdir(parents=True)
    image = supplement / "figure.png"
    _write_png(image)
    image_list = supplement / "images_list.json"
    image_list.write_text(json.dumps([{"img_path": "figure.png"}]), encoding="utf-8")

    images, skipped = benchmark_run._referenced_images(preprint, "")

    assert images == (image.resolve(),)
    assert skipped == 0

    image.unlink()
    with pytest.raises(benchmark_run.InfrastructureError, match="Referenced image is missing"):
        benchmark_run._referenced_images(preprint, "")


def test_markdown_manager_rejects_symlinked_prepared_paper(tmp_path: Path) -> None:
    prepared = _prepared_paper(
        tmp_path / "prepared",
        paper_id=7,
        dataset_id="test/peerreview-bench",
        dataset_revision="locked-revision",
    )
    linked = tmp_path / "linked-paper"
    linked.symlink_to(prepared, target_is_directory=True)

    with pytest.raises(BenchmarkError, match="unsafe symlink"):
        PeerReviewBenchManuscriptManager(tmp_path, linked)


def test_markdown_manager_revalidates_completed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared_paper(
        tmp_path / "prepared",
        paper_id=7,
        dataset_id="test/peerreview-bench",
        dataset_revision="locked-revision",
    )
    _, manager = _project(tmp_path, prepared)
    revision = manager.resolve_revision("ignored")
    original_copytree = shutil.copytree

    def mutate_then_copy(source: Path, destination: Path, *args, **kwargs):
        if Path(source) == prepared / "preprint":
            (Path(source) / "preprint.md").write_text("changed during snapshot creation\n", encoding="utf-8")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(benchmark_run.shutil, "copytree", mutate_then_copy)

    with pytest.raises(BenchmarkError, match="Prepared file failed verification"):
        manager.create_snapshot(revision, tmp_path / "snapshot")


def test_generated_project_rejects_symlinked_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    create_paper_project(project)
    state = project / ".scriptorium"
    moved = tmp_path / "external-state"
    state.rename(moved)
    state.symlink_to(moved, target_is_directory=True)

    with pytest.raises(BenchmarkError, match="missing or unsafe"):
        benchmark_run.validate_paper_project(project)


def test_package_versions_include_pydantic() -> None:
    assert "pydantic" in benchmark_run.package_versions()


def test_benchmark_rejects_scriptorium_imported_from_another_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_package = tmp_path / "other-checkout" / "src" / "scriptorium" / "__init__.py"
    installed_package.parent.mkdir(parents=True)
    installed_package.write_text("", encoding="utf-8")
    monkeypatch.setattr(benchmark_run.scriptorium, "__file__", str(installed_package))
    runs_root = tmp_path / "runs"

    with pytest.raises(BenchmarkError, match="Imported Scriptorium package does not match this repository"):
        asyncio.run(run_benchmark(runs_root=runs_root))

    assert not runs_root.exists()


def test_full_profile_service_persists_findings_artifacts_and_cost(tmp_path: Path) -> None:
    prepared = _prepared_paper(
        tmp_path / "prepared",
        paper_id=8,
        dataset_id="test/peerreview-bench",
        dataset_revision="locked-revision",
    )
    project, manager = _project(tmp_path, prepared)
    with ScriptoriumService(project, manuscript_manager=manager) as service:
        view = asyncio.run(service.start_run("prepared", "full"))
        run = view["run"]
        assert run.status == RunStatus.REVIEWING
        _complete_reviews(service, run.id)

        assert service.get_run(run.id)["run"].status == RunStatus.AWAITING_DECISION
        assert benchmark_run.summarize_scriptorium_run(service, run.id)["estimated_cost_usd"] is None

        review_tasks = service.list_tasks(run.id)["tasks"]
        assert {item["task"].role for item in review_tasks} == ROLES
        assert {item["task"].status for item in review_tasks} == {TaskStatus.COMPLETED}
        attempts = [attempt for item in review_tasks for attempt in item["attempts"]]
        assert len(attempts) == 4
        assert all(attempt.external_client == "codex" for attempt in attempts)
        for attempt in attempts:
            assert service.database.get_artifact(attempt.output_artifact_digest).media_type == "application/json"
            assert attempt.trace_artifact_digest is None

        findings = service.list_findings(run.id)
        assert len(findings) == 4
        assert {finding.role for finding in findings} == ROLES
        assert {finding.task_id for finding in findings} == {item["task"].id for item in review_tasks}
        valid_entry = {
            "paper_id": 8,
            "scriptorium_run_id": run.id,
            "scriptorium": benchmark_run.summarize_scriptorium_run(service, run.id),
        }
        assert benchmark_run.validate_completed_scriptorium_state(service, valid_entry) is None

        frozen_entry = {
            "paper_id": valid_entry["paper_id"],
            "scriptorium_run_id": valid_entry["scriptorium_run_id"],
            "scriptorium": dict(valid_entry["scriptorium"]),
        }
        frozen_entry["scriptorium"]["finding_payload_digest"] = "0" * 64
        with pytest.raises(BenchmarkError, match="state changed after completion"):
            benchmark_run.validate_completed_scriptorium_state(service, frozen_entry)

        bundle_manifest = json.loads(
            (project / ".scriptorium" / "runs" / run.id / "bundle" / "manifest.json").read_text(encoding="utf-8")
        )
        for source in bundle_manifest["sources"]:
            assert service.database.get_artifact(source["digest"]).digest == source["digest"]
        for page in bundle_manifest["pages"]:
            assert service.database.get_artifact(page["digest"]).digest == page["digest"]

        build_log = service.database.connection.execute(
            "SELECT digest FROM artifacts WHERE media_type = ? ORDER BY digest",
            ("text/plain; charset=utf-8",),
        ).fetchone()
        assert build_log is not None
        build_log_path = service.artifacts.path_for(str(build_log["digest"]))
        build_log_bytes = build_log_path.read_bytes()
        build_log_path.write_bytes(b"corrupt build evidence")
        with pytest.raises(BenchmarkError, match="artifact failed verification"):
            benchmark_run.validate_completed_scriptorium_state(service, valid_entry)
        build_log_path.write_bytes(build_log_bytes)

        bundle_source = project / ".scriptorium" / "runs" / run.id / "bundle" / "sources" / "preprint" / "preprint.md"
        bundle_source.unlink()
        with pytest.raises(BenchmarkError, match="bundle"):
            benchmark_run.validate_completed_scriptorium_state(service, valid_entry)


def test_completed_bundle_validation_allows_explicit_legacy_source_map_read_only(tmp_path: Path) -> None:
    prepared = _prepared_paper(
        tmp_path / "prepared",
        paper_id=18,
        dataset_id="test/peerreview-bench",
        dataset_revision="locked-revision",
    )
    project, manager = _project(tmp_path, prepared)
    with ScriptoriumService(project, manuscript_manager=manager) as service:
        run = asyncio.run(service.start_run("prepared", "full"))["run"]
        _complete_reviews(service, run.id)
        run_dir = service.armarius._run_dir(run.id)
        bundle_dir = run_dir / "bundle"
        bundle_manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        source_map_path = bundle_dir / "source-map.json"
        source_map_path.write_text(
            json.dumps({"sources": bundle_manifest["sources"]}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run_manifest_path = run_dir / "manifest.json"
        run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        run_manifest["bundle_digest"] = service.armarius._directory_digest(bundle_dir)
        run_manifest_path.write_text(
            json.dumps(run_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        service.armarius._record_file(source_map_path, "application/json")
        service.armarius._record_text(canonical_json(run_manifest), "application/json")
        legacy_config = dict(run.frozen_config)
        legacy_config.pop("evidence_anchor_contract")
        legacy_run = replace(run, frozen_config=legacy_config)

        benchmark_run._validate_completed_bundle(service, legacy_run, 18)
        with pytest.raises(InfrastructureError, match="predates the frozen evidence anchor contract"):
            service.armarius._bundle_for_run(legacy_run)
        assert len(service.database.list_tasks(run.id)) == 4


def test_completed_benchmark_paper_is_not_repeated_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    prepared = _prepared_paper(
        cache_root / "dataset" / dataset["revision"],
        paper_id=9,
        dataset_id=dataset["id"],
        dataset_revision=dataset["revision"],
    )
    run_dir, initial = asyncio.run(run_benchmark(paper_ids=[9], cache_root=cache_root, runs_root=tmp_path / "runs"))
    assert initial["status"] == "awaiting_review"
    source_paths = {item["path"] for item in initial["frozen_inputs"]["source_manifest"]}
    assert "egs/peerreviewbench/run.py" in source_paths
    assert "src/scriptorium/service.py" in source_paths
    assert "skills/scriptorium/SKILL.md" in source_paths

    entry = initial["papers"]["9"]
    project = run_dir / entry["project_dir"]
    with ScriptoriumService(project, manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared)) as service:
        _complete_reviews(service, entry["scriptorium_run_id"])
    _, completed = asyncio.run(run_benchmark(resume=run_dir, cache_root=cache_root))
    assert completed["status"] == "complete"
    summary = completed["papers"]["9"]["scriptorium"]
    assert summary["status"] == RunStatus.AWAITING_DECISION.value
    assert summary["estimated_cost_usd"] is None
    assert len(summary["tasks"]) == 4
    assert len(summary["finding_payload_digest"]) == 64
    assert {attempt["model"] for task in summary["tasks"] for attempt in task["attempts"]} == {"test-model"}
    manifest_bytes = (run_dir / "run_manifest.json").read_bytes()

    resumed_dir, resumed = asyncio.run(run_benchmark(resume=run_dir, cache_root=cache_root))
    assert resumed_dir == run_dir
    assert resumed["status"] == "complete"
    assert resumed["papers"]["9"]["scriptorium_run_id"] == entry["scriptorium_run_id"]
    assert (run_dir / "run_manifest.json").read_bytes() == manifest_bytes

    monkeypatch.setattr(benchmark_run, "source_manifest", lambda: [{"path": "changed"}])
    with pytest.raises(BenchmarkError, match="do not match the resumed run"):
        asyncio.run(run_benchmark(resume=run_dir, cache_root=cache_root))
    assert json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))["status"] == "complete"


def test_shared_skill_change_invalidates_frozen_benchmark_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for relative in benchmark_run.BENCHMARK_SOURCE_FILES:
        source = benchmark_run.REPOSITORY_ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    manifest = benchmark_run.source_manifest
    monkeypatch.setattr(benchmark_run, "repository_commit", lambda: "unchanged-commit")
    monkeypatch.setattr(benchmark_run, "source_manifest", lambda: manifest(tmp_path))
    frozen = {
        "scriptorium_commit": "unchanged-commit",
        "source_manifest": manifest(tmp_path),
    }
    skill = tmp_path / "skills/scriptorium/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nChanged reviewer instructions.\n", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="Benchmark source files changed"):
        benchmark_run._validate_frozen_benchmark_sources(frozen)


def test_corrupt_completed_artifact_stays_incomplete_across_resumes(tmp_path: Path) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    prepared = _prepared_paper(
        cache_root / "dataset" / dataset["revision"],
        paper_id=13,
        dataset_id=dataset["id"],
        dataset_revision=dataset["revision"],
    )
    run_dir, initial = asyncio.run(run_benchmark(paper_ids=[13], cache_root=cache_root, runs_root=tmp_path / "runs"))
    entry = initial["papers"]["13"]
    project = run_dir / entry["project_dir"]
    with ScriptoriumService(project, manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared)) as service:
        _complete_reviews(service, entry["scriptorium_run_id"])
    _, completed = asyncio.run(run_benchmark(resume=run_dir, cache_root=cache_root))
    assert completed["status"] == "complete"
    digest = completed["papers"]["13"]["scriptorium"]["tasks"][0]["attempts"][0]["output_artifact_digest"]
    artifact = project / ".scriptorium" / "artifacts" / "sha256" / digest[:2] / digest[2:]
    artifact.unlink()

    for _ in range(2):
        _, resumed = asyncio.run(run_benchmark(resume=run_dir, cache_root=cache_root))
        assert resumed["status"] == "incomplete"
        assert resumed["papers"]["13"]["status"] == "incomplete"


def test_benchmark_manifest_rejects_project_path_escape() -> None:
    manifest = {
        "benchmark": "peerreviewbench",
        "run_id": "benchmark-run",
        "status": "complete",
        "frozen_inputs": {"paper_ids": [1]},
        "package_versions": {},
        "papers": {
            "1": {
                "paper_id": 1,
                "project_dir": "../paper1",
                "status": "complete",
                "prepared_manifest_digest": "0" * 64,
            }
        },
    }

    with pytest.raises(BenchmarkError, match="invalid paper1 entry"):
        benchmark_run.validate_benchmark_manifest(manifest)


@pytest.mark.parametrize("run_id", ("../outside", "/absolute", "nested/run"))
def test_benchmark_manifest_rejects_unsafe_run_id(run_id: str) -> None:
    manifest = {
        "benchmark": "peerreviewbench",
        "run_id": run_id,
        "status": "incomplete",
        "frozen_inputs": {"paper_ids": [1]},
        "package_versions": {},
        "papers": {
            "1": {
                "paper_id": 1,
                "project_dir": "papers/paper1",
                "status": "incomplete",
                "prepared_manifest_digest": "0" * 64,
            }
        },
    }

    with pytest.raises(BenchmarkError, match="unsafe run ID"):
        benchmark_run.validate_benchmark_manifest(manifest)


def test_complete_benchmark_manifest_requires_complete_papers() -> None:
    manifest = {
        "benchmark": "peerreviewbench",
        "run_id": "benchmark-run",
        "status": "complete",
        "frozen_inputs": {"paper_ids": [1]},
        "package_versions": {},
        "papers": {
            "1": {
                "paper_id": 1,
                "project_dir": "papers/paper1",
                "status": "incomplete",
                "prepared_manifest_digest": "0" * 64,
            }
        },
    }

    with pytest.raises(BenchmarkError, match="contains incomplete papers"):
        benchmark_run.validate_benchmark_manifest(manifest)


def test_incomplete_benchmark_resumes_only_pending_review_lane(tmp_path: Path) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    prepared = _prepared_paper(
        cache_root / "dataset" / dataset["revision"],
        paper_id=10,
        dataset_id=dataset["id"],
        dataset_revision=dataset["revision"],
    )
    run_dir, initial = asyncio.run(run_benchmark(paper_ids=[10], cache_root=cache_root, runs_root=tmp_path / "runs"))
    assert initial["status"] == "awaiting_review"
    entry = initial["papers"]["10"]
    project = run_dir / entry["project_dir"]
    run_id = entry["scriptorium_run_id"]
    with ScriptoriumService(project, manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared)) as service:
        _complete_reviews(service, run_id, skip=AgentRole.COPYEDIT)
        before = service.get_run(run_id)
        assert before["run"].status == RunStatus.REVIEWING
        before_by_role = {item["task"].role: item for item in before["tasks"]}
        preserved = {
            role: (before_by_role[role]["task"].id, [attempt.id for attempt in before_by_role[role]["attempts"]])
            for role in ROLES - {AgentRole.COPYEDIT}
        }
        assert before_by_role[AgentRole.COPYEDIT]["task"].status == TaskStatus.PENDING

    _, waiting = asyncio.run(run_benchmark(resume=run_dir, cache_root=cache_root))
    assert waiting["status"] == "awaiting_review"
    with ScriptoriumService(project, manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared)) as service:
        _complete_reviews(service, run_id)
    _, completed = asyncio.run(run_benchmark(resume=run_dir, cache_root=cache_root))
    assert completed["status"] == "complete"
    with ScriptoriumService(project, manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared)) as service:
        after = service.get_run(run_id)
        assert after["run"].status == RunStatus.AWAITING_DECISION
        after_by_role = {item["task"].role: item for item in after["tasks"]}
        for role, (task_id, attempt_ids) in preserved.items():
            assert after_by_role[role]["task"].id == task_id
            assert [attempt.id for attempt in after_by_role[role]["attempts"]] == attempt_ids
        assert len(after_by_role[AgentRole.COPYEDIT]["attempts"]) == 1


def test_failed_paper_does_not_stop_later_papers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    for paper_id in (11, 12):
        _prepared_paper(
            cache_root / "dataset" / dataset["revision"],
            paper_id=paper_id,
            dataset_id=dataset["id"],
            dataset_revision=dataset["revision"],
        )
    original = benchmark_run._run_paper

    async def fail_first(entry, run_dir, prepared_paper):
        if entry["paper_id"] == 11:
            raise RuntimeError("paper-specific failure")
        return await original(entry, run_dir, prepared_paper)

    monkeypatch.setattr(benchmark_run, "_run_paper", fail_first)
    _, manifest = asyncio.run(run_benchmark(paper_ids=[11, 12], cache_root=cache_root, runs_root=tmp_path / "runs"))
    assert manifest["status"] == "incomplete"
    assert manifest["papers"]["11"]["status"] == "incomplete"
    assert manifest["papers"]["12"]["status"] == "awaiting_review"


def test_source_change_before_next_paper_stops_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    for paper_id in (18, 24):
        _prepared_paper(
            cache_root / "dataset" / dataset["revision"],
            paper_id=paper_id,
            dataset_id=dataset["id"],
            dataset_revision=dataset["revision"],
        )
    frozen_sources = benchmark_run.source_manifest()
    source_calls = 0
    paper_calls = 0

    def changing_source_manifest():
        nonlocal source_calls
        source_calls += 1
        return frozen_sources if source_calls <= 3 else [{"path": "changed"}]

    async def fake_run_paper(entry, *args, **kwargs):
        nonlocal paper_calls
        paper_calls += 1
        entry["status"] = "awaiting_review"
        return entry

    monkeypatch.setattr(benchmark_run, "source_manifest", changing_source_manifest)
    monkeypatch.setattr(benchmark_run, "_run_paper", fake_run_paper)
    with pytest.raises(BenchmarkError, match="Benchmark source files changed"):
        asyncio.run(run_benchmark(paper_ids=[18, 24], cache_root=cache_root, runs_root=tmp_path / "runs"))
    assert paper_calls == 1
    run_dir = next((tmp_path / "runs").iterdir())
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] != "complete"
    assert manifest["papers"]["18"]["status"] == "awaiting_review"
    assert manifest["papers"]["24"]["status"] == "pending"


def test_source_change_during_paper_marks_it_incomplete_and_stops_later_papers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    for paper_id in (19, 20):
        _prepared_paper(
            cache_root / "dataset" / dataset["revision"],
            paper_id=paper_id,
            dataset_id=dataset["id"],
            dataset_revision=dataset["revision"],
        )
    frozen_sources = benchmark_run.source_manifest()
    source_changed = False
    paper_calls: list[int] = []

    def changing_source_manifest():
        return [{"path": "changed"}] if source_changed else frozen_sources

    async def fake_run_paper(entry, *args, **kwargs):
        nonlocal source_changed
        paper_calls.append(entry["paper_id"])
        entry["status"] = "awaiting_review"
        source_changed = True
        return entry

    monkeypatch.setattr(benchmark_run, "source_manifest", changing_source_manifest)
    monkeypatch.setattr(benchmark_run, "_run_paper", fake_run_paper)
    run_dir, manifest = asyncio.run(
        run_benchmark(paper_ids=[19, 20], cache_root=cache_root, runs_root=tmp_path / "runs")
    )
    assert paper_calls == [19]
    assert manifest["status"] == "incomplete"
    assert manifest["papers"]["19"]["status"] == "incomplete"
    assert "Benchmark source files changed" in manifest["papers"]["19"]["error"]
    assert manifest["papers"]["20"]["status"] == "pending"
    assert json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8")) == manifest


def test_commit_change_during_paper_marks_it_incomplete_and_stops_later_papers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    for paper_id in (21, 22):
        _prepared_paper(
            cache_root / "dataset" / dataset["revision"],
            paper_id=paper_id,
            dataset_id=dataset["id"],
            dataset_revision=dataset["revision"],
        )
    frozen_commit = benchmark_run.repository_commit()
    commit_changed = False
    paper_calls: list[int] = []

    def changing_repository_commit():
        return "0" * 40 if commit_changed else frozen_commit

    async def fake_run_paper(entry, *args, **kwargs):
        nonlocal commit_changed
        paper_calls.append(entry["paper_id"])
        entry["status"] = "awaiting_review"
        commit_changed = True
        return entry

    monkeypatch.setattr(benchmark_run, "repository_commit", changing_repository_commit)
    monkeypatch.setattr(benchmark_run, "_run_paper", fake_run_paper)
    _, manifest = asyncio.run(run_benchmark(paper_ids=[21, 22], cache_root=cache_root, runs_root=tmp_path / "runs"))
    assert paper_calls == [21]
    assert manifest["status"] == "incomplete"
    assert manifest["papers"]["21"]["status"] == "incomplete"
    assert "Scriptorium commit changed" in manifest["papers"]["21"]["error"]
    assert manifest["papers"]["22"]["status"] == "pending"
