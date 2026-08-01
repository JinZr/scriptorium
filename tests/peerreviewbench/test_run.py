import asyncio
from hashlib import sha256
import json
from pathlib import Path
import shutil

import fitz
import pytest

from egs.peerreviewbench.prepare import BenchmarkError, file_digest, load_lock
import egs.peerreviewbench.run as benchmark_run
from egs.peerreviewbench.run import PeerReviewBenchManuscriptManager, create_paper_project, run_benchmark
from scriptorium.config import load_project_config
from scriptorium.domain import AgentRole, AttemptStatus, RunStatus, TaskStatus
from scriptorium.runtime import AgentResult, AgentUsage
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


class FullProfileRuntime:
    def __init__(self) -> None:
        self.calls: list[AgentRole] = []

    async def run_agent(self, task, role, workspace, schema, session_dir):
        del task, schema, session_dir
        assert role in ROLES
        self.calls.append(role)
        source = workspace / "sources" / "preprint" / "preprint.md"
        output = {
            "summary": f"One {role.value} finding.",
            "findings": [
                {
                    "category": role.value,
                    "severity": "moderate",
                    "title": f"{role.value} finding",
                    "claim": f"The {role.value} reviewer found an issue.",
                    "evidence": [
                        {
                            "source_path": "preprint/preprint.md",
                            "start_line": 3,
                            "end_line": 3,
                            "source_digest": sha256(source.read_bytes()).hexdigest(),
                            "page": 1,
                            "quoted_text": "The reported result needs review.",
                        }
                    ],
                    "explanation": "The finding is grounded in the frozen Markdown source.",
                    "suggested_action": "Inspect and clarify the reported result.",
                    "confidence": 0.9,
                }
            ],
        }
        return AgentResult(
            thread_id=f"thread-{role.value}",
            status="completed",
            final_response=json.dumps(output),
            usage=AgentUsage(input_tokens=10, cached_input_tokens=2, output_tokens=4, reasoning_tokens=1),
            trace_jsonl=json.dumps({"role": role.value, "status": "completed"}) + "\n",
            runtime_name="codex",
            runtime_version="0.144.4",
            model="fake-model",
            model_provider="test",
            duration_ms=5,
            error=None,
        )

    async def resume_agent(self, thread_id, task, role, workspace, schema, session_dir):
        raise AssertionError(f"unexpected resume for {thread_id}/{role.value}")


