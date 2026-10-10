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


def claim_inventory(claim_checks):
    return [
        {
            "claim": check["claim"],
            "claim_anchor": check.get("claim_anchor", check["evidence"][0]),
            "prominence": "headline",
            "check_indices": [index],
        }
        for index, check in enumerate(claim_checks)
    ]


def links_claims(schema):
    return "verdict" in schema["properties"]


def link_claims(output, *, prior_high=False):
    """Rewrite a claim-restating substantive output into the claim_index shape, adding a consistent verdict.

    prior_high says whether the task's earlier accepted outputs already hold a major or blocker finding.
    """
    checks = output.get("claim_checks", [])
    if any("claim" in check for check in checks):
        inventory = output.get("claim_inventory") or claim_inventory(checks)
        indices = {}
        for entry_index, entry in enumerate(inventory):
            indices.update({check_index: entry_index for check_index in entry.get("check_indices", [])})
        checks = [
            {
                **{key: value for key, value in check.items() if key not in {"claim", "claim_anchor"}},
                "claim_index": indices.get(check_index, check_index),
            }
            for check_index, check in enumerate(checks)
        ]
        inventory = [{key: value for key, value in entry.items() if key != "check_indices"} for entry in inventory]
        output = {**output, "claim_checks": checks, "claim_inventory": inventory}
    if "verdict" not in output:
        high = prior_high or any(
            finding.get("severity") in {"blocker", "major"} for finding in output.get("findings", [])
        )
        output = {
            **output,
            "verdict": {
                "recommendation": "major_revision" if high else "minor_revision",
                "decisive_questions": ["Does the reported result support the conclusion?"],
            },
        }
    return output


def submit(service, claim_data, output):
    if claim_data["task"].stage == "review" and "scope" not in output:
        output = {
            **output,
            "scope": {"completion": "complete", "checked": [], "outstanding": [], "limitations": []},
        }
    if "claim_checks" in claim_data["schema"]["required"] and "claim_checks" not in output:
        findings = output.get("findings", [])
        output = {
            **output,
            "claim_checks": [
                {
                    "claim": "The reported result is clear.",
                    "evidence": [{"source_path": "manuscript.pdf", "page": 1}],
                    "critical_question": "Does the reported result support the conclusion?",
                    "countercheck": "Checked the frozen manuscript page.",
                    "claim_anchor": {"source_path": "manuscript.pdf", "page": 1},
                    "stated_scope": "As stated in the manuscript.",
                    "check_type": "design_and_analysis",
                    "question_answer": "no" if findings else "yes",
                    "exceptions": [],
                    "assessment": "finding" if findings else "supported",
                    "finding_indices": list(range(len(findings))),
                }
            ],
        }
    if links_claims(claim_data["schema"]):
        prior_high = any(
            finding.task_id == claim_data["task"].id and finding.severity.value in {"blocker", "major"}
            for finding in service.database.list_findings(claim_data["task"].run_id)
        )
        output = link_claims(output, prior_high=prior_high)
    elif "claim_inventory" in claim_data["schema"]["required"] and "claim_inventory" not in output:
        output = {**output, "claim_inventory": claim_inventory(output["claim_checks"])}
    return asyncio.run(
        service.submit_task(
            claim_data["attempt"].id,
            claim_data["input_digest"],
            json.dumps(output),
        )
    )


def finding_definition(schema):
    reference = schema["properties"]["findings"]["items"]["$ref"].rpartition("/")[2]
    return schema["$defs"][reference]


def requires_consequence(schema):
    return "consequence" in finding_definition(schema)["required"]


def review_finding(claim_data):
    source = next(item for item in claim_data["source_map"]["sources"] if item["source_path"] == "main.tex")
    finding = {
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
    if requires_consequence(claim_data["schema"]):
        finding["consequence"] = "A reader cannot tell what the main result sentence states."
    if "affected_claim" in finding_definition(claim_data["schema"])["properties"]:
        finding["affected_claim"] = "The main result sentence states that the result is clear."
    return finding


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
