from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scriptorium.domain import AttemptStatus, RunStatus, TaskStatus
from scriptorium.errors import ConfigurationError, InfrastructureError, StateError
from scriptorium.service import ScriptoriumService

pytestmark = pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("pdflatex") is None,
    reason="LaTeX runtime is required for external task integration",
)


def _repo(root: Path) -> Path:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "A result is described here.\\input{supplement}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (root / "supplement.tex").write_text("The supplement explains that result.\n", encoding="utf-8")
    (root / "scriptorium.toml").write_text(
        '[manuscript]\nmain = "main.tex"\nengine = "pdflatex"\n' '[profiles.full]\nroles = ["substantive_review"]\n',
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".scriptorium/\n", encoding="utf-8")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    return root


def _start(repo: Path):
    with ScriptoriumService(repo) as service:
        view = asyncio.run(service.start_run("HEAD", "full"))
        return view["run"].id, view["tasks"][0]["task"].id


@pytest.mark.parametrize("client", ["codex", "claude_code", "antigravity"])
def test_each_host_uses_the_same_frozen_task_contract(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, client, "selected-model", "max", "session-1", "host")
        attempt_id = claim["attempt"].id
        assert claim["input_digest"] == claim["task"].input_digest
        assert claim["schema"]["title"] == "ReviewOutput"
        assert (
            service.claim_task(task_id, client, "selected-model", "max", "session-1", "host")["attempt"].id
            == attempt_id
        )
        with pytest.raises(StateError, match="different session"):
            service.claim_task(task_id, client, "selected-model", "max", "session-2", "host")
        supplement = service.search_task(attempt_id, "explains", None, 0, 1)
        assert supplement["matches"][0]["path"] == "supplement.tex"
        assert service.read_task(attempt_id, "supplement.tex", 1, 1, 0, 15)["next_offset"] == 15
        assert service.read_task(attempt_id, "sources/supplement.tex", 1, 1, 0, 8000)["lines"][0]["text"].startswith(
            "The supplement"
        )
        assert service.read_task(attempt_id, "manifest.json", 1, 1, 0, 8000)["lines"]
        assert service.search_task(attempt_id, "supplement.tex", "manifest.json", 0, 1)["matches"]
        assert len(service.page_task(attempt_id, 1)["digest"]) == 64
        result = asyncio.run(
            service.submit_task(
                attempt_id,
                claim["input_digest"],
                json.dumps({"summary": "The result and supplementary explanation were checked.", "findings": []}),
            )
        )
        assert result["attempt"].status == AttemptStatus.COMPLETED
        assert result["run_status"] == RunStatus.AWAITING_DECISION
        repeated = asyncio.run(
            service.submit_task(
                attempt_id,
                claim["input_digest"],
                json.dumps({"summary": "The result and supplementary explanation were checked.", "findings": []}),
            )
        )
        assert repeated["output_digest"] == result["output_digest"]
        assert {event.event_type for event in service.database.list_events(run_id)} >= {
            "tool.search",
            "tool.read",
            "tool.page",
        }