class InterruptingFullProfileRuntime(FullProfileRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False
        self.resume_calls: list[AgentRole] = []

    async def run_agent(self, task, role, workspace, schema, session_dir):
        if role == AgentRole.COPYEDIT and not self.interrupted:
            self.interrupted = True
            self.calls.append(role)
            return AgentResult(
                thread_id="thread-copyedit-interrupted",
                status="interrupted",
                final_response=None,
                usage=AgentUsage(input_tokens=10, cached_input_tokens=2, output_tokens=0, reasoning_tokens=1),
                trace_jsonl=json.dumps({"role": role.value, "status": "interrupted"}) + "\n",
                runtime_name="codex",
                runtime_version="0.144.4",
                model="fake-model",
                model_provider="test",
                duration_ms=5,
                error="simulated interruption",
            )
        return await super().run_agent(task, role, workspace, schema, session_dir)

    async def resume_agent(self, thread_id, task, role, workspace, schema, session_dir):
        assert thread_id == "thread-copyedit-interrupted"
        assert role == AgentRole.COPYEDIT
        self.resume_calls.append(role)
        return await super().run_agent(task, role, workspace, schema, session_dir)


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


def _routes(path: Path) -> Path:
    path.write_text(
        (
            "max_concurrency = 4\n\n"
            "[roles]\n"
            'substantive_review = "primary"\n'
            'copyedit = "primary"\n'
            'consistency = "primary"\n'
            'figure_review = "primary"\n'
            'revision = "primary"\n'
            'verification = "primary"\n\n'
            "[routes.primary]\n"
            'runtime = "codex"\n'
            'model_provider = "test"\n'
            'model = "fake-model"\n'
            "input_usd_per_million = 2\n"
            "output_usd_per_million = 3\n"
            'reasoning_effort = "high"\n'
        ),
        encoding="utf-8",
    )
    return path


def _project(tmp_path: Path, prepared: Path) -> tuple[Path, PeerReviewBenchManuscriptManager]:
    project = tmp_path / "project"
    create_paper_project(project, _routes(tmp_path / "routes.toml"))
    (project / "AGENTS.md").write_text("This project file must not enter the benchmark bundle.\n", encoding="utf-8")
    return project, PeerReviewBenchManuscriptManager(project, prepared)


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

    bundle = manager.create_bundle(snapshot, tmp_path / "bundle", revision, sources, build.pdf_path)

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


def test_generated_project_rejects_symlinked_state(tmp_path: Path) -> None:
    routes = _routes(tmp_path / "routes.toml")
    project = tmp_path / "project"
    create_paper_project(project, routes)
    state = project / ".scriptorium"
    moved = tmp_path / "external-state"
    state.rename(moved)
    state.symlink_to(moved, target_is_directory=True)

    with pytest.raises(BenchmarkError, match="missing or unsafe"):
        benchmark_run.validate_paper_project(project, routes)


def test_package_versions_include_pydantic() -> None:
    assert "pydantic" in benchmark_run.package_versions()


def test_full_profile_service_persists_findings_artifacts_and_cost(tmp_path: Path) -> None:
    prepared = _prepared_paper(
        tmp_path / "prepared",
        paper_id=8,
        dataset_id="test/peerreview-bench",
        dataset_revision="locked-revision",
    )
    project, manager = _project(tmp_path, prepared)
    runtime = FullProfileRuntime()

    with ScriptoriumService(
        project,
        runtime_factory=lambda route: runtime,
        manuscript_manager=manager,
    ) as service:
        view = asyncio.run(service.start_run("prepared", "full", None))
        run = view["run"]

        assert run.status == RunStatus.AWAITING_DECISION
        assert run.estimated_cost_usd == pytest.approx(0.000128)
        assert set(runtime.calls) == ROLES
        assert len(runtime.calls) == 4

        review_tasks = [item for item in view["tasks"] if item["task"].stage == "review"]
        assert {item["task"].role for item in review_tasks} == ROLES
        assert {item["task"].status for item in review_tasks} == {TaskStatus.COMPLETED}
        attempts = [attempt for item in review_tasks for attempt in item["attempts"]]
        assert len(attempts) == 4
        assert all(attempt.estimated_cost_usd == pytest.approx(0.000032) for attempt in attempts)
        for attempt in attempts:
            assert service.database.get_artifact(attempt.output_artifact_digest).media_type == "application/json"
            assert (
                service.database.get_artifact(attempt.trace_artifact_digest).media_type
                == "application/x-ndjson; charset=utf-8"
            )

        findings = service.list_findings(run.id)
        assert len(findings) == 4
        assert {finding.role for finding in findings} == ROLES
        assert {finding.task_id for finding in findings} == {item["task"].id for item in review_tasks}
        frozen_entry = {
            "paper_id": 8,
            "scriptorium_run_id": run.id,
            "scriptorium": benchmark_run.summarize_scriptorium_run(service, run.id),
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


def test_completed_benchmark_paper_is_not_repeated_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    _prepared_paper(
        cache_root / "dataset" / dataset["revision"],
        paper_id=9,
        dataset_id=dataset["id"],
        dataset_revision=dataset["revision"],
    )
    routes = _routes(tmp_path / "routes.toml")
    runtime = FullProfileRuntime()

    run_dir, initial = asyncio.run(
        run_benchmark(
            paper_ids=[9],
            cache_root=cache_root,
            runs_root=tmp_path / "runs",
            routes_path=routes,
            runtime_factory=lambda route: runtime,
        )
    )

    assert initial["status"] == "complete"
    assert initial["papers"]["9"]["status"] == "complete"
    assert initial["frozen_inputs"]["route_config"]["routes"]["primary"]["model"] == "fake-model"
    source_paths = {item["path"] for item in initial["frozen_inputs"]["source_manifest"]}
    assert "egs/peerreviewbench/run.py" in source_paths
    assert "egs/peerreviewbench/precision.Dockerfile" in source_paths
    assert "egs/peerreviewbench/.dockerignore" in source_paths
    assert "src/scriptorium/service.py" in source_paths
    paper_summary = initial["papers"]["9"]["scriptorium"]
    assert paper_summary["status"] == RunStatus.AWAITING_DECISION.value
    assert paper_summary["estimated_cost_usd"] == pytest.approx(0.000128)
    assert len(paper_summary["tasks"]) == 4
    assert len(paper_summary["finding_payload_digest"]) == 64
    assert {
        (attempt["model"], attempt["model_provider"]) for task in paper_summary["tasks"] for attempt in task["attempts"]
    } == {("fake-model", "test")}
    assert len(runtime.calls) == 4
    manifest_bytes = (run_dir / "run_manifest.json").read_bytes()

    resumed_dir, resumed = asyncio.run(
        run_benchmark(
            resume=run_dir,
            cache_root=cache_root,
            runs_root=tmp_path / "unused-runs",
            routes_path=routes,
            runtime_factory=lambda route: runtime,
        )
    )

    assert resumed_dir == run_dir
    assert resumed["status"] == "complete"
    assert resumed["papers"]["9"]["scriptorium_run_id"] == initial["papers"]["9"]["scriptorium_run_id"]
    assert len(runtime.calls) == 4
    assert (run_dir / "run_manifest.json").read_bytes() == manifest_bytes

    monkeypatch.setattr(benchmark_run, "source_manifest", lambda: [{"path": "changed"}])
    with pytest.raises(BenchmarkError, match="do not match the resumed run"):
        asyncio.run(
            run_benchmark(
                resume=run_dir,
                cache_root=cache_root,
                routes_path=routes,
                runtime_factory=lambda route: runtime,
            )
        )
    assert json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))["status"] == "complete"


