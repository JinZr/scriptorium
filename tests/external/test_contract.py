from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scriptorium.artifacts import ArtifactStore
from scriptorium.domain import AttemptStatus, RunStatus, TaskStatus
from scriptorium.errors import ConfigurationError, DuplicateRunError, InfrastructureError, StateError
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


def _review_json(summary: str, findings: list | None = None) -> str:
    findings = findings or []
    return json.dumps(
        {
            "summary": summary,
            "findings": findings,
            "limitations": [],
            "claim_checks": [
                {
                    "claim_index": 0,
                    "evidence": [{"source_path": "manuscript.pdf", "page": 1}],
                    "critical_question": "Does the evidence support the result?",
                    "countercheck": "Checked the manuscript and supplement.",
                    "stated_scope": "As stated in the manuscript.",
                    "check_type": "design_and_analysis",
                    "question_answer": "no" if findings else "yes",
                    "exceptions": [],
                    "assessment": "finding" if findings else "supported",
                    "finding_indices": list(range(len(findings))),
                }
            ],
            "claim_inventory": [
                {
                    "claim": "The manuscript reports a result.",
                    "claim_anchor": {"source_path": "manuscript.pdf", "page": 1},
                    "prominence": "headline",
                }
            ],
            "verdict": {
                "recommendation": "minor_revision" if findings else "accept",
                "decisive_questions": ["Does the evidence support the result?"],
            },
        }
    )


@pytest.mark.parametrize("client", ["codex", "claude_code", "antigravity"])
def test_each_host_uses_the_same_frozen_task_contract(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, client, "selected-model", "max", "session-1", "host")
        attempt_id = claim["attempt"].id
        assert claim["input_digest"] == claim["task"].input_digest
        assert claim["schema"]["title"] == "ReviewOutput"
        assert "limitations" in claim["schema"]["required"]
        assert (
            service.claim_task(task_id, client, "selected-model", "max", "session-1", "host")["attempt"].id
            == attempt_id
        )
        with pytest.raises(StateError, match="different session"):
            service.claim_task(task_id, client, "selected-model", "max", "session-2", "host")
        exported = tmp_path / "export"
        service.export_task(attempt_id, exported)
        assert (exported / "sources/supplement.tex").read_text(encoding="utf-8").startswith("The supplement")
        manifest = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
        assert "supplement.tex" in {item["path"] for item in manifest["sources"]}
        assert json.loads((exported / "task/schema.json").read_text(encoding="utf-8")) == claim["schema"]
        assert len(service.page_task(attempt_id, 1)["digest"]) == 64
        result = asyncio.run(
            service.submit_task(
                attempt_id,
                claim["input_digest"],
                _review_json("The result and supplementary explanation were checked."),
            )
        )
        assert result["attempt"].status == AttemptStatus.COMPLETED
        assert result["run_status"] == RunStatus.AWAITING_DECISION
        repeated = asyncio.run(
            service.submit_task(
                attempt_id,
                claim["input_digest"],
                _review_json("The result and supplementary explanation were checked."),
            )
        )
        assert repeated["output_digest"] == result["output_digest"]
        assert {event.event_type for event in service.store.list_events(run_id)} >= {"tool.export", "tool.page"}


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
        assert service.store.get_task(task_id).status == TaskStatus.RUNNING


def test_cancel_rejects_late_submission(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "antigravity", "model", "high", "session", "host")
        service.cancel_run(run_id, "stopped")
        with pytest.raises(StateError, match="no longer active"):
            service.armarius.submit_task(claim["attempt"].id, claim["input_digest"], "{}")


