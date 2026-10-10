from scriptorium.domain import AgentRole, Finding, FindingSeverity, FindingStatus
from scriptorium.finding_groups import finding_groups, markdown_lines

_DIGEST = "a" * 64


def _lines(path, start, end=None):
    return {
        "source_path": path,
        "start_line": start,
        "end_line": start if end is None else end,
        "source_digest": _DIGEST,
        "quoted_text": "Text.",
    }


def _page(page, path="manuscript.pdf"):
    return {"source_path": path, "page": page}


def _finding(
    name,
    *evidence,
    role=AgentRole.COPYEDIT,
    severity=FindingSeverity.MINOR,
    category="clarity",
    status=FindingStatus.PENDING,
    created_at="2026-10-10T00:00:00Z",
    task_id=None,
    affected_claim=None,
):
    return Finding(
        run_id="run",
        task_id=task_id or f"task_{role.value}",
        attempt_id=f"attempt_{role.value}",
        fingerprint=f"fingerprint_{name}",
        role=role,
        category=category,
        severity=severity,
        title=f"Title {name}",
        claim=f"Claim {name}.",
        evidence=tuple(evidence),
        explanation="Explanation.",
        suggested_action="Act.",
        confidence=0.5,
        consequence=f"Consequence {name}.",
        id=f"finding_{name}",
        status=status,
        created_at=created_at,
        affected_claim=affected_claim,
    )


def _submitted(finding, **extra):
    return {
        "category": finding.category,
        "severity": finding.severity.value,
        "title": finding.title,
        "claim": finding.claim,
        "evidence": [dict(anchor) for anchor in finding.evidence],
        **extra,
    }


def _ids(grouped):
    return [group["finding_ids"] for group in grouped["groups"]]


def test_line_ranges_share_a_group_only_when_half_the_shorter_range_overlaps():
    findings = [
        _finding("a", _lines("main.tex", 3, 6)),
        _finding("b", _lines("main.tex", 5, 7)),
        _finding("c", _lines("main.tex", 7, 10)),
        _finding("d", _lines("other.tex", 3, 6)),
        _finding("e", _lines("main.tex", 20, 60)),
        _finding("f", _lines("main.tex", 40)),
    ]
    grouped = finding_groups(findings, [])

    # b covers two of its three lines with a; c touches b on one line of three; a 41-line table holds f.
    assert sorted(_ids(grouped)) == [
        ["finding_a", "finding_b"],
        ["finding_c"],
        ["finding_d"],
        ["finding_e", "finding_f"],
    ]
    shared = next(group for group in grouped["groups"] if group["primary_finding_id"] == "finding_a")
    assert shared["evidence"] == [{"source_path": "main.tex", "start_line": 3, "end_line": 7}]


def test_findings_share_a_group_only_when_most_anchors_of_one_overlap_the_other():
    findings = [
        _finding("a", _lines("main.tex", 1), _lines("main.tex", 10), _lines("main.tex", 20)),
        _finding("b", _lines("main.tex", 1), _lines("main.tex", 10), _lines("main.tex", 30)),
        _finding("c", _lines("main.tex", 1), _lines("main.tex", 40)),
        _finding("d", _lines("main.tex", 20), _lines("main.tex", 20)),
    ]
    grouped = finding_groups(findings, [])

    # b shares two of three anchors, c only a shared opening line, and d repeats one anchor a cites.
    assert sorted(_ids(grouped)) == [["finding_a", "finding_b", "finding_d"], ["finding_c"]]


def test_a_group_never_chains_through_a_finding_that_bridges_two_others():
    findings = [
        _finding("a", _lines("main.tex", 1, 2), severity=FindingSeverity.MAJOR),
        _finding("b", _lines("intro.tex", 10, 12), severity=FindingSeverity.MODERATE),
        _finding("c", _lines("main.tex", 2), _lines("intro.tex", 11)),
    ]
    grouped = finding_groups(findings, [])

    # c shares evidence with both a and b, but joins only the group whose primary comes first.
    assert sorted(_ids(grouped)) == [["finding_a", "finding_c"], ["finding_b"]]
    bridged = next(group for group in grouped["groups"] if group["primary_finding_id"] == "finding_a")
    assert bridged["evidence"] == [
        {"source_path": "intro.tex", "start_line": 11, "end_line": 11},
        {"source_path": "main.tex", "start_line": 1, "end_line": 2},
    ]


