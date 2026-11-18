from pydantic import ValidationError
import pytest

from scriptorium.schemas import (
    SCHEMA_MODELS,
    SEVERITY_DEFINITIONS,
    SEVERITY_RUBRIC,
    SEVERITY_RULES,
    RatedFinding,
    Severity,
    output_schema,
)


def _finding(**changes):
    finding = {
        "category": "reporting",
        "severity": "moderate",
        "title": "Measure is undefined",
        "claim": "The manuscript reports a result.",
        "evidence": [{"source_path": "manuscript.pdf", "page": 1}],
        "explanation": "The measured outcome is not defined.",
        "suggested_action": "Define the outcome.",
        "confidence": 0.7,
        "consequence": "A reader cannot tell what the reported result measures.",
    }
    return {key: value for key, value in {**finding, **changes}.items() if value is not None}


def _errors(model, value):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(value)
    return caught.value.errors(include_url=False)


@pytest.mark.parametrize("kind", ["review", "scientific_review"])
def test_new_review_schemas_require_a_consequence_and_publish_the_rubric(kind) -> None:
    schema = output_schema(kind)
    reference = schema["properties"]["findings"]["items"]["$ref"].rpartition("/")[2]
    finding = schema["$defs"][reference]

    assert issubclass(SCHEMA_MODELS[kind].model_fields["findings"].annotation.__args__[0], RatedFinding)
    assert "consequence" in finding["required"]
    assert "wrongly believe" in finding["properties"]["consequence"]["description"]
    severity = finding["properties"]["severity"]["description"]
    for level, definition in SEVERITY_DEFINITIONS.items():
        assert f"{level.value}: {definition}" in severity
        assert f"- {level.value}: {definition}" in SEVERITY_RUBRIC
    assert all(rule in severity and rule in SEVERITY_RUBRIC for rule in SEVERITY_RULES)
    assert "submission_compliance" in finding["properties"]["category"]["description"]
    assert len(SEVERITY_RUBRIC.split()) < 290


def test_rated_finding_rejects_a_missing_or_blank_consequence() -> None:
    assert RatedFinding.model_validate(_finding()).consequence.startswith("A reader")
    assert [error["type"] for error in _errors(RatedFinding, _finding(consequence=None))] == ["missing"]
    assert [error["type"] for error in _errors(RatedFinding, _finding(consequence=""))] == ["string_too_short"]
    blank = _errors(RatedFinding, _finding(consequence="  "))
    assert "consequence must state what a reader would wrongly believe" in blank[0]["msg"]


@pytest.mark.parametrize("severity", ["blocker", "major", "moderate"])
def test_submission_compliance_findings_cannot_exceed_minor(severity) -> None:
    errors = _errors(RatedFinding, _finding(category="submission_compliance", severity=severity))

    assert errors[0]["type"] == "value_error"
    assert "submission_compliance finding must be minor or suggestion" in errors[0]["msg"]
    assert "does not change how any claim should be read" in errors[0]["msg"]


@pytest.mark.parametrize("severity", [Severity.MINOR, Severity.SUGGESTION])
def test_submission_compliance_findings_accept_minor_and_suggestion(severity) -> None:
    finding = RatedFinding.model_validate(_finding(category="submission_compliance", severity=severity.value))
    assert finding.severity == severity
    assert RatedFinding.model_validate(_finding(category="formatting", severity="major")).severity == Severity.MAJOR
