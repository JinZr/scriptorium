from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
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

from .artifacts import ArtifactError, ArtifactStore
from .config import find_repo, load_project_config, reject_legacy_local_config, validate_ready
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
    digest_json,
    new_id,
    utc_now,
)
from .errors import ConfigurationError, InfrastructureError, NotFoundError, StateError
from .manuscript import ManuscriptManager
from .schemas import ScopedReviewOutput
from .storage import ConflictError, Database, NotFoundError as StorageNotFoundError, StorageError
from .workflow import Armarius


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
                    shutil.copytree(snapshot, build_workspace, symlinks=True)
                    build = self.manuscript.build(build_workspace, project.manuscript)
                    self.manuscript.validate_build_sources(build, sources)
                    if not build.pdf_path.is_file():
                        raise InfrastructureError(f"LaTeX build did not create {build.pdf_path.name}")
                except (InfrastructureError, OSError) as exc:
                    check("manuscript_compile", False, str(exc), "infrastructure")
                else:
                    check(
                        "manuscript_compile",
                        True,
                        f"compiled {project.manuscript.main} from {frozen_revision.commit_sha}; build-only inputs: "
                        + (
                            ", ".join(item.path for item in (build.compiler_inputs or ()) if item.kind == "build")
                            or "none"
                        ),
                    )

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
        }

    async def start_run(self, revision: str, profile: str) -> dict[str, Any]:
        reject_legacy_local_config(self.repo)
        run_id = new_id("run")
        with self._run_operation(run_id, "run start"):
            self._storage(self.database.ensure_external_schema)
            await self.armarius.start_run(revision, profile, run_id=run_id)
            return self.get_run(run_id)

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
            return self.list_tasks(run_id)

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
            next_actions.append(
                {
                    "command": "patch show",
                    "patch_id": view["patch_ids"][-1],
                    "requires_human_decision": True,
                }
            )
        elif view["run"].status == RunStatus.READY_TO_APPLY:
            next_actions.append(
                {
                    "command": "patch apply",
                    "patch_id": view["patch_ids"][-1],
                    "requires_human_decision": True,
                }
            )
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
        self._record_access(
            run.id,
            attempt.id,
            "show",
            {
                "prompt_digest": attempt.prompt_digest,
                "schema_digest": metadata["schema_digest"],
                "bundle_digest": metadata["bundle_digest"],
            },
        )
        return {
            "run_id": run.id,
            "task": task,
            "attempt": attempt,
            "prompt": prompt,
            "schema": schema,
            "input_digest": digest_json(
                {
                    "prompt_digest": attempt.prompt_digest,
                    "schema_digest": attempt.schema_digest,
                    "bundle_digest": attempt.bundle_digest,
                }
            ),
            "bundle_digest": metadata["bundle_digest"],
            "bundle_path": str(bundle.workspace),
            "navigation_digest": ArtifactStore.digest_file(bundle.workspace / "navigation.json"),
            "source_map": bundle.anchor_map.model_dump(mode="json"),
        }

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

    def read_task(self, attempt_id: str, path: str, start_line: int, max_lines: int, offset: int, max_chars: int):
        with self._retrieval_operation(attempt_id, "task read"):
            return self._read_task(attempt_id, path, start_line, max_lines, offset, max_chars)

    def _read_task(self, attempt_id: str, path: str, start_line: int, max_lines: int, offset: int, max_chars: int):
        attempt, task, bundle, files = self._readable_task(attempt_id)
        if start_line < 1 or not 1 <= max_lines <= 100 or offset < 0 or not 1 <= max_chars <= 8000:
            raise ConfigurationError("invalid read range or size")
        source = next((item for item in bundle.anchor_map.sources if item.source_path == path), None)
        if source is None:
            source = next((item for item in bundle.anchor_map.sources if item.read_path == path), None)
        if path in {"manifest.json", "navigation.json", "source-map.json"}:
            read_path = bundle.workspace / path
            source_digest = files[path]["digest"]
        elif source is not None and source.text_anchorable:
            read_path = bundle.workspace / source.read_path
            source_digest = source.source_digest
        else:
            raise ConfigurationError("path is not a text source in the frozen bundle")
        self.armarius._verify_retrieval_file(
            bundle.workspace, files, read_path.relative_to(bundle.workspace).as_posix()
        )
        pieces, next_line, next_offset = _read_text_window(read_path, start_line, max_lines, offset, max_chars)
        self._record_access(
            task.run_id,
            attempt.id,
            "read",
            {
                "path": path,
                "source_digest": source_digest,
                "ranges": [
                    {
                        "line": item["line"],
                        "start_offset": item["offset"],
                        "end_offset": item["offset"] + len(item["text"]),
                    }
                    for item in pieces
                ],
                "next_line": next_line,
                "next_offset": next_offset,
            },
        )
        return {
            "path": path,
            "source_digest": source_digest,
            "lines": pieces,
            "next_line": next_line,
            "next_offset": next_offset,
        }

    def search_task(self, attempt_id: str, query: str, path: str | None, cursor: int, limit: int):
        with self._retrieval_operation(attempt_id, "task search"):
            return self._search_task(attempt_id, query, path, cursor, limit)

    def _search_task(self, attempt_id: str, query: str, path: str | None, cursor: int, limit: int):
        attempt, task, bundle, files = self._readable_task(attempt_id)
        if not query or len(query) > 200 or cursor < 0 or not 1 <= limit <= 50:
            raise ConfigurationError("invalid search query, cursor, or limit")
        sources = [item for item in bundle.anchor_map.sources if item.text_anchorable]
        search_items = [(item.source_path, bundle.workspace / item.read_path, item.source_digest) for item in sources]
        for name in ("manifest.json", "navigation.json", "source-map.json"):
            read_path = bundle.workspace / name
            search_items.append((name, read_path, files[name]["digest"]))
        if path is not None:
            search_items = [item for item in search_items if item[0] == path]
            if not search_items:
                raise ConfigurationError("path is not a text source in the frozen bundle")
        matches = []
        total_matches = 0
        folded_query = query.casefold()
        for source_path, read_path, source_digest in search_items:
            self.armarius._verify_retrieval_file(
                bundle.workspace, files, read_path.relative_to(bundle.workspace).as_posix()
            )
            with read_path.open(encoding="utf-8") as source_file:
                for number, line in enumerate(source_file, 1):
                    line = line.removesuffix("\n")
                    folded = line.casefold()
                    source_offsets = (
                        [index for index, character in enumerate(line) for _ in character.casefold()]
                        if len(folded) != len(line)
                        else None
                    )
                    position = folded.find(folded_query)
                    while position >= 0:
                        if cursor <= total_matches < cursor + limit:
                            source_position = source_offsets[position] if source_offsets is not None else position
                            excerpt_start = max(0, source_position - 100)
                            matches.append(
                                {
                                    "path": source_path,
                                    "line": number,
                                    "column": source_position + 1,
                                    "excerpt": line[excerpt_start : excerpt_start + 300],
                                    "source_digest": source_digest,
                                }
                            )
                        total_matches += 1
                        position = folded.find(folded_query, position + max(1, len(folded_query)))
        next_cursor = cursor + len(matches) if cursor + len(matches) < total_matches else None
        self._record_access(
            task.run_id,
            attempt.id,
            "search",
            {
                "query": query,
                "path": path,
                "matches": [{"path": item["path"], "line": item["line"]} for item in matches],
                "next_cursor": next_cursor,
            },
        )
        return {"matches": matches, "total_matches": total_matches, "next_cursor": next_cursor}

    def page_task(self, attempt_id: str, page_number: int):
        with self._retrieval_operation(attempt_id, "task page"):
            return self._page_task(attempt_id, page_number)

    def _page_task(self, attempt_id: str, page_number: int):
        attempt, task, bundle, files = self._readable_task(attempt_id)
        page = next((item for item in bundle.anchor_map.compiled_pdf.pages if item.page == page_number), None)
        if page is None:
            raise ConfigurationError("page is outside the frozen PDF")
        self.armarius._verify_retrieval_file(bundle.workspace, files, page.read_path)
        self._record_access(task.run_id, attempt.id, "page", {"page": page_number, "read_path": page.read_path})
        return {"page": page_number, "path": str(bundle.workspace / page.read_path), "digest": page.page_digest}

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
        initial = self._storage(self.database.get_finding, finding_id)
        with self._run_operation(initial.run_id, "finding decide"):
            finding = self._storage(self.database.get_finding, finding_id)
            run = self._storage(self.database.get_run, finding.run_id)
            self.armarius.require_external_run(run)
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                raise StateError(f"findings cannot be decided while run is {run.status.value}")
            if run.status != RunStatus.AWAITING_DECISION and decision != "waive":
                raise StateError("only a later explicit waiver is allowed after the decision stage")
            record = self._storage(self.database.decide_finding, finding_id, decision, reason)
            updated = self._storage(self.database.get_finding, finding_id)
            if finding.status == FindingStatus.CONFIRMED and updated.status == FindingStatus.WAIVED:
                self._storage(self.armarius.invalidate_stale_tasks, run.id)
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
        events = self._storage(self.database.list_events, run_id)
        external_run = run_view["run"].frozen_config.get("execution") == "external"
        access_counts = {}
        for event in events:
            if event.event_type in {"tool.read", "tool.search", "tool.page"}:
                counts = access_counts.setdefault(event.entity_id, {"read": 0, "search": 0, "page": 0})
                counts[event.event_type.removeprefix("tool.")] += 1
        validation_reports = []
        review_scopes = []
        review_tool_access = []
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
                if task.stage == "review":
                    review_tool_access.append(
                        {
                            "task_id": task.id,
                            "attempt_id": attempt.id,
                            "role": task.role.value,
                            "status": attempt.status.value,
                            "returns": (
                                access_counts.get(attempt.id, {"read": 0, "search": 0, "page": 0})
                                if external_run
                                else None
                            ),
                        }
                    )
                if task.stage == "review" and attempt.status == AttemptStatus.COMPLETED:
                    scope = None
                    if external_run:
                        metadata = self._storage(self.database.get_external_task, task.id)
                        schema = self.armarius._load_schema_artifact(
                            run_view["run"], metadata["schema_kind"], attempt.schema_digest
                        )
                        model = self.armarius._output_model_for_schema(run_view["run"], "review", schema)
                        if model is ScopedReviewOutput:
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
            "events": events,
            "validation_reports": validation_reports,
            "review_scopes": review_scopes,
            "review_tool_access": review_tool_access,
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
            if completion in {"complete", "unreported"}:
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
        cost = "unknown" if run["estimated_cost_usd"] is None else f"${run['estimated_cost_usd']:.6f}"
        lines = [
            f"# Scriptorium run {run['id']}",
            "",
            f"- Status: `{run['status']}`",
            f"- Commit: `{run['commit_sha']}`",
            f"- Profile: `{run['profile']}`",
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
        lines.extend(["", "## Observed review task-tool returns", ""])
        if plain["review_tool_access"]:
            for item in plain["review_tool_access"]:
                counts = item["returns"]
                if counts is None:
                    lines.append(f"- `{item['attempt_id']}` / {item['status']}: unavailable for legacy execution")
                else:
                    lines.append(
                        f"- `{item['attempt_id']}` / {item['status']}: "
                        f"read {counts['read']}, search {counts['search']}, page {counts['page']}"
                    )
        else:
            lines.append("- None")
        lines.append("Scope is model-declared; tool returns do not prove inspection.")
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
