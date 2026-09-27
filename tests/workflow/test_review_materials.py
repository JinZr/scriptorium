import asyncio
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import fitz
import pytest

from scriptorium.domain import AgentRole, RunStatus
from scriptorium.errors import ConfigurationError, InfrastructureError
from scriptorium.manuscript import BuildResult, CompilerInput, ManuscriptManager
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES

from ._support import claim, make_repository, review_finding, submit

SUPPLEMENT = "\\documentclass{article}\n\\begin{document}\nThe result is limited to adults.\n\\end{document}\n"


def supplemented_repository(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    (repo / "supplement.tex").write_text(SUPPLEMENT)
    config = repo / "scriptorium.toml"
    config.write_text(
        config.read_text().replace('engine = "pdflatex"', 'engine = "pdflatex"\nsupplements = ["supplement.tex"]')
    )
    subprocess.run(["git", "-C", str(repo), "add", "scriptorium.toml", "supplement.tex"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Declare supplement"], check=True)
    return repo


class DocumentBuilder(ManuscriptManager):
    def __init__(self, repo):
        super().__init__(repo)
        self.built = []
        self.fail_at = None

    def build(self, workspace, manuscript):
        self.built.append(manuscript.main)
        if len(self.built) == self.fail_at:
            raise InfrastructureError("supplement build failed")
        pdf = workspace / Path(manuscript.main).with_suffix(".pdf")
        with fitz.open() as document:
            document.new_page().insert_text((40, 40), (workspace / manuscript.main).read_text())
            document.save(pdf)
        compiler_input = CompilerInput(
            manuscript.main, sha256((workspace / manuscript.main).read_bytes()).hexdigest(), "review"
        )
        return BuildResult(pdf, "built", (compiler_input,))


def test_supplement_retrieval_survives_processes_and_records_unambiguous_pages(tmp_path):
    repo = supplemented_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=DocumentBuilder(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        attempt_id = review["attempt"].id
        (repo / "supplement.tex").write_text("uncommitted replacement")
        matches = service.search_task(attempt_id, "limited to adults", None, 0, 20)
        assert matches["matches"][0]["path"] == "supplement.tex"
        read = service.read_task(attempt_id, "sources/supplement.tex", 3, 1, 0, 6000)
        assert read["lines"][0]["text"] == "The result is limited to adults."
        main = service.page_task(attempt_id, 1, "main.tex")
        assert main["page"] == 1 and main["document_page"] == 1
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scriptorium",
            "--json",
            "task",
            "page",
            attempt_id,
            "--document",
            "supplement.tex",
            "--number",
            "1",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(result.stdout.encode()) <= MAX_TOOL_RESPONSE_BYTES
    page = json.loads(result.stdout)["data"]
    assert page["page"] == 2 and page["document_page"] == 1
    assert page["document"] == "supplement.tex" and page["source_path"] == "manuscript.pdf"
    assert page["digest"] != main["digest"]
    with ScriptoriumService(repo) as service:
        assert service.page_task(attempt_id, 2) == page
        finding = review_finding(review)
        finding["evidence"] = [{"source_path": "manuscript.pdf", "page": page["page"]}]
        source = next(item for item in review["source_map"]["sources"] if item["source_path"] == "supplement.tex")
        finding["evidence"].append(
            {
                "source_path": "supplement.tex",
                "source_digest": source["source_digest"],
                "start_line": 3,
                "end_line": 3,
                "quoted_text": "The result is limited to adults.",
            }
        )
        result = submit(
            service,
            review,
            {
                "summary": "Compared the frozen supplement.",
                "findings": [finding],
                "scope": {
                    "completion": "complete",
                    "checked": [{"source_path": "manuscript.pdf", "page": 2}],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        assert result["run_status"] == RunStatus.AWAITING_DECISION
        events = [item.payload for item in service.database.list_events(run.id) if item.event_type == "tool.page"]
        assert events[-1]["page"] == 2 and events[-1]["document"] == "supplement.tex"
        assert events[-1]["document_page"] == 1
        assert len(service.list_findings(run.id)) == 1
        assert not service.evaluate_gate(run.id)["passed"]


@pytest.mark.parametrize(
    "document,number", [("../supplement.tex", 1), ("missing.tex", 1), ("supplement.tex", 0), ("supplement.tex", 2)]
)
def test_invalid_document_page_has_no_successful_access_event(tmp_path, document, number):
    repo = supplemented_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=DocumentBuilder(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        with pytest.raises(ConfigurationError):
            service.page_task(review["attempt"].id, number, document)
        assert not any(item.event_type == "tool.page" for item in service.database.list_events(run.id))


@pytest.mark.parametrize("artifact", ["sources/supplement.tex", "pages/page-0002.png", "source-map.json"])
def test_corrupt_supplement_blocks_submission_without_partial_findings(tmp_path, artifact):
    repo = supplemented_repository(tmp_path)
    with ScriptoriumService(repo, manuscript_manager=DocumentBuilder(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        (Path(review["bundle_path"]) / artifact).write_text("corrupt")
        with pytest.raises(InfrastructureError):
            if artifact == "sources/supplement.tex":
                service.read_task(review["attempt"].id, "supplement.tex", 1, 40, 0, 6000)
            else:
                service.page_task(review["attempt"].id, 1, "supplement.tex")
        with pytest.raises(InfrastructureError):
            submit(service, review, {"summary": "Reviewed.", "findings": [review_finding(review)]})
        assert service.list_findings(run.id) == []
        assert not service.evaluate_gate(run.id)["passed"]


def test_supplement_failure_does_not_publish_review_tasks_and_can_resume(tmp_path):
    repo = supplemented_repository(tmp_path)
    manager = DocumentBuilder(repo)
    manager.fail_at = 2
    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        with pytest.raises(InfrastructureError, match="supplement.tex"):
            asyncio.run(service.start_run("HEAD", "quick"))
        run = service.database.list_runs()[0]
        assert run.status == RunStatus.FAILED
        assert service.database.list_tasks(run.id) == []
        manager.fail_at = None
        assert asyncio.run(service.resume_run(run.id))["run"].status == RunStatus.REVIEWING


@pytest.mark.parametrize("stale", [False, True])
def test_verification_rebuilds_supplement_and_keeps_human_and_stale_worktree_gates(tmp_path, stale):
    repo = supplemented_repository(tmp_path)
    manager = DocumentBuilder(repo)
    with ScriptoriumService(repo, manuscript_manager=manager) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        source = next(item for item in review["source_map"]["sources"] if item["source_path"] == "supplement.tex")
        finding = review_finding(review)
        finding["evidence"] = [
            {
                "source_path": "supplement.tex",
                "source_digest": source["source_digest"],
                "start_line": 3,
                "end_line": 3,
                "quoted_text": "The result is limited to adults.",
            }
        ]
        submit(service, review, {"summary": "Clarify the supplement.", "findings": [finding]})
        finding_id = service.list_findings(run.id)[0].id
        service.decide_finding(finding_id, "confirm", "Test authorization to clarify.")
        asyncio.run(service.resume_run(run.id))
        revision = claim(service, run.id, AgentRole.REVISION, session="revision")
        submit(
            service,
            revision,
            {
                "summary": "Clarified the supplement.",
                "edits": [
                    {
                        "finding_ids": [finding_id],
                        "path": "supplement.tex",
                        "source_digest": source["source_digest"],
                        "start_line": 3,
                        "end_line": 3,
                        "before": "The result is limited to adults.",
                        "after": "The result is limited to adults aged 18 years or older.",
                        "rationale": "Clarify eligibility.",
                    }
                ],
            },
        )
        patch = service.database.list_patches(run.id)[-1]
        assert service.database.get_run(run.id).status == RunStatus.AWAITING_PATCH_APPROVAL
        assert manager.built == ["main.tex", "supplement.tex"] * 2
        service.decide_patch(patch.id, "approve", "Test authorization to verify.")
        manager.fail_at = 6
        with pytest.raises(InfrastructureError, match="supplement.tex"):
            asyncio.run(service.resume_run(run.id))
        assert not service.evaluate_gate(run.id)["passed"]
        assert not any(item.stage == "verification" for item in service.database.list_tasks(run.id))
        manager.fail_at = None
        asyncio.run(service.resume_run(run.id))
        verifier = claim(service, run.id, AgentRole.VERIFICATION, session="independent-verification")
        assert len(verifier["source_map"]["compiled_pdf"]["documents"]) == 2
        assert service.page_task(verifier["attempt"].id, 1, "supplement.tex")["page"] == 2
        submit(
            service,
            verifier,
            {
                "verdict": "pass",
                "summary": "Verified all documents.",
                "resolved_finding_ids": [finding_id],
                "issues": [],
            },
        )
        assert not service.evaluate_gate(run.id)["passed"]
        if stale:
            (repo / "supplement.tex").write_text("stale worktree")
            assert service.apply_patch(patch.id).status.value == "stale"
            assert (repo / "supplement.tex").read_text() == "stale worktree"
            assert not service.evaluate_gate(run.id)["passed"]
            return
        service.apply_patch(patch.id)
        assert service.evaluate_gate(run.id)["passed"]
        assert "adults aged 18 years or older" in (repo / "supplement.tex").read_text()
