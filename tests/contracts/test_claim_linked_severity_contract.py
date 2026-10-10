from decimal import Decimal

from pydantic import ValidationError
import pytest

from scriptorium.schemas import (
    SCHEMA_MODELS,
    SEVERITY_RUBRIC,
    SEVERITY_RULES,
    ClaimedFinding,
    ClaimedReviewOutput,
    LinkedScientificReviewOutput,
    RatedReviewOutput,
    RatedScientificReviewOutput,
    output_schema,
)

_PAGE = {"source_path": "manuscript.pdf", "page": 1}
_SCOPE = {"completion": "complete", "checked": [], "outstanding": [], "limitations": []}


def _finding(severity="moderate", **changes):
    finding = {
        "category": "reporting",
        "severity": severity,
        "title": "Measure is undefined",
        "claim": "The manuscript reports a result.",
        "evidence": [_PAGE],
        "explanation": "The measured outcome is not defined.",
        "suggested_action": "Define the outcome.",
        "confidence": 0.7,
        "consequence": "A reader cannot tell what the reported result measures.",
    }
    return {key: value for key, value in {**finding, **changes}.items() if value is not None}


def _check(claim_index=0, finding_indices=()):
    return {
        "claim_index": claim_index,
        "evidence": [_PAGE],
        "critical_question": "Does the result support the conclusion?",
        "countercheck": "Checked the reported result.",
        "stated_scope": "As stated in the manuscript.",
        "check_type": "design_and_analysis",
        "question_answer": "no" if finding_indices else "yes",
        "exceptions": [],
        "assessment": "finding" if finding_indices else "supported",
        "finding_indices": list(finding_indices),
    }


def _claim(claim="The method is accurate.", prominence="headline", reason=None):
    entry = {"claim": claim, "claim_anchor": _PAGE, "prominence": prominence}
    return entry if reason is None else {**entry, "not_checked_reason": reason}


def _output(*, findings=(), checks=None, inventory=None, recommendation=None, completion="complete"):
    findings = list(findings)
    high = any(finding["severity"] in {"blocker", "major"} for finding in findings)
    return {
        "summary": "Checked the central claim.",
        "findings": findings,
        "scope": {**_SCOPE, "completion": completion},
        "claim_checks": checks if checks is not None else [_check(finding_indices=range(len(findings)))],
        "claim_inventory": inventory if inventory is not None else [_claim()],
        "verdict": {
            "recommendation": recommendation or ("major_revision" if high else "minor_revision"),
            "decisive_questions": ["Does the result support the conclusion?"],
        },
    }


def test_new_schemas_link_checks_to_the_inventory_and_require_a_verdict() -> None:
    assert SCHEMA_MODELS["review"] is ClaimedReviewOutput
    assert SCHEMA_MODELS["scientific_review"] is LinkedScientificReviewOutput
    schema = output_schema("scientific_review")
    assert "verdict" in schema["required"]
    assert "claim_index" in schema["$defs"]["LinkedClaimCheck"]["required"]
    assert "check_indices" not in schema["$defs"]["LinkedClaim"]["properties"]
    finding = schema["$defs"][schema["properties"]["findings"]["items"]["$ref"].rpartition("/")[2]]
    assert "affected_claim" not in finding["properties"]
    review = output_schema("review")
    claimed = review["$defs"][review["properties"]["findings"]["items"]["$ref"].rpartition("/")[2]]
    assert "affected_claim" in claimed["properties"] and "affected_claim" not in claimed["required"]
    assert SEVERITY_RULES[2] in SEVERITY_RUBRIC and "a blocker needs a headline claim" in SEVERITY_RULES[2]


@pytest.mark.parametrize("severity", ["blocker", "major", "moderate"])
def test_findings_at_moderate_or_above_must_name_their_affected_claim(severity) -> None:
    for missing in (None, "  "):
        with pytest.raises(ValidationError, match="must name its affected_claim.*or be rated minor or suggestion"):
            ClaimedFinding.model_validate(_finding(severity, affected_claim=missing))
    named = _finding(severity, affected_claim="The reported accuracy holds for every molecule.")
    assert ClaimedFinding.model_validate(named).affected_claim.startswith("The reported")


