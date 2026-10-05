import io

from pydantic import ValidationError
import pytest

from scriptorium.schemas import (
    DEFAULT_EVIDENCE_ANCHOR_CONTRACT,
    EvidenceAnchorContract,
    evidence_anchor_contract_content,
    evidence_anchor_contract_digest,
)

CASES = [
    "",
    "one",
    "one\n",
    "one\ntwo",
    "one\r\ntwo\rthree\n",
    "alpha\fbeta\vgamma\x1c\x1d\x1e\x85  end\n",
    "\n\n",
]
LEGACY = DEFAULT_EVIDENCE_ANCHOR_CONTRACT.model_copy(update={"line_terminators": None})


@pytest.mark.parametrize("text", CASES)
def test_default_contract_numbers_lines_like_universal_newline_retrieval(text):
    with io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8") as stream:
        retrieved = [line.removesuffix("\n") for line in stream]
    assert DEFAULT_EVIDENCE_ANCHOR_CONTRACT.split_lines(text) == retrieved
    assert "".join(DEFAULT_EVIDENCE_ANCHOR_CONTRACT.split_lines(text, keepends=True)) == text


@pytest.mark.parametrize("text", CASES)
def test_contract_frozen_without_terminators_keeps_splitlines(text):
    assert LEGACY.split_lines(text) == text.splitlines()
    assert LEGACY.split_lines(text, keepends=True) == text.splitlines(keepends=True)


def test_terminators_are_frozen_content_and_absent_from_earlier_contracts():
    content = evidence_anchor_contract_content()
    legacy_content = evidence_anchor_contract_content(LEGACY)

    assert content["line_terminators"] == ["\r\n", "\r", "\n"]
    assert legacy_content == {key: value for key, value in content.items() if key != "line_terminators"}
    restored = EvidenceAnchorContract.model_validate(legacy_content)
    assert restored.line_terminators is None
    assert evidence_anchor_contract_digest(restored) == evidence_anchor_contract_digest(LEGACY)
    assert evidence_anchor_contract_digest(LEGACY) != evidence_anchor_contract_digest()
    with pytest.raises(ValidationError, match="line terminators"):
        EvidenceAnchorContract.model_validate({**content, "line_terminators": ["\n", "\f"]})