def test_a_shared_pdf_page_groups_only_page_only_findings_linked_to_one_claim():
    figure = "Figure 2 shows the gain."
    findings = [
        _finding("a", _page(2), role=AgentRole.FIGURE_REVIEW, affected_claim=figure),
        _finding("b", _page(2), _page(3), role=AgentRole.CONSISTENCY, affected_claim=figure),
        _finding("c", _page(2), affected_claim="Another claim."),
        _finding("d", _page(2)),
        _finding("e", _page(2), _lines("main.tex", 4), affected_claim=figure),
        _finding("f", _lines("manuscript.pdf", 2), affected_claim=figure),
    ]
    grouped = finding_groups(findings, [])

    assert sorted(_ids(grouped)) == [
        ["finding_a", "finding_b"],
        ["finding_c"],
        ["finding_d"],
        ["finding_e"],
        ["finding_f"],
    ]
    paged = next(group for group in grouped["groups"] if len(group["finding_ids"]) == 2)
    assert paged["evidence"] == [_page(2), _page(3)]
    assert paged["roles"] == ["consistency", "figure_review"]


def test_the_primary_is_the_most_severe_then_substantive_then_earliest():
    findings = [
        _finding("a", _lines("main.tex", 3), severity=FindingSeverity.MAJOR, created_at="2026-01-01"),
        _finding("b", _lines("main.tex", 3), severity=FindingSeverity.MAJOR, role=AgentRole.SUBSTANTIVE_REVIEW),
        _finding("c", _lines("main.tex", 3), severity=FindingSeverity.MINOR, role=AgentRole.SUBSTANTIVE_REVIEW),
        _finding("d", _lines("main.tex", 3), severity=FindingSeverity.MAJOR, created_at="2025-01-01"),
    ]
    (group,) = finding_groups(findings, [])["groups"]
    assert group["primary_finding_id"] == "finding_b"
    assert group["max_severity"] == "major"
    assert group["consequence"] == "Consequence b."
    assert group["titles"] == ["Title a", "Title b", "Title c", "Title d"]

    without_substantive = [finding for finding in findings if finding.id != "finding_b"]
    assert finding_groups(without_substantive, [])["groups"][0]["primary_finding_id"] == "finding_d"


def test_the_group_id_depends_only_on_the_member_ids():
    findings = [_finding("a", _lines("main.tex", 3)), _finding("b", _lines("main.tex", 3))]
    first = finding_groups(findings, [])["groups"][0]["group_id"]
    assert first == finding_groups(list(reversed(findings)), [])["groups"][0]["group_id"]
    assert first.startswith("group_") and len(first) == len("group_") + 16


def _linked_claim_report(task_id, submitted, prominences):
    """A post-claim_index substantive output: one check per inventoried claim, each linking one finding."""
    return {
        "task_id": task_id,
        "attempt_id": "attempt_substantive",
        "submitted_findings": submitted,
        "claim_checks": [
            {"claim_index": index, "assessment": "finding", "finding_indices": [index]}
            for index in range(len(prominences))
        ],
        "claim_inventory": [
            {"claim": f"Claim number {index}.", "prominence": prominence}
            for index, prominence in enumerate(prominences)
        ],
        "verdict": {"recommendation": "major_revision", "decisive_questions": ["Is the effect real?"]},
    }


def _tiered_findings():
    headline = _finding("h", _lines("main.tex", 10), role=AgentRole.SUBSTANTIVE_REVIEW, severity=FindingSeverity.MAJOR)
    supporting = _finding(
        "s", _lines("main.tex", 20), role=AgentRole.SUBSTANTIVE_REVIEW, severity=FindingSeverity.MODERATE
    )
    affected = _finding("x", _lines("main.tex", 30), severity=FindingSeverity.MAJOR, affected_claim="Table 2 holds.")
    other_major = _finding("o", _lines("main.tex", 40), severity=FindingSeverity.MAJOR)
    other_minor = _finding("m", _lines("main.tex", 50))
    compliance = _finding("c", _page(9), category="submission_compliance", role=AgentRole.CONSISTENCY)
    return headline, supporting, affected, other_major, other_minor, compliance


