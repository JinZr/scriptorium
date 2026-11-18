"""Child-process actions for the storage concurrency and crash tests: ``python -m tests.storage._driver``."""

from pathlib import Path
import sys
import time

from scriptorium import storage
from scriptorium.artifacts import ArtifactStore
from scriptorium.domain import Event, Run
from scriptorium.storage import ConflictError, RunStore


def _wait_for(signal: Path) -> None:
    deadline = time.monotonic() + 20
    while not signal.exists():
        if time.monotonic() > deadline:
            raise SystemExit(f"start signal never appeared: {signal}")
        time.sleep(0.001)


def _append_events(store: RunStore, run_id: str, tag: str, count: str) -> None:
    for index in range(int(count)):
        store.append_event(Event(run_id, "test.concurrent", "run", run_id, {"tag": tag, "index": index}))


def _decide(store: RunStore, decision: str, *finding_ids: str) -> None:
    for finding_id in finding_ids:
        store.decide_finding(finding_id, decision, f"decided by {decision}")


def _create(store: RunStore, run_id: str) -> None:
    try:
        store.create_run(Run("/tmp/paper", "a" * 40, "b" * 40, "full", "c" * 64, {}, id=run_id))
    except ConflictError:
        print("conflict")
    else:
        print("created")


def _publish(store: RunStore, text: str) -> None:
    artifact = ArtifactStore(store.root / "artifacts").put_text(text, "text/plain; charset=utf-8")
    print(store.record_artifact(artifact).digest)


def _stall_before_replace(store: RunStore, run_id: str, ready: Path) -> None:
    def stall(source, target):
        ready.write_text(f"{source}\n", encoding="utf-8")
        time.sleep(60)

    storage.os.replace = stall
    store.cancel_run(run_id, "stop", "cancel-killed")


ACTIONS = {
    "events": _append_events,
    "decide": _decide,
    "create": _create,
    "publish": _publish,
}


def main(argv: list[str]) -> int:
    action, root, signal, *arguments = argv
    store = RunStore(Path(root))
    if action == "stall":
        _stall_before_replace(store, arguments[0], Path(signal))
        return 1
    _wait_for(Path(signal))
    ACTIONS[action](store, *arguments)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
