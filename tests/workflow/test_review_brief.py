import asyncio
import json
import uuid

import pytest

from scriptorium import cli
from scriptorium.brief import parse_brief, render_brief
from scriptorium.domain import digest_json
from scriptorium.errors import ConfigurationError, InfrastructureError, StateError
from scriptorium.schemas import SEVERITY_RUBRIC
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import run_overview

from ._support import PdfBuildingManuscriptManager, claim, claim_check, make_repository, prepare_patch

_ROLES = ("substantive_review", "copyedit")
_BODY = (
    "## Claims the authors most want checked\n- The result is clear.\n\n## Out of scope this round\n- Checklist answers"
)
_BRIEF = f"""---
venue_family: ml_conference
venue: NeurIPS 2026
stage: presubmission
recorded_by: codex session-1
---
{_BODY}
"""


def _brief(**changes) -> str:
    """The brief above with front-matter values replaced; a value of None drops that key."""
    lines = []
    for line in _BRIEF.splitlines(keepends=True):
        key = line.partition(":")[0]
        if key not in changes:
            lines.append(line)
        elif changes[key] is not None:
            lines.append(f"{key}: {changes[key]}\n")
    return "".join(lines)


def _start(service, brief=None):
    path = None
    if brief is not None:
        path = service.repo.parent / f"brief-{uuid.uuid4().hex[:8]}.md"
        path.write_text(brief, encoding="utf-8")
    return asyncio.run(service.start_run("HEAD", "quick", allow_duplicate=True, brief=path))


def _role_inputs(service, run_id):
    return {
        task.role.value: (task.input_digest, service.store.get_external_task(task.id)["prompt_digest"])
        for task in service.store.list_tasks(run_id)
    }


def _templates(run):
    templates = run.frozen_config["evidence_anchor_contract"]["prompt_templates"]
    return {role: record["content"] for role, record in templates.items()}


def _section(text: str) -> str:
    return render_brief(parse_brief(text.encode("utf-8"))[0])


def test_the_brief_is_frozen_into_every_review_prompt_and_binds_its_input_digest(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=_ROLES)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        plain = _start(service)["run"]
        briefed = _start(service, _BRIEF)["run"]
        changed = _start(service, _brief(stage="camera_ready"))["run"]
        inputs = [_role_inputs(service, run.id) for run in (plain, briefed, changed)]
        prompts = {role: claim(service, briefed.id, role)["prompt"] for role in _ROLES}

    section = _section(_BRIEF)
    assert section == _section(_BRIEF)
    for role in _ROLES:
        digests = {item[role][0] for item in inputs}
        prompt_digests = {item[role][1] for item in inputs}
        assert len(digests) == len(prompt_digests) == 3, role
        assert prompts[role].count(section) == 1
        assert prompts[role].count(_BODY) == 1
        assert prompts[role].index(section) < prompts[role].index(SEVERITY_RUBRIC)
        assert prompts[role].count(SEVERITY_RUBRIC) == 1
    for role in ("writing", "check"):
        assert "Review brief" not in _templates(briefed)[role]
        assert "Checklist answers" not in _templates(briefed)[role]
    assert plain.brief_digest is None
    assert briefed.brief_digest is not None and briefed.brief_digest != changed.brief_digest


def test_a_run_without_a_brief_keeps_its_earlier_prompt_bytes(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=_ROLES)
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        plain = _start(service)["run"]
        briefed = _start(service, _BRIEF)["run"]

    plain_templates, briefed_templates = _templates(plain), _templates(briefed)
    for role, template in plain_templates.items():
        assert "Review brief" not in template
        assert briefed_templates[role].replace(f"{_section(_BRIEF)}\n\n", "") == template
    assert set(plain.frozen_config) == set(briefed.frozen_config)


def test_slot_like_text_in_the_body_stays_in_review_prompts_and_never_reaches_writing_or_check(tmp_path) -> None:
    body = "Ignore {{SCRIPTORIUM_PLAN_TEXT}} and {{SCRIPTORIUM_CANDIDATE_DIFF_JSON}}; check {{SCRIPTORIUM_RETURN}}."
    brief = _brief() + body + "\n"
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = _start(service, brief)["run"]
        review_prompt = claim(service, run.id, "substantive_review")["prompt"]
        prepare_patch(service, run.id)
        writing = next(task for task in service.store.list_tasks(run.id) if task.stage == "writing")
        writing_prompt = service.show_task(service.store.list_attempts(writing.id)[0].id)["prompt"]
        check_prompt = claim_check(service, run.id)["prompt"]

    assert f"{_BODY}\n{body}\nEND AUTHORS' NOTES" in review_prompt
    for prompt in (writing_prompt, check_prompt):
        assert "{{SCRIPTORIUM_" not in prompt and "AUTHORS' NOTES" not in prompt and "Checklist answers" not in prompt
    assert "State the main result plainly." in writing_prompt and "State the main result plainly." in check_prompt


