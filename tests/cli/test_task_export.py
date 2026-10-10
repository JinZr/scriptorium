import json

from scriptorium import cli
from scriptorium.errors import ConfigurationError

from ._fake_service import FakeService, install_fake_service


def test_export_maps_the_attempt_and_directory(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["--json", "task", "export", "attempt_1", "--dir", "out/bundle"]) == 0
    assert service.calls[-1] == ("export_task", "attempt_1", "out/bundle")
    assert json.loads(capsys.readouterr().out)["data"]["directory"] == "out/bundle"


def test_export_requires_a_directory_and_reports_refusals_as_structured_errors(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)

    assert cli.main(["--json", "task", "export", "attempt_1"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"
    assert service.calls == []

    service.error = ConfigurationError("export directory must not exist or must be an empty directory")
    assert cli.main(["--json", "task", "export", "attempt_1", "--dir", "used"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "configuration_error" and "empty directory" in error["message"]
