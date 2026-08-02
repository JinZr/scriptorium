from __future__ import annotations

from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from scriptorium.domain import AgentRole, Finding, FindingSeverity

EGS_ROOT = Path(__file__).resolve().parents[2] / "egs" / "peerreviewbench"


def _load_evaluate_module():
    sys.path.insert(0, str(EGS_ROOT))
    try:
        spec = importlib.util.spec_from_file_location("peerreviewbench_evaluate", EGS_ROOT / "evaluate.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EGS_ROOT))


benchmark_evaluate = _load_evaluate_module()
PRECISION_IMAGE_ID = "sha256:" + "a" * 64


def _finding(
    role: AgentRole,
    ordinal: int,
    *,
    severity: FindingSeverity = FindingSeverity.MODERATE,
    evidence: tuple[dict[str, object], ...] | None = None,
) -> Finding:
    return Finding(
        run_id="run_1",
        task_id=f"task_{role.value}",
        attempt_id=f"attempt_{role.value}",
        fingerprint=f"{role.value}-{ordinal}",
        role=role,
        category="methods",
        severity=severity,
        title=f"{role.value} title {ordinal}",
        claim=f"{role.value} claim {ordinal}",
        evidence=evidence
        or (
            {
                "source_path": "preprint/preprint.md",
                "start_line": ordinal,
                "end_line": ordinal,
                "quoted_text": f"quote {ordinal}",
            },
        ),
        explanation=f"{role.value} explanation {ordinal}",
        suggested_action=f"private suggested action {ordinal}",
        confidence=0.8,
        id=f"finding_{role.value}_{ordinal}",
    )


def _selection_stats(*, raw: int = 1, selected: int = 1) -> dict[str, object]:
    by_role = {
        role: {
            "raw": raw if role == "substantive_review" else 0,
            "selected": selected if role == "substantive_review" else 0,
            "dropped": raw - selected if role == "substantive_review" else 0,
        }
        for role in benchmark_evaluate.ROLE_ORDER
    }
    return {
        "total": {
            "raw": raw,
            "selected": selected,
            "dropped": raw - selected,
            "by_role": by_role,
        },
        "per_paper": {},
    }


def _valid_component_outputs() -> tuple[dict[str, object], dict[str, object]]:
    recall = {
        "elapsed_seconds": 1.25,
        "n_papers": 1,
        "total_rubric_items": 2,
        "total_covered": 1,
        "overall_recall": 0.5,
        "per_paper": [
            {
                "paper_id": 1,
                "n_rubric": 2,
                "n_ai": 1,
                "n_pairs_scored": 2,
                "n_covered": 1,
                "recall": 0.5,
                "pair_details": [
                    {
                        "rubric_idx": 0,
                        "ai_item_number": 1,
                        "parsed_binary": "similar",
                        "is_similar": True,
                        "error": None,
                    },
                    {
                        "rubric_idx": 1,
                        "ai_item_number": 1,
                        "parsed_binary": "not_similar",
                        "is_similar": False,
                        "error": None,
                    },
                ],
            }
        ],
    }
    precision = {
        "elapsed_seconds": 2.5,
        "n_papers": 1,
        "n_items": 1,
        "n_fully_good": 1,
        "precision": 1.0,
        "per_item": [
            {
                "paper_id": 1,
                "item_number": 1,
                "correctness": "Correct",
                "significance": "Significant",
                "evidence": "Sufficient",
                "is_fully_good": True,
            }
        ],
    }
    return recall, precision


def test_select_findings_supports_all_and_deterministic_per_role_limit() -> None:
    role_members = {
        role: [_finding(AgentRole(role), ordinal) for ordinal in range(1, 7)] for role in benchmark_evaluate.ROLE_ORDER
    }
    findings = [role_members[role][ordinal] for ordinal in range(6) for role in reversed(benchmark_evaluate.ROLE_ORDER)]

    selected_all, all_stats = benchmark_evaluate.select_findings(findings, "all")
    selected_limited, limited_stats = benchmark_evaluate.select_findings(findings, "per-role-5")

    assert selected_all == findings
    assert all_stats["raw"] == all_stats["selected"] == 24
    assert all_stats["dropped"] == 0
    assert selected_limited == [finding for role in benchmark_evaluate.ROLE_ORDER for finding in role_members[role][:5]]
    assert limited_stats["raw"] == 24
    assert limited_stats["selected"] == 20
    assert limited_stats["dropped"] == 4
    assert all(values == {"raw": 6, "selected": 5, "dropped": 1} for values in limited_stats["by_role"].values())


def test_export_findings_maps_byoj_fields_without_suggested_action() -> None:
    findings = [
        _finding(AgentRole.SUBSTANTIVE_REVIEW, 4),
        _finding(
            AgentRole.FIGURE_REVIEW,
            9,
            evidence=(
                {
                    "source_path": "manuscript.pdf",
                    "page": 3,
                    "quoted_text": "figure evidence",
                },
            ),
        ),
    ]

    items = benchmark_evaluate.export_findings(findings)

    assert [item["item_number"] for item in items] == [1, 2]
    assert items[0]["title"] == findings[0].title
    assert items[0]["main_point"] == findings[0].claim
    assert items[0]["claim_full"] == f"{findings[0].claim}\n\n{findings[0].explanation}"
    assert items[0]["evidence_full"] == "[preprint/preprint.md:4] quote 4"
    assert items[0]["text"] == f"{items[0]['claim_full']}\n\n{items[0]['evidence_full']}"
    assert items[1]["evidence_full"] == "[manuscript.pdf page 3] figure evidence"
    assert items[1]["scriptorium"]["finding_id"] == findings[1].id
    assert items[1]["scriptorium"]["role"] == AgentRole.FIGURE_REVIEW.value
    assert "private suggested action" not in json.dumps(items)


def test_evaluation_view_uses_plural_reviews_path_and_detects_changes(tmp_path) -> None:
    cache_root = tmp_path / "cache"
    preprint = cache_root / "dataset" / "revision-1" / "paper7" / "preprint"
    preprint.mkdir(parents=True)
    content = b"# Frozen paper\n"
    (preprint / "preprint.md").write_bytes(content)
    (preprint.parent / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "owner/dataset",
                "dataset_revision": "revision-1",
                "paper_id": 7,
                "paper_title": "Frozen paper",
                "files": [
                    {
                        "path": "preprint.md",
                        "content_hash": sha256(content).hexdigest(),
                        "size_bytes": len(content),
                        "is_text": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    exports = {7: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    frozen_inputs = {
        "dataset": {"revision": "revision-1"},
        "model_slug": "scriptorium_all",
    }
    evaluation_dir = tmp_path / "evaluations" / "run-1" / "all"

    benchmark_evaluate.prepare_evaluation_view(
        evaluation_dir,
        frozen_inputs,
        _selection_stats(),
        exports,
        cache_root,
    )

    paper_view = evaluation_dir / "papers" / "paper7"
    review_path = paper_view / "reviews" / "review_items_scriptorium_all.json"
    assert (paper_view / "preprint").is_dir()
    assert not (paper_view / "preprint").is_symlink()
    assert (paper_view / "preprint" / "preprint.md").read_bytes() == content
    assert review_path.is_file()
    assert not (paper_view / "review").exists()
    assert json.loads(review_path.read_text(encoding="utf-8")) == exports[7]
    assert (preprint / "preprint.md").read_text(encoding="utf-8") == "# Frozen paper\n"

    review_path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(benchmark_evaluate.BenchmarkError, match="BYOJ export changed"):
        benchmark_evaluate.prepare_evaluation_view(
            evaluation_dir,
            frozen_inputs,
            _selection_stats(),
            exports,
            cache_root,
        )

    review_path.write_text(json.dumps(exports[7]), encoding="utf-8")
    (paper_view / "preprint" / "preprint.md").write_text("# Modified by judge\n", encoding="utf-8")
    with pytest.raises(benchmark_evaluate.BenchmarkError, match="preprint copy changed"):
        benchmark_evaluate.prepare_evaluation_view(
            evaluation_dir,
            frozen_inputs,
            _selection_stats(),
            exports,
            cache_root,
        )
    assert (preprint / "preprint.md").read_text(encoding="utf-8") == "# Frozen paper\n"


def test_evaluation_view_rejects_symlinked_manifest_before_reading(tmp_path) -> None:
    evaluation_dir = tmp_path / "evaluation"
    evaluation_dir.mkdir()
    external = tmp_path / "external.json"
    external.write_text("{}\n", encoding="utf-8")
    (evaluation_dir / "evaluation_manifest.json").symlink_to(external)

    with pytest.raises(benchmark_evaluate.BenchmarkError, match="manifest is unsafe"):
        benchmark_evaluate.prepare_evaluation_view(
            evaluation_dir,
            {"dataset": {"revision": "revision-1"}, "model_slug": "scriptorium_all"},
            _selection_stats(),
            {},
            tmp_path / "cache",
        )


def test_component_commands_use_separate_supported_entrypoints(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_API_KEY", "must-not-appear-in-command")
    recall, precision = benchmark_evaluate.build_component_commands(
        tmp_path / "upstream",
        tmp_path / "papers",
        tmp_path / "output",
        dataset_id="owner/dataset",
        dataset_revision="revision-1",
        model_slug="scriptorium_per_role_5",
        similarity_model="similarity-model",
        judge_model="precision-model",
        concurrency=7,
        temperature=0.25,
        precision_image_id=PRECISION_IMAGE_ID,
    )

    assert recall[1] == "-c"
    assert "revision" in recall[2]
    compile(recall[2], "<pinned-dataset-launcher>", "exec")
    assert "owner/dataset" in recall
    assert "revision-1" in recall
    assert any(Path(part).name == "evaluate_recall.py" for part in recall)
    assert recall[recall.index("--model-name") + 1] == "scriptorium_per_role_5"
    assert recall[recall.index("--concurrency") + 1] == "7"
    assert recall[recall.index("--temperature") + 1] == "0.25"
    assert precision[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in precision
    assert "--cap-drop=ALL" in precision
    assert "--security-opt=no-new-privileges" in precision
    assert "--network=bridge" in precision
    assert PRECISION_IMAGE_ID in precision
    mounts = [precision[index + 1] for index, part in enumerate(precision) if part == "--mount"]
    precision_output = (tmp_path / "output" / "precision.json").resolve()
    assert set(mounts) == {
        f"type=bind,source={(tmp_path / 'upstream').resolve()},target=/upstream,readonly",
        f"type=bind,source={(tmp_path / 'papers').resolve()},target=/papers,readonly",
        f"type=bind,source={precision_output},target=/output/precision.json",
        f"type=bind,source={(tmp_path / 'output' / 'precision-work').resolve()},target=/output/precision-work",
        f"type=bind,source={(tmp_path / 'output' / 'precision-cache').resolve()},target=/cache",
    }
    launcher = precision[precision.index("-c") + 1]
    compile(launcher, "<precision-container-launcher>", "exec")
    assert "sys.stdin.readline" in launcher
    assert "__scriptorium_api_key" in launcher
    assert "--env=LITELLM_API_KEY" not in precision
    assert "--env=LITELLM_BASE_URL" not in precision
    assert "must-not-appear-in-command" not in " ".join(precision)
    assert any(Path(part).name == "evaluate_precision.py" for part in precision)
    assert precision[precision.index("--model-name") + 1] == "scriptorium_per_role_5"
    assert precision[precision.index("--judge-model") + 1] == "precision-model"
    assert "--output-dir" in precision
    assert "--concurrency" not in precision
    assert "--temperature" not in precision
    assert all(not any(Path(part).name == "evaluate.py" for part in command) for command in (recall, precision))


def test_precision_image_resolution_requires_an_immutable_image_id(monkeypatch) -> None:
    monkeypatch.setattr(
        benchmark_evaluate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=PRECISION_IMAGE_ID + "\n", stderr=""),
    )

    assert benchmark_evaluate.resolve_precision_image("precision:test") == PRECISION_IMAGE_ID

    monkeypatch.setattr(
        benchmark_evaluate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr="missing"),
    )
    with pytest.raises(benchmark_evaluate.BenchmarkError, match="image is unavailable"):
        benchmark_evaluate.resolve_precision_image("precision:test")


def test_precision_launcher_keeps_judge_credentials_out_of_the_environment(tmp_path, monkeypatch) -> None:
    upstream_root = tmp_path / "peerreview_bench"
    evaluation_root = upstream_root / "evaluation"
    evaluation_root.mkdir(parents=True)
    (evaluation_root / "precision_evaluation_marker.py").write_text("VALUE = 'evaluation'\n", encoding="utf-8")
    (upstream_root / "precision_root_marker.py").write_text("VALUE = 'root'\n", encoding="utf-8")
    script = evaluation_root / "evaluate_precision.py"
    output = tmp_path / "credentials.json"
    script.write_text(
        "from pathlib import Path\n"
        "import json, os, sys\n"
        "_HERE = Path(__file__).resolve().parent\n"
        "_BENCH_DIR = _HERE.parent\n"
        "for _path in (_HERE, _BENCH_DIR):\n"
        "    if str(_path) not in sys.path:\n"
        "        sys.path.insert(0, str(_path))\n"
        "from precision_evaluation_marker import VALUE as EVALUATION_MARKER\n"
        "from precision_root_marker import VALUE as ROOT_MARKER\n"
        "for _path in (str(_HERE), str(_BENCH_DIR)):\n"
        "    while _path in sys.path:\n"
        "        sys.path.remove(_path)\n"
        "def main():\n"
        "    api_key = os.environ.get('LITELLM_API_KEY')\n"
        "    base_url = os.environ.get('LITELLM_BASE_URL')\n"
        "    Path(sys.argv[1]).write_text(json.dumps([api_key, base_url, EVALUATION_MARKER, ROOT_MARKER]))\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    datasets = ModuleType("datasets")
    datasets.load_dataset = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "datasets", datasets)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"api_key": "judge-secret", "base_url": "https://judge.test"}) + "\n"),
    )
    monkeypatch.setattr(sys, "argv", ["launcher", "owner/dataset", "revision-1", str(script), str(output)])
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)

    exec(benchmark_evaluate.PRECISION_CONTAINER_LAUNCHER, {})

    assert json.loads(output.read_text(encoding="utf-8")) == [
        "judge-secret",
        "https://judge.test",
        "evaluation",
        "root",
    ]
    assert "LITELLM_API_KEY" not in os.environ
    assert "LITELLM_BASE_URL" not in os.environ


