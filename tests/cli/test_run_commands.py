import json

import pytest

from scriptorium import cli
from scriptorium.errors import DuplicateRunError

from ._fake_service import FakeService, install_fake_service


def test_start_emits_json_and_passes_frozen_inputs(monkeypatch, capsys) -> None:
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
            "--json",
        ]
    )

    assert exit_code == 0
    assert service.calls == [("start_run", "abc123", "quick", False)]
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["data"]["run"] == {"id": "run_1", "status": "preparing", "commit_sha": "abc123"}
    assert output["data"]["next_actions"] == [{"command": "run status", "run_id": "run_1"}]


def test_start_passes_the_brief_file_text_to_the_service(monkeypatch, capsys, tmp_path) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)
    brief = tmp_path / "brief.json"
    brief.write_text('{"venue_family": "other", "stage": "internal_draft"}', encoding="utf-8")

    assert cli.main(["--json", "run", "start", "--profile", "quick", "--brief", str(brief)]) == 0

    assert service.calls == [("start_run", "HEAD", "quick", False, brief.read_text(encoding="utf-8"))]
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize(
    ("contents", "message"),
    [(None, "cannot read review brief"), (b"\xff", "must be UTF-8"), (b" " * 256_001, "256 KB")],
)
def test_start_rejects_an_unreadable_brief_before_calling_the_service(
    monkeypatch, capsys, tmp_path, contents, message
) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)
    brief = tmp_path / "brief.json"
    if contents is not None:
        brief.write_bytes(contents)

    assert cli.main(["--json", "run", "start", "--brief", str(brief)]) == 2

    assert service.calls == []
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "configuration_error" and message in error["message"]


def test_task_show_accepts_the_brief_part(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["--json", "task", "show", "attempt_1", "--part", "brief", "--offset", "3"]) == 0
    assert cli.main(["--json", "task", "show", "attempt_1", "--part", "summary"]) == 2

    assert service.calls == [("show_task", "attempt_1"), ("task_view", "attempt_1", "brief", 3)]


def test_start_passes_allow_duplicate(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["run", "start", "--revision", "abc123", "--profile", "quick", "--allow-duplicate"]) == 0
    assert service.calls == [("start_run", "abc123", "quick", True)]
    capsys.readouterr()


def test_duplicate_run_error_is_structured_json(monkeypatch, capsys) -> None:
    service = FakeService()
    service.error = DuplicateRunError("run run_9 is already reviewing")
    install_fake_service(monkeypatch, service)

    assert cli.main(["run", "start", "--json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"]["code"] == "duplicate_run"
    assert "run_9" in output["error"]["message"]


def test_doctor_passes_default_and_explicit_revision(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["doctor"]) == 0
    assert service.calls[-1] == ("doctor", None, "HEAD")
    assert cli.main(["doctor", "--revision", "abc123", "--profile", "quick"]) == 0
    assert service.calls[-1] == ("doctor", "quick", "abc123")
    capsys.readouterr()


def test_run_commands_dispatch_to_service(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    commands = [
        (["run", "status", "run_1"], ("run_status", "run_1")),
        (["run", "resume", "run_1"], ("resume_run", "run_1")),
        (["run", "continue", "run_1", "--task", "task_1"], ("continue_review", "run_1", "task_1")),
        (
            ["run", "retry", "run_1", "--task", "task_1"],
            ("retry_task", "run_1", "task_1", None, None),
        ),
        (
            ["run", "retry", "run_1", "--task", "task_1", "--abandon-attempt", "attempt_1", "--reason", "lost"],
            ("retry_task", "run_1", "task_1", "attempt_1", "lost"),
        ),
        (["run", "cancel", "run_1", "--reason", "stop"], ("cancel_run", "run_1", "stop")),
    ]

    for arguments, expected in commands:
        service.calls.clear()
        assert cli.main(arguments) == 0
        assert service.calls == [expected]
    capsys.readouterr()


@pytest.mark.parametrize(
    "arguments",
    [
        ["doctor", "--budget-usd", "1"],
        ["run", "start", "--budget-usd", "1"],
        ["run", "retry", "run_1", "--task", "task_1", "--route", "old"],
    ],
)
def test_retired_budget_and_route_options_require_migration(monkeypatch, capsys, arguments) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["--json", *arguments]) == 2
    assert "retired internal model runner" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert service.calls == []


def test_query_and_report_commands_dispatch(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["finding", "list", "run_1"]) == 0
    assert service.calls[-1] == ("list_findings", "run_1")
    assert cli.main(["finding", "show", "finding_1"]) == 0
    assert service.calls[-1] == ("get_finding", "finding_1")
    assert cli.main(["patch", "show", "patch_1"]) == 0
    assert service.calls[-1] == ("get_patch", "patch_1")

    assert cli.main(["run", "report", "run_1"]) == 0
    assert service.calls[-1] == ("render_report", "run_1", "markdown")
    assert capsys.readouterr().out.endswith("# Report\n")

    assert cli.main(["run", "report", "run_1", "--format", "markdown"]) == 0
    assert service.calls[-1] == ("render_report", "run_1", "markdown")
    assert capsys.readouterr().out.endswith("# Report\n")

    assert cli.main(["run", "report", "run_1", "--format", "json"]) == 0
    assert service.calls[-1] == ("render_report", "run_1", "json")
    assert json.loads(capsys.readouterr().out) == {"run_id": "run_1"}


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


def test_report_fragments_dispatch_with_continuation_identity(monkeypatch, capsys):
    service = FakeService()
    install_fake_service(monkeypatch, service)
    assert cli.main(["--json", "run", "report", "run_1", "--part", "findings"]) == 0
    assert service.calls[-1] == ("read_report", "run_1", "findings", 0, None)
    assert json.loads(capsys.readouterr().out)["data"]["text"] == "[]"
    assert (
        cli.main(
            ["--json", "run", "report", "run_1", "--part", "findings", "--offset", "120", "--report-digest", "abc"]
        )
        == 0
    )
    assert service.calls[-1] == ("read_report", "run_1", "findings", 120, "abc")


def test_decision_stats_part_dispatches_through_the_bounded_report_reader(monkeypatch, capsys):
    service = FakeService()
    install_fake_service(monkeypatch, service)
    assert cli.main(["--json", "run", "report", "run_1", "--part", "decision_stats"]) == 0
    assert service.calls[-1] == ("read_report", "run_1", "decision_stats", 0, None)
    assert json.loads(capsys.readouterr().out)["data"]["part"] == "decision_stats"


@pytest.mark.parametrize(
    "options",
    [
        ["--part", "missing"],
        ["--offset", "3"],
        ["--report-digest", "abc"],
        ["--part", "findings", "--format", "json"],
        ["--part", "findings", "--format", "markdown"],
        ["--format", "json", "--part", "findings"],
        ["--format", "markdown", "--part", "findings"],
    ],
)
def test_report_fragment_options_are_not_silently_ignored(monkeypatch, capsys, options):
    service = FakeService()
    install_fake_service(monkeypatch, service)
    assert cli.main(["--json", "run", "report", "run_1", *options]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert service.calls == []
