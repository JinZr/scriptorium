from collections import Counter
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from scriptorium.domain import AgentRole, Finding, FindingSeverity, RunStatus
from scriptorium.storage import Database

from ._factories import create_completed_attempt, make_run

ROOT = Path(__file__).parents[2]


def _spawn(*arguments) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "tests.storage._driver", *map(str, arguments)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_together(tmp_path: Path, commands: list[tuple]) -> list[str]:
    go = tmp_path / "go"
    processes = [_spawn(command[0], tmp_path / "state", go, *command[1:]) for command in commands]
    time.sleep(0.3)
    go.touch()
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())
    return outputs


def _finding(run, task, attempt, index: int) -> Finding:
    return Finding(
        run_id=run.id,
        task_id=task.id,
        attempt_id=attempt.id,
        fingerprint=f"{index:064x}",
        role=AgentRole.COPYEDIT,
        category="language",
        severity=FindingSeverity.MINOR,
        title=f"Finding {index}",
        claim="A word is misspelled.",
        evidence=({"source_path": "main.tex", "start_line": 1, "end_line": 1},),
        explanation="The word is misspelled.",
        suggested_action="Fix the spelling.",
        confidence=0.5,
        consequence="A reader stumbles.",
    )


def test_processes_mutating_one_run_lose_no_update(tmp_path) -> None:
    with Database(tmp_path / "state") as database:
        run = database.create_run(make_run())
        task, attempt = create_completed_attempt(database, run)
        findings = [database.create_finding(_finding(run, task, attempt, index)) for index in range(12)]

    _run_together(
        tmp_path,
        [
            ("events", run.id, "first", 40),
            ("events", run.id, "second", 40),
            ("decide", "confirm", *(finding.id for finding in findings[:6])),
            ("decide", "reject", *(finding.id for finding in findings[6:])),
        ],
    )

    with Database(tmp_path / "state") as database:
        events = database.list_events(run.id)
        tags = Counter(event.payload["tag"] for event in events if event.event_type == "test.concurrent")
        assert tags == {"first": 40, "second": 40}
        for tag in ("first", "second"):
            indices = sorted(e.payload["index"] for e in events if e.payload.get("tag") == tag)
            assert indices == list(range(40))
        statuses = {finding.id: database.get_finding(finding.id).status.value for finding in findings}
        assert statuses == {
            **{finding.id: "confirmed" for finding in findings[:6]},
            **{finding.id: "rejected" for finding in findings[6:]},
        }
        assert all(len(database.list_decisions("finding", finding.id)) == 1 for finding in findings)
        assert len([event for event in events if event.event_type == "finding.decided"]) == 12


def test_processes_creating_runs_create_each_run_once(tmp_path) -> None:
    outputs = _run_together(
        tmp_path,
        [("create", "run_a"), ("create", "run_b"), ("create", "run_same"), ("create", "run_same"), ("create", "run_c")],
    )

    assert sorted(outputs) == ["conflict", "created", "created", "created", "created"]
    with Database(tmp_path / "state") as database:
        assert sorted(run.id for run in database.list_runs()) == ["run_a", "run_b", "run_c", "run_same"]
        assert [event.event_type for event in database.list_events("run_same")] == ["run.created"]


def test_processes_publishing_one_artifact_keep_one_record(tmp_path) -> None:
    outputs = _run_together(tmp_path, [("publish", "shared evidence")] * 5)

    assert len(set(outputs)) == 1
    digest = outputs[0]
    with Database(tmp_path / "state") as database:
        record = database.get_artifact(digest)
    assert record.size == len("shared evidence")
    shard = tmp_path / "state" / "artifacts" / "sha256" / digest[:2]
    assert sorted(path.name for path in shard.iterdir()) == [digest[2:], f"{digest[2:]}.json"]


def test_a_process_killed_before_the_replace_leaves_the_previous_state(tmp_path) -> None:
    with Database(tmp_path / "state") as database:
        run = database.create_run(make_run())
        database.update_run(run.id, RunStatus.REVIEWING)
    state_file = tmp_path / "state" / "runs" / run.id / "state.json"
    before = state_file.read_bytes()
    ready = tmp_path / "ready"

    process = _spawn("stall", tmp_path / "state", ready, run.id)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail(f"driver exited early: {process.stderr.read()}")
            if time.monotonic() > deadline:
                pytest.fail("driver never reached the replace")
            time.sleep(0.01)
        pending = Path(ready.read_text(encoding="utf-8").strip())
        assert json.loads(pending.read_bytes())["run"]["status"] == "cancelled"
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert state_file.read_bytes() == before
    with Database(tmp_path / "state") as database:
        assert database.get_run(run.id).status == RunStatus.REVIEWING
        assert [run.id for run in database.list_runs()] == [run.id]
        assert database.cancel_run(run.id, "stop", "cancel-after-kill").status == RunStatus.CANCELLED
        assert [event.payload for event in database.list_events(run.id) if event.event_type == "run.cancelled"] == [
            {"reason": "stop", "request_id": "cancel-after-kill"}
        ]
