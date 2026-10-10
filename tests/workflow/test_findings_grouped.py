import asyncio
import json
import shlex

import pytest

from scriptorium import cli
from scriptorium.domain import AgentRole, canonical_json
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit

_ROLES = ("substantive_review", "copyedit", "consistency")
_LINES = {1: "\\documentclass{article}", 3: "The result is teh clear.", 4: "\\end{document}"}


def _service(repo):
    return ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo))


def _accepts_affected_claim(review):
    schema = review["schema"]
    reference = schema["properties"]["findings"]["items"]["$ref"].rpartition("/")[2]
    return "affected_claim" in schema["$defs"][reference]["properties"]


def _finding(review, line, title, *, category="style", severity="minor", affected_claim=None):
    finding = review_finding(review)
    finding.pop("affected_claim", None)
    anchor = {**finding["evidence"][0], "start_line": line, "end_line": line, "quoted_text": _LINES[line]}
    finding.update(category=category, severity=severity, title=title, claim=f"{title} at line {line}.")
    finding["evidence"] = [anchor]
    if affected_claim is not None:
        finding["affected_claim"] = affected_claim
    return finding


def _submit(service, run_id, role, findings):
    review = claim(service, run_id, role)
    submit(service, review, {"summary": "Reviewed.", "findings": [build(review) for build in findings]})


def _three_role_run(service):
    run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    _submit(service, run.id, AgentRole.SUBSTANTIVE_REVIEW, [review_finding])
    _submit(
        service,
        run.id,
        AgentRole.COPYEDIT,
        [
            lambda review: _finding(review, 3, "Misspelled word in the result"),
            lambda review: _finding(review, 4, "Unbalanced environment"),
        ],
    )
    _submit(
        service,
        run.id,
        AgentRole.CONSISTENCY,
        [lambda review: _finding(review, 1, "Template class differs", category="submission_compliance")],
    )
    return run, {finding.title: finding.id for finding in service.list_findings(run.id)}


def test_findings_from_several_roles_on_shared_evidence_are_grouped_and_ordered(tmp_path):
    repo = make_repository(tmp_path, roles=_ROLES)
    with _service(repo) as service:
        run, ids = _three_role_run(service)
        service.decide_findings([ids["Template class differs"]], "waive", "Template rules come later.")
        report = service.render_report(run.id, "json")
        markdown = service.render_report(run.id, "markdown")
        fragment = service.read_report(run.id, "findings_grouped")

    grouped = report["findings_grouped"]
    substantive = ids["Typo obscures the claim"]
    shared = sorted([substantive, ids["Misspelled word in the result"]])
    assert [(group["tier"], group["finding_ids"]) for group in grouped["groups"]] == [
        ("headline", shared),
        ("other", [ids["Unbalanced environment"]]),
        ("compliance", [ids["Template class differs"]]),
    ]
    headline = grouped["groups"][0]
    assert headline["primary_finding_id"] == substantive
    assert headline["roles"] == ["copyedit", "substantive_review"]
    assert headline["max_severity"] == "major"
    assert headline["categories"] == ["clarity", "style"]
    assert headline["evidence"] == [{"source_path": "main.tex", "start_line": 3, "end_line": 3}]
    assert headline["claim"]["prominence"] == "headline" and headline["claim"]["finding_id"] == substantive
    assert headline["decision_state"] == "pending"
    assert grouped["groups"][2]["decision_state"] == "waived"
    assert grouped["counts"]["headline"] == {"groups": 1, "findings": 2}
    claim_report = report["review_claim_checks"][0]
    if "verdict" in claim_report:
        assert grouped["verdict"] == {"attempt_id": claim_report["attempt_id"], **claim_report["verdict"]}
    else:
        assert grouped["verdict"] is None

    assert json.loads(fragment["text"]) == grouped and fragment["next_command"] is None
    section = markdown.index("## Findings by group")
    assert section < markdown.index("\n## Findings\n")
    lines = [line for line in markdown[section:].split("\n## Findings\n")[0].splitlines() if line]
    assert [line for line in lines if line.startswith("### ")] == ["### Headline", "### Other", "### Compliance"]
    assert (
        f"- headline / major / copyedit, substantive_review / {', '.join(f'`{item}`' for item in shared)} — "
        "Typo obscures the claim (pending)"
    ) in lines
    assert lines[-1] == (
        f"- compliance / minor / consistency / `{ids['Template class differs']}` — Template class differs (waived)"
    )


def test_a_detail_finding_with_an_affected_claim_forms_a_supporting_group(tmp_path):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with _service(repo) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        review = claim(service, run.id, AgentRole.COPYEDIT)
        if not _accepts_affected_claim(review):
            pytest.skip("this frozen review contract has no affected_claim")
        findings = [
            _finding(review, 3, "Misread result", severity="moderate", affected_claim="The result is clear."),
            _finding(review, 4, "Unbalanced environment"),
        ]
        submit(service, review, {"summary": "Reviewed.", "findings": findings})
        grouped = service.render_report(run.id, "json")["findings_grouped"]

    assert [(group["tier"], group["titles"]) for group in grouped["groups"]] == [
        ("supporting", ["Misread result"]),
        ("other", ["Unbalanced environment"]),
    ]
    supporting = grouped["groups"][0]
    assert supporting["claim"] == {
        "text": "The result is clear.",
        "prominence": None,
        "finding_id": supporting["primary_finding_id"],
    }


def test_a_long_grouped_findings_part_is_bounded_and_traversable(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("copyedit",))
    with _service(repo) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        titles = [f"Repeated wording problem {index} " + "é" * 150 for index in range(40)]
        _submit(
            service,
            run.id,
            AgentRole.COPYEDIT,
            [lambda review, title=title: _finding(review, 3, title) for title in titles],
        )
        full = service.render_report(run.id, "json")
        content = canonical_json(full["findings_grouped"])
        assert len(content.encode()) > 2 * MAX_TOOL_RESPONSE_BYTES
        assert len(full["findings_grouped"]["groups"]) == 1
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        arguments = ["--json", "run", "report", run.id, "--part", "findings_grouped"]
        text, fragments = "", 0
        while arguments:
            assert cli.main(arguments) == 0
            raw = capsys.readouterr().out
            assert len(raw.encode()) <= MAX_TOOL_RESPONSE_BYTES
            fragment = json.loads(raw)["data"]
            assert fragment["offset"] == len(text)
            text += fragment["text"]
            fragments += 1
            arguments = shlex.split(fragment["next_command"])[1:] if fragment["next_command"] else None
        assert fragments > 2
        assert text == content
        assert json.loads(text) == full["findings_grouped"]