def test_tiers_follow_claim_links_and_order_groups_for_triage():
    headline, supporting, affected, other_major, other_minor, compliance = _tiered_findings()
    findings = [compliance, other_minor, other_major, affected, supporting, headline]
    claim_report = _linked_claim_report(
        headline.task_id, [_submitted(headline), _submitted(supporting)], ["headline", "supporting"]
    )
    grouped = finding_groups(findings, [claim_report])

    assert [(group["tier"], group["primary_finding_id"]) for group in grouped["groups"]] == [
        ("headline", "finding_h"),
        ("supporting", "finding_x"),
        ("supporting", "finding_s"),
        ("other", "finding_o"),
        ("other", "finding_m"),
        ("compliance", "finding_c"),
    ]
    claims = {group["primary_finding_id"]: group["claim"] for group in grouped["groups"]}
    assert claims["finding_h"] == {"text": "Claim number 0.", "prominence": "headline", "finding_id": "finding_h"}
    assert claims["finding_s"]["prominence"] == "supporting"
    assert claims["finding_x"] == {"text": "Table 2 holds.", "prominence": None, "finding_id": "finding_x"}
    assert claims["finding_o"] is None and claims["finding_c"] is None
    assert grouped["verdict"] == {
        "attempt_id": "attempt_substantive",
        "recommendation": "major_revision",
        "decisive_questions": ["Is the effect real?"],
    }
    assert grouped["counts"] == {
        "headline": {"groups": 1, "findings": 1},
        "supporting": {"groups": 2, "findings": 2},
        "other": {"groups": 2, "findings": 2},
        "compliance": {"groups": 1, "findings": 1},
    }


def test_a_headline_link_lifts_the_whole_group_and_mixed_compliance_is_not_compliance():
    headline = _finding("h", _lines("main.tex", 3), role=AgentRole.SUBSTANTIVE_REVIEW)
    detail = _finding("d", _lines("main.tex", 3), severity=FindingSeverity.MAJOR, category="submission_compliance")
    mixed = [
        _finding("p", _lines("main.tex", 9), category="submission_compliance"),
        _finding("q", _lines("main.tex", 9), category="style"),
    ]
    claim_report = _linked_claim_report(headline.task_id, [_submitted(headline)], ["headline"])
    grouped = finding_groups([detail, headline, *mixed], [claim_report])

    tiers = {tuple(group["finding_ids"]): group["tier"] for group in grouped["groups"]}
    assert tiers == {("finding_d", "finding_h"): "headline", ("finding_p", "finding_q"): "other"}
    lifted = grouped["groups"][0]
    assert lifted["primary_finding_id"] == "finding_d"
    assert lifted["claim"]["finding_id"] == "finding_h"
    assert lifted["categories"] == ["clarity", "submission_compliance"]


def test_outputs_frozen_before_claim_index_link_through_inventory_check_indices():
    finding = _finding("h", _lines("main.tex", 3), role=AgentRole.SUBSTANTIVE_REVIEW)
    restated = {
        "task_id": finding.task_id,
        "attempt_id": "attempt_old",
        "submitted_findings": [_submitted(finding)],
        "claim_checks": [{"claim": "Restated claim.", "assessment": "finding", "finding_indices": [0]}],
    }
    inventoried = {
        **restated,
        "claim_inventory": [{"claim": "Restated claim.", "prominence": "supporting", "check_indices": [0]}],
    }

    without_inventory = finding_groups([finding], [restated])
    assert without_inventory["groups"][0]["claim"] is None
    assert without_inventory["groups"][0]["tier"] == "other"
    assert without_inventory["verdict"] is None
    with_inventory = finding_groups([finding], [inventoried])["groups"][0]
    assert with_inventory["claim"] == {"text": "Restated claim.", "prominence": "supporting", "finding_id": "finding_h"}
    assert with_inventory["tier"] == "supporting"


def test_a_claim_check_from_another_task_does_not_link():
    finding = _finding("x", _lines("main.tex", 3), role=AgentRole.SUBSTANTIVE_REVIEW)
    claim_report = _linked_claim_report("task_elsewhere", [_submitted(finding)], ["headline"])
    assert finding_groups([finding], [claim_report])["groups"][0]["claim"] is None


