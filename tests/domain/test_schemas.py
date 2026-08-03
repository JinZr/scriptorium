import copy

from pydantic import ValidationError
import pytest

from scriptorium.schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    SCHEMA_MODELS,
    Evidence,
    EvidenceAnchorContract,
    EvidenceAnchorMap,
    ReviewOutput,
    VisualTranscriptionOutput,
    evidence_anchor_contract_content,
    evidence_anchor_contract_digest,
    output_schema,
)


def test_evidence_requires_complete_anchor() -> None:
    with pytest.raises(ValidationError):
        Evidence(source_path="main.tex", start_line=1, quoted_text="claim")


def test_evidence_requires_exactly_one_anchor_shape() -> None:
    digest = "a" * 64
    source = Evidence(
        source_path="main.tex",
        start_line=1,
        end_line=2,
        source_digest=digest,
        quoted_text="claim",
    )
    page = Evidence(source_path="manuscript.pdf", page=2)

    assert source.page is None
    assert page.start_line is None
    with pytest.raises(ValidationError):
        Evidence(
            source_path="main.tex",
            start_line=1,
            end_line=2,
            source_digest=digest,
            page=2,
        )
    with pytest.raises(ValidationError):
        Evidence(
            source_path="main.tex",
            start_line=1,
            end_line=2,
            source_digest=digest,
            page=None,
            quoted_text="claim",
        )
    with pytest.raises(ValidationError):
        Evidence(
            source_path="manuscript.pdf",
            start_line=None,
            page=2,
            quoted_text="claim",
        )


def test_pdf_shape_leaves_path_for_semantic_validation() -> None:
    evidence = Evidence(source_path="pages/page-0001.png", page=1)

    assert evidence.source_path == "pages/page-0001.png"


def test_anchor_contract_is_content_addressed_and_not_a_runtime_schema() -> None:
    content = evidence_anchor_contract_content()

    assert EvidenceAnchorContract.model_validate(content) == DEFAULT_EVIDENCE_ANCHOR_CONTRACT
    assert evidence_anchor_contract_digest() == evidence_anchor_contract_digest(
        EvidenceAnchorContract.model_validate(copy.deepcopy(content))
    )
    assert "version" not in content
    assert EvidenceAnchorContract not in SCHEMA_MODELS.values()


def test_evidence_schema_exposes_mutually_exclusive_contract_shapes() -> None:
    for kind in ("review", "verification"):
        evidence_schema = output_schema(kind)["$defs"]["Evidence"]

        assert evidence_schema["additionalProperties"] is False
        assert evidence_schema["required"] == ["source_path"]
        source_line, pdf_page = evidence_schema["oneOf"]
        assert source_line["required"] == ["start_line", "end_line", "source_digest", "quoted_text"]
        assert source_line["not"] == {"anyOf": [{"required": ["page"]}]}
        assert pdf_page["required"] == ["page"]
        assert pdf_page["properties"]["source_path"] == {"const": "manuscript.pdf"}
        assert pdf_page["not"] == {
            "anyOf": [
                {"required": ["start_line"]},
                {"required": ["end_line"]},
                {"required": ["source_digest"]},
                {"required": ["quoted_text"]},
            ]
        }


def test_output_schema_uses_the_supplied_frozen_contract() -> None:
    content = evidence_anchor_contract_content()
    content["source_line"]["matching_rule"] = "frozen source match"
    content["revision_edit"]["source_path_rule"] = "frozen edit path"
    contract = EvidenceAnchorContract.model_validate(content)

    evidence = output_schema("review", contract)["$defs"]["Evidence"]
    edit = output_schema("revision", contract)["$defs"]["ExactEdit"]

    assert "frozen source match" in evidence["properties"]["quoted_text"]["description"]
    assert edit["properties"]["path"]["description"] == "frozen edit path"


def test_visual_transcription_schema_is_unchanged_by_anchor_contract() -> None:
    assert "oneOf" not in output_schema("visual_transcription").get("$defs", {}).get("Evidence", {})


def test_source_map_models_enforce_canonical_paths_and_page_order() -> None:
    digest = "a" * 64
    source_map = EvidenceAnchorMap.model_validate(
        {
            "contract_digest": evidence_anchor_contract_digest(),
            "sources": [
                {
                    "source_path": "sections/methods.tex",
                    "read_path": "sources/sections/methods.tex",
                    "source_digest": digest,
                    "line_count": 12,
                    "text_anchorable": True,
                },
                {
                    "source_path": "figures/result.pdf",
                    "read_path": "sources/figures/result.pdf",
                    "source_digest": digest,
                    "line_count": None,
                    "text_anchorable": False,
                },
            ],
            "compiled_pdf": {
                "source_path": "manuscript.pdf",
                "read_path": "manuscript.pdf",
                "page_count": 2,
                "pages": [
                    {"page": 1, "read_path": "pages/page-0001.png", "page_digest": digest},
                    {"page": 2, "read_path": "pages/page-0002.png", "page_digest": digest},
                ],
            },
        }
    )

    assert source_map.compiled_pdf.pages[-1].read_path == "pages/page-0002.png"
    invalid = source_map.model_dump(mode="json")
    invalid["compiled_pdf"]["pages"][1]["read_path"] = "pages/page-2.png"
    with pytest.raises(ValidationError):
        EvidenceAnchorMap.model_validate(invalid)


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
