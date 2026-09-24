import asyncio
import json

from scriptorium.domain import AgentRole, AttemptStatus, digest_json
from scriptorium.schemas import output_schema
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import PdfBuildingManuscriptManager, claim, make_repository


def test_review_scope_is_reported_separately_from_tool_returns(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        assert "scope" in review["schema"]["required"]
        service.read_task(review["attempt"].id, "main.tex", 1, 2, 0, 8000)
        scope = {
            "completion": "partial",
            "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 4}],
            "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
            "limitations": ["The rendered page was not visually inspected."],
        }
        receipt = asyncio.run(
            service.submit_task(
                review["attempt"].id,
                review["input_digest"],
                json.dumps({"summary": "Reviewed source text.", "findings": [], "scope": scope}),
            )
        )
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        assert not service.evaluate_gate(run.id)["passed"]
        report = service.render_report(run.id, "json")
        assert report["review_scopes"] == [
            {
                "task_id": review["task"].id,
                "attempt_id": review["attempt"].id,
                "role": "substantive_review",
                "scope": scope,
            }
        ]
        assert [event["event_type"] for event in report["events"]].count("tool.read") == 1
        assert report["review_tool_access"] == [
            {
                "task_id": review["task"].id,
                "attempt_id": review["attempt"].id,
                "role": "substantive_review",
                "status": "completed",
                "returns": {"read": 1, "search": 0, "page": 0},
            }
        ]
        markdown = service.render_report(run.id, "markdown")
        assert "declared `partial`" in markdown
        assert "outstanding: `manuscript.pdf:page 1`" in markdown
        assert "## Observed review task-tool returns" in markdown
        assert "read 1, search 0, page 0" in markdown
        assert "Scope is model-declared; tool returns do not prove inspection" in markdown


def test_old_review_report_marks_scope_as_unreported(tmp_path, monkeypatch):
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
        service.read_task(review["attempt"].id, "main.tex", 1, 1, 0, 8000)
        asyncio.run(
            service.submit_task(
                review["attempt"].id,
                review["input_digest"],
                json.dumps({"summary": "Old review output.", "findings": []}),
            )
        )
        report = service.render_report(run.id, "json")
        assert report["review_scopes"][0]["scope"] is None
        assert report["review_tool_access"][0]["returns"] == {"read": 1, "search": 0, "page": 0}
        assert "scope not reported by frozen contract" in service.render_report(run.id, "markdown")
        assert "read 1, search 0, page 0" in service.render_report(run.id, "markdown")


def test_failed_review_still_reports_tool_returns(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        service.search_task(review["attempt"].id, "result", None, 0, 1)
        rejected = asyncio.run(
            service.submit_task(
                review["attempt"].id,
                review["input_digest"],
                json.dumps({"summary": "Missing scope.", "findings": []}),
            )
        )
        assert rejected["attempt"].status == AttemptStatus.FAILED
        report = service.render_report(run.id, "json")
        assert report["review_scopes"] == []
        assert report["review_tool_access"][0]["status"] == "failed"
        assert report["review_tool_access"][0]["returns"] == {"read": 0, "search": 1, "page": 0}
        assert "search 1" in service.render_report(run.id, "markdown")
