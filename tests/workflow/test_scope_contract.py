import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, digest_json
from scriptorium.errors import InfrastructureError
from scriptorium.schemas import ReviewOutput, RevisionOutput, output_schema
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit


@pytest.mark.parametrize(
    ("scope", "code", "path"),
    [
        (None, "schema.missing", "/scope"),
        (
            {
                "completion": "partial",
                "checked": [{"source_path": "../private.tex"}],
                "outstanding": [],
                "limitations": [],
            },
            "scope.path_unknown",
            "/scope/checked/0/source_path",
        ),
        (
            {
                "completion": "partial",
                "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 99}],
                "outstanding": [],
                "limitations": [],
            },
            "scope.line_out_of_range",
            "/scope/checked/0",
        ),
        (
            {
                "completion": "partial",
                "checked": [],
                "outstanding": [{"source_path": "manuscript.pdf", "page": 99}],
                "limitations": [],
            },
            "scope.page_out_of_range",
            "/scope/outstanding/0/page",
        ),
        (
            {
                "completion": "unknown",
                "checked": [{"source_path": "main.tex", "start_line": 1}],
                "outstanding": [],
                "limitations": [],
            },
            "schema.cross_field",
            "/scope/checked/0",
        ),
        (
            {
                "completion": "unknown",
                "checked": [{"source_path": "manuscript.pdf", "page": 1, "start_line": None}],
                "outstanding": [],
                "limitations": [],
            },
            "schema.cross_field",
            "/scope/checked/0",
        ),
        (
            {
                "completion": "unknown",
                "checked": [{"source_path": "main.tex", "page": None}],
                "outstanding": [],
                "limitations": [],
            },
            "schema.cross_field",
            "/scope/checked/0",
        ),
        (
            {
                "completion": "unknown",
                "checked": [{"source_path": "main.tex", "start_line": None, "end_line": None}],
                "outstanding": [],
                "limitations": [],
            },
            "schema.cross_field",
            "/scope/checked/0",
        ),
        (
            {
                "completion": "unknown",
                "checked": [{"source_path": "manuscript.pdf", "page": "1"}],
                "outstanding": [],
                "limitations": [],
            },
            "schema.type",
            "/scope/checked/0/page",
        ),
        (
            {
                "completion": "complete",
                "checked": [],
                "outstanding": [{"source_path": "main.tex"}],
                "limitations": [],
            },
            "schema.cross_field",
            "/scope",
        ),
    ],
)
def test_invalid_review_scope_rejects_whole_result(tmp_path, scope, code, path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        output = {"summary": "Reviewed the manuscript.", "findings": []}
        if scope is not None:
            output["scope"] = scope
        receipt = asyncio.run(service.submit_task(review["attempt"].id, review["input_digest"], json.dumps(output)))
        assert receipt["attempt"].status == AttemptStatus.FAILED
        assert [(issue["code"], issue["path"]) for issue in receipt["validation_report"]["issues"]] == [(code, path)]
        assert service.list_findings(run.id) == []


def test_integral_json_number_scope_is_accepted(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        result = submit(
            service,
            review,
            {
                "summary": "Reviewed the manuscript.",
                "findings": [],
                "scope": {
                    "completion": "complete",
                    "checked": [{"source_path": "manuscript.pdf", "page": 1.0}],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        assert result["attempt"].status == AttemptStatus.COMPLETED


def test_fractional_coordinate_keeps_raw_json_precision(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        output = (
            '{"summary":"Reviewed.","findings":[],"scope":{"completion":"unknown",'
            '"checked":[{"source_path":"manuscript.pdf","page":1.0000000000000001}],'
            '"outstanding":[],"limitations":[]}}'
        )
        result = asyncio.run(service.submit_task(review["attempt"].id, review["input_digest"], output))

        assert result["attempt"].status == AttemptStatus.FAILED
        assert [(issue["code"], issue["path"], issue["actual"]) for issue in result["validation_report"]["issues"]] == [
            ("schema.type", "/scope/checked/0/page", "1.0000000000000001")
        ]
        assert service.list_findings(run.id) == []


def test_old_frozen_review_schema_replays_without_scope(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    original = Armarius._freeze_config

    def old_freeze(self, *args, **kwargs):
        frozen = original(self, *args, **kwargs)
        schema = output_schema("review", legacy_review=True)
        frozen["schemas"]["review"] = {"digest": digest_json(schema), "content": schema}
        return frozen

    with monkeypatch.context() as patch:
        patch.setattr(Armarius, "_freeze_config", old_freeze)
        with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
            run = asyncio.run(service.start_run("HEAD", "quick"))["run"]

    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "scope" not in review["schema"]["properties"]
        monkeypatch.setattr(
            "scriptorium.workflow.output_schema",
            lambda *args, **kwargs: pytest.fail("frozen output must not depend on the installed schema generator"),
        )
        result = asyncio.run(
            service.submit_task(
                review["attempt"].id,
                review["input_digest"],
                json.dumps({"summary": "Reviewed the old contract.", "findings": []}),
            )
        )
        assert result["attempt"].status == AttemptStatus.COMPLETED

    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        asyncio.run(service.resume_run(run.id))
        frozen = service.database.get_run(run.id)
        changed_title = {**frozen.frozen_config["schemas"]["review"]["content"], "title": "OtherReviewOutput"}
        assert service.armarius._output_model_for_schema(frozen, "review", changed_title) is ReviewOutput
        assert (
            service.armarius._output_model_for_schema(frozen, "revision", {"title": "OtherRevisionOutput"})
            is RevisionOutput
        )
        unsupported = {**changed_title, "required": ["summary"]}
        with pytest.raises(InfrastructureError, match="unsupported frozen review output schema"):
            service.armarius._output_model_for_schema(frozen, "review", unsupported)


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
