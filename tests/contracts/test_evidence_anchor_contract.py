import copy

from pydantic import ValidationError
import pytest

from scriptorium.schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    SCHEMA_MODELS,
    Evidence,
    EvidenceAnchorContract,
    evidence_anchor_contract_content,
    evidence_anchor_contract_digest,
    output_schema,
)
from scriptorium.storage import SCHEMA_VERSION


def test_anchor_contract_is_content_addressed_without_a_version_field() -> None:
    content = evidence_anchor_contract_content()
    restored = EvidenceAnchorContract.model_validate(copy.deepcopy(content))

    assert "version" not in content
    assert evidence_anchor_contract_digest(restored) == evidence_anchor_contract_digest()
    assert EvidenceAnchorContract not in SCHEMA_MODELS.values()
    assert "evidence_anchor_contract" not in SCHEMA_MODELS
    assert SCHEMA_VERSION == 3
    assert (
        "final line terminator"
        in output_schema("revision")["$defs"]["ExactEdit"]["properties"]["before"]["description"]
    )


def test_anchor_contract_digest_changes_only_when_contract_content_changes() -> None:
    content = evidence_anchor_contract_content()
    same_content = {
        "revision_edit": copy.deepcopy(content["revision_edit"]),
        "pdf_page": copy.deepcopy(content["pdf_page"]),
        "source_line": copy.deepcopy(content["source_line"]),
    }
    changed_content = copy.deepcopy(content)
    changed_content["source_line"]["matching_rule"] = "different matching rule"

    assert evidence_anchor_contract_digest(EvidenceAnchorContract.model_validate(same_content)) == (
        evidence_anchor_contract_digest()
    )
    assert evidence_anchor_contract_digest(EvidenceAnchorContract.model_validate(changed_content)) != (
        evidence_anchor_contract_digest()
    )


def test_provider_and_local_evidence_shapes_are_mutually_exclusive() -> None:
    digest = "a" * 64
    source = {
        "source_path": "sections/methods.tex",
        "start_line": 2,
        "end_line": 3,
        "source_digest": digest,
        "quoted_text": "claim",
    }
    pdf = {
        "source_path": "manuscript.pdf",
        "page": 2,
        "quoted_text": "claim",
    }

    assert Evidence.model_validate(source).page is None
    assert Evidence.model_validate(pdf).page == 2

    invalid = (
        {**source, "page": 2},
        {**source, "page": None},
        {**pdf, "start_line": None},
        {**pdf, "source_digest": None},
        {key: value for key, value in source.items() if key != "source_digest"},
    )
    for payload in invalid:
        with pytest.raises(ValidationError):
            Evidence.model_validate(payload)

    for kind in ("review", "verification"):
        schema = output_schema(kind)["$defs"]["Evidence"]
        source_branch, pdf_branch = schema["oneOf"]

        assert source_branch["required"] == ["start_line", "end_line", "source_digest"]
        assert source_branch["not"] == {"anyOf": [{"required": ["page"]}]}
        assert pdf_branch["required"] == ["page"]
        assert pdf_branch["properties"]["source_path"] == {"const": "manuscript.pdf"}
        assert pdf_branch["not"] == {
            "anyOf": [
                {"required": ["start_line"]},
                {"required": ["end_line"]},
                {"required": ["source_digest"]},
            ]
        }


def test_visual_transcription_schema_does_not_consume_anchor_contract() -> None:
    content = evidence_anchor_contract_content()
    content["source_line"]["matching_rule"] = "changed for this test"
    contract = EvidenceAnchorContract.model_validate(content)

    assert output_schema("visual_transcription", contract) == output_schema("visual_transcription")
    assert output_schema("visual_transcription", contract) == SCHEMA_MODELS["visual_transcription"].model_json_schema()


def test_provider_schema_uses_the_supplied_contract_content() -> None:
    content = evidence_anchor_contract_content()
    content["pdf_page"]["matching_rule"] = "frozen PDF matching rule"
    content["revision_edit"]["source_path_rule"] = "frozen editable paths"
    contract = EvidenceAnchorContract.model_validate(content)

    review = output_schema("review", contract)["$defs"]["Evidence"]
    revision = output_schema("revision", contract)["$defs"]["ExactEdit"]

    assert review["oneOf"][1]["properties"]["source_path"] == {"const": "manuscript.pdf"}
    assert "frozen PDF matching rule" in review["properties"]["quoted_text"]["description"]
    assert revision["properties"]["path"]["description"] == "frozen editable paths"
    assert DEFAULT_EVIDENCE_ANCHOR_CONTRACT.pdf_page.source_path == "manuscript.pdf"
