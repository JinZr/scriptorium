"""Group a run's findings by shared evidence so a host can triage them a group at a time."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .domain import AgentRole, Finding, FindingSeverity, canonical_json, digest_json

TIERS = ("headline", "supporting", "other", "compliance")
COMPLIANCE_CATEGORY = "submission_compliance"
_TIER_HEADINGS = {"headline": "Headline", "supporting": "Supporting", "other": "Other", "compliance": "Compliance"}
_SEVERITY_ORDER = {severity.value: index for index, severity in enumerate(FindingSeverity)}
# An inventoried claim outranks a detail role's affected_claim, which carries no prominence.
_PROMINENCE_ORDER = {"headline": 0, "supporting": 1, None: 2}


def _inline(value: object) -> str:
    return " ".join(str(value).split())


def _identity(task_id: str, category: str, severity: str, title: str, claim: str, evidence: Iterable) -> tuple:
    """Key a finding the way the workflow matches a submitted finding to its stored row."""
    return (task_id, category, severity, title, claim, tuple(sorted(canonical_json(dict(item)) for item in evidence)))


def _submitted_identity(task_id: str, finding: Mapping[str, Any]) -> tuple:
    fields = (finding["category"], finding["severity"], finding["title"], finding["claim"], finding["evidence"])
    return _identity(task_id, *fields)


def _claim_checks(entry: Mapping[str, Any]) -> Iterable[tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    """Yield each claim check with its inventoried claim, or None when the output has no inventory link."""
    inventory = entry.get("claim_inventory", [])
    for index, check in enumerate(entry["claim_checks"]):
        if "claim_index" in check:
            yield check, inventory[check["claim_index"]]
        else:
            # Outputs frozen before claim_index restate the claim; an inventory, if any, lists its checks.
            yield check, next((claim for claim in inventory if index in claim.get("check_indices", ())), None)


def linked_claims(
    findings: Sequence[Finding],
    claim_reports: Iterable[Mapping[str, Any]],
    duplicates: Iterable[Mapping[str, Any]] = (),
) -> dict[str, dict[str, Any]]:
    """Return, by finding ID, the most prominent claim a stored finding is linked to.

    Substantive findings link through their claim check to an inventoried claim; detail-role findings through
    their stored affected_claim. Findings and outputs recorded before either link yield none. A submission that
    repeated a finding stored under another task resolves to that row through its duplicate attribution.
    """
    by_id = {item.id: item for item in findings}
    reporters = [(item.task_id, item) for item in findings] + [
        (duplicate["task_id"], by_id[duplicate["finding_id"]])
        for duplicate in duplicates
        if duplicate["finding_id"] in by_id and duplicate.get("task_id")
    ]
    stored = {
        _identity(task_id, item.category, item.severity.value, item.title, item.claim, item.evidence): item.id
        for task_id, item in reporters
    }
    linked: dict[str, dict[str, Any]] = {}

    def link(finding_id: str | None, text: str, prominence: str | None) -> None:
        if finding_id is None:
            return
        current = linked.get(finding_id)
        if current is None or _PROMINENCE_ORDER[prominence] < _PROMINENCE_ORDER[current["prominence"]]:
            linked[finding_id] = {"text": text, "prominence": prominence}

    for entry in claim_reports:
        for check, claim in _claim_checks(entry):
            for index in check["finding_indices"] if claim is not None else ():
                submitted = _submitted_identity(entry["task_id"], entry["submitted_findings"][index])
                link(stored.get(submitted), claim["claim"], claim["prominence"])
    for finding in findings:
        if finding.affected_claim:
            link(finding.id, finding.affected_claim, None)
    return linked


def _anchors(finding: Finding) -> Iterable[tuple[str, str, int, int]]:
    """Yield ("lines", path, start, end) or ("page", path, page, page) for each usable evidence anchor."""
    for anchor in finding.evidence:
        if anchor.get("page") is not None:
            yield "page", anchor["source_path"], anchor["page"], anchor["page"]
        elif anchor.get("start_line") is not None and anchor.get("end_line") is not None:
            yield "lines", anchor["source_path"], anchor["start_line"], anchor["end_line"]


def _components(findings: Sequence[Finding]) -> list[list[Finding]]:
    """Union findings whose line anchors overlap on one source path or whose PDF anchors name one page."""
    parent = {finding.id: finding.id for finding in findings}

    def root(finding_id: str) -> str:
        while parent[finding_id] != finding_id:
            parent[finding_id] = parent[parent[finding_id]]
            finding_id = parent[finding_id]
        return finding_id

    def union(first: str, second: str) -> None:
        first, second = root(first), root(second)
        if first != second:
            parent[max(first, second)] = min(first, second)

    spans: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
    for finding in findings:
        for kind, path, start, end in _anchors(finding):
            spans.setdefault((kind, path), []).append((start, end, finding.id))
    for intervals in spans.values():
        reach, owner = 0, None
        for start, end, finding_id in sorted(intervals):
            if owner is not None and start <= reach:
                union(finding_id, owner)
                reach = max(reach, end)
            else:
                reach, owner = end, finding_id
    members: dict[str, list[Finding]] = {}
    for finding in findings:
        members.setdefault(root(finding.id), []).append(finding)
    return list(members.values())


def _merged_evidence(members: Sequence[Finding]) -> list[dict[str, Any]]:
    """Merge the members' overlapping line spans per path and list each distinct PDF page once."""
    spans: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for finding in members:
        for kind, path, start, end in _anchors(finding):
            spans.setdefault((path, kind), []).append((start, end))
    merged: list[dict[str, Any]] = []
    for (path, kind), intervals in sorted(spans.items()):
        closed: list[list[int]] = []
        for start, end in sorted(intervals):
            if closed and start <= closed[-1][1]:
                closed[-1][1] = max(closed[-1][1], end)
            else:
                closed.append([start, end])
        for start, end in closed:
            location = {"page": start} if kind == "page" else {"start_line": start, "end_line": end}
            merged.append({"source_path": path, **location})
    return merged


