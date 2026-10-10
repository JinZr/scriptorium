from __future__ import annotations

from collections import Counter, deque
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
import difflib
from enum import Enum
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any, Iterator

from pydantic import ValidationError

from .artifacts import ArtifactError, ArtifactStore
from .config import find_repo, load_project_config, reject_legacy_local_config, validate_ready
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
    new_id,
    utc_now,
)
from .errors import ConfigurationError, DuplicateRunError, InfrastructureError, NotFoundError, StateError
from .finding_groups import finding_groups, markdown_lines as markdown_finding_groups
from .manuscript import (
    EQUATION_ENVIRONMENTS,
    QUANTITY_COMMANDS,
    TABLE_ENVIRONMENTS,
    ManuscriptManager,
    export_files,
    export_root,
    page_text,
    render_page_view,
)
from .schemas import (
    EvidenceAnchorContract,
    InventoriedScientificReviewOutput,
    ReviewBrief,
    ScientificReviewOutput,
    ScopedReviewOutput,
)
from .storage import ConflictError, Database, NotFoundError as StorageNotFoundError, StorageError
from .tool_output import (
    REPORT_PARTS,
    bound_export,
    bound_nav,
    bound_read,
    bound_search,
    page_text_fragment,
    report_fragment,
    require_bounded,
    run_overview,
    task_view,
)
from .workflow import Armarius, task_input_digest


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
                reject_legacy_local_config(self.repo)
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
        reject_legacy_local_config(self.repo)
        run_id = new_id("run")
        self._storage(self.database.ensure_external_schema)
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
        with self._run_operation(run.id, "run status"):
            view = self.get_run(run_id)
            external = view["run"].frozen_config.get("execution") == "external"
            next_actions = self.list_tasks(run_id)["next_actions"] if external else []
            return run_overview(self._with_brief(view), next_actions)

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

    async def continue_review(self, run_id: str, task_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        self.armarius.require_external_run(run)
        with self._run_operation(run.id, "run continue"):
            await self.armarius.continue_review(run_id, task_id)
            return self.get_run(run_id)

    def cancel_run(self, run_id: str, reason: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        self.armarius.require_external_run(run)
        with self._run_operation(run.id, "run cancel", wait=True):
            self._storage(self.armarius.cancel_run, run.id, reason, new_id("cancel"))
            return self.get_run(run.id)

    def list_tasks(self, run_id: str) -> dict[str, Any]:
        view = self.get_run(run_id)
        self.armarius.require_external_run(view["run"])
        tasks = []
        next_actions = []
        active_stage = {
            RunStatus.REVIEWING: "review",
            RunStatus.REVISING: "revision",
            RunStatus.VERIFYING: "verification",
        }.get(view["run"].status)
        for item in view["tasks"]:
            task = item["task"]
            completion = self._storage(self.armarius.review_completion, task) if task.stage == "review" else None
            tasks.append({**item, "review_completion": completion})
            if task.stage != active_stage and not (
                task.stage == "review" and view["run"].status == RunStatus.AWAITING_DECISION
            ):
                continue
            if task.status == TaskStatus.PENDING:
                next_actions.append({"command": "task claim", "task_id": task.id})
            elif task.status == TaskStatus.RUNNING:
                next_actions.append({"command": "task show", "attempt_id": item["attempts"][-1].id})
            elif task.status == TaskStatus.COMPLETED and completion in {"partial", "unknown"}:
                next_actions.append({"command": "run continue", "run_id": run_id, "task_id": task.id})
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
        return {"run_id": run_id, "run_status": view["run"].status, "tasks": tasks, "next_actions": next_actions}

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
        source_map_bytes = (bundle.workspace / "source-map.json").read_bytes()
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
            "source_map_text": source_map_bytes.decode("utf-8"),
            "source_map_digest": ArtifactStore.digest_bytes(source_map_bytes),
            "brief": self.armarius.review_brief(run),
        }

    def task_view(self, context: dict[str, Any], part: str | None = None, offset: int = 0):
        response = task_view(context, part, offset)
        access = {"part": part or "overview", "input_digest": context["input_digest"]}
        if part is not None:
            access.update(digest=response["digest"], start_offset=offset, end_offset=offset + len(response["text"]))
        self._record_access(context["run_id"], context["attempt"].id, "show", access)
        return response

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

    def _readable_task(self, attempt_id: str):
        attempt = self._storage(self.database.get_attempt, attempt_id)
        task = self._storage(self.database.get_task, attempt.task_id)
        run = self._storage(self.database.get_run, task.run_id)
        self.armarius.require_external_run(run)
        if attempt.status != AttemptStatus.RUNNING or task.status != TaskStatus.RUNNING:
            raise StateError("retrieval requires an active attempt")
        metadata = self._storage(self.database.get_external_task, task.id)
        bundle, files = self.armarius._retrieval_bundle(run, metadata)
        return attempt, task, bundle, files

    def read_task(
        self,
        attempt_id: str,
        path: str,
        start_line: int,
        max_lines: int,
        offset: int,
        max_chars: int,
        end_line: int | None = None,
        anchor: bool = False,
    ):
        with self._retrieval_operation(attempt_id, "task read"):
            return self._read_task(attempt_id, path, start_line, max_lines, offset, max_chars, end_line, anchor)

    def _read_task(
        self,
        attempt_id: str,
        path: str,
        start_line: int,
        max_lines: int,
        offset: int,
        max_chars: int,
        end_line: int | None,
        anchor: bool,
    ):
        attempt, task, bundle, files = self._readable_task(attempt_id)
        if start_line < 1 or not 1 <= max_lines <= 100 or offset < 0 or not 1 <= max_chars <= 8000:
            raise ConfigurationError("invalid read range or size")
        if end_line is not None and end_line < start_line:
            raise ConfigurationError("end line cannot precede start line")
        source = next((item for item in bundle.anchor_map.sources if item.read_path == path), None)
        if source is None:
            source = next((item for item in bundle.anchor_map.sources if item.source_path == path), None)
        if path in _BUNDLE_METADATA:
            source = None
            read_path = bundle.workspace / path
            source_digest = files[path]["digest"]
        elif source is not None and source.text_anchorable:
            read_path = bundle.workspace / source.read_path
            source_digest = source.source_digest
        else:
            raise _unknown_text_path(path, bundle.anchor_map)
        self.armarius._verify_retrieval_file(
            bundle.workspace, files, read_path.relative_to(bundle.workspace).as_posix()
        )
        window = max_lines if end_line is None else min(max_lines, end_line - start_line + 1)
        pieces, next_line, next_offset = _read_text_window(read_path, start_line, window, offset, max_chars)
        if end_line is not None and next_line is not None and next_line > end_line:
            next_line = next_offset = None
        response = bound_read(
            {
                "path": path,
                "source_path": source.source_path if source is not None else None,
                "source_digest": source_digest,
                "lines": pieces,
                "next_line": next_line,
                "next_offset": next_offset,
            },
            attempt_id,
            max_lines,
            max_chars,
            end_line,
            anchor,
            not anchor or source is None or self._retrieval_numbers_like_contract(task.run_id, source, read_path),
        )
        self._record_access(
            task.run_id,
            attempt.id,
            "read",
            {
                "path": path,
                "source_path": response["source_path"],
                "source_digest": source_digest,
                "ranges": [
                    {
                        "line": item["line"],
                        "start_offset": item["offset"],
                        "end_offset": item["offset"] + len(item["text"]),
                    }
                    for item in response["lines"]
                ],
                "next_line": response["next_line"],
                "next_offset": response["next_offset"],
            },
        )
        return response

    def _retrieval_numbers_like_contract(self, run_id: str, source, read_path: Path) -> bool:
        contract = self.armarius.require_evidence_anchor_contract(run_id)
        if contract.line_terminators is not None:
            return True
        # Retrieval splits only at newlines; an earlier contract also split at form feeds and Unicode separators.
        # Those break points are a superset, so the numberings agree exactly when the line counts do.
        with read_path.open(encoding="utf-8") as stream:
            return sum(1 for _ in stream) == source.line_count

    def search_task(
        self,
        attempt_id: str,
        query: str,
        path: str | None,
        cursor: int,
        limit: int,
        context: int = 0,
        include_metadata: bool = False,
    ):
        with self._retrieval_operation(attempt_id, "task search"):
            return self._search_task(attempt_id, query, path, cursor, limit, context, include_metadata)

    def _search_task(
        self,
        attempt_id: str,
        query: str,
        path: str | None,
        cursor: int,
        limit: int,
        context: int,
        include_metadata: bool,
    ):
        attempt, task, bundle, files = self._readable_task(attempt_id)
        if not query or len(query) > 200 or cursor < 0 or not 1 <= limit <= 50 or not 0 <= context <= 3:
            raise ConfigurationError("invalid search query, cursor, limit, or context")
        search_items, path = self._search_items(bundle, files, path, include_metadata)
        matches = []
        total_matches = 0
        for source_path, read_path, source_digest in search_items:
            self.armarius._verify_retrieval_file(
                bundle.workspace, files, read_path.relative_to(bundle.workspace).as_posix()
            )
            for match in _scan_matches(read_path, query, cursor - total_matches, limit - len(matches), context):
                if match is None:
                    total_matches += 1
                    continue
                matches.append({"path": source_path, **match, "source_digest": source_digest})
                total_matches += 1
        response = bound_search(
            matches, total_matches, attempt_id, query, path, cursor, limit, context, include_metadata
        )
        self._record_access(
            task.run_id,
            attempt.id,
            "search",
            {
                "query": query,
                "path": path,
                "include_metadata": include_metadata,
                "matches": [_search_access(item) for item in response["matches"]],
                "next_cursor": response["next_cursor"],
            },
        )
        return response

    @staticmethod
    def _search_items(bundle, files, path: str | None, include_metadata: bool):
        sources = [item for item in bundle.anchor_map.sources if item.text_anchorable]
        source_items = [(item.source_path, bundle.workspace / item.read_path, item.source_digest) for item in sources]
        metadata_items = [(name, bundle.workspace / name, files[name]["digest"]) for name in _BUNDLE_METADATA]
        if path is None:
            # Generated metadata repeats every label and citation, so whole-bundle searches omit it by default.
            return source_items + (metadata_items if include_metadata else []), None
        path_items = {item[0]: item for item in source_items}
        path_items.update((source.read_path, item) for source, item in zip(sources, source_items))
        path_items.update((item[0], item) for item in metadata_items)
        item = path_items.get(path)
        if item is None:
            raise _unknown_text_path(path, bundle.anchor_map)
        return [item], item[0] if path_items[item[0]] == item else path

    def nav_task(
        self,
        attempt_id: str,
        commands: list[str] | None = None,
        query: str | None = None,
        path: str | None = None,
        cursor: int = 0,
        limit: int = 50,
    ):
        with self._retrieval_operation(attempt_id, "task nav"):
            return self._nav_task(attempt_id, commands or [], query, path, cursor, limit)

    def _nav_task(self, attempt_id: str, commands: list[str], query: str | None, path: str | None, cursor, limit):
        attempt, task, bundle, files = self._readable_task(attempt_id)
        if cursor < 0 or not 1 <= limit <= 100 or (query is not None and not 1 <= len(query) <= 200):
            raise ConfigurationError("invalid navigation query, cursor, or limit")
        selected = _navigation_commands(commands)
        source_path = None
        if path is not None:
            # Read paths take priority over a colliding source path, exactly as in task read.
            source = next((item for item in bundle.anchor_map.sources if item.read_path == path), None)
            if source is None:
                source = next((item for item in bundle.anchor_map.sources if item.source_path == path), None)
            if source is None:
                raise _unknown_text_path(path, bundle.anchor_map)
            source_path = source.source_path
        if "navigation.json" not in files:
            raise StateError("this run has no frozen navigation index")
        navigation = self.armarius._verify_retrieval_file(bundle.workspace, files, "navigation.json")
        index = json.loads(navigation.read_text(encoding="utf-8"))
        entries = index["entries"]
        indexed = set(index.get("commands", _LEGACY_NAVIGATION_COMMANDS))
        if selected is not None and not selected <= indexed:
            raise ConfigurationError(
                "this run's navigation index predates table, equation, and quantity entries; "
                "use task search to locate them"
            )
        folded = query.casefold() if query is not None else None
        matched = [
            entry
            for entry in entries
            if (selected is None or entry["command"] in selected)
            and (source_path is None or entry["source_path"] == source_path)
            and (folded is None or folded in entry["value"].casefold())
        ]
        # A repeated filter selects nothing new; keep one of each so continuations stay bounded.
        filters = {"commands": list(dict.fromkeys(commands)), "query": query, "path": path}
        response = bound_nav(
            [_navigation_entry(entry) for entry in matched[cursor : cursor + limit]],
            len(matched),
            dict(Counter(entry["command"] for entry in matched)),
            attempt_id,
            filters,
            cursor,
            limit,
            {item.source_path: index for index, item in enumerate(bundle.anchor_map.sources)},
        )
        self._record_access(
            task.run_id,
            attempt.id,
            "nav",
            {
                **filters,
                # Record the index entries themselves; returned entries may omit fields to fit the bound.
                "entries": [
                    {key: entry[key] for key in ("command", "source_path", "start_line", "end_line")}
                    for entry in matched[cursor : cursor + len(response["entries"])]
                ],
                "next_cursor": response["next_cursor"],
            },
        )
        return response

    def page_task(
        self,
        attempt_id: str,
        page_number: int,
        document: str | None = None,
        scale: float | None = None,
        crop: tuple[float, float, float, float] | None = None,
        text: bool = False,
        offset: int = 0,
        text_digest: str | None = None,
    ):
        _validate_page_view(scale, crop, text, offset, text_digest)
        with self._retrieval_operation(attempt_id, "task page"):
            attempt, task, bundle, files = self._readable_task(attempt_id)
            location, page = self._page_location(bundle, page_number, document)
            if text:
                request = {"number": page_number, "document": document, "offset": offset, "text_digest": text_digest}
                return self._page_text(attempt, task, bundle, files, location, request)
            self.armarius._verify_retrieval_file(bundle.workspace, files, page.read_path)
            response = {**location, "path": str(bundle.workspace / page.read_path), "digest": page.page_digest}
            access = {**location, "read_path": page.read_path}
            if scale is not None or crop is not None:
                view = self._page_view(task.run_id, bundle, files, location["page"], scale or 1.5, crop)
                response.update(path=view.pop("path"), view=view)
                access["view"] = view
            response = require_bounded(response)
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
        views = self.state_dir / "runs" / run_id / "page-views"
        pending = views / ".pending.png"
        render_page_view(pdf, page, scale, crop, pending)
        # A returned path keeps its bytes: views are named by digest, never overwritten, and bounded per run.
        digest = ArtifactStore.digest_file(pending)
        destination = views / f"{digest}.png"
        if destination.exists():
            pending.unlink()
            return {**view, "digest": digest, "path": str(destination)}
        kept = sum(path.stat().st_size for path in views.glob("*.png") if not path.name.startswith("."))
        if kept + pending.stat().st_size > MAX_RUN_PAGE_VIEW_BYTES:
            pending.unlink()
            limit = MAX_RUN_PAGE_VIEW_BYTES // (1024 * 1024)
            raise StateError(f"page views for this run reached the {limit} MiB limit; reuse an earlier view")
        pending.replace(destination)
        return {**view, "digest": digest, "path": str(destination)}

    def _page_text(self, attempt, task, bundle, files, location, request):
        pdf = self.armarius._verify_retrieval_file(bundle.workspace, files, "manuscript.pdf")
        content = page_text(pdf, location["page"])
        offset = request["offset"]
        response = page_text_fragment(
            location, content, offset, attempt.id, request["number"], request["document"], request["text_digest"]
        )
        self._record_access(
            task.run_id,
            attempt.id,
            "page_text",
            {
                **location,
                "text_digest": response["text_digest"],
                "start_offset": offset,
                "end_offset": offset + len(response["text"]),
            },
        )
        return response

    def export_task(self, attempt_id: str, directory: str | Path):
        with self._retrieval_operation(attempt_id, "task export"):
            return self._export_task(attempt_id, directory)

    def _export_task(self, attempt_id: str, directory: str | Path):
        attempt, task, bundle, files = self._readable_task(attempt_id)
        root = export_root(directory)
        names = self._export_names(bundle, files)
        listing = [{"path": name, "digest": files[name]["digest"]} for name in names]
        response = bound_export(attempt.id, str(root), listing, sum(files[name]["size"] for name in names))

        def record() -> None:
            payload = {"directory": str(root), "bundle_digest": attempt.bundle_digest, "files": listing}
            self._record_access(task.run_id, attempt.id, "export", payload)

        export_files(root, names, lambda name: self._frozen_bytes(bundle, files, name), record)
        return response

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

    @staticmethod
    def _line_spans(numbers: set[int]) -> list[dict[str, int]]:
        spans: list[dict[str, int]] = []
        for number in sorted(numbers):
            if spans and number == spans[-1]["end_line"] + 1:
                spans[-1]["end_line"] = number
            else:
                spans.append({"start_line": number, "end_line": number})
        return spans

    @staticmethod
    def _missing_line_spans(declared: list[tuple[int, int]], returned: set[int]) -> list[dict[str, int]]:
        merged: list[dict[str, int]] = []
        for start, end in sorted(declared):
            if merged and start <= merged[-1]["end_line"] + 1:
                merged[-1]["end_line"] = max(merged[-1]["end_line"], end)
            else:
                merged.append({"start_line": start, "end_line": end})
        missing = []
        read_lines = iter(sorted(returned))
        next_read = next(read_lines, None)
        for span in merged:
            cursor = span["start_line"]
            while next_read is not None and next_read < cursor:
                next_read = next(read_lines, None)
            while next_read is not None and next_read <= span["end_line"]:
                if next_read > cursor:
                    missing.append({"start_line": cursor, "end_line": next_read - 1})
                cursor = next_read + 1
                next_read = next(read_lines, None)
            if cursor <= span["end_line"]:
                missing.append({"start_line": cursor, "end_line": span["end_line"]})
        return missing

    @staticmethod
    def _exported_sources(events: list[Event], read_paths: dict[str, Any]) -> tuple[int, set[str]]:
        count = 0
        exported: set[str] = set()
        for event in events:
            if event.event_type != "tool.export":
                continue
            count += 1
            for item in event.payload["files"]:
                source = read_paths.get(item["path"])
                if source is not None and item["digest"] == source.source_digest:
                    exported.add(source.source_path)
        return count, exported

    @classmethod
    def _review_coverage_audit(cls, scope: dict[str, Any], events: list[Event], sources) -> dict[str, Any]:
        source_index = {source.source_path: source for source in sources}
        read_paths = {source.read_path: source for source in sources}
        read_lines: dict[str, set[int]] = {}
        search_matches: dict[str, set[int]] = {}
        pages: set[int] = set()
        for event in events:
            if event.event_type == "tool.read":
                path = event.payload.get("source_path", event.payload["path"])
                if path is None:
                    continue
                source = source_index.get(path) or read_paths.get(path)
                if source is not None:
                    if event.payload["source_digest"] != source.source_digest:
                        continue
                    path = source.source_path
                read_lines.setdefault(path, set()).update(
                    item["line"]
                    for item in event.payload["ranges"]
                    if item["end_offset"] > item["start_offset"] or item["start_offset"] == 0
                )
            elif event.event_type == "tool.search":
                for match in event.payload["matches"]:
                    source = source_index.get(match["path"])
                    if source is not None and match["source_digest"] != source.source_digest:
                        continue
                    search_matches.setdefault(match["path"], set()).add(match["line"])
            elif event.event_type == "tool.page":
                pages.add(event.payload["page"])
        declared_lines: dict[str, list[tuple[int, int]]] = {}
        declared_pages: set[int] = set()
        not_comparable = []
        for area in scope["checked"]:
            path = area["source_path"]
            if path == "manuscript.pdf":
                declared_pages.add(area["page"])
            elif source_index[path].text_anchorable:
                source = source_index[path]
                start = area.get("start_line", 1)
                end = area.get("end_line", source.line_count)
                declared_lines.setdefault(path, []).append((start, end))
            else:
                not_comparable.append(area)
        missing = [
            {"source_path": path, **span}
            for path, lines in sorted(declared_lines.items())
            for span in cls._missing_line_spans(lines, read_lines.get(path, set()))
        ]
        export_count, exported = cls._exported_sources(events, read_paths)
        return {
            "read_lines": [
                {"source_path": path, "ranges": cls._line_spans(lines)}
                for path, lines in sorted(read_lines.items())
                if lines
            ],
            "search_matches": [
                {"source_path": path, "lines": sorted(lines)} for path, lines in sorted(search_matches.items()) if lines
            ],
            "pages_returned": sorted(pages),
            "declared_without_task_read": missing,
            "exports": export_count,
            "exported_sources": sorted(exported),
            "exported": [span for span in missing if span["source_path"] in exported],
            "declared_without_access": [span for span in missing if span["source_path"] not in exported],
            "declared_without_task_page": sorted(declared_pages - pages),
            "not_comparable": not_comparable,
        }

    def _collect_review_coverage_audits(
        self, run_view: dict[str, Any], review_scopes: list[dict[str, Any]], events: list[Event]
    ) -> list[dict[str, Any]]:
        if not review_scopes:
            return []
        sources = self.armarius._bundle_for_run(run_view["run"]).anchor_map.sources
        audits = []
        for item in run_view["tasks"]:
            scopes = [scope for scope in review_scopes if scope["task_id"] == item["task"].id and scope["scope"]]
            if not scopes:
                continue
            latest = scopes[-1]
            latest_ordinal = next(attempt.ordinal for attempt in item["attempts"] if attempt.id == latest["attempt_id"])
            attempt_ids = {attempt.id for attempt in item["attempts"] if attempt.ordinal <= latest_ordinal}
            access_events = [event for event in events if event.entity_id in attempt_ids]
            audits.append(
                {
                    "task_id": item["task"].id,
                    "attempt_id": latest["attempt_id"],
                    "role": latest["role"],
                    **self._review_coverage_audit(latest["scope"], access_events, sources),
                }
            )
        return audits

    def read_report(self, run_id: str, part: str, offset: int = 0, report_digest: str | None = None):
        if part not in REPORT_PARTS:
            raise ConfigurationError(f"unknown report part: {part}")
        run = self._storage(self.database.get_run, run_id)
        with self._run_operation(run.id, "run report"):
            report = self.render_report(run_id, "json")
            return report_fragment(run_id, report, part, offset, report_digest)

    def render_report(self, run_id: str, format: str) -> str | dict[str, Any]:
        if format not in {"markdown", "json"}:
            raise ConfigurationError("report format must be markdown or json")
        run_view = self.get_run(run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        events = self._storage(self.database.list_events, run_id)
        external_run = run_view["run"].frozen_config.get("execution") == "external"
        access_counts = {}
        export_counts = Counter(event.entity_id for event in events if event.event_type == "tool.export")
        for event in events:
            if event.event_type in {"tool.read", "tool.search", "tool.page", "tool.nav", "tool.page_text"}:
                counts = access_counts.setdefault(event.entity_id, dict.fromkeys(_RETURN_KINDS, 0))
                counts[event.event_type.removeprefix("tool.")] += 1
        validation_reports = []
        review_scopes = []
        review_claim_checks = []
        review_tool_access = []
        review_outputs = []
        for item in run_view["tasks"]:
            task = item["task"]
            schema_kind = {
                "review": "review",
                "review_transcription": "visual_transcription",
                "revision": "revision",
                "verification": "verification",
                "verification_transcription": "visual_transcription",
            }.get(task.stage)
            if task.stage == "review" and external_run:
                metadata = self._storage(self.database.get_external_task, task.id)
                schema_kind = metadata["schema_kind"]
            for attempt in item["attempts"]:
                if task.stage == "review":
                    review_tool_access.append(
                        {
                            "task_id": task.id,
                            "attempt_id": attempt.id,
                            "role": task.role.value,
                            "status": attempt.status.value,
                            "returns": (
                                access_counts.get(attempt.id, dict.fromkeys(_RETURN_KINDS, 0)) if external_run else None
                            ),
                            "exports": export_counts.get(attempt.id, 0) if external_run else None,
                        }
                    )
                if task.stage == "review" and attempt.status == AttemptStatus.COMPLETED:
                    scope = None
                    if external_run:
                        schema = self.armarius._load_schema_artifact(
                            run_view["run"], metadata["schema_kind"], attempt.schema_digest
                        )
                        model = self.armarius._output_model_for_schema(run_view["run"], schema_kind, schema)
                        if issubclass(model, ScopedReviewOutput):
                            try:
                                output_text = self.artifacts.get_bytes(attempt.output_artifact_digest).decode("utf-8")
                            except (ArtifactError, UnicodeDecodeError) as exc:
                                raise InfrastructureError(
                                    f"completed review attempt {attempt.id} has unreadable output"
                                ) from exc
                            output, issues = self.armarius._parse_and_validate_output(model, output_text, lambda _: [])
                            if output is None or issues:
                                raise InfrastructureError(
                                    f"completed review attempt {attempt.id} has invalid scope output"
                                )
                            scope = output.scope.model_dump(mode="json", exclude_none=True)
                            review_outputs.append(self._submitted_review_output(task.id, output))
                            if isinstance(output, ScientificReviewOutput):
                                review_claim_checks.append(
                                    {
                                        "task_id": task.id,
                                        "attempt_id": attempt.id,
                                        "submitted_findings": [
                                            finding.model_dump(mode="json", exclude_none=True)
                                            for finding in output.findings
                                        ],
                                        "claim_checks": [
                                            check.model_dump(mode="json", exclude_none=True)
                                            for check in output.claim_checks
                                        ],
                                        **(
                                            {
                                                "claim_inventory": [
                                                    entry.model_dump(mode="json", exclude_none=True)
                                                    for entry in output.claim_inventory
                                                ]
                                            }
                                            if isinstance(output, InventoriedScientificReviewOutput)
                                            else {}
                                        ),
                                    }
                                )
                    review_scopes.append(
                        {"task_id": task.id, "attempt_id": attempt.id, "role": task.role.value, "scope": scope}
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
        review_coverage_audit = (
            self._collect_review_coverage_audits(run_view, review_scopes, events) if external_run else []
        )
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
                [item["finding"] for item in finding_records], review_outputs, review_claim_checks
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
            "review_scopes": review_scopes,
            "review_claim_checks": review_claim_checks,
            "review_tool_access": review_tool_access,
            "review_coverage_audit": review_coverage_audit,
            "gate": self.evaluate_gate(run_id),
        }
        if format == "json":
            return _plain(payload)
        return self._markdown_report(payload)

    @staticmethod
    def _submitted_review_output(task_id: str, output: ScopedReviewOutput) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "submitted_findings": [finding.model_dump(mode="json", exclude_none=True) for finding in output.findings],
        }

    def _report_run(self, run: Run) -> Run | dict[str, Any]:
        brief = self.armarius.review_brief(run)
        return run if brief is None else {**_plain(run), "review_brief": brief["content"]}

    def evaluate_gate(self, run_id: str) -> dict[str, Any]:
        run = self._storage(self.database.get_run, run_id)
        tasks = self._storage(self.database.list_tasks, run_id)
        findings = self._storage(self.database.list_findings, run_id)
        patches = self._storage(self.database.list_patches, run_id)
        required_roles = set(run.frozen_config["profile_roles"])
        completed_roles = set()
        review_errors = []
        for task in tasks:
            if task.stage != "review" or task.status != TaskStatus.COMPLETED:
                continue
            if run.frozen_config.get("execution") != "external":
                completed_roles.add(task.role.value)
                continue
            try:
                completion = self._storage(self.armarius.review_completion, task)
            except InfrastructureError as exc:
                review_errors.append(str(exc))
                continue
            if completion in {"complete", "unreported"} or (
                completion in {"partial", "unknown"}
                and run.status
                in {
                    RunStatus.REVISING,
                    RunStatus.AWAITING_PATCH_APPROVAL,
                    RunStatus.VERIFYING,
                    RunStatus.READY_TO_APPLY,
                    RunStatus.COMPLETED,
                }
            ):
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
    def _markdown_claim_checks(items: list[dict[str, Any]]) -> list[str]:
        lines = ["", "## Scientific claim checks", ""]
        if not items:
            return [*lines, "- None"]
        for item in items:
            lines.append(f"- `{item['attempt_id']}`: {len(item['claim_checks'])} checks")
            for index, finding in enumerate(item["submitted_findings"]):
                lines.append(f"  - submitted finding [{index}]: {finding['severity']} — {finding['title']}")
            for entry in item.get("claim_inventory", []):
                location = ScriptoriumService._markdown_location(entry["claim_anchor"])
                status = (
                    f"claim checks {entry['check_indices']}"
                    if entry["check_indices"]
                    else f"not checked: {entry['not_checked_reason']}"
                )
                lines.append(
                    f"  - inventoried {entry['prominence']} claim at `{location}`: {entry['claim']} — {status}"
                )
            for index, check in enumerate(item["claim_checks"]):
                lines.append(
                    f"  - [{index}] {check['claim']} — {check['assessment']}; question: {check['critical_question']}; "
                    f"countercheck: {check['countercheck']}; submitted finding indices: {check['finding_indices']}"
                )
                lines.extend(ScriptoriumService._markdown_claim_judgment(check))
                for evidence in check["evidence"]:
                    lines.append(f"    - evidence: `{ScriptoriumService._markdown_location(evidence)}`")
        lines.append("Claim checks are reviewer declarations; valid anchors do not establish scientific correctness.")
        return lines

    @staticmethod
    def _markdown_location(anchor: dict[str, Any]) -> str:
        if "page" in anchor:
            return f"{anchor['source_path']}:page {anchor['page']}"
        return f"{anchor['source_path']}:{anchor['start_line']}-{anchor['end_line']}"

    @staticmethod
    def _markdown_claim_judgment(check: dict[str, Any]) -> list[str]:
        if "question_answer" not in check:
            return []
        location = ScriptoriumService._markdown_location(check["claim_anchor"])
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
    def _markdown_coverage_audit(items: list[dict[str, Any]]) -> list[str]:
        lines = ["", "## Declared coverage versus task-tool returns", ""]
        if not items:
            lines.append("- None")
        for audit in items:
            lines.append(f"- `{audit['attempt_id']}` / {audit['role']} (returns through this attempt):")
            for item in audit["read_lines"]:
                spans = ", ".join(f"{span['start_line']}-{span['end_line']}" for span in item["ranges"])
                lines.append(f"  - task read returned line fragments: `{item['source_path']}:{spans}`")
            for item in audit["search_matches"]:
                numbers = ", ".join(str(number) for number in item["lines"])
                lines.append(f"  - task search returned matches: `{item['source_path']}:{numbers}`")
            if audit["pages_returned"]:
                numbers = ", ".join(str(number) for number in audit["pages_returned"])
                lines.append(f"  - task page returned paths: {numbers}")
            if audit["exports"]:
                lines.append(f"  - task export recorded {audit['exports']} time(s); exported files are not reads")
            for area in audit["declared_without_task_read"]:
                state = (
                    "exported but not returned by task read"
                    if area in audit["exported"]
                    else "without task read return"
                )
                lines.append(
                    f"  - declared checked {state}: `{area['source_path']}:{area['start_line']}-{area['end_line']}`"
                )
            for page in audit["declared_without_task_page"]:
                lines.append(f"  - declared checked without task page return: `manuscript.pdf:page {page}`")
            for area in audit["not_comparable"]:
                lines.append(f"  - non-text source not comparable to task read: `{area['source_path']}`")
        lines.append(
            "Read ranges show lines with returned fragments, not complete lines; exact character offsets remain in "
            "the tool events. Search excerpts do not count as full reads. Host file or image access is not logged, "
            "and a returned page path does not prove that an image was viewed."
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
        lines.extend(["", "## Reviewer-declared scope", ""])
        if plain["review_scopes"]:
            for item in plain["review_scopes"]:
                scope = item["scope"]
                if scope is None:
                    lines.append(f"- `{item['attempt_id']}` / {item['role']}: scope not reported by frozen contract")
                    continue
                lines.append(
                    f"- `{item['attempt_id']}` / {item['role']}: declared `{scope['completion']}`; "
                    f"checked {len(scope['checked'])}, outstanding {len(scope['outstanding'])}"
                )
                for group in ("checked", "outstanding"):
                    for area in scope[group]:
                        location = area["source_path"]
                        if "page" in area:
                            location += f":page {area['page']}"
                        elif "start_line" in area:
                            location += f":{area['start_line']}-{area['end_line']}"
                        lines.append(f"  - {group}: `{location}`")
                for limitation in scope["limitations"]:
                    lines.append(f"  - limitation: {limitation}")
        else:
            lines.append("- None")
        lines.extend(ScriptoriumService._markdown_claim_checks(plain["review_claim_checks"]))
        lines.extend(["", "## Observed review task-tool returns", ""])
        if plain["review_tool_access"]:
            for item in plain["review_tool_access"]:
                counts = item["returns"]
                if counts is None:
                    lines.append(f"- `{item['attempt_id']}` / {item['status']}: unavailable for legacy execution")
                else:
                    lines.append(
                        f"- `{item['attempt_id']}` / {item['status']}: "
                        f"read {counts['read']}, search {counts['search']}, page {counts['page']}, "
                        f"nav {counts.get('nav', 0)}, page text {counts.get('page_text', 0)}, "
                        f"export {item.get('exports', 0)}"
                    )
        else:
            lines.append("- None")
        lines.append("Scope is model-declared; tool returns do not prove inspection.")
        lines.extend(ScriptoriumService._markdown_coverage_audit(plain["review_coverage_audit"]))
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
        lines.extend(["", "## Findings", ""])
        if plain["findings"]:
            for item in plain["findings"]:
                finding = item["finding"]
                lines.append(f"- `{finding['id']}` — {finding['severity']} / {finding['status']}: {finding['title']}")
        else:
            lines.append("- None")
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
_CONTEXT_CHARS = 200
_RETURN_KINDS = ("read", "search", "page", "nav", "page_text")
MAX_RUN_PAGE_VIEW_BYTES = 512 * 1024 * 1024


def _unknown_text_path(path: str, anchor_map) -> ConfigurationError:
    text_paths = [item.read_path for item in anchor_map.sources if item.text_anchorable]
    candidates = sorted(
        [*text_paths, *_BUNDLE_METADATA],
        key=lambda candidate: (-difflib.SequenceMatcher(None, path, candidate).ratio(), candidate),
    )[:3]
    message = f"path is not a text source in the frozen bundle: {path[:200]}"
    if any(item.source_path == path or item.read_path == path for item in anchor_map.sources):
        message += "; it is a frozen non-text source, so inspect the rendered pages with task page"
    return ConfigurationError(f"{message}; closest text read paths: {', '.join(candidates)}")


NAVIGATION_GROUPS = {
    "heading": ("part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"),
    "reference": ("ref", "eqref", "pageref", "autoref", "cref", "Cref"),
    "citation": ("cite", "citep", "citet", "autocite", "parencite", "textcite"),
    "label": ("label",),
    "caption": ("caption",),
    "graphics": ("includegraphics",),
}
# Indexes written before table, equation, and quantity entries carry no command list and hold only these.
_LEGACY_NAVIGATION_COMMANDS = frozenset(name for names in NAVIGATION_GROUPS.values() for name in names)
NAVIGATION_GROUPS.update(
    table=TABLE_ENVIRONMENTS,
    equation=EQUATION_ENVIRONMENTS,
    quantity=("quantity", *QUANTITY_COMMANDS),
)
_NAVIGATION_VALUE_CHARS = 500
_NAVIGATION_CANDIDATES = 10


def _navigation_commands(commands: list[str]) -> set[str] | None:
    if not commands:
        return None
    known = {name for names in NAVIGATION_GROUPS.values() for name in names}
    selected: set[str] = set()
    for command in commands:
        if command in NAVIGATION_GROUPS:
            selected.update(NAVIGATION_GROUPS[command])
        elif command in known:
            selected.add(command)
        else:
            choices = ", ".join([*NAVIGATION_GROUPS, *sorted(known - set(NAVIGATION_GROUPS))])
            raise ConfigurationError(f"unknown navigation command {command[:50]!r}; use one of: {choices}")
    return selected


def _navigation_entry(entry: dict[str, Any]) -> dict[str, Any]:
    # Cut long literals and long candidate lists; the response bound trims candidates further by encoded size.
    trimmed = dict(entry)
    if len(entry["value"]) > _NAVIGATION_VALUE_CHARS:
        trimmed.update(value=entry["value"][:_NAVIGATION_VALUE_CHARS], value_truncated=True)
    candidates = entry.get("candidate_paths") or []
    if len(candidates) > _NAVIGATION_CANDIDATES:
        trimmed.update(
            candidate_paths=candidates[:_NAVIGATION_CANDIDATES],
            candidate_count=len(candidates),
            candidate_paths_truncated=True,
        )
    return trimmed


def _validate_page_view(scale, crop, text: bool, offset: int, text_digest: str | None) -> None:
    if text and (scale is not None or crop is not None):
        raise ConfigurationError("--text cannot be combined with --scale or --crop")
    if (offset or text_digest is not None) and not text:
        raise ConfigurationError("--offset and --text-digest require --text")
    if offset < 0:
        raise ConfigurationError("offset must not be negative")
    if scale is not None and not 0.5 <= scale <= 4.0:
        raise ConfigurationError("scale must be between 0.5 and 4.0")
    if crop is not None:
        x0, y0, x1, y1 = crop
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ConfigurationError("crop must be x0,y0,x1,y1 fractions with 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")


def _scan_matches(path: Path, query: str, skip: int, take: int, context: int):
    """Yield one item per match: a match record while inside the requested page, otherwise None."""
    folded_query = query.casefold()
    before: deque[dict[str, Any]] = deque(maxlen=context)
    awaiting: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source_file:
        for number, line in enumerate(source_file, 1):
            line = line.removesuffix("\n")
            if context:
                awaiting = _attach_following(awaiting, number, line, context)
            folded = line.casefold()
            source_offsets = (
                [index for index, character in enumerate(line) for _ in character.casefold()]
                if len(folded) != len(line)
                else None
            )
            pending = []
            position = folded.find(folded_query)
            while position >= 0:
                if skip <= 0 < take:
                    source_position = source_offsets[position] if source_offsets is not None else position
                    excerpt_start = max(0, source_position - 100)
                    match = {"line": number, "column": source_position + 1}
                    match["excerpt"] = line[excerpt_start : excerpt_start + 300]
                    if context:
                        match.update(before=list(before), after=[])
                    pending.append(match)
                    take -= 1
                else:
                    yield None
                skip -= 1
                position = folded.find(folded_query, position + max(1, len(folded_query)))
            awaiting.extend(pending)
            if context:
                before.append({"line": number, "text": line[:_CONTEXT_CHARS]})
            yield from pending


def _attach_following(awaiting: list[dict[str, Any]], number: int, line: str, context: int):
    for match in awaiting:
        match["after"].append({"line": number, "text": line[:_CONTEXT_CHARS]})
    return [match for match in awaiting if len(match["after"]) < context]


def _search_access(match: dict[str, Any]) -> dict[str, Any]:
    access = {"path": match["path"], "line": match["line"], "source_digest": match["source_digest"]}
    if "before" in match:
        numbers = [match["line"], *(item["line"] for item in (*match["before"], *match["after"]))]
        access["context"] = {"start_line": min(numbers), "end_line": max(numbers)}
    return access


def _read_text_window(path: Path, start_line: int, max_lines: int, offset: int, max_chars: int):
    pieces = []
    remaining = max_chars
    next_line = next_offset = None
    with path.open(encoding="utf-8") as source:
        current_line = 1
        while current_line < start_line:
            fragment = source.readline(8192)
            if not fragment:
                raise ConfigurationError("start line is beyond the source")
            if fragment.endswith("\n"):
                current_line += 1

        for number in range(start_line, start_line + max_lines):
            if number == start_line:
                start = source.tell()
                if not source.read(1):
                    raise ConfigurationError("start line is beyond the source")
                source.seek(start)
            position = offset if number == start_line else 0
            skipped = 0
            while skipped < position:
                fragment = source.readline(min(8192, position - skipped))
                if not fragment or fragment.endswith("\n"):
                    raise ConfigurationError("offset is beyond the line")
                skipped += len(fragment)

            fragment = source.readline(remaining + 1)
            if not fragment:
                if number == start_line and position == 0:
                    raise ConfigurationError("start line is beyond the source")
                if number == start_line:
                    pieces.append({"line": number, "offset": position, "text": ""})
                break
            ended = fragment.endswith("\n")
            content = fragment[:-1] if ended else fragment
            if len(content) > remaining:
                pieces.append({"line": number, "offset": position, "text": content[:remaining]})
                next_line, next_offset = number, position + remaining
                break
            pieces.append({"line": number, "offset": position, "text": content})
            remaining -= len(content)
            if not ended:
                break
            if remaining == 0 or number == start_line + max_lines - 1:
                if source.read(1):
                    next_line, next_offset = number + 1, 0
                break
    return pieces, next_line, next_offset


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
