import asyncio
import json
import shutil
import subprocess

import pytest

from scriptorium.errors import InfrastructureError
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit

FORM_FEED_SOURCE = "\\documentclass{article}\n\\begin{document}\nalpha\fbeta\ngamma delta\n\\end{document}\n"


@pytest.fixture
def form_feed_run(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    (repo / "main.tex").write_text(FORM_FEED_SOURCE, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "Form feed"], check=True)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        yield service, run


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


def test_read_tool_line_numbers_produce_accepted_evidence(form_feed_run):
    service, run = form_feed_run
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


@pytest.mark.parametrize(("line_count", "loads"), [(5, True), (6, True), (7, False)])
def test_bundles_keep_their_frozen_line_count_definition(form_feed_run, tmp_path, line_count, loads):
    service, run = form_feed_run
    workspace = tmp_path / "bundle"
    shutil.copytree(service.repo / ".scriptorium/runs" / run.id / "bundle", workspace)
    source_map_path = workspace / "source-map.json"
    source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
    entry = next(item for item in source_map["sources"] if item["source_path"] == "main.tex")
    assert entry["line_count"] == 5
    # A bundle frozen before this definition recorded str.splitlines(), which also breaks at the form feed.
    entry["line_count"] = line_count
    source_map_path.write_text(json.dumps(source_map, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    contract = service.armarius.require_evidence_anchor_contract(run.id)
    if loads:
        bundle = Armarius._load_bundle(workspace, contract)
        loaded = next(item for item in bundle.anchor_map.sources if item.source_path == "main.tex")
        assert loaded.line_count == line_count
    else:
        with pytest.raises(InfrastructureError, match="source map does not match"):
            Armarius._load_bundle(workspace, contract)
