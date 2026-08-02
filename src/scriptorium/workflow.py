from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from importlib import resources
import json
import math
from pathlib import Path
import shutil
from typing import Any, Callable

import fitz
from pydantic import ValidationError

from .artifacts import ArtifactStore
from .config import LocalConfig, ManuscriptConfig, ProjectConfig, RouteConfig, load_project_config, validate_ready
from .domain import (
    AgentRole,
    Attempt,
    AttemptStatus,
    Event,
    Finding,
    FindingSeverity,
    FindingStatus,
    Patch,
    PatchStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    Verification,
    VerificationResult,
    canonical_json,
    digest_json,
    new_id,
)
from .errors import InfrastructureError, StateError
from .manuscript import BuildResult, FrozenRevision, ManuscriptBundle, ManuscriptManager, SourceFile
from .runtime import RUNTIME_SDK_VERSIONS, AgentRuntime, RuntimeUnavailable
from .schemas import (
    ExactEdit,
    ReviewOutput,
    RevisionOutput,
    VerificationOutput,
    VisualTranscriptionOutput,
    output_schema,
    parse_output,
)
from .storage import Database

RuntimeFactory = Callable[[RouteConfig], AgentRuntime]


@dataclass(frozen=True)
class TaskOutcome:
    task: Task
    attempt: Attempt
    output: ReviewOutput | VisualTranscriptionOutput | RevisionOutput | VerificationOutput