@pytest.mark.parametrize("stale", [False, True])
def test_writing_requires_human_gates_and_independent_verification(tmp_path: Path, stale: bool) -> None:
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
            "consequence": "A reader would misread the reported result.",
            "confidence": 0.9,
        }
        asyncio.run(
            service.submit_task(
                review["attempt"].id,
                review["input_digest"],
                _review_json("Read the result and supplement.", [finding]),
            )
        )
        assert service.store.get_run(run_id).status == RunStatus.AWAITING_DECISION
        finding_id = service.list_findings(run_id)[0].id
        service.decide_finding(finding_id, "confirm", "Accept the scoped wording")
        asyncio.run(service.resume_run(run_id))
        plan = tmp_path / "plan"
        (plan / "assignments").mkdir(parents=True)
        (plan / "plan.md").write_text("# Story\n\nThe result holds in the scope the supplement states.\n")
        (plan / "assignments" / "result.md").write_text(
            f"---\ntarget: main.tex:3-3\nfindings: {finding_id}\n---\nQualify the result.\n", encoding="utf-8"
        )
        assert service.check_plan(run_id, plan)["valid"]
        asyncio.run(service.approve_plan(run_id, plan, "The author agreed to qualify the result."))
        writing_task = next(task for task in service.store.list_tasks(run_id) if task.stage == "writing")
        writing = service.claim_task(writing_task.id, "antigravity", "model", "max", "writing-session", "host")
        export = tmp_path / "writing-export"
        service.export_task(writing["attempt"].id, export)
        passage = export / "task" / "passages" / "1.tex"
        before = passage.read_text(encoding="utf-8")
        # A real LaTeX error in the passage comes back to the writer as a validation issue.
        passage.write_text("A scoped result \\undefinedmacro is described here.\\input{supplement}", encoding="utf-8")
        output = service.pack_writing_output(writing["attempt"].id, export)
        checked = service.check_submission(writing["attempt"].id, writing["input_digest"], output)
        assert [issue["code"] for issue in checked["validation_report"]["issues"]] == ["writing.build_failed"]
        assert "undefinedmacro" in checked["validation_report"]["issues"][0]["actual"]["log_tail"]
        passage.write_text(before.replace("A result", "A scoped result"), encoding="utf-8")
        output = service.pack_writing_output(writing["attempt"].id, export)
        asyncio.run(service.submit_task(writing["attempt"].id, writing["input_digest"], output))
        patch = service.store.list_patches(run_id)[-1]
        assert service.store.get_run(run_id).status == RunStatus.AWAITING_PATCH_APPROVAL
        service.decide_patch(patch.id, "approve", "The edit matches the finding")
        asyncio.run(service.resume_run(run_id))
        verification_task = next(task for task in service.store.list_tasks(run_id) if task.stage == "verification")
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
            assert service.list_tasks(run_id)["next_actions"] == []
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
    exported = command("task", "export", attempt_id, "--dir", str(tmp_path / "export"))
    assert exported["ok"] and exported["data"]["attempt_id"] == attempt_id
    assert (tmp_path / "export/sources/supplement.tex").read_text(encoding="utf-8").startswith("The supplement")
    output = _review_json("Read the supplement.")
    unescaped = output.replace("Read the supplement.", "Read the \\AA supplement.")
    checked = command(
        "task",
        "submit",
        attempt_id,
        "--input-digest",
        claim["input_digest"],
        "--file",
        "-",
        "--check",
        input_text=unescaped,
    )["data"]
    assert checked["valid"] is False and checked["recorded"] is False
    assert checked["validation_report"]["issues"][0]["code"] == "json.invalid"
    checked = command(
        "task",
        "submit",
        attempt_id,
        "--input-digest",
        claim["input_digest"],
        "--file",
        "-",
        "--check",
        input_text=output,
    )["data"]
    assert checked["valid"] is True and checked["validation_report"] is None
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