def test_corrupt_completed_artifact_stays_incomplete_across_resumes(tmp_path: Path) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    _prepared_paper(
        cache_root / "dataset" / dataset["revision"],
        paper_id=13,
        dataset_id=dataset["id"],
        dataset_revision=dataset["revision"],
    )
    routes = _routes(tmp_path / "routes.toml")
    runtime = FullProfileRuntime()
    run_dir, initial = asyncio.run(
        run_benchmark(
            paper_ids=[13],
            cache_root=cache_root,
            runs_root=tmp_path / "runs",
            routes_path=routes,
            runtime_factory=lambda route: runtime,
        )
    )
    digest = initial["papers"]["13"]["scriptorium"]["tasks"][0]["attempts"][0]["output_artifact_digest"]
    artifact = run_dir / "papers" / "paper13" / ".scriptorium" / "artifacts" / "sha256" / digest[:2] / digest[2:]
    artifact.unlink()

    for _ in range(2):
        _, resumed = asyncio.run(
            run_benchmark(
                resume=run_dir,
                cache_root=cache_root,
                routes_path=routes,
                runtime_factory=lambda route: runtime,
            )
        )
        assert resumed["status"] == "incomplete"
        assert resumed["papers"]["13"]["status"] == "incomplete"
    assert len(runtime.calls) == 4


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


