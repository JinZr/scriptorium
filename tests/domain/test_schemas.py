from pydantic import ValidationError
import pytest

from scriptorium.schemas import Evidence, ReviewOutput, output_schema


def test_evidence_requires_complete_anchor() -> None:
    with pytest.raises(ValidationError):
        Evidence(source_path="main.tex", start_line=1, quoted_text="claim")


def test_review_output_schema_forbids_unknown_fields() -> None:
    payload = {
        "summary": "No findings.",
        "findings": [],
        "unexpected": True,
    }
    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(payload)
    assert output_schema("review")["additionalProperties"] is False