def _primary_key(finding: Finding) -> tuple:
    return (
        _SEVERITY_ORDER[finding.severity.value],
        finding.role != AgentRole.SUBSTANTIVE_REVIEW,
        finding.created_at,
        finding.id,
    )


def _tier(categories: Sequence[str], claims: Sequence[Mapping[str, Any]]) -> str:
    if any(claim["prominence"] == "headline" for claim in claims):
        return "headline"
    if claims:
        return "supporting"
    return "compliance" if list(categories) == [COMPLIANCE_CATEGORY] else "other"


def _decision_state(members: Sequence[Finding]) -> str:
    """Return the members' shared current status, or mixed when they differ."""
    states = {finding.status.value for finding in members}
    return states.pop() if len(states) == 1 else "mixed"


def _group(
    members: Sequence[Finding], claims: Mapping[str, dict[str, Any]], duplicate_roles: Mapping[str, set[str]]
) -> dict[str, Any]:
    ordered = sorted(members, key=_primary_key)
    primary = ordered[0]
    # Members are in primary order and min keeps the first of equals, so the primary-most link wins ties.
    linked = [{**claims[finding.id], "finding_id": finding.id} for finding in ordered if finding.id in claims]
    claim = min(linked, key=lambda item: _PROMINENCE_ORDER[item["prominence"]], default=None)
    categories = sorted({finding.category for finding in members})
    finding_ids = sorted(finding.id for finding in members)
    by_id = {finding.id: finding for finding in members}
    return {
        "group_id": f"group_{digest_json(finding_ids)[:16]}",
        "primary_finding_id": primary.id,
        "finding_ids": finding_ids,
        # A role whose identical finding was stored under another role's row still reported it.
        "roles": sorted(
            {finding.role.value for finding in members}.union(
                *(duplicate_roles.get(finding.id, set()) for finding in members)
            )
        ),
        "max_severity": primary.severity.value,
        "categories": categories,
        "evidence": _merged_evidence(members),
        "claim": claim,
        "consequence": primary.consequence,
        "tier": _tier(categories, linked),
        "decision_state": _decision_state(members),
        "pending_finding_ids": sorted(finding.id for finding in members if finding.status.value == "pending"),
        "members": [
            {
                "finding_id": finding_id,
                "role": by_id[finding_id].role.value,
                "severity": by_id[finding_id].severity.value,
                "status": by_id[finding_id].status.value,
            }
            for finding_id in finding_ids
        ],
        "titles": [by_id[finding_id].title for finding_id in finding_ids],
    }


def _verdict(claim_reports: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Return the verdict of the latest substantive output that recorded one."""
    latest = None
    for entry in claim_reports:
        if entry.get("verdict") is not None:
            latest = {"attempt_id": entry["attempt_id"], **entry["verdict"]}
    return latest


def _group_order(group: Mapping[str, Any]) -> tuple:
    return (TIERS.index(group["tier"]), _SEVERITY_ORDER[group["max_severity"]], group["primary_finding_id"])


def finding_groups(
    findings: Sequence[Finding],
    claim_reports: Sequence[Mapping[str, Any]],
    duplicates: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Group findings across roles and attempts by overlapping evidence and order the groups for triage.

    `findings` carry the status their latest decision implies; `claim_reports` are the report's substantive
    claim-check entries; `duplicates` attribute a stored finding to each later task (`finding_id`, `task_id`,
    `role`) that submitted it again.
    """
    claims = linked_claims(findings, claim_reports, duplicates)
    duplicate_roles: dict[str, set[str]] = {}
    for duplicate in duplicates:
        if duplicate.get("role"):
            duplicate_roles.setdefault(duplicate["finding_id"], set()).add(duplicate["role"])
    groups = sorted((_group(members, claims, duplicate_roles) for members in _components(findings)), key=_group_order)
    counts = {
        tier: {
            "groups": sum(1 for group in groups if group["tier"] == tier),
            "findings": sum(len(group["finding_ids"]) for group in groups if group["tier"] == tier),
        }
        for tier in TIERS
    }
    return {"verdict": _verdict(claim_reports), "counts": counts, "groups": groups}


def markdown_lines(grouped: Mapping[str, Any]) -> list[str]:
    """Render the grouped findings as one line per group, verdict first and compliance last."""
    lines = ["", "## Findings by group", ""]
    verdict = grouped["verdict"]
    if verdict is None:
        lines.append("- Verdict: none recorded")
    else:
        questions = " | ".join(_inline(question) for question in verdict["decisive_questions"])
        lines.append(f"- Verdict: {verdict['recommendation']}; decisive questions: {questions}")
    if not grouped["groups"]:
        lines.append("- No findings")
    for tier in TIERS:
        members = [group for group in grouped["groups"] if group["tier"] == tier]
        if members:
            lines.extend(["", f"### {_TIER_HEADINGS[tier]}", ""])
        for group in members:
            ids = ", ".join(f"`{finding_id}`" for finding_id in group["finding_ids"])
            primary_title = group["titles"][group["finding_ids"].index(group["primary_finding_id"])]
            state = group["decision_state"]
            if state == "mixed" and group["pending_finding_ids"]:
                state = f"mixed; {len(group['pending_finding_ids'])} of {len(group['finding_ids'])} pending"
            lines.append(
                f"- {group['tier']} / {group['max_severity']} / {', '.join(group['roles'])} / {ids} — "
                f"{_inline(primary_title)} ({state})"
            )
    return lines
