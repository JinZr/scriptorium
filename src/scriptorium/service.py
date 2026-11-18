from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any, Iterator

from pydantic import ValidationError

from .artifacts import ArtifactError, ArtifactStore
from .config import find_repo, load_project_config, validate_ready
from .decision_stats import current_status, decision_stats, markdown_lines as markdown_decision_stats
from .domain import (
    Attempt,
    AttemptStatus,
    Event,
    FindingSeverity,
    FindingStatus,
    Patch,
    PatchStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    VerificationResult,
    canonical_json,
    digest_json,
    new_id,
    utc_now,
)
from .errors import ConfigurationError, DuplicateRunError, InfrastructureError, NotFoundError, StateError
from .finding_groups import finding_groups, markdown_lines as markdown_finding_groups
from .manuscript import ManuscriptManager, export_files, export_root, page_text, render_page_view
from .schemas import (
    SCHEMA_MODELS,
    UNIVERSAL_LINE_TERMINATORS,
    EvidenceAnchorContract,
    ReviewBrief,
    ScientificReviewOutput,
)
from .storage import ConflictError, Database, NotFoundError as StorageNotFoundError, StorageError
from .tool_output import REPORT_PARTS, run_list, run_overview, task_view
from .workflow import Armarius, task_input_digest

RUN_LIST_MAX_LIMIT = 100


class _RunBusyError(StateError):
    pass


