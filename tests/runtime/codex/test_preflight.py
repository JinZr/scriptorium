import json
import os
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace

import pytest

from scriptorium.runtime.base import CODEX_SDK_VERSION, RuntimeUnavailable
from scriptorium.runtime.codex import probe_native_configuration
from scriptorium.runtime.codex_preflight import codex_startup_preflight


def test_real_preflight_does_not_use_user_configuration_or_credentials(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    user_home.mkdir()
    (user_home / "config.toml").write_text("invalid TOML [")
    (user_home / "auth.json").write_text('{"OPENAI_API_KEY":"synthetic-secret"}')
    before = {path.name: path.read_bytes() for path in user_home.iterdir()}
    for key in ("HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(user_home))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-secret")
    monkeypatch.setenv("PYTHONPATH", "/unrelated/python/path")
    message = codex_startup_preflight()
    assert "synthetic configuration read passed" in message
    assert "authentication" in message and "unverified" in message
    assert "synthetic-secret" not in message
    assert {path.name: path.read_bytes() for path in user_home.iterdir()} == before


@pytest.mark.parametrize("problem", [None, "timeout", "interrupt", "failure", "malformed", "wrong-version"])
def test_preflight_bounds_process_and_redacts_output(tmp_path, monkeypatch, problem):
    processes = []
    killed = []
    monkeypatch.setenv("PRIVATE_TOKEN", "do-not-inherit")

    class Process:
        pid = 999999
        returncode = 1 if problem == "failure" else 0

        def __init__(self, command, **kwargs):
            self.kwargs = kwargs
            self.waited = False
            processes.append(self)
            assert command[1:] == ["-m", "scriptorium.runtime.codex_preflight"]
            assert kwargs["start_new_session"] is True
            assert (Path(kwargs["cwd"]) / "manifest.json").read_text() == "{}"
            assert "PRIVATE_TOKEN" not in kwargs["env"]
            assert kwargs["env"]["HOME"] == str(kwargs["cwd"])
            assert kwargs["env"]["CODEX_HOME"] == str(kwargs["cwd"])
            assert kwargs["stderr"] is subprocess.DEVNULL

        def communicate(self, *, timeout):
            assert timeout == 30
            if problem == "timeout":
                raise subprocess.TimeoutExpired("probe", timeout)
            if problem == "interrupt":
                raise KeyboardInterrupt
            if problem in {"failure", "malformed"}:
                return "PRIVATE-secret-error", None
            version = "wrong" if problem == "wrong-version" else CODEX_SDK_VERSION
            return json.dumps({"ok": True, "sdk_version": version}), None

        def wait(self):
            self.waited = True

    monkeypatch.setattr("scriptorium.runtime.codex_preflight.subprocess.Popen", Process)
    monkeypatch.setattr("scriptorium.runtime.codex_preflight.os.killpg", lambda *args: killed.append(args))
    if problem == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            codex_startup_preflight()
    elif problem:
        with pytest.raises(RuntimeUnavailable) as error:
            codex_startup_preflight()
        assert "PRIVATE-secret" not in str(error.value)
    else:
        assert "passed" in codex_startup_preflight()
    assert killed == [(Process.pid, signal.SIGKILL)]
    assert processes[0].waited
    assert not Path(processes[0].kwargs["cwd"]).exists()


def test_preflight_launch_failure_is_normalized(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("private detail")

    monkeypatch.setattr("scriptorium.runtime.codex_preflight.subprocess.Popen", unavailable)
    with pytest.raises(RuntimeUnavailable, match="Cannot launch") as error:
        codex_startup_preflight()
    assert "private detail" not in str(error.value)


@pytest.mark.parametrize("mismatch", [False, True])
def test_probe_only_initializes_and_reads_synthetic_config(monkeypatch, capsys, mismatch):
    import openai_codex
    import openai_codex.client

    calls = []

    class Client:
        def __init__(self, config):
            calls.append(config)
            self.values = {
                key: json.loads(value)
                for override in config.config_overrides
                for key, _, value in [override.partition("=")]
                if not key.startswith("model_providers.")
            }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("close")

        def initialize(self):
            calls.append("initialize")

        def request(self, method, params, *, response_model):
            calls.append(method)
            assert params == {"includeLayers": False}
            if mismatch:
                self.values["model_provider"] = "wrong"
            return SimpleNamespace(config=SimpleNamespace(model_dump=lambda **kwargs: self.values))

    monkeypatch.setattr(openai_codex.client, "CodexClient", Client)
    if mismatch:
        with pytest.raises(RuntimeError, match="differs"):
            probe_native_configuration()
        assert capsys.readouterr().out == ""
    else:
        probe_native_configuration()
        assert capsys.readouterr().out == ""
    assert calls[1:] == ["initialize", "config/read", "close"]
    assert calls[0].cwd == os.getcwd()
    assert 'model_provider="scriptorium_preflight"' in calls[0].config_overrides
