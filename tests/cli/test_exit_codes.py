import json

from scriptorium import cli
from scriptorium.errors import ConfigurationError

from ._fake_service import FakeService, install_fake_service


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
