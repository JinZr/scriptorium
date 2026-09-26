import asyncio
import json

import pytest

from scriptorium.domain import AgentRole, AttemptStatus, Event, RunStatus, digest_json
from scriptorium.schemas import SourceAnchorRecord, output_schema
from scriptorium.service import ScriptoriumService
from scriptorium.workflow import Armarius

from ._support import PdfBuildingManuscriptManager, claim, make_repository, submit


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
                json.dumps({"summary": "Reviewed source text.", "findings": [], "scope": scope, "claim_checks": []}),
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
        assert report["review_coverage_audit"] == [
            {
                "task_id": review["task"].id,
                "attempt_id": review["attempt"].id,
                "role": "substantive_review",
                "read_lines": [{"source_path": "main.tex", "ranges": [{"start_line": 1, "end_line": 2}]}],
                "search_matches": [],
                "pages_returned": [],
                "declared_without_task_read": [{"source_path": "main.tex", "start_line": 3, "end_line": 4}],
                "declared_without_task_page": [],
                "not_comparable": [],
            }
        ]
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
        assert "task read returned line fragments: `main.tex:1-2`" in markdown
        assert "declared checked without task read return: `main.tex:3-4`" in markdown


def test_coverage_audit_uses_cumulative_attempt_returns_and_separates_search(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        first = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        service.read_task(first["attempt"].id, "main.tex", 1, 2, 0, 8000)
        service.search_task(first["attempt"].id, "result", None, 0, 10)
        submit(
            service,
            first,
            {
                "summary": "First half checked.",
                "findings": [],
                "scope": {
                    "completion": "partial",
                    "checked": [{"source_path": "main.tex", "start_line": 1, "end_line": 2}],
                    "outstanding": [{"source_path": "manuscript.pdf", "page": 1}],
                    "limitations": [],
                },
            },
        )
        asyncio.run(service.continue_review(run.id, first["task"].id))
        second = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, session="continued-session")
        service.read_task(second["attempt"].id, "main.tex", 4, 1, 0, 8000)
        service.page_task(second["attempt"].id, 1)
        submit(
            service,
            second,
            {
                "summary": "Remaining material checked.",
                "findings": [],
                "scope": {
                    "completion": "complete",
                    "checked": [{"source_path": "main.tex"}, {"source_path": "manuscript.pdf", "page": 1}],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        audit = service.render_report(run.id, "json")["review_coverage_audit"][0]
        assert audit["attempt_id"] == second["attempt"].id
        assert audit["read_lines"] == [
            {"source_path": "main.tex", "ranges": [{"start_line": 1, "end_line": 2}, {"start_line": 4, "end_line": 4}]}
        ]
        assert audit["search_matches"] == [{"source_path": "main.tex", "lines": [3]}]
        assert audit["pages_returned"] == [1]
        assert audit["declared_without_task_read"] == [{"source_path": "main.tex", "start_line": 3, "end_line": 3}]
        assert audit["declared_without_task_page"] == []


def test_coverage_audit_flags_checked_page_without_task_page_return(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        submit(
            service,
            review,
            {
                "summary": "The page was checked by the host.",
                "findings": [],
                "scope": {
                    "completion": "complete",
                    "checked": [{"source_path": "manuscript.pdf", "page": 1}],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        audit = service.render_report(run.id, "json")["review_coverage_audit"][0]
        assert audit["declared_without_task_page"] == [1]
        assert "declared checked without task page return: `manuscript.pdf:page 1`" in service.render_report(
            run.id, "markdown"
        )


def test_coverage_audit_prioritizes_canonical_source_paths_over_read_aliases():
    sources = [
        SourceAnchorRecord(
            source_path=path,
            read_path=f"sources/{path}",
            source_digest="a" * 64,
            line_count=2,
            text_anchorable=True,
        )
        for path in ("foo.tex", "sources/foo.tex")
    ]
    events = [
        Event(
            run_id="run",
            event_type="tool.read",
            entity_type="attempt",
            entity_id="attempt",
            payload={
                "path": "sources/foo.tex",
                "ranges": [{"line": 2, "start_offset": 0, "end_offset": 2}],
            },
        ),
        Event(
            run_id="run",
            event_type="tool.search",
            entity_type="attempt",
            entity_id="attempt",
            payload={"matches": [{"path": "sources/foo.tex", "line": 1}]},
        ),
    ]
    scope = {"checked": [{"source_path": "sources/foo.tex", "start_line": 2, "end_line": 2}]}
    audit = ScriptoriumService._review_coverage_audit(scope, events, sources)
    assert audit["read_lines"] == [{"source_path": "sources/foo.tex", "ranges": [{"start_line": 2, "end_line": 2}]}]
    assert audit["search_matches"] == [{"source_path": "sources/foo.tex", "lines": [1]}]
    assert audit["declared_without_task_read"] == []


def test_coverage_audit_does_not_credit_an_empty_read_fragment(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        result = service.read_task(review["attempt"].id, "main.tex", 3, 1, len("The result is teh clear."), 8000)
        assert result["lines"] == [{"line": 3, "offset": len("The result is teh clear."), "text": ""}]
        submit(
            service,
            review,
            {
                "summary": "The line was checked by the host.",
                "findings": [],
                "scope": {
                    "completion": "complete",
                    "checked": [{"source_path": "main.tex", "start_line": 3, "end_line": 3}],
                    "outstanding": [],
                    "limitations": [],
                },
            },
        )
        audit = service.render_report(run.id, "json")["review_coverage_audit"][0]
        assert audit["read_lines"] == []
        assert audit["declared_without_task_read"] == [{"source_path": "main.tex", "start_line": 3, "end_line": 3}]


def test_coverage_audit_subtracts_large_whole_file_scope_as_spans():
    source = SourceAnchorRecord(
        source_path="large.tex",
        read_path="sources/large.tex",
        source_digest="a" * 64,
        line_count=1_000_000,
        text_anchorable=True,
    )
    event = Event(
        run_id="run",
        event_type="tool.read",
        entity_type="attempt",
        entity_id="attempt",
        payload={"path": "large.tex", "ranges": [{"line": 500_000, "start_offset": 0, "end_offset": 1}]},
    )
    audit = ScriptoriumService._review_coverage_audit({"checked": [{"source_path": "large.tex"}]}, [event], [source])
    assert audit["declared_without_task_read"] == [
        {"source_path": "large.tex", "start_line": 1, "end_line": 499_999},
        {"source_path": "large.tex", "start_line": 500_001, "end_line": 1_000_000},
    ]


def test_coverage_audit_merges_overlapping_declarations_before_subtraction():
    assert ScriptoriumService._missing_line_spans([(4, 10), (1, 5), (12, 12)], {2, 6, 12}) == [
        {"start_line": 1, "end_line": 1},
        {"start_line": 3, "end_line": 5},
        {"start_line": 7, "end_line": 10},
    ]


@pytest.mark.parametrize("completion", ["partial", "unknown"])
def test_finalized_scoped_run_keeps_prior_gate_result(tmp_path, monkeypatch, completion):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
        with monkeypatch.context() as prior_release:
            prior_release.setattr(service.armarius, "review_completion", lambda task: "complete")
            receipt = asyncio.run(
                service.submit_task(
                    review["attempt"].id,
                    review["input_digest"],
                    json.dumps(
                        {
                            "summary": "Reviewed available material.",
                            "findings": [],
                            "claim_checks": [],
                            "scope": {
                                "completion": completion,
                                "checked": [],
                                "outstanding": [],
                                "limitations": ["Some material remains unchecked."],
                            },
                        }
                    ),
                )
            )
            asyncio.run(service.resume_run(run.id))
        assert service.database.get_run(run.id).status == RunStatus.COMPLETED
        assert service.armarius.review_completion(review["task"]) == completion
        assert service.evaluate_gate(run.id)["passed"]
        service.artifacts.path_for(receipt["output_digest"]).write_text("corrupt", encoding="utf-8")
        gate = service.evaluate_gate(run.id)
        assert not gate["passed"]
        assert not gate["conditions"]["review_artifacts_valid"]


def test_old_review_report_marks_scope_as_unreported(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    original = Armarius._freeze_config

    def old_freeze(self, *args, **kwargs):
        frozen = original(self, *args, **kwargs)
        schema = output_schema("review", legacy_review=True)
        frozen["schemas"]["review"] = {"digest": digest_json(schema), "content": schema}
        frozen["schemas"].pop("scientific_review")
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
