import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from scriptorium import cli
from scriptorium.errors import ConfigurationError, StateError

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


def test_owner_conflict_keeps_the_stable_json_error_envelope(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)
    message = (
        "run run_1 is already being changed by run start "
        "(pid 123, host test-host, acquired_at 2026-08-03T00:00:00+00:00, age 4s)"
    )
    service.error = StateError(message)

    assert cli.main(["--json", "run", "resume", "run_1"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ok": False,
        "error": {"code": "invalid_state", "message": message},
    }


def test_cross_process_cancellation_uses_the_interrupted_envelope(monkeypatch, capsys) -> None:
    service = FakeService()
    install_fake_service(monkeypatch, service)
    service.error = asyncio.CancelledError("cancelled by request")

    assert cli.main(["--json", "run", "resume", "run_1"]) == 3
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "ok": False,
        "error": {"code": "interrupted", "message": "operation interrupted"},
    }


def test_real_sigint_uses_exit_three_and_the_interrupted_json_envelope(tmp_path) -> None:
    ready = tmp_path / "ready"
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.cli._interrupt_driver", str(ready)],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail(f"interrupt driver exited early: {process.stderr.read()}")
            if time.monotonic() >= deadline:
                pytest.fail("interrupt driver did not become ready")
            time.sleep(0.02)
        os.kill(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 3
    assert stderr == ""
    assert json.loads(stdout) == {
        "ok": False,
        "error": {"code": "interrupted", "message": "operation interrupted"},
    }
