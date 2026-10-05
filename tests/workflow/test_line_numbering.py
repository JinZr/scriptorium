import asyncio
import json
import shutil
import subprocess

import pytest

from scriptorium import workflow
from scriptorium.domain import AgentRole
from scriptorium.errors import InfrastructureError
from scriptorium.schemas import DEFAULT_EVIDENCE_ANCHOR_CONTRACT
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit

FORM_FEED_SOURCE = "\\documentclass{article}\n\\begin{document}\nalpha\fbeta\ngamma delta\n\\end{document}\n"
LEGACY_CONTRACT = DEFAULT_EVIDENCE_ANCHOR_CONTRACT.model_copy(update={"line_terminators": None})


def _start(tmp_path, monkeypatch, contract=None):
    repo = make_repository(tmp_path, roles=("copyedit",))
    (repo / "main.tex").write_text(FORM_FEED_SOURCE, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "Form feed"], check=True)
    service = ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo))
    if contract is not None:
        # Freeze the run as an earlier version did; the frozen contract then governs every later stage.
        monkeypatch.setattr(workflow, "DEFAULT_EVIDENCE_ANCHOR_CONTRACT", contract)
    run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    monkeypatch.undo()
    return service, run


def _finding(source, line, quote):
    return {
        "category": "clarity",
        "severity": "minor",
        "title": "Quoted line",
        "claim": "The quoted line is unclear.",
        "evidence": [
            {
                "source_path": "main.tex",
                "start_line": line,
                "end_line": line,
                "source_digest": source["source_digest"],
                "quoted_text": quote,
            }
        ],
        "explanation": "The text is hard to read.",
        "suggested_action": "Clarify it.",
        "confidence": 0.5,
    }


def _revise_verify_apply(service, run, source, line, before, after):
    findings = service.list_findings(run.id)
    finding = next(item for item in findings if item.evidence[0]["start_line"] == line)
    for other in findings:
        if other.id != finding.id:
            service.decide_finding(other.id, "reject", "Out of scope for this edit.")
    service.decide_finding(finding.id, "confirm", "Capitalize it.")
    asyncio.run(service.resume_run(run.id))
    revision = claim(service, run.id, AgentRole.REVISION, session="revision-session")
    edit = {
        "finding_ids": [finding.id],
        "path": "main.tex",
        "source_digest": source["source_digest"],
        "start_line": line,
        "end_line": line,
        "before": before,
        "after": after,
        "rationale": "Capitalize the quoted word.",
    }
    receipt = submit(service, revision, {"summary": "Capitalized.", "edits": [edit]})
    assert receipt["validation_report"] is None
    patch = service.database.list_patches(run.id)[-1]
    diff = service.artifacts.get_bytes(patch.diff_digest).decode("utf-8")
    service.decide_patch(patch.id, "approve", "Apply the exact edit.")
    asyncio.run(service.resume_run(run.id))
    verification = claim(service, run.id, AgentRole.VERIFICATION, session="verification-session")
    receipt = submit(
        service,
        verification,
        {"verdict": "pass", "summary": "Resolved.", "resolved_finding_ids": [finding.id], "issues": []},
    )
    assert receipt["validation_report"] is None
    assert receipt["run_status"].value == "ready_to_apply"
    # Applying recomputes the diff from the frozen snapshot and compares it with the immutable artifact.
    service.apply_patch(patch.id)
    return diff, (service.repo / "main.tex").read_text(encoding="utf-8")


def test_read_tool_line_numbers_produce_accepted_evidence(tmp_path, monkeypatch):
    service, run = _start(tmp_path, monkeypatch)
    with service:
        context = claim(service, run.id, "copyedit")
        source = next(item for item in context["source_map"]["sources"] if item["source_path"] == "main.tex")
        read = service.read_task(context["attempt"].id, "main.tex", 1, 100, 0, 8000)
        lines = {piece["line"]: piece["text"] for piece in read["lines"]}
        assert lines[3] == "alpha\fbeta"
        assert source["line_count"] == max(lines) == 5
        receipt = submit(
            service,
            context,
            {"summary": "Checked.", "findings": [_finding(source, 3, lines[3]), _finding(source, 4, lines[4])]},
        )
        assert receipt["validation_report"] is None
        assert receipt["attempt"].status.value == "completed"
        diff, applied = _revise_verify_apply(service, run, source, 3, "alpha\fbeta", "alpha\fBETA")
        # Each hunk line starts on its own physical line; the form feed stays inside the changed line.
        assert "-alpha\fbeta\n+alpha\fBETA\n" in diff
        assert all(line[:1] in {" ", "-", "+", "@"} for line in diff.split("\n")[2:] if line)
        assert applied == FORM_FEED_SOURCE.replace("beta", "BETA")


def test_bundle_count_must_match_its_frozen_contract(tmp_path, monkeypatch):
    service, run = _start(tmp_path, monkeypatch)
    with service:
        contract = service.armarius.require_evidence_anchor_contract(run.id)
        workspace = tmp_path / "bundle"
        shutil.copytree(service.repo / ".scriptorium/runs" / run.id / "bundle", workspace)
        assert Armarius._load_bundle(workspace, contract).anchor_map.sources[0].line_count == 5
        source_map_path = workspace / "source-map.json"
        source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
        # The splitlines() count of an earlier contract is corruption in a bundle frozen under this one.
        source_map["sources"][0]["line_count"] = 6
        source_map_path.write_text(json.dumps(source_map, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(InfrastructureError, match="source map does not match"):
            Armarius._load_bundle(workspace, contract)


def test_run_frozen_before_the_line_rule_keeps_its_numbering_through_replay_and_edits(tmp_path, monkeypatch):
    service, run = _start(tmp_path, monkeypatch, LEGACY_CONTRACT)
    with service:
        assert "line_terminators" not in run.frozen_config["evidence_anchor_contract"]["content"]
        context = claim(service, run.id, "copyedit")
        source = next(item for item in context["source_map"]["sources"] if item["source_path"] == "main.tex")
        assert source["line_count"] == 6
        # Under str.splitlines() the form feed ends line 3, so "beta" is line 4.
        receipt = submit(service, context, {"summary": "Checked.", "findings": [_finding(source, 4, "beta")]})
        assert receipt["validation_report"] is None
        assert receipt["run_status"].value == "awaiting_decision"
        diff, applied = _revise_verify_apply(service, run, source, 4, "beta", "BETA")
        # Diffs of runs frozen under the earlier contract keep their historical bytes.
        assert "-beta\n+BETA\n" in diff
        assert applied == FORM_FEED_SOURCE.replace("beta", "BETA")
