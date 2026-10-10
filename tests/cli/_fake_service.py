from dataclasses import dataclass
from pathlib import Path

from scriptorium import cli
from scriptorium.domain import Run, RunStatus


@dataclass
class Result:
    id: str
    status: RunStatus


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.doctor_result = {"ok": True, "checks": []}
        self.gate_result = {"passed": True, "reasons": []}
        self.error: Exception | None = None

    def _record(self, *call: object) -> Result:
        if self.error:
            raise self.error
        self.calls.append(call)
        return Result("result_1", RunStatus.REVIEWING)

    def doctor(
        self,
        profile: str | None = None,
        revision: str = "HEAD",
    ) -> dict:
        self.calls.append(("doctor", profile, revision))
        return self.doctor_result

    async def start_run(
        self, revision: str, profile: str, allow_duplicate: bool = False, brief: str | None = None
    ) -> dict:
        return self._record_run("start_run", revision, profile, allow_duplicate, *(() if brief is None else (brief,)))

    def _record_run(self, *call: object) -> dict:
        self._record(*call)
        run = Run("/paper", "abc123", "tree", "quick", "config", {"execution": "external"}, id="run_1")
        return {"run": run, "tasks": [], "finding_ids": [], "patch_ids": []}

    def show_task(self, attempt_id: str) -> dict:
        self.calls.append(("show_task", attempt_id))
        return {"attempt_id": attempt_id}

    def task_view(self, context: dict, part: str | None = None, offset: int = 0) -> dict:
        self.calls.append(("task_view", context["attempt_id"], part, offset))
        return {"part": part, "offset": offset}

    def run_status(self, run_id: str) -> Result:
        return self._record("run_status", run_id)

    def list_runs(self, status: str | None, limit: int) -> Result:
        return self._record("list_runs", status, limit)

    async def resume_run(self, run_id: str) -> dict:
        return self._record_run("resume_run", run_id)

    async def retry_task(
        self, run_id: str, task_id: str, abandon_attempt_id: str | None = None, reason: str | None = None
    ) -> dict:
        return self._record_run("retry_task", run_id, task_id, abandon_attempt_id, reason)

    async def continue_review(self, run_id: str, task_id: str) -> dict:
        return self._record_run("continue_review", run_id, task_id)

    def cancel_run(self, run_id: str, reason: str) -> dict:
        return self._record_run("cancel_run", run_id, reason)

    def render_report(self, run_id: str, format: str) -> str | dict:
        self.calls.append(("render_report", run_id, format))
        if format == "markdown":
            return "# Report"
        return {"run_id": run_id}

    def read_report(self, run_id: str, part: str, offset: int, report_digest: str | None) -> dict:
        self.calls.append(("read_report", run_id, part, offset, report_digest))
        return {"run_id": run_id, "part": part, "offset": offset, "text": "[]", "next_command": None}

    def evaluate_gate(self, run_id: str) -> dict:
        self.calls.append(("evaluate_gate", run_id))
        return self.gate_result

    def list_findings(self, run_id: str) -> list[dict]:
        self.calls.append(("list_findings", run_id))
        return [{"id": "finding_1"}]

    def get_finding(self, finding_id: str) -> dict:
        self.calls.append(("get_finding", finding_id))
        return {"id": finding_id}

    def export_task(self, attempt_id: str, directory: str) -> dict:
        if self.error:
            raise self.error
        self.calls.append(("export_task", attempt_id, directory))
        return {"attempt_id": attempt_id, "directory": directory, "files": []}

    def decide_findings(self, finding_ids: list[str], decision: str, reason: str) -> Result:
        return self._record("decide_findings", finding_ids, decision, reason)

    def get_patch(self, patch_id: str) -> dict:
        self.calls.append(("get_patch", patch_id))
        return {"id": patch_id}

    def decide_patch(self, patch_id: str, decision: str, reason: str) -> Result:
        return self._record("decide_patch", patch_id, decision, reason)

    def apply_patch(self, patch_id: str) -> Result:
        return self._record("apply_patch", patch_id)


def install_fake_service(monkeypatch, service: FakeService) -> None:
    monkeypatch.setattr(cli, "find_repo", lambda path: Path("/paper"))
    monkeypatch.setattr(cli, "_build_service", lambda repo: service)
