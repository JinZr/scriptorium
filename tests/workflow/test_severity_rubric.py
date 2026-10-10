import asyncio

from scriptorium.domain import AgentRole, AttemptStatus, canonical_json, digest_json
import scriptorium.schemas
from scriptorium.schemas import SEVERITY_RUBRIC, InventoriedScientificReviewOutput, ScopedReviewOutput
from scriptorium.service import ScriptoriumService
import scriptorium.workflow

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit

_REVIEW_ROLES = ("substantive_review", "copyedit", "consistency", "figure_review")


def _role_inputs(service, run_id):
    inputs = {}
    for task in service.database.list_tasks(run_id):
        metadata = service.database.get_external_task(task.id)
        bound = {key: metadata[key] for key in ("prompt_digest", "schema_digest", "bundle_digest")}
        assert task.input_digest == digest_json(bound)
        inputs[task.role.value] = (task.input_digest, metadata["prompt_digest"])
    return inputs


def test_every_review_role_freezes_the_rubric_once_into_its_task_input(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=_REVIEW_ROLES)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        for role in _REVIEW_ROLES:
            prompt = claim(service, run.id, role)["prompt"]
            assert prompt.count(SEVERITY_RUBRIC) == 1, role
            assert prompt.count("Severity rubric") == 1, role
        assert all(
            "Severity rubric" not in run.frozen_config["evidence_anchor_contract"]["prompt_templates"][role]["content"]
            for role in ("revision", "verification")
        )
        current = _role_inputs(service, run.id)

        monkeypatch.setattr(scriptorium.workflow, "SEVERITY_RUBRIC", "Severity rubric: a different frozen text.")
        changed = _role_inputs(service, asyncio.run(service.start_run("HEAD", "quick", allow_duplicate=True))["run"].id)

    for role in _REVIEW_ROLES:
        assert changed[role][1] != current[role][1]
        assert changed[role][0] != current[role][0]


def test_new_findings_require_a_recorded_consequence(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.COPYEDIT)
        finding = review_finding(review)
        missing = {key: value for key, value in finding.items() if key != "consequence"}
        checked = service.check_submission(
            review["attempt"].id,
            review["input_digest"],
            canonical_json(
                {
                    "summary": "Checked language.",
                    "findings": [missing],
                    "scope": {"completion": "complete", "checked": [], "outstanding": [], "limitations": []},
                }
            ),
        )
        assert [(issue["code"], issue["path"]) for issue in checked["validation_report"]["issues"]] == [
            ("schema.missing", "/findings/0/consequence")
        ]
        compliance = {**finding, "category": "submission_compliance"}
        rejected = service.check_submission(
            review["attempt"].id,
            review["input_digest"],
            canonical_json(
                {
                    "summary": "Checked language.",
                    "findings": [compliance],
                    "scope": {"completion": "complete", "checked": [], "outstanding": [], "limitations": []},
                }
            ),
        )
        issue = rejected["validation_report"]["issues"][0]
        assert (issue["code"], issue["path"]) == ("schema.cross_field", "/findings/0")
        assert "submission_compliance finding must be minor or suggestion" in issue["message"]

        receipt = submit(service, review, {"summary": "Checked language.", "findings": [finding]})
        assert receipt["attempt"].status == AttemptStatus.COMPLETED
        [stored] = service.database.list_findings(run.id)
        assert stored.consequence == finding["consequence"]
        assert service.get_finding(stored.id)["finding"].consequence == finding["consequence"]
        report = service.render_report(run.id, "json")
        assert report["findings"][0]["finding"]["consequence"] == finding["consequence"]


def test_scope_entries_given_as_strings_are_explained_by_check(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.COPYEDIT)
        checked = service.check_submission(
            review["attempt"].id,
            review["input_digest"],
            canonical_json(
                {
                    "summary": "Checked language.",
                    "findings": [],
                    "scope": {"completion": "partial", "checked": ["main.tex"], "outstanding": [], "limitations": []},
                }
            ),
        )
        [issue] = checked["validation_report"]["issues"]
        assert (issue["code"], issue["path"], issue["actual"]) == ("schema.type", "/scope/checked/0", "main.tex")
        assert "must be an object, not a string" in issue["message"]
        assert "source_path" in issue["message"] and "start_line" in issue["message"]


def test_runs_frozen_before_the_rubric_accept_findings_without_a_consequence(tmp_path, monkeypatch):
    repo = make_repository(tmp_path, roles=("substantive_review", "copyedit"))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        with monkeypatch.context() as patch:
            patch.setitem(scriptorium.schemas.SCHEMA_MODELS, "review", ScopedReviewOutput)
            patch.setitem(scriptorium.schemas.SCHEMA_MODELS, "scientific_review", InventoriedScientificReviewOutput)
            run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        for role in (AgentRole.SUBSTANTIVE_REVIEW, AgentRole.COPYEDIT):
            review = claim(service, run.id, role)
            finding = review_finding(review)
            assert "consequence" not in finding
            receipt = submit(service, review, {"summary": "Checked the result.", "findings": [finding]})
            assert receipt["attempt"].status == AttemptStatus.COMPLETED, receipt.get("validation_report")
        findings = service.database.list_findings(run.id)
        assert findings and all(item.consequence is None for item in findings)
        report = service.render_report(run.id, "json")
        assert all(item["finding"]["consequence"] is None for item in report["findings"])
        assert service.render_report(run.id, "markdown")