def test_pinned_dataset_launcher_honors_upstream_script_path_bootstrap(tmp_path, monkeypatch) -> None:
    upstream_root = tmp_path / "peerreview_bench"
    evaluation_root = upstream_root / "evaluation"
    evaluation_root.mkdir(parents=True)
    (evaluation_root / "recall_evaluation_marker.py").write_text("VALUE = 'evaluation'\n", encoding="utf-8")
    (upstream_root / "recall_root_marker.py").write_text("VALUE = 'root'\n", encoding="utf-8")
    script = evaluation_root / "evaluate_recall.py"
    output = tmp_path / "markers.txt"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "_HERE = Path(__file__).resolve().parent\n"
        "_BENCH_DIR = _HERE.parent\n"
        "for _path in (_HERE, _BENCH_DIR):\n"
        "    if str(_path) not in sys.path:\n"
        "        sys.path.insert(0, str(_path))\n"
        "from recall_evaluation_marker import VALUE as EVALUATION_MARKER\n"
        "from recall_root_marker import VALUE as ROOT_MARKER\n"
        "for _path in (str(_HERE), str(_BENCH_DIR)):\n"
        "    while _path in sys.path:\n"
        "        sys.path.remove(_path)\n"
        "Path(sys.argv[1]).write_text(EVALUATION_MARKER + ':' + ROOT_MARKER)\n",
        encoding="utf-8",
    )
    datasets = ModuleType("datasets")
    datasets.load_dataset = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "datasets", datasets)
    monkeypatch.setattr(sys, "argv", ["launcher", "owner/dataset", "revision-1", str(script), str(output)])

    exec(benchmark_evaluate.PINNED_DATASET_LAUNCHER, {})

    assert output.read_text(encoding="utf-8") == "evaluation:root"


