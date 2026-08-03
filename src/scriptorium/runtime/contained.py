from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, replace
import errno
import fcntl
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any

from ..config import RouteConfig
from ..domain import AgentRole
from ..errors import InfrastructureError
from .base import (
    RUNTIME_SDK_VERSIONS,
    AgentCancelled,
    AgentResult,
    AgentRuntime,
    AgentUsage,
    RuntimeUnavailable,
    SessionStartedCallback,
)

_PROTOCOL_VERSION = 1
_WORKER_READY_SECONDS = 5.0
_WORKER_CANCEL_SECONDS = 13.0
_WORKER_EXIT_SECONDS = 2.0
_NATIVE_CONFIG_CLEANUP_SECONDS = 0.5


class ProviderCleanupTimeout(InfrastructureError):
    pass


class _SessionCallbackFailed(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error


class ContainedAgentRuntime:
    def __init__(
        self,
        route: RouteConfig,
        repo: Path,
        *,
        _worker_runtime_factory: str | None = None,
    ) -> None:
        expected_version = RUNTIME_SDK_VERSIONS.get(route.runtime)
        if expected_version is None:
            raise RuntimeUnavailable(f"Unsupported runtime: {route.runtime}")
        if route.runtime_version != expected_version:
            raise RuntimeUnavailable(
                f"Frozen route {route.name!r} requires {route.runtime}=={route.runtime_version}, "
                f"but this Scriptorium build supports {expected_version}."
            )
        self.route = route
        self.repo = repo.resolve()
        self._worker_runtime_factory = _worker_runtime_factory

    async def run_agent(
        self,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None = None,
    ) -> AgentResult:
        return await self._invoke(
            thread_id=None,
            task=task,
            role=role,
            workspace=workspace,
            schema=schema,
            session_dir=session_dir,
            on_session_started=on_session_started,
        )

    async def resume_agent(
        self,
        thread_id: str,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None = None,
    ) -> AgentResult:
        return await self._invoke(
            thread_id=thread_id,
            task=task,
            role=role,
            workspace=workspace,
            schema=schema,
            session_dir=session_dir,
            on_session_started=on_session_started,
        )

    async def _invoke(
        self,
        *,
        thread_id: str | None,
        task: str,
        role: AgentRole,
        workspace: Path,
        schema: Mapping[str, object],
        session_dir: Path,
        on_session_started: SessionStartedCallback | None,
    ) -> AgentResult:
        run_id = _run_id_from_session_dir(self.repo, session_dir)
        if not cleanup_native_config_dirs(session_dir, self.route.runtime):
            raise InfrastructureError("Claude native config cleanup remains pending")
        provider_descriptor = _open_provider_lock(self.repo, run_id)
        parent_socket, child_socket = socket.socketpair()
        process: subprocess.Popen[bytes] | None = None
        channel: _AsyncJsonChannel | None = None
        result: AgentResult | None = None
        ready_received = False
        terminal_received = False
        try:
            fcntl.flock(provider_descriptor, fcntl.LOCK_SH)
            # close_fds plus this narrow allowlist prevents the worker from inheriting the run mutation lock.
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scriptorium.runtime.worker",
                    str(child_socket.fileno()),
                    str(provider_descriptor),
                ],
                cwd=Path(__file__).resolve().parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(child_socket.fileno(), provider_descriptor),
                # The dedicated session is the containment boundary for every provider descendant.
                start_new_session=True,
            )
            child_socket.close()
            os.close(provider_descriptor)
            provider_descriptor = -1
            parent_socket.setblocking(False)
            channel = _AsyncJsonChannel(parent_socket)
            ready = await asyncio.wait_for(channel.receive(), _WORKER_READY_SECONDS)
            if ready.get("type") != "READY" or ready.get("version") != _PROTOCOL_VERSION:
                raise InfrastructureError("runtime worker did not complete its protocol handshake")
            ready_received = True
            await channel.send(
                {
                    "type": "INVOKE",
                    "version": _PROTOCOL_VERSION,
                    "operation": "resume" if thread_id is not None else "run",
                    "thread_id": thread_id,
                    "route": asdict(self.route),
                    "task": task,
                    "role": role.value,
                    "workspace": str(workspace.resolve()),
                    "schema": dict(schema),
                    "session_dir": str(session_dir.resolve()),
                    "runtime_factory": self._worker_runtime_factory,
                }
            )
            try:
                result = await self._receive_result(channel, on_session_started)
            except asyncio.CancelledError:
                try:
                    result = await self._cancel_and_collect(channel, process, on_session_started)
                except asyncio.CancelledError:
                    raise
                except Exception as cleanup_error:
                    result = _with_cleanup_error(
                        _interrupted_result(self.route, "runtime worker was cancelled"),
                        cleanup_error,
                    )
                terminal_received = True
                try:
                    await _reap_worker(process)
                except Exception as cleanup_error:
                    result = _with_cleanup_error(result, cleanup_error)
                if result.status != "interrupted":
                    result = replace(
                        result,
                        status="interrupted",
                        final_response=None,
                        error=result.error or "Agent runtime was cancelled.",
                    )
                raise AgentCancelled(result)
            except _SessionCallbackFailed as callback_failure:
                callback_error = callback_failure.error
                # A parent-side session persistence failure still gets native cleanup before it is re-raised.
                try:
                    await self._cancel_and_collect(channel, process, None)
                    await _reap_worker(process)
                except asyncio.CancelledError:
                    raise
                except Exception as cleanup_error:
                    try:
                        await _terminate_process_group(process)
                    except Exception as termination_error:
                        terminal_received = True
                        raise callback_error from termination_error
                    terminal_received = True
                    raise callback_error from cleanup_error
                terminal_received = True
                raise callback_error
            terminal_received = True
            await _reap_worker(process)
            return result
        except AgentCancelled:
            raise
        except asyncio.CancelledError:
            if process is None:
                raise
            interrupted = result or _interrupted_result(self.route, "runtime worker was cancelled")
            try:
                if terminal_received:
                    await _reap_worker(process)
                elif ready_received and channel is not None:
                    interrupted = await self._cancel_and_collect(channel, process, on_session_started)
                    terminal_received = True
                    await _reap_worker(process)
                else:
                    # Before READY no SDK can run; EOF plus group termination still leaves no orphan worker.
                    parent_socket.close()
                    await _terminate_process_group(process)
            except asyncio.CancelledError:
                raise
            except Exception as cleanup_error:
                interrupted = _with_cleanup_error(interrupted, cleanup_error)
            if interrupted.status != "interrupted":
                interrupted = replace(
                    interrupted,
                    status="interrupted",
                    final_response=None,
                    error=interrupted.error or "Agent runtime was cancelled.",
                )
            raise AgentCancelled(interrupted)
        except Exception:
            if process is not None and not terminal_received:
                parent_socket.close()
                await _terminate_process_group(process)
            raise
        finally:
            parent_socket.close()
            child_socket.close()
            if provider_descriptor >= 0:
                os.close(provider_descriptor)
            cleanup_native_config_dirs(session_dir, self.route.runtime)

    async def _receive_result(
        self,
        channel: _AsyncJsonChannel,
        on_session_started: SessionStartedCallback | None,
    ) -> AgentResult:
        while True:
            try:
                message = await channel.receive()
            except ConnectionError as exc:
                raise InfrastructureError("runtime worker exited without returning a result") from exc
            message_type = message.get("type")
            if message_type == "SESSION_STARTED":
                thread_id = message.get("thread_id")
                if not isinstance(thread_id, str):
                    raise InfrastructureError("runtime worker returned an invalid session identifier")
                if on_session_started is not None:
                    try:
                        pending = on_session_started(thread_id)
                        if inspect.isawaitable(pending):
                            await pending
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise _SessionCallbackFailed(exc) from exc
                continue
            if message_type == "RESULT":
                return _result_from_dict(message.get("result"))
            if message_type == "ERROR":
                raise InfrastructureError(f"runtime worker failed: {message.get('error', 'unknown error')}")
            raise InfrastructureError("runtime worker returned an unexpected protocol message")

    async def _cancel_and_collect(
        self,
        channel: _AsyncJsonChannel,
        process: subprocess.Popen[bytes],
        on_session_started: SessionStartedCallback | None,
    ) -> AgentResult:
        try:
            await channel.send({"type": "CANCEL", "version": _PROTOCOL_VERSION})
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        try:
            return await asyncio.wait_for(
                self._receive_result(channel, on_session_started),
                _WORKER_CANCEL_SECONDS,
            )
        except (asyncio.TimeoutError, ConnectionError, InfrastructureError) as exc:
            await _terminate_process_group(process)
            return _interrupted_result(self.route, f"runtime worker cancellation cleanup failed: {exc}")


