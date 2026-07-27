from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from enum import Enum
import fcntl
from hashlib import sha256
from importlib import metadata
from pathlib import Path
import shutil
import sqlite3
import subprocess
from typing import Any, Iterator

from .artifacts import ArtifactStore
from .config import find_repo, load_local_config, load_project_config, validate_ready
from .domain import FindingSeverity, FindingStatus, Patch, PatchStatus, RunStatus, TaskStatus, VerificationResult
from .errors import ConfigurationError, InfrastructureError, NotFoundError, StateError
from .manuscript import ManuscriptManager
from .runtime import CODEX_SDK_VERSION
from .storage import ConflictError, Database, NotFoundError as StorageNotFoundError, StorageError
from .workflow import Armarius, RuntimeFactory


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
        self.project_config = load_project_config(self.repo)
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
    ) -> dict[str, Any]:
        selected_profile = profile or (
            "full" if "full" in self.project_config.profiles else next(iter(self.project_config.profiles))
        )
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, message: str) -> None:
            checks.append({"name": name, "ok": ok, "message": message})

        git_path = shutil.which("git")
        if git_path is None:
            check("git_repository", False, "git was not found")
            tracked_config = None
        else:
            git = subprocess.run(
                [git_path, "-C", str(self.repo), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                check=False,
            )
            check("git_repository", git.returncode == 0, git.stderr.strip() or git.stdout.strip())
            tracked_config = subprocess.run(
                [git_path, "-C", str(self.repo), "ls-files", "--error-unmatch", "scriptorium.toml"],
                capture_output=True,
                text=True,
                check=False,
            )
        config_tracked = tracked_config is not None and tracked_config.returncode == 0
        check(
            "tracked_project_config",
            config_tracked,
            "scriptorium.toml is tracked" if config_tracked else "scriptorium.toml is not tracked",
        )
        main_path = self.repo / self.project_config.manuscript.main
        check("manuscript_main", main_path.is_file(), str(main_path))
        latexmk = shutil.which("latexmk")
        check("latexmk", latexmk is not None, latexmk or "latexmk was not found")
        engine = shutil.which(self.project_config.manuscript.engine)
        check(
            "latex_engine",
            engine is not None,
            engine or f"{self.project_config.manuscript.engine} was not found",
        )
        try:
            version = metadata.version("openai-codex")
            codex_ok = version == CODEX_SDK_VERSION
            codex_message = version
        except metadata.PackageNotFoundError:
            codex_ok = False
            codex_message = "openai-codex is not installed"
        check("codex_sdk", codex_ok, codex_message)
        try:
            validate_ready(self.project_config, self.local_config, selected_profile, budget_usd)
        except ConfigurationError as exc:
            check("model_routes", False, str(exc))
        else:
            check("model_routes", True, f"profile {selected_profile}")
        check("sqlite", True, str(self.state_dir / "state.sqlite3"))

        failed = [item for item in checks if not item["ok"]]
        infrastructure_names = {
            "git_repository",
            "manuscript_main",
            "latexmk",
            "latex_engine",
            "codex_sdk",
            "sqlite",
        }
        exit_code = 3 if any(item["name"] in infrastructure_names for item in failed) else 2 if failed else 0
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
        run = await self.armarius.start_run(revision, profile, budget_usd)
        return self.get_run(run.id)

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
        with self._run_lock(run_id):
            await self.armarius.resume_run(run_id)
        return self.get_run(run_id)

    async def retry_task(
        self,
        run_id: str,
        task_id: str,
        route: str | None = None,
    ) -> dict[str, Any]:
        with self._run_lock(run_id):
            await self.armarius.retry_task(run_id, task_id, route)
        return self.get_run(run_id)

    def cancel_run(self, run_id: str, reason: str) -> dict[str, Any]:
        with self._run_lock(run_id):
            self._storage(self.armarius.cancel_run, run_id, reason)
        return self.get_run(run_id)

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
        with self._run_lock(initial.run_id):
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
        with self._run_lock(initial.run_id):
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
        with self._run_lock(initial.run_id):
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

    @contextmanager
    def _run_lock(self, run_id: str) -> Iterator[None]:
        locks = self.state_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        path = locks / f"{run_id}.lock"
        with path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateError(f"run {run_id} is already being changed") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