def test_rubric_counts_launcher_emits_only_counts_and_pins_dataset_revision(tmp_path, monkeypatch, capsys) -> None:
    calls = []
    datasets = ModuleType("datasets")

    def fake_load_dataset(path, *args, **kwargs):
        calls.append((path, kwargs.get("revision")))

    datasets.load_dataset = fake_load_dataset
    build_rubric = ModuleType("build_rubric")

    def build_rubric_with_texts():
        datasets.load_dataset("owner/dataset")
        print("gold rubric text must stay out of stdout")
        return {1: [{"text": "secret"}, {"text": "secret"}], 2: [{"text": "secret"}]}, []

    build_rubric.build_rubric_with_texts = build_rubric_with_texts
    monkeypatch.setitem(sys.modules, "datasets", datasets)
    monkeypatch.setitem(sys.modules, "build_rubric", build_rubric)
    monkeypatch.setattr(
        sys,
        "argv",
        ["launcher", "owner/dataset", "revision-1", str(tmp_path / "evaluation")],
    )

    exec(benchmark_evaluate.RUBRIC_COUNTS_LAUNCHER, {})

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"1": 2, "2": 1}
    assert "gold rubric text" not in captured.out
    assert "gold rubric text" in captured.err
    assert calls == [("owner/dataset", "revision-1")]


