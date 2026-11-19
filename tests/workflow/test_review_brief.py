import asyncio
import json

import pytest

from scriptorium.domain import canonical_json, digest_json
from scriptorium.errors import ConfigurationError, InfrastructureError, StateError
from scriptorium.schemas import SEVERITY_RUBRIC, ReviewBrief, render_review_brief
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import run_overview

from ._support import PdfBuildingManuscriptManager, claim, make_repository

_ROLES = ("substantive_review", "copyedit")
_BRIEF = {
    "venue_family": "ml_conference",
    "venue": "NeurIPS 2026",
    "stage": "presubmission",
    "priority_claims": ["The result is clear."],
    "ignore": ["Checklist answers"],
    "recorded_by": "codex session-1",
}


def _start(service, brief=None):
    text = None if brief is None else json.dumps(brief)
    return asyncio.run(service.start_run("HEAD", "quick", allow_duplicate=True, brief=text))


def _role_inputs(service, run_id):
    return {
        task.role.value: (task.input_digest, service.store.get_external_task(task.id)["prompt_digest"])
        for task in service.store.list_tasks(run_id)
    }


def _templates(run):
    templates = run.frozen_config["evidence_anchor_contract"]["prompt_templates"]
    return {role: record["content"] for role, record in templates.items()}


def test_the_brief_is_frozen_into_every_review_prompt_and_binds_its_input_digest(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=_ROLES)
    section = render_review_brief(ReviewBrief.model_validate(_BRIEF))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        plain = _start(service)["run"]
        briefed = _start(service, _BRIEF)["run"]
        changed = _start(service, {**_BRIEF, "stage": "camera_ready"})["run"]
        inputs = [_role_inputs(service, run.id) for run in (plain, briefed, changed)]
        prompts = {role: claim(service, briefed.id, role)["prompt"] for role in _ROLES}

    for role in _ROLES:
        digests = {item[role][0] for item in inputs}
        prompt_digests = {item[role][1] for item in inputs}
        assert len(digests) == len(prompt_digests) == 3, role
        assert prompts[role].count(section) == 1
        assert prompts[role].index(section) < prompts[role].index(SEVERITY_RUBRIC)
        assert prompts[role].count(SEVERITY_RUBRIC) == 1
    for role in ("writing", "check"):
        assert "Review brief" not in _templates(briefed)[role]
    assert plain.brief_digest is None
    assert briefed.brief_digest is not None and briefed.brief_digest != changed.brief_digest


def test_a_run_without_a_brief_keeps_its_earlier_prompt_bytes(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=_ROLES)
    section = render_review_brief(ReviewBrief.model_validate(_BRIEF))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        plain = _start(service)["run"]
        briefed = _start(service, _BRIEF)["run"]

    plain_templates, briefed_templates = _templates(plain), _templates(briefed)
    for role, template in plain_templates.items():
        assert "Review brief" not in template
        assert briefed_templates[role].replace(f"{section}\n\n", "") == template
    assert set(plain.frozen_config) == set(briefed.frozen_config)


def test_task_export_writes_the_frozen_brief_and_status_and_report_summarize_it(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        started = _start(service, _BRIEF)
        run = started["run"]
        context = claim(service, run.id, "copyedit")
        exported = service.export_task(context["attempt"].id, tmp_path / "export")
        status = service.run_status(run.id)
        report = service.render_report(run.id, "json")
        markdown = service.render_report(run.id, "markdown")

    expected = canonical_json(ReviewBrief.model_validate(_BRIEF).model_dump(mode="json"))
    assert (tmp_path / "export/task/brief.json").read_text(encoding="utf-8") == expected
    assert {"path": "task/brief.json", "digest": run.brief_digest} in exported["task_inputs"]
    summary = {
        "digest": run.brief_digest,
        "venue_family": "ml_conference",
        "venue": "NeurIPS 2026",
        "stage": "presubmission",
        "priority_claims": 1,
        "known_weaknesses": 0,
        "ignore": 1,
        "has_prior_reviews": False,
        "has_severity_notes": False,
    }
    assert status["review_brief"] == summary
    assert run_overview(started)["review_brief"] == summary
    assert run_overview(started)["detected_template"] == {"template": None, "venue_family": "unknown"}
    assert report["run"]["brief_digest"] == run.brief_digest
    assert report["run"]["review_brief"] == json.loads(expected)
    assert "- Review brief: `ml_conference` (NeurIPS 2026), stage `presubmission`" in markdown


def test_a_run_without_a_brief_exports_no_brief(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = _start(service)["run"]
        context = claim(service, run.id, "copyedit")
        exported = service.export_task(context["attempt"].id, tmp_path / "export")
        status = service.run_status(run.id)
        report = service.render_report(run.id, "json")

    assert [item["path"] for item in exported["task_inputs"]] == [
        "task/prompt.md",
        "task/schema.json",
        "task/example.json",
    ]
    assert not (tmp_path / "export/task/brief.json").exists()
    assert "review_brief" not in status
    assert report["run"]["brief_digest"] is None and "review_brief" not in report["run"]


def test_an_invalid_brief_is_rejected_before_any_run_is_created(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        with pytest.raises(ConfigurationError, match="invalid review brief: reviewer"):
            _start(service, {**_BRIEF, "reviewer": "x"})
        with pytest.raises(ConfigurationError, match="invalid review brief"):
            asyncio.run(service.start_run("HEAD", "quick", brief="not json"))

        assert service.store.list_runs() == []


def test_briefs_that_render_alike_still_give_distinct_bound_input_digests(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        plain = _start(service)["run"]
        first = _start(service, _BRIEF)["run"]
        second = _start(service, {**_BRIEF, "recorded_by": "claude session-2"})["run"]
        claims = [claim(service, run.id, "copyedit") for run in (plain, first, second)]
        tasks = [service.store.get_external_task(item["task"].id) for item in claims]
        with pytest.raises(StateError, match="input digest does not match"):
            service.check_submission(claims[2]["attempt"].id, claims[1]["input_digest"], "{}")

    assert tasks[1]["prompt_digest"] == tasks[2]["prompt_digest"]
    assert first.brief_digest != second.brief_digest
    assert len({item["input_digest"] for item in claims}) == 3
    assert [item["input_digest"] for item in claims] == [item["task"].input_digest for item in claims]
    keys = ("prompt_digest", "schema_digest", "bundle_digest")
    assert claims[0]["input_digest"] == digest_json({key: tasks[0][key] for key in keys})
    briefed_inputs = {key: tasks[2][key] for key in keys}
    assert claims[2]["input_digest"] == digest_json({**briefed_inputs, "brief_digest": second.brief_digest})


def test_a_corrupt_brief_artifact_blocks_submission(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = _start(service, _BRIEF)["run"]
        context = claim(service, run.id, "copyedit")
        path = service.artifacts.path_for(run.brief_digest)
        path.chmod(0o644)
        path.write_text("corrupt", encoding="utf-8")
        with pytest.raises(InfrastructureError, match="missing or corrupt review brief"):
            service.check_submission(context["attempt"].id, context["input_digest"], "{}")