class _AsyncJsonChannel:
    def __init__(self, transport: socket.socket) -> None:
        self.transport = transport
        self.buffer = bytearray()

    async def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        await asyncio.get_running_loop().sock_sendall(self.transport, data)

    async def receive(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        while b"\n" not in self.buffer:
            data = await loop.sock_recv(self.transport, 65536)
            if not data:
                raise ConnectionError("runtime worker control channel closed")
            self.buffer.extend(data)
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InfrastructureError("runtime worker returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise InfrastructureError("runtime worker returned a non-object protocol message")
        return message


def wait_for_provider_cleanup(repo: Path, run_id: str, timeout: float = 15.0) -> None:
    descriptor = _open_provider_lock(repo.resolve(), run_id)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                # This is only a cleanup barrier; the separate run lock remains mutation authority.
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProviderCleanupTimeout(
                        f"provider cleanup for run {run_id} did not finish within {timeout:g}s"
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    finally:
        os.close(descriptor)


def native_runtime_for_route(route: RouteConfig) -> AgentRuntime:
    if route.runtime == "codex":
        from .codex import CodexAgentRuntime

        runtime_type = CodexAgentRuntime
    elif route.runtime == "claude_code":
        from .claude_code import ClaudeCodeAgentRuntime

        runtime_type = ClaudeCodeAgentRuntime
    elif route.runtime == "antigravity":
        from .antigravity import AntigravityAgentRuntime

        runtime_type = AntigravityAgentRuntime
    else:
        raise RuntimeUnavailable(f"Unsupported runtime: {route.runtime}")
    return runtime_type(
        route=route.name,
        model=route.model,
        provider=route.model_provider,
        reasoning=route.reasoning_effort,
    )


def cleanup_native_config_dirs(
    session_dir: Path,
    runtime: str,
    *,
    wait_seconds: float = _NATIVE_CONFIG_CLEANUP_SECONDS,
) -> bool:
    if runtime != "claude_code":
        return True
    errors: list[OSError] = []

    def remove() -> None:
        try:
            for path in session_dir.glob("scriptorium-claude-*"):
                if path.is_symlink() or not path.is_dir():
                    continue
                shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(exc)

    cleanup = threading.Thread(target=remove, name="scriptorium-native-config-cleanup", daemon=True)
    cleanup.start()
    cleanup.join(wait_seconds)
    return not cleanup.is_alive() and not errors


def load_worker_runtime_factory(reference: str | None):
    if reference is None:
        return native_runtime_for_route
    module_name, separator, attribute = reference.partition(":")
    if not separator:
        raise RuntimeError("worker runtime factory must use module:attribute syntax")
    module_path = Path(module_name)
    if module_path.is_absolute():
        spec = spec_from_file_location("_scriptorium_worker_test_runtime", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load worker runtime factory from {module_path}")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = import_module(module_name)
    return getattr(module, attribute)


def result_to_dict(result: AgentResult) -> dict[str, Any]:
    value = asdict(result)
    return value


def _result_from_dict(value: Any) -> AgentResult:
    if not isinstance(value, dict) or not isinstance(value.get("usage"), dict):
        raise InfrastructureError("runtime worker returned an invalid result")
    return AgentResult(
        thread_id=value.get("thread_id"),
        status=value["status"],
        final_response=value.get("final_response"),
        usage=AgentUsage(**value["usage"]),
        trace_jsonl=value["trace_jsonl"],
        runtime_name=value["runtime_name"],
        runtime_version=value["runtime_version"],
        model=value["model"],
        model_provider=value["model_provider"],
        duration_ms=value.get("duration_ms"),
        error=value.get("error"),
    )


def _run_id_from_session_dir(repo: Path, session_dir: Path) -> str:
    sessions_root = (repo / ".scriptorium" / "runs").resolve()
    try:
        relative = session_dir.resolve().relative_to(sessions_root)
    except ValueError as exc:
        raise InfrastructureError("runtime session directory is outside the Scriptorium run store") from exc
    # The run ID is intentionally derived from Armarius' frozen runs/<id>/sessions/<identity> layout.
    if len(relative.parts) != 3 or relative.parts[1] != "sessions":
        raise InfrastructureError("runtime session directory does not match the frozen run layout")
    return relative.parts[0]


def _open_provider_lock(repo: Path, run_id: str) -> int:
    locks = repo / ".scriptorium" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    if Path(run_id).name != run_id or run_id in {"", ".", ".."}:
        raise InfrastructureError(f"invalid run ID for provider lock: {run_id}")
    directory_descriptor = -1
    descriptor = -1
    try:
        directory_descriptor = os.open(locks, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(
            f"{run_id}.providers.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise InfrastructureError(f"unsafe provider lock file: {locks / f'{run_id}.providers.lock'}")
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise InfrastructureError(f"cannot open provider lock for run {run_id}: {exc}") from exc
    except InfrastructureError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


async def _wait_for_process(process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def _reap_worker(process: subprocess.Popen[bytes]) -> None:
    # RESULT means SDK cleanup is complete; the live leader still holds the barrier while its whole group is reaped.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
    await _wait_for_process(process, _WORKER_EXIT_SECONDS)
    if process.poll() is None:
        raise InfrastructureError("runtime worker did not exit after process-group cleanup")


async def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    # Signal only the live group represented by this Popen; persisted owner metadata is never a kill authority.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except PermissionError:
        if not _process_group_has_live_members(process.pid):
            process.poll()
            return
        raise
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
        return
    # Do not reap the leader before SIGKILL; its unreaped PID keeps this invocation's PGID from being reused.
    await asyncio.sleep(2.0)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except PermissionError:
        if not _process_group_has_live_members(process.pid):
            process.poll()
            return
        raise
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
    await _wait_for_process(process, 1.0)


def _process_group_has_live_members(process_group: int) -> bool:
    # EPERM is safe only when the current invocation's group contains zombies and no executable provider process.
    if sys.platform == "darwin":
        return _darwin_process_group_has_live_members(process_group)
    try:
        observed = subprocess.run(
            ["ps", "-o", "pgid=,stat=", "-ax"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return True
    if observed.returncode != 0:
        return True
    for line in observed.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[0] == str(process_group) and not fields[1].startswith("Z"):
            return True
    return False


def _darwin_process_group_has_live_members(process_group: int) -> bool:
    import ctypes

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        libproc.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_listpids.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        needed = libproc.proc_listpids(2, process_group, None, 0)
        if needed <= 0:
            return False
        pids = (ctypes.c_int * max(1, needed // ctypes.sizeof(ctypes.c_int)))()
        used = libproc.proc_listpids(2, process_group, pids, ctypes.sizeof(pids))
        for pid in pids[: max(0, used // ctypes.sizeof(ctypes.c_int))]:
            info = ctypes.create_string_buffer(136)
            if libproc.proc_pidinfo(pid, 3, 0, info, len(info)) > 0:
                status = int.from_bytes(info.raw[4:8], sys.byteorder)
                if status != 5:
                    return True
        return False
    except (AttributeError, OSError):
        return True


def _interrupted_result(route: RouteConfig, error: str) -> AgentResult:
    return AgentResult(
        thread_id=None,
        status="interrupted",
        final_response=None,
        usage=AgentUsage(),
        trace_jsonl=json.dumps({"event": "runtime.containment_interrupted", "error": error}) + "\n",
        runtime_name=route.runtime,
        runtime_version=route.runtime_version or "",
        model=route.model,
        model_provider=route.model_provider,
        duration_ms=None,
        error=error,
    )


def _with_cleanup_error(result: AgentResult, cleanup_error: Exception) -> AgentResult:
    detail = f"runtime containment cleanup failed: {cleanup_error}"
    trace = (
        result.trace_jsonl
        + json.dumps({"event": "runtime.containment_cleanup_failed", "error": str(cleanup_error)})
        + "\n"
    )
    return replace(
        result,
        status="interrupted",
        final_response=None,
        trace_jsonl=trace,
        error=f"{result.error}; {detail}" if result.error else detail,
    )