def test_load_rubric_counts_validates_total_and_returns_selected_subset(tmp_path) -> None:
    def fake_runner(command, *, check, capture_output, text, input):
        assert command[0] == sys.executable
        assert command[2] == benchmark_evaluate.RUBRIC_COUNTS_LAUNCHER
        assert command[-3:] == ["owner/dataset", "revision-1", str(tmp_path / "upstream/peerreview_bench/evaluation")]
        assert check is False and capture_output is True and text is True and input is None
        return subprocess.CompletedProcess(command, 0, stdout='{"1": 2, "2": 3}', stderr="")

    counts = benchmark_evaluate.load_rubric_counts(
        tmp_path / "upstream",
        dataset_id="owner/dataset",
        dataset_revision="revision-1",
        paper_ids=[2],
        expected_total=5,
        runner=fake_runner,
    )

    assert counts == {"2": 3}


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_total", "paper_ids", "message"),
    [
        (1, "", 2, [1], "exited with 1"),
        (0, "not-json", 2, [1], "invalid JSON"),
        (0, "[]", 2, [1], "invalid counts"),
        (0, '{"01": 2}', 2, [1], "invalid counts"),
        (0, '{"1": true}', 1, [1], "invalid counts"),
        (0, '{"1": 0}', 0, [1], "invalid counts"),
        (0, '{"1": 2}', 3, [1], "rubric total differs"),
        (0, '{"1": 2}', 2, [2], "paper2"),
    ],
)
def test_load_rubric_counts_rejects_invalid_preflight_output(
    tmp_path,
    returncode: int,
    stdout: str,
    expected_total: int,
    paper_ids: list[int],
    message: str,
) -> None:
    def fake_runner(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    with pytest.raises(benchmark_evaluate.BenchmarkError, match=message):
        benchmark_evaluate.load_rubric_counts(
            tmp_path / "upstream",
            dataset_id="owner/dataset",
            dataset_revision="revision-1",
            paper_ids=paper_ids,
            expected_total=expected_total,
            runner=fake_runner,
        )


def test_rubric_count_failure_precedes_evaluation_view_and_judges(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evaluations_root = tmp_path / "evaluations"
    upstream = {
        "repository": "https://example.invalid/upstream",
        "commit": "commit-1",
        "archive_sha256": "archive-digest",
    }
    lock = {
        "dataset": {"id": "owner/dataset", "revision": "revision-1", "rubric_items": 2},
        "upstream": upstream,
        "precision": {"container_image": "precision:test"},
        "defaults": {
            "similarity_model": "similarity-model",
            "judge_model": "precision-model",
            "recall_concurrency": 2,
            "recall_temperature": 0.0,
        },
    }
    run_manifest = {
        "run_id": "benchmark-run",
        "frozen_inputs": {
            "dataset": {"id": "owner/dataset", "revision": "revision-1"},
            "upstream": upstream,
        },
    }
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    calls = []

    def fake_runner(command, **kwargs):
        calls.append("rubric")
        return subprocess.CompletedProcess(command, 0, stdout='{"2": 2}', stderr="")

    monkeypatch.setattr(benchmark_evaluate, "load_lock", lambda: lock)
    monkeypatch.setattr(benchmark_evaluate, "load_run_manifest", lambda path: run_manifest)
    monkeypatch.setattr(
        benchmark_evaluate,
        "collect_exports",
        lambda *args, **kwargs: (exports, _selection_stats()),
    )
    monkeypatch.setattr(
        benchmark_evaluate,
        "DatasetClient",
        lambda *args: type("Client", (), {"assert_current_revision": lambda self: None})(),
    )
    monkeypatch.setattr(benchmark_evaluate, "prepare_upstream", lambda *args: tmp_path / "upstream")
    monkeypatch.setattr(benchmark_evaluate, "resolve_precision_image", lambda image: PRECISION_IMAGE_ID)
    monkeypatch.setattr(
        benchmark_evaluate,
        "prepare_evaluation_view",
        lambda *args, **kwargs: pytest.fail("evaluation view must not be created"),
    )

    with pytest.raises(benchmark_evaluate.BenchmarkError, match="paper1"):
        benchmark_evaluate.evaluate_benchmark(
            run_dir=run_dir,
            finding_mode="all",
            cache_root=tmp_path / "cache",
            evaluations_root=evaluations_root,
            runner=fake_runner,
        )

    assert calls == ["rubric"]
    assert not evaluations_root.exists()


@pytest.mark.parametrize("temperature", [float("nan"), float("inf"), float("-inf")])
def test_evaluate_rejects_non_finite_temperature(tmp_path, monkeypatch, temperature) -> None:
    monkeypatch.setattr(
        benchmark_evaluate,
        "load_lock",
        lambda: {
            "defaults": {
                "similarity_model": "similarity-model",
                "judge_model": "judge-model",
                "recall_concurrency": 1,
                "recall_temperature": 0.0,
            }
        },
    )

    with pytest.raises(benchmark_evaluate.BenchmarkError, match="temperature must be finite"):
        benchmark_evaluate.evaluate_benchmark(
            run_dir=tmp_path / "run",
            finding_mode="all",
            temperature=temperature,
        )


def test_invalid_component_cache_files_are_removed_for_retry(tmp_path) -> None:
    frozen = {
        "model_slug": "scriptorium_per_role_5",
        "similarity_model": "provider/similarity-model",
        "judge_model": "provider/precision-model",
        "paper_ids": [1],
    }
    recall = (
        tmp_path / "reviewer_scriptorium_per_role_5_similarity_similarity-model_recall_cache" / "paper1" / "recall.json"
    )
    precision = (
        tmp_path
        / "precision-work"
        / "reviewer_scriptorium_per_role_5_meta_reviewer_precision-model_precision_trajectories"
        / "paper1"
        / "prediction.json"
    )
    for path in (recall, precision):
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")

    removed_recall = benchmark_evaluate.invalidate_component_caches(tmp_path, frozen, "recall")
    removed_precision = benchmark_evaluate.invalidate_component_caches(tmp_path, frozen, "precision")

    assert removed_recall == [recall.relative_to(tmp_path).as_posix()]
    assert removed_precision == [precision.relative_to(tmp_path).as_posix()]
    assert not recall.exists()
    assert not precision.exists()


def test_component_cache_invalidation_is_limited_to_bad_papers(tmp_path) -> None:
    frozen = {
        "model_slug": "scriptorium_per_role_5",
        "similarity_model": "provider/similarity-model",
        "judge_model": "provider/precision-model",
        "paper_ids": [1, 2],
    }
    root = (
        tmp_path
        / "precision-work"
        / "reviewer_scriptorium_per_role_5_meta_reviewer_precision-model_precision_trajectories"
    )
    predictions = [root / f"paper{paper_id}" / "prediction.json" for paper_id in frozen["paper_ids"]]
    for path in predictions:
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")

    removed = benchmark_evaluate.invalidate_component_caches(
        tmp_path,
        frozen,
        "precision",
        [2],
    )

    assert removed == [predictions[1].relative_to(tmp_path).as_posix()]
    assert predictions[0].is_file()
    assert not predictions[1].exists()


def test_component_path_validation_rejects_nested_symlink(tmp_path) -> None:
    evaluation_dir = tmp_path / "evaluation"
    evaluation_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (evaluation_dir / "precision-work").symlink_to(external, target_is_directory=True)
    frozen = {
        "model_slug": "scriptorium_all",
        "similarity_model": "similarity-model",
        "judge_model": "precision-model",
        "paper_ids": [1],
    }

    with pytest.raises(benchmark_evaluate.BenchmarkError, match="contains a symlink"):
        benchmark_evaluate._validate_component_paths(evaluation_dir, frozen)


def test_component_path_validation_rejects_runner_output_symlink(tmp_path) -> None:
    evaluation_dir = tmp_path / "evaluation"
    evaluation_dir.mkdir()
    external = tmp_path / "external-recall.json"
    external.write_text("{}\n", encoding="utf-8")
    (evaluation_dir / "recall.json").symlink_to(external)
    frozen = {
        "model_slug": "scriptorium_all",
        "similarity_model": "similarity-model",
        "judge_model": "precision-model",
        "paper_ids": [1],
    }

    with pytest.raises(benchmark_evaluate.BenchmarkError, match="contains a symlink"):
        benchmark_evaluate._validate_component_paths(evaluation_dir, frozen)


def test_evaluation_manifest_validation_detects_component_mutation(tmp_path) -> None:
    evaluation_dir = tmp_path / "evaluation"
    evaluation_dir.mkdir()
    manifest_path = evaluation_dir / "evaluation_manifest.json"
    expected = {"benchmark": "peerreviewbench", "frozen_inputs": {"paper_ids": [1]}}
    manifest_path.write_text(json.dumps(expected), encoding="utf-8")

    benchmark_evaluate._validate_evaluation_manifest(evaluation_dir, expected)

    manifest_path.write_text('{"benchmark": "changed"}\n', encoding="utf-8")
    with pytest.raises(benchmark_evaluate.BenchmarkError, match="changed during evaluation"):
        benchmark_evaluate._validate_evaluation_manifest(evaluation_dir, expected)


def test_missing_component_executable_returns_nonzero_and_writes_log(tmp_path) -> None:
    def missing_runner(command, **kwargs):
        raise OSError("missing evaluator")

    code = benchmark_evaluate.run_component(
        "recall",
        ["python", "missing.py"],
        tmp_path,
        missing_runner,
    )

    assert code == 127
    assert "missing evaluator" in (tmp_path / "recall.log").read_text(encoding="utf-8")


def test_component_output_validation_reports_missing_and_errored_judgments() -> None:
    exports = {
        1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)]),
        2: benchmark_evaluate.export_findings([_finding(AgentRole.COPYEDIT, 2)]),
    }
    recall = {
        "per_paper": [
            {
                "paper_id": 1,
                "pair_details": [
                    {
                        "rubric_idx": 0,
                        "ai_item_number": 1,
                        "parsed_binary": None,
                        "error": "judge failed",
                    }
                ],
            }
        ]
    }
    precision = {
        "n_items": 1,
        "per_item": [
            {
                "paper_id": 1,
                "item_number": 1,
                "correctness": None,
            }
        ],
    }

    errors = benchmark_evaluate.validate_component_outputs(recall, precision, exports, {"1": 1, "2": 1})

    assert any("recall papers differ" in error for error in errors)
    assert any("precision papers differ" in error for error in errors)
    assert any("precision item count differs" in error for error in errors)
    assert any("recall judge error" in error for error in errors)
    assert any("precision judge labels are invalid" in error for error in errors)


def test_component_output_validation_rejects_partial_pairs_and_invalid_labels() -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    recall, precision = _valid_component_outputs()

    assert benchmark_evaluate.validate_component_outputs(recall, precision, exports, {"1": 2}) == []

    recall["per_paper"][0]["pair_details"].pop()
    precision["per_item"][0]["evidence"] = None
    errors = benchmark_evaluate.validate_component_outputs(recall, precision, exports, {"1": 2})

    assert any("pair identities are incomplete" in error for error in errors)
    assert any("precision judge labels are invalid" in error for error in errors)


@pytest.mark.parametrize("component", ["recall", "precision"])
@pytest.mark.parametrize("elapsed_seconds", [float("nan"), float("inf"), -0.1, True, "1"])
def test_component_output_validation_rejects_invalid_timing(
    component: str,
    elapsed_seconds: object,
) -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    recall, precision = _valid_component_outputs()
    payload = recall if component == "recall" else precision
    payload["elapsed_seconds"] = elapsed_seconds

    errors = benchmark_evaluate.validate_component_outputs(recall, precision, exports, {"1": 2})

    assert f"{component} elapsed_seconds must be a finite non-negative number" in errors


def test_component_output_validation_allows_missing_timing() -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    recall, precision = _valid_component_outputs()
    recall.pop("elapsed_seconds")
    precision.pop("elapsed_seconds")

    assert benchmark_evaluate.validate_component_outputs(recall, precision, exports, {"1": 2}) == []


