from scriptorium.decision_stats import MAX_REASONS, REASON_CHARS, decision_stats, markdown_lines
from scriptorium.domain import AgentRole, Decision, Finding, FindingSeverity, FindingStatus

_STATUS_BY_DECISION = {
    "confirm": FindingStatus.CONFIRMED,
    "reject": FindingStatus.REJECTED,
    "waive": FindingStatus.WAIVED,
}


def _finding(index, role=AgentRole.SUBSTANTIVE_REVIEW, severity=FindingSeverity.MAJOR, category="clarity"):
    return Finding(
        run_id="run",
        task_id="task",
        attempt_id="attempt",
        fingerprint=f"fingerprint-{index}",
        role=role,
        category=category,
        severity=severity,
        title=f"Finding {index}",
        claim="Claim.",
        evidence=(),
        explanation="Explanation.",
        suggested_action="Act.",
        confidence=0.5,
        id=f"finding_{index:03d}",
    )


def _decided(finding, *decisions):
    records = [
        Decision("finding", finding.id, decision, reason, id=f"decision_{finding.id}_{n}", created_at=at)
        for n, (decision, reason, at) in enumerate(decisions)
    ]
    status = _STATUS_BY_DECISION[decisions[-1][0]]
    return (Finding(**{**finding.__dict__, "status": status}), records)


def test_an_empty_run_has_zero_totals_and_no_rows():
    stats = decision_stats([])
    assert stats["totals"] == {"confirmed": 0, "rejected": 0, "waived": 0, "pending": 0, "total": 0}
    assert stats["by_role"] == stats["by_category"] == stats["by_severity"] == stats["by_role_severity"] == []
    assert stats["reasons"] == []


def test_reasons_are_capped_to_the_most_recent_decisions():
    items = [
        _decided(_finding(index), ("reject", f"Reason {index}.", f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}"))
        for index in range(MAX_REASONS + 5)
    ]
    stats = decision_stats(items)
    assert stats["totals"]["rejected"] == MAX_REASONS + 5
    assert len(stats["reasons"]) == MAX_REASONS
    assert stats["reasons"][0]["finding_id"] == f"finding_{MAX_REASONS + 4:03d}"
    assert stats["reasons"][-1]["finding_id"] == "finding_005"


def test_markdown_escapes_table_cells_and_flattens_reasons():
    finding = _finding(1, category="a|b")
    stats = decision_stats([_decided(finding, ("reject", "Line one.\nLine two " + "x" * REASON_CHARS, "2026-01-01"))])
    text = "\n".join(markdown_lines(stats))
    assert "| a\\|b | 0 | 1 | 0 | 0 |" in text
    assert "Line one. Line two" in text


def test_markdown_keeps_line_separators_in_one_table_row_and_bullet():
    finding = _finding(1, category="line one\r\n## heading\u2028line two\rend")
    stats = decision_stats([_decided(finding, ("reject", "Why\n## no\r\nsplit\x85here.", "2026-01-01"))])
    lines = markdown_lines(stats)
    assert "| line one ## heading line two end | 0 | 1 | 0 | 0 |" in lines
    assert any(line.endswith("/ line one ## heading line two end / major: Why ## no split here.") for line in lines)
    assert not any(line.startswith("## heading") or line.startswith("## no") for line in lines)
