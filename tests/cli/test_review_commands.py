from scriptorium import cli

from ._fake_service import FakeService, install_fake_service


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