def test_export_writes_a_long_line_whole(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    text = "result " * 1500 + "counterevidence\n"
    (repo / "supplement.tex").write_text(text, encoding="utf-8")
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
        service.export_task(claim["attempt"].id, tmp_path / "export")
        assert (tmp_path / "export/sources/supplement.tex").read_text(encoding="utf-8") == text


def test_retrieval_checks_requested_files_without_reloading_the_bundle(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        attempt = service.claim_task(task_id, "codex", "model", "max", "session", "host")["attempt"]

    with ScriptoriumService(repo) as service:
        workspace = repo / ".scriptorium" / "runs" / run_id / "bundle"
        touched = []
        digest_file = ArtifactStore.digest_file

        def record_digest(path):
            touched.append(Path(path).relative_to(workspace).as_posix())
            return digest_file(path)

        monkeypatch.setattr(ArtifactStore, "digest_file", staticmethod(record_digest))
        monkeypatch.setattr(service.armarius, "_external_bundle", lambda *args: pytest.fail("full bundle reload"))
        assert service.page_task(attempt.id, 1)["page"] == 1
        assert set(touched) == {"manifest.json", "source-map.json", "navigation.json", "pages/page-0001.png"}
        touched.clear()
        assert service.export_task(attempt.id, tmp_path / "export")["file_count"]
        # Export checks every file it writes and no page image.
        assert set(touched) == {
            "manifest.json",
            "source-map.json",
            "navigation.json",
            "manuscript.pdf",
            "sources/main.tex",
            "sources/supplement.tex",
        }


def test_retrieval_rejects_selected_corruption_and_submission_checks_the_whole_bundle(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        workspace = repo / ".scriptorium" / "runs" / run_id / "bundle"
        (workspace / "sources/main.tex").write_text("damaged\n", encoding="utf-8")
        assert service.page_task(claim["attempt"].id, 1)["page"] == 1
        with pytest.raises(InfrastructureError, match="frozen bundle file digest"):
            service.export_task(claim["attempt"].id, tmp_path / "export")
        assert not (tmp_path / "export").exists()
        with pytest.raises(InfrastructureError, match="bundle source digest"):
            asyncio.run(
                service.submit_task(
                    claim["attempt"].id,
                    claim["input_digest"],
                    _review_json("Review complete."),
                )
            )
        (workspace / "pages/page-0001.png").write_bytes(b"damaged")
        with pytest.raises(InfrastructureError, match="frozen bundle file digest"):
            service.page_task(claim["attempt"].id, 1)


def test_retrieval_rebuilds_a_missing_index_after_full_validation(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    _, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        index = service.artifacts.path_for(claim["bundle_digest"])
        assert index.is_file()
        index.unlink()
        assert service.page_task(claim["attempt"].id, 1)["page"] == 1
        assert index.is_file()


@pytest.mark.parametrize("name", ["manifest.json", "source-map.json", "navigation.json"])
def test_retrieval_rejects_corrupt_frozen_metadata(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        metadata = repo / ".scriptorium" / "runs" / run_id / "bundle" / name
        metadata.write_text("{}\n", encoding="utf-8")
        with pytest.raises(InfrastructureError, match="frozen bundle file digest"):
            service.page_task(claim["attempt"].id, 1)
        with pytest.raises(InfrastructureError, match="frozen bundle file digest"):
            service.export_task(claim["attempt"].id, tmp_path / "export")
        assert not (tmp_path / "export").exists()


def test_retrieval_rejects_corrupt_bundle_index(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    _, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        service.artifacts.path_for(claim["bundle_digest"]).write_bytes(b"corrupt")
        with pytest.raises(InfrastructureError, match="bundle index is corrupt"):
            service.page_task(claim["attempt"].id, 1)


def test_export_preserves_both_sources_with_colliding_bundle_alias(tmp_path: Path) -> None:
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
        exported = tmp_path / "export"
        service.export_task(attempt.id, exported)
        source_map = json.loads((exported / "source-map.json").read_text(encoding="utf-8"))
        read_paths = {item["source_path"]: item["read_path"] for item in source_map["sources"]}
        assert read_paths["supplement.tex"] == "sources/supplement.tex"
        assert read_paths["sources/supplement.tex"] == "sources/sources/supplement.tex"
        assert (exported / "sources/supplement.tex").read_text(encoding="utf-8") == (
            "The supplement explains that result.\n"
        )
        assert (exported / "sources/sources/supplement.tex").read_text(encoding="utf-8") == (
            "Canonical nested source.\n"
        )


def test_frozen_navigation_damage_blocks_claim_before_any_attempt(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        navigation = repo / ".scriptorium" / "runs" / run_id / "bundle" / "navigation.json"
        navigation.write_text("{}\n", encoding="utf-8")
        with pytest.raises(InfrastructureError, match="navigation"):
            service.claim_task(task_id, "codex", "model", "max", "session", "host")
        assert service.store.list_attempts(task_id) == []


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
            "consequence": "A reader would misread the reported result.",
            "confidence": 0.5,
        }
        result = asyncio.run(
            service.submit_task(
                claim["attempt"].id,
                claim["input_digest"],
                _review_json("Examined the manuscript.", [candidate]),
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
            _review_json("Reviewed the manuscript."),
        )
        assert service.store.get_run(run_id).status == RunStatus.REVIEWING
        assert service.list_tasks(run_id)["next_actions"] == [{"command": "run resume", "run_id": run_id}]
    with ScriptoriumService(repo) as service:
        asyncio.run(service.resume_run(run_id))
        assert service.store.get_run(run_id).status == RunStatus.AWAITING_DECISION
        assert len(service.store.list_attempts(task_id)) == 1


def test_corrupt_completed_output_cannot_be_replayed_or_pass_the_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        attempt = service.armarius.submit_task(
            claim["attempt"].id,
            claim["input_digest"],
            _review_json("Reviewed the manuscript."),
        )
        service.artifacts.path_for(attempt.output_artifact_digest).write_text("corrupt", encoding="utf-8")
    with ScriptoriumService(repo) as service:
        with pytest.raises(InfrastructureError, match="unreadable output"):
            asyncio.run(service.resume_run(run_id))
        assert service.list_findings(run_id) == []
        assert not service.evaluate_gate(run_id)["passed"]


def test_json_cli_freezes_a_review_brief_and_detects_a_committed_template(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    main = (repo / "main.tex").read_text(encoding="utf-8")
    (repo / "main.tex").write_text(main.replace("\\begin{document}", "\\usepackage{neurips_2026}\n\\begin{document}"))
    (repo / "neurips_2026.sty").write_text("\\ProvidesPackage{neurips_2026}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "tpl"],
        check=True,
        capture_output=True,
    )
    brief = {"venue_family": "ml_conference", "stage": "presubmission", "priority_claims": ["A result."]}
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(brief), encoding="utf-8")

    def command(*args: str):
        result = subprocess.run(
            [sys.executable, "-m", "scriptorium", "--json", *args], cwd=repo, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)["data"]

    template = {"template": "neurips_2026", "venue_family": "ml_conference"}
    assert command("doctor", "--profile", "full")["detected_template"] == template
    started = command("run", "start", "--profile", "full", "--brief", str(brief_path))
    assert started["detected_template"] == template
    assert started["review_brief"]["venue_family"] == "ml_conference"
    assert started["review_brief"]["priority_claims"] == 1
    run_id = started["run"]["id"]
    status = command("run", "status", run_id)
    assert status["review_brief"] == started["review_brief"]
    claim = command(
        "task",
        "claim",
        status["next_actions"][0]["task_id"],
        "--client",
        "claude_code",
        "--model",
        "selected-model",
        "--effort",
        "high",
        "--session-id",
        "host-session",
        "--session-source",
        "host",
    )
    exported = command("task", "export", claim["attempt"]["id"], "--dir", str(tmp_path / "export"))
    assert {"path": "task/brief.json", "digest": started["review_brief"]["digest"]} in exported["task_inputs"]
    assert json.loads((tmp_path / "export/task/brief.json").read_text(encoding="utf-8")) == {
        **brief,
        "venue": None,
        "known_weaknesses": [],
        "prior_reviews": None,
        "ignore": [],
        "severity_notes": None,
        "recorded_by": None,
    }
    with ScriptoriumService(repo) as service:
        prompt = service.show_task(claim["attempt"]["id"])["prompt"]
    assert "- Venue family: ml_conference" in prompt and "  - A result." in prompt


def test_duplicate_start_on_a_real_project_is_refused_until_the_first_run_ends(tmp_path):
    repo = _repo(tmp_path / "paper")
    run_id, _task_id = _start(repo)
    with ScriptoriumService(repo) as service:
        with pytest.raises(DuplicateRunError, match=run_id):
            asyncio.run(service.start_run("HEAD", "full"))
        service.cancel_run(run_id, "Superseded")
        restarted = asyncio.run(service.start_run("HEAD", "full"))["run"]
        assert restarted.id != run_id


def test_export_writes_the_frozen_bundle_and_task_inputs_and_records_each_export(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    run_id, task_id = _start(repo)
    destination = tmp_path / "exported"
    with ScriptoriumService(repo) as service:
        claim = service.claim_task(task_id, "codex", "model", "max", "session", "host")
        attempt_id = claim["attempt"].id
        response = service.export_task(attempt_id, destination)
        expected = {
            "manifest.json",
            "manuscript.pdf",
            "navigation.json",
            "source-map.json",
            "sources/main.tex",
            "sources/supplement.tex",
        }
        inputs = {"task/prompt.md", "task/schema.json", "task/example.json"}
        assert response["directory"] == str(destination.resolve())
        assert response["file_count"] == len(expected | inputs)
        assert {item["path"] for item in response["task_inputs"]} == inputs
        bundle = Path(claim["bundle_path"])
        on_disk = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
        assert on_disk == expected | inputs
        events = [e for e in service.store.list_events(run_id) if e.event_type == "tool.export"]
        assert len(events) == 1 and events[0].entity_id == attempt_id
        for item in events[0].payload["files"]:
            exported = destination / item["path"]
            if item["path"] in expected:
                assert exported.read_bytes() == (bundle / item["path"]).read_bytes()
            assert ArtifactStore.digest_file(exported) == item["digest"]
            assert exported.stat().st_mode & 0o777 == 0o444
        assert (destination / "sources/main.tex").read_bytes() == (repo / "main.tex").read_bytes()
        # A used directory is never overwritten, and a refused export records nothing.
        with pytest.raises(ConfigurationError, match="empty directory"):
            service.export_task(attempt_id, destination)
        empty = tmp_path / "empty"
        empty.mkdir()
        service.export_task(attempt_id, empty)
        assert len([e for e in service.store.list_events(run_id) if e.event_type == "tool.export"]) == 2
        receipt = asyncio.run(service.submit_task(attempt_id, claim["input_digest"], _review_json("Checked.")))
        assert receipt["attempt"].status.value == "completed"
        assert service.export_task(attempt_id, tmp_path / "after")["files_digest"] == response["files_digest"]


def test_json_cli_export_command(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "paper")
    _, task_id = _start(repo)
    destination = tmp_path / "cli-export"

    def command(*args: str):
        result = subprocess.run(
            [sys.executable, "-m", "scriptorium", "--json", *args], cwd=repo, capture_output=True, text=True
        )
        return result.returncode, json.loads(result.stdout)

    claim_args = ["task", "claim", task_id, "--client", "codex", "--model", "m", "--effort", "max"]
    claim_args += ["--session-id", "s", "--session-source", "host"]
    attempt_id = command(*claim_args)[1]["data"]["attempt"]["id"]
    code, exported = command("task", "export", attempt_id, "--dir", str(destination))
    assert code == 0 and exported["data"]["file_count"] == 9
    assert (destination / "manuscript.pdf").stat().st_mode & 0o777 == 0o444
    code, refused = command("task", "export", attempt_id, "--dir", str(destination))
    assert code == 2 and refused["error"]["code"] == "configuration_error"
