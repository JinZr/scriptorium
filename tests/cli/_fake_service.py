from dataclasses import dataclass
from pathlib import Path

from scriptorium import cli
from scriptorium.domain import RunStatus


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
        budget_usd: float | None = None,
        revision: str = "HEAD",
    ) -> dict:
        self.calls.append(("doctor", profile, budget_usd, revision))
        return self.doctor_result

    async def start_run(self, revision: str, profile: str, budget_usd: float | None) -> Result:
        return self._record("start_run", revision, profile, budget_usd)

    def get_run(self, run_id: str) -> Result:
        return self._record("get_run", run_id)

    async def resume_run(self, run_id: str) -> Result:
        return self._record("resume_run", run_id)

    async def retry_task(self, run_id: str, task_id: str, route: str | None = None) -> Result:
        return self._record("retry_task", run_id, task_id, route)

    def cancel_run(self, run_id: str, reason: str) -> Result:
        return self._record("cancel_run", run_id, reason)

    def render_report(self, run_id: str, format: str) -> str | dict:
        self.calls.append(("render_report", run_id, format))
        if format == "markdown":
            return "# Report"
        return {"run_id": run_id}

    def evaluate_gate(self, run_id: str) -> dict:
        self.calls.append(("evaluate_gate", run_id))
        return self.gate_result

    def list_findings(self, run_id: str) -> list[dict]:
        self.calls.append(("list_findings", run_id))
        return [{"id": "finding_1"}]

    def get_finding(self, finding_id: str) -> dict:
        self.calls.append(("get_finding", finding_id))
        return {"id": finding_id}

    def decide_finding(self, finding_id: str, decision: str, reason: str) -> Result:
        return self._record("decide_finding", finding_id, decision, reason)

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
