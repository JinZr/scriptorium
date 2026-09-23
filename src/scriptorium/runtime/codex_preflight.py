from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from .base import CODEX_SDK_VERSION, RuntimeUnavailable

_TIMEOUT_SECONDS = 30
_SUCCESS = {"ok": True, "sdk_version": CODEX_SDK_VERSION}


def codex_startup_preflight() -> str:
    with tempfile.TemporaryDirectory(prefix="scriptorium-codex-preflight-") as temporary:
        root = Path(temporary)
        (root / "manifest.json").write_text("{}", encoding="utf-8")
        env = {
            "HOME": temporary,
            "CODEX_HOME": temporary,
            "XDG_CONFIG_HOME": temporary,
            "XDG_CACHE_HOME": temporary,
            "XDG_DATA_HOME": temporary,
            "TMPDIR": temporary,
            "PATH": os.defpath,
        }
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "scriptorium.runtime.codex_preflight"],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeUnavailable("Cannot launch the isolated Codex startup probe") from exc
        try:
            output, _ = process.communicate(timeout=_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeUnavailable("Codex startup/configuration probe exceeded 30 seconds") from exc
        finally:
            # The SDK has unbounded RPC waits; kill the whole group even on interruption.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        try:
            result = json.loads(output)
        except ValueError:
            result = None
        if process.returncode != 0 or result != _SUCCESS:
            raise RuntimeUnavailable(
                "Codex startup/configuration probe failed; verify the pinned SDK and its bundled binary. "
                "Native output is suppressed; authentication and model access were not tested."
            )
    return (
        f"openai-codex {CODEX_SDK_VERSION}: isolated startup and synthetic configuration read passed; "
        "user configuration, authentication, model access, retrieval, and session recovery are unverified"
    )


if __name__ == "__main__":
    try:
        from .codex import probe_native_configuration

        probe_native_configuration()
        print(json.dumps(_SUCCESS))
    except Exception:
        sys.exit(1)
