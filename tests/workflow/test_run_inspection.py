import asyncio
import json
import subprocess
import sys

import pytest

from scriptorium import cli
from scriptorium.domain import AgentRole, RunStatus
from scriptorium.errors import ConfigurationError, InfrastructureError, NotFoundError
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import REPORT_PARTS, success_json

from ._support import PdfBuildingManuscriptManager, claim, make_repository, prepare_checked, review_finding, submit


@pytest.mark.parametrize("json_output", [True, False], ids=["json", "plain"])
def test_status_and_report_parts_return_the_full_report(tmp_path, monkeypatch, capsys, json_output):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    options = ["--json"] if json_output else []
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)

        def command(arguments):
            assert cli.main(arguments) == 0
            value = json.loads(capsys.readouterr().out)
            return value["data"] if json_output else value

        overview = command([*options, "run", "start", "--profile", "quick"])
        run_id = overview["run"]["id"]
        assert overview["task_counts"] == {"pending": 1}
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        failed = asyncio.run(service.submit_task(context["attempt"].id, context["input_digest"], "{invalid"))
        assert failed["attempt"].status.value == "failed"
        command([*options, "run", "retry", run_id, "--task", context["task"].id])
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = {**review_finding(context), "explanation": 'Evidence 🧬 实验 "quoted"\\\n' * 1800}
        submit(service, context, {"summary": "Review complete.", "findings": [finding]})
        overview = command([*options, "run", "status", run_id])
        assert overview["run"]["status"] == "awaiting_decision"
        assert overview["finding_count"] == 1
        assert not {"execution", "estimated_cost_usd"} & set(overview)
        assert overview["next_actions"] == service.list_tasks(run_id)["next_actions"]
        assert overview["next_actions"][0]["requires_human_decision"] is True
        assert "frozen_config" not in overview["run"]
        full = service.render_report(run_id, "json")
        events = service.store.list_events(run_id)
        assert len(success_json(full).encode()) > 100_000
        assert set(full) == set(REPORT_PARTS)
        assert full["validation_reports"]
        assert set(overview["report_parts"]) == set(REPORT_PARTS)
        for part in overview["report_parts"]:
            assert command([*options, "run", "report", run_id, "--part", part]) == full[part]
        assert service.store.list_events(run_id) == events
        assert cli.main(["run", "report", run_id, "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out) == full
        assert cli.main(["run", "report", run_id, "--format", "markdown"]) == 0
        assert capsys.readouterr().out == service.render_report(run_id, "markdown") + "\n"
        cancelled = command([*options, "run", "cancel", run_id, "--reason", "Stop."])
        assert cancelled["run"]["status"] == "cancelled"
        assert cancelled["next_actions"] == [{"command": "run status", "run_id": run_id}]
        assert command([*options, "run", "status", run_id])["next_actions"] == []


@pytest.mark.parametrize("json_output", [True, False], ids=["json", "plain"])
def test_a_report_part_is_one_complete_json_value_across_processes(tmp_path, json_output):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run_id = asyncio.run(service.start_run("HEAD", "quick"))["run"].id
        expected = service.render_report(run_id, "json")["findings_grouped"]
    options = ["--json"] if json_output else []
    command = [sys.executable, "-m", "scriptorium", *options, "run", "report", run_id, "--part", "findings_grouped"]
    result = subprocess.run(command, cwd=repo, text=True, capture_output=True, check=True)
    value = json.loads(result.stdout)
    assert (value["data"] if json_output else value) == expected


def test_report_parts_are_the_report_sections(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run_id = asyncio.run(service.start_run("HEAD", "quick"))["run"].id
        claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        report = service.render_report(run_id, "json")
        for part in REPORT_PARTS:
            assert service.read_report(run_id, part) == report[part]
        with pytest.raises(ConfigurationError, match="unknown report part"):
            service.read_report(run_id, "missing")


def test_status_preserves_human_gates(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run_id = asyncio.run(service.start_run("HEAD", "quick"))["run"].id
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        submit(
            service,
            context,
            {"summary": "Reviewed.", "findings": [review_finding(context)], "limitations": ["Not inspected."]},
        )
        status = service.run_status(run_id)
        assert status["run"]["status"] == RunStatus.AWAITING_DECISION.value
        decide = {"command": "finding list", "run_id": run_id, "requires_human_decision": True}
        assert status["next_actions"] == [decide]
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        assert cli.main(["--json", "run", "status", run_id]) == 0
        assert json.loads(capsys.readouterr().out)["data"]["next_actions"] == status["next_actions"]
        patch, _ = prepare_checked(service, run_id)
        status = service.run_status(run_id)
        assert status["run"]["status"] == RunStatus.AWAITING_PATCH_APPROVAL.value
        assert status["next_actions"] == [
            {"command": "patch show", "patch_id": patch.id, "requires_human_decision": True}
        ]
        service.decide_patch(patch.id, "approve", "The author approved the checked patch.")
        asyncio.run(service.resume_run(run_id))
        status = service.run_status(run_id)
        assert status["run"]["status"] == RunStatus.READY_TO_APPLY.value
        assert status["next_actions"] == [
            {"command": "patch apply", "patch_id": patch.id, "requires_human_decision": True}
        ]
        assert not service.evaluate_gate(run_id)["passed"]


def test_report_parts_do_not_hide_corrupt_accepted_outputs(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run_id = asyncio.run(service.start_run("HEAD", "quick"))["run"].id
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        receipt = submit(service, context, {"summary": "Complete.", "findings": []})
        digest = receipt["output_digest"]
        (repo / ".scriptorium/artifacts/sha256" / digest[:2] / digest[2:]).write_text("corrupt")
        with pytest.raises(InfrastructureError):
            service.read_report(run_id, "findings")
        assert not service.evaluate_gate(run_id)["passed"]


def test_inspection_resolves_run_identity_before_opening_lock_paths(tmp_path):
    repo = make_repository(tmp_path)
    with ScriptoriumService(repo) as service:
        sentinel = repo / ".scriptorium/sentinel.lock"
        sentinel.write_text("preserve")
        for run_id in ["../sentinel", "../../outside", "missing"]:
            with pytest.raises(NotFoundError):
                service.run_status(run_id)
            with pytest.raises(NotFoundError):
                service.read_report(run_id, "run")
        assert sentinel.read_text() == "preserve"
        assert not (repo / ".scriptorium/locks").exists()
