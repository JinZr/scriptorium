import json

from scriptorium import cli

from ._fake_service import FakeService, install_fake_service


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


def test_doctor_passes_default_and_explicit_revision(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["doctor"]) == 0
    assert service.calls[-1] == ("doctor", None, None, "HEAD")
    assert cli.main(["doctor", "--revision", "abc123", "--profile", "quick"]) == 0
    assert service.calls[-1] == ("doctor", "quick", None, "abc123")
    capsys.readouterr()


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