class Armarius:
    def __init__(
        self,
        repo: Path,
        local_config: LocalConfig,
        database: Database,
        artifacts: ArtifactStore,
        manuscript: ManuscriptManager,
        runtime_factory: RuntimeFactory | None = None,
    ) -> None:
        self.repo = repo
        self.local_config = local_config
        self.database = database
        self.artifacts = artifacts
        self.manuscript = manuscript
        self.runtime_factory = runtime_factory or self._runtime_for_route
        self.state_dir = repo / ".scriptorium"
        self.runs_dir = self.state_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    async def start_run(
        self,
        revision_name: str,
        profile: str,
        budget_usd: float | None,
    ) -> Run:
        if budget_usd is not None and (not math.isfinite(budget_usd) or budget_usd < 0):
            raise StateError("budget_usd must be a finite non-negative number")
        revision = self.manuscript.resolve_revision(revision_name)
        run_id = new_id("run")
        run_dir = self._run_dir(run_id)
        snapshot = run_dir / "snapshot"
        self.manuscript.create_snapshot(revision, snapshot)
        project = load_project_config(snapshot)
        validate_ready(project, self.local_config, profile, budget_usd)
        sources = self.manuscript.scan_sources(snapshot, project.manuscript.main)
        frozen_config = self._freeze_config(
            project,
            profile,
            sources,
            (snapshot / "scriptorium.toml").read_text(encoding="utf-8"),
        )
        run = Run(
            id=run_id,
            repository=str(self.repo),
            commit_sha=revision.commit_sha,
            tree_sha=revision.tree_sha,
            profile=profile,
            config_digest=digest_json(frozen_config),
            frozen_config=frozen_config,
            budget_usd=budget_usd,
        )
        self.database.create_run(run)
        try:
            await self._prepare_and_review(run, project, revision, sources)
        except Exception as exc:
            self._fail_active_run(run.id, exc)
            raise
        return self.database.get_run(run.id)

    async def resume_run(self, run_id: str) -> Run:
        self.database.interrupt_running_attempts(run_id)
        run = self.database.get_run(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
            return run
        if run.status == RunStatus.READY_TO_APPLY:
            if not self.database.list_findings(run.id, [FindingStatus.CONFIRMED]):
                return self.database.update_run(run.id, RunStatus.COMPLETED)
            return run
        if run.status == RunStatus.FAILED:
            run = self.database.update_run(run.id, self._status_before_failure(run.id))
        try:
            if run.status == RunStatus.PREPARING:
                project = self._project_for_run(run)
                sources = self._sources_for_run(run)
                revision = FrozenRevision(run.commit_sha, run.tree_sha)
                await self._prepare_and_review(run, project, revision, sources)
            elif run.status == RunStatus.REVIEWING:
                await self._run_reviews(run)
            elif run.status == RunStatus.AWAITING_DECISION:
                await self._advance_after_decisions(run)
            elif run.status == RunStatus.REVISING:
                await self._run_revision(run)
            elif run.status == RunStatus.AWAITING_PATCH_APPROVAL:
                await self._advance_after_patch_decision(run)
            elif run.status == RunStatus.VERIFYING:
                await self._run_verification(run)
            elif run.status == RunStatus.WAITING_BUDGET:
                await self._resume_budget_wait(run)
        except Exception as exc:
            self._fail_active_run(run.id, exc)
            raise
        return self.database.get_run(run_id)

    async def retry_task(self, run_id: str, task_id: str, route_override: str | None = None) -> Run:
        self.database.interrupt_running_attempts(run_id)
        run = self.database.get_run(run_id)
        task = self.database.get_task(task_id)
        if task.run_id != run_id:
            raise StateError(f"task {task_id} does not belong to run {run_id}")
        if task.status == TaskStatus.COMPLETED:
            raise StateError(f"task {task_id} is already completed")
        if run.status == RunStatus.FAILED:
            target = {
                "review_transcription": RunStatus.REVIEWING,
                "review": RunStatus.REVIEWING,
                "revision": RunStatus.REVISING,
                "verification_transcription": RunStatus.VERIFYING,
                "verification": RunStatus.VERIFYING,
            }.get(task.stage)
            if target is None:
                raise StateError(f"failed run cannot retry task stage {task.stage}")
            run = self.database.update_run(run.id, target)
        if run.status == RunStatus.WAITING_BUDGET:
            target = {
                "review_transcription": RunStatus.REVIEWING,
                "review": RunStatus.REVIEWING,
                "revision": RunStatus.REVISING,
                "verification_transcription": RunStatus.VERIFYING,
                "verification": RunStatus.VERIFYING,
            }.get(task.stage)
            if target is None:
                raise StateError(f"unknown task stage: {task.stage}")
            run = self.database.update_run(run.id, target)
        if task.stage == "review_transcription":
            if run.status != RunStatus.REVIEWING:
                raise StateError(f"review transcription cannot be retried while run is {run.status.value}")
            await self._run_reviews(run, transcription_route_override=route_override)
        elif task.stage == "review":
            if run.status != RunStatus.REVIEWING:
                raise StateError(f"review tasks cannot be retried while run is {run.status.value}")
            await self._run_review_role(run, task.role, route_override)
            await self._advance_review_if_complete(run)
        elif task.stage == "revision":
            if run.status != RunStatus.REVISING:
                raise StateError(f"revision tasks cannot be retried while run is {run.status.value}")
            await self._run_revision(run, route_override=route_override)
        elif task.stage == "verification_transcription":
            if run.status != RunStatus.VERIFYING:
                raise StateError(f"verification transcription cannot be retried while run is {run.status.value}")
            await self._run_verification(run, transcription_route_override=route_override)
        elif task.stage == "verification":
            if run.status != RunStatus.VERIFYING:
                raise StateError(f"verification tasks cannot be retried while run is {run.status.value}")
            await self._run_verification(run, route_override=route_override)
        else:
            raise StateError(f"unknown task stage: {task.stage}")
        return self.database.get_run(run_id)

    async def _resume_budget_wait(self, run: Run) -> None:
        incomplete = [
            task
            for task in self.database.list_tasks(run.id)
            if task.status in {TaskStatus.PENDING, TaskStatus.FAILED, TaskStatus.INTERRUPTED}
        ]
        if not incomplete:
            raise StateError("the run is waiting for budget but has no resumable task")
        stage = incomplete[-1].stage
        target = {
            "review_transcription": RunStatus.REVIEWING,
            "review": RunStatus.REVIEWING,
            "revision": RunStatus.REVISING,
            "verification_transcription": RunStatus.VERIFYING,
            "verification": RunStatus.VERIFYING,
        }.get(stage)
        if target is None:
            raise StateError(f"unknown task stage: {stage}")
        self.database.update_run(run.id, target)
        current = self.database.get_run(run.id)
        if stage in {"review_transcription", "review"}:
            await self._run_reviews(current)
        elif stage == "revision":
            await self._run_revision(current)
        else:
            await self._run_verification(current)

    def cancel_run(self, run_id: str, reason: str) -> Run:
        if not reason.strip():
            raise StateError("cancellation reason is required")
        run = self.database.get_run(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise StateError(f"run cannot be cancelled while {run.status.value}")
        self.database.interrupt_running_attempts(run_id)
        self.database.cancel_incomplete_tasks(run_id)
        cancelled = self.database.update_run(run_id, RunStatus.CANCELLED)
        self.database.append_event(
            Event(
                run_id=run_id,
                event_type="run.cancelled",
                entity_type="run",
                entity_id=run_id,
                payload={"reason": reason},
            )
        )
        return cancelled

    async def _prepare_and_review(
        self,
        run: Run,
        project: ProjectConfig,
        revision: FrozenRevision,
        sources: tuple[SourceFile, ...],
    ) -> None:
        run_dir = self._run_dir(run.id)
        snapshot = run_dir / "snapshot"
        build = self._build_copy(snapshot, run_dir / "build" / "base", project.manuscript)
        self._record_text(build.log, "text/plain; charset=utf-8")
        self._record_file(build.pdf_path, "application/pdf")
        bundle = self.manuscript.create_bundle(
            snapshot,
            run_dir / "bundle",
            revision,
            sources,
            build.pdf_path,
        )
        self._record_file(snapshot / "scriptorium.toml", "application/toml")
        for source in sources:
            self._record_file(snapshot / source.path, "application/octet-stream")
        self._record_file(bundle.workspace / "manifest.json", "application/json")
        self._record_file(bundle.workspace / "source-map.json", "application/json")
        for page in sorted((bundle.workspace / "pages").glob("page-*.png")):
            self._record_file(page, "image/png")
        manifest = {
            "run_id": run.id,
            "commit_sha": revision.commit_sha,
            "tree_sha": revision.tree_sha,
            "profile": run.profile,
            "config_digest": run.config_digest,
            "budget_usd": run.budget_usd,
            "sources": [asdict(source) for source in sources],
            "bundle_digest": self._directory_digest(bundle.workspace),
        }
        self._write_json(run_dir / "manifest.json", manifest)
        self._record_text(canonical_json(manifest), "application/json")
        self.database.update_run(run.id, RunStatus.REVIEWING)
        await self._run_reviews(self.database.get_run(run.id))

    async def _run_reviews(
        self,
        run: Run,
        transcription_route_override: str | None = None,
    ) -> None:
        bundle = self._bundle_for_run(run)
        transcription = None
        visual_pages = self._visual_page_records(bundle) if self._has_visual_transcription_contract(run) else []
        if visual_pages:
            transcription = await self._run_visual_transcription(
                run,
                "review_transcription",
                bundle,
                visual_pages,
                bundle_id="base",
                route_override=transcription_route_override,
            )
            if transcription is None:
                if self.database.get_run(run.id).status != RunStatus.WAITING_BUDGET:
                    self.database.update_run(
                        run.id,
                        RunStatus.REVIEWING,
                        "visual transcription failed or was interrupted",
                    )
                return
        roles = [AgentRole(role) for role in self._profile_roles(run)]
        semaphore = asyncio.Semaphore(self._max_concurrency(run))

        async def execute(role: AgentRole) -> TaskOutcome | None:
            async with semaphore:
                return await self._run_review_role(run, role, visual_transcription=transcription)

        await asyncio.gather(*(execute(role) for role in roles))
        await self._advance_review_if_complete(run)

    async def _run_review_role(
        self,
        run: Run,
        role: AgentRole,
        route_override: str | None = None,
        visual_transcription: VisualTranscriptionOutput | None = None,
    ) -> TaskOutcome | None:
        bundle = self._bundle_for_run(run)
        if visual_transcription is None and self._has_visual_transcription_contract(run):
            visual_pages = self._visual_page_records(bundle)
            if visual_pages:
                _, _, input_digest = self._visual_transcription_input(
                    run,
                    "review_transcription",
                    bundle,
                    visual_pages,
                    "base",
                )
                visual_transcription = self._completed_visual_transcription(
                    run,
                    "review_transcription",
                    bundle,
                    visual_pages,
                    input_digest,
                )
                if visual_transcription is None:
                    raise InfrastructureError("review task is missing its required visual transcription")
        route = self._route_for_run(run, role, route_override)
        prompt = self._review_prompt(run, role)
        outcome = await self._execute_task(
            run=run,
            stage="review",
            role=role,
            route=route,
            prompt=prompt,
            schema_kind="review",
            base_bundle=bundle,
            validator=lambda output: self._validate_review_output(
                output,
                self._sources_for_run(run),
                bundle.pdf_pages,
                self._run_dir(run.id) / "snapshot",
                bundle.workspace / "manuscript.pdf",
                visual_transcription,
            ),
        )
        if outcome is None:
            return None
        output = outcome.output
        assert isinstance(output, ReviewOutput)
        for candidate in output.findings:
            evidence = tuple(item.model_dump(mode="json") for item in candidate.evidence)
            fingerprint = digest_json(
                {
                    "category": candidate.category,
                    "severity": candidate.severity.value,
                    "title": candidate.title,
                    "claim": candidate.claim,
                    "evidence": evidence,
                    "explanation": candidate.explanation,
                    "suggested_action": candidate.suggested_action,
                    "confidence": candidate.confidence,
                }
            )
            finding = Finding(
                run_id=run.id,
                task_id=outcome.task.id,
                attempt_id=outcome.attempt.id,
                fingerprint=fingerprint,
                role=role,
                category=candidate.category,
                severity=FindingSeverity(candidate.severity.value),
                title=candidate.title,
                claim=candidate.claim,
                evidence=evidence,
                explanation=candidate.explanation,
                suggested_action=candidate.suggested_action,
                confidence=candidate.confidence,
            )
            stored = self.database.get_or_create_finding(finding)
            if stored.id != finding.id and (
                stored.task_id != outcome.task.id or stored.attempt_id != outcome.attempt.id
            ):
                self.database.append_event(
                    Event(
                        run_id=run.id,
                        event_type="finding.duplicate",
                        entity_type="finding",
                        entity_id=stored.id,
                        payload={
                            "task_id": outcome.task.id,
                            "attempt_id": outcome.attempt.id,
                            "role": role.value,
                        },
                    )
                )
        return outcome

    async def _advance_review_if_complete(self, run: Run) -> None:
        current = self.database.get_run(run.id)
        if current.status == RunStatus.WAITING_BUDGET:
            return
        roles = {AgentRole(role) for role in self._profile_roles(run)}
        completed_roles = {
            task.role
            for task in self.database.list_tasks(run.id)
            if task.stage == "review" and task.status == TaskStatus.COMPLETED
        }
        if roles.issubset(completed_roles):
            self.database.update_run(run.id, RunStatus.AWAITING_DECISION)
        else:
            self.database.update_run(
                run.id,
                RunStatus.REVIEWING,
                "one or more review tasks failed or were interrupted",
            )

    async def _advance_after_decisions(self, run: Run) -> None:
        findings = self.database.list_findings(run.id)
        pending = [finding for finding in findings if finding.status == FindingStatus.PENDING]
        if pending:
            raise StateError(f"{len(pending)} findings still require a human decision")
        confirmed = [finding for finding in findings if finding.status == FindingStatus.CONFIRMED]
        if not confirmed:
            self.database.update_run(run.id, RunStatus.COMPLETED)
            return
        self.database.update_run(run.id, RunStatus.REVISING)
        await self._run_revision(self.database.get_run(run.id))

    async def _run_revision(
        self,
        run: Run,
        route_override: str | None = None,
        feedback: str | None = None,
        resume_attempt: Attempt | None = None,
    ) -> None:
        confirmed = self.database.list_findings(run.id, [FindingStatus.CONFIRMED])
        if not confirmed:
            self.database.update_run(run.id, RunStatus.COMPLETED)
            return
        if feedback is None:
            recovered = self._rejected_patch_revision_context(run)
            if recovered is not None:
                feedback, generating_route, generating_attempt = recovered
                if route_override is None:
                    route_override = generating_route
                    resume_attempt = generating_attempt
        route = self._route_for_run(run, AgentRole.REVISION, route_override)
        prompt = self._revision_prompt(run, confirmed, feedback)
        outcome = await self._execute_task(
            run=run,
            stage="revision",
            role=AgentRole.REVISION,
            route=route,
            prompt=prompt,
            schema_kind="revision",
            base_bundle=self._bundle_for_run(run),
            validator=lambda output: self._validate_revision_output(
                output,
                confirmed,
                self._sources_for_run(run),
                self._run_dir(run.id) / "snapshot",
            ),
            resume_attempt=resume_attempt,
        )
        if outcome is None:
            if self.database.get_run(run.id).status != RunStatus.WAITING_BUDGET:
                self.database.update_run(run.id, RunStatus.REVISING, "revision task failed or was interrupted")
            return
        output = outcome.output
        assert isinstance(output, RevisionOutput)
        candidate = self._run_dir(run.id) / "patched" / ".candidate"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        diff, changed_paths = self.manuscript.apply_edits(
            self._run_dir(run.id) / "snapshot",
            candidate,
            output.edits,
        )
        if not changed_paths:
            raise StateError("the revision contains no manuscript changes")
        build = self._build_copy(
            candidate,
            self._run_dir(run.id) / "build" / "patch-candidate",
            self._manuscript_config(run),
        )
        diff_artifact = self._record_text(diff, "text/x-diff; charset=utf-8")
        self._record_text(build.log, "text/plain; charset=utf-8")
        patch = Patch(
            run_id=run.id,
            base_commit=run.commit_sha,
            diff_digest=diff_artifact.digest,
            summary=output.summary,
            edits=tuple(edit.model_dump(mode="json") for edit in output.edits),
            attempt_id=outcome.attempt.id,
            build_succeeded=True,
        )
        existing = next(
            (
                item
                for item in self.database.list_patches(run.id)
                if item.base_commit == patch.base_commit and item.diff_digest == patch.diff_digest
            ),
            None,
        )
        if existing is None:
            target = candidate.parent / patch.id
            candidate.replace(target)
            self.database.create_patch(patch)
        else:
            shutil.rmtree(candidate)
            if existing.status == PatchStatus.REJECTED:
                patch = self.database.repropose_patch(
                    existing.id,
                    attempt_id=outcome.attempt.id,
                    summary=patch.summary,
                    edits=patch.edits,
                    build_succeeded=patch.build_succeeded,
                )
            else:
                patch = existing
        self._write_json(
            self._run_dir(run.id) / "patches" / f"{patch.id}.json",
            {
                "patch_id": patch.id,
                "attempt_id": patch.attempt_id,
                "diff_digest": patch.diff_digest,
                "changed_paths": list(changed_paths),
                "edits": list(patch.edits),
            },
        )
        self.database.update_run(run.id, RunStatus.AWAITING_PATCH_APPROVAL)

    async def _advance_after_patch_decision(self, run: Run) -> None:
        if not self.database.list_findings(run.id, [FindingStatus.CONFIRMED]):
            self.database.update_run(run.id, RunStatus.COMPLETED)
            return
        patches = self.database.list_patches(run.id)
        if not patches:
            raise StateError("the run has no proposed patch")
        patch = patches[-1]
        if patch.status == PatchStatus.PROPOSED:
            raise StateError(f"patch {patch.id} still requires a human decision")
        if patch.status == PatchStatus.REJECTED:
            self.database.update_run(run.id, RunStatus.REVISING)
            await self._run_revision(self.database.get_run(run.id))
            return
        if patch.status == PatchStatus.APPROVED:
            self.database.update_run(run.id, RunStatus.VERIFYING)
            await self._run_verification(self.database.get_run(run.id))
            return
        if patch.status == PatchStatus.VERIFIED:
            self.database.update_run(run.id, RunStatus.VERIFYING)
            self.database.update_run(run.id, RunStatus.READY_TO_APPLY)
            return
        raise StateError(f"patch {patch.id} cannot advance while {patch.status.value}")

    def _rejected_patch_revision_context(
        self,
        run: Run,
    ) -> tuple[str, str | None, Attempt | None] | None:
        patches = self.database.list_patches(run.id)
        if not patches or patches[-1].status != PatchStatus.REJECTED:
            return None
        patch = patches[-1]
        decisions = self.database.list_decisions("patch", patch.id)
        feedback = decisions[-1].reason
        if patch.attempt_id is None:
            return feedback, None, None
        attempt = self.database.get_attempt(patch.attempt_id)
        task = self.database.get_task(attempt.task_id)
        if task.run_id != run.id or task.stage != "revision" or task.role != AgentRole.REVISION:
            raise InfrastructureError(f"patch {patch.id} has an invalid generating attempt")
        return feedback, task.route, attempt

    async def _run_verification(
        self,
        run: Run,
        route_override: str | None = None,
        transcription_route_override: str | None = None,
    ) -> None:
        if not self.database.list_findings(run.id, [FindingStatus.CONFIRMED]):
            self.database.update_run(run.id, RunStatus.COMPLETED)
            return
        patches = self.database.list_patches(run.id)
        approved = [patch for patch in patches if patch.status in {PatchStatus.APPROVED, PatchStatus.VERIFIED}]
        if not approved:
            raise StateError("verification requires an approved patch")
        patch = approved[-1]
        if patch.status == PatchStatus.VERIFIED:
            self.database.update_run(run.id, RunStatus.READY_TO_APPLY)
            return
        patched = self._run_dir(run.id) / "patched" / patch.id
        verification_workspace = self._run_dir(run.id) / "verifications" / patch.id / "bundle"
        if (verification_workspace / "manifest.json").is_file():
            verification_bundle = self._load_bundle(verification_workspace)
        else:
            build = self._build_copy(
                patched,
                self._run_dir(run.id) / "build" / f"verification-{patch.id}",
                self._manuscript_config(run),
            )
            verification_bundle = self.manuscript.create_bundle(
                patched,
                verification_workspace,
                FrozenRevision(run.commit_sha, run.tree_sha),
                self.manuscript.scan_sources(patched, self._manuscript_config(run).main),
                build.pdf_path,
            )
        transcription = None
        visual_pages = (
            self._visual_page_records(verification_bundle) if self._has_visual_transcription_contract(run) else []
        )
        if visual_pages:
            transcription = await self._run_visual_transcription(
                run,
                "verification_transcription",
                verification_bundle,
                visual_pages,
                bundle_id=patch.id,
                route_override=transcription_route_override,
            )
            if transcription is None:
                if self.database.get_run(run.id).status != RunStatus.WAITING_BUDGET:
                    self.database.update_run(
                        run.id,
                        RunStatus.VERIFYING,
                        "visual transcription failed or was interrupted",
                    )
                return
        confirmed = self.database.list_findings(run.id, [FindingStatus.CONFIRMED])
        route = self._route_for_run(run, AgentRole.VERIFICATION, route_override)
        prompt = self._verification_prompt(run, patch, confirmed)
        outcome = await self._execute_task(
            run=run,
            stage="verification",
            role=AgentRole.VERIFICATION,
            route=route,
            prompt=prompt,
            schema_kind="verification",
            base_bundle=verification_bundle,
            validator=lambda output: self._validate_verification_output(
                output,
                confirmed,
                verification_bundle.sources,
                verification_bundle.pdf_pages,
                patched,
                verification_bundle.workspace / "manuscript.pdf",
                transcription,
            ),
        )
        if outcome is None:
            if self.database.get_run(run.id).status != RunStatus.WAITING_BUDGET:
                self.database.update_run(run.id, RunStatus.VERIFYING, "verification task failed or was interrupted")
            return
        output = outcome.output
        assert isinstance(output, VerificationOutput)
        result = VerificationResult(output.verdict)
        verification = Verification(
            patch_id=patch.id,
            attempt_id=outcome.attempt.id,
            result=result,
            summary=output.summary,
            artifact_digest=outcome.attempt.output_artifact_digest,
        )
        self.database.create_verification(verification)
        if result == VerificationResult.PASS:
            self.database.update_run(run.id, RunStatus.READY_TO_APPLY)
        else:
            self.database.update_run(run.id, RunStatus.AWAITING_PATCH_APPROVAL)

    async def _run_visual_transcription(
        self,
        run: Run,
        stage: str,
        bundle: ManuscriptBundle,
        pages: list[dict[str, Any]],
        *,
        bundle_id: str,
        route_override: str | None = None,
    ) -> VisualTranscriptionOutput | None:
        prompt, input_bundle, input_digest = self._visual_transcription_input(
            run,
            stage,
            bundle,
            pages,
            bundle_id,
        )
        completed = self._completed_visual_transcription(
            run,
            stage,
            bundle,
            pages,
            input_digest,
        )
        if completed is not None:
            return completed
        route = self._route_for_run(run, AgentRole.VISUAL_TRANSCRIPTION, route_override)
        outcome = await self._execute_task(
            run=run,
            stage=stage,
            role=AgentRole.VISUAL_TRANSCRIPTION,
            route=route,
            prompt=prompt,
            schema_kind="visual_transcription",
            base_bundle=input_bundle,
            validator=lambda output: self._validate_visual_transcription(output, bundle, pages),
        )
        if outcome is None:
            return None
        output = outcome.output
        assert isinstance(output, VisualTranscriptionOutput)
        return output

    def _completed_visual_transcription(
        self,
        run: Run,
        stage: str,
        bundle: ManuscriptBundle,
        pages: list[dict[str, Any]],
        input_digest: str,
    ) -> VisualTranscriptionOutput | None:
        expected_pdf_digest = self._bundle_manifest(bundle)["pdf_digest"]
        for task in reversed(self.database.list_tasks(run.id)):
            if (
                task.stage != stage
                or task.role != AgentRole.VISUAL_TRANSCRIPTION
                or task.status != TaskStatus.COMPLETED
                or task.input_digest != input_digest
            ):
                continue
            attempt = next(
                item
                for item in reversed(self.database.list_attempts(task.id))
                if item.status == AttemptStatus.COMPLETED
            )
            if attempt.output_artifact_digest is None:
                raise InfrastructureError(f"completed attempt {attempt.id} has no output artifact")
            output = parse_output(
                "visual_transcription",
                self.artifacts.get_bytes(attempt.output_artifact_digest).decode("utf-8"),
            )
            if not isinstance(output, VisualTranscriptionOutput) or output.pdf_digest != expected_pdf_digest:
                continue
            try:
                self._validate_visual_transcription(output, bundle, pages)
            except ValueError:
                continue
            return output
        return None

    @staticmethod
    def _has_visual_transcription_contract(run: Run) -> bool:
        markers = (
            AgentRole.VISUAL_TRANSCRIPTION.value in run.frozen_config.get("role_routes", {}),
            AgentRole.VISUAL_TRANSCRIPTION.value in run.frozen_config.get("prompts", {}),
            "visual_transcription" in run.frozen_config.get("schemas", {}),
        )
        if any(markers) and not all(markers):
            raise InfrastructureError("the frozen visual transcription contract is incomplete")
        return all(markers)

    @staticmethod
    def _bundle_manifest(bundle: ManuscriptBundle) -> dict[str, Any]:
        return json.loads((bundle.workspace / "manifest.json").read_text(encoding="utf-8"))

    def _visual_page_records(self, bundle: ManuscriptBundle) -> list[dict[str, Any]]:
        manifest = self._bundle_manifest(bundle)
        with fitz.open(bundle.workspace / "manuscript.pdf") as document:
            page_numbers = [index + 1 for index, page in enumerate(document) if page.get_image_info()]
        return [
            {
                "page": page_number,
                "path": manifest["pages"][page_number - 1]["path"],
                "page_digest": manifest["pages"][page_number - 1]["digest"],
            }
            for page_number in page_numbers
        ]

    def _visual_transcription_prompt(
        self,
        run: Run,
        request: dict[str, Any],
    ) -> str:
        role_prompt = run.frozen_config["prompts"][AgentRole.VISUAL_TRANSCRIPTION.value]["content"]
        return (
            f"{role_prompt}\n\n"
            f"Requested pages:\n{json.dumps(request, indent=2, ensure_ascii=False)}\n\n"
            "Read only the listed page images at their workspace paths. Return pdf_digest exactly as "
            "provided and one entry per requested page with its exact page and page_digest. The text "
            "field must contain only verbatim visible raster text and may be empty when none is visible. "
            "Return only the VisualTranscriptionOutput JSON object with no prose before or after it."
        )

    def _visual_transcription_input(
        self,
        run: Run,
        stage: str,
        bundle: ManuscriptBundle,
        pages: list[dict[str, Any]],
        bundle_id: str,
    ) -> tuple[str, ManuscriptBundle, str]:
        bundle_digest = self._directory_digest(bundle.workspace)
        request = {
            "stage": stage,
            "bundle_id": bundle_id,
            "bundle_digest": bundle_digest,
            "pdf_digest": self._bundle_manifest(bundle)["pdf_digest"],
            "pages": pages,
        }
        input_identity = digest_json({"bundle_id": bundle_id, "bundle_digest": bundle_digest})
        workspace = self._run_dir(run.id) / "visual-inputs" / stage / input_identity
        if workspace.exists():
            shutil.rmtree(workspace)
        for page in pages:
            source = bundle.workspace / page["path"]
            target = workspace / page["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self._write_json(workspace / "manifest.json", request)
        prompt = self._visual_transcription_prompt(run, request)
        input_bundle = ManuscriptBundle(workspace, (), len(pages))
        schema = dict(run.frozen_config["schemas"]["visual_transcription"]["content"])
        input_digest = digest_json(
            {
                "prompt_digest": ArtifactStore.digest_bytes(prompt.encode("utf-8")),
                "schema_digest": ArtifactStore.digest_bytes(canonical_json(schema).encode("utf-8")),
                "bundle_digest": self._directory_digest(workspace),
            }
        )
        return prompt, input_bundle, input_digest

    def _validate_visual_transcription(
        self,
        output: VisualTranscriptionOutput,
        bundle: ManuscriptBundle,
        pages: list[dict[str, Any]],
    ) -> None:
        manifest = self._bundle_manifest(bundle)
        if output.pdf_digest != manifest["pdf_digest"]:
            raise ValueError("visual transcription PDF digest does not match the current bundle")
        page_numbers = [item.page for item in output.pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("visual transcription contains duplicate pages")
        expected = {item["page"]: item["page_digest"] for item in pages}
        actual = {item.page: item.page_digest for item in output.pages}
        if set(actual) != set(expected):
            raise ValueError("visual transcription pages do not match the raster pages in the current bundle")
        for page, digest in expected.items():
            if actual[page] != digest:
                raise ValueError(f"visual transcription page digest does not match page {page}")

    async def _execute_task(
        self,
        *,
        run: Run,
        stage: str,
        role: AgentRole,
        route: RouteConfig,
        prompt: str,
        schema_kind: str,
        base_bundle: ManuscriptBundle,
        validator: Callable[[Any], None],
        resume_attempt: Attempt | None = None,
    ) -> TaskOutcome | None:
        schema = dict(run.frozen_config["schemas"][schema_kind]["content"])
        prompt_artifact = self._record_text(prompt, "text/markdown; charset=utf-8")
        schema_artifact = self._record_text(canonical_json(schema), "application/schema+json")
        input_digest = digest_json(
            {
                "prompt_digest": prompt_artifact.digest,
                "schema_digest": schema_artifact.digest,
                "bundle_digest": self._directory_digest(base_bundle.workspace),
            }
        )
        task = self.database.find_task(run.id, stage, role, route.name, input_digest)
        if task is None:
            task = self.database.create_task(
                Task(
                    run_id=run.id,
                    stage=stage,
                    role=role,
                    route=route.name,
                    input_digest=input_digest,
                )
            )
        if task.status == TaskStatus.COMPLETED:
            attempts = self.database.list_attempts(task.id)
            completed = next(attempt for attempt in reversed(attempts) if attempt.status == AttemptStatus.COMPLETED)
            if completed.output_artifact_digest is None:
                raise InfrastructureError(f"completed attempt {completed.id} has no output artifact")
            output = parse_output(
                schema_kind,
                self.artifacts.get_bytes(completed.output_artifact_digest).decode("utf-8"),
            )
            validator(output)
            return TaskOutcome(task, completed, output)
        if not self._budget_available(run.id, route):
            current = self.database.get_run(run.id)
            if current.status != RunStatus.WAITING_BUDGET:
                self.database.update_run(run.id, RunStatus.WAITING_BUDGET)
            return None

        workspace = self._task_workspace(run.id, task.id, base_bundle.workspace, prompt)
        bundle_digest = self._directory_digest(workspace)
        session_dir = self._session_dir(run.id, role, route)
        runtime = self.runtime_factory(route)
        previous_attempts = self.database.list_attempts(task.id)
        previous = previous_attempts[-1] if previous_attempts else None
        if resume_attempt is not None and not self._attempt_matches_route(resume_attempt, route):
            raise InfrastructureError(
                f"attempt {resume_attempt.id} does not belong to frozen runtime route {route.name!r}"
            )
        thread_id = resume_attempt.thread_id if resume_attempt is not None else None
        if (
            previous is not None
            and previous.status in {AttemptStatus.FAILED, AttemptStatus.INTERRUPTED}
            and self._attempt_matches_route(previous, route)
        ):
            thread_id = previous.thread_id or thread_id
        invocation_prompt = prompt
        invocation_prompt_digest = prompt_artifact.digest
        correction_used = False
        if previous is not None and previous.error and previous.error.startswith("invalid structured output:"):
            if (
                previous.prompt_digest == prompt_artifact.digest
                and previous.thread_id
                and self._attempt_matches_route(previous, route)
            ):
                invocation_prompt = self._correction_prompt(previous.error)
                invocation_prompt_digest = self._record_text(
                    invocation_prompt,
                    "text/markdown; charset=utf-8",
                ).digest
                correction_used = True
            else:
                thread_id = None
        while True:
            attempt = self.database.begin_attempt(
                task.id,
                runtime_name=route.runtime,
                runtime_version=route.runtime_version,
                model=route.model,
                model_provider=route.model_provider,
                prompt_digest=invocation_prompt_digest,
                schema_digest=schema_artifact.digest,
                bundle_digest=bundle_digest,
            )
            if thread_id is None:
                result = await runtime.run_agent(
                    invocation_prompt,
                    role,
                    workspace,
                    schema,
                    session_dir,
                )
            else:
                result = await runtime.resume_agent(
                    thread_id,
                    invocation_prompt,
                    role,
                    workspace,
                    schema,
                    session_dir,
                )
            output_artifact = (
                self._record_text(result.final_response, "application/json")
                if result.final_response is not None
                else None
            )
            trace_artifact = self._record_text(
                result.trace_jsonl,
                "application/x-ndjson; charset=utf-8",
            )
            error = result.error
            parsed: ReviewOutput | VisualTranscriptionOutput | RevisionOutput | VerificationOutput | None = None
            provenance_error = self._result_provenance_error(result, route)
            if provenance_error is not None:
                error = provenance_error
            if provenance_error is None and result.status == "completed" and result.final_response is None:
                error = "invalid structured output: completed agent turn returned no final response"
            if result.status == "completed" and result.final_response is not None and provenance_error is None:
                try:
                    parsed = parse_output(schema_kind, result.final_response)
                    validator(parsed)
                except (ValidationError, ValueError, StateError) as exc:
                    parsed = None
                    error = f"invalid structured output: {exc}"
            if provenance_error is not None:
                terminal = AttemptStatus.FAILED
            elif result.status == "completed" and parsed is not None:
                terminal = AttemptStatus.COMPLETED
            elif result.status == "interrupted":
                terminal = AttemptStatus.INTERRUPTED
            else:
                terminal = AttemptStatus.FAILED
            cost = route.estimate_cost(
                result.usage.input_tokens,
                result.usage.cached_input_tokens,
                result.usage.output_tokens,
                result.usage.reasoning_tokens,
            )
            finished = self.database.finish_attempt(
                attempt.id,
                terminal,
                thread_id=result.thread_id,
                runtime_name=result.runtime_name,
                runtime_version=result.runtime_version,
                model=result.model,
                model_provider=result.model_provider,
                input_tokens=result.usage.input_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
                output_tokens=result.usage.output_tokens,
                reasoning_tokens=result.usage.reasoning_tokens,
                estimated_cost_usd=cost,
                trace_artifact_digest=trace_artifact.digest,
                output_artifact_digest=output_artifact.digest if output_artifact else None,
                duration_ms=result.duration_ms,
                error=error,
            )
            if parsed is not None:
                return TaskOutcome(self.database.get_task(task.id), finished, parsed)
            if (
                result.status == "completed"
                and result.thread_id
                and not correction_used
                and error
                and error.startswith("invalid structured output:")
            ):
                correction_used = True
                thread_id = result.thread_id
                invocation_prompt = self._correction_prompt(error)
                invocation_prompt_digest = self._record_text(
                    invocation_prompt,
                    "text/markdown; charset=utf-8",
                ).digest
                continue
            return None

    @staticmethod
    def _correction_prompt(error: str) -> str:
        return (
            "Correct your previous response. It was rejected for this reason:\n"
            f"{error}\nReturn only one JSON object matching the original schema and valid manuscript anchors."
        )

    def _validate_review_output(
        self,
        output: ReviewOutput,
        sources: tuple[SourceFile, ...],
        pdf_pages: int,
        source_root: Path,
        pdf_path: Path,
        visual_transcription: VisualTranscriptionOutput | None = None,
    ) -> None:
        source_index = {source.path: source for source in sources}
        for finding in output.findings:
            for evidence in finding.evidence:
                self._validate_evidence(
                    evidence,
                    source_index,
                    pdf_pages,
                    source_root,
                    pdf_path,
                    visual_transcription,
                )

    def _validate_revision_output(
        self,
        output: RevisionOutput,
        findings: list[Finding],
        sources: tuple[SourceFile, ...],
        snapshot: Path,
    ) -> None:
        source_index = {source.path: source for source in sources}
        expected_ids = {finding.id for finding in findings}
        covered_ids: set[str] = set()
        if not output.edits:
            raise ValueError("confirmed findings require at least one edit")
        by_path: dict[str, list[ExactEdit]] = {}
        for edit in output.edits:
            try:
                source = source_index[edit.path]
            except KeyError as exc:
                raise ValueError(f"edit path is outside the frozen source manifest: {edit.path}") from exc
            if edit.source_digest != source.digest:
                raise ValueError(f"source digest does not match for {edit.path}")
            if edit.end_line > source.lines:
                raise ValueError(f"edit range exceeds {edit.path}")
            unknown = set(edit.finding_ids) - expected_ids
            if unknown:
                raise ValueError(f"edit references unknown findings: {sorted(unknown)}")
            covered_ids.update(edit.finding_ids)
            excerpt = self._line_excerpt(snapshot / edit.path, edit.start_line, edit.end_line)
            if excerpt != edit.before and excerpt.rstrip("\r\n") != edit.before:
                raise ValueError(f"before text does not exactly match {edit.path}")
            by_path.setdefault(edit.path, []).append(edit)
        missing = expected_ids - covered_ids
        if missing:
            raise ValueError(f"confirmed findings are not covered: {sorted(missing)}")
        for path, edits in by_path.items():
            ordered = sorted(edits, key=lambda item: (item.start_line, item.end_line))
            for previous, current in zip(ordered, ordered[1:]):
                if current.start_line <= previous.end_line:
                    raise ValueError(f"edits overlap in {path}")

    def _validate_verification_output(
        self,
        output: VerificationOutput,
        findings: list[Finding],
        sources: tuple[SourceFile, ...],
        pdf_pages: int,
        source_root: Path,
        pdf_path: Path,
        visual_transcription: VisualTranscriptionOutput | None = None,
    ) -> None:
        expected_ids = {finding.id for finding in findings}
        unknown = set(output.resolved_finding_ids) - expected_ids
        if unknown:
            raise ValueError(f"verification references unknown findings: {sorted(unknown)}")
        if output.verdict == "pass" and set(output.resolved_finding_ids) != expected_ids:
            raise ValueError("passing verification must cover every confirmed finding")
        source_index = {source.path: source for source in sources}
        for issue in output.issues:
            for evidence in issue.evidence:
                self._validate_evidence(
                    evidence,
                    source_index,
                    pdf_pages,
                    source_root,
                    pdf_path,
                    visual_transcription,
                )

    def _validate_evidence(
        self,
        evidence: Any,
        source_index: dict[str, SourceFile],
        pdf_pages: int,
        source_root: Path,
        pdf_path: Path,
        visual_transcription: VisualTranscriptionOutput | None = None,
    ) -> None:
        if evidence.page is not None and evidence.page > pdf_pages:
            raise ValueError(f"PDF page {evidence.page} is outside the manuscript")
        if evidence.start_line is None:
            if evidence.source_path != "manuscript.pdf":
                raise ValueError("page-only evidence must use source_path manuscript.pdf")
            quoted_text = " ".join(evidence.quoted_text.split())
            with fitz.open(pdf_path) as document:
                page_text = " ".join(document[evidence.page - 1].get_text(sort=True).split())
            if quoted_text and quoted_text in page_text:
                return
            if visual_transcription is not None:
                visual_page = next(
                    (item for item in visual_transcription.pages if item.page == evidence.page),
                    None,
                )
                if visual_page is not None:
                    visual_text = " ".join(visual_page.text.split())
                    if quoted_text and quoted_text in visual_text:
                        return
            raise ValueError(f"quoted PDF evidence does not match manuscript.pdf page {evidence.page}")
        try:
            source = source_index[evidence.source_path]
        except KeyError as exc:
            raise ValueError(f"evidence path is outside the frozen source manifest: {evidence.source_path}") from exc
        if evidence.source_digest != source.digest:
            raise ValueError(f"source digest does not match for {evidence.source_path}")
        if evidence.end_line > source.lines:
            raise ValueError(f"evidence range exceeds {evidence.source_path}")
        excerpt = self._line_excerpt(
            source_root / evidence.source_path,
            evidence.start_line,
            evidence.end_line,
        )
        if evidence.quoted_text not in excerpt:
            raise ValueError(f"quoted evidence does not match {evidence.source_path}")

    def _freeze_config(
        self,
        project: ProjectConfig,
        profile: str,
        sources: tuple[SourceFile, ...],
        project_config_text: str,
    ) -> dict[str, Any]:
        roles = (
            *project.profiles[profile],
            AgentRole.VISUAL_TRANSCRIPTION.value,
            AgentRole.REVISION.value,
            AgentRole.VERIFICATION.value,
        )
        prompts = {role: self._prompt_record(AgentRole(role)) for role in roles}
        schemas = {
            kind: {"digest": digest_json(output_schema(kind)), "content": output_schema(kind)}
            for kind in ("review", "visual_transcription", "revision", "verification")
        }
        frozen_local = self.local_config.frozen_dict()
        for route in frozen_local["routes"].values():
            route["runtime_version"] = RUNTIME_SDK_VERSIONS[route["runtime"]]
        return {
            "project": project.frozen_dict(),
            "local": frozen_local,
            "config_files": {
                "scriptorium.toml": {
                    "digest": ArtifactStore.digest_bytes(project_config_text.encode("utf-8")),
                    "content": project_config_text,
                }
            },
            "profile_roles": list(project.profiles[profile]),
            "role_routes": {role: self.local_config.roles[role] for role in roles},
            "prompts": prompts,
            "schemas": schemas,
            "sources": [asdict(source) for source in sources],
        }

    def _review_prompt(self, run: Run, role: AgentRole) -> str:
        role_prompt = run.frozen_config["prompts"][role.value]["content"]
        return (
            f"{role_prompt}\n\n"
            "Review the frozen manuscript in the workspace: source files are under sources/, "
            "the rendered PDF is manuscript.pdf, and rendered page images are under pages/. "
            "Every finding must cite resolvable evidence with the exact anchors required by "
            "the output schema. For source-file evidence, source_path must be the bare "
            'relative path from source-map.json (for example "main.tex", never '
            '"sources/main.tex"), and start_line, end_line, source_digest, and quoted_text '
            "must be supplied; quoted_text must be copied verbatim from those lines so that "
            "it appears exactly inside the cited line range, without additions, omissions, "
            'or ellipses. For rendered-PDF evidence, use source_path "manuscript.pdf", set '
            "page to a valid 1-based PDF page number, and copy quoted_text verbatim from the "
            "cited page. Use start_line/end_line only for UTF-8 text sources; never line-anchor "
            ".pdf files or other graphics/binary assets from source-map.json. Only "
            "manuscript.pdf supports page evidence. Do not modify files. Return only the "
            "ReviewOutput JSON object with no prose before or after it."
        )

    def _revision_prompt(self, run: Run, findings: list[Finding], feedback: str | None) -> str:
        role_prompt = run.frozen_config["prompts"][AgentRole.REVISION.value]["content"]
        finding_data = [
            {
                "id": finding.id,
                "category": finding.category,
                "severity": finding.severity.value,
                "title": finding.title,
                "claim": finding.claim,
                "evidence": list(finding.evidence),
                "explanation": finding.explanation,
                "suggested_action": finding.suggested_action,
            }
            for finding in findings
        ]
        feedback_text = f"\nHuman rejection feedback:\n{feedback}\n" if feedback else ""
        return (
            f"{role_prompt}\n\nConfirmed findings:\n{json.dumps(finding_data, indent=2, ensure_ascii=False)}\n"
            f"{feedback_text}"
            "Propose exact, non-overlapping source replacements. For every edit, path must "
            'be the bare relative path from source-map.json (for example "main.tex", never '
            '"sources/main.tex"), source_digest must match source-map.json, and before must '
            "reproduce the exact current text of the cited lines. Do not modify files. "
            "Return only the RevisionOutput JSON object."
        )

    def _verification_prompt(self, run: Run, patch: Patch, findings: list[Finding]) -> str:
        role_prompt = run.frozen_config["prompts"][AgentRole.VERIFICATION.value]["content"]
        finding_data = [
            {"id": finding.id, "title": finding.title, "claim": finding.claim, "evidence": list(finding.evidence)}
            for finding in findings
        ]
        diff = self.artifacts.get_bytes(patch.diff_digest).decode("utf-8")
        return (
            f"{role_prompt}\n\nConfirmed findings:\n{json.dumps(finding_data, indent=2, ensure_ascii=False)}\n\n"
            f"Approved diff:\n{diff}\n\n"
            "Verify the patched manuscript independently for resolution, factual or numeric changes, "
            "citation/figure consistency, and regressions. Every issue must cite resolvable evidence "
            "with the exact anchors required by the output schema. For source-file evidence, "
            "source_path must be the bare relative path from source-map.json (never "
            '"sources/..."), and start_line, end_line, source_digest, and quoted_text must be '
            "supplied; quoted_text must be copied verbatim from those lines so that it appears "
            "exactly inside the cited line range, without additions, omissions, or ellipses. For "
            'rendered-PDF evidence, use source_path "manuscript.pdf", set page to a valid 1-based '
            "PDF page number, and copy quoted_text verbatim from the cited page. Use "
            "start_line/end_line only for UTF-8 text sources; never line-anchor .pdf files or "
            "other graphics/binary assets from source-map.json. Only manuscript.pdf supports "
            "page evidence. Return only the VerificationOutput JSON object with no prose before "
            "or after it."
        )

    def _route_for_run(
        self,
        run: Run,
        role: AgentRole,
        override: str | None = None,
    ) -> RouteConfig:
        local = run.frozen_config["local"]
        route_name = override or run.frozen_config["role_routes"][role.value]
        try:
            values = dict(local["routes"][route_name])
        except KeyError as exc:
            raise StateError(f"route {route_name!r} was not frozen for run {run.id}") from exc
        if "runtime" not in values:
            legacy_runtime = run.frozen_config.get("runtime", {})
            values["runtime"] = legacy_runtime.get("name", "codex")
        if not values.get("runtime_version"):
            legacy_runtime = run.frozen_config.get("runtime", {})
            values["runtime_version"] = legacy_runtime.get("version") or RUNTIME_SDK_VERSIONS[values["runtime"]]
        return RouteConfig(**values)

    def _project_for_run(self, run: Run) -> ProjectConfig:
        project = run.frozen_config["project"]
        manuscript = ManuscriptConfig(**project["manuscript"])
        profiles = {name: tuple(roles) for name, roles in project["profiles"].items()}
        return ProjectConfig(manuscript, profiles)

    def _manuscript_config(self, run: Run) -> ManuscriptConfig:
        return ManuscriptConfig(**run.frozen_config["project"]["manuscript"])

    def _sources_for_run(self, run: Run) -> tuple[SourceFile, ...]:
        return tuple(SourceFile(**source) for source in run.frozen_config["sources"])

    def _profile_roles(self, run: Run) -> tuple[str, ...]:
        return tuple(run.frozen_config["profile_roles"])

    def _max_concurrency(self, run: Run) -> int:
        return int(run.frozen_config["local"]["max_concurrency"])

    def _bundle_for_run(self, run: Run) -> ManuscriptBundle:
        return self._load_bundle(self._run_dir(run.id) / "bundle")

    @staticmethod
    def _load_bundle(workspace: Path) -> ManuscriptBundle:
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        sources = tuple(SourceFile(**source) for source in manifest["sources"])
        pdf_pages = int(manifest["pdf_pages"])
        pdf = workspace / "manuscript.pdf"
        if not pdf.is_file():
            raise InfrastructureError(f"bundle PDF is missing: {workspace}")
        if ArtifactStore.digest_file(pdf) != manifest["pdf_digest"]:
            raise InfrastructureError(f"bundle PDF digest does not match: {workspace}")
        pages = list((workspace / "pages").glob("page-*.png"))
        if len(pages) != pdf_pages or len(manifest["pages"]) != pdf_pages:
            raise InfrastructureError(f"bundle page render is incomplete: {workspace}")
        for source in sources:
            path = workspace / "sources" / source.path
            if not path.is_file() or ArtifactStore.digest_file(path) != source.digest:
                raise InfrastructureError(f"bundle source digest does not match: {source.path}")
        for page in manifest["pages"]:
            path = workspace / page["path"]
            if not path.is_file() or ArtifactStore.digest_file(path) != page["digest"]:
                raise InfrastructureError(f"bundle page digest does not match: {page['path']}")
        return ManuscriptBundle(workspace, sources, pdf_pages)

    def _budget_available(self, run_id: str, route: RouteConfig) -> bool:
        run = self.database.get_run(run_id)
        return (
            run.budget_usd is None
            or run.estimated_cost_usd < run.budget_usd
            or (
                run.estimated_cost_usd <= run.budget_usd
                and route.input_usd_per_million == 0
                and route.output_usd_per_million == 0
            )
        )

    @staticmethod
    def _attempt_matches_route(attempt: Attempt, route: RouteConfig) -> bool:
        return (
            attempt.runtime_name == route.runtime
            and attempt.runtime_version == route.runtime_version
            and attempt.model == route.model
            and attempt.model_provider == route.model_provider
        )

    @staticmethod
    def _result_provenance_error(result: Any, route: RouteConfig) -> str | None:
        expected = (route.runtime, route.runtime_version, route.model, route.model_provider)
        actual = (result.runtime_name, result.runtime_version, result.model, result.model_provider)
        if actual == expected:
            return None
        return (
            "runtime provenance mismatch: "
            f"expected {expected[0]}=={expected[1]} {expected[3]}/{expected[2]}, "
            f"got {actual[0]}=={actual[1]} {actual[3]}/{actual[2]}"
        )

    def _status_before_failure(self, run_id: str) -> RunStatus:
        for event in reversed(self.database.list_events(run_id)):
            if event.event_type != "run.status_changed" or event.payload.get("to") != RunStatus.FAILED.value:
                continue
            previous = RunStatus(event.payload["from"])
            if previous in {
                RunStatus.PREPARING,
                RunStatus.REVIEWING,
                RunStatus.REVISING,
                RunStatus.VERIFYING,
            }:
                return previous
            break
        raise StateError("this failed run cannot be resumed safely; start a new run")

    def _task_workspace(self, run_id: str, task_id: str, source: Path, prompt: str) -> Path:
        workspace = self._run_dir(run_id) / "workspaces" / task_id
        if workspace.exists():
            shutil.rmtree(workspace)
        shutil.copytree(source, workspace)
        (workspace / "task.md").write_text(prompt, encoding="utf-8")
        return workspace

    def _session_dir(self, run_id: str, role: AgentRole, route: RouteConfig) -> Path:
        identity = digest_json(
            {
                "runtime": route.runtime,
                "role": role.value,
                "route": route.name,
            }
        )
        path = self._run_dir(run_id) / "sessions" / identity
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _build_copy(self, source: Path, destination: Path, manuscript: ManuscriptConfig) -> BuildResult:
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return self.manuscript.build(destination, manuscript)

    def _run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def _record_text(self, value: str, media_type: str):
        artifact = self.artifacts.put_text(value, media_type)
        return self.database.record_artifact(artifact)

    def _record_file(self, path: Path, media_type: str):
        artifact = self.artifacts.put_file(path, media_type)
        return self.database.record_artifact(artifact)

    def _prompt_record(self, role: AgentRole) -> dict[str, str]:
        content = self._load_prompt(role)
        return {"digest": self.artifacts.digest_bytes(content.encode("utf-8")), "content": content}

    @staticmethod
    def _load_prompt(role: AgentRole) -> str:
        return resources.files("scriptorium").joinpath("prompts", f"{role.value}.md").read_text(encoding="utf-8")

    @staticmethod
    def _directory_digest(path: Path) -> str:
        files = []
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            data = file_path.read_bytes()
            files.append(
                {
                    "path": file_path.relative_to(path).as_posix(),
                    "digest": ArtifactStore.digest_bytes(data),
                    "size": len(data),
                }
            )
        return digest_json(files)

    @staticmethod
    def _line_excerpt(path: Path, start_line: int, end_line: int) -> str:
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise ValueError(f"source anchor is not UTF-8 text: {path.name}") from exc
        return "".join(lines[start_line - 1 : end_line])

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _fail_active_run(self, run_id: str, exc: Exception) -> None:
        run = self.database.get_run(run_id)
        if run.status in {
            RunStatus.PREPARING,
            RunStatus.REVIEWING,
            RunStatus.REVISING,
            RunStatus.VERIFYING,
            RunStatus.READY_TO_APPLY,
        }:
            self.database.update_run(run_id, RunStatus.FAILED, str(exc))

    @staticmethod
    def _runtime_for_route(route: RouteConfig) -> AgentRuntime:
        expected_version = RUNTIME_SDK_VERSIONS.get(route.runtime)
        if expected_version is None:
            raise RuntimeUnavailable(f"Unsupported runtime: {route.runtime}")
        if route.runtime_version != expected_version:
            raise RuntimeUnavailable(
                f"Frozen route {route.name!r} requires {route.runtime}=={route.runtime_version}, "
                f"but this Scriptorium build supports {expected_version}."
            )
        runtime_type: type[AgentRuntime]
        if route.runtime == "codex":
            from .runtime.codex import CodexAgentRuntime

            runtime_type = CodexAgentRuntime
        elif route.runtime == "claude_code":
            from .runtime.claude_code import ClaudeCodeAgentRuntime

            runtime_type = ClaudeCodeAgentRuntime
        elif route.runtime == "antigravity":
            from .runtime.antigravity import AntigravityAgentRuntime

            runtime_type = AntigravityAgentRuntime
        else:
            raise RuntimeUnavailable(f"Unsupported runtime: {route.runtime}")
        return runtime_type(
            route=route.name,
            model=route.model,
            provider=route.model_provider,
            reasoning=route.reasoning_effort,
        )