@pytest.mark.parametrize("severity", ["minor", "suggestion"])
def test_minor_findings_need_no_affected_claim(severity) -> None:
    assert ClaimedFinding.model_validate(_finding(severity)).affected_claim is None


def test_outputs_frozen_before_affected_claim_keep_their_finding_shape() -> None:
    legacy = {"summary": "Checked.", "findings": [_finding("major")], "scope": _SCOPE}
    assert RatedReviewOutput.model_validate(legacy).findings[0].severity.value == "major"
    with pytest.raises(ValidationError, match="affected_claim"):
        ClaimedReviewOutput.model_validate(legacy)
    with pytest.raises(ValidationError, match="Extra inputs"):
        RatedReviewOutput.model_validate({**legacy, "findings": [_finding("major", affected_claim="A claim.")]})


def test_linked_output_accepts_indexed_checks_and_explained_unchecked_claims() -> None:
    output = _output(inventory=[_claim(), _claim("It transfers.", "supporting", "Outstanding page.")])
    output["claim_checks"][0]["claim_index"] = Decimal("0")
    parsed = LinkedScientificReviewOutput.model_validate(output)
    assert parsed.claim_checks[0].claim_index == 0
    assert parsed.claim_inventory[1].not_checked_reason == "Outstanding page."


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"checks": [_check(claim_index=1)]}, "unknown claim_index"),
        ({"inventory": [_claim(reason="Also unchecked.")]}, "not_checked_reason exactly when no claim check"),
        ({"inventory": [_claim(), _claim("It transfers.", "supporting")]}, "not_checked_reason exactly when"),
        (
            {"inventory": [_claim(), _claim("It scales.", reason="Not reached.")]},
            "must check every inventoried headline claim",
        ),
        (
            {
                "findings": [_finding("blocker")],
                "inventory": [_claim(prominence="supporting")],
            },
            "a blocker finding needs a headline claim",
        ),
        ({"findings": [_finding("major")], "recommendation": "accept"}, "accept verdict cannot be submitted"),
    ],
)
def test_linked_output_rejects_broken_links_and_unsupported_severity(changes, message) -> None:
    with pytest.raises(ValidationError, match=message):
        LinkedScientificReviewOutput.model_validate(_output(**changes))


def test_blocker_linked_from_a_headline_check_is_accepted() -> None:
    parsed = LinkedScientificReviewOutput.model_validate(_output(findings=[_finding("blocker")]))
    assert parsed.verdict.recommendation == "major_revision"


@pytest.mark.parametrize(
    "verdict",
    [
        {"recommendation": "accept", "decisive_questions": []},
        {"recommendation": "accept", "decisive_questions": ["One?", "Two?", "Three?", "Four?"]},
        {"recommendation": "accept", "decisive_questions": [""]},
        {"recommendation": "weak_accept", "decisive_questions": ["One?"]},
    ],
)
def test_verdict_has_a_known_recommendation_and_one_to_three_questions(verdict) -> None:
    with pytest.raises(ValidationError):
        LinkedScientificReviewOutput.model_validate({**_output(), "verdict": verdict})


def test_outputs_frozen_before_the_verdict_keep_restated_claim_checks() -> None:
    check = {key: value for key, value in _check().items() if key != "claim_index"}
    legacy = {
        "summary": "Checked the central claim.",
        "findings": [],
        "scope": _SCOPE,
        "claim_checks": [{**check, "claim": "The method is accurate.", "claim_anchor": _PAGE}],
        "claim_inventory": [{**_claim(), "check_indices": [0]}],
    }
    assert RatedScientificReviewOutput.model_validate(legacy).claim_inventory[0].check_indices == [0]
    with pytest.raises(ValidationError):
        LinkedScientificReviewOutput.model_validate(legacy)
