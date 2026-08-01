#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from typing import Any
from urllib.parse import unquote, urlparse
import uuid

import fitz

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from scriptorium.config import ManuscriptConfig
from scriptorium.domain import RunStatus
from scriptorium.errors import InfrastructureError, StateError
from scriptorium.manuscript import BuildResult, FrozenRevision, ManuscriptManager, SourceFile
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import RuntimeFactory

try:
    from .prepare import (
        DEFAULT_CACHE_ROOT,
        BenchmarkError,
        file_digest,
        json_digest,
        load_lock,
        safe_cache_directory,
        safe_relative_path,
        validate_prepared_paper,
        write_json_atomic,
    )
except ImportError:  # Direct script execution.
    from prepare import (
        DEFAULT_CACHE_ROOT,
        BenchmarkError,
        file_digest,
        json_digest,
        load_lock,
        safe_cache_directory,
        safe_relative_path,
        validate_prepared_paper,
        write_json_atomic,
    )

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
DEFAULT_RUNS_ROOT = HERE / "runs"
DEFAULT_ROUTES_PATH = HERE / ".scriptorium" / "config.toml"
REVIEW_ROLES = ("substantive_review", "copyedit", "consistency", "figure_review")
RASTER_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_IMAGE_PATTERN = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
CONVERSION_IMAGE_PLACEHOLDER = re.compile(r"page_(?:\d+_){3}\d+\.png")
SENTINEL_TEXT = "% Configuration sentinel only; PeerReviewBenchManuscriptManager builds preprint.md.\n"
BENCHMARK_SOURCE_FILES = (
    "egs/peerreviewbench/.dockerignore",
    "egs/peerreviewbench/benchmark.lock.toml",
    "egs/peerreviewbench/prepare.py",
    "egs/peerreviewbench/precision.Dockerfile",
    "egs/peerreviewbench/run.py",
    "egs/peerreviewbench/evaluate.py",
    "egs/peerreviewbench/requirements.txt",
    "pyproject.toml",
)


class PeerReviewBenchManuscriptManager(ManuscriptManager):
    def __init__(self, repo: Path, prepared_paper: Path) -> None:
        super().__init__(repo)
        self.prepared_manifest = validate_prepared_paper(prepared_paper)
        self.prepared_paper = prepared_paper.resolve()

    def resolve_revision(self, revision: str) -> FrozenRevision:
        del revision
        validate_prepared_paper(self.prepared_paper, self.prepared_manifest)
        tree = json_digest(
            [
                {
                    "path": item["path"],
                    "digest": item["content_hash"],
                    "size_bytes": item["size_bytes"],
                }
                for item in self.prepared_manifest["files"]
            ]
        )
        commit = json_digest(
            {
                "dataset_revision": self.prepared_manifest["dataset_revision"],
                "paper_id": self.prepared_manifest["paper_id"],
                "tree": tree,
            }
        )
        return FrozenRevision(commit_sha=commit, tree_sha=tree)

    def create_snapshot(self, revision: FrozenRevision, destination: Path) -> None:
        del revision
        if destination.exists() and any(destination.iterdir()):
            raise StateError(f"Snapshot destination is not empty: {destination}")
        validate_prepared_paper(self.prepared_paper, self.prepared_manifest)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.repo / "scriptorium.toml", destination / "scriptorium.toml")
        shutil.copy2(self.repo / "benchmark.tex", destination / "benchmark.tex")
        shutil.copytree(self.prepared_paper / "preprint", destination / "preprint")

    def scan_sources(self, snapshot: Path, main: str) -> tuple[SourceFile, ...]:
        del main
        preprint = snapshot / "preprint"
        if not (preprint / "preprint.md").is_file():
            raise InfrastructureError("Prepared snapshot is missing preprint/preprint.md")
        sources = []
        for path in sorted(preprint.rglob("*")):
            if path.is_symlink():
                raise InfrastructureError(f"Symlinks are not allowed in benchmark sources: {path}")
            if not path.is_file():
                continue
            data = path.read_bytes()
            try:
                lines = len(data.decode("utf-8").splitlines())
            except UnicodeDecodeError:
                lines = 0
            sources.append(
                SourceFile(
                    path=path.relative_to(snapshot).as_posix(),
                    digest=sha256(data).hexdigest(),
                    lines=lines,
                )
            )
        return tuple(sources)

    def build(self, workspace: Path, manuscript: ManuscriptConfig) -> BuildResult:
        markdown_path = workspace / "preprint" / "preprint.md"
        try:
            markdown = markdown_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InfrastructureError(f"Cannot read benchmark Markdown: {markdown_path}") from exc
        images, skipped_placeholders = _referenced_images(workspace / "preprint", markdown)
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        _render_markdown_pdf(
            markdown,
            images,
            pdf_path,
            int(self.prepared_manifest["paper_id"]),
        )
        return BuildResult(
            pdf_path=pdf_path,
            log=(
                "Generated a neutral PDF from preprint/preprint.md for the Scriptorium "
                f"bundle contract; appended {len(images)} referenced figure(s) and ignored "
                f"{skipped_placeholders} unavailable conversion placeholder(s)."
            ),
        )


