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
        finding = service.list_findings(run.id)[0]
        service.decide_finding(finding.id, "confirm", "Capitalize it.")
        asyncio.run(service.resume_run(run.id))
        revision = claim(service, run.id, AgentRole.REVISION, session="revision-session")
        edit = {
            "finding_ids": [finding.id],
            "path": "main.tex",
            "source_digest": source["source_digest"],
            "start_line": 4,
            "end_line": 4,
            "before": "beta",
            "after": "BETA",
            "rationale": "Capitalize the quoted word.",
        }
        receipt = submit(service, revision, {"summary": "Capitalized.", "edits": [edit]})
        assert receipt["validation_report"] is None
        patch = service.database.list_patches(run.id)[-1]
        assert "-beta\n+BETA\n" in service.artifacts.get_bytes(patch.diff_digest).decode("utf-8")