def test_invalid_submission_requires_explicit_retry_and_never_creates_findings(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        first = service.claim_task(task_id, "codex", "model", "high", "first", "declared")
        attempt_id = first["attempt"].id
        rejected = asyncio.run(service.submit_task(attempt_id, first["input_digest"], "{}"))
        assert rejected["attempt"].status == AttemptStatus.FAILED
        assert rejected["validation_report"]
        assert service.list_findings(run_id) == []
        with pytest.raises(StateError, match="retry explicitly"):
            service.claim_task(task_id, "codex", "model", "high", "first", "declared")
        asyncio.run(service.retry_task(run_id, task_id))
        second = service.claim_task(task_id, "codex", "model", "high", "second", "declared")
        assert "Your previous ReviewOutput was rejected" in second["prompt"]
        with pytest.raises(StateError, match="input digest"):
            service.armarius.submit_task(second["attempt"].id, "0" * 64, "{}")
        assert service.database.get_task(task_id).status == TaskStatus.RUNNING


def test_cancel_rejects_late_submission(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "antigravity", "model", "high", "session", "host")
        service.cancel_run(run_id, "stopped")
        with pytest.raises(StateError, match="no longer active"):
            service.armarius.submit_task(claim["attempt"].id, claim["input_digest"], "{}")


@pytest.mark.parametrize("stale", [False, True])
def test_revision_requires_human_gates_and_independent_verification(tmp_path: Path, stale: bool) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, review_task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        review = service.claim_task(review_task_id, "codex", "model", "max", "review-session", "host")
        source = next(item for item in review["source_map"]["sources"] if item["source_path"] == "main.tex")
        finding = {
            "category": "claim",
            "severity": "moderate",
            "title": "Unqualified result",
            "claim": "The result lacks a qualifier.",
            "evidence": [
                {
                    "source_path": "main.tex",
                    "start_line": 3,
                    "end_line": 3,
                    "source_digest": source["source_digest"],
                    "quoted_text": "A result",
                }
            ],
            "explanation": "The main text should state the scope.",
            "suggested_action": "Qualify the result.",
            "confidence": 0.9,
        }
        asyncio.run(
            service.submit_task(
                review["attempt"].id,
                review["input_digest"],
                json.dumps({"summary": "Read the result and supplement.", "findings": [finding]}),
            )
        )
        assert service.database.get_run(run_id).status == RunStatus.AWAITING_DECISION
        finding_id = service.list_findings(run_id)[0].id
        service.decide_finding(finding_id, "confirm", "Accept the scoped wording")
        asyncio.run(service.resume_run(run_id))
        revision_task = next(task for task in service.database.list_tasks(run_id) if task.stage == "revision")
        revision = service.claim_task(revision_task.id, "claude_code", "sonnet", "max", "revision-session", "host")
        before = (repo / "main.tex").read_text(encoding="utf-8").splitlines()[2]
        edit = {
            "finding_ids": [finding_id],
            "path": "main.tex",
            "source_digest": source["source_digest"],
            "start_line": 3,
            "end_line": 3,
            "before": before,
            "after": before.replace("A result", "A scoped result"),
            "rationale": "Qualify the claim.",
        }
        asyncio.run(
            service.submit_task(
                revision["attempt"].id,
                revision["input_digest"],
                json.dumps({"summary": "Qualified the result.", "edits": [edit]}),
            )
        )
        patch = service.database.list_patches(run_id)[-1]
        assert service.database.get_run(run_id).status == RunStatus.AWAITING_PATCH_APPROVAL
        service.decide_patch(patch.id, "approve", "The edit matches the finding")
        asyncio.run(service.resume_run(run_id))
        verification_task = next(task for task in service.database.list_tasks(run_id) if task.stage == "verification")
        reused = service.claim_task(verification_task.id, "codex", "model", "max", "review-session", "host")
        output = json.dumps(
            {
                "verdict": "pass",
                "summary": "The qualifier is present.",
                "resolved_finding_ids": [finding_id],
                "issues": [],
            }
        )
        invalid = asyncio.run(service.submit_task(reused["attempt"].id, reused["input_digest"], output))
        assert invalid["attempt"].status == AttemptStatus.FAILED
        assert invalid["validation_report"]["issues"][0]["code"] == "verification.session_unconfirmed"
        asyncio.run(service.retry_task(run_id, verification_task.id))
        verifier = service.claim_task(verification_task.id, "codex", "model", "max", "new-session", "host")
        accepted = asyncio.run(service.submit_task(verifier["attempt"].id, verifier["input_digest"], output))
        assert accepted["run_status"] == RunStatus.READY_TO_APPLY
        assert not service.evaluate_gate(run_id)["passed"]
        if stale:
            (repo / "main.tex").write_text("changed after approval\n", encoding="utf-8")
            assert service.apply_patch(patch.id).status.value == "stale"
            assert not service.evaluate_gate(run_id)["passed"]
        else:
            service.apply_patch(patch.id)
            assert service.evaluate_gate(run_id)["passed"]
            assert "A scoped result" in (repo / "main.tex").read_text(encoding="utf-8")


def test_json_cli_reads_and_submits_from_stdin_across_processes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)

    def command(*args: str, input_text: str | None = None, success: bool = True):
        result = subprocess.run(
            [sys.executable, "-m", "scriptorium", "--json", *args],
            cwd=repo,
            input=input_text,
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) == success
        return json.loads(result.stdout)

    claim_args = (
        "task",
        "claim",
        task_id,
        "--client",
        "codex",
        "--model",
        "selected-model",
        "--effort",
        "max",
        "--session-id",
        "host-session",
        "--session-source",
        "host",
    )
    claim = command(*claim_args)["data"]
    attempt_id = claim["attempt"]["id"]
    assert command(*claim_args)["data"]["attempt"]["id"] == attempt_id
    conflict_args = list(claim_args)
    conflict_args[-3] = "other-session"
    assert command(*conflict_args, success=False)["error"]["code"] == "invalid_state"
    searched = command("task", "search", attempt_id, "--query", "explains")
    assert searched["ok"] and searched["data"]["matches"][0]["path"] == "supplement.tex"
    output = json.dumps({"summary": "Read the supplement.", "findings": []})
    submitted = command(
        "task",
        "submit",
        attempt_id,
        "--input-digest",
        claim["input_digest"],
        "--file",
        "-",
        input_text=output,
    )
    assert submitted["data"]["attempt"]["status"] == "completed"
    assert submitted["data"]["attempt"]["estimated_cost_usd"] is None
    assert (
        command(
            "task", "submit", attempt_id, "--input-digest", claim["input_digest"], "--file", "-", input_text=output
        )["data"]["output_digest"]
        == submitted["data"]["output_digest"]
    )
    assert (
        command(
            "task",
            "submit",
            attempt_id,
            "--input-digest",
            claim["input_digest"],
            "--file",
            "-",
            input_text='{"summary":"different","findings":[]}',
            success=False,
        )["error"]["code"]
        == "invalid_state"
    )
    assert command("task", "list", run_id)["data"]["run_status"] == "awaiting_decision"


