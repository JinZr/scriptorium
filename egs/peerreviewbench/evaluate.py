#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
import uuid

from scriptorium.domain import Finding
from scriptorium.service import ScriptoriumService

try:
    from .prepare import (
        DEFAULT_CACHE_ROOT,
        BenchmarkError,
        DatasetClient,
        file_digest,
        json_digest,
        load_lock,
        prepare_upstream,
        safe_cache_directory,
        safe_relative_path,
        validate_prepared_paper,
        write_json_atomic,
    )
    from .run import (
        PeerReviewBenchManuscriptManager,
        package_versions,
        paper_project_path,
        repository_commit,
        source_manifest,
        validate_benchmark_manifest,
        validate_completed_scriptorium_state,
        validate_paper_project,
    )
except ImportError:  # Direct script execution.
    from prepare import (
        DEFAULT_CACHE_ROOT,
        BenchmarkError,
        DatasetClient,
        file_digest,
        json_digest,
        load_lock,
        prepare_upstream,
        safe_cache_directory,
        safe_relative_path,
        validate_prepared_paper,
        write_json_atomic,
    )
    from run import (
        PeerReviewBenchManuscriptManager,
        package_versions,
        paper_project_path,
        repository_commit,
        source_manifest,
        validate_benchmark_manifest,
        validate_completed_scriptorium_state,
        validate_paper_project,
    )

HERE = Path(__file__).resolve().parent
DEFAULT_EVALUATIONS_ROOT = HERE / "evaluations"
ROLE_ORDER = ("substantive_review", "copyedit", "consistency", "figure_review")
FINDING_MODES = ("all", "per-role-5")
ComponentRunner = Callable[..., subprocess.CompletedProcess[str]]
PINNED_DATASET_LAUNCHER = (
    "import os, runpy, sys\n"
    "os.environ.setdefault('HF_HUB_DISABLE_XET', '1')\n"
    "os.environ.setdefault('HF_HUB_ENABLE_HF_TRANSFER', '0')\n"
    "os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '120')\n"
    "os.environ.setdefault('HF_HUB_ETAG_TIMEOUT', '120')\n"
    "import datasets\n"
    "dataset_id, revision, script = sys.argv[1:4]\n"
    "script_args = sys.argv[4:]\n"
    "load_dataset = datasets.load_dataset\n"
    "def pinned_load_dataset(path, *args, **kwargs):\n"
    "    if path == dataset_id:\n"
    "        requested = kwargs.get('revision')\n"
    "        if requested not in (None, revision):\n"
    "            raise RuntimeError('dataset revision conflicts with benchmark lock')\n"
    "        kwargs['revision'] = revision\n"
    "    return load_dataset(path, *args, **kwargs)\n"
    "datasets.load_dataset = pinned_load_dataset\n"
    "sys.argv = [script, *script_args]\n"
    "runpy.run_path(script, run_name='__main__')\n"
)
PRECISION_CONTAINER_LAUNCHER = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "credentials = json.loads(sys.stdin.readline())\n"
    "api_key = credentials.get('api_key')\n"
    "base_url = credentials.get('base_url')\n"
    "if not isinstance(api_key, str) or not api_key:\n"
    "    raise RuntimeError('precision judge API key is missing')\n"
    "if base_url is not None and (not isinstance(base_url, str) or not base_url):\n"
    "    raise RuntimeError('precision judge base URL is invalid')\n"
    "dataset_id, revision, script = sys.argv[1:4]\n"
    "script_args = sys.argv[4:]\n"
    "import datasets\n"
    "load_dataset = datasets.load_dataset\n"
    "def pinned_load_dataset(path, *args, **kwargs):\n"
    "    if path == dataset_id:\n"
    "        requested = kwargs.get('revision')\n"
    "        if requested not in (None, revision):\n"
    "            raise RuntimeError('dataset revision conflicts with benchmark lock')\n"
    "        kwargs['revision'] = revision\n"
    "    return load_dataset(path, *args, **kwargs)\n"
    "datasets.load_dataset = pinned_load_dataset\n"
    "source = Path(script).read_text(encoding='utf-8')\n"
    "replacements = {\n"
    "    \"    api_key = os.environ.get('LITELLM_API_KEY')\\n\": "
    '"    api_key = __scriptorium_api_key\\n",\n'
    "    \"    base_url = os.environ.get('LITELLM_BASE_URL')\\n\": "
    '"    base_url = __scriptorium_base_url\\n",\n'
    "}\n"
    "for needle, replacement in replacements.items():\n"
    "    if source.count(needle) != 1:\n"
    "        raise RuntimeError('pinned precision credential contract changed')\n"
    "    source = source.replace(needle, replacement)\n"
    "sys.argv = [script, *script_args]\n"
    "namespace = {\n"
    "    '__name__': '__main__',\n"
    "    '__file__': script,\n"
    "    '__scriptorium_api_key': api_key,\n"
    "    '__scriptorium_base_url': base_url,\n"
    "}\n"
    "exec(compile(source, script, 'exec'), namespace)\n"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def judge_endpoint_identity() -> dict[str, str | None]:
    base_url = os.environ.get("LITELLM_BASE_URL") or "https://cmu.litellm.ai"
    parsed = urlparse(base_url)
    return {
        "scheme": parsed.scheme.lower() or None,
        "host": parsed.hostname,
        "base_url_sha256": json_digest(base_url),
    }