def _referenced_images(preprint: Path, markdown: str) -> tuple[tuple[Path, ...], int]:
    candidates: set[Path] = set()
    skipped_placeholders = 0
    for raw_target in (*MARKDOWN_IMAGE_PATTERN.findall(markdown), *HTML_IMAGE_PATTERN.findall(markdown)):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        parsed = urlparse(target)
        if parsed.scheme or parsed.netloc:
            raise InfrastructureError(f"Remote Markdown image references are unsupported: {target}")
        relative = safe_relative_path(unquote(parsed.path))
        if (
            relative.parent == Path(".")
            and CONVERSION_IMAGE_PLACEHOLDER.fullmatch(relative.name)
            and not (preprint / relative).exists()
        ):
            skipped_placeholders += 1
            continue
        candidates.add(relative)

    for image_list in sorted(preprint.rglob("images_list.json")):
        try:
            entries = json.loads(image_list.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InfrastructureError(f"Invalid image list: {image_list}") from exc
        if not isinstance(entries, list):
            raise InfrastructureError(f"Image list must contain a JSON array: {image_list}")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("img_path"), str) or not entry["img_path"]:
                raise InfrastructureError(f"Image list contains an invalid entry: {image_list}")
            relative = safe_relative_path(str(entry["img_path"]))
            candidates.add(Path(image_list.parent.relative_to(preprint)) / relative)

    images_root = preprint / "images"
    if images_root.is_dir() and not images_root.is_symlink():
        candidates.update(
            path.relative_to(preprint)
            for path in images_root.rglob("*")
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in RASTER_EXTENSIONS
        )

    resolved = []
    for relative in sorted(candidates):
        path = (preprint / relative).resolve()
        try:
            path.relative_to(preprint.resolve())
        except ValueError as exc:
            raise InfrastructureError(f"Image reference escapes preprint/: {relative}") from exc
        if not path.is_file():
            raise InfrastructureError(f"Referenced image is missing: {relative}")
        if path.suffix.lower() not in RASTER_EXTENSIONS:
            raise InfrastructureError(f"Referenced image format is unsupported: {relative}")
        resolved.append(path)
    return tuple(resolved), skipped_placeholders


