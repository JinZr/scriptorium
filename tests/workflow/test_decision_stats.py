import asyncio
import json
import shlex

from scriptorium import cli
from scriptorium.domain import AgentRole, canonical_json
from scriptorium.service import ScriptoriumService
from scriptorium.tool_output import MAX_TOOL_RESPONSE_BYTES

from ._support import PdfBuildingManuscriptManager, claim, make_repository, review_finding, submit

_COUNTS = ("confirmed", "rejected", "waived", "pending")


def _service(repo):
    return ScriptoriumService(repo, manuscript_manager=PdfBuildingManuscriptManager(repo))


def _submit(service, run_id, role, specs):
    review = claim(service, run_id, role)
    findings = []
    for name, category, severity in specs:
        finding = review_finding(review)
        finding.update(
            category=category, severity=severity, title=f"Problem {name}", claim=f"Distinct claim {name} on the result."
        )
        findings.append(finding)
    submit(service, review, {"summary": "Reviewed.", "findings": findings})


def _two_role_run(service):
    run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
    _submit(
        service,
        run.id,
        AgentRole.SUBSTANTIVE_REVIEW,
        [
            ("a", "clarity", "blocker"),
            ("b", "clarity", "major"),
            ("c", "method", "major"),
            ("d", "method", "suggestion"),
            ("e", "method", "moderate"),
        ],
    )
    _submit(
        service,
        run.id,
        AgentRole.COPYEDIT,
        [("f", "style", "minor"), ("g", "style", "minor"), ("h", "clarity", "moderate")],
    )
    by_title = {finding.title.removeprefix("Problem "): finding.id for finding in service.list_findings(run.id)}
    assert sorted(by_title) == list("abcdefgh")
    return run, by_title


def _row(rows, **key):
    (row,) = [row for row in rows if all(row[name] == value for name, value in key.items())]
    return {name: row[name] for name in _COUNTS}


def _counts(confirmed=0, rejected=0, waived=0, pending=0):
    return dict(zip(_COUNTS, (confirmed, rejected, waived, pending)))


def test_decision_stats_count_each_finding_once_by_its_latest_decision(tmp_path):
    repo = make_repository(tmp_path)
    with _service(repo) as service:
        run, ids = _two_role_run(service)
        long_reason = "Not a real problem. " + "x" * 400
        service.decide_finding(ids["a"], "confirm", "Real blocker.")
        service.decide_finding(ids["b"], "reject", "First thought: wrong.")
        service.decide_finding(ids["b"], "confirm", "On reflection this holds.")
        service.decide_finding(ids["c"], "reject", long_reason)
        service.decide_finding(ids["d"], "reject", "Taste, not a defect.")
        service.decide_finding(ids["d"], "waive", "Accepted as a suggestion.")
        service.decide_finding(ids["f"], "waive", "House style differs.")
        service.decide_finding(ids["g"], "reject", "Already fixed upstream.")
        # e and h stay pending.
        assert len(service.database.list_decisions("finding", ids["b"])) == 2
        stats = service.render_report(run.id, "json")["decision_stats"]

        assert stats["totals"] == {**_counts(confirmed=2, rejected=2, waived=2, pending=2), "total": 8}
        assert [row["role"] for row in stats["by_role"]] == ["copyedit", "substantive_review"]
        assert _row(stats["by_role"], role="copyedit") == _counts(waived=1, rejected=1, pending=1)
        assert _row(stats["by_role"], role="substantive_review") == _counts(
            confirmed=2, rejected=1, waived=1, pending=1
        )
        assert [row["category"] for row in stats["by_category"]] == ["clarity", "method", "style"]
        assert _row(stats["by_category"], category="clarity") == _counts(confirmed=2, pending=1)
        assert _row(stats["by_category"], category="method") == _counts(rejected=1, waived=1, pending=1)
        assert _row(stats["by_category"], category="style") == _counts(waived=1, rejected=1)
        assert [row["severity"] for row in stats["by_severity"]] == [
            "blocker",
            "major",
            "moderate",
            "minor",
            "suggestion",
        ]
        assert _row(stats["by_severity"], severity="major") == _counts(confirmed=1, rejected=1)
        assert _row(stats["by_severity"], severity="moderate") == _counts(pending=2)
        assert _row(stats["by_severity"], severity="minor") == _counts(waived=1, rejected=1)
        assert [(row["role"], row["severity"]) for row in stats["by_role_severity"]] == [
            ("copyedit", "moderate"),
            ("copyedit", "minor"),
            ("substantive_review", "blocker"),
            ("substantive_review", "major"),
            ("substantive_review", "moderate"),
            ("substantive_review", "suggestion"),
        ]
        assert _row(stats["by_role_severity"], role="substantive_review", severity="major") == _counts(
            confirmed=1, rejected=1
        )

        # Most recent decision first; the superseded reject of d and the confirmed b do not appear.
        assert [(entry["finding_id"], entry["decision"]) for entry in stats["reasons"]] == [
            (ids["g"], "rejected"),
            (ids["f"], "waived"),
            (ids["d"], "waived"),
            (ids["c"], "rejected"),
        ]
        by_id = {entry["finding_id"]: entry for entry in stats["reasons"]}
        assert by_id[ids["c"]]["reason"] == long_reason[:200]
        assert len(by_id[ids["c"]]["reason"]) == 200
        assert by_id[ids["d"]]["reason"] == "Accepted as a suggestion."
        assert by_id[ids["g"]] == {
            "finding_id": ids["g"],
            "role": "copyedit",
            "category": "style",
            "severity": "minor",
            "decision": "rejected",
            "reason": "Already fixed upstream.",
        }
        assert service.render_report(run.id, "json")["decision_stats"] == stats

        markdown = service.render_report(run.id, "markdown")
        assert "## Decision statistics" in markdown
        assert "| substantive_review | 2 | 1 | 1 | 1 |" in markdown
        assert f"`{ids['g']}` — rejected / copyedit / style / minor: Already fixed upstream." in markdown


def test_a_run_without_decisions_reports_every_finding_pending(tmp_path):
    repo = make_repository(tmp_path)
    with _service(repo) as service:
        run, _ids = _two_role_run(service)
        stats = service.render_report(run.id, "json")["decision_stats"]
        assert stats["totals"] == {**_counts(pending=8), "total": 8}
        assert stats["reasons"] == []


def test_a_long_decision_stats_part_is_bounded_and_traversable(tmp_path, monkeypatch, capsys):
    repo = make_repository(tmp_path, roles=("substantive_review",))
    with _service(repo) as service:
        run = asyncio.run(service.start_run("HEAD", "quick"))["run"]
        _submit(
            service, run.id, AgentRole.SUBSTANTIVE_REVIEW, [(f"n{index}", "method", "major") for index in range(30)]
        )
        for index, finding in enumerate(service.list_findings(run.id)):
            service.decide_finding(finding.id, "reject", f"Reason {index}: " + "é" * 300)
        full = service.render_report(run.id, "json")
        content = canonical_json(full["decision_stats"])
        assert len(content.encode()) > 2 * MAX_TOOL_RESPONSE_BYTES
        assert len(full["decision_stats"]["reasons"]) == 30
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cli, "_build_service", lambda _: service)
        arguments = ["--json", "run", "report", run.id, "--part", "decision_stats"]
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
        assert json.loads(text) == full["decision_stats"]
