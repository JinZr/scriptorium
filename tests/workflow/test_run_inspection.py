import asyncio
from hashlib import sha256
import json
import shlex
import subprocess
import sys

import pytest

from scriptorium import cli
from scriptorium.domain import AgentRole, RunStatus, canonical_json, digest_json
from scriptorium.errors import ConfigurationError, InfrastructureError, NotFoundError
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES, REPORT_PARTS, success_json

from ._support import PdfBuildingManuscriptManager, claim, make_repository, prepare_verification, review_finding, submit


@pytest.mark.parametrize("json_output", [True, False], ids=["json", "plain"])
def test_bounded_status_and_report_parts_reconstruct_full_report(tmp_path, monkeypatch, capsys, json_output):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)

        def command(arguments):
            arguments = [argument for argument in arguments if argument != "--json"]
            assert cli.main((["--json"] if json_output else []) + arguments) == 0
            raw = capsys.readouterr().out
            assert len(raw.encode()) <= MAX_TOOL_RESPONSE_BYTES
            value = json.loads(raw)
            return value["data"] if json_output else value

        overview = command(["run", "start", "--profile", "quick"])
        run_id = overview["run"]["id"]
        assert overview["task_counts"] == {"pending": 1}
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        failed = asyncio.run(service.submit_task(context["attempt"].id, context["input_digest"], "{invalid"))
        assert failed["attempt"].status.value == "failed"
        command(["run", "retry", run_id, "--task", context["task"].id])
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        finding = {**review_finding(context), "explanation": 'Evidence 🧬 实验 "quoted"\\\n' * 1800}
        submit(service, context, {"summary": "Review complete.", "findings": [finding]})
        overview = command(["run", "status", run_id])
        assert overview["run"]["status"] == "awaiting_decision"
        assert overview["finding_count"] == 1
        assert overview["estimated_cost_usd"] is None
        assert overview["next_actions"] == service.list_tasks(run_id)["next_actions"]
        assert overview["next_actions"][0]["requires_human_decision"] is True
        assert "frozen_config" not in overview["run"]
        full = service.render_report(run_id, "json")
        events = service.database.list_events(run_id)
        assert len(success_json(full).encode()) > 100_000
        assert set(full) == set(REPORT_PARTS)
        assert full["validation_reports"]
        for part, next_command in overview["report_parts"].items():
            text = ""
            while next_command:
                fragment = command(shlex.split(next_command)[1:])
                assert fragment["offset"] == len(text)
                assert fragment["part"] == part
                assert fragment["report_digest"] == digest_json(full)
                text += fragment["text"]
                next_command = fragment["next_command"]
            assert len(text) == fragment["total_chars"]
            assert fragment["next_offset"] is None
            assert sha256(text.encode()).hexdigest() == fragment["digest"]
            assert text == canonical_json(full[part])
        assert service.database.list_events(run_id) == events
        assert cli.main(["run", "report", run_id, "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out) == full
        assert cli.main(["run", "report", run_id, "--format", "markdown"]) == 0
        assert capsys.readouterr().out == service.render_report(run_id, "markdown") + "\n"
        cancelled = command(["run", "cancel", run_id, "--reason", "Stop."])
        assert cancelled["run"]["status"] == "cancelled"
        assert cancelled["next_actions"] == [{"command": "run status", "run_id": run_id}]
        assert command(["run", "status", run_id])["next_actions"] == []


def test_report_continuation_crosses_processes_and_rejects_changed_state(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run_id = asyncio.run(service.start_run("HEAD", "quick"))["run"].id
        first = service.read_report(run_id, "run")
        assert first["next_command"]
    command = [sys.executable, "-m", "scriptorium", *shlex.split(first["next_command"])[1:]]
    continued = subprocess.run(command, cwd=repo, text=True, capture_output=True, check=True)
    assert len(continued.stdout.encode()) <= MAX_TOOL_RESPONSE_BYTES
    assert json.loads(continued.stdout)["data"]["offset"] == first["next_offset"]
    with ScriptoriumService(repo) as service:
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        # The run part is unchanged by claiming a task, but the whole report has changed.
        assert digest_json(service.render_report(run_id, "json")["run"]) == first["digest"]
        with pytest.raises(ConfigurationError, match="report changed"):
            service.read_report(run_id, "run", first["next_offset"], first["report_digest"])
        refreshed = service.read_report(run_id, "run")
        assert refreshed["report_digest"] != first["report_digest"]
        assert (
            service.read_report(run_id, "tasks", 0, refreshed["report_digest"])["report_digest"]
            == refreshed["report_digest"]
        )
        assert service.database.get_attempt(context["attempt"].id).status.value == "running"


@pytest.mark.parametrize("offset,digest", [(1, None), (-1, None), (10**9, None), (0, "wrong")])
def test_report_invalid_cursors_are_rejected(tmp_path, offset, digest):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run_id = asyncio.run(service.start_run("HEAD", "quick"))["run"].id
        with pytest.raises(ConfigurationError):
            service.read_report(run_id, "run", offset, digest)
        first = service.read_report(run_id, "findings")
        end = service.read_report(run_id, "findings", first["total_chars"], first["report_digest"])
        assert end["text"] == "" and end["next_command"] is None
        with pytest.raises(ConfigurationError, match="outside"):
            service.read_report(run_id, "findings", first["total_chars"] + 1, first["report_digest"])
        with pytest.raises(ConfigurationError, match="unknown report part"):
            service.read_report(run_id, "missing")


def test_status_preserves_partial_continuation_and_human_gates(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run_id = asyncio.run(service.start_run("HEAD", "quick"))["run"].id
        context = claim(service, run_id, AgentRole.SUBSTANTIVE_REVIEW)
        submit(
            service,
            context,
            {
                "summary": "A page remains.",
                "findings": [],
                "scope": {
                    "completion": "partial",
                    "checked": [],
                    "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
                    "limitations": ["Not inspected."],
                },
            },
        )
        status = service.run_status(run_id)
        assert status["next_actions"] == [{"command": "run continue", "run_id": run_id, "task_id": context["task"].id}]
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        assert cli.main(["--json", "run", "continue", run_id, "--task", context["task"].id]) == 0
        raw = capsys.readouterr().out
        assert len(raw.encode()) <= MAX_TOOL_RESPONSE_BYTES
        assert json.loads(raw)["data"]["next_actions"] == [{"command": "run status", "run_id": run_id}]
        assert service.run_status(run_id)["next_actions"] == [{"command": "task claim", "task_id": context["task"].id}]
        patch, finding_id = prepare_verification(service, run_id)
        verification = claim(service, run_id, AgentRole.VERIFICATION, session="independent-verifier")
        receipt = submit(
            service,
            verification,
            {
                "summary": "The approved correction is sound.",
                "verdict": "pass",
                "resolved_finding_ids": [finding_id],
                "issues": [],
            },
        )
        assert receipt["attempt"].status.value == "completed"
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
