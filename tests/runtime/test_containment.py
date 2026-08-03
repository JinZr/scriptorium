from __future__ import annotations

import asyncio
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from scriptorium.config import RouteConfig
from scriptorium.domain import AgentRole
from scriptorium.errors import InfrastructureError
from scriptorium.runtime import AgentCancelled
from scriptorium.runtime.contained import (
    ContainedAgentRuntime,
    _AsyncJsonChannel,
    _process_group_has_live_members,
    _terminate_process_group,
    cleanup_native_config_dirs,
    wait_for_provider_cleanup,
)
from scriptorium.runtime.worker import _cancel_watchdog

_FAKE_FACTORY = f"{Path(__file__).with_name('_contained_fakes.py').resolve()}:make_runtime"


def test_completed_attempt_runs_in_a_dedicated_posix_session(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_complete")
    observed_sessions = []
    runtime = _runtime(repo, "complete")

    result = asyncio.run(
        runtime.run_agent(
            "task",
            AgentRole.COPYEDIT,
            repo / "workspace",
            {},
            session_dir,
            on_session_started=observed_sessions.append,
        )
    )

    state = json.loads(result.final_response)
    assert result.status == "completed"
    assert observed_sessions == ["thread-contained"]
    assert state["pid"] == state["pgid"]
    assert state["pgid"] != os.getpgrp()
    wait_for_provider_cleanup(repo, "run_complete", timeout=0.1)


def test_cancellation_returns_interrupted_result_and_releases_barrier(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_cancel")
    runtime = _runtime(repo, "block")

    async def exercise():
        invocation = asyncio.create_task(
            runtime.run_agent("task", AgentRole.COPYEDIT, repo / "workspace", {}, session_dir)
        )
        await _wait_for_path(session_dir / "worker-state.json")
        with pytest.raises(InfrastructureError, match="did not finish within 0.1s"):
            await asyncio.to_thread(wait_for_provider_cleanup, repo, "run_cancel", 0.1)
        invocation.cancel()
        with pytest.raises(AgentCancelled) as cancelled:
            await invocation
        return cancelled.value.result

    result = asyncio.run(exercise())

    worker_pid = json.loads((session_dir / "worker-state.json").read_text(encoding="utf-8"))["pid"]
    assert result.status == "interrupted"
    assert result.thread_id == "thread-contained"
    assert (session_dir / "cancelled.json").is_file()
    with pytest.raises(ChildProcessError):
        os.waitpid(worker_pid, os.WNOHANG)
    wait_for_provider_cleanup(repo, "run_cancel", timeout=0.1)


def test_cancellation_before_ready_reaps_the_worker_without_starting_the_sdk(tmp_path, monkeypatch):
    repo, session_dir = _layout(tmp_path, "run_before_ready")
    runtime = _runtime(repo, "block")
    receive_started = asyncio.Event()

    async def block_ready(_channel):
        receive_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_AsyncJsonChannel, "receive", block_ready)

    async def exercise():
        invocation = asyncio.create_task(
            runtime.run_agent("task", AgentRole.COPYEDIT, repo / "workspace", {}, session_dir)
        )
        await receive_started.wait()
        invocation.cancel()
        with pytest.raises(AgentCancelled):
            await invocation

    asyncio.run(exercise())

    assert not (session_dir / "worker-state.json").exists()
    wait_for_provider_cleanup(repo, "run_before_ready", timeout=0.1)


def test_cancellation_while_invoke_is_being_sent_still_cancels_and_reaps_the_worker(tmp_path, monkeypatch):
    repo, session_dir = _layout(tmp_path, "run_during_invoke")
    runtime = _runtime(repo, "block")
    invoke_sent = asyncio.Event()
    original_send = _AsyncJsonChannel.send

    async def pause_after_invoke(channel, message):
        await original_send(channel, message)
        if message.get("type") == "INVOKE":
            invoke_sent.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(_AsyncJsonChannel, "send", pause_after_invoke)

    async def exercise():
        invocation = asyncio.create_task(
            runtime.run_agent("task", AgentRole.COPYEDIT, repo / "workspace", {}, session_dir)
        )
        await invoke_sent.wait()
        invocation.cancel()
        with pytest.raises(AgentCancelled) as caught:
            await invocation
        return caught.value.result

    result = asyncio.run(exercise())

    assert result.status == "interrupted"
    assert (session_dir / "cancelled.json").is_file()
    wait_for_provider_cleanup(repo, "run_during_invoke", timeout=0.1)


def test_session_callback_failure_cancels_worker_before_reraising(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_callback")
    runtime = _runtime(repo, "block")

    def reject_session(thread_id):
        assert thread_id == "thread-contained"
        raise RuntimeError("attempt session CAS failed")

    with pytest.raises(RuntimeError, match="attempt session CAS failed"):
        asyncio.run(
            runtime.run_agent(
                "task",
                AgentRole.COPYEDIT,
                repo / "workspace",
                {},
                session_dir,
                on_session_started=reject_session,
            )
        )

    assert (session_dir / "cancelled.json").is_file()
    wait_for_provider_cleanup(repo, "run_callback", timeout=0.1)


def test_unexpected_worker_exit_is_an_infrastructure_error(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_exit")
    runtime = _runtime(repo, "exit")

    with pytest.raises(InfrastructureError, match="exited without returning a result"):
        asyncio.run(runtime.run_agent("task", AgentRole.COPYEDIT, repo / "workspace", {}, session_dir))

    wait_for_provider_cleanup(repo, "run_exit", timeout=0.1)


def test_owner_sigkill_is_observed_as_control_eof(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_owner")
    owner = subprocess.Popen(
        [sys.executable, "-m", "tests.runtime._contained_fakes", str(repo), "thread"],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    try:
        _wait_for_path_sync(session_dir / "native-thread.json")
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=5)
        _wait_for_path_sync(session_dir / "cancelled.json")
        wait_for_provider_cleanup(repo, "run_owner", timeout=15)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_worker_does_not_inherit_the_parent_run_lock(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_fd_scope")
    runtime = _runtime(repo, "block")
    run_lock = repo / ".scriptorium" / "locks" / "run_fd_scope.lock"
    owner_descriptor = os.open(run_lock, os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(owner_descriptor, True)
    fcntl.flock(owner_descriptor, fcntl.LOCK_EX)

    async def exercise():
        invocation = asyncio.create_task(
            runtime.run_agent("task", AgentRole.COPYEDIT, repo / "workspace", {}, session_dir)
        )
        await _wait_for_path(session_dir / "worker-state.json")
        os.close(owner_descriptor)
        probe = os.open(run_lock, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        invocation.cancel()
        with pytest.raises(AgentCancelled):
            await invocation

    try:
        asyncio.run(exercise())
    finally:
        try:
            os.close(owner_descriptor)
        except OSError:
            pass


def test_provider_lock_rejects_symlink_alias(tmp_path):
    repo, _ = _layout(tmp_path, "run_link")
    target = repo / "target.lock"
    target.touch()
    (repo / ".scriptorium" / "locks" / "run_link.providers.lock").symlink_to(target)

    with pytest.raises(InfrastructureError, match="cannot open provider lock"):
        wait_for_provider_cleanup(repo, "run_link", timeout=0.1)


def test_provider_lock_rejects_hardlink_alias_and_path_traversal(tmp_path):
    repo, _ = _layout(tmp_path, "run_link")
    target = repo / "target.lock"
    target.touch()
    os.link(target, repo / ".scriptorium" / "locks" / "run_link.providers.lock")

    with pytest.raises(InfrastructureError, match="unsafe provider lock file"):
        wait_for_provider_cleanup(repo, "run_link", timeout=0.1)
    with pytest.raises(InfrastructureError, match="invalid run ID"):
        wait_for_provider_cleanup(repo, "../outside", timeout=0.1)


def test_process_group_permission_error_is_not_ignored_for_live_descendants(monkeypatch):
    def reject_signal(pid, sig):
        raise PermissionError

    monkeypatch.setattr("scriptorium.runtime.contained.os.killpg", reject_signal)
    monkeypatch.setattr("scriptorium.runtime.contained._process_group_has_live_members", lambda pid: True)

    with pytest.raises(PermissionError):
        asyncio.run(_terminate_process_group(SimpleNamespace(pid=12345)))


def test_watchdog_signal_deadlines_do_not_wait_for_native_config_deletion(tmp_path, monkeypatch):
    native_config = tmp_path / "scriptorium-claude-blocked"
    native_config.mkdir()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    signals = []

    def block_cleanup(path):
        cleanup_started.set()
        release_cleanup.wait(5)

    monkeypatch.setattr("scriptorium.runtime.contained.shutil.rmtree", block_cleanup)
    monkeypatch.setattr("scriptorium.runtime.worker._GRACEFUL_CANCEL_SECONDS", 0)
    monkeypatch.setattr("scriptorium.runtime.worker._TERM_SECONDS", 0)
    monkeypatch.setattr("scriptorium.runtime.worker.os.killpg", lambda pid, sig: signals.append(sig))

    try:
        _cancel_watchdog(threading.Event(), (tmp_path, "claude_code"))
        assert cleanup_started.wait(1)
        assert signals == [signal.SIGTERM, signal.SIGKILL]
    finally:
        release_cleanup.set()


def test_watchdog_escalates_when_native_config_cleanup_cannot_start(tmp_path, monkeypatch):
    signals = []

    def fail_cleanup(*args, **kwargs):
        raise RuntimeError("thread exhaustion")

    monkeypatch.setattr("scriptorium.runtime.worker.cleanup_native_config_dirs", fail_cleanup)
    monkeypatch.setattr("scriptorium.runtime.worker._GRACEFUL_CANCEL_SECONDS", 0)
    monkeypatch.setattr("scriptorium.runtime.worker._TERM_SECONDS", 0)
    monkeypatch.setattr("scriptorium.runtime.worker.os.killpg", lambda pid, sig: signals.append(sig))

    _cancel_watchdog(threading.Event(), (tmp_path, "claude_code"))

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_native_config_cleanup_reports_directory_enumeration_failure(tmp_path, monkeypatch):
    original_glob = Path.glob

    def fail_target_glob(path, pattern):
        if path == tmp_path:
            raise OSError("cannot enumerate")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", fail_target_glob)

    assert cleanup_native_config_dirs(tmp_path, "claude_code") is False


def test_watchdog_kills_a_runtime_and_descendant_that_ignore_cancellation(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_hang")
    runtime = _runtime(repo, "hang_auth", runtime_name="claude_code")

    async def exercise():
        invocation = asyncio.create_task(
            runtime.run_agent("task", AgentRole.COPYEDIT, repo / "workspace", {}, session_dir)
        )
        await _wait_for_path(session_dir / "provider-state.json")
        state = json.loads((session_dir / "provider-state.json").read_text(encoding="utf-8"))
        started = time.monotonic()
        invocation.cancel()
        with pytest.raises(AgentCancelled) as cancelled:
            await invocation
        return cancelled.value.result, state, time.monotonic() - started

    result, state, elapsed = asyncio.run(exercise())

    assert result.status == "interrupted"
    assert elapsed < 15
    _wait_for_process_group_exit(state["pgid"])
    assert list(session_dir.glob("scriptorium-claude-*")) == []
    wait_for_provider_cleanup(repo, "run_hang", timeout=0.1)


def test_returned_interruption_still_reaps_a_leaked_provider_descendant(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_leak")
    runtime = _runtime(repo, "leak")

    async def exercise():
        invocation = asyncio.create_task(
            runtime.run_agent("task", AgentRole.COPYEDIT, repo / "workspace", {}, session_dir)
        )
        await _wait_for_path(session_dir / "provider-state.json")
        state = json.loads((session_dir / "provider-state.json").read_text(encoding="utf-8"))
        invocation.cancel()
        with pytest.raises(AgentCancelled) as cancelled:
            await invocation
        return cancelled.value.result, state

    result, state = asyncio.run(exercise())

    assert result.status == "interrupted"
    _wait_for_process_group_exit(state["pgid"])
    wait_for_provider_cleanup(repo, "run_leak", timeout=0.1)


def test_worker_does_not_accept_an_incomplete_invoke_frame(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_partial")
    route = _runtime(repo, "complete").route
    provider_lock = repo / ".scriptorium" / "locks" / "run_partial.providers.lock"
    provider_descriptor = os.open(provider_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(provider_descriptor, fcntl.LOCK_SH)
    parent_socket, child_socket = socket.socketpair()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scriptorium.runtime.worker",
            str(child_socket.fileno()),
            str(provider_descriptor),
        ],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(child_socket.fileno(), provider_descriptor),
        start_new_session=True,
    )
    try:
        child_socket.close()
        os.close(provider_descriptor)
        provider_descriptor = -1
        reader = parent_socket.makefile("rb")
        ready = json.loads(reader.readline())
        assert ready["type"] == "READY"
        message = {
            "type": "INVOKE",
            "version": 1,
            "operation": "run",
            "thread_id": None,
            "route": asdict(route),
            "task": "task",
            "role": AgentRole.COPYEDIT.value,
            "workspace": str((repo / "workspace").resolve()),
            "schema": {},
            "session_dir": str(session_dir.resolve()),
            "runtime_factory": _FAKE_FACTORY,
        }
        parent_socket.sendall(json.dumps(message).encode("utf-8"))
        parent_socket.shutdown(socket.SHUT_WR)
        process.wait(timeout=5)

        assert process.returncode == 0
        assert not (session_dir / "worker-state.json").exists()
        wait_for_provider_cleanup(repo, "run_partial", timeout=0.1)
    finally:
        reader.close()
        parent_socket.close()
        if provider_descriptor >= 0:
            os.close(provider_descriptor)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_provider_barrier_stays_held_after_result_if_the_parent_disappears(tmp_path):
    repo, session_dir = _layout(tmp_path, "run_post_result")
    route = _runtime(repo, "leak").route
    provider_lock = repo / ".scriptorium" / "locks" / "run_post_result.providers.lock"
    provider_descriptor = os.open(provider_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(provider_descriptor, fcntl.LOCK_SH)
    parent_socket, child_socket = socket.socketpair()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scriptorium.runtime.worker",
            str(child_socket.fileno()),
            str(provider_descriptor),
        ],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(child_socket.fileno(), provider_descriptor),
        start_new_session=True,
    )
    reader = parent_socket.makefile("rb")
    try:
        child_socket.close()
        os.close(provider_descriptor)
        provider_descriptor = -1
        assert json.loads(reader.readline())["type"] == "READY"
        invocation = {
            "type": "INVOKE",
            "version": 1,
            "operation": "run",
            "thread_id": None,
            "route": asdict(route),
            "task": "task",
            "role": AgentRole.COPYEDIT.value,
            "workspace": str((repo / "workspace").resolve()),
            "schema": {},
            "session_dir": str(session_dir.resolve()),
            "runtime_factory": _FAKE_FACTORY,
        }
        parent_socket.sendall((json.dumps(invocation) + "\n").encode("utf-8"))
        _wait_for_path_sync(session_dir / "provider-state.json")
        parent_socket.sendall(b'{"type":"CANCEL","version":1}\n')
        while True:
            message = json.loads(reader.readline())
            if message["type"] == "RESULT":
                break
        state = json.loads((session_dir / "provider-state.json").read_text(encoding="utf-8"))

        reader.close()
        parent_socket.close()
        with pytest.raises(InfrastructureError, match="did not finish within 0.1s"):
            wait_for_provider_cleanup(repo, "run_post_result", timeout=0.1)
        wait_for_provider_cleanup(repo, "run_post_result", timeout=15)

        assert message["result"]["status"] == "interrupted"
        _wait_for_process_group_exit(state["pgid"])
        process.wait(timeout=5)
    finally:
        reader.close()
        parent_socket.close()
        if provider_descriptor >= 0:
            os.close(provider_descriptor)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _runtime(repo: Path, model: str, *, runtime_name: str = "codex") -> ContainedAgentRuntime:
    versions = {"codex": "0.144.4", "claude_code": "0.2.128"}
    return ContainedAgentRuntime(
        RouteConfig(
            name="contained",
            model_provider="openai",
            model=model,
            input_usd_per_million=0,
            output_usd_per_million=0,
            runtime=runtime_name,
            runtime_version=versions[runtime_name],
        ),
        repo,
        _worker_runtime_factory=_FAKE_FACTORY,
    )


def _layout(tmp_path: Path, run_id: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    session_dir = repo / ".scriptorium" / "runs" / run_id / "sessions" / "fake"
    session_dir.mkdir(parents=True)
    (repo / ".scriptorium" / "locks").mkdir(parents=True)
    (repo / "workspace").mkdir()
    return repo, session_dir


async def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        await asyncio.sleep(0.02)


def _wait_for_path_sync(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.02)


def _wait_for_process_group_exit(process_group: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            if not _process_group_has_live_members(process_group):
                return
            raise
        if time.monotonic() >= deadline:
            raise AssertionError(f"process group {process_group} is still alive")
        time.sleep(0.02)