def _render_markdown_pdf(markdown: str, images: tuple[Path, ...], output: Path, paper_id: int) -> None:
    document = fitz.open()
    try:
        lines: list[str] = []
        for source_line in markdown.expandtabs(4).splitlines():
            if not source_line:
                lines.append("")
                continue
            lines.extend(
                textwrap.wrap(
                    source_line,
                    width=100,
                    replace_whitespace=False,
                    drop_whitespace=False,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or [""]
            )
        lines_per_page = 64
        for offset in range(0, max(len(lines), 1), lines_per_page):
            page = document.new_page(width=595, height=842)
            page.insert_text(
                (42, 32),
                f"PeerReviewBench paper{paper_id} — neutral Markdown rendering",
                fontsize=7,
                fontname="courier",
                color=(0.35, 0.35, 0.35),
            )
            y = 50
            for line in lines[offset : offset + lines_per_page]:
                page.insert_text((42, y), line, fontsize=8, fontname="courier")
                y += 12
        for image_path in images:
            page = document.new_page(width=595, height=842)
            page.insert_text((36, 30), image_path.name, fontsize=8, fontname="helv")
            try:
                page.insert_image(fitz.Rect(36, 48, 559, 806), filename=str(image_path), keep_proportion=True)
            except (RuntimeError, ValueError) as exc:
                raise InfrastructureError(f"Cannot render referenced image: {image_path}") from exc
        document.set_metadata(
            {
                "title": f"PeerReviewBench paper{paper_id}",
                "author": "Scriptorium benchmark adapter",
                "subject": "Neutral Markdown rendering",
                "creator": "Scriptorium",
                "producer": "PyMuPDF",
            }
        )
        document.save(output, garbage=4, deflate=True, no_new_id=True)
    finally:
        document.close()


def project_config_text() -> str:
    roles = ", ".join(json.dumps(role) for role in REVIEW_ROLES)
    return "\n".join(
        (
            "[manuscript]",
            'main = "benchmark.tex"',
            'engine = "pdflatex"',
            "",
            "[profiles.full]",
            f"roles = [{roles}]",
            "",
        )
    )


def create_paper_project(project: Path, routes_path: Path) -> None:
    if project.exists():
        raise BenchmarkError(f"Paper project already exists: {project}")
    project.parent.mkdir(parents=True, exist_ok=True)
    temporary = project.parent / f".{project.name}.{uuid.uuid4().hex}.tmp"
    try:
        (temporary / ".scriptorium").mkdir(parents=True)
        (temporary / "scriptorium.toml").write_text(project_config_text(), encoding="utf-8")
        (temporary / "benchmark.tex").write_text(SENTINEL_TEXT, encoding="utf-8")
        shutil.copy2(routes_path, temporary / ".scriptorium" / "config.toml")
        validate_paper_project(temporary, routes_path)
        temporary.replace(project)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def validate_paper_project(project: Path, routes_path: Path) -> None:
    state_dir = project / ".scriptorium"
    if (
        project.is_symlink()
        or project.parent.is_symlink()
        or not project.is_dir()
        or state_dir.is_symlink()
        or not state_dir.is_dir()
    ):
        raise BenchmarkError(f"Generated paper project is missing or unsafe: {project}")
    expected = {
        "scriptorium.toml": project_config_text(),
        "benchmark.tex": SENTINEL_TEXT,
    }
    for relative, content in expected.items():
        path = project / relative
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise BenchmarkError(f"Generated paper project changed: {path}")
    local_routes = state_dir / "config.toml"
    if (
        routes_path.is_symlink()
        or local_routes.is_symlink()
        or not local_routes.is_file()
        or file_digest(local_routes) != file_digest(routes_path)
    ):
        raise BenchmarkError(f"Generated paper route configuration changed: {local_routes}")
    for path in (state_dir / "state.sqlite3", state_dir / "artifacts", state_dir / "runs"):
        if path.is_symlink():
            raise BenchmarkError(f"Generated paper state path is unsafe: {path}")


def available_prepared_papers(
    cache_root: Path,
    dataset_revision: str,
    dataset_id: str | None = None,
) -> dict[int, Path]:
    dataset_root = safe_cache_directory(cache_root, "dataset", dataset_revision)
    papers: dict[int, Path] = {}
    for path in sorted(dataset_root.glob("paper*")):
        if not path.is_dir():
            continue
        manifest = validate_prepared_paper(path)
        if manifest.get("dataset_revision") != dataset_revision:
            raise BenchmarkError(f"Prepared paper has the wrong dataset revision: {path}")
        if dataset_id is not None and manifest.get("dataset_id") != dataset_id:
            raise BenchmarkError(f"Prepared paper has the wrong dataset ID: {path}")
        paper_id = int(manifest["paper_id"])
        if path.name != f"paper{paper_id}" or paper_id in papers:
            raise BenchmarkError(f"Invalid prepared paper directory: {path}")
        papers[paper_id] = path
    return papers


def select_paper_ids(
    prepared: dict[int, Path],
    *,
    all_papers: bool,
    paper_ids: list[int] | None,
    expected_count: int,
    smoke_count: int,
) -> list[int]:
    if all_papers:
        if len(prepared) != expected_count:
            raise BenchmarkError(f"--all requires {expected_count} prepared papers, found {len(prepared)}")
        selected = sorted(prepared)
    elif paper_ids:
        selected = list(dict.fromkeys(paper_ids))
    else:
        if len(prepared) < smoke_count:
            raise BenchmarkError(f"Default smoke run requires {smoke_count} prepared papers")
        selected = sorted(prepared)[:smoke_count]
    missing = [paper_id for paper_id in selected if paper_id not in prepared]
    if missing:
        raise BenchmarkError(f"Papers are not prepared: {', '.join(map(str, missing))}")
    return selected


def repository_commit(repo: Path = REPOSITORY_ROOT) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BenchmarkError(f"Cannot resolve Scriptorium commit: {result.stderr.strip()}")
    return result.stdout.strip()


def source_manifest(repo: Path = REPOSITORY_ROOT) -> list[dict[str, Any]]:
    explicit_paths = [repo / relative for relative in BENCHMARK_SOURCE_FILES]
    for path in explicit_paths:
        if path.is_symlink() or not path.is_file():
            raise BenchmarkError(f"Benchmark source is missing or unsafe: {path}")
    paths = explicit_paths + [
        path
        for path in sorted((repo / "src" / "scriptorium").rglob("*"))
        if path.suffix in {".md", ".py"} and (path.is_file() or path.is_symlink())
    ]
    files = []
    for path in paths:
        if path.is_symlink():
            raise BenchmarkError(f"Benchmark source must not be a symlink: {path}")
        files.append(
            {
                "path": path.relative_to(repo).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": file_digest(path),
            }
        )
    return sorted(files, key=lambda item: item["path"])


def package_versions() -> dict[str, str | None]:
    names = (
        "scriptorium",
        "openai-codex",
        "claude-agent-sdk",
        "google-antigravity",
        "PyMuPDF",
        "litellm",
        "datasets",
        "huggingface_hub",
        "numpy",
        "tqdm",
        "openhands-ai",
    )
    versions = {"python": sys.version.split()[0]}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def route_config_summary(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        routes = {}
        for name, route in sorted(data.get("routes", {}).items()):
            routes[str(name)] = {
                "runtime": str(route.get("runtime", "codex")),
                "model_provider": str(route.get("model_provider", "")),
                "model": str(route.get("model", "")),
                "reasoning_effort": str(route.get("reasoning_effort", "high")),
                "input_usd_per_million": float(route.get("input_usd_per_million", 0)),
                "output_usd_per_million": float(route.get("output_usd_per_million", 0)),
            }
        return {
            "max_concurrency": int(data.get("max_concurrency", 2)),
            "roles": {str(key): str(value) for key, value in sorted(data.get("roles", {}).items())},
            "routes": routes,
        }
    except (AttributeError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise BenchmarkError(f"Invalid route configuration: {path}") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def finding_payload_digest(findings: list[Any]) -> str:
    return json_digest([_jsonable(asdict(finding)) for finding in findings])


def summarize_scriptorium_run(service: ScriptoriumService, run_id: str) -> dict[str, Any]:
    view = service.get_run(run_id)
    run = view["run"]
    task_summaries = []
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "duration_ms": 0,
    }
    for task_view in view["tasks"]:
        task = task_view["task"]
        attempts = []
        for attempt in task_view["attempts"]:
            attempt_data = _jsonable(asdict(attempt))
            attempts.append(attempt_data)
            for key in totals:
                totals[key] += int(attempt_data.get(key) or 0)
        task_summaries.append(
            {
                "id": task.id,
                "stage": task.stage,
                "role": task.role.value,
                "route": task.route,
                "status": task.status.value,
                "input_digest": task.input_digest,
                "attempts": attempts,
            }
        )
    findings = service.list_findings(run_id)
    return {
        "id": run.id,
        "status": run.status.value,
        "error": run.error,
        "commit_sha": run.commit_sha,
        "tree_sha": run.tree_sha,
        "profile": run.profile,
        "config_digest": run.config_digest,
        "estimated_cost_usd": run.estimated_cost_usd,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "tokens": totals,
        "tasks": task_summaries,
        "finding_count": len(findings),
        "finding_ids": [finding.id for finding in findings],
        "finding_payload_digest": finding_payload_digest(findings),
    }


def _frozen_inputs(
    lock: dict[str, Any],
    routes_digest: str,
    routes_summary: dict[str, Any],
    paper_ids: list[int],
    budget_usd: float | None,
) -> dict[str, Any]:
    return {
        "scriptorium_commit": repository_commit(),
        "source_manifest": source_manifest(),
        "dataset": {
            "id": lock["dataset"]["id"],
            "revision": lock["dataset"]["revision"],
        },
        "upstream": {
            "repository": lock["upstream"]["repository"],
            "commit": lock["upstream"]["commit"],
            "archive_sha256": lock["upstream"]["archive_sha256"],
        },
        "route_config_digest": routes_digest,
        "route_config": routes_summary,
        "profile": "full",
        "paper_ids": paper_ids,
        "budget_usd_per_paper": budget_usd,
    }


def _new_run_directory(runs_root: Path) -> Path:
    if runs_root.is_symlink():
        raise BenchmarkError(f"Benchmark runs root must not be a symlink: {runs_root}")
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid.uuid4().hex[:8]}"
    path = runs_root / name
    path.mkdir(parents=True, exist_ok=False)
    return path


def validate_benchmark_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    run_id = manifest.get("run_id")
    if (
        manifest.get("benchmark") != "peerreviewbench"
        or not isinstance(run_id, str)
        or not run_id
        or manifest.get("status") not in {"running", "complete", "incomplete"}
        or not isinstance(manifest.get("frozen_inputs"), dict)
        or not isinstance(manifest.get("package_versions"), dict)
        or not isinstance(manifest.get("papers"), dict)
    ):
        raise BenchmarkError("Benchmark run manifest has an invalid structure")
    try:
        run_id_path = safe_relative_path(run_id)
    except BenchmarkError as exc:
        raise BenchmarkError("Benchmark run manifest has an unsafe run ID") from exc
    if len(run_id_path.parts) != 1:
        raise BenchmarkError("Benchmark run manifest has an unsafe run ID")
    raw_ids = manifest["frozen_inputs"].get("paper_ids")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or any(not isinstance(paper_id, int) or isinstance(paper_id, bool) or paper_id < 1 for paper_id in raw_ids)
    ):
        raise BenchmarkError("Benchmark run manifest has invalid paper IDs")
    paper_ids = list(raw_ids)
    if len(paper_ids) != len(set(paper_ids)):
        raise BenchmarkError("Benchmark run manifest has duplicate paper IDs")
    if set(manifest["papers"]) != {str(paper_id) for paper_id in paper_ids}:
        raise BenchmarkError("Benchmark run manifest paper entries do not match its frozen selection")
    for paper_id in paper_ids:
        entry = manifest["papers"][str(paper_id)]
        if (
            not isinstance(entry, dict)
            or entry.get("paper_id") != paper_id
            or entry.get("project_dir") != f"papers/paper{paper_id}"
            or entry.get("status") not in {"pending", "running", "complete", "incomplete"}
            or not isinstance(entry.get("prepared_manifest_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["prepared_manifest_digest"]) is None
        ):
            raise BenchmarkError(f"Benchmark run manifest has an invalid paper{paper_id} entry")
        if entry["status"] == "complete":
            core_run_id = entry.get("scriptorium_run_id")
            summary = entry.get("scriptorium")
            if (
                not isinstance(core_run_id, str)
                or not core_run_id
                or not isinstance(summary, dict)
                or summary.get("id") != core_run_id
                or summary.get("status") != RunStatus.AWAITING_DECISION.value
                or not isinstance(summary.get("finding_count"), int)
                or summary["finding_count"] < 0
                or not isinstance(summary.get("finding_ids"), list)
                or len(summary["finding_ids"]) != summary["finding_count"]
                or re.fullmatch(r"[0-9a-f]{64}", str(summary.get("finding_payload_digest", ""))) is None
            ):
                raise BenchmarkError(f"Benchmark run manifest has an invalid completed paper{paper_id} entry")
    if manifest["status"] == "complete" and any(
        entry.get("status") != "complete" for entry in manifest["papers"].values()
    ):
        raise BenchmarkError("A complete benchmark manifest contains incomplete papers")
    return manifest


def _load_resume_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Cannot resume without a valid manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise BenchmarkError(f"Cannot resume without a valid manifest: {path}")
    validate_benchmark_manifest(manifest)
    if manifest["run_id"] != run_dir.name:
        raise BenchmarkError("Benchmark run ID does not match its directory")
    return manifest


def _recover_run_id(service: ScriptoriumService, known_run_id: str | None) -> str | None:
    if known_run_id:
        return known_run_id
    runs = service.database.list_runs()
    if len(runs) == 1:
        return runs[0].id
    if len(runs) > 1:
        raise BenchmarkError("Cannot recover a Scriptorium run ID from a project containing multiple runs")
    return None


def paper_project_path(run_dir: Path, entry: dict[str, Any]) -> Path:
    projects_root = run_dir / "papers"
    project = run_dir / entry["project_dir"]
    if (
        projects_root.is_symlink()
        or project.parent != projects_root
        or project.resolve().parent != projects_root.resolve()
    ):
        raise BenchmarkError(f"paper{entry['paper_id']} project path escapes the run directory")
    return project


async def _run_paper(
    entry: dict[str, Any],
    run_dir: Path,
    prepared_paper: Path,
    routes_path: Path,
    budget_usd: float | None,
    runtime_factory: RuntimeFactory | None,
) -> dict[str, Any]:
    project = paper_project_path(run_dir, entry)
    if not project.exists():
        create_paper_project(project, routes_path)
    validate_paper_project(project, routes_path)
    manager = PeerReviewBenchManuscriptManager(project, prepared_paper)
    with ScriptoriumService(
        project,
        runtime_factory=runtime_factory,
        manuscript_manager=manager,
    ) as service:
        run_id = _recover_run_id(service, entry.get("scriptorium_run_id"))
        try:
            if run_id is None:
                view = await service.start_run("prepared", "full", budget_usd)
            else:
                current = service.get_run(run_id)["run"]
                if current.status == RunStatus.AWAITING_DECISION:
                    frozen_summary = entry.get("scriptorium")
                    if (
                        isinstance(frozen_summary, dict)
                        and frozen_summary.get("status") == RunStatus.AWAITING_DECISION.value
                    ):
                        validate_completed_scriptorium_state(service, entry)
                    view = service.get_run(run_id)
                elif current.status in {
                    RunStatus.PREPARING,
                    RunStatus.REVIEWING,
                    RunStatus.FAILED,
                    RunStatus.WAITING_BUDGET,
                }:
                    view = await service.resume_run(run_id)
                else:
                    raise BenchmarkError(
                        f"paper{entry['paper_id']} cannot resume from Scriptorium status {current.status.value}"
                    )
            run_id = view["run"].id
        except BaseException:
            recovered = _recover_run_id(service, run_id)
            if recovered:
                entry["scriptorium_run_id"] = recovered
                frozen_summary = entry.get("scriptorium")
                if (
                    not isinstance(frozen_summary, dict)
                    or frozen_summary.get("status") != RunStatus.AWAITING_DECISION.value
                ):
                    entry["scriptorium"] = summarize_scriptorium_run(service, recovered)
            raise
        entry["scriptorium_run_id"] = run_id
        entry["scriptorium"] = summarize_scriptorium_run(service, run_id)
        if view["run"].status == RunStatus.AWAITING_DECISION:
            entry["status"] = "complete"
            entry["completed_at"] = entry.get("completed_at") or utc_now()
            entry["error"] = None
        else:
            entry["status"] = "incomplete"
            entry["error"] = view["run"].error or f"Scriptorium stopped at {view['run'].status.value}"
    return entry


def _validate_completed_paper(
    entry: dict[str, Any],
    run_dir: Path,
    prepared_paper: Path,
    routes_path: Path,
) -> None:
    project = paper_project_path(run_dir, entry)
    validate_paper_project(project, routes_path)
    manager = PeerReviewBenchManuscriptManager(project, prepared_paper)
    with ScriptoriumService(project, manuscript_manager=manager) as service:
        validate_completed_scriptorium_state(service, entry)


def validate_completed_scriptorium_state(
    service: ScriptoriumService,
    entry: dict[str, Any],
) -> None:
    run_id = entry.get("scriptorium_run_id")
    if not run_id:
        raise BenchmarkError(f"paper{entry['paper_id']} has no Scriptorium run ID")
    core_run = service.get_run(run_id)["run"]
    if core_run.status != RunStatus.AWAITING_DECISION:
        raise BenchmarkError(
            f"paper{entry['paper_id']} completed record is at Scriptorium status {core_run.status.value}"
        )
    current_summary = summarize_scriptorium_run(service, run_id)
    if current_summary != entry.get("scriptorium"):
        raise BenchmarkError(f"paper{entry['paper_id']} Scriptorium state changed after completion")
    for task in current_summary["tasks"]:
        for attempt in task["attempts"]:
            for field in (
                "prompt_digest",
                "schema_digest",
                "trace_artifact_digest",
                "output_artifact_digest",
            ):
                digest = attempt.get(field)
                if digest is None:
                    continue
                service.database.get_artifact(digest)
                if not service.artifacts.verify(digest):
                    raise BenchmarkError(f"paper{entry['paper_id']} artifact failed verification: {digest}")


async def run_benchmark(
    *,
    all_papers: bool = False,
    paper_ids: list[int] | None = None,
    resume: Path | None = None,
    budget_usd: float | None = None,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    runs_root: Path = DEFAULT_RUNS_ROOT,
    routes_path: Path = DEFAULT_ROUTES_PATH,
    runtime_factory: RuntimeFactory | None = None,
) -> tuple[Path, dict[str, Any]]:
    lock = load_lock()
    dataset = lock["dataset"]
    if cache_root.is_symlink():
        raise BenchmarkError(f"Benchmark cache root must not be a symlink: {cache_root}")
    if routes_path.is_symlink() or not routes_path.is_file():
        raise BenchmarkError(
            f"Missing route configuration: {routes_path}. Copy routes.example.toml there and configure a model."
        )
    routes_digest = file_digest(routes_path)
    routes_summary = route_config_summary(routes_path)
    prepared = available_prepared_papers(cache_root, dataset["revision"], dataset["id"])

    if resume is not None:
        if all_papers or paper_ids:
            raise BenchmarkError("--resume cannot be combined with a paper selection")
        if resume.is_symlink():
            raise BenchmarkError(f"Benchmark run directory must not be a symlink: {resume}")
        run_dir = resume.resolve()
        manifest = _load_resume_manifest(run_dir)
        selected_ids = [int(paper_id) for paper_id in manifest["frozen_inputs"]["paper_ids"]]
        if manifest["package_versions"] != package_versions():
            raise BenchmarkError("Installed package versions do not match the resumed run")
        frozen_budget = manifest["frozen_inputs"].get("budget_usd_per_paper")
        if budget_usd is not None and budget_usd != frozen_budget:
            raise BenchmarkError("--budget-usd does not match the resumed run")
        budget_usd = frozen_budget
        expected_frozen = _frozen_inputs(lock, routes_digest, routes_summary, selected_ids, budget_usd)
        if manifest.get("frozen_inputs") != expected_frozen:
            raise BenchmarkError("Current code, data, routes, or selection do not match the resumed run")
    else:
        selected_ids = select_paper_ids(
            prepared,
            all_papers=all_papers,
            paper_ids=paper_ids,
            expected_count=int(dataset["papers"]),
            smoke_count=int(lock["defaults"]["smoke_papers"]),
        )
        frozen = _frozen_inputs(lock, routes_digest, routes_summary, selected_ids, budget_usd)
        run_dir = _new_run_directory(runs_root)
        manifest = {
            "benchmark": "peerreviewbench",
            "run_id": run_dir.name,
            "status": "running",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "frozen_inputs": frozen,
            "package_versions": package_versions(),
            "papers": {
                str(paper_id): {
                    "paper_id": paper_id,
                    "prepared_manifest_digest": json_digest(validate_prepared_paper(prepared[paper_id])),
                    "project_dir": f"papers/paper{paper_id}",
                    "status": "pending",
                    "scriptorium_run_id": None,
                    "scriptorium": None,
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                }
                for paper_id in selected_ids
            },
        }
        write_json_atomic(run_dir / "run_manifest.json", manifest)

    missing = [paper_id for paper_id in selected_ids if paper_id not in prepared]
    if missing:
        raise BenchmarkError(f"Prepared papers needed for this run are missing: {', '.join(map(str, missing))}")
    failures = 0
    did_work = False
    for index, paper_id in enumerate(selected_ids, 1):
        entry = manifest["papers"][str(paper_id)]
        if entry["prepared_manifest_digest"] != json_digest(validate_prepared_paper(prepared[paper_id])):
            raise BenchmarkError(f"Prepared paper changed since the run was created: paper{paper_id}")
        if entry["status"] == "complete":
            try:
                _validate_completed_paper(entry, run_dir, prepared[paper_id], routes_path)
            except Exception as exc:
                did_work = True
                entry["status"] = "incomplete"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                failures += 1
                manifest["status"] = "incomplete"
                manifest["updated_at"] = utc_now()
                write_json_atomic(run_dir / "run_manifest.json", manifest)
                continue
            print(f"[{index}/{len(selected_ids)}] paper{paper_id}: already complete and verified", flush=True)
            continue
        print(f"[{index}/{len(selected_ids)}] paper{paper_id}: running full review profile", flush=True)
        did_work = True
        entry["status"] = "running"
        entry["started_at"] = entry["started_at"] or utc_now()
        entry["error"] = None
        manifest["updated_at"] = utc_now()
        write_json_atomic(run_dir / "run_manifest.json", manifest)
        try:
            await _run_paper(
                entry,
                run_dir,
                prepared[paper_id],
                routes_path,
                budget_usd,
                runtime_factory,
            )
        except KeyboardInterrupt:
            entry["status"] = "incomplete"
            entry["error"] = "interrupted"
            manifest["status"] = "incomplete"
            manifest["updated_at"] = utc_now()
            write_json_atomic(run_dir / "run_manifest.json", manifest)
            raise
        except Exception as exc:
            entry["status"] = "incomplete"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        if entry["status"] != "complete":
            failures += 1
        manifest["updated_at"] = utc_now()
        write_json_atomic(run_dir / "run_manifest.json", manifest)

    if resume is not None and not did_work and manifest["status"] == "complete":
        return run_dir, manifest
    manifest["status"] = "complete" if failures == 0 else "incomplete"
    manifest["updated_at"] = utc_now()
    manifest["reviewer_cost_usd"] = sum(
        float(entry.get("scriptorium", {}).get("estimated_cost_usd") or 0)
        for entry in manifest["papers"].values()
        if entry.get("scriptorium")
    )
    write_json_atomic(run_dir / "run_manifest.json", manifest)
    return run_dir, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Scriptorium on prepared PeerReviewBench Markdown papers")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="Run all 78 prepared papers")
    selection.add_argument("--paper-id", action="append", type=int, dest="paper_ids", help="Run one paper ID")
    parser.add_argument("--resume", type=Path, help="Resume an existing benchmark run directory")
    parser.add_argument("--budget-usd", type=float, help="Scriptorium budget per paper")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.budget_usd is not None and args.budget_usd < 0:
        print("error: --budget-usd must be non-negative", file=sys.stderr)
        return 2
    try:
        import asyncio

        run_dir, manifest = asyncio.run(
            run_benchmark(
                all_papers=args.all,
                paper_ids=args.paper_ids,
                resume=args.resume,
                budget_usd=args.budget_usd,
            )
        )
    except (BenchmarkError, InfrastructureError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"run_dir": str(run_dir), "status": manifest["status"]}, indent=2))
    return 0 if manifest["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