def test_recall_validation_rejects_self_consistent_truncated_rubric() -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    recall, _ = _valid_component_outputs()
    row = recall["per_paper"][0]
    row["n_rubric"] = 1
    row["n_pairs_scored"] = 1
    row["n_covered"] = 1
    row["recall"] = 1.0
    row["pair_details"] = row["pair_details"][:1]
    recall["total_rubric_items"] = 1
    recall["total_covered"] = 1
    recall["overall_recall"] = 1.0
    invalid_papers: set[int] = set()

    errors = benchmark_evaluate._validate_recall_output(recall, exports, {"1": 2}, invalid_papers)

    assert any("rubric count differs for paper1" in error for error in errors)
    assert any("pair identities are incomplete for paper1" in error for error in errors)
    assert any("rubric total differs: expected 2, found 1" in error for error in errors)
    assert invalid_papers == {1}


@pytest.mark.parametrize("field", ["paper_id", "item_number"])
@pytest.mark.parametrize("value", [True, 1.0, 1.9, "1"])
def test_precision_validation_rejects_non_integer_identities(field: str, value: object) -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    _, precision = _valid_component_outputs()
    precision["per_item"][0][field] = value

    errors = benchmark_evaluate._validate_precision_output(precision, exports)

    assert any("without a valid paper_id or item_number" in error for error in errors)


@pytest.mark.parametrize("field", ["paper_id", "rubric_idx", "ai_item_number"])
@pytest.mark.parametrize("value", [True, 1.0, 1.9, "1"])
def test_recall_validation_rejects_non_integer_identities(field: str, value: object) -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    recall, _ = _valid_component_outputs()
    if field == "paper_id":
        recall["per_paper"][0][field] = value
        expected_error = "without a valid paper_id"
    else:
        recall["per_paper"][0]["pair_details"][0][field] = value
        expected_error = "pair without valid identity"

    errors = benchmark_evaluate._validate_recall_output(recall, exports, {"1": 2})

    assert any(expected_error in error for error in errors)


def test_precision_validation_identifies_only_the_bad_paper() -> None:
    exports = {
        1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)]),
        2: benchmark_evaluate.export_findings([_finding(AgentRole.COPYEDIT, 2)]),
    }
    precision = {
        "n_papers": 2,
        "n_items": 2,
        "n_fully_good": 1,
        "precision": 0.5,
        "per_item": [
            {
                "paper_id": 1,
                "item_number": 1,
                "correctness": "Correct",
                "significance": "Significant",
                "evidence": "Sufficient",
                "is_fully_good": True,
            },
            {
                "paper_id": 2,
                "item_number": 1,
                "correctness": None,
                "significance": None,
                "evidence": None,
                "is_fully_good": False,
            },
        ],
    }
    invalid_papers: set[int] = set()

    errors = benchmark_evaluate._validate_precision_output(
        precision,
        exports,
        invalid_papers,
    )

    assert any("paper2 item 1" in error for error in errors)
    assert invalid_papers == {2}


def test_recall_validation_identifies_only_the_bad_paper() -> None:
    exports = {
        1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)]),
        2: benchmark_evaluate.export_findings([_finding(AgentRole.COPYEDIT, 2)]),
    }
    recall = {
        "n_papers": 2,
        "total_rubric_items": 2,
        "total_covered": 1,
        "overall_recall": 0.5,
        "per_paper": [
            {
                "paper_id": 1,
                "n_rubric": 1,
                "n_ai": 1,
                "n_pairs_scored": 1,
                "n_covered": 1,
                "recall": 1.0,
                "pair_details": [
                    {
                        "rubric_idx": 0,
                        "ai_item_number": 1,
                        "parsed_binary": "similar",
                        "is_similar": True,
                        "error": None,
                    }
                ],
            },
            {
                "paper_id": 2,
                "n_rubric": 1,
                "n_ai": 1,
                "n_pairs_scored": 1,
                "n_covered": 0,
                "recall": 0.0,
                "pair_details": [
                    {
                        "rubric_idx": 0,
                        "ai_item_number": 1,
                        "parsed_binary": None,
                        "is_similar": False,
                        "error": "judge failed",
                    }
                ],
            },
        ],
    }
    invalid_papers: set[int] = set()

    errors = benchmark_evaluate._validate_recall_output(
        recall,
        exports,
        {"1": 1, "2": 1},
        invalid_papers,
    )

    assert any("judge error for paper2" in error for error in errors)
    assert invalid_papers == {2}


@pytest.mark.parametrize("paper_recall", [float("nan"), float("inf"), float("-inf")])
def test_recall_validation_rejects_non_finite_per_paper_score(paper_recall: float) -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    recall, _ = _valid_component_outputs()
    recall["per_paper"][0]["recall"] = paper_recall
    invalid_papers: set[int] = set()

    errors = benchmark_evaluate._validate_recall_output(recall, exports, {"1": 2}, invalid_papers)

    assert any("recall score is inconsistent for paper1" in error for error in errors)
    assert invalid_papers == {1}


def test_complete_summary_reuse_requires_unchanged_component_outputs(tmp_path) -> None:
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    recall, precision = _valid_component_outputs()
    recall_path = tmp_path / "recall.json"
    precision_path = tmp_path / "precision.json"
    recall_path.write_text(json.dumps(recall), encoding="utf-8")
    precision_path.write_text(json.dumps(precision), encoding="utf-8")
    selection = _selection_stats()
    frozen_inputs = {
        "finding_mode": "all",
        "model_slug": "scriptorium_all",
        "paper_ids": [1],
        "rubric_counts": {"1": 2},
        "similarity_model": "similarity-model",
        "judge_model": "precision-model",
    }
    evaluation_manifest = {
        "frozen_inputs": frozen_inputs,
        "selection": selection,
        "package_versions": {"python": "3.12"},
    }
    run_manifest = {"reviewer_cost_usd": 1.25}
    summary = {
        "benchmark": "peerreviewbench",
        "status": "complete",
        "created_at": "2026-07-27T00:00:00+00:00",
        "evaluation_manifest_digest": benchmark_evaluate.json_digest(evaluation_manifest),
        "finding_mode": "all",
        "model_slug": "scriptorium_all",
        "paper_ids": [1],
        "selection": selection,
        "judges": {
            "similarity_model": "similarity-model",
            "precision_model": "precision-model",
        },
        "metrics": {
            "recall": 0.5,
            "precision": 1.0,
            "f1": 2 / 3,
            "axes": benchmark_evaluate.axis_breakdown(precision),
            "by_role": benchmark_evaluate.derive_role_metrics(
                recall,
                precision,
                exports,
                selection,
            ),
        },
        "costs": {
            "reviewer_estimated_usd": 1.25,
            "judge_estimated_usd": None,
            "judge_cost_note": "Pinned upstream components do not report judge cost.",
        },
        "timing": {
            "wrapper_elapsed_seconds": 1.0,
            "recall_elapsed_seconds": 1.25,
            "precision_elapsed_seconds": 2.5,
        },
        "component_exit_codes": {"recall": 0, "precision": 0},
        "invalidated_component_caches": {},
        "component_output_digests": {
            "recall": benchmark_evaluate.file_digest(recall_path),
            "precision": benchmark_evaluate.file_digest(precision_path),
        },
        "errors": [],
        "package_versions": {"python": "3.12"},
    }

    assert benchmark_evaluate.complete_summary_is_reusable(
        summary,
        evaluation_manifest,
        recall_path,
        precision_path,
        exports,
        run_manifest,
    )

    summary["metrics"]["axes"] = {}
    assert not benchmark_evaluate.complete_summary_is_reusable(
        summary,
        evaluation_manifest,
        recall_path,
        precision_path,
        exports,
        run_manifest,
    )
    summary["metrics"]["axes"] = benchmark_evaluate.axis_breakdown(precision)
    malformed_precision = dict(precision)
    malformed_precision["per_item"] = ["malformed"]
    precision_path.write_text(json.dumps(malformed_precision), encoding="utf-8")
    assert not benchmark_evaluate.complete_summary_is_reusable(
        summary,
        evaluation_manifest,
        recall_path,
        precision_path,
        exports,
        run_manifest,
    )

    precision_path.write_text("{}\n", encoding="utf-8")
    assert not benchmark_evaluate.complete_summary_is_reusable(
        summary,
        evaluation_manifest,
        recall_path,
        precision_path,
        exports,
        run_manifest,
    )


