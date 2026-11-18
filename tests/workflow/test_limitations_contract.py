import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, link_claims, make_repository, submit

CLAIM_CHECK = {
    "claim": "The paper reports a result.",
    "evidence": [{"source_path": "manuscript.pdf", "page": 1}],
    "critical_question": "Does the evidence support the result?",
    "countercheck": "Checked the rendered manuscript.",
    "claim_anchor": {"source_path": "manuscript.pdf", "page": 1},
    "stated_scope": "As stated in the manuscript.",
    "check_type": "design_and_analysis",
    "question_answer": "yes",
    "exceptions": [],
    "assessment": "supported",
    "finding_indices": [],
}
_MISSING = object()


def _substantive_output(limitations=_MISSING):
    output = link_claims({"summary": "Reviewed the manuscript.", "findings": [], "claim_checks": [CLAIM_CHECK]})
    if limitations is not _MISSING:
        output["limitations"] = limitations
    return output


@pytest.mark.parametrize(
    ("limitations", "code", "path"),
    [
        (_MISSING, "schema.missing", "/limitations"),
        ("The supplement is absent.", "schema.type", "/limitations"),
        ([1], "schema.type", "/limitations/0"),
        ([""], "schema.constraint", "/limitations/0"),
        (["   "], "schema.cross_field", "/limitations"),
    ],
)
def test_invalid_limitations_reject_whole_result(tmp_path, limitations, code, path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        output = _substantive_output(limitations)
        receipt = asyncio.run(service.submit_task(review["attempt"].id, review["input_digest"], json.dumps(output)))
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert [(issue["code"], issue["path"]) for issue in receipt["validation_report"]["issues"]] == [(code, path)]
        assert service.list_findings(run.id) == []


@pytest.mark.parametrize("role", ["substantive_review", "copyedit"])
@pytest.mark.parametrize("limitations", [[], ["The appendix the text cites is not in the frozen sources."]])
def test_limitations_may_be_empty_or_list_unchecked_material(tmp_path, role, limitations):
    repo = make_repository(tmp_path, roles=(role,))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole(role))
        output = {"summary": "Reviewed the manuscript.", "findings": [], "limitations": limitations}
        assert submit(service, review, output)["attempt"].status == AttemptStatus.COMPLETED


@pytest.mark.parametrize(
    ("number", "actual"),
    [
        ("1.0000000000000001", "1.0000000000000001"),
        ("1e1000000000", "1E+1000000000"),
        ("1e-1000000000", "1E-1000000000"),
    ],
)
def test_invalid_index_keeps_raw_json_precision(tmp_path, number, actual):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        output = json.dumps(_substantive_output([])).replace('"claim_index": 0', f'"claim_index": {number}')
        result = asyncio.run(service.submit_task(review["attempt"].id, review["input_digest"], output))

        assert result["attempt"].status == AttemptStatus.FAILED
        assert [(issue["code"], issue["path"], issue["actual"]) for issue in result["validation_report"]["issues"]] == [
            ("schema.type", "/claim_checks/0/claim_index", actual)
        ]
        assert service.list_findings(run.id) == []


@pytest.mark.parametrize("number", ["1e999999999999999999999999", "9" * 5000])
def test_unparseable_json_number_creates_validation_report(tmp_path, number):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        output = json.dumps(_substantive_output([])).replace('"claim_index": 0', f'"claim_index": {number}')
        result = asyncio.run(service.submit_task(review["attempt"].id, review["input_digest"], output))

        assert result["attempt"].status == AttemptStatus.FAILED
        assert result["validation_report"]["issues"][0]["code"] == "json.invalid"
        assert service.list_findings(run.id) == []


def test_new_frozen_review_schema_survives_generator_drift(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        monkeypatch.setattr(
            "scriptorium.workflow.output_schema",
            lambda *args, **kwargs: pytest.fail("frozen output must not depend on the installed schema generator"),
        )
        assert (
            submit(service, review, {"summary": "Reviewed.", "findings": []})["attempt"].status
            == AttemptStatus.COMPLETED
        )
        asyncio.run(service.resume_run(run.id))
