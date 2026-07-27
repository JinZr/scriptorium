from dataclasses import dataclass
import json
from pathlib import Path

from scriptorium import cli
from scriptorium.domain import RunStatus
from scriptorium.errors import ConfigurationError


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

    def doctor(self, profile: str | None = None, budget_usd: float | None = None) -> dict:
        self.calls.append(("doctor", profile, budget_usd))
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


def test_start_emits_json_and_passes_route_inputs(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    exit_code = cli.main(
        [
            "run",
            "start",
            "--revision",
            "abc123",
            "--profile",
            "quick",
            "--budget-usd",
            "3.5",
            "--json",
        ]
    )

    assert exit_code == 0
    assert service.calls == [("start_run", "abc123", "quick", 3.5)]
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ok": True,
        "data": {"id": "result_1", "status": "reviewing"},
    }


def test_run_commands_dispatch_to_service(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    commands = [
        (["run", "status", "run_1"], ("get_run", "run_1")),
        (["run", "resume", "run_1"], ("resume_run", "run_1")),
        (
            ["run", "retry", "run_1", "--task", "task_1", "--route", "other"],
            ("retry_task", "run_1", "task_1", "other"),
        ),
        (["run", "cancel", "run_1", "--reason", "stop"], ("cancel_run", "run_1", "stop")),
    ]

    for arguments, expected in commands:
        assert cli.main(arguments) == 0
        assert service.calls[-1] == expected
    capsys.readouterr()


def test_finding_and_patch_decisions_map_flags(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["finding", "decide", "finding_1", "--waive", "--reason", "accepted risk"]) == 0
    assert service.calls[-1] == ("decide_finding", "finding_1", "waive", "accepted risk")

    assert cli.main(["patch", "decide", "patch_1", "--approve", "--reason", "looks good"]) == 0
    assert service.calls[-1] == ("decide_patch", "patch_1", "approve", "looks good")

    assert cli.main(["patch", "apply", "patch_1"]) == 0
    assert service.calls[-1] == ("apply_patch", "patch_1")
    capsys.readouterr()


def test_query_and_report_commands_dispatch(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["finding", "list", "run_1"]) == 0
    assert service.calls[-1] == ("list_findings", "run_1")
    assert cli.main(["finding", "show", "finding_1"]) == 0
    assert service.calls[-1] == ("get_finding", "finding_1")
    assert cli.main(["patch", "show", "patch_1"]) == 0
    assert service.calls[-1] == ("get_patch", "patch_1")

    assert cli.main(["run", "report", "run_1", "--format", "markdown"]) == 0
    assert service.calls[-1] == ("render_report", "run_1", "markdown")
    assert capsys.readouterr().out.endswith("# Report\n")

    assert cli.main(["run", "report", "run_1", "--format", "json"]) == 0
    assert service.calls[-1] == ("render_report", "run_1", "json")
    assert json.loads(capsys.readouterr().out) == {"run_id": "run_1"}


def test_gate_failure_is_a_domain_exit(monkeypatch, capsys) -> None:
    service = FakeService()
    service.gate_result = {"passed": False, "reasons": ["pending major finding"]}
    install_fake_service(monkeypatch, service)

    assert cli.main(["--json", "run", "gate", "run_1"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["data"]["passed"] is False


def test_stale_patch_is_a_domain_exit(monkeypatch, capsys) -> None:
    service = FakeService()
    service.apply_patch = lambda patch_id: {"id": patch_id, "status": "stale"}
    install_fake_service(monkeypatch, service)

    assert cli.main(["--json", "patch", "apply", "patch_1"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["status"] == "stale"


def test_doctor_reports_configuration_failure(monkeypatch, capsys) -> None:
    service = FakeService()
    service.doctor_result = {"ok": False, "checks": ["model is not configured"]}
    install_fake_service(monkeypatch, service)

    assert cli.main(["doctor", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ok": False,
        "error": {
            "code": "configuration_error",
            "message": "model is not configured",
        },
    }


def test_init_calls_configuration_boundary(monkeypatch, tmp_path, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        cli,
        "initialize_project",
        lambda repo, main, engine: calls.append((repo, main, engine)),
    )

    assert cli.main(["--json", "init", str(tmp_path), "--main", "paper.tex", "--engine", "xelatex"]) == 0
    assert calls == [(tmp_path.resolve(), "paper.tex", "xelatex")]
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["initialized"] is True


def test_argument_errors_use_stable_json_envelope(capsys) -> None:
    exit_code = cli.main(["--json", "finding", "decide", "finding_1", "--confirm"])

    assert exit_code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"]["code"] == "invalid_arguments"


def test_known_and_unexpected_errors_map_to_exit_codes(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    service.error = ConfigurationError("bad route")
    assert cli.main(["--json", "run", "status", "run_1"]) == 2
    known = json.loads(capsys.readouterr().out)
    assert known == {
        "ok": False,
        "error": {"code": "configuration_error", "message": "bad route"},
    }

    service.error = RuntimeError("database unavailable")
    assert cli.main(["--json", "run", "status", "run_1"]) == 3
    unexpected = json.loads(capsys.readouterr().out)
    assert unexpected == {
        "ok": False,
        "error": {"code": "infrastructure_error", "message": "database unavailable"},
    }
