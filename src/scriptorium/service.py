from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
from typing import Any, Awaitable, Callable, Iterator

from .artifacts import ArtifactStore
from .config import find_repo, load_local_config, load_project_config, validate_ready
from .domain import (
    FindingSeverity,
    FindingStatus,
    Patch,
    PatchStatus,
    RunStatus,
    TaskStatus,
    VerificationResult,
    new_id,
    utc_now,
)
from .errors import ConfigurationError, InfrastructureError, NotFoundError, StateError
from .manuscript import ManuscriptManager
from .runtime import RUNTIME_SDK_VERSIONS
from .storage import ConflictError, Database, NotFoundError as StorageNotFoundError, StorageError
from .workflow import Armarius, RuntimeFactory


class _RunBusyError(StateError):
    pass


_CANCEL_WAIT_SECONDS = 15.0
_CANCEL_POLL_SECONDS = 0.2


class ScriptoriumService:
    def __init__(
        self,
        repo: str | Path = ".",
        *,
        runtime_factory: RuntimeFactory | None = None,
        manuscript_manager: ManuscriptManager | None = None,
    ) -> None:
        self.repo = find_repo(repo)
        self.state_dir = self.repo / ".scriptorium"
        self.local_config = load_local_config(self.repo)
        try:
            self.database = Database(self.state_dir / "state.sqlite3")
            self.artifacts = ArtifactStore(self.state_dir / "artifacts")
        except (sqlite3.Error, StorageError, OSError) as exc:
            raise InfrastructureError(f"cannot initialize local state: {exc}") from exc
        self.manuscript = manuscript_manager or ManuscriptManager(self.repo)
        self.armarius = Armarius(
            repo=self.repo,
            local_config=self.local_config,
            database=self.database,
            artifacts=self.artifacts,
            manuscript=self.manuscript,
            runtime_factory=runtime_factory,
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
        budget_usd: float | None = None,
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
                    sources = self.manuscript.scan_sources(snapshot, project.manuscript.main)
                except (InfrastructureError, OSError, UnicodeError) as exc:
                    check("manuscript_sources", False, str(exc), "infrastructure")
                else:
                    check(
                        "manuscript_sources",
                        True,
                        f"resolved {len(sources)} source files from {project.manuscript.main}",
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
                    shutil.copytree(snapshot, build_workspace)
                    build = self.manuscript.build(build_workspace, project.manuscript)
                    if not build.pdf_path.is_file():
                        raise InfrastructureError(f"LaTeX build did not create {build.pdf_path.name}")
                except (InfrastructureError, OSError) as exc:
                    check("manuscript_compile", False, str(exc), "infrastructure")
                else:
                    check(
                        "manuscript_compile",
                        True,
                        f"compiled {project.manuscript.main} from {frozen_revision.commit_sha}",
                    )

        if project is None:
            role_keys = ()
        else:
            try:
                role_keys = (
                    *project.profiles[selected_profile],
                    "revision",
                    "verification",
                )
            except KeyError:
                role_keys = ()
        selected_runtimes: set[str] = set()
        for role_key in role_keys:
            try:
                selected_runtimes.add(self.local_config.route_for_role(role_key).runtime)
            except ConfigurationError:
                continue
        if project is None:
            check(
                "model_routes",
                False,
                "not run because tracked_project_config failed",
            )
        else:
            try:
                validate_ready(project, self.local_config, selected_profile, budget_usd)
            except ConfigurationError as exc:
                check("model_routes", False, str(exc), "configuration")
            else:
                check("model_routes", True, f"profile {selected_profile}")
        package_names = {
            "codex": "openai-codex",
            "claude_code": "claude-agent-sdk",
            "antigravity": "google-antigravity",
        }
        for runtime_name in sorted(selected_runtimes):
            package_name = package_names[runtime_name]
            check_name = "codex_sdk" if runtime_name == "codex" else f"runtime_{runtime_name}_sdk"
            try:
                version = metadata.version(package_name)
                expected = RUNTIME_SDK_VERSIONS[runtime_name]
                ok = version == expected
                message = version if ok else f"expected {expected}, found {version}"
            except metadata.PackageNotFoundError:
                ok = False
                message = f"{package_name} is not installed"
            check(check_name, ok, message, "infrastructure")
        if "antigravity" in selected_runtimes:
            antigravity_auth_ok = bool(os.environ.get("GEMINI_API_KEY", "").strip())
            check(
                "antigravity_auth",
                antigravity_auth_ok,
                "GEMINI_API_KEY is set" if antigravity_auth_ok else "GEMINI_API_KEY is not set",
                "infrastructure",
            )
        check("sqlite", True, str(self.state_dir / "state.sqlite3"))

        failed = [item for item in checks if not item["ok"]]
        exit_code = 3 if infrastructure_failed else 2 if configuration_failed or failed else 0
        return {
            "ok": not failed,
            "exit_code": exit_code,
            "repository": str(self.repo),
            "profile": selected_profile,
            "checks": checks,
        }

    async def start_run(
        self,
        revision: str,
        profile: str,
        budget_usd: float | None,
    ) -> dict[str, Any]:
        # Reserve the trusted ID before any run files or database rows exist so ownership starts first.
        run_id = new_id("run")
        with self._run_operation(run_id, "run start"):
            return await self._run_with_cancel_watcher(
                run_id,
                self._run_and_build_view(
                    run_id,
                    lambda: self.armarius.start_run(revision, profile, budget_usd, run_id=run_id),
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        tasks = self._storage(self.database.list_tasks, run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        return {
            "run": run,
            "tasks": [
                {
                    "task": task,
                    "attempts": self._storage(self.database.list_attempts, task.id),
                }
                for task in tasks
            ],
            "finding_ids": [finding.id for finding in findings],
            "patch_ids": [patch.id for patch in patches],
        }

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        with self._run_operation(run.id, "run resume"):
            current = self._storage(self.database.get_run, run.id)
            if current.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                self.armarius.require_evidence_anchor_contract(current.id)
            self._wait_for_provider_cleanup(run.id)
            request = self._complete_pending_cancel(run.id, provider_cleanup_ready=True)
            if request is not None:
                current = self._storage(self.database.get_run, run.id)
                if current.status == RunStatus.CANCELLED:
                    raise StateError(f"run {run.id} was cancelled by request {request['request_id']}")
                raise StateError(
                    f"run {run.id} cannot be resumed while {current.status.value}; "
                    f"cancellation request {request['request_id']} was cleared"
                )
            return await self._run_with_cancel_watcher(
                run.id,
                self._run_and_build_view(run.id, lambda: self.armarius.resume_run(run.id)),
            )

    async def retry_task(
        self,
        run_id: str,
        task_id: str,
        route: str | None = None,
    ) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        with self._run_operation(run.id, "run retry"):
            current = self._storage(self.database.get_run, run.id)
            if current.status not in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                self.armarius.require_evidence_anchor_contract(current.id)
            self._wait_for_provider_cleanup(run.id)
            request = self._complete_pending_cancel(run.id, provider_cleanup_ready=True)
            if request is not None:
                current = self._storage(self.database.get_run, run.id)
                if current.status == RunStatus.CANCELLED:
                    raise StateError(f"run {run.id} was cancelled by request {request['request_id']}")
                raise StateError(
                    f"run {run.id} cannot be retried while {current.status.value}; "
                    f"cancellation request {request['request_id']} was cleared"
                )
            return await self._run_with_cancel_watcher(
                run.id,
                self._run_and_build_view(
                    run.id,
                    lambda: self.armarius.retry_task(run.id, task_id, route),
                ),
            )

    def cancel_run(self, run_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise StateError("cancellation reason is required")
        run = self._storage(self.database.get_run, run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            with self._run_operation(run.id, "run cancel"):
                request = self._complete_pending_cancel(run.id)
                if request is not None and run.status == RunStatus.CANCELLED:
                    return self.get_run(run.id)
            raise StateError(f"run cannot be cancelled while {run.status.value}")
        request = self._publish_cancel_request(run.id, reason)
        deadline = time.monotonic() + _CANCEL_WAIT_SECONDS
        while True:
            try:
                with self._run_operation(run.id, "run cancel"):
                    remaining = max(0.0, deadline - time.monotonic())
                    from .runtime.contained import ProviderCleanupTimeout

                    try:
                        self._wait_for_provider_cleanup(run.id, timeout=remaining)
                    except ProviderCleanupTimeout:
                        raise StateError(
                            f"cancellation request {request['request_id']} remains pending for run {run.id}"
                        ) from None
                    self._complete_pending_cancel(run.id, provider_cleanup_ready=True)
                    current = self._storage(self.database.get_run, run.id)
                    if current.status == RunStatus.CANCELLED:
                        return self.get_run(run.id)
                    raise StateError(f"run cannot be cancelled while {current.status.value}")
            except _RunBusyError:
                if time.monotonic() >= deadline:
                    pending = any(item["request_id"] == request["request_id"] for item in self._cancel_requests(run.id))
                    if pending:
                        raise StateError(
                            f"cancellation request {request['request_id']} remains pending for run {run.id}"
                        ) from None
                    current = self._storage(self.database.get_run, run.id)
                    if current.status == RunStatus.CANCELLED:
                        return self.get_run(run.id)
                    if current.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                        raise StateError(f"run cannot be cancelled while {current.status.value}") from None
                    raise StateError(
                        f"cancellation request {request['request_id']} remains pending for run {run.id}"
                    ) from None
                time.sleep(min(_CANCEL_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

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
        initial = self._storage(self.database.get_finding, finding_id)
        with self._run_operation(initial.run_id, "finding decide"):
            self._reject_if_pending_cancel(initial.run_id)
            finding = self._storage(self.database.get_finding, finding_id)
            run = self._storage(self.database.get_run, finding.run_id)
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                raise StateError(f"findings cannot be decided while run is {run.status.value}")
            if run.status != RunStatus.AWAITING_DECISION and decision != "waive":
                raise StateError("only a later explicit waiver is allowed after the decision stage")
            record = self._storage(self.database.decide_finding, finding_id, decision, reason)
            updated = self._storage(self.database.get_finding, finding_id)
        return {"decision": record, "finding": updated}

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
            self._reject_if_pending_cancel(initial.run_id)
            patch = self._storage(self.database.get_patch, patch_id)
            run = self._storage(self.database.get_run, patch.run_id)
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
            self._reject_if_pending_cancel(initial.run_id)
            patch = self._storage(self.database.get_patch, patch_id)
            run = self._storage(self.database.get_run, patch.run_id)
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
            self._validate_patch_materialization(patch, snapshot, patched, paths)
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

    def render_report(self, run_id: str, format: str) -> str | dict[str, Any]:
        if format not in {"markdown", "json"}:
            raise ConfigurationError("report format must be markdown or json")
        run_view = self.get_run(run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        validation_reports = []
        for item in run_view["tasks"]:
            task = item["task"]
            schema_kind = {
                "review": "review",
                "review_transcription": "visual_transcription",
                "revision": "revision",
                "verification": "verification",
                "verification_transcription": "visual_transcription",
            }.get(task.stage)
            for attempt in item["attempts"]:
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
        payload = {
            **run_view,
            "findings": [
                {
                    "finding": finding,
                    "decisions": self._storage(self.database.list_decisions, "finding", finding.id),
                }
                for finding in findings
            ],
            "patches": [
                {
                    "patch": patch,
                    "decisions": self._storage(self.database.list_decisions, "patch", patch.id),
                    "verifications": self._storage(self.database.list_verifications, patch.id),
                }
                for patch in patches
            ],
            "events": self._storage(self.database.list_events, run_id),
            "validation_reports": validation_reports,
            "gate": self.evaluate_gate(run_id),
        }
        if format == "json":
            return _plain(payload)
        return self._markdown_report(payload)

    def evaluate_gate(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        tasks = self._storage(self.database.list_tasks, run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        required_roles = set(run.frozen_config["profile_roles"])
        completed_roles = {
            task.role.value for task in tasks if task.stage == "review" and task.status == TaskStatus.COMPLETED
        }
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
        }

    async def _run_and_build_view(
        self,
        run_id: str,
        operation_factory: Callable[[], Awaitable[Any]],
    ) -> dict[str, Any]:
        await operation_factory()
        return self.get_run(run_id)

    async def _run_with_cancel_watcher(self, run_id: str, operation) -> Any:
        operation_task = asyncio.create_task(operation)
        watcher = asyncio.create_task(self._wait_for_cancel_request(run_id))
        try:
            done, _ = await asyncio.wait(
                {operation_task, watcher},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            operation_task.cancel()
            try:
                await operation_task
            except asyncio.CancelledError:
                pass
            finally:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
            raise

        if operation_task in done:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            result = await operation_task
            request = self._complete_pending_cancel(run_id)
            if request is not None and self._storage(self.database.get_run, run_id).status == RunStatus.CANCELLED:
                raise asyncio.CancelledError(f"run {run_id} was cancelled by request {request['request_id']}")
            return result

        try:
            request = watcher.result()
        except BaseException:
            operation_task.cancel()
            with suppress(asyncio.CancelledError):
                await operation_task
            raise
        operation_task.cancel()
        result: Any = None
        operation_error: Exception | None = None
        try:
            result = await operation_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            operation_error = exc
        self._wait_for_provider_cleanup(run_id)
        self._complete_pending_cancel(run_id, provider_cleanup_ready=True)
        status = self._storage(self.database.get_run, run_id).status
        if status == RunStatus.CANCELLED:
            interrupted = asyncio.CancelledError(f"run {run_id} was cancelled by request {request['request_id']}")
            if operation_error is not None:
                raise interrupted from operation_error
            raise interrupted
        if status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            if operation_error is not None:
                raise operation_error
            return result if result is not None else self.get_run(run_id)
        if operation_error is not None:
            raise operation_error
        raise asyncio.CancelledError(f"run {run_id} was cancelled by request {request['request_id']}")

    async def _wait_for_cancel_request(self, run_id: str) -> dict[str, Any]:
        while True:
            requests = self._cancel_requests(run_id)
            if requests:
                return requests[0]
            await asyncio.sleep(_CANCEL_POLL_SECONDS)

    def _reject_if_pending_cancel(self, run_id: str) -> None:
        request = self._complete_pending_cancel(run_id)
        if request is not None:
            status = self._storage(self.database.get_run, run_id).status
            raise StateError(
                f"run {run_id} has pending cancellation request {request['request_id']} " f"and is now {status.value}"
            )

    def _complete_pending_cancel(
        self,
        run_id: str,
        *,
        provider_cleanup_ready: bool = False,
    ) -> dict[str, Any] | None:
        requests = self._cancel_requests(run_id)
        if not requests:
            return None
        request = requests[0]
        run = self._storage(self.database.get_run, run_id)
        if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            if not provider_cleanup_ready:
                self._wait_for_provider_cleanup(run_id)
            self._storage(
                self.armarius.cancel_run,
                run_id,
                request["reason"],
                request["request_id"],
            )
        self._remove_cancel_requests(run_id)
        return request

    def _wait_for_provider_cleanup(self, run_id: str, *, timeout: float = 15.0) -> None:
        from .runtime.contained import wait_for_provider_cleanup

        wait_for_provider_cleanup(self.repo, run_id, timeout=timeout)

    def _publish_cancel_request(self, run_id: str, reason: str) -> dict[str, Any]:
        request = {
            "version": 1,
            "request_id": new_id("cancel"),
            "run_id": run_id,
            "reason": reason,
            "requested_at": utc_now(),
        }
        contents = (json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(contents) > 65536:
            raise StateError("cancellation reason is too long")
        final_name = f"{run_id}.{request['request_id']}.json"
        temporary_name = f".{final_name}.{new_id('tmp')}"
        with self._cancel_directory() as directory_descriptor:
            descriptor = -1
            try:
                descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(contents)
                    handle.flush()
                    os.fsync(handle.fileno())
                # The inbox is durable before waiting, so either the current or next owner can replay it.
                os.rename(
                    temporary_name,
                    final_name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
            except OSError as exc:
                with suppress(OSError):
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                raise InfrastructureError(f"cannot publish cancellation request for run {run_id}: {exc}") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return request

    def _cancel_requests(self, run_id: str) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        prefix = f"{run_id}."
        with self._cancel_directory() as directory_descriptor:
            for name in os.listdir(directory_descriptor):
                if not name.startswith(prefix) or not name.endswith(".json"):
                    continue
                descriptor = -1
                try:
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
                    file_stat = os.fstat(descriptor)
                    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                        raise InfrastructureError(f"unsafe cancellation request file: {name}")
                    with os.fdopen(descriptor, "rb") as handle:
                        descriptor = -1
                        contents = handle.read(65537)
                    if len(contents) > 65536:
                        raise InfrastructureError(f"invalid cancellation request for run {run_id}: {name}")
                    value = json.loads(contents.decode("utf-8"))
                except FileNotFoundError:
                    continue
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise InfrastructureError(f"invalid cancellation request for run {run_id}: {name}") from exc
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                if not self._valid_cancel_request(value, run_id, name):
                    raise InfrastructureError(f"invalid cancellation request for run {run_id}: {name}")
                value["_name"] = name
                value["_requested_at_sort"] = datetime.fromisoformat(value["requested_at"])
                requests.append(value)
        return sorted(requests, key=lambda item: (item["_requested_at_sort"], item["request_id"]))

    @staticmethod
    def _valid_cancel_request(value: Any, run_id: str, name: str) -> bool:
        expected = {"version", "request_id", "run_id", "reason", "requested_at"}
        if not isinstance(value, dict) or set(value) != expected:
            return False
        if type(value["version"]) is not int or value["version"] != 1:
            return False
        if value.get("run_id") != run_id:
            return False
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or name != f"{run_id}.{request_id}.json":
            return False
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            return False
        requested_at = value.get("requested_at")
        if not isinstance(requested_at, str):
            return False
        try:
            timestamp = datetime.fromisoformat(requested_at)
        except ValueError:
            return False
        return timestamp.tzinfo is not None

    def _remove_cancel_requests(self, run_id: str) -> None:
        requests = self._cancel_requests(run_id)
        with self._cancel_directory() as directory_descriptor:
            for request in requests:
                try:
                    os.unlink(request["_name"], dir_fd=directory_descriptor)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise InfrastructureError(
                        f"cannot remove cancellation request {request['request_id']}: {exc}"
                    ) from exc
            os.fsync(directory_descriptor)

    @contextmanager
    def _cancel_directory(self) -> Iterator[int]:
        descriptors: list[int] = []
        try:
            # Walk from the trusted state directory so a linked control directory cannot redirect request I/O.
            parent = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(parent)
            for name in ("control", "cancel"):
                created = False
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                    created = True
                except FileExistsError:
                    pass
                if created:
                    os.fsync(parent)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(child)
                parent = child
            yield parent
        except OSError as exc:
            raise InfrastructureError(f"cannot open cancellation request directory: {exc}") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextmanager
    def _run_operation(self, run_id: str, operation: str) -> Iterator[None]:
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
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    ) -> None:
        for edit in patch.edits:
            source = snapshot / str(edit["path"])
            if not source.is_file() or sha256(source.read_bytes()).hexdigest() != edit["source_digest"]:
                raise InfrastructureError(f"frozen patch source is corrupt: {edit['path']}")
        expected_diff = self.artifacts.get_bytes(patch.diff_digest).decode("utf-8")
        actual_diff = self.manuscript.diff(snapshot, patched, paths)
        if actual_diff != expected_diff:
            raise InfrastructureError(f"patched snapshot does not match immutable diff {patch.diff_digest}")

    @staticmethod
    def _markdown_report(payload: dict[str, Any]) -> str:
        plain = _plain(payload)
        run = plain["run"]
        lines = [
            f"# Scriptorium run {run['id']}",
            "",
            f"- Status: `{run['status']}`",
            f"- Commit: `{run['commit_sha']}`",
            f"- Profile: `{run['profile']}`",
            f"- Estimated cost: `${run['estimated_cost_usd']:.6f}`",
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
        lines.extend(["", "## Findings", ""])
        if plain["findings"]:
            for item in plain["findings"]:
                finding = item["finding"]
                lines.append(f"- `{finding['id']}` — {finding['severity']} / {finding['status']}: {finding['title']}")
        else:
            lines.append("- None")
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


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