def resolve_precision_image(image: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format={{.Id}}", image],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BenchmarkError("Docker is required for the sandboxed precision evaluator") from exc
    image_id = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise BenchmarkError(
            f"Precision evaluator image is unavailable: {image}. "
            "Build it with the command documented in egs/peerreviewbench/README.md."
        )
    return image_id


def load_run_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Invalid benchmark run manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise BenchmarkError(f"Invalid benchmark run manifest: {path}")
    validate_benchmark_manifest(manifest)
    if manifest["run_id"] != run_dir.name:
        raise BenchmarkError("Benchmark run ID does not match its directory")
    if manifest.get("status") != "complete":
        raise BenchmarkError("Only a complete benchmark review run can be evaluated")
    return manifest


def select_findings(findings: list[Finding], mode: str) -> tuple[list[Finding], dict[str, Any]]:
    if mode not in FINDING_MODES:
        raise BenchmarkError(f"Unknown finding mode: {mode}")
    by_role = {role: [] for role in ROLE_ORDER}
    for finding in findings:
        if finding.role.value in by_role:
            by_role[finding.role.value].append(finding)
    if mode == "all":
        selected = list(findings)
    else:
        selected = []
        for role in ROLE_ORDER:
            selected.extend(by_role[role][:5])
    selected_ids = {finding.id for finding in selected}
    counts = {
        role: {
            "raw": len(by_role[role]),
            "selected": sum(finding.id in selected_ids for finding in by_role[role]),
        }
        for role in ROLE_ORDER
    }
    for values in counts.values():
        values["dropped"] = values["raw"] - values["selected"]
    return selected, {
        "raw": len(findings),
        "selected": len(selected),
        "dropped": len(findings) - len(selected),
        "by_role": counts,
    }


def _evidence_text(finding: Finding) -> str:
    blocks = []
    for evidence in finding.evidence:
        source_path = str(evidence.get("source_path", ""))
        if source_path == "manuscript.pdf":
            location = f"manuscript.pdf page {evidence.get('page')}"
        else:
            start = evidence.get("start_line")
            end = evidence.get("end_line")
            location = f"{source_path}:{start}" if start == end else f"{source_path}:{start}-{end}"
            if evidence.get("page") is not None:
                location += f" (page {evidence['page']})"
        quote = str(evidence.get("quoted_text", "")).strip()
        blocks.append(f"[{location}] {quote}".strip())
    return "\n\n".join(blocks)


def export_findings(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    items = []
    for item_number, finding in enumerate(findings, 1):
        evidence_full = _evidence_text(finding)
        claim_full = "\n\n".join(part for part in (finding.claim.strip(), finding.explanation.strip()) if part)
        text = "\n\n".join(part for part in (claim_full, evidence_full) if part)
        if not text:
            raise BenchmarkError(f"Finding {finding.id} has no judgeable text")
        items.append(
            {
                "item_number": item_number,
                "title": finding.title,
                "main_point": finding.claim,
                "claim_full": claim_full,
                "evidence_full": evidence_full,
                "text": text,
                "scriptorium": {
                    "finding_id": finding.id,
                    "role": finding.role.value,
                    "category": finding.category,
                    "severity": finding.severity.value,
                    "confidence": finding.confidence,
                    "anchors": list(finding.evidence),
                },
            }
        )
    return items


def collect_exports(
    run_dir: Path,
    run_manifest: dict[str, Any],
    finding_mode: str,
    cache_root: Path,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    revision = run_manifest["frozen_inputs"]["dataset"]["revision"]
    exports: dict[int, list[dict[str, Any]]] = {}
    per_paper_stats: dict[str, Any] = {}
    totals = {
        "raw": 0,
        "selected": 0,
        "dropped": 0,
        "by_role": {role: {"raw": 0, "selected": 0, "dropped": 0} for role in ROLE_ORDER},
    }
    for paper_id in run_manifest["frozen_inputs"]["paper_ids"]:
        entry = run_manifest["papers"][str(paper_id)]
        project = paper_project_path(run_dir, entry)
        local_routes = project / ".scriptorium" / "config.toml"
        validate_paper_project(project, local_routes)
        if file_digest(local_routes) != run_manifest["frozen_inputs"].get("route_config_digest"):
            raise BenchmarkError(f"paper{paper_id} route configuration changed after review")
        prepared = cache_root / "dataset" / revision / f"paper{paper_id}"
        prepared_manifest = validate_prepared_paper(prepared)
        if json_digest(prepared_manifest) != entry["prepared_manifest_digest"]:
            raise BenchmarkError(f"Prepared paper changed after review: paper{paper_id}")
        manager = PeerReviewBenchManuscriptManager(project, prepared)
        with ScriptoriumService(project, manuscript_manager=manager) as service:
            validate_completed_scriptorium_state(service, entry)
            findings = service.list_findings(entry["scriptorium_run_id"])
        selected, stats = select_findings(findings, finding_mode)
        exports[int(paper_id)] = export_findings(selected)
        per_paper_stats[str(paper_id)] = stats
        for key in ("raw", "selected", "dropped"):
            totals[key] += stats[key]
        for role in ROLE_ORDER:
            for key in ("raw", "selected", "dropped"):
                totals["by_role"][role][key] += stats["by_role"][role][key]
    return exports, {"total": totals, "per_paper": per_paper_stats}


def _evaluation_frozen_inputs(
    run_dir: Path,
    run_manifest: dict[str, Any],
    exports: dict[int, list[dict[str, Any]]],
    finding_mode: str,
    similarity_model: str,
    judge_model: str,
    concurrency: int,
    temperature: float,
    precision_image: str,
    precision_image_id: str,
    lock: dict[str, Any],
) -> dict[str, Any]:
    return {
        "evaluator_commit": repository_commit(),
        "evaluator_source_manifest": source_manifest(),
        "review_run": str(run_dir.resolve()),
        "review_run_manifest_digest": json_digest(run_manifest),
        "finding_mode": finding_mode,
        "model_slug": f"scriptorium_{finding_mode.replace('-', '_')}",
        "export_digests": {str(paper_id): json_digest(items) for paper_id, items in sorted(exports.items())},
        "paper_ids": sorted(exports),
        "similarity_model": similarity_model,
        "judge_model": judge_model,
        "judge_endpoint": judge_endpoint_identity(),
        "recall_concurrency": concurrency,
        "recall_temperature": temperature,
        "precision_container": {
            "image": precision_image,
            "image_id": precision_image_id,
        },
        "dataset": {
            "id": lock["dataset"]["id"],
            "revision": lock["dataset"]["revision"],
        },
        "upstream": {
            "repository": lock["upstream"]["repository"],
            "commit": lock["upstream"]["commit"],
            "archive_sha256": lock["upstream"]["archive_sha256"],
        },
    }


def prepare_evaluation_view(
    evaluation_dir: Path,
    frozen_inputs: dict[str, Any],
    selection_stats: dict[str, Any],
    exports: dict[int, list[dict[str, Any]]],
    cache_root: Path,
) -> dict[str, Any]:
    if evaluation_dir.is_symlink() or evaluation_dir.parent.is_symlink():
        raise BenchmarkError(f"Evaluation directory is unsafe: {evaluation_dir}")
    manifest_path = evaluation_dir / "evaluation_manifest.json"
    if manifest_path.is_symlink():
        raise BenchmarkError(f"Evaluation manifest is unsafe: {manifest_path}")
    expected_manifest = {
        "benchmark": "peerreviewbench",
        "created_at": None,
        "frozen_inputs": frozen_inputs,
        "selection": selection_stats,
        "package_versions": package_versions(),
    }
    if evaluation_dir.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchmarkError(f"Existing evaluation directory has no valid manifest: {evaluation_dir}") from exc
        comparable = dict(existing)
        comparable["created_at"] = None
        if comparable != expected_manifest:
            raise BenchmarkError(f"Existing evaluation directory has different frozen inputs: {evaluation_dir}")
        _validate_evaluation_view(evaluation_dir, exports, frozen_inputs, cache_root)
        return existing

    temporary = evaluation_dir.parent / f".{evaluation_dir.name}.{uuid.uuid4().hex}.tmp"
    papers_root = temporary / "papers"
    try:
        for paper_id, items in sorted(exports.items()):
            paper = papers_root / f"paper{paper_id}"
            paper.mkdir(parents=True)
            prepared = cache_root / "dataset" / frozen_inputs["dataset"]["revision"] / f"paper{paper_id}"
            validate_prepared_paper(prepared)
            shutil.copytree(prepared / "preprint", paper / "preprint")
            reviews = paper / "reviews"
            reviews.mkdir()
            (reviews / f"review_items_{frozen_inputs['model_slug']}.json").write_text(
                json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        manifest = dict(expected_manifest)
        manifest["created_at"] = utc_now()
        (temporary / "evaluation_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        evaluation_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(evaluation_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _validate_evaluation_view(evaluation_dir, exports, frozen_inputs, cache_root)
    return manifest


def _validate_evaluation_view(
    evaluation_dir: Path,
    exports: dict[int, list[dict[str, Any]]],
    frozen_inputs: dict[str, Any],
    cache_root: Path,
) -> None:
    papers_root = evaluation_dir / "papers"
    if evaluation_dir.is_symlink() or papers_root.is_symlink() or not papers_root.is_dir():
        raise BenchmarkError(f"Evaluation directory is missing or unsafe: {evaluation_dir}")
    actual_papers = set()
    for path in papers_root.iterdir():
        if path.is_symlink() or not path.is_dir():
            raise BenchmarkError(f"Evaluation papers directory contains an unsafe entry: {path}")
        actual_papers.add(path.name)
    expected_papers = {f"paper{paper_id}" for paper_id in exports}
    if actual_papers != expected_papers:
        raise BenchmarkError("Evaluation papers directory has unexpected or missing papers")
    for paper_id, expected_items in sorted(exports.items()):
        paper = papers_root / f"paper{paper_id}"
        preprint = paper / "preprint"
        prepared = cache_root / "dataset" / frozen_inputs["dataset"]["revision"] / f"paper{paper_id}"
        prepared_manifest = validate_prepared_paper(prepared)
        if paper.is_symlink() or not paper.is_dir() or preprint.is_symlink() or not preprint.is_dir():
            raise BenchmarkError(f"Evaluation paper directory is missing or unsafe: paper{paper_id}")
        expected_paths = set()
        for ref in prepared_manifest["files"]:
            relative = safe_relative_path(ref["path"])
            expected_paths.add(relative.as_posix())
            copied = preprint / relative
            if (
                copied.is_symlink()
                or not copied.is_file()
                or copied.stat().st_size != ref["size_bytes"]
                or file_digest(copied) != ref["content_hash"]
            ):
                raise BenchmarkError(f"Evaluation preprint copy changed: {copied}")
        actual_paths = set()
        for path in preprint.rglob("*"):
            if path.is_symlink():
                raise BenchmarkError(f"Evaluation preprint copy contains a symlink: {path}")
            if path.is_file():
                actual_paths.add(path.relative_to(preprint).as_posix())
        if actual_paths != expected_paths:
            raise BenchmarkError(f"Evaluation preprint copy has unexpected or missing files: paper{paper_id}")
        reviews = paper / "reviews"
        review_path = reviews / f"review_items_{frozen_inputs['model_slug']}.json"
        if reviews.is_symlink() or not reviews.is_dir() or review_path.is_symlink():
            raise BenchmarkError(f"Invalid BYOJ export: {review_path}")
        if {path.name for path in paper.iterdir()} != {"preprint", "reviews"} or {
            path.name for path in reviews.iterdir()
        } != {review_path.name}:
            raise BenchmarkError(f"Evaluation paper contains unexpected files: paper{paper_id}")
        try:
            actual_items = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchmarkError(f"Invalid BYOJ export: {review_path}") from exc
        if actual_items != expected_items:
            raise BenchmarkError(f"BYOJ export changed: {review_path}")


def build_component_commands(
    upstream_root: Path,
    papers_root: Path,
    output_dir: Path,
    *,
    dataset_id: str,
    dataset_revision: str,
    model_slug: str,
    similarity_model: str,
    judge_model: str,
    concurrency: int,
    temperature: float,
    precision_image_id: str,
) -> tuple[list[str], list[str]]:
    evaluation = upstream_root / "peerreview_bench" / "evaluation"
    recall = [
        sys.executable,
        "-c",
        PINNED_DATASET_LAUNCHER,
        dataset_id,
        dataset_revision,
        str(evaluation / "evaluate_recall.py"),
        "--paper-root",
        str(papers_root),
        "--model-name",
        model_slug,
        "--similarity-model",
        similarity_model,
        "--concurrency",
        str(concurrency),
        "--temperature",
        str(temperature),
        "--output",
        str(output_dir / "recall.json"),
    ]
    output_dir = output_dir.resolve()
    papers_root = papers_root.resolve()
    upstream_root = upstream_root.resolve()
    precision_output = output_dir / "precision.json"
    precision_work = output_dir / "precision-work"
    precision_cache = output_dir / "precision-cache"
    precision_work.mkdir(parents=True, exist_ok=True)
    precision_cache.mkdir(parents=True, exist_ok=True)
    precision_output.touch(exist_ok=True)
    precision = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--network=bridge",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=1g",
        "--mount",
        f"type=bind,source={upstream_root},target=/upstream,readonly",
        "--mount",
        f"type=bind,source={papers_root},target=/papers,readonly",
        "--mount",
        f"type=bind,source={precision_output},target=/output/precision.json",
        "--mount",
        f"type=bind,source={precision_work},target=/output/precision-work",
        "--mount",
        f"type=bind,source={precision_cache},target=/cache",
        "--env=HF_HOME=/cache/huggingface",
        "--env=HOME=/tmp/home",
        "--env=PYTHONDONTWRITEBYTECODE=1",
    ]
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        precision.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    precision.extend(
        [
            precision_image_id,
            "python",
            "-c",
            PRECISION_CONTAINER_LAUNCHER,
            dataset_id,
            dataset_revision,
            "/upstream/peerreview_bench/evaluation/evaluate_precision.py",
            "--paper-root",
            "/papers",
            "--model-name",
            model_slug,
            "--judge-model",
            judge_model,
            "--output",
            "/output/precision.json",
            "--output-dir",
            "/output/precision-work",
        ]
    )
    return recall, precision


def run_component(
    name: str,
    command: list[str],
    output_dir: Path,
    runner: ComponentRunner = subprocess.run,
    input_text: str | None = None,
) -> int:
    print(f"Running PeerReviewBench {name} component", flush=True)
    log_path = output_dir / f"{name}.log"
    if output_dir.is_symlink() or not output_dir.is_dir() or log_path.is_symlink():
        raise BenchmarkError(f"Unsafe {name} component output path: {log_path}")
    try:
        result = runner(command, check=False, capture_output=True, text=True, input=input_text)
    except OSError as exc:
        _write_text_atomic(log_path, "$ " + " ".join(command) + f"\n\n{type(exc).__name__}: {exc}\n")
        return 127
    log = "$ " + " ".join(command) + "\n\n" + (result.stdout or "") + (result.stderr or "")
    _write_text_atomic(log_path, log)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return int(result.returncode)


def invalidate_component_caches(
    evaluation_dir: Path,
    frozen_inputs: dict[str, Any],
    component: str,
    paper_ids: Iterable[int] | None = None,
) -> list[str]:
    selected_papers = list(paper_ids if paper_ids is not None else frozen_inputs["paper_ids"])
    root = _component_cache_root(evaluation_dir, frozen_inputs, component)
    if component == "recall":
        targets = [root / f"paper{paper_id}" / "recall.json" for paper_id in selected_papers]
    elif component == "precision":
        targets = [root / f"paper{paper_id}" / "prediction.json" for paper_id in selected_papers]
    for path in (root, root.parent):
        if path != evaluation_dir and path.is_symlink():
            raise BenchmarkError(f"Unsafe {component} component cache: {path}")
    removed = []
    for target in targets:
        if target.parent.is_symlink() or target.is_symlink():
            raise BenchmarkError(f"Unsafe {component} component cache: {target}")
        if target.is_file():
            target.unlink()
            removed.append(target.relative_to(evaluation_dir).as_posix())
    return removed


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Missing or invalid component output: {path}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkError(f"Component output is not a JSON object: {path}")
    return payload


def _write_text_atomic(path: Path, text: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise BenchmarkError(f"Unsafe output path: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _component_cache_root(
    evaluation_dir: Path,
    frozen_inputs: dict[str, Any],
    component: str,
) -> Path:
    reviewer = frozen_inputs["model_slug"].split("/")[-1]
    if component == "recall":
        judge = frozen_inputs["similarity_model"].split("/")[-1]
        return evaluation_dir / f"reviewer_{reviewer}_similarity_{judge}_recall_cache"
    if component == "precision":
        judge = frozen_inputs["judge_model"].split("/")[-1]
        return evaluation_dir / "precision-work" / f"reviewer_{reviewer}_meta_reviewer_{judge}_precision_trajectories"
    raise BenchmarkError(f"Unknown component cache: {component}")


def _assert_evaluation_path(evaluation_dir: Path, path: Path) -> None:
    if evaluation_dir.is_symlink() or not evaluation_dir.is_dir():
        raise BenchmarkError(f"Evaluation directory is missing or unsafe: {evaluation_dir}")
    try:
        relative = path.relative_to(evaluation_dir)
    except ValueError as exc:
        raise BenchmarkError(f"Evaluation output path escapes its root: {path}") from exc
    if ".." in relative.parts:
        raise BenchmarkError(f"Evaluation output path escapes its root: {path}")
    current = evaluation_dir
    for index, part in enumerate(relative.parts):
        current /= part
        if current.is_symlink():
            raise BenchmarkError(f"Evaluation output path contains a symlink: {current}")
        if index < len(relative.parts) - 1 and current.exists() and not current.is_dir():
            raise BenchmarkError(f"Evaluation output path contains a non-directory: {current}")
    try:
        path.resolve(strict=False).relative_to(evaluation_dir.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise BenchmarkError(f"Evaluation output path escapes its root: {path}") from exc


def _validate_component_paths(evaluation_dir: Path, frozen_inputs: dict[str, Any]) -> None:
    paths = [
        evaluation_dir / "evaluation_manifest.json",
        evaluation_dir / "summary.json",
        evaluation_dir / "recall.json",
        evaluation_dir / "precision.json",
        evaluation_dir / "recall.log",
        evaluation_dir / "precision.log",
    ]
    for component, filename in (("recall", "recall.json"), ("precision", "prediction.json")):
        root = _component_cache_root(evaluation_dir, frozen_inputs, component)
        paths.extend(root / f"paper{paper_id}" / filename for paper_id in frozen_inputs["paper_ids"])
    for path in paths:
        _assert_evaluation_path(evaluation_dir, path)
        if path.exists() and not path.is_file():
            raise BenchmarkError(f"Evaluation output path is not a file: {path}")
    for path in (evaluation_dir / "precision-work", evaluation_dir / "precision-cache"):
        _assert_evaluation_path(evaluation_dir, path)
        if path.exists() and not path.is_dir():
            raise BenchmarkError(f"Evaluation output path is not a directory: {path}")


def _validate_evaluation_manifest(
    evaluation_dir: Path,
    expected: dict[str, Any],
) -> None:
    path = evaluation_dir / "evaluation_manifest.json"
    _assert_evaluation_path(evaluation_dir, path)
    if _load_json(path) != expected:
        raise BenchmarkError(f"Evaluation manifest changed during evaluation: {path}")


def validate_component_outputs(
    recall: dict[str, Any],
    precision: dict[str, Any],
    exports: dict[int, list[dict[str, Any]]],
) -> list[str]:
    return _validate_recall_output(recall, exports) + _validate_precision_output(precision, exports)


def _validate_recall_output(
    recall: dict[str, Any],
    exports: dict[int, list[dict[str, Any]]],
    invalid_papers: set[int] | None = None,
) -> list[str]:
    errors = []
    expected_papers = set(exports)
    recall_rows = recall.get("per_paper")
    if not isinstance(recall_rows, list):
        errors.append("recall per_paper is missing or invalid")
        recall_rows = []
    rows_by_paper = {}
    for row in recall_rows:
        if not isinstance(row, dict):
            errors.append("recall contains a non-object paper row")
            continue
        try:
            paper_id = int(row["paper_id"])
        except (KeyError, TypeError, ValueError):
            errors.append("recall contains a row without a valid paper_id")
            continue
        if paper_id in rows_by_paper:
            errors.append(f"recall contains duplicate rows for paper{paper_id}")
            if invalid_papers is not None and paper_id in expected_papers:
                invalid_papers.add(paper_id)
            continue
        rows_by_paper[paper_id] = row
    if set(rows_by_paper) != expected_papers:
        errors.append(f"recall papers differ: expected {sorted(expected_papers)}, found {sorted(rows_by_paper)}")
        if invalid_papers is not None:
            invalid_papers.update(expected_papers - set(rows_by_paper))

    total_rubric = 0
    total_covered = 0
    for paper_id in sorted(expected_papers & set(rows_by_paper)):
        row = rows_by_paper[paper_id]
        numeric_fields = ("n_rubric", "n_ai", "n_pairs_scored", "n_covered")
        if any(not isinstance(row.get(field), int) or isinstance(row.get(field), bool) for field in numeric_fields):
            errors.append(f"recall counts are missing or invalid for paper{paper_id}")
            if invalid_papers is not None:
                invalid_papers.add(paper_id)
            n_rubric = n_ai = n_pairs = n_covered = 0
        else:
            n_rubric = row["n_rubric"]
            n_ai = row["n_ai"]
            n_pairs = row["n_pairs_scored"]
            n_covered = row["n_covered"]
        expected_numbers = {int(item["item_number"]) for item in exports[paper_id]}
        if n_rubric < 1:
            errors.append(f"recall rubric count is invalid for paper{paper_id}")
            if invalid_papers is not None:
                invalid_papers.add(paper_id)
        if n_ai != len(expected_numbers):
            errors.append(
                f"recall AI item count differs for paper{paper_id}: " f"expected {len(expected_numbers)}, found {n_ai}"
            )
            if invalid_papers is not None:
                invalid_papers.add(paper_id)
        expected_pair_keys = {
            (rubric_index, item_number) for rubric_index in range(max(n_rubric, 0)) for item_number in expected_numbers
        }
        pairs = row.get("pair_details")
        if not isinstance(pairs, list):
            errors.append(f"recall pair_details is missing for paper{paper_id}")
            if invalid_papers is not None:
                invalid_papers.add(paper_id)
            pairs = []
        actual_pair_keys = []
        covered = set()
        for pair in pairs:
            if not isinstance(pair, dict):
                errors.append(f"recall contains an invalid pair for paper{paper_id}")
                if invalid_papers is not None:
                    invalid_papers.add(paper_id)
                continue
            try:
                key = (int(pair["rubric_idx"]), int(pair["ai_item_number"]))
            except (KeyError, TypeError, ValueError):
                errors.append(f"recall contains a pair without valid identity for paper{paper_id}")
                if invalid_papers is not None:
                    invalid_papers.add(paper_id)
                continue
            actual_pair_keys.append(key)
            label = pair.get("parsed_binary")
            if pair.get("error") or label not in {"similar", "not_similar"}:
                errors.append(
                    f"recall judge error for paper{paper_id} "
                    f"rubric {pair.get('rubric_idx')} / item {pair.get('ai_item_number')}"
                )
                if invalid_papers is not None:
                    invalid_papers.add(paper_id)
            expected_similar = label == "similar"
            if not isinstance(pair.get("is_similar"), bool) or pair.get("is_similar") != expected_similar:
                errors.append(
                    f"recall similarity flag is invalid for paper{paper_id} "
                    f"rubric {pair.get('rubric_idx')} / item {pair.get('ai_item_number')}"
                )
                if invalid_papers is not None:
                    invalid_papers.add(paper_id)
            if expected_similar:
                covered.add(key[0])
        if (
            n_pairs != len(expected_pair_keys)
            or len(actual_pair_keys) != len(expected_pair_keys)
            or set(actual_pair_keys) != expected_pair_keys
        ):
            errors.append(f"recall pair identities are incomplete for paper{paper_id}")
            if invalid_papers is not None:
                invalid_papers.add(paper_id)
        if n_covered != len(covered) or not 0 <= n_covered <= n_rubric:
            errors.append(f"recall covered count is invalid for paper{paper_id}")
            if invalid_papers is not None:
                invalid_papers.add(paper_id)
        try:
            paper_recall = float(row["recall"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"recall score is missing or invalid for paper{paper_id}")
            if invalid_papers is not None:
                invalid_papers.add(paper_id)
        else:
            expected_recall = n_covered / n_rubric if n_rubric else 0.0
            if abs(paper_recall - expected_recall) > 1e-12:
                errors.append(f"recall score is inconsistent for paper{paper_id}")
                if invalid_papers is not None:
                    invalid_papers.add(paper_id)
        total_rubric += n_rubric
        total_covered += n_covered

    if recall.get("n_papers") != len(expected_papers):
        errors.append(f"recall paper count differs: expected {len(expected_papers)}, found {recall.get('n_papers')}")
    if recall.get("total_rubric_items") != total_rubric:
        errors.append(f"recall rubric total differs: expected {total_rubric}, found {recall.get('total_rubric_items')}")
    if recall.get("total_covered") != total_covered:
        errors.append(f"recall covered total differs: expected {total_covered}, found {recall.get('total_covered')}")
    try:
        overall_recall = float(recall["overall_recall"])
    except (KeyError, TypeError, ValueError):
        errors.append("component output has no valid overall_recall")
    else:
        expected_recall = total_covered / total_rubric if total_rubric else 0.0
        if abs(overall_recall - expected_recall) > 1e-12:
            errors.append("overall recall is inconsistent with paper results")
    return errors


def _validate_precision_output(
    precision: dict[str, Any],
    exports: dict[int, list[dict[str, Any]]],
    invalid_papers: set[int] | None = None,
) -> list[str]:
    errors = []
    expected_papers = set(exports)
    precision_rows = precision.get("per_item")
    if not isinstance(precision_rows, list):
        errors.append("precision per_item is missing or invalid")
        precision_rows = []
    expected_item_keys = {(paper_id, int(item["item_number"])) for paper_id, items in exports.items() for item in items}
    precision_item_keys = []
    fully_good = 0
    for item in precision_rows:
        if not isinstance(item, dict):
            errors.append("precision contains a non-object item")
            continue
        try:
            key = (int(item["paper_id"]), int(item["item_number"]))
        except (KeyError, TypeError, ValueError):
            errors.append("precision contains an item without a valid paper_id or item_number")
            continue
        precision_item_keys.append(key)
        correctness = item.get("correctness")
        significance = item.get("significance")
        evidence = item.get("evidence")
        if correctness == "Not Correct":
            valid = significance is None and evidence is None
        elif correctness == "Correct":
            if significance in {"Significant", "Marginally Significant"}:
                valid = evidence in {"Sufficient", "Requires More"}
            else:
                valid = significance == "Not Significant" and evidence is None
        else:
            valid = False
        if not valid:
            errors.append(f"precision judge labels are invalid for paper{key[0]} item {key[1]}")
            if invalid_papers is not None and key[0] in expected_papers:
                invalid_papers.add(key[0])
        expected_good = correctness == "Correct" and significance == "Significant" and evidence == "Sufficient"
        if not isinstance(item.get("is_fully_good"), bool) or item.get("is_fully_good") != expected_good:
            errors.append(f"precision fully-good flag is invalid for paper{key[0]} item {key[1]}")
            if invalid_papers is not None and key[0] in expected_papers:
                invalid_papers.add(key[0])
        if expected_good:
            fully_good += 1
    actual_item_keys = set(precision_item_keys)
    if len(precision_item_keys) != len(expected_item_keys) or actual_item_keys != expected_item_keys:
        errors.append("precision item identities differ from the exported findings")
        if invalid_papers is not None:
            invalid_papers.update(paper_id for paper_id, _ in expected_item_keys - actual_item_keys)
            invalid_papers.update(
                paper_id
                for paper_id, item_number in actual_item_keys - expected_item_keys
                if paper_id in expected_papers
            )
            invalid_papers.update(
                paper_id
                for (paper_id, item_number), count in Counter(precision_item_keys).items()
                if count != 1 and paper_id in expected_papers
            )
    precision_papers = {paper_id for paper_id, _ in precision_item_keys}
    if precision_papers != expected_papers:
        errors.append(f"precision papers differ: expected {sorted(expected_papers)}, found {sorted(precision_papers)}")
    expected_items = sum(len(items) for items in exports.values())
    try:
        precision_count = int(precision.get("n_items", -1))
    except (TypeError, ValueError):
        precision_count = -1
    if precision_count != expected_items:
        errors.append(f"precision item count differs: expected {expected_items}, found {precision.get('n_items')}")
    if precision.get("n_papers") != len(expected_papers):
        errors.append(
            f"precision paper count differs: expected {len(expected_papers)}, found {precision.get('n_papers')}"
        )
    if precision.get("n_fully_good") != fully_good:
        errors.append(
            f"precision fully-good count differs: expected {fully_good}, found {precision.get('n_fully_good')}"
        )
    try:
        precision_value = float(precision["precision"])
    except (KeyError, TypeError, ValueError):
        errors.append("component output has no valid precision")
    else:
        expected_precision = fully_good / expected_items if expected_items else 0.0
        if abs(precision_value - expected_precision) > 1e-12:
            errors.append("precision score is inconsistent with item decisions")
    return errors


def metric_value(payload: dict[str, Any], key: str, errors: list[str]) -> float | None:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError):
        errors.append(f"component output has no valid {key}")
        return None
    if not 0 <= value <= 1:
        errors.append(f"component output {key} is outside [0, 1]")
        return None
    return value


def derive_role_metrics(
    recall: dict[str, Any],
    precision: dict[str, Any],
    exports: dict[int, list[dict[str, Any]]],
    selection_stats: dict[str, Any],
) -> dict[str, Any]:
    item_roles = {
        (paper_id, int(item["item_number"])): item["scriptorium"]["role"]
        for paper_id, items in exports.items()
        for item in items
    }
    total_rubric = int(recall.get("total_rubric_items", 0))
    covered_by_role = {role: set() for role in ROLE_ORDER}
    for paper in recall.get("per_paper", []):
        paper_id = int(paper["paper_id"])
        for pair in paper.get("pair_details", []):
            if not pair.get("is_similar"):
                continue
            role = item_roles.get((paper_id, int(pair["ai_item_number"])))
            if role in covered_by_role:
                covered_by_role[role].add((paper_id, int(pair["rubric_idx"])))

    precision_items = {role: [] for role in ROLE_ORDER}
    for item in precision.get("per_item", []):
        role = item_roles.get((int(item["paper_id"]), int(item["item_number"])))
        if role in precision_items:
            precision_items[role].append(item)

    metrics = {}
    for role in ROLE_ORDER:
        items = precision_items[role]
        n_good = sum(bool(item.get("is_fully_good")) for item in items)
        role_precision = n_good / len(items) if items else 0.0
        role_recall = len(covered_by_role[role]) / total_rubric if total_rubric else 0.0
        role_f1 = (
            2 * role_precision * role_recall / (role_precision + role_recall) if role_precision + role_recall else 0.0
        )
        metrics[role] = {
            **selection_stats["total"]["by_role"][role],
            "precision_items": len(items),
            "fully_good_items": n_good,
            "precision": role_precision,
            "covered_rubric_items": len(covered_by_role[role]),
            "recall": role_recall,
            "f1": role_f1,
        }
    return metrics


def axis_breakdown(precision: dict[str, Any]) -> dict[str, dict[str, int]]:
    items = precision.get("per_item", [])
    return {
        "correctness": dict(Counter(str(item.get("correctness")) for item in items)),
        "significance": dict(Counter(str(item.get("significance")) for item in items)),
        "evidence": dict(Counter(str(item.get("evidence")) for item in items)),
    }


def _verify_prepared_inputs(
    paper_ids: Iterable[int],
    dataset_revision: str,
    cache_root: Path,
    expected_digests: dict[str, str],
) -> None:
    for paper_id in paper_ids:
        prepared = cache_root / "dataset" / dataset_revision / f"paper{paper_id}"
        if json_digest(validate_prepared_paper(prepared)) != expected_digests[str(paper_id)]:
            raise BenchmarkError(f"Prepared paper was modified during evaluation: paper{paper_id}")


def complete_summary_is_reusable(
    summary: dict[str, Any],
    evaluation_manifest: dict[str, Any],
    recall_path: Path,
    precision_path: Path,
    exports: dict[int, list[dict[str, Any]]],
    run_manifest: dict[str, Any],
) -> bool:
    try:
        recall = _load_json(recall_path)
        precision = _load_json(precision_path)
        actual_digests = {
            "recall": file_digest(recall_path),
            "precision": file_digest(precision_path),
        }
        output_errors = validate_component_outputs(recall, precision, exports)
        metric_errors: list[str] = []
        recall_value = metric_value(recall, "overall_recall", metric_errors)
        precision_value = metric_value(precision, "precision", metric_errors)
        if output_errors or metric_errors:
            return False
        f1 = (
            2 * recall_value * precision_value / (recall_value + precision_value)
            if recall_value is not None and precision_value is not None and recall_value + precision_value
            else 0.0
        )
        frozen_inputs = evaluation_manifest["frozen_inputs"]
        selection = evaluation_manifest["selection"]
        created_at = summary["created_at"]
        created = datetime.fromisoformat(created_at)
        wrapper_elapsed = summary["timing"]["wrapper_elapsed_seconds"]
        if (
            created.tzinfo is None
            or isinstance(wrapper_elapsed, bool)
            or not isinstance(wrapper_elapsed, (int, float))
            or not math.isfinite(wrapper_elapsed)
            or wrapper_elapsed < 0
        ):
            return False
        expected = {
            "benchmark": "peerreviewbench",
            "status": "complete",
            "created_at": created_at,
            "evaluation_manifest_digest": json_digest(evaluation_manifest),
            "finding_mode": frozen_inputs["finding_mode"],
            "model_slug": frozen_inputs["model_slug"],
            "paper_ids": frozen_inputs["paper_ids"],
            "selection": selection,
            "judges": {
                "similarity_model": frozen_inputs["similarity_model"],
                "precision_model": frozen_inputs["judge_model"],
            },
            "metrics": {
                "recall": recall_value,
                "precision": precision_value,
                "f1": f1,
                "axes": axis_breakdown(precision),
                "by_role": derive_role_metrics(recall, precision, exports, selection),
            },
            "costs": {
                "reviewer_estimated_usd": run_manifest.get("reviewer_cost_usd"),
                "judge_estimated_usd": None,
                "judge_cost_note": "Pinned upstream components do not report judge cost.",
            },
            "timing": {
                "wrapper_elapsed_seconds": wrapper_elapsed,
                "recall_elapsed_seconds": recall.get("elapsed_seconds"),
                "precision_elapsed_seconds": precision.get("elapsed_seconds"),
            },
            "component_exit_codes": {"recall": 0, "precision": 0},
            "invalidated_component_caches": {},
            "component_output_digests": actual_digests,
            "errors": [],
            "package_versions": evaluation_manifest["package_versions"],
        }
    except (BenchmarkError, KeyError, OSError, TypeError, ValueError):
        return False
    return not output_errors and not metric_errors and summary == expected


def evaluate_benchmark(
    *,
    run_dir: Path,
    finding_mode: str,
    similarity_model: str | None = None,
    judge_model: str | None = None,
    concurrency: int | None = None,
    temperature: float | None = None,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    evaluations_root: Path = DEFAULT_EVALUATIONS_ROOT,
    runner: ComponentRunner = subprocess.run,
) -> tuple[Path, dict[str, Any]]:
    started = time.monotonic()
    if run_dir.is_symlink():
        raise BenchmarkError(f"Benchmark run directory must not be a symlink: {run_dir}")
    run_dir = run_dir.resolve()
    if cache_root.is_symlink():
        raise BenchmarkError(f"Benchmark cache root must not be a symlink: {cache_root}")
    if evaluations_root.is_symlink():
        raise BenchmarkError(f"Benchmark evaluations root must not be a symlink: {evaluations_root}")
    lock = load_lock()
    defaults = lock["defaults"]
    similarity_model = similarity_model or str(defaults["similarity_model"])
    judge_model = judge_model or str(defaults["judge_model"])
    concurrency = concurrency if concurrency is not None else int(defaults["recall_concurrency"])
    temperature = temperature if temperature is not None else float(defaults["recall_temperature"])
    if concurrency < 1:
        raise BenchmarkError("--concurrency must be positive")
    if not math.isfinite(temperature):
        raise BenchmarkError("--temperature must be finite")
    run_manifest = load_run_manifest(run_dir)
    expected_dataset = {
        "id": lock["dataset"]["id"],
        "revision": lock["dataset"]["revision"],
    }
    expected_upstream = {
        "repository": lock["upstream"]["repository"],
        "commit": lock["upstream"]["commit"],
        "archive_sha256": lock["upstream"]["archive_sha256"],
    }
    if (
        run_manifest["frozen_inputs"].get("dataset") != expected_dataset
        or run_manifest["frozen_inputs"].get("upstream") != expected_upstream
    ):
        raise BenchmarkError("Benchmark run pins do not match benchmark.lock.toml")
    precision_image = str(lock["precision"]["container_image"])
    precision_image_id = resolve_precision_image(precision_image)
    safe_cache_directory(cache_root, "dataset", str(lock["dataset"]["revision"]))
    exports, selection_stats = collect_exports(run_dir, run_manifest, finding_mode, cache_root)
    if any(not items for items in exports.values()):
        empty = [str(paper_id) for paper_id, items in exports.items() if not items]
        raise BenchmarkError(f"Upstream BYOJ cannot score papers with zero findings: {', '.join(empty)}")

    client = DatasetClient(lock["dataset"]["id"], lock["dataset"]["revision"])
    client.assert_current_revision()
    upstream_root = prepare_upstream(lock["upstream"], cache_root)
    frozen_inputs = _evaluation_frozen_inputs(
        run_dir,
        run_manifest,
        exports,
        finding_mode,
        similarity_model,
        judge_model,
        concurrency,
        temperature,
        precision_image,
        precision_image_id,
        lock,
    )
    evaluation_dir = safe_cache_directory(
        evaluations_root,
        run_manifest["run_id"],
        finding_mode,
    )
    evaluation_manifest = prepare_evaluation_view(
        evaluation_dir,
        frozen_inputs,
        selection_stats,
        exports,
        cache_root,
    )
    _validate_component_paths(evaluation_dir, frozen_inputs)
    _validate_evaluation_manifest(evaluation_dir, evaluation_manifest)
    evaluation_manifest_digest = json_digest(evaluation_manifest)
    existing_summary_path = evaluation_dir / "summary.json"
    if existing_summary_path.is_file():
        existing = _load_json(existing_summary_path)
        if complete_summary_is_reusable(
            existing,
            evaluation_manifest,
            evaluation_dir / "recall.json",
            evaluation_dir / "precision.json",
            exports,
            run_manifest,
        ):
            client.assert_current_revision()
            return evaluation_dir, existing

    recall_path = evaluation_dir / "recall.json"
    precision_path = evaluation_dir / "precision.json"
    judge_api_key = os.environ.get("LITELLM_API_KEY")
    if not judge_api_key:
        raise BenchmarkError("LITELLM_API_KEY is required for benchmark evaluation")
    precision_input = (
        json.dumps(
            {
                "api_key": judge_api_key,
                "base_url": os.environ.get("LITELLM_BASE_URL") or None,
            }
        )
        + "\n"
    )
    recall_path.unlink(missing_ok=True)
    precision_path.unlink(missing_ok=True)
    recall_command, precision_command = build_component_commands(
        upstream_root,
        evaluation_dir / "papers",
        evaluation_dir,
        dataset_id=lock["dataset"]["id"],
        dataset_revision=lock["dataset"]["revision"],
        model_slug=frozen_inputs["model_slug"],
        similarity_model=similarity_model,
        judge_model=judge_model,
        concurrency=concurrency,
        temperature=temperature,
        precision_image_id=precision_image_id,
    )
    component_codes = {
        "recall": run_component("recall", recall_command, evaluation_dir, runner),
        "precision": run_component(
            "precision",
            precision_command,
            evaluation_dir,
            runner,
            input_text=precision_input,
        ),
    }
    errors = [f"{name} component exited with {code}" for name, code in component_codes.items() if code]
    recall: dict[str, Any] = {}
    precision: dict[str, Any] = {}
    recall_loaded = False
    precision_loaded = False
    component_paths_safe = True
    try:
        _validate_component_paths(evaluation_dir, frozen_inputs)
    except BenchmarkError as exc:
        component_paths_safe = False
        errors.append(str(exc))
    if component_paths_safe:
        try:
            recall = _load_json(recall_path)
            recall_loaded = True
        except BenchmarkError as exc:
            errors.append(str(exc))
        try:
            precision = _load_json(precision_path)
            precision_loaded = True
        except BenchmarkError as exc:
            errors.append(str(exc))
    invalid_recall_papers: set[int] = set()
    invalid_precision_papers: set[int] = set()
    recall_output_errors = _validate_recall_output(recall, exports, invalid_recall_papers) if recall_loaded else []
    precision_output_errors = (
        _validate_precision_output(precision, exports, invalid_precision_papers) if precision_loaded else []
    )
    output_errors = recall_output_errors + precision_output_errors
    errors.extend(output_errors)
    cache_invalidations: dict[str, list[str]] = {}
    for component, component_errors, invalid_papers in (
        ("recall", recall_output_errors, invalid_recall_papers),
        ("precision", precision_output_errors, invalid_precision_papers),
    ):
        if not component_errors:
            continue
        try:
            target_papers = sorted(invalid_papers) or frozen_inputs["paper_ids"]
            cache_invalidations[component] = invalidate_component_caches(
                evaluation_dir,
                frozen_inputs,
                component,
                target_papers,
            )
        except BenchmarkError as exc:
            errors.append(str(exc))

    expected_digests = {
        str(paper_id): run_manifest["papers"][str(paper_id)]["prepared_manifest_digest"]
        for paper_id in frozen_inputs["paper_ids"]
    }
    try:
        _verify_prepared_inputs(
            frozen_inputs["paper_ids"],
            lock["dataset"]["revision"],
            cache_root,
            expected_digests,
        )
        _validate_evaluation_view(
            evaluation_dir,
            exports,
            frozen_inputs,
            cache_root,
        )
        _validate_component_paths(evaluation_dir, frozen_inputs)
        _validate_evaluation_manifest(evaluation_dir, evaluation_manifest)
        client.assert_current_revision()
    except BenchmarkError as exc:
        errors.append(str(exc))

    recall_value = metric_value(recall, "overall_recall", errors) if recall_loaded else None
    precision_value = metric_value(precision, "precision", errors) if precision_loaded else None
    f1 = (
        2 * recall_value * precision_value / (recall_value + precision_value)
        if recall_value is not None and precision_value is not None and recall_value + precision_value
        else 0.0 if recall_value is not None and precision_value is not None else None
    )
    summary = {
        "benchmark": "peerreviewbench",
        "status": "complete" if not errors else "incomplete",
        "created_at": utc_now(),
        "evaluation_manifest_digest": evaluation_manifest_digest,
        "finding_mode": finding_mode,
        "model_slug": frozen_inputs["model_slug"],
        "paper_ids": frozen_inputs["paper_ids"],
        "selection": selection_stats,
        "judges": {
            "similarity_model": similarity_model,
            "precision_model": judge_model,
        },
        "metrics": {
            "recall": recall_value,
            "precision": precision_value,
            "f1": f1,
            "axes": (axis_breakdown(precision) if precision_loaded and not output_errors else {}),
            "by_role": (
                derive_role_metrics(recall, precision, exports, selection_stats)
                if recall_loaded and precision_loaded and not output_errors
                else {}
            ),
        },
        "costs": {
            "reviewer_estimated_usd": run_manifest.get("reviewer_cost_usd"),
            "judge_estimated_usd": None,
            "judge_cost_note": "Pinned upstream components do not report judge cost.",
        },
        "timing": {
            "wrapper_elapsed_seconds": round(time.monotonic() - started, 1),
            "recall_elapsed_seconds": (recall.get("elapsed_seconds") if recall_loaded else None),
            "precision_elapsed_seconds": (precision.get("elapsed_seconds") if precision_loaded else None),
        },
        "component_exit_codes": component_codes,
        "invalidated_component_caches": cache_invalidations,
        "component_output_digests": {
            "recall": (file_digest(recall_path) if component_paths_safe and recall_path.is_file() else None),
            "precision": (file_digest(precision_path) if component_paths_safe and precision_path.is_file() else None),
        },
        "errors": errors,
        "package_versions": evaluation_manifest["package_versions"],
    }
    _assert_evaluation_path(evaluation_dir, existing_summary_path)
    write_json_atomic(existing_summary_path, summary)
    return evaluation_dir, summary


def build_parser() -> argparse.ArgumentParser:
    lock = load_lock()
    defaults = lock["defaults"]
    parser = argparse.ArgumentParser(description="Export and evaluate a completed PeerReviewBench review run")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--finding-mode", choices=FINDING_MODES, default=defaults["finding_mode"])
    parser.add_argument("--similarity-model", default=defaults["similarity_model"])
    parser.add_argument("--judge-model", default=defaults["judge_model"])
    parser.add_argument("--concurrency", type=int, default=defaults["recall_concurrency"])
    parser.add_argument("--temperature", type=float, default=defaults["recall_temperature"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evaluation_dir, summary = evaluate_benchmark(
            run_dir=args.run_dir,
            finding_mode=args.finding_mode,
            similarity_model=args.similarity_model,
            judge_model=args.judge_model,
            concurrency=args.concurrency,
            temperature=args.temperature,
        )
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"evaluation_dir": str(evaluation_dir), "status": summary["status"]}, indent=2))
    return 0 if summary["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