def test_incomplete_benchmark_resumes_only_interrupted_review_lane(tmp_path: Path) -> None:
    lock = load_lock()
    dataset = lock["dataset"]
    cache_root = tmp_path / "cache"
    prepared = _prepared_paper(
        cache_root / "dataset" / dataset["revision"],
        paper_id=10,
        dataset_id=dataset["id"],
        dataset_revision=dataset["revision"],
    )
    routes = _routes(tmp_path / "routes.toml")
    runtime = InterruptingFullProfileRuntime()

    run_dir, initial = asyncio.run(
        run_benchmark(
            paper_ids=[10],
            cache_root=cache_root,
            runs_root=tmp_path / "runs",
            routes_path=routes,
            runtime_factory=lambda route: runtime,
        )
    )

    assert initial["status"] == "incomplete"
    assert initial["papers"]["10"]["status"] == "incomplete"
    assert set(runtime.calls) == ROLES
    assert len(runtime.calls) == 4

    project = run_dir / "papers" / "paper10"
    run_id = initial["papers"]["10"]["scriptorium_run_id"]
    with ScriptoriumService(
        project,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared),
    ) as service:
        before = service.get_run(run_id)
        assert before["run"].status == RunStatus.REVIEWING
        before_by_role = {item["task"].role: item for item in before["tasks"]}
        preserved = {
            role: (
                before_by_role[role]["task"].id,
                [attempt.id for attempt in before_by_role[role]["attempts"]],
            )
            for role in ROLES - {AgentRole.COPYEDIT}
        }
        assert all(before_by_role[role]["task"].status == TaskStatus.COMPLETED for role in preserved)
        copyedit_before = before_by_role[AgentRole.COPYEDIT]
        assert copyedit_before["task"].status == TaskStatus.INTERRUPTED
        assert [attempt.status for attempt in copyedit_before["attempts"]] == [AttemptStatus.INTERRUPTED]

    resumed_dir, resumed = asyncio.run(
        run_benchmark(
            resume=run_dir,
            cache_root=cache_root,
            runs_root=tmp_path / "unused-runs",
            routes_path=routes,
            runtime_factory=lambda route: runtime,
        )
    )

    assert resumed_dir == run_dir
    assert resumed["status"] == "complete"
    assert resumed["papers"]["10"]["status"] == "complete"
    assert resumed["papers"]["10"]["scriptorium"]["status"] == RunStatus.AWAITING_DECISION.value
    assert runtime.resume_calls == [AgentRole.COPYEDIT]
    assert runtime.calls.count(AgentRole.COPYEDIT) == 2
    assert all(runtime.calls.count(role) == 1 for role in ROLES - {AgentRole.COPYEDIT})

    with ScriptoriumService(
        project,
        runtime_factory=lambda route: runtime,
        manuscript_manager=PeerReviewBenchManuscriptManager(project, prepared),
    ) as service:
        after = service.get_run(run_id)
        assert after["run"].status == RunStatus.AWAITING_DECISION
        after_by_role = {item["task"].role: item for item in after["tasks"]}
        for role, (task_id, attempt_ids) in preserved.items():
            assert after_by_role[role]["task"].id == task_id
            assert [attempt.id for attempt in after_by_role[role]["attempts"]] == attempt_ids
        copyedit_after = after_by_role[AgentRole.COPYEDIT]
        assert copyedit_after["task"].id == copyedit_before["task"].id
        assert [attempt.id for attempt in copyedit_after["attempts"][:1]] == [copyedit_before["attempts"][0].id]
        assert [attempt.status for attempt in copyedit_after["attempts"]] == [
            AttemptStatus.INTERRUPTED,
            AttemptStatus.COMPLETED,
        ]


def test_failed_paper_does_not_stop_later_papers(tmp_path: Path) -> None:
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
    runtime = InterruptingFullProfileRuntime()

    _, manifest = asyncio.run(
        run_benchmark(
            paper_ids=[11, 12],
            cache_root=cache_root,
            runs_root=tmp_path / "runs",
            routes_path=_routes(tmp_path / "routes.toml"),
            runtime_factory=lambda route: runtime,
        )
    )

    assert manifest["status"] == "incomplete"
    assert manifest["papers"]["11"]["status"] == "incomplete"
    assert manifest["papers"]["12"]["status"] == "complete"
    assert manifest["papers"]["12"]["scriptorium"]["status"] == RunStatus.AWAITING_DECISION.value
