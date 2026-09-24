import asyncio
import json
from pathlib import Path
import subprocess

import fitz

from scriptorium.domain import AgentRole
from scriptorium.manuscript import BuildResult, ManuscriptManager

MANUSCRIPT = "\\documentclass{article}\n\\begin{document}\nThe result is teh clear.\n\\end{document}\n"


class PdfBuildingManuscriptManager(ManuscriptManager):
    def build(self, workspace, manuscript):
        pdf_path = workspace / Path(manuscript.main).with_suffix(".pdf")
        pdf_path.unlink(missing_ok=True)
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), "The result is clear.")
            document.save(pdf_path)
        finally:
            document.close()
        return BuildResult(pdf_path=pdf_path, log="fake LaTeX build succeeded")


def make_repository(tmp_path, roles=("substantive_review", "copyedit")):
    repo = tmp_path / "paper"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Scriptorium Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "scriptorium@example.test"], check=True)
    (repo / "main.tex").write_text(MANUSCRIPT, encoding="utf-8")
    role_list = ", ".join(json.dumps(role) for role in roles)
    (repo / "scriptorium.toml").write_text(
        '[manuscript]\nmain = "main.tex"\nengine = "pdflatex"\n\n' f"[profiles.quick]\nroles = [{role_list}]\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(".scriptorium/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "Initial manuscript"], check=True)
    return repo


def claim(service, run_id, role, *, session=None, source="host"):
    role = AgentRole(role)
    task = next(task for task in service.database.list_tasks(run_id) if task.role == role)
    return service.claim_task(task.id, "codex", "test-model", "max", session or f"session-{role.value}", source)


def submit(service, claim_data, output):
    if claim_data["task"].stage == "review" and "scope" not in output:
        output = {
            **output,
            "scope": {"completion": "complete", "checked": [], "outstanding": [], "limitations": []},
        }
    return asyncio.run(
        service.submit_task(
            claim_data["attempt"].id,
            claim_data["input_digest"],
            json.dumps(output),
        )
    )


def review_finding(claim_data):
    source = next(item for item in claim_data["source_map"]["sources"] if item["source_path"] == "main.tex")
    return {
        "category": "clarity",
        "severity": "major",
        "title": "Typo obscures the claim",
        "claim": "The main result sentence contains a typo.",
        "evidence": [
            {
                "source_path": "main.tex",
                "start_line": 3,
                "end_line": 3,
                "source_digest": source["source_digest"],
                "quoted_text": "The result is teh clear.",
            }
        ],
        "explanation": "The typo makes the result sentence harder to read.",
        "suggested_action": "Replace the sentence with the corrected wording.",
        "confidence": 0.99,
    }


def complete_reviews(service, run_id, *, with_finding=True):
    for task in service.database.list_tasks(run_id):
        if task.stage != "review" or task.status.value == "completed":
            continue
        review = claim(service, run_id, task.role)
        findings = [review_finding(review)] if with_finding and task.role == AgentRole.SUBSTANTIVE_REVIEW else []
        receipt = submit(service, review, {"summary": "Reviewed the frozen manuscript.", "findings": findings})
        assert receipt["attempt"].status.value == "completed"


def prepare_patch(service, run_id, *, after="The result is clear."):
    complete_reviews(service, run_id)
    finding_id = service.list_findings(run_id)[0].id
    service.decide_finding(finding_id, "confirm", "Correct the typo.")
    asyncio.run(service.resume_run(run_id))
    revision = claim(service, run_id, AgentRole.REVISION, session="revision-session")
    source = next(item for item in revision["source_map"]["sources"] if item["source_path"] == "main.tex")
    edit = {
        "finding_ids": [finding_id],
        "path": "main.tex",
        "source_digest": source["source_digest"],
        "start_line": 3,
        "end_line": 3,
        "before": "The result is teh clear.",
        "after": after,
        "rationale": "Fix the typo.",
    }
    receipt = submit(service, revision, {"summary": "Corrected the sentence.", "edits": [edit]})
    assert receipt["attempt"].status.value == "completed"
    return service.database.list_patches(run_id)[-1], finding_id


def prepare_verification(service, run_id):
    patch, finding_id = prepare_patch(service, run_id)
    service.decide_patch(patch.id, "approve", "Verify the exact edit.")
    asyncio.run(service.resume_run(run_id))
    return patch, finding_id
