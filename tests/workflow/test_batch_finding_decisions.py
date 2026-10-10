import asyncio

import pytest

from scriptorium.domain import AgentRole, FindingStatus
from scriptorium.errors import NotFoundError, StateError
from scriptorium.service import ScriptoriumService

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit


def _service(repo):
    return ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo))


def _run_with_findings(service, count=3):
    run = asyncio.run(service.start_run("HEAD", "quick", allow_duplicate=True))["run"]
    review = claim(service, run.id, AgentRole.SUBSTANTIVE_REVIEW)
    findings = []
    for index in range(count):
        finding = review_finding(review)
        finding["title"] = f"Distinct problem {index}"
        finding["claim"] = f"Distinct claim number {index} about the result sentence."
        findings.append(finding)
    submit(service, review, {"summary": "Reviewed.", "findings": findings})
    return run, [finding.id for finding in service.list_findings(run.id)]


def test_several_findings_are_decided_with_one_record_each(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        run, ids = _run_with_findings(service)
        assert len(ids) == 3
        result = service.decide_findings(ids, "reject", "Not a real problem.")
        assert result["finding_ids"] == ids
        assert [record.target_id for record in result["decisions"]] == ids
        assert [finding.status for finding in result["findings"]] == [FindingStatus.REJECTED] * 3
        for finding_id in ids:
            decisions = service.database.list_decisions("finding", finding_id)
            assert [(d.decision, d.reason, d.actor) for d in decisions] == [("reject", "Not a real problem.", "user")]
        decided = [
            event.entity_id for event in service.database.list_events(run.id) if event.event_type == "finding.decided"
        ]
        assert decided == ids


def test_a_single_id_keeps_the_original_acknowledgement_fields(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        _run, ids = _run_with_findings(service, count=1)
        result = service.decide_finding(ids[0], "confirm", "Correct the typo.")
        assert result["decision"].target_id == ids[0]
        assert result["finding"].status == FindingStatus.CONFIRMED
        assert result["finding_ids"] == ids


def test_an_unknown_id_records_nothing(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        _run, ids = _run_with_findings(service)
        before = [service.database.get_finding(finding_id).status for finding_id in ids]
        with pytest.raises(NotFoundError):
            service.decide_findings([ids[0], "finding_missing", ids[1]], "reject", "No.")
        for finding_id, status in zip(ids, before):
            assert service.database.list_decisions("finding", finding_id) == []
            assert service.database.get_finding(finding_id).status == status


def test_ids_from_different_runs_record_nothing(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        _first, first_ids = _run_with_findings(service, count=1)
        _second, second_ids = _run_with_findings(service, count=1)
        with pytest.raises(StateError, match="same run"):
            service.decide_findings([first_ids[0], second_ids[0]], "reject", "No.")
        for finding_id in (first_ids[0], second_ids[0]):
            assert service.database.list_decisions("finding", finding_id) == []


def test_repeated_ids_record_nothing(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        _run, ids = _run_with_findings(service, count=1)
        with pytest.raises(StateError, match="repeat"):
            service.decide_findings([ids[0], ids[0]], "reject", "No.")
        assert service.database.list_decisions("finding", ids[0]) == []


def test_a_cancelled_run_rejects_the_whole_batch(tmp_path):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        run, ids = _run_with_findings(service)
        service.cancel_run(run.id, "Stop.")
        with pytest.raises(StateError, match="cannot be decided"):
            service.decide_findings(ids, "reject", "No.")
        for finding_id in ids:
            assert service.database.list_decisions("finding", finding_id) == []