def test_search_paging_long_line_and_bundle_path_boundary(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    (repo / "supplement.tex").write_text("result " * 1500 + "counterevidence\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "supplement.tex"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-m",
            "long line",
        ],
        check=True,
        capture_output=True,
    )
    _, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        attempt_id = claim["attempt"].id
        first = service.search_task(attempt_id, "result", "supplement.tex", 0, 1)
        second = service.search_task(attempt_id, "result", "supplement.tex", first["next_cursor"], 1)
        assert first["matches"][0]["column"] == 1
        assert second["matches"][0]["column"] == 8
        read = service.read_task(attempt_id, "supplement.tex", 1, 1, 0, 8000)
        assert read["next_line"] == 1 and read["next_offset"] == 8000
        continued = service.read_task(attempt_id, "supplement.tex", read["next_line"], 1, read["next_offset"], 8000)
        assert "counterevidence" in continued["lines"][0]["text"]
        accesses = [
            event.payload
            for event in service.database.list_events(claim["task"].run_id)
            if event.event_type == "tool.read"
        ]
        for response, access in zip((read, continued), accesses):
            assert access["path"] == response["path"]
            assert access["source_digest"] == response["source_digest"]
            assert access["ranges"] == [
                {
                    "line": item["line"],
                    "start_offset": item["offset"],
                    "end_offset": item["offset"] + len(item["text"]),
                }
                for item in response["lines"]
            ]
            assert (access["next_line"], access["next_offset"]) == (
                response["next_line"],
                response["next_offset"],
            )
        assert accesses[0]["ranges"] != accesses[1]["ranges"]
        with pytest.raises(ConfigurationError, match="not a text source"):
            service.read_task(attempt_id, "../scriptorium.toml", 1, 1, 0, 8000)