def test_members_expose_each_status_and_only_undecided_ids_are_pending():
    partly = [
        _finding("a", _lines("main.tex", 3), status=FindingStatus.CONFIRMED, severity=FindingSeverity.MAJOR),
        _finding("b", _lines("main.tex", 3), role=AgentRole.CONSISTENCY),
    ]
    (group,) = finding_groups(partly, [])["groups"]
    assert group["decision_state"] == "mixed"
    assert group["pending_finding_ids"] == ["finding_b"]
    assert group["members"] == [
        {"finding_id": "finding_a", "role": "copyedit", "severity": "major", "status": "confirmed"},
        {"finding_id": "finding_b", "role": "consistency", "severity": "minor", "status": "pending"},
    ]
    assert markdown_lines(finding_groups(partly, []))[-1].endswith("— Title a (mixed; 1 of 2 pending)")

    decided = [
        _finding("a", _lines("main.tex", 3), status=FindingStatus.WAIVED),
        _finding("b", _lines("main.tex", 3), status=FindingStatus.CONFIRMED),
    ]
    (group,) = finding_groups(decided, [])["groups"]
    assert (group["decision_state"], group["pending_finding_ids"]) == ("mixed", [])
    assert markdown_lines(finding_groups(decided, []))[-1].endswith("(mixed)")
    same = [_finding(name, _lines("main.tex", 3), status=FindingStatus.REJECTED) for name in "ab"]
    assert finding_groups(same, [])["groups"][0]["decision_state"] == "rejected"
    undecided = [_finding(name, _lines("main.tex", 3)) for name in "ab"]
    (group,) = finding_groups(undecided, [])["groups"]
    assert (group["decision_state"], group["pending_finding_ids"]) == ("pending", ["finding_a", "finding_b"])


def test_a_duplicate_report_adds_its_role_and_its_claim_link():
    # A detail role stored the finding first; the substantive review later submitted the identical finding.
    stored = _finding("d", _lines("main.tex", 3), severity=FindingSeverity.MAJOR, affected_claim="A detail claim.")
    claim_report = _linked_claim_report("task_substantive_review", [_submitted(stored)], ["headline"])
    duplicate = {
        "finding_id": stored.id,
        "task_id": "task_substantive_review",
        "attempt_id": "attempt_substantive",
        "role": "substantive_review",
    }

    (unattributed,) = finding_groups([stored], [claim_report])["groups"]
    assert (unattributed["tier"], unattributed["roles"]) == ("supporting", ["copyedit"])
    (group,) = finding_groups([stored], [claim_report], [duplicate])["groups"]
    assert group["roles"] == ["copyedit", "substantive_review"]
    assert group["tier"] == "headline"
    assert group["claim"] == {"text": "Claim number 0.", "prominence": "headline", "finding_id": stored.id}
    assert group["members"] == [{"finding_id": stored.id, "role": "copyedit", "severity": "major", "status": "pending"}]


def test_duplicate_events_without_attribution_fields_are_ignored():
    stored = _finding("d", _lines("main.tex", 3))
    (group,) = finding_groups([stored], [], [{"finding_id": stored.id}, {"finding_id": "finding_gone", "role": "x"}])[
        "groups"
    ]
    assert group["roles"] == ["copyedit"]


def test_markdown_lists_the_verdict_first_and_compliance_last():
    headline, supporting, affected, other_major, other_minor, compliance = _tiered_findings()
    claim_report = _linked_claim_report(headline.task_id, [_submitted(headline)], ["headline"])
    grouped = finding_groups([compliance, other_major, headline], [claim_report])
    lines = markdown_lines(grouped)

    assert lines[1:4] == [
        "## Findings by group",
        "",
        "- Verdict: major_revision; decisive questions: Is the effect real?",
    ]
    headings = [line for line in lines if line.startswith("### ")]
    assert headings == ["### Headline", "### Other", "### Compliance"]
    assert lines[-1] == "- compliance / minor / consistency / `finding_c` — Title c (pending)"
    assert "- headline / major / substantive_review / `finding_h` — Title h (pending)" in lines


def test_an_empty_run_has_no_groups_and_no_verdict():
    grouped = finding_groups([], [])
    assert grouped["groups"] == [] and grouped["verdict"] is None
    assert all(count == {"groups": 0, "findings": 0} for count in grouped["counts"].values())
    assert markdown_lines(grouped)[3:] == ["- Verdict: none recorded", "- No findings"]
