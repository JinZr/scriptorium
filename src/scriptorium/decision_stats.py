"""Aggregate human finding decisions into calibration counts for one run."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .domain import Decision, Finding, FindingSeverity, FindingStatus

REASON_CHARS = 200
MAX_REASONS = 50
_STATES = ("confirmed", "rejected", "waived", "pending")
_STATE_BY_DECISION = {"confirm": "confirmed", "reject": "rejected", "waive": "waived"}
_DECIDED_AGAINST = frozenset({FindingStatus.REJECTED.value, FindingStatus.WAIVED.value})
_SEVERITY_ORDER = {severity.value: index for index, severity in enumerate(FindingSeverity)}
_MARKDOWN_TABLES = (
    ("By role", "by_role", ("role",)),
    ("By category", "by_category", ("category",)),
    ("By severity", "by_severity", ("severity",)),
    ("By role and severity", "by_role_severity", ("role", "severity")),
)


def _counts() -> dict[str, int]:
    return dict.fromkeys(_STATES, 0)


def _inline(value: object) -> str:
    return " ".join(str(value).split())


def current_status(finding: Finding, decisions: Sequence[Decision]) -> FindingStatus:
    """Return the status implied by the latest decision, or the stored status when none exists."""
    if not decisions:
        return finding.status
    return FindingStatus(_STATE_BY_DECISION.get(decisions[-1].decision, finding.status.value))


def _severity_key(severity: str) -> tuple[int, str]:
    return (_SEVERITY_ORDER.get(severity, len(_SEVERITY_ORDER)), severity)


def decision_stats(items: Iterable[tuple[Finding, Sequence[Decision]]]) -> dict:
    """Count findings by decision state, role, category, and severity.

    Each item pairs a finding with its decisions in append order. A later decision supersedes an earlier one, so every
    finding is counted once under the state of its latest decision, even when the finding row was read before it; a
    finding without a decision is pending. Group rows are lists
    because canonical report JSON sorts object keys and would lose the severity order.
    """
    totals = _counts()
    by_role: dict[str, dict[str, int]] = {}
    by_category: dict[str, dict[str, int]] = {}
    by_severity: dict[str, dict[str, int]] = {}
    by_role_severity: dict[tuple[str, str], dict[str, int]] = {}
    reasons = []
    for finding, decisions in items:
        state = current_status(finding, decisions).value
        role, category, severity = finding.role.value, finding.category, finding.severity.value
        totals[state] += 1
        for table, key in (
            (by_role, role),
            (by_category, category),
            (by_severity, severity),
            (by_role_severity, (role, severity)),
        ):
            table.setdefault(key, _counts())[state] += 1
        if state in _DECIDED_AGAINST:
            latest = decisions[-1]
            entry = {
                "finding_id": finding.id,
                "role": role,
                "category": category,
                "severity": severity,
                "decision": state,
                "reason": latest.reason[:REASON_CHARS],
            }
            reasons.append(((latest.created_at, latest.id), entry))
    reasons.sort(key=lambda item: item[0], reverse=True)
    return {
        "totals": {**totals, "total": sum(totals.values())},
        "by_role": [{"role": key, **by_role[key]} for key in sorted(by_role)],
        "by_category": [{"category": key, **by_category[key]} for key in sorted(by_category)],
        "by_severity": [{"severity": key, **by_severity[key]} for key in sorted(by_severity, key=_severity_key)],
        "by_role_severity": [
            {"role": role, "severity": severity, **by_role_severity[(role, severity)]}
            for role, severity in sorted(by_role_severity, key=lambda pair: (pair[0], _severity_key(pair[1])))
        ],
        "reasons": [entry for _, entry in reasons[:MAX_REASONS]],
    }


def markdown_lines(stats: dict) -> list[str]:
    """Render the section as compact markdown tables."""
    totals = stats["totals"]
    lines = [
        "",
        "## Decision statistics",
        "",
        f"Findings: {totals['total']} (confirmed {totals['confirmed']}, rejected {totals['rejected']}, "
        f"waived {totals['waived']}, pending {totals['pending']}). "
        "Calibration input for the next review, not a quality metric by itself.",
    ]
    for title, key, columns in _MARKDOWN_TABLES:
        if not stats[key]:
            continue
        lines.extend(["", f"### {title}", "", "| " + " | ".join([*columns, *_STATES]) + " |"])
        lines.append("|" + "---|" * (len(columns) + len(_STATES)))
        for row in stats[key]:
            cells = [_inline(row[column]).replace("\\", "\\\\").replace("|", "\\|") for column in columns]
            lines.append("| " + " | ".join([*cells, *(str(row[state]) for state in _STATES)]) + " |")
    if stats["reasons"]:
        lines.extend(["", "### Rejected and waived reasons", ""])
        for entry in stats["reasons"]:
            lines.append(
                f"- `{entry['finding_id']}` — {entry['decision']} / {entry['role']} / {_inline(entry['category'])} / "
                f"{entry['severity']}: {_inline(entry['reason'])}"
            )
    return lines
