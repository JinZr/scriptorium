from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
from typing import Any

from ..config import RouteConfig
from ..domain import AgentRole
from .base import AgentCancelled
from .contained import cleanup_native_config_dirs, load_worker_runtime_factory, result_to_dict

_PROTOCOL_VERSION = 1
_GRACEFUL_CANCEL_SECONDS = 10.0
_TERM_SECONDS = 2.0


class _WorkerChannel:
    def __init__(self, transport: socket.socket) -> None:
        self.transport = transport
        self.reader = transport.makefile("rb")
        self.write_lock = threading.Lock()

    def receive(self) -> dict[str, Any] | None:
        line = self.reader.readline()
        if not line or not line.endswith(b"\n"):
            return None
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("worker protocol message must be a JSON object")
        return message

    def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            self.transport.sendall(data)

    def close(self) -> None:
        try:
            self.transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.reader.close()
        self.transport.close()


def _watch_control(
    channel: _WorkerChannel,
    loop: asyncio.AbstractEventLoop,
    invocation: asyncio.Task[Any],
    finished: threading.Event,
    cancel_requested: threading.Event,
    native_config: tuple[Path, str],
) -> None:
    while not finished.is_set():
        try:
            message = channel.receive()
        except (OSError, ValueError, json.JSONDecodeError):
            message = None
        if message is not None and message.get("type") != "CANCEL":
            continue
        if finished.is_set():
            return
        cancel_requested.set()
        loop.call_soon_threadsafe(invocation.cancel)
        threading.Thread(
            target=_cancel_watchdog,
            args=(finished, native_config),
            name="scriptorium-runtime-watchdog",
            daemon=True,
        ).start()
        return


def _cancel_watchdog(finished: threading.Event, native_config: tuple[Path, str]) -> None:
    # This thread cannot depend on the asyncio loop that a wedged native SDK may be blocking.
    if finished.wait(_GRACEFUL_CANCEL_SECONDS):
        return
    # Best-effort auth cleanup must never delay or prevent process-group escalation.
    try:
        cleanup_native_config_dirs(*native_config, wait_seconds=0)
    except Exception:
        pass
    try:
        os.killpg(os.getpid(), signal.SIGTERM)
    except ProcessLookupError:
        return
    if finished.wait(_TERM_SECONDS):
        return
    try:
        cleanup_native_config_dirs(*native_config, wait_seconds=0)
    except Exception:
        pass
    try:
        os.killpg(os.getpid(), signal.SIGKILL)
    except ProcessLookupError:
        return


async def _invoke(message: dict[str, Any], channel: _WorkerChannel):
    route = RouteConfig(**message["route"])
    factory = load_worker_runtime_factory(message.get("runtime_factory"))
    runtime = factory(route)

    def session_started(thread_id: str) -> None:
        channel.send(
            {
                "type": "SESSION_STARTED",
                "version": _PROTOCOL_VERSION,
                "thread_id": thread_id,
            }
        )

    arguments = (
        message["task"],
        AgentRole(message["role"]),
        Path(message["workspace"]),
        message["schema"],
        Path(message["session_dir"]),
    )
    if message["operation"] == "resume":
        return await runtime.resume_agent(
            message["thread_id"],
            *arguments,
            on_session_started=session_started,
        )
    return await runtime.run_agent(*arguments, on_session_started=session_started)


async def _run(
    channel: _WorkerChannel,
    invocation_message: dict[str, Any],
    finished: threading.Event,
) -> int:
    cancel_requested = threading.Event()
    invocation = asyncio.create_task(_invoke(invocation_message, channel))
    watcher = threading.Thread(
        target=_watch_control,
        args=(
            channel,
            asyncio.get_running_loop(),
            invocation,
            finished,
            cancel_requested,
            (Path(invocation_message["session_dir"]), invocation_message["route"]["runtime"]),
        ),
        name="scriptorium-runtime-control",
        daemon=True,
    )
    watcher.start()
    try:
        try:
            result = await invocation
        except AgentCancelled as exc:
            result = exc.result
        except asyncio.CancelledError:
            raise RuntimeError("runtime cancelled without a normalized interrupted result")
        if cancel_requested.is_set() and result.status != "interrupted":
            result = replace(
                result,
                status="interrupted",
                final_response=None,
                error=result.error or "Agent runtime was cancelled.",
            )
        channel.send({"type": "RESULT", "version": _PROTOCOL_VERSION, "result": result_to_dict(result)})
        await asyncio.Event().wait()
    except Exception as exc:
        try:
            channel.send({"type": "ERROR", "version": _PROTOCOL_VERSION, "error": str(exc)})
        except OSError:
            pass
        await asyncio.Event().wait()
    return 1


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    control_descriptor = int(sys.argv[1])
    provider_descriptor = int(sys.argv[2])
    os.set_inheritable(control_descriptor, False)
    os.set_inheritable(provider_descriptor, False)
    transport = socket.socket(fileno=control_descriptor)
    channel = _WorkerChannel(transport)
    finished = threading.Event()
    # Executed provider children reset this caught handler; it only keeps the watchdog alive for SIGKILL escalation.
    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    try:
        channel.send({"type": "READY", "version": _PROTOCOL_VERSION})
        invocation = channel.receive()
        if invocation is None:
            return 0
        if invocation.get("type") != "INVOKE" or invocation.get("version") != _PROTOCOL_VERSION:
            channel.send({"type": "ERROR", "version": _PROTOCOL_VERSION, "error": "invalid invocation"})
            return 2
        return asyncio.run(_run(channel, invocation, finished))
    finally:
        # Keep the watchdog armed through asyncio executor shutdown, not merely until RESULT is sent.
        finished.set()
        channel.close()
        os.close(provider_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