class ScriptoriumService:
    def __init__(
        self,
        repo: str | Path = ".",
        *,
        manuscript_manager: ManuscriptManager | None = None,
    ) -> None:
        self.repo = find_repo(repo)
        self.state_dir = self.repo / ".scriptorium"
        try:
            self.database = Database(self.state_dir / "state.sqlite3")
            self.artifacts = ArtifactStore(self.state_dir / "artifacts")
        except (sqlite3.Error, StorageError, OSError) as exc:
            raise InfrastructureError(f"cannot initialize local state: {exc}") from exc
        self.manuscript = manuscript_manager or ManuscriptManager(self.repo)
        self.armarius = Armarius(
            repo=self.repo,
            database=self.database,
            artifacts=self.artifacts,
            manuscript=self.manuscript,
        )

    def close(self) -> None:
        self.database.close()

    def __enter__(self) -> ScriptoriumService:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def doctor(
        self,
        profile: str | None = None,
        revision: str = "HEAD",
    ) -> dict[str, Any]:
        selected_profile = profile or "full"
        checks: list[dict[str, Any]] = []
        configuration_failed = False
        infrastructure_failed = False

        def check(name: str, ok: bool, message: str, failure_kind: str | None = None) -> None:
            nonlocal configuration_failed, infrastructure_failed
            checks.append({"name": name, "ok": ok, "message": message})
            if not ok and failure_kind == "configuration":
                configuration_failed = True
            elif not ok and failure_kind == "infrastructure":
                infrastructure_failed = True

        git_path = shutil.which("git")
        if git_path is None:
            git_ok = False
            check("git_repository", False, "git was not found", "infrastructure")
        else:
            git = subprocess.run(
                [git_path, "-C", str(self.repo), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                check=False,
            )
            git_ok = git.returncode == 0
            check(
                "git_repository",
                git_ok,
                git.stderr.strip() or git.stdout.strip(),
                "infrastructure",
            )

        project = None
        frozen_revision = None
        with tempfile.TemporaryDirectory(prefix="scriptorium-doctor-") as temporary:
            temporary_root = Path(temporary)
            snapshot = temporary_root / "snapshot"
            if git_ok:
                try:
                    resolved_revision = self.manuscript.resolve_revision(revision)
                    self.manuscript.create_snapshot(resolved_revision, snapshot)
                except (InfrastructureError, OSError) as exc:
                    check("frozen_revision", False, str(exc), "infrastructure")
                else:
                    frozen_revision = resolved_revision
                    check(
                        "frozen_revision",
                        True,
                        f"{revision} -> {frozen_revision.commit_sha}",
                    )
            else:
                check("frozen_revision", False, "not run because git_repository failed")

            if frozen_revision is None:
                check(
                    "tracked_project_config",
                    False,
                    "not run because frozen_revision failed",
                )
            else:
                config_path = snapshot / "scriptorium.toml"
                if not config_path.is_file():
                    check(
                        "tracked_project_config",
                        False,
                        "scriptorium.toml is not present in frozen revision",
                        "configuration",
                    )
                else:
                    try:
                        # Project settings must come from the frozen commit, never the dirty worktree.
                        project = load_project_config(snapshot)
                    except ConfigurationError as exc:
                        check("tracked_project_config", False, str(exc), "configuration")
                    else:
                        check(
                            "tracked_project_config",
                            True,
                            "scriptorium.toml is present in frozen revision",
                        )
                        selected_profile = profile or (
                            "full" if "full" in project.profiles else next(iter(project.profiles))
                        )

            if project is None:
                main_ok = False
                check(
                    "manuscript_main",
                    False,
                    "not run because tracked_project_config failed",
                )
            else:
                main_ok = (snapshot / project.manuscript.main).is_file()
                check(
                    "manuscript_main",
                    main_ok,
                    (
                        f"{project.manuscript.main} in {frozen_revision.commit_sha}"
                        if main_ok
                        else f"{project.manuscript.main} is missing from frozen revision"
                    ),
                    "infrastructure",
                )

            latexmk = shutil.which("latexmk")
            latexmk_ok = latexmk is not None
            check(
                "latexmk",
                latexmk_ok,
                latexmk or "latexmk was not found",
                "infrastructure",
            )
            if project is None:
                engine_ok = False
                check(
                    "latex_engine",
                    False,
                    "not run because tracked_project_config failed",
                )
            else:
                engine = shutil.which(project.manuscript.engine)
                engine_ok = engine is not None
                check(
                    "latex_engine",
                    engine_ok,
                    engine or f"{project.manuscript.engine} was not found",
                    "infrastructure",
                )

            sources = None
            if project is None:
                check(
                    "manuscript_sources",
                    False,
                    "not run because tracked_project_config failed",
                )
            elif not main_ok:
                check(
                    "manuscript_sources",
                    False,
                    "not run because manuscript_main failed",
                )
            else:
                try:
                    sources = self.manuscript.scan_project_sources(snapshot, project.manuscript)
                except (InfrastructureError, OSError, UnicodeError) as exc:
                    check("manuscript_sources", False, str(exc), "infrastructure")
                else:
                    check(
                        "manuscript_sources",
                        True,
                        f"resolved {len(sources)} source files from {', '.join(project.manuscript.entrypoints)}",
                    )

            compile_dependency = None
            if sources is None:
                compile_dependency = "manuscript_sources"
            elif not latexmk_ok:
                compile_dependency = "latexmk"
            elif not engine_ok:
                compile_dependency = "latex_engine"
            if compile_dependency is not None:
                check(
                    "manuscript_compile",
                    False,
                    f"not run because {compile_dependency} failed",
                )
            else:
                build_workspace = temporary_root / "build"
                try:
                    # Compile a copy so generated LaTeX files cannot mutate the frozen snapshot.
                    build = self.manuscript.build_project(snapshot, build_workspace, project.manuscript)
                    self.manuscript.validate_build_sources(build, sources)
                    if not build.pdf_path.is_file():
                        raise InfrastructureError(f"LaTeX build did not create {build.pdf_path.name}")
                except (InfrastructureError, OSError) as exc:
                    check("manuscript_compile", False, str(exc), "infrastructure")
                else:
                    check(
                        "manuscript_compile",
                        True,
                        f"compiled {', '.join(project.manuscript.entrypoints)} "
                        f"from {frozen_revision.commit_sha}; build-only inputs: "
                        + (
                            ", ".join(item.path for item in (build.compiler_inputs or ()) if item.kind == "build")
                            or "none"
                        ),
                    )
            detected_template = self._detected_template(snapshot, project)

        if project is None:
            check("review_profile", False, "not run because tracked_project_config failed")
        else:
            try:
                validate_ready(project, selected_profile)
            except ConfigurationError as exc:
                check("review_profile", False, str(exc), "configuration")
            else:
                check("review_profile", True, f"profile {selected_profile}")
        check("sqlite", True, str(self.state_dir / "state.sqlite3"))

        failed = [item for item in checks if not item["ok"]]
        exit_code = 3 if infrastructure_failed else 2 if configuration_failed or failed else 0
        return {
            "ok": not failed,
            "exit_code": exit_code,
            "repository": str(self.repo),
            "profile": selected_profile,
            "checks": checks,
            "detected_template": detected_template,
        }

    def _detected_template(self, snapshot: Path, project) -> dict[str, str | None] | None:
        if project is None:
            return None
        try:
            return self.manuscript.detect_template(snapshot, project.manuscript)
        except (InfrastructureError, OSError):
            return None

    def _refuse_duplicate_run(self, commit_sha: str) -> None:
        active = self._storage(self.database.list_active_runs, commit_sha)
        if active:
            existing = active[0]
            remedy = "resume" if existing.status == RunStatus.FAILED else "cancel"
            raise DuplicateRunError(
                f"run {existing.id} is already {existing.status.value} on commit {commit_sha[:12]}; "
                f"inspect it with `run status {existing.id}`, {remedy} it, or pass --allow-duplicate to start another"
            )

    async def start_run(
        self, revision: str, profile: str, allow_duplicate: bool = False, brief: str | None = None
    ) -> dict[str, Any]:
        review_brief = self._parse_brief(brief)
        run_id = new_id("run")
        with ExitStack() as locks:
            if not allow_duplicate:
                # Serialize check-and-create across processes: a concurrent starter waits here, then sees this run.
                revision = self.manuscript.resolve_revision(revision).commit_sha
                locks.enter_context(self._run_operation(f"start-{revision}", "run start", wait=True))
            locks.enter_context(self._run_operation(run_id, "run start"))
            if not allow_duplicate:
                self._refuse_duplicate_run(revision)
            run = await self.armarius.start_run(revision, profile, run_id=run_id, brief=review_brief)
            return {**self._with_brief(self.get_run(run_id)), "detected_template": self.armarius.detected_template(run)}

    @staticmethod
    def _parse_brief(text: str | None) -> ReviewBrief | None:
        if text is None:
            return None
        try:
            return ReviewBrief.model_validate_json(text)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, error['loc'])) or 'brief'}: {error['msg']}" for error in exc.errors()
            )
            raise ConfigurationError(f"invalid review brief: {problems}") from exc

    def _with_brief(self, view: dict[str, Any]) -> dict[str, Any]:
        brief = self.armarius.review_brief(view["run"])
        return view if brief is None else {**view, "review_brief": brief}

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        tasks = self._storage(self.database.list_tasks, run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        return {
            "run": run,
            "tasks": [
                {"task": task, "attempts": self._storage(self.database.list_attempts, task.id)} for task in tasks
            ],
            "finding_ids": [finding.id for finding in findings],
            "patch_ids": [patch.id for patch in patches],
        }

    def run_status(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        self.armarius.require_external_run(run)
        with self._run_operation(run.id, "run status"):
            view = self.get_run(run_id)
            return run_overview(self._with_brief(view), self.list_tasks(run_id)["next_actions"])

    def list_runs(self, status: str | None = None, limit: int = 20) -> dict[str, Any]:
        if status is not None and status not in {item.value for item in RunStatus}:
            allowed = ", ".join(item.value for item in RunStatus)
            raise ConfigurationError(f"unknown run status {status!r}; use one of: {allowed}")
        if not 1 <= limit <= RUN_LIST_MAX_LIMIT:
            raise ConfigurationError(f"--limit must be between 1 and {RUN_LIST_MAX_LIMIT}")
        total, summaries = self._storage(
            self.database.list_run_summaries, None if status is None else RunStatus(status), limit
        )
        rows = []
        for run, task_counts in summaries:
            try:
                brief = self.armarius.review_brief(run)
            except (InfrastructureError, OSError):
                brief = None  # one unreadable brief must not hide the other runs; `run status` reports it
            rows.append((run, task_counts, None if brief is None else brief["content"]))
        return run_list(total, rows, limit)

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        self.armarius.require_external_run(run)
        with self._run_operation(run.id, "run resume"):
            self._storage(self.armarius.invalidate_stale_tasks, run.id)
            await self.armarius.resume_run(run.id)
            return self.get_run(run.id)

    async def retry_task(
        self, run_id: str, task_id: str, abandon_attempt_id: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        self.armarius.require_external_run(run)
        with self._run_operation(run.id, "run retry"):
            await self.armarius.retry_task(run.id, task_id, abandon_attempt_id, reason)
            return self.get_run(run.id)

    def cancel_run(self, run_id: str, reason: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        self.armarius.require_external_run(run)
        with self._run_operation(run.id, "run cancel", wait=True):
            self._storage(self.armarius.cancel_run, run.id, reason, new_id("cancel"))
            return self.get_run(run.id)

    def list_tasks(self, run_id: str) -> dict[str, Any]:
        view = self.get_run(run_id)
        self.armarius.require_external_run(view["run"])
        next_actions = []
        active_stage = {
            RunStatus.REVIEWING: "review",
            RunStatus.REVISING: "revision",
            RunStatus.VERIFYING: "verification",
        }.get(view["run"].status)
        for item in view["tasks"]:
            task = item["task"]
            if task.stage != active_stage:
                continue
            if task.status == TaskStatus.PENDING:
                next_actions.append({"command": "task claim", "task_id": task.id})
            elif task.status == TaskStatus.RUNNING:
                next_actions.append({"command": "task show", "attempt_id": item["attempts"][-1].id})
            elif task.status in {TaskStatus.FAILED, TaskStatus.INTERRUPTED}:
                next_actions.append({"command": "run retry", "run_id": run_id, "task_id": task.id})
        if (
            view["run"].status
            in {
                RunStatus.PREPARING,
                RunStatus.REVIEWING,
                RunStatus.REVISING,
                RunStatus.VERIFYING,
            }
            and not next_actions
        ):
            next_actions.append({"command": "run resume", "run_id": run_id})
        elif view["run"].status == RunStatus.AWAITING_DECISION and not next_actions:
            findings = self._storage(self.database.list_findings, run_id)
            if any(item.status == FindingStatus.PENDING for item in findings):
                next_actions.append({"command": "finding list", "run_id": run_id, "requires_human_decision": True})
            else:
                next_actions.append({"command": "run resume", "run_id": run_id})
        elif view["run"].status == RunStatus.AWAITING_PATCH_APPROVAL:
            patch = self._storage(self.database.get_patch, view["patch_ids"][-1])
            if patch.status == PatchStatus.PROPOSED:
                next_actions.append({"command": "patch show", "patch_id": patch.id, "requires_human_decision": True})
            else:
                next_actions.append({"command": "run resume", "run_id": run_id})
        elif view["run"].status == RunStatus.READY_TO_APPLY:
            next_actions.append(
                {
                    "command": "patch apply",
                    "patch_id": view["patch_ids"][-1],
                    "requires_human_decision": True,
                }
            )
        elif view["run"].status == RunStatus.FAILED:
            try:
                self.armarius._status_before_failure(run_id)
            except StateError:
                pass
            else:
                next_actions.append({"command": "run resume", "run_id": run_id})
        return {
            "run_id": run_id,
            "run_status": view["run"].status,
            "tasks": view["tasks"],
            "next_actions": next_actions,
        }

    def claim_task(
        self, task_id: str, client: str, model: str, effort: str, session_id: str, session_source: str
    ) -> dict[str, Any]:
        if client not in {"codex", "claude_code", "antigravity"}:
            raise ConfigurationError("client must be codex, claude_code, or antigravity")
        if session_source not in {"host", "declared"}:
            raise ConfigurationError("session source must be host or declared")
        if not all(value.strip() and len(value) <= 200 for value in (model, effort, session_id)):
            raise ConfigurationError("model, effort, and session ID must be 1-200 characters")
        task = self._storage(self.database.get_task, task_id)
        with self._run_operation(task.run_id, "task claim"):
            attempt = self._storage(
                self.armarius.claim_task, task_id, client, model, effort, session_id, session_source
            )
            return self.show_task(attempt.id)

    def show_task(self, attempt_id: str) -> dict[str, Any]:
        attempt = self._storage(self.database.get_attempt, attempt_id)
        task = self._storage(self.database.get_task, attempt.task_id)
        run = self._storage(self.database.get_run, task.run_id)
        self.armarius.require_external_run(run)
        metadata = self._storage(self.database.get_external_task, task.id)
        if attempt.schema_digest != metadata["schema_digest"]:
            raise InfrastructureError("attempt is not bound to its frozen task")
        bundle = self.armarius._external_bundle(run, metadata)
        prompt = self.armarius._load_prompt_artifact(attempt.prompt_digest)
        schema = self.armarius._load_schema_artifact(run, metadata["schema_kind"], attempt.schema_digest)
        example = self.armarius.frozen_output_example(metadata)
        return {
            "run_id": run.id,
            "task": task,
            "attempt": attempt,
            "prompt": prompt,
            "schema": schema,
            "input_digest": task_input_digest(run, attempt.prompt_digest, attempt.schema_digest, attempt.bundle_digest),
            "bundle_digest": metadata["bundle_digest"],
            "bundle_path": str(bundle.workspace),
            "navigation_digest": ArtifactStore.digest_file(bundle.workspace / "navigation.json"),
            "source_map": bundle.anchor_map.model_dump(mode="json"),
            "brief": self.armarius.review_brief(run),
            "example": example,
            "example_digest": metadata.get("example_digest"),
        }

    @staticmethod
    def task_view(context: dict[str, Any]) -> dict[str, Any]:
        return task_view(context)

    async def submit_task(self, attempt_id: str, input_digest: str, output_text: str) -> dict[str, Any]:
        if len(output_text.encode("utf-8")) > 2_000_000:
            raise ConfigurationError("submission exceeds the 2 MB limit")
        attempt = self._storage(self.database.get_attempt, attempt_id)
        task = self._storage(self.database.get_task, attempt.task_id)
        with self._run_operation(task.run_id, "task submit"):
            attempt = self._storage(self.database.get_attempt, attempt_id)
            finished = self._storage(self.armarius.submit_task, attempt_id, input_digest, output_text)
            if attempt.status == AttemptStatus.RUNNING and finished.status == AttemptStatus.COMPLETED:
                await self.armarius.resume_run(task.run_id)
            report = None
            if finished.validation_report_artifact_digest:
                metadata = self._storage(self.database.get_external_task, task.id)
                report = self.armarius._load_validation_report(
                    finished, metadata["schema_kind"], metadata["schema_digest"], metadata["bundle_digest"]
                ).model_dump(mode="json")
            return {
                "attempt": finished,
                "output_digest": finished.output_artifact_digest,
                "validation_report": report,
                "run_status": self._storage(self.database.get_run, task.run_id).status,
                "next_actions": self.list_tasks(task.run_id)["next_actions"],
            }

    def check_submission(self, attempt_id: str, input_digest: str, output_text: str) -> dict[str, Any]:
        if len(output_text.encode("utf-8")) > 2_000_000:
            raise ConfigurationError("submission exceeds the 2 MB limit")
        with self._retrieval_operation(attempt_id, "task submit --check"):
            attempt = self._storage(self.database.get_attempt, attempt_id)
            task = self._storage(self.database.get_task, attempt.task_id)
            report = self._storage(self.armarius.check_submission, attempt_id, input_digest, output_text)
            output_digest = ArtifactStore.digest_bytes(output_text.encode("utf-8"))
            valid = report is None
            codes = [] if valid else sorted({issue.code for issue in report.issues})
            self._record_access(
                task.run_id,
                attempt_id,
                "submit_check",
                {"input_digest": input_digest, "output_digest": output_digest, "valid": valid, "codes": codes},
            )
            return {
                "attempt_id": attempt_id,
                "input_digest": input_digest,
                "output_digest": output_digest,
                "valid": valid,
                "recorded": False,
                "validation_report": None if valid else report.model_dump(mode="json"),
                "next_action": (
                    "submit this exact file without --check"
                    if valid
                    else "fix the reported issues and check again; the attempt remains active"
                ),
            }

    @contextmanager
    def _retrieval_operation(self, attempt_id: str, operation: str) -> Iterator[None]:
        attempt = self._storage(self.database.get_attempt, attempt_id)
        task = self._storage(self.database.get_task, attempt.task_id)
        with self._run_operation(task.run_id, operation, wait=True):
            yield

    def _task_bundle(self, attempt_id: str, *, active: bool):
        attempt = self._storage(self.database.get_attempt, attempt_id)
        task = self._storage(self.database.get_task, attempt.task_id)
        run = self._storage(self.database.get_run, task.run_id)
        self.armarius.require_external_run(run)
        if active and (attempt.status != AttemptStatus.RUNNING or task.status != TaskStatus.RUNNING):
            raise StateError("rendering pages requires an active attempt")
        metadata = self._storage(self.database.get_external_task, task.id)
        if attempt.schema_digest != metadata["schema_digest"]:
            raise InfrastructureError("attempt is not bound to its frozen task")
        bundle, files = self.armarius._retrieval_bundle(run, metadata)
        return attempt, task, run, metadata, bundle, files

    def page_task(
        self,
        attempt_id: str,
        page_number: int,
        document: str | None = None,
        scale: float | None = None,
        crop: tuple[float, float, float, float] | None = None,
        text: bool = False,
    ):
        _validate_page_view(scale, crop, text)
        with self._retrieval_operation(attempt_id, "task page"):
            attempt, task, _, _, bundle, files = self._task_bundle(attempt_id, active=True)
            location, page = self._page_location(bundle, page_number, document)
            if text:
                response = self._page_text(task.run_id, bundle, files, location)
                access = {**location, "text": {key: response[key] for key in ("digest", "path")}}
            else:
                self.armarius._verify_retrieval_file(bundle.workspace, files, page.read_path)
                response = {**location, "path": str(bundle.workspace / page.read_path), "digest": page.page_digest}
                access = {**location, "read_path": page.read_path}
                if scale is not None or crop is not None:
                    view = self._page_view(task.run_id, bundle, files, location["page"], scale or 1.5, crop)
                    response.update(path=view.pop("path"), view=view)
                    access["view"] = view
            self._record_access(task.run_id, attempt.id, "page", access)
            return response

    @staticmethod
    def _page_location(bundle, page_number: int, document: str | None):
        pdf = bundle.anchor_map.compiled_pdf
        if document is not None:
            selected = next((item for item in pdf.documents if item.entrypoint == document), None)
            if selected is None:
                raise ConfigurationError("document is not a frozen LaTeX entrypoint; inspect source-map.json")
            if not 1 <= page_number <= selected.page_count:
                raise ConfigurationError("page is outside the frozen document")
            page_number += selected.start_page - 1
        page = next((item for item in pdf.pages if item.page == page_number), None)
        if page is None:
            raise ConfigurationError("page is outside the frozen PDF")
        selected = next(
            (item for item in pdf.documents if item.start_page <= page_number < item.start_page + item.page_count), None
        )
        location = {
            "source_path": pdf.source_path,
            "page": page_number,
            "document": selected.entrypoint if selected else None,
            "document_page": page_number - selected.start_page + 1 if selected else None,
        }
        return location, page

    def _page_view(self, run_id: str, bundle, files, page: int, scale: float, crop) -> dict[str, Any]:
        pdf = self.armarius._verify_retrieval_file(bundle.workspace, files, "manuscript.pdf")
        view = {"scale": scale, "crop": list(crop) if crop is not None else None}
        digest, path = self._keep_page_view(
            run_id, ".png", lambda pending: render_page_view(pdf, page, scale, crop, pending)
        )
        return {**view, "digest": digest, "path": str(path)}

    def _page_text(self, run_id: str, bundle, files, location: dict[str, Any]) -> dict[str, Any]:
        pdf = self.armarius._verify_retrieval_file(bundle.workspace, files, "manuscript.pdf")
        content = page_text(pdf, location["page"]).encode("utf-8")

        def write(pending: Path) -> None:
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_bytes(content)

        digest, path = self._keep_page_view(run_id, ".txt", write)
        return {
            **location,
            "text_layer": "pdf",
            "evidence": False,
            "path": str(path),
            "digest": digest,
            "bytes": len(content),
            "note": PAGE_TEXT_NOTE,
        }

    def _keep_page_view(self, run_id: str, suffix: str, write) -> tuple[str, Path]:
        # A returned path keeps its bytes: views are named by digest, never overwritten, and bounded per run.
        views = self.state_dir / "runs" / run_id / "page-views"
        pending = views / f".pending{suffix}"
        write(pending)
        digest = ArtifactStore.digest_file(pending)
        destination = views / f"{digest}{suffix}"
        if destination.exists():
            pending.unlink()
            return digest, destination
        kept = sum(
            path.stat().st_size
            for path in views.iterdir()
            if path.suffix in {".png", ".txt"} and not path.name.startswith(".")
        )
        if kept + pending.stat().st_size > MAX_RUN_PAGE_VIEW_BYTES:
            pending.unlink()
            limit = MAX_RUN_PAGE_VIEW_BYTES // (1024 * 1024)
            raise StateError(f"page views for this run reached the {limit} MiB limit; reuse an earlier view")
        pending.replace(destination)
        return digest, destination

    def export_task(self, attempt_id: str, directory: str | Path):
        with self._retrieval_operation(attempt_id, "task export"):
            return self._export_task(attempt_id, directory)

    def _export_task(self, attempt_id: str, directory: str | Path):
        attempt, task, run, metadata, bundle, files = self._task_bundle(attempt_id, active=False)
        root = export_root(directory)
        names = self._export_names(bundle, files)
        collisions = [name for name in names if name.split("/")[0] == TASK_INPUT_DIRECTORY]
        if collisions:
            raise StateError(f"frozen bundle path collides with the exported task inputs: {collisions[0]}")
        inputs = self._task_inputs(run, attempt, metadata)
        listing = sorted(
            [
                *({"path": name, "digest": files[name]["digest"]} for name in names),
                *({"path": name, "digest": digest} for name, (_, digest) in inputs.items()),
            ],
            key=lambda item: item["path"],
        )

        def read(name: str) -> bytes:
            return inputs[name][0] if name in inputs else self._frozen_bytes(bundle, files, name)

        def record() -> None:
            payload = {"directory": str(root), "bundle_digest": attempt.bundle_digest, "files": listing}
            self._record_access(task.run_id, attempt.id, "export", payload)

        export_files(root, [item["path"] for item in listing], read, record)
        return {
            "attempt_id": attempt.id,
            "directory": str(root),
            "file_count": len(listing),
            "total_bytes": sum(files[name]["size"] for name in names) + sum(len(data) for data, _ in inputs.values()),
            "files_digest": digest_json(listing),
            "task_inputs": [{"path": name, "digest": digest} for name, (_, digest) in inputs.items()],
        }

    def _task_inputs(self, run: Run, attempt: Attempt, metadata: dict[str, Any]) -> dict[str, tuple[bytes, str]]:
        """Return this attempt's frozen task inputs by export path, each checked against its recorded digest."""
        schema = self.armarius._load_schema_artifact(run, metadata["schema_kind"], attempt.schema_digest)
        inputs = {
            f"{TASK_INPUT_DIRECTORY}/prompt.md": (
                self.armarius._load_prompt_artifact(attempt.prompt_digest).encode("utf-8"),
                attempt.prompt_digest,
            ),
            f"{TASK_INPUT_DIRECTORY}/schema.json": (canonical_json(schema).encode("utf-8"), attempt.schema_digest),
        }
        brief = self.armarius.review_brief(run)
        if brief is not None:
            inputs[f"{TASK_INPUT_DIRECTORY}/brief.json"] = (brief["text"].encode("utf-8"), brief["digest"])
        example = self.armarius.frozen_output_example(metadata)
        if example is not None:
            inputs[f"{TASK_INPUT_DIRECTORY}/example.json"] = (example.encode("utf-8"), metadata["example_digest"])
        for name, (data, digest) in inputs.items():
            if ArtifactStore.digest_bytes(data) != digest:
                raise InfrastructureError(f"frozen task input does not match its recorded digest: {name}")
        return inputs

    @staticmethod
    def _export_names(bundle, files) -> list[str]:
        names = {source.read_path for source in bundle.anchor_map.sources}
        names.update(name for name in (*_BUNDLE_METADATA, "manuscript.pdf") if name in files)
        if not names <= files.keys():
            raise InfrastructureError("a source is missing from the frozen bundle index")
        return sorted(names)

    def _frozen_bytes(self, bundle, files, name: str) -> bytes:
        path = self.armarius._verify_retrieval_file(bundle.workspace, files, name)
        data = path.read_bytes()
        if len(data) != files[name]["size"] or ArtifactStore.digest_bytes(data) != files[name]["digest"]:
            raise InfrastructureError(f"frozen bundle file changed during export: {name}")
        return data

    def _record_access(self, run_id: str, attempt_id: str, operation: str, payload: dict[str, Any]) -> None:
        self._storage(
            self.database.append_event,
            Event(
                run_id=run_id,
                event_type=f"tool.{operation}",
                entity_type="attempt",
                entity_id=attempt_id,
                payload=payload,
            ),
        )

    def list_findings(self, run_id: str):
        self._storage(self.database.get_run, run_id)
        return self._storage(self.database.list_findings, run_id)

    def get_finding(self, finding_id: str) -> dict[str, Any]:
        finding = self._storage(self.database.get_finding, finding_id)
        decisions = self._storage(self.database.list_decisions, "finding", finding_id)
        return {"finding": finding, "decisions": decisions}

    def decide_finding(
        self,
        finding_id: str,
        decision: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.decide_findings([finding_id], decision, reason)

    def decide_findings(
        self,
        finding_ids: list[str],
        decision: str,
        reason: str,
    ) -> dict[str, Any]:
        if not finding_ids:
            raise StateError("at least one finding ID is required")
        if len(set(finding_ids)) != len(finding_ids):
            raise StateError("finding IDs must not repeat")
        initial = [self._storage(self.database.get_finding, finding_id) for finding_id in finding_ids]
        run_ids = {finding.run_id for finding in initial}
        if len(run_ids) != 1:
            raise StateError("findings must all belong to the same run")
        with self._run_operation(initial[0].run_id, "finding decide"):
            before = [self._storage(self.database.get_finding, finding_id) for finding_id in finding_ids]
            run = self._storage(self.database.get_run, initial[0].run_id)
            self.armarius.require_external_run(run)
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                raise StateError(f"findings cannot be decided while run is {run.status.value}")
            if run.status != RunStatus.AWAITING_DECISION and decision != "waive":
                raise StateError("only a later explicit waiver is allowed after the decision stage")
            records = self._storage(self.database.decide_findings, finding_ids, decision, reason)
            updated = [self._storage(self.database.get_finding, finding_id) for finding_id in finding_ids]
            if any(
                old.status == FindingStatus.CONFIRMED and new.status == FindingStatus.WAIVED
                for old, new in zip(before, updated)
            ):
                self._storage(self.armarius.invalidate_stale_tasks, run.id)
        result: dict[str, Any] = {"finding_ids": list(finding_ids)}
        if len(finding_ids) == 1:
            result.update({"decision": records[0], "finding": updated[0]})
        else:
            result.update({"decisions": records, "findings": updated})
        return result

    def get_patch(self, patch_id: str) -> dict[str, Any]:
        patch = self._storage(self.database.get_patch, patch_id)
        decisions = self._storage(self.database.list_decisions, "patch", patch_id)
        verifications = self._storage(self.database.list_verifications, patch_id)
        diff = self.artifacts.get_bytes(patch.diff_digest).decode("utf-8")
        return {
            "patch": patch,
            "decisions": decisions,
            "verifications": verifications,
            "diff": diff,
        }

    def decide_patch(
        self,
        patch_id: str,
        decision: str,
        reason: str,
    ) -> dict[str, Any]:
        initial = self._storage(self.database.get_patch, patch_id)
        with self._run_operation(initial.run_id, "patch decide"):
            patch = self._storage(self.database.get_patch, patch_id)
            run = self._storage(self.database.get_run, patch.run_id)
            self.armarius.require_external_run(run)
            if run.status != RunStatus.AWAITING_PATCH_APPROVAL:
                raise StateError(f"patches cannot be decided while run is {run.status.value}")
            patches = self._storage(self.database.list_patches, run.id)
            if patches[-1].id != patch_id:
                raise StateError("only the latest patch can be decided")
            record = self._storage(self.database.decide_patch, patch_id, decision, reason)
            updated = self._storage(self.database.get_patch, patch_id)
        return {"decision": record, "patch": updated}

    def apply_patch(self, patch_id: str):
        initial = self._storage(self.database.get_patch, patch_id)
        with self._run_operation(initial.run_id, "patch apply"):
            patch = self._storage(self.database.get_patch, patch_id)
            run = self._storage(self.database.get_run, patch.run_id)
            self.armarius.require_external_run(run)
            if run.status != RunStatus.READY_TO_APPLY:
                raise StateError(f"patch cannot be applied while run is {run.status.value}")
            if patch.status == PatchStatus.APPLIED:
                self._storage(self.database.update_run, run.id, RunStatus.COMPLETED)
                return patch
            if patch.status != PatchStatus.VERIFIED:
                raise StateError(f"patch cannot be applied while {patch.status.value}")
            paths = tuple(sorted({str(edit["path"]) for edit in patch.edits}))
            snapshot = self.state_dir / "runs" / run.id / "snapshot"
            patched = self.state_dir / "runs" / run.id / "patched" / patch.id
            contract = self.armarius.require_evidence_anchor_contract(run.id)
            self._validate_patch_materialization(patch, snapshot, patched, paths, contract)
            try:
                self.manuscript.apply_to_worktree(snapshot, patched, paths)
            except StateError as exc:
                if not self._finish_partially_applied_patch(snapshot, patched, paths):
                    stale = self._storage(self.database.update_patch, patch.id, PatchStatus.STALE)
                    self._storage(self.database.update_run, run.id, RunStatus.FAILED, str(exc))
                    return stale
            applied = self._storage(self.database.update_patch, patch.id, PatchStatus.APPLIED)
            self._storage(self.database.update_run, run.id, RunStatus.COMPLETED)
        return applied

    def read_report(self, run_id: str, part: str):
        if part not in REPORT_PARTS:
            raise ConfigurationError(f"unknown report part: {part}")
        run = self._storage(self.database.get_run, run_id)
        with self._run_operation(run.id, "run report"):
            return self.render_report(run_id, "json")[part]

    def render_report(self, run_id: str, format: str) -> str | dict[str, Any]:
        if format not in {"markdown", "json"}:
            raise ConfigurationError("report format must be markdown or json")
        self.armarius.require_external_run(self._storage(self.database.get_run, run_id))
        run_view = self.get_run(run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        events = self._storage(self.database.list_events, run_id)
        validation_reports = []
        review_limitations = []
        review_claim_checks = []
        for item in run_view["tasks"]:
            task = item["task"]
            schema_kind = {
                "review": "review",
                "revision": "revision",
                "verification": "verification",
            }.get(task.stage)
            if task.stage == "review":
                metadata = self._storage(self.database.get_external_task, task.id)
                schema_kind = metadata["schema_kind"]
            for attempt in item["attempts"]:
                if task.stage == "review" and attempt.status == AttemptStatus.COMPLETED:
                    self.armarius._load_schema_artifact(run_view["run"], metadata["schema_kind"], attempt.schema_digest)
                    try:
                        output_text = self.artifacts.get_bytes(attempt.output_artifact_digest).decode("utf-8")
                    except (ArtifactError, UnicodeDecodeError) as exc:
                        raise InfrastructureError(
                            f"completed review attempt {attempt.id} has unreadable output"
                        ) from exc
                    output, issues = self.armarius._parse_and_validate_output(
                        SCHEMA_MODELS[schema_kind], output_text, lambda _: []
                    )
                    if output is None or issues:
                        raise InfrastructureError(f"completed review attempt {attempt.id} has invalid output")
                    if isinstance(output, ScientificReviewOutput):
                        review_claim_checks.append(self._claim_check_report(task.id, attempt.id, output))
                    review_limitations.append(
                        {
                            "task_id": task.id,
                            "attempt_id": attempt.id,
                            "role": task.role.value,
                            "limitations": list(output.limitations),
                        }
                    )
                digest = attempt.validation_report_artifact_digest
                if digest is None:
                    continue
                if schema_kind is None or attempt.schema_digest is None or attempt.bundle_digest is None:
                    raise InfrastructureError(f"attempt {attempt.id} has incomplete validation provenance")
                report = self.armarius._load_validation_report(
                    attempt,
                    schema_kind,
                    attempt.schema_digest,
                    attempt.bundle_digest,
                )
                validation_reports.append(
                    {
                        "attempt_id": attempt.id,
                        "artifact_digest": digest,
                        "report": report.model_dump(mode="json"),
                        "created_at": attempt.created_at,
                    }
                )
        validation_reports.sort(key=lambda item: (item["created_at"], item["attempt_id"]))
        for item in validation_reports:
            item.pop("created_at")
        finding_records = []
        for finding in findings:
            decisions = self._storage(self.database.list_decisions, "finding", finding.id)
            # The findings were read before their decisions; report the status the latest decision implies.
            finding_records.append(
                {"finding": replace(finding, status=current_status(finding, decisions)), "decisions": decisions}
            )
        payload = {
            **run_view,
            "run": self._report_run(run_view["run"]),
            "findings": finding_records,
            "findings_grouped": finding_groups(
                [item["finding"] for item in finding_records],
                review_claim_checks,
                [
                    {"finding_id": event.entity_id, **event.payload}
                    for event in events
                    if event.event_type == "finding.duplicate"
                ],
                self.armarius._navigation_for_run(run_view["run"]),
            ),
            "decision_stats": decision_stats((item["finding"], item["decisions"]) for item in finding_records),
            "patches": [
                {
                    "patch": patch,
                    "decisions": self._storage(self.database.list_decisions, "patch", patch.id),
                    "verifications": self._storage(self.database.list_verifications, patch.id),
                }
                for patch in patches
            ],
            "events": events,
            "validation_reports": validation_reports,
            "review_limitations": review_limitations,
            "review_claim_checks": review_claim_checks,
            "gate": self.evaluate_gate(run_id),
        }
        if format == "json":
            return _plain(payload)
        return self._markdown_report(payload)

    def _report_run(self, run: Run) -> Run | dict[str, Any]:
        brief = self.armarius.review_brief(run)
        return run if brief is None else {**_plain(run), "review_brief": brief["content"]}

    def evaluate_gate(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        self.armarius.require_external_run(run)
        tasks = self._storage(self.database.list_tasks, run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        required_roles = set(run.frozen_config["profile_roles"])
        completed_roles = set()
        review_errors = []
        for task in tasks:
            if task.stage != "review" or task.status != TaskStatus.COMPLETED:
                continue
            # An accepted submission completes its review; its stored output must still be readable.
            try:
                accepted = self._storage(self.armarius.completed_review_output, task)
            except InfrastructureError as exc:
                review_errors.append(str(exc))
                continue
            if accepted is not None:
                completed_roles.add(task.role.value)
        pending_high = [
            finding.id
            for finding in findings
            if finding.status == FindingStatus.PENDING
            and finding.severity in {FindingSeverity.BLOCKER, FindingSeverity.MAJOR}
        ]
        pending_findings = [finding.id for finding in findings if finding.status == FindingStatus.PENDING]
        confirmed_ids = {finding.id for finding in findings if finding.status == FindingStatus.CONFIRMED}
        applied_patch = next((patch for patch in reversed(patches) if patch.status == PatchStatus.APPLIED), None)
        covered_ids = (
            {finding_id for edit in applied_patch.edits for finding_id in edit["finding_ids"]}
            if applied_patch is not None
            else set()
        )
        verifications = (
            self._storage(self.database.list_verifications, applied_patch.id) if applied_patch is not None else []
        )
        conditions = {
            "required_reviews_completed": required_roles.issubset(completed_roles),
            "review_artifacts_valid": not review_errors,
            "all_findings_decided": not pending_findings,
            "no_unhandled_blocker_or_major": not pending_high,
            "confirmed_findings_covered": confirmed_ids.issubset(covered_ids) if confirmed_ids else True,
            "patched_build_succeeded": (
                applied_patch.build_succeeded if applied_patch is not None else not confirmed_ids
            ),
            "verifier_passed": (
                any(item.result == VerificationResult.PASS for item in verifications)
                if applied_patch is not None
                else not confirmed_ids
            ),
            "patch_applied_when_required": applied_patch is not None if confirmed_ids else True,
            "run_completed": run.status == RunStatus.COMPLETED,
        }
        reasons = [name for name, passed in conditions.items() if not passed]
        return {
            "run_id": run_id,
            "passed": not reasons,
            "conditions": conditions,
            "reasons": reasons,
            "errors": review_errors,
        }

    @contextmanager
    def _run_operation(self, run_id: str, operation: str, *, wait: bool = False) -> Iterator[None]:
        locks = self.state_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        path = locks / f"{run_id}.lock"
        directory_descriptor = -1
        descriptor = -1
        try:
            directory_descriptor = os.open(locks, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            # Reject link-based aliases so every contender locks the one canonical inode.
            descriptor = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise InfrastructureError(f"unsafe run lock file: {path}")
            handle = os.fdopen(descriptor, "r+b")
            descriptor = -1
        except OSError as exc:
            raise InfrastructureError(f"cannot open run lock file {path}: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if directory_descriptor >= 0:
                os.close(directory_descriptor)
        with handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                owner = self._lock_owner(handle)
                if owner is None:
                    message = f"run {run_id} is already being changed (owner details unavailable)"
                else:
                    message = (
                        f"run {run_id} is already being changed by {owner['operation']} "
                        f"(pid {owner['pid']}, host {owner['hostname']}, "
                        f"acquired_at {owner['acquired_at']}, age {owner['age_seconds']}s)"
                    )
                raise _RunBusyError(message) from exc
            try:
                owner = {
                    "operation": operation,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "acquired_at": utc_now(),
                }
                # Rewrite diagnostics in place: replacing the file would split flock ownership across inodes.
                handle.seek(0)
                handle.truncate()
                handle.write((json.dumps(owner, sort_keys=True) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _lock_owner(handle) -> dict[str, Any] | None:
        try:
            handle.seek(0)
            owner = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(owner, dict):
            return None
        if not isinstance(owner.get("operation"), str):
            return None
        if not isinstance(owner.get("pid"), int):
            return None
        if not isinstance(owner.get("hostname"), str):
            return None
        if not isinstance(owner.get("acquired_at"), str):
            return None
        try:
            acquired_at = datetime.fromisoformat(owner["acquired_at"])
        except ValueError:
            return None
        if acquired_at.tzinfo is None:
            return None
        owner["age_seconds"] = max(0, int((datetime.now(timezone.utc) - acquired_at).total_seconds()))
        return owner

    @staticmethod
    def _storage(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except StorageNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except ConflictError as exc:
            raise StateError(str(exc)) from exc
        except (StorageError, sqlite3.Error) as exc:
            raise InfrastructureError(str(exc)) from exc
        except ValueError as exc:
            raise StateError(str(exc)) from exc

    def _finish_partially_applied_patch(
        self,
        snapshot: Path,
        patched: Path,
        paths: tuple[str, ...],
    ) -> bool:
        pending: list[str] = []
        for relative in paths:
            current = self.repo / relative
            base = snapshot / relative
            replacement = patched / relative
            if not current.is_file() or not base.is_file() or not replacement.is_file():
                return False
            current_digest = sha256(current.read_bytes()).digest()
            if current_digest == sha256(replacement.read_bytes()).digest():
                continue
            if current_digest != sha256(base.read_bytes()).digest():
                return False
            pending.append(relative)
        for relative in pending:
            current = self.repo / relative
            temporary = current.with_name(f".{current.name}.scriptorium.tmp")
            temporary.write_bytes((patched / relative).read_bytes())
            shutil.copymode(current, temporary)
            temporary.replace(current)
        return True

    def _validate_patch_materialization(
        self,
        patch: Patch,
        snapshot: Path,
        patched: Path,
        paths: tuple[str, ...],
        contract: EvidenceAnchorContract,
    ) -> None:
        for edit in patch.edits:
            source = snapshot / str(edit["path"])
            if not source.is_file() or sha256(source.read_bytes()).hexdigest() != edit["source_digest"]:
                raise InfrastructureError(f"frozen patch source is corrupt: {edit['path']}")
        expected_diff = self.artifacts.get_bytes(patch.diff_digest).decode("utf-8")
        actual_diff = self.manuscript.diff(snapshot, patched, paths, contract)
        if actual_diff != expected_diff:
            raise InfrastructureError(f"patched snapshot does not match immutable diff {patch.diff_digest}")

    @staticmethod
    def _markdown_findings(items: list[dict[str, Any]]) -> list[str]:
        lines = ["", "## Findings", ""]
        if not items:
            return [*lines, "- None"]
        text = ScriptoriumService._markdown_text
        for item in items:
            finding = item["finding"]
            lines.append(f"- `{finding['id']}` — {finding['severity']} / {finding['status']}: {finding['title']}")
            lines.append(
                f"  - category: {text(finding['category'])}; role: {finding['role']}; "
                f"confidence: {finding['confidence']}"
            )
            lines.append(f"  - claim: {text(finding['claim'])}")
            if finding.get("affected_claim"):
                lines.append(f"  - affected claim: {text(finding['affected_claim'])}")
            lines.append(f"  - consequence: {text(finding['consequence'])}")
            lines.append(f"  - explanation: {text(finding['explanation'])}")
            lines.append(f"  - suggested action: {text(finding['suggested_action'])}")
            for anchor in finding["evidence"]:
                line = f"  - evidence: `{ScriptoriumService._markdown_location(anchor)}`"
                if quoted_text := anchor.get("quoted_text"):
                    line += ": " + ScriptoriumService._markdown_code_span(text(quoted_text))
                lines.append(line)
        return lines

    @staticmethod
    def _markdown_text(text: str) -> str:
        """Keep a field on one list item: each line break of the evidence anchor contract becomes a visible ⏎.

        A terminal line break keeps its mark. Lines end only at CRLF, CR, and LF; other separators stay verbatim.
        """
        rendered = re.sub("|".join(re.escape(terminator) for terminator in UNIVERSAL_LINE_TERMINATORS), " ⏎ ", text)
        return rendered[:-1] if text.endswith(UNIVERSAL_LINE_TERMINATORS) else rendered

    @staticmethod
    def _markdown_code_span(text: str) -> str:
        """Show evidence verbatim: LaTeX escapes, math, and comments must not be consumed by a Markdown renderer."""
        longest = max((len(run) for run in re.findall("`+", text)), default=0)
        fence = "`" * (longest + 1)
        # A renderer strips one space from both ends of a span that has spaces on both and some other content.
        spaced = text.startswith(" ") and text.endswith(" ") and text.strip(" ") != ""
        pad = " " if spaced or text.startswith("`") or text.endswith("`") else ""
        return f"{fence}{pad}{text}{pad}{fence}"

    @staticmethod
    def _claim_check_report(task_id: str, attempt_id: str, output: ScientificReviewOutput) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "submitted_findings": [finding.model_dump(mode="json", exclude_none=True) for finding in output.findings],
            "claim_checks": [check.model_dump(mode="json", exclude_none=True) for check in output.claim_checks],
            "claim_inventory": [claim.model_dump(mode="json", exclude_none=True) for claim in output.claim_inventory],
            "verdict": output.verdict.model_dump(mode="json"),
        }

    @staticmethod
    def _markdown_claim_checks(items: list[dict[str, Any]]) -> list[str]:
        lines = ["", "## Scientific claim checks", ""]
        if not items:
            return [*lines, "- None"]
        for item in items:
            lines.append(f"- `{item['attempt_id']}`: {len(item['claim_checks'])} checks")
            verdict = item["verdict"]
            lines.append(
                f"  - verdict: {verdict['recommendation']}; decisive questions: "
                + " | ".join(verdict["decisive_questions"])
            )
            for index, finding in enumerate(item["submitted_findings"]):
                lines.append(f"  - submitted finding [{index}]: {finding['severity']} — {finding['title']}")
            inventory = item["claim_inventory"]
            claims = [inventory[check["claim_index"]] for check in item["claim_checks"]]
            for entry_index, entry in enumerate(inventory):
                location = ScriptoriumService._markdown_location(entry["claim_anchor"])
                checked = [
                    index for index, check in enumerate(item["claim_checks"]) if check["claim_index"] == entry_index
                ]
                status = f"claim checks {checked}" if checked else f"not checked: {entry['not_checked_reason']}"
                lines.append(
                    f"  - inventoried {entry['prominence']} claim at `{location}`: {entry['claim']} — {status}"
                )
            for index, (check, claim) in enumerate(zip(item["claim_checks"], claims)):
                lines.append(
                    f"  - [{index}] {claim['claim']} — {check['assessment']}; question: {check['critical_question']}; "
                    f"countercheck: {check['countercheck']}; submitted finding indices: {check['finding_indices']}"
                )
                lines.extend(ScriptoriumService._markdown_claim_judgment(check, claim["claim_anchor"]))
                for evidence in check["evidence"]:
                    lines.append(f"    - evidence: `{ScriptoriumService._markdown_location(evidence)}`")
        lines.append("Claim checks are reviewer declarations; valid anchors do not establish scientific correctness.")
        return lines

    @staticmethod
    def _markdown_location(anchor: dict[str, Any]) -> str:
        # Validation anchors any record with a line range to the source; a page beside it was only bounds-checked.
        if "start_line" in anchor:
            return f"{anchor['source_path']}:{anchor['start_line']}-{anchor['end_line']}"
        return f"{anchor['source_path']}:page {anchor['page']}"

    @staticmethod
    def _markdown_claim_judgment(check: dict[str, Any], claim_anchor: dict[str, Any]) -> list[str]:
        location = ScriptoriumService._markdown_location(claim_anchor)
        lines = [
            f"    - claim at `{location}`; stated scope: {check['stated_scope']}",
            f"    - {check['check_type']}; answer: {check['question_answer']}",
        ]
        lines.extend(f"    - exception: {exception}" for exception in check["exceptions"])
        if recomputation := check.get("recomputation"):
            lines.append(
                f"    - recomputation {recomputation['outcome']}: {recomputation['calculation']} = "
                f"{recomputation['result']}; reported {recomputation['reported']}; "
                f"inputs: {'; '.join(recomputation['inputs'])}"
            )
        return lines

    @staticmethod
    def _markdown_brief(brief: dict[str, Any] | None) -> list[str]:
        if brief is None:
            return []
        venue = f" ({brief['venue']})" if brief["venue"] else ""
        return [
            f"- Review brief: `{brief['venue_family']}`{venue}, stage `{brief['stage']}`; "
            f"{len(brief['priority_claims'])} priority claims, {len(brief['known_weaknesses'])} known weaknesses, "
            f"{len(brief['ignore'])} out-of-scope items"
        ]

    @staticmethod
    def _markdown_report(payload: dict[str, Any]) -> str:
        plain = _plain(payload)
        run = plain["run"]
        cost = "unknown" if run["estimated_cost_usd"] is None else f"${run['estimated_cost_usd']:.6f}"
        lines = [
            f"# Scriptorium run {run['id']}",
            "",
            f"- Status: `{run['status']}`",
            f"- Commit: `{run['commit_sha']}`",
            f"- Profile: `{run['profile']}`",
            *ScriptoriumService._markdown_brief(run.get("review_brief")),
            f"- Estimated cost: {cost}",
            f"- Gate: `{'pass' if plain['gate']['passed'] else 'not passed'}`",
            "",
            "## Tasks",
            "",
        ]
        for item in plain["tasks"]:
            task = item["task"]
            lines.append(
                f"- `{task['id']}` — {task['stage']} / {task['role']} / {task['status']} "
                f"({len(item['attempts'])} attempts)"
            )
        lines.extend(["", "## Reviewer limitations", ""])
        if plain["review_limitations"]:
            for item in plain["review_limitations"]:
                lines.append(f"- `{item['attempt_id']}` / {item['role']}:{'' if item['limitations'] else ' none'}")
                lines.extend(f"  - {limitation}" for limitation in item["limitations"])
        else:
            lines.append("- None")
        lines.extend(ScriptoriumService._markdown_claim_checks(plain["review_claim_checks"]))
        lines.extend(["", "## Validation failures", ""])
        if plain["validation_reports"]:
            for item in plain["validation_reports"]:
                report = item["report"]
                first = report["issues"][0]
                lines.append(
                    f"- `{item['attempt_id']}` — {len(report['issues'])} issues; "
                    f"first `{first['code']}` at `{first['path'] or '(root)'}`; "
                    f"report `{item['artifact_digest']}`"
                )
        else:
            lines.append("- None")
        lines.extend(markdown_finding_groups(plain["findings_grouped"]))
        lines.extend(ScriptoriumService._markdown_findings(plain["findings"]))
        lines.extend(markdown_decision_stats(plain["decision_stats"]))
        lines.extend(["", "## Patches", ""])
        if plain["patches"]:
            for item in plain["patches"]:
                patch = item["patch"]
                lines.append(f"- `{patch['id']}` — {patch['status']}: {patch['summary']}")
        else:
            lines.append("- None")
        if plain["gate"]["reasons"]:
            lines.extend(["", "## Gate conditions still open", ""])
            lines.extend(f"- `{reason}`" for reason in plain["gate"]["reasons"])
        return "\n".join(lines) + "\n"


_BUNDLE_METADATA = ("manifest.json", "navigation.json", "source-map.json")
TASK_INPUT_DIRECTORY = "task"
MAX_RUN_PAGE_VIEW_BYTES = 512 * 1024 * 1024
PAGE_TEXT_NOTE = (
    "The PDF text layer is a reading aid and never evidence: extraction can reorder or drop text. "
    "Quote manuscript text from the frozen sources and cite pages of manuscript.pdf."
)


def _validate_page_view(scale, crop, text: bool) -> None:
    if text and (scale is not None or crop is not None):
        raise ConfigurationError("--text cannot be combined with --scale or --crop")
    if scale is not None and not 0.5 <= scale <= 4.0:
        raise ConfigurationError("scale must be between 0.5 and 4.0")
    if crop is not None:
        x0, y0, x1, y1 = crop
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ConfigurationError("crop must be x0,y0,x1,y1 fractions with 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Run) and value.frozen_config.get("execution") == "external":
        return {**{key: _plain(item) for key, item in asdict(value).items()}, "estimated_cost_usd": None}
    if isinstance(value, Attempt) and value.external_client is not None:
        return {
            **{key: _plain(item) for key, item in asdict(value).items()},
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "estimated_cost_usd": None,
        }
    if isinstance(value, Task) and value.route == "":
        return {key: _plain(item) for key, item in asdict(value).items() if key != "route"}
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
