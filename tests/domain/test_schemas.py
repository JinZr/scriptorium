from pydantic import ValidationError
import pytest

from scriptorium.schemas import Evidence, ReviewOutput, VisualTranscriptionOutput, output_schema


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


def test_visual_transcription_schema_accepts_empty_page_text() -> None:
    digest = "a" * 64
    output = VisualTranscriptionOutput.model_validate(
        {
            "pdf_digest": digest,
            "pages": [{"page": 1, "page_digest": digest, "text": ""}],
        }
    )

    assert output.pages[0].text == ""
    assert output_schema("visual_transcription")["additionalProperties"] is False
