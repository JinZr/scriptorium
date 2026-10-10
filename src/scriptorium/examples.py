"""Illustrative task outputs shaped by an attempt's frozen output schema."""

from __future__ import annotations

from typing import Any

# Placeholder content per review role: category, finding title, and the affected claim it would change.
_ROLE_CONTENT = {
    "substantive_review": ("methodology", "The headline gain is not separated from the tuning budget"),
    "copyedit": ("clarity", "The result sentence can be read as the opposite comparison"),
    "consistency": ("notation", "The reported metric changes name between the text and the table"),
    "figure_review": ("figure", "The figure axis does not state the unit of the reported gain"),
}
EXAMPLE_ROLES = frozenset(_ROLE_CONTENT)
_HEADLINE = "The proposed method improves the main metric over the baseline."
_SUPPORTING = "The improvement is computed from the reported per-run values."


def build_review_example(
    schema: dict[str, Any], role: str, source_anchor: dict[str, Any] | None, source_area: dict[str, Any] | None
) -> dict[str, Any]:
    """Fill the fields a frozen review schema publishes; callers validate the result against the frozen model.

    source_anchor is an exact source-line evidence object and source_area the scope area covering its source; when
    the bundle has no anchorable text, both are None and only the compiled PDF page is cited.
    """
    page = {"source_path": "manuscript.pdf", "page": 1}
    anchor = source_anchor or page
    category, title = _ROLE_CONTENT[role]
    finding = {
        "category": category,
        "severity": "major" if role == "substantive_review" else "moderate",
        "title": title,
        "claim": "The cited passage states the result without the condition it depends on.",
        "evidence": [anchor],
        "explanation": "The cited text reports the result, but the condition that produces it is not stated there.",
        "suggested_action": "State the condition next to the result, or qualify the result.",
        "confidence": 0.7,
        "consequence": "A reader would take the result to hold without the condition it depends on.",
        "affected_claim": _HEADLINE,
    }
    claims = [(_HEADLINE, "headline"), (_SUPPORTING, "supporting")]
    checks = [
        {
            "claim_index": 0,
            "claim": _HEADLINE,
            "claim_anchor": anchor,
            "evidence": [anchor],
            "critical_question": "Does the gain remain when the baseline receives the same tuning budget?",
            "countercheck": "Compared the stated tuning budgets of the method and the baseline.",
            "stated_scope": "On the reported benchmark, at the stated model size.",
            "check_type": "design_and_analysis",
            "question_answer": "no",
            "exceptions": ["The baseline is reported without the tuning the method receives."],
            "assessment": "finding",
            "finding_indices": [0],
        },
        {
            "claim_index": 1,
            "claim": _SUPPORTING,
            "claim_anchor": anchor,
            "evidence": [page],
            "critical_question": "Does the reported mean follow from the reported per-run values?",
            "countercheck": "Recomputed the mean from the per-run values.",
            "stated_scope": "The per-run values reported for the main setting.",
            "check_type": "recomputation",
            "question_answer": "yes",
            "exceptions": [],
            "recomputation": {
                "inputs": ["Per-run values 1.0, 2.0, and 3.0, as reported on page 1."],
                "calculation": "(1.0 + 2.0 + 3.0) / 3",
                "result": "2.0 points",
                "reported": "2.0 points",
                "outcome": "matches",
            },
            "assessment": "supported",
            "finding_indices": [],
        },
    ]
    inventory = [
        {"claim": claim, "claim_anchor": anchor, "prominence": prominence, "check_indices": [index]}
        for index, (claim, prominence) in enumerate(claims)
    ]
    output = {
        "summary": "Placeholder summary: one sentence on what was reviewed and the main concern.",
        "findings": [_published(schema, "findings", finding)],
        "scope": {
            "completion": "partial",
            "checked": [source_area or page],
            "outstanding": [page] if source_area else [],
            "limitations": ["Placeholder limitation: the rendered page was not inspected."],
        },
        "claim_checks": [_published(schema, "claim_checks", check) for check in checks],
        "claim_inventory": [_published(schema, "claim_inventory", entry) for entry in inventory],
        "verdict": {
            "recommendation": "major_revision",
            "decisive_questions": ["Does the gain hold when the baseline receives the same tuning budget?"],
        },
    }
    return {key: value for key, value in output.items() if key in schema.get("properties", {})}


def _published(schema: dict[str, Any], name: str, value: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields that the frozen schema's item definition for an array property publishes."""
    reference = schema.get("properties", {}).get(name, {}).get("items", {}).get("$ref", "")
    properties = schema.get("$defs", {}).get(reference.rpartition("/")[2], {}).get("properties", {})
    return {key: item for key, item in value.items() if key in properties}