def test_read_prefers_canonical_source_path_over_colliding_bundle_alias(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    nested = repo / "sources" / "supplement.tex"
    nested.parent.mkdir()
    nested.write_text("Canonical nested source.\n", encoding="utf-8")
    main = repo / "main.tex"
    main.write_text(
        main.read_text(encoding="utf-8").replace("\\end{document}", "\\input{sources/supplement}\\end{document}"),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "main.tex", "sources/supplement.tex"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "nested"],
        check=True,
        capture_output=True,
    )
    _, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        attempt = service.claim_task(task_id, "codex", "model", "max", "session", "host")["attempt"]
        assert service.read_task(attempt.id, "sources/supplement.tex", 1, 1, 0, 8000)["lines"][0]["text"] == (
            "Canonical nested source."
        )


def test_frozen_navigation_damage_blocks_claim_before_any_attempt(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        navigation = repo / ".scriptorium" / "runs" / run_id / "bundle" / "navigation.json"
        navigation.write_text("{}\n", encoding="utf-8")
        with pytest.raises(InfrastructureError, match="navigation"):
            service.claim_task(task_id, "codex", "model", "max", "session", "host")
        assert service.database.list_attempts(task_id) == []


def test_wrong_evidence_is_rejected_as_a_whole_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "claude_code", "sonnet", "max", "session", "host")
        source = claim["source_map"]["sources"][0]
        candidate = {
            "category": "claim",
            "severity": "moderate",
            "title": "Concern",
            "claim": "This claim needs review.",
            "evidence": [
                {
                    "source_path": source["source_path"],
                    "start_line": 1,
                    "end_line": 1,
                    "source_digest": source["source_digest"],
                    "quoted_text": "not in the source",
                }
            ],
            "explanation": "Explanation.",
            "suggested_action": "Recheck.",
            "confidence": 0.5,
        }
        result = asyncio.run(
            service.submit_task(
                claim["attempt"].id,
                claim["input_digest"],
                json.dumps({"summary": "Examined the manuscript.", "findings": [candidate]}),
            )
        )
        assert result["attempt"].status == AttemptStatus.FAILED
        assert result["validation_report"]
        assert service.list_findings(run_id) == []


def test_completed_attempt_replays_after_process_exit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "antigravity", "model", "high", "session", "host")
        service.armarius.submit_task(
            claim["attempt"].id,
            claim["input_digest"],
            json.dumps({"summary": "Reviewed the manuscript.", "findings": []}),
        )
        assert service.database.get_run(run_id).status == RunStatus.REVIEWING
    with ScriptoriumService(repo) as service:
        asyncio.run(service.resume_run(run_id))
        assert service.database.get_run(run_id).status == RunStatus.AWAITING_DECISION
        assert len(service.database.list_attempts(task_id)) == 1


def test_corrupt_completed_output_cannot_be_replayed_or_pass_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        attempt = service.armarius.submit_task(
            claim["attempt"].id,
            claim["input_digest"],
            json.dumps({"summary": "Reviewed the manuscript.", "findings": []}),
        )
        service.artifacts.path_for(attempt.output_artifact_digest).write_text("corrupt", encoding="utf-8")
    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError, match="unreadable output"):
            asyncio.run(service.resume_run(run_id))
        assert service.list_findings(run_id) == []
        assert not service.evaluate_gate(run_id)["passed"]


def test_historical_sdk_run_stays_readable_but_cannot_resume(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, _ = _start(repo)
    with ScriptoriumService(repo) as service:
        row = service.database.connection.execute(
            "SELECT frozen_config_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        config = json.loads(row["frozen_config_json"])
        config.pop("execution")
        with service.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET frozen_config_json = ? WHERE id = ?",
                (json.dumps(config), run_id),
            )
    with ScriptoriumService(repo) as service:
        assert service.get_run(run_id)["run"].id == run_id
        assert service.render_report(run_id, "json")["run"]["id"] == run_id
        assert not service.evaluate_gate(run_id)["passed"]
        with pytest.raises(StateError, match="retired SDK execution contract"):
            asyncio.run(service.resume_run(run_id))