def test_complete_evaluation_is_reused_without_reinvoking_judges(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_BASE_URL", "")
    run_dir = tmp_path / "benchmark-run"
    run_dir.mkdir()
    evaluations_root = tmp_path / "evaluations"
    exports = {1: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])}
    selection = _selection_stats()
    lock = {
        "dataset": {"id": "owner/dataset", "revision": "revision-1", "rubric_items": 2},
        "upstream": {
            "repository": "https://example.invalid/upstream",
            "commit": "commit-1",
            "archive_sha256": "archive-digest",
        },
        "precision": {"container_image": "precision:test"},
        "defaults": {
            "similarity_model": "similarity-model",
            "judge_model": "precision-model",
            "recall_concurrency": 2,
            "recall_temperature": 0.0,
        },
    }
    run_manifest = {
        "run_id": "benchmark-run",
        "status": "complete",
        "frozen_inputs": {
            "dataset": {"id": "owner/dataset", "revision": "revision-1"},
            "upstream": lock["upstream"],
            "paper_ids": [1],
        },
        "papers": {"1": {"prepared_manifest_digest": "prepared-digest"}},
        "reviewer_cost_usd": 1.25,
    }
    component_calls: list[str] = []

    class FakeDatasetClient:
        def __init__(self, dataset_id: str, revision: str) -> None:
            assert (dataset_id, revision) == ("owner/dataset", "revision-1")

        def assert_current_revision(self) -> None:
            return None

    def fake_prepare_view(evaluation_dir, frozen_inputs, selection_stats, actual_exports, cache_root):
        del actual_exports, cache_root
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "frozen_inputs": frozen_inputs,
            "selection": selection_stats,
            "package_versions": {},
        }
        (evaluation_dir / "evaluation_manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        return manifest

    def fake_runner(command, *, check, capture_output, text, input):
        assert check is False and capture_output is True and text is True
        if command[0] == sys.executable and command[2] == benchmark_evaluate.RUBRIC_COUNTS_LAUNCHER:
            assert input is None
            component_calls.append("rubric")
            return subprocess.CompletedProcess(command, 0, stdout='{"1": 2}', stderr="")
        if command[0] == "docker":
            assert json.loads(input) == {"api_key": "test-key", "base_url": None}
            component_calls.append("precision")
        else:
            assert input is None
            component_calls.append("recall")
        recall, precision = _valid_component_outputs()
        payload = recall if any(Path(part).name == "evaluate_recall.py" for part in command) else precision
        output = (
            evaluations_root / "benchmark-run" / "all" / "precision.json"
            if command[0] == "docker"
            else Path(command[command.index("--output") + 1])
        )
        output.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(benchmark_evaluate, "load_lock", lambda: lock)
    monkeypatch.setattr(benchmark_evaluate, "load_run_manifest", lambda path: run_manifest)
    monkeypatch.setattr(
        benchmark_evaluate,
        "collect_exports",
        lambda path, manifest, finding_mode, cache_root: (exports, selection),
    )
    monkeypatch.setattr(benchmark_evaluate, "DatasetClient", FakeDatasetClient)
    monkeypatch.setattr(benchmark_evaluate, "prepare_upstream", lambda upstream, cache_root: tmp_path / "upstream")
    monkeypatch.setattr(benchmark_evaluate, "prepare_evaluation_view", fake_prepare_view)
    monkeypatch.setattr(benchmark_evaluate, "_verify_prepared_inputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(benchmark_evaluate, "_validate_evaluation_view", lambda *args, **kwargs: None)
    monkeypatch.setattr(benchmark_evaluate, "package_versions", lambda: {})
    monkeypatch.setattr(benchmark_evaluate, "resolve_precision_image", lambda image: PRECISION_IMAGE_ID)

    first_dir, first = benchmark_evaluate.evaluate_benchmark(
        run_dir=run_dir,
        finding_mode="all",
        cache_root=tmp_path / "cache",
        evaluations_root=evaluations_root,
        runner=fake_runner,
    )
    second_dir, second = benchmark_evaluate.evaluate_benchmark(
        run_dir=run_dir,
        finding_mode="all",
        cache_root=tmp_path / "cache",
        evaluations_root=evaluations_root,
        runner=fake_runner,
    )

    assert first["status"] == "complete"
    frozen = json.loads(
        (evaluations_root / "benchmark-run" / "all" / "evaluation_manifest.json").read_text(encoding="utf-8")
    )["frozen_inputs"]
    assert frozen["precision_container"] == {
        "image": "precision:test",
        "image_id": PRECISION_IMAGE_ID,
    }
    assert frozen["rubric_counts"] == {"1": 2}
    assert second == first
    assert second_dir == first_dir
    assert component_calls == ["rubric", "recall", "precision", "rubric"]


def test_evaluate_rejects_run_from_different_locked_dataset(tmp_path, monkeypatch) -> None:
    lock = {
        "dataset": {"id": "owner/dataset", "revision": "revision-1", "rubric_items": 2},
        "upstream": {
            "repository": "https://example.invalid/upstream",
            "commit": "commit-1",
            "archive_sha256": "archive-digest",
        },
        "precision": {"container_image": "precision:test"},
        "defaults": {
            "similarity_model": "similarity-model",
            "judge_model": "precision-model",
            "recall_concurrency": 2,
            "recall_temperature": 0.0,
        },
    }
    run_manifest = {
        "run_id": "benchmark-run",
        "status": "complete",
        "frozen_inputs": {
            "dataset": {"id": "owner/dataset", "revision": "different-revision"},
            "upstream": lock["upstream"],
        },
    }
    monkeypatch.setattr(benchmark_evaluate, "load_lock", lambda: lock)
    monkeypatch.setattr(benchmark_evaluate, "load_run_manifest", lambda path: run_manifest)
    monkeypatch.setattr(
        benchmark_evaluate,
        "collect_exports",
        lambda *args, **kwargs: pytest.fail("exports must not be read after pin drift"),
    )

    with pytest.raises(benchmark_evaluate.BenchmarkError, match="run pins"):
        benchmark_evaluate.evaluate_benchmark(
            run_dir=tmp_path,
            finding_mode="all",
            cache_root=tmp_path / "cache",
            evaluations_root=tmp_path / "evaluations",
        )


def test_judge_endpoint_identity_is_stable_and_secret_free(monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_BASE_URL", "https://token@example.test/proxy?key=secret")

    identity = benchmark_evaluate.judge_endpoint_identity()

    assert identity["scheme"] == "https"
    assert identity["host"] == "example.test"
    assert "token" not in json.dumps(identity)
    assert "secret" not in json.dumps(identity)


def test_role_metrics_attribute_items_and_compute_f1() -> None:
    exports = {
        1: benchmark_evaluate.export_findings(
            [
                _finding(AgentRole.SUBSTANTIVE_REVIEW, 1),
                _finding(AgentRole.COPYEDIT, 2),
            ]
        ),
        2: benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 3)]),
    }
    recall = {
        "total_rubric_items": 4,
        "per_paper": [
            {
                "paper_id": 1,
                "pair_details": [
                    {"rubric_idx": 0, "ai_item_number": 1, "is_similar": True},
                    {"rubric_idx": 1, "ai_item_number": 2, "is_similar": True},
                ],
            },
            {
                "paper_id": 2,
                "pair_details": [
                    {"rubric_idx": 2, "ai_item_number": 1, "is_similar": True},
                ],
            },
        ],
    }
    precision = {
        "per_item": [
            {"paper_id": 1, "item_number": 1, "is_fully_good": True},
            {"paper_id": 1, "item_number": 2, "is_fully_good": True},
            {"paper_id": 2, "item_number": 1, "is_fully_good": False},
        ]
    }
    selection = _selection_stats(raw=3, selected=3)
    selection["total"]["by_role"] = {
        "substantive_review": {"raw": 2, "selected": 2, "dropped": 0},
        "copyedit": {"raw": 1, "selected": 1, "dropped": 0},
        "consistency": {"raw": 0, "selected": 0, "dropped": 0},
        "figure_review": {"raw": 0, "selected": 0, "dropped": 0},
    }

    metrics = benchmark_evaluate.derive_role_metrics(recall, precision, exports, selection)

    assert metrics["substantive_review"]["precision"] == pytest.approx(0.5)
    assert metrics["substantive_review"]["recall"] == pytest.approx(0.5)
    assert metrics["substantive_review"]["f1"] == pytest.approx(0.5)
    assert metrics["copyedit"]["precision"] == pytest.approx(1.0)
    assert metrics["copyedit"]["recall"] == pytest.approx(0.25)
    assert metrics["copyedit"]["f1"] == pytest.approx(0.4)
    assert metrics["consistency"]["f1"] == 0.0