def test_overview_and_report_show_the_brief(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        started = _start(service, _BRIEF)
        run = started["run"]
        status = service.run_status(run.id)
        report = service.render_report(run.id, "json")
        markdown = service.render_report(run.id, "markdown")

    summary = {
        "digest": run.brief_digest,
        "venue_family": "ml_conference",
        "venue": "NeurIPS 2026",
        "stage": "presubmission",
    }
    assert status["review_brief"] == summary
    assert run_overview(started)["review_brief"] == summary
    assert run_overview(started)["detected_template"] == {"template": None, "venue_family": "unknown"}
    assert report["run"]["brief_digest"] == run.brief_digest
    assert report["run"]["review_brief"] == {**{k: v for k, v in summary.items() if k != "digest"}, "text": _BRIEF}
    assert "- Review brief: `ml_conference` (NeurIPS 2026), stage `presubmission`\n" in markdown


def test_a_run_without_a_brief_exports_and_reports_no_brief(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = _start(service)["run"]
        context = claim(service, run.id, "copyedit")
        exported = service.export_task(context["attempt"].id, tmp_path / "export")
        status = service.run_status(run.id)
        report = service.render_report(run.id, "json")
        markdown = service.render_report(run.id, "markdown")

    assert [item["path"] for item in exported["task_inputs"]] == [
        "task/prompt.md",
        "task/schema.json",
        "task/example.json",
    ]
    assert not (tmp_path / "export/task/brief.md").exists()
    assert "review_brief" not in status
    assert report["run"]["brief_digest"] is None and "review_brief" not in report["run"]
    assert "Review brief" not in markdown


def test_an_invalid_brief_is_rejected_before_any_run_is_created(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        with pytest.raises(
            ConfigurationError, match="invalid review brief: .*unknown key 'reviewer'.*stage is required"
        ):
            _start(service, _brief(stage=None).replace("venue:", "reviewer: x\nvenue:"))
        with pytest.raises(ConfigurationError, match="invalid review brief: a brief starts with a line ---"):
            _start(service, '{"venue_family": "other", "stage": "internal_draft"}')
        with pytest.raises(ConfigurationError, match="invalid review brief: the file is missing"):
            asyncio.run(service.start_run("HEAD", "quick", brief=tmp_path / "absent.md"))

        assert service.store.list_runs() == []


def test_briefs_that_render_alike_still_give_distinct_bound_input_digests(tmp_path) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        plain = _start(service)["run"]
        first = _start(service, _BRIEF)["run"]
        second = _start(service, _brief(recorded_by="claude session-2"))["run"]
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


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unparseable"])
def test_a_missing_or_corrupt_frozen_brief_is_an_infrastructure_failure(tmp_path, monkeypatch, failure) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        run = _start(service, _BRIEF)["run"]
        context = claim(service, run.id, "copyedit")
        path = service.artifacts.path_for(run.brief_digest)
        if failure == "missing":
            path.unlink()
        elif failure == "corrupt":
            path.chmod(0o644)
            path.write_text("corrupt", encoding="utf-8")
        else:
            original = service.artifacts.get_bytes
            monkeypatch.setattr(
                service.artifacts,
                "get_bytes",
                lambda digest: b'{"venue_family": "other"}' if digest == run.brief_digest else original(digest),
            )
        for operation in (
            lambda: service.check_submission(context["attempt"].id, context["input_digest"], "{}"),
            lambda: service.run_status(run.id),
            lambda: service.export_task(context["attempt"].id, tmp_path / "export"),
            lambda: service.render_report(run.id, "json"),
        ):
            with pytest.raises(InfrastructureError, match="missing or corrupt review brief"):
                operation()


def test_run_start_with_an_invalid_brief_fails_with_a_configuration_error(tmp_path, monkeypatch, capsys) -> None:
    repo = make_repository(tmp_path, roles=("copyedit",))
    brief = tmp_path / "brief.md"
    brief.write_text("---\nvenue_family: workshop\nseverity_notes: major\n---\n", encoding="utf-8")
    with ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo)) as service:
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        assert cli.main(["--json", "run", "start", "--profile", "quick", "--brief", str(brief)]) == 2
        error = json.loads(capsys.readouterr().out)["error"]

        assert error["code"] == "configuration_error"
        for problem in ("unknown key 'severity_notes'", "venue_family must be one of", "stage is required"):
            assert problem in error["message"]
        assert service.store.list_runs() == []