def test_evaluate_summary_marks_judge_errors_incomplete_and_keeps_f1(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evaluations_root = tmp_path / "evaluations"
    export = benchmark_evaluate.export_findings([_finding(AgentRole.SUBSTANTIVE_REVIEW, 1)])
    exports = {1: export}
    selection = _selection_stats()
    run_manifest = {
        "run_id": "benchmark-run",
        "status": "complete",
        "frozen_inputs": {
            "dataset": {"id": "owner/dataset", "revision": "revision-1"},
            "upstream": {
                "repository": "https://example.invalid/upstream",
                "commit": "commit-1",
                "archive_sha256": "archive-digest",
            },
            "paper_ids": [1],
        },
        "papers": {"1": {"prepared_manifest_digest": "prepared-digest"}},
        "reviewer_cost_usd": 1.25,
    }
    lock = {
        "dataset": {"id": "owner/dataset", "revision": "revision-1", "rubric_items": 2},
        "upstream": {
            "repository": "https://example.invalid/upstream",
            "commit": "commit-1",
            "archive_sha256": "archive-digest",
        },
        "precision": {"container_image": "precision:test"},
        "defaults": {
            "similarity_model": "similarity-model",
            "judge_model": "precision-model",
            "recall_concurrency": 2,
            "recall_temperature": 0.0,
        },
    }

    class FakeDatasetClient:
        def __init__(self, dataset_id: str, revision: str) -> None:
            assert (dataset_id, revision) == ("owner/dataset", "revision-1")

        def assert_current_revision(self) -> None:
            return None

    empty_outputs = {"enabled": False}

    def fake_prepare_view(evaluation_dir, frozen_inputs, selection_stats, actual_exports, cache_root):
        del actual_exports, cache_root
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "frozen_inputs": frozen_inputs,
            "selection": selection_stats,
            "package_versions": {},
        }
        (evaluation_dir / "evaluation_manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        return manifest

    def fake_runner(command, *, check, capture_output, text, input):
        assert check is False
        assert capture_output is True
        assert text is True
        if command[0] == sys.executable and command[2] == benchmark_evaluate.RUBRIC_COUNTS_LAUNCHER:
            assert input is None
            return subprocess.CompletedProcess(command, 0, stdout='{"1": 2}', stderr="")
        if command[0] == "docker":
            assert json.loads(input) == {"api_key": "test-key", "base_url": None}
        else:
            assert input is None
        output = (
            evaluations_root / "benchmark-run" / "all" / "precision.json"
            if command[0] == "docker"
            else Path(command[command.index("--output") + 1])
        )
        if empty_outputs["enabled"]:
            payload = {}
        elif any(Path(part).name == "evaluate_recall.py" for part in command):
            payload = {
                "overall_recall": 0.5,
                "total_rubric_items": 2,
                "per_paper": [
                    {
                        "paper_id": 1,
                        "pair_details": [
                            {
                                "rubric_idx": 0,
                                "ai_item_number": 1,
                                "is_similar": True,
                                "parsed_binary": 1,
                                "error": None,
                            }
                        ],
                    }
                ],
            }
        else:
            payload = {
                "precision": 0.8,
                "n_items": 1,
                "per_item": [
                    {
                        "paper_id": 1,
                        "item_number": 1,
                        "correctness": None,
                        "significance": "OK",
                        "evidence": "OK",
                        "is_fully_good": False,
                    }
                ],
            }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(benchmark_evaluate, "load_lock", lambda: lock)
    monkeypatch.setattr(benchmark_evaluate, "load_run_manifest", lambda path: run_manifest)
    monkeypatch.setattr(
        benchmark_evaluate,
        "collect_exports",
        lambda path, manifest, finding_mode, cache_root: (exports, selection),
    )
    monkeypatch.setattr(benchmark_evaluate, "DatasetClient", FakeDatasetClient)
    monkeypatch.setattr(benchmark_evaluate, "prepare_upstream", lambda upstream, cache_root: tmp_path / "upstream")
    monkeypatch.setattr(benchmark_evaluate, "prepare_evaluation_view", fake_prepare_view)
    monkeypatch.setattr(benchmark_evaluate, "_verify_prepared_inputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(benchmark_evaluate, "package_versions", lambda: {})
    monkeypatch.setattr(benchmark_evaluate, "resolve_precision_image", lambda image: PRECISION_IMAGE_ID)

    _, summary = benchmark_evaluate.evaluate_benchmark(
        run_dir=run_dir,
        finding_mode="all",
        cache_root=tmp_path / "cache",
        evaluations_root=evaluations_root,
        runner=fake_runner,
    )

    assert summary["status"] == "incomplete"
    assert summary["metrics"]["recall"] == pytest.approx(0.5)
    assert summary["metrics"]["precision"] == pytest.approx(0.8)
    assert summary["metrics"]["f1"] == pytest.approx(2 * 0.5 * 0.8 / 1.3)
    assert any("precision judge labels are invalid" in error for error in summary["errors"])
    assert json.loads((evaluations_root / "benchmark-run" / "all" / "summary.json").read_text()) == summary

    empty_outputs["enabled"] = True
    _, empty_summary = benchmark_evaluate.evaluate_benchmark(
        run_dir=run_dir,
        finding_mode="all",
        cache_root=tmp_path / "cache",
        evaluations_root=evaluations_root,
        runner=fake_runner,
    )

    assert empty_summary["status"] == "incomplete"
    assert empty_summary["metrics"]["recall"] is None
    assert empty_summary["metrics"]["precision"] is None
    assert any("recall per_paper is missing" in error for error in empty_summary["errors"])
    assert any("precision per_item is missing" in error for error in empty_summary["errors"])


def test_malformed_component_rows_are_reported_instead_of_raising() -> None:
    exports = {1: [{"item_number": 1}]}

    errors = benchmark_evaluate.validate_component_outputs(
        {"per_paper": [{}]},
        {"n_items": "invalid", "per_item": [{}]},
        exports,
        {"1": 1},
    )

    assert any("valid paper_id" in error for error in errors)
    assert any("item identities differ" in error for error in errors)
    assert any("item count differs" in error for error in errors)


def test_non_finite_component_timing_invalidates_all_selected_caches(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evaluations_root = tmp_path / "evaluations"
    exports = {
        paper_id: [
            {
                "item_number": 1,
                "scriptorium": {"role": "substantive_review"},
            }
        ]
        for paper_id in (1, 2)
    }
    selection = _selection_stats(raw=2, selected=2)
    run_manifest = {
        "run_id": "benchmark-run",
        "status": "complete",
        "frozen_inputs": {
            "dataset": {"id": "owner/dataset", "revision": "revision-1"},
            "upstream": {
                "repository": "https://example.invalid/upstream",
                "commit": "commit-1",
                "archive_sha256": "archive-digest",
            },
            "paper_ids": [1, 2],
        },
        "papers": {
            "1": {"prepared_manifest_digest": "prepared-1"},
            "2": {"prepared_manifest_digest": "prepared-2"},
        },
        "reviewer_cost_usd": 1.25,
    }
    lock = {
        "dataset": {"id": "owner/dataset", "revision": "revision-1", "rubric_items": 2},
        "upstream": {
            "repository": "https://example.invalid/upstream",
            "commit": "commit-1",
            "archive_sha256": "archive-digest",
        },
        "precision": {"container_image": "precision:test"},
        "defaults": {
            "similarity_model": "similarity-model",
            "judge_model": "precision-model",
            "recall_concurrency": 2,
            "recall_temperature": 0.0,
        },
    }

    def fake_prepare_view(evaluation_dir, frozen_inputs, selection_stats, actual_exports, cache_root):
        del actual_exports, cache_root
        evaluation_dir.mkdir(parents=True, exist_ok=True)
        (evaluation_dir / "papers").mkdir()
        for paper_id in frozen_inputs["paper_ids"]:
            (evaluation_dir / "papers" / f"paper{paper_id}").mkdir()
        precision_root = benchmark_evaluate._component_cache_root(evaluation_dir, frozen_inputs, "precision")
        for paper_id in frozen_inputs["paper_ids"]:
            prediction = precision_root / f"paper{paper_id}" / "prediction.json"
            prediction.parent.mkdir(parents=True)
            prediction.write_text("cached", encoding="utf-8")
        manifest = {
            "frozen_inputs": frozen_inputs,
            "selection": selection_stats,
            "package_versions": {},
        }
        (evaluation_dir / "evaluation_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    recall = {
        "n_papers": 2,
        "total_rubric_items": 2,
        "total_covered": 2,
        "overall_recall": 1.0,
        "per_paper": [
            {
                "paper_id": paper_id,
                "n_rubric": 1,
                "n_ai": 1,
                "n_pairs_scored": 1,
                "n_covered": 1,
                "recall": 1.0,
                "pair_details": [
                    {
                        "rubric_idx": 0,
                        "ai_item_number": 1,
                        "parsed_binary": "similar",
                        "is_similar": True,
                        "error": None,
                    }
                ],
            }
            for paper_id in (1, 2)
        ],
    }
    precision = {
        "elapsed_seconds": float("nan"),
        "n_papers": 2,
        "n_items": 2,
        "n_fully_good": 2,
        "precision": 1.0,
        "per_item": [
            {
                "paper_id": paper_id,
                "item_number": 1,
                "correctness": "Correct",
                "significance": "Significant",
                "evidence": "Sufficient",
                "is_fully_good": True,
            }
            for paper_id in (1, 2)
        ],
    }

    def fake_run_component(name, command, output_dir, runner=subprocess.run, input_text=None):
        del command, runner, input_text
        (output_dir / f"{name}.json").write_text(json.dumps(recall if name == "recall" else precision))
        return 0

    monkeypatch.setattr(benchmark_evaluate, "load_lock", lambda: lock)
    monkeypatch.setattr(benchmark_evaluate, "load_run_manifest", lambda path: run_manifest)
    monkeypatch.setattr(
        benchmark_evaluate,
        "collect_exports",
        lambda path, manifest, finding_mode, cache_root: (exports, selection),
    )
    monkeypatch.setattr(
        benchmark_evaluate,
        "DatasetClient",
        lambda dataset_id, revision: type("Client", (), {"assert_current_revision": lambda self: None})(),
    )
    monkeypatch.setattr(benchmark_evaluate, "prepare_upstream", lambda upstream, cache_root: tmp_path / "upstream")
    monkeypatch.setattr(
        benchmark_evaluate,
        "load_rubric_counts",
        lambda *args, **kwargs: {"1": 1, "2": 1},
    )
    monkeypatch.setattr(benchmark_evaluate, "prepare_evaluation_view", fake_prepare_view)
    monkeypatch.setattr(benchmark_evaluate, "_validate_evaluation_view", lambda *args: None)
    monkeypatch.setattr(benchmark_evaluate, "_verify_prepared_inputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(benchmark_evaluate, "package_versions", lambda: {})
    monkeypatch.setattr(benchmark_evaluate, "resolve_precision_image", lambda image: PRECISION_IMAGE_ID)
    monkeypatch.setattr(
        benchmark_evaluate, "build_component_commands", lambda *args, **kwargs: (["recall"], ["precision"])
    )
    monkeypatch.setattr(benchmark_evaluate, "run_component", fake_run_component)

    evaluation_dir, summary = benchmark_evaluate.evaluate_benchmark(
        run_dir=run_dir,
        finding_mode="all",
        cache_root=tmp_path / "cache",
        evaluations_root=evaluations_root,
    )

    precision_root = (
        evaluation_dir
        / "precision-work"
        / "reviewer_scriptorium_all_meta_reviewer_precision-model_precision_trajectories"
    )
    cache_prefix = "precision-work/reviewer_scriptorium_all_meta_reviewer_precision-model_precision_trajectories"
    assert summary["status"] == "incomplete"
    assert "precision elapsed_seconds must be a finite non-negative number" in summary["errors"]
    assert summary["timing"]["precision_elapsed_seconds"] is None
    assert summary["invalidated_component_caches"]["precision"] == [
        f"{cache_prefix}/paper{paper_id}/prediction.json" for paper_id in (1, 2)
    ]
    assert all(not (precision_root / f"paper{paper_id}" / "prediction.json").exists() for paper_id in (1, 2))
    summary_text = (evaluation_dir / "summary.json").read_text(encoding="utf-8")
    assert "NaN" not in summary_text
    assert json.loads(summary_text) == summary
