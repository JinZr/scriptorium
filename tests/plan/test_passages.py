import pytest

from scriptorium.plan import effective_passages, passage_file, replacement_text, writing_edits, writing_issues
from scriptorium.schemas import WritingOutput

ASSIGNMENT = {
    "id": "results",
    "artifact_digest": "d" * 64,
    "targets": [
        {"path": "sections/results.tex", "start_line": 2, "end_line": 3},
        {"path": "main.tex", "start_line": 5, "end_line": 5},
    ],
    "finding_ids": [],
}
FROZEN = ["Line two.\r\nLine three.\r\n", "Last line without a break"]
DIGESTS = {"sections/results.tex": "a" * 64, "main.tex": "b" * 64}


def _output(*texts, indexes=None):
    indexes = indexes or range(1, len(texts) + 1)
    return WritingOutput.model_validate(
        {"passages": [{"index": index, "text": text} for index, text in zip(indexes, texts)]}
    )


@pytest.mark.parametrize(
    ("frozen", "submitted", "expected"),
    [
        ("old\n", "new", "new\n"),
        ("old\r\n", "new", "new\r\n"),
        ("old\r", "new\nnext", "new\nnext\r"),
        ("old\n", "new\r\n", "new\r\n"),
        ("old\n", "new\r", "new\r"),
        ("old\n", "", ""),
        ("old", "new", "new"),
        ("old", "new\n", "new\n"),
    ],
)
def test_a_dropped_final_line_terminator_is_kept_and_empty_text_deletes(frozen, submitted, expected):
    assert replacement_text(frozen, submitted) == expected


def test_passage_files_are_named_by_index_and_source_suffix():
    assert passage_file(1, "sections/results.tex") == "1.tex"
    assert passage_file(12, "refs.bib") == "12.bib"
    assert passage_file(3, "Makefile") == "3"


def test_each_target_needs_exactly_one_passage():
    for output in (_output("x"), _output("x", "y", "z"), _output("x", "y", indexes=[1, 1]), _output("x", indexes=[3])):
        issues = writing_issues(output, FROZEN)
        assert [issue.code for issue in issues] == ["writing.passages_mismatch"]
        assert issues[0].expected == {"indexes": [1, 2]}


def test_an_output_that_changes_nothing_is_refused():
    # Dropping the final terminator alone is no change: it is restored.
    unchanged = _output("Line two.\r\nLine three.", "Last line without a break")
    assert [issue.code for issue in writing_issues(unchanged, FROZEN)] == ["writing.no_change"]
    assert writing_issues(_output("Line two.\r\nLine 3.", "Last line without a break"), FROZEN) == []


def test_a_redo_must_change_the_returned_text_and_still_differ_from_the_frozen_text():
    returned = effective_passages(_output("Line two.\r\nLine 3.", "Last line without a break"), FROZEN)
    assert returned == ["Line two.\r\nLine 3.\r\n", "Last line without a break"]
    # The returned text with its final terminator dropped is the same text.
    same = _output("Line two.\r\nLine 3.", "Last line without a break")
    assert [issue.code for issue in writing_issues(same, FROZEN, returned)] == ["writing.unchanged_after_return"]
    back_to_frozen = _output("Line two.\r\nLine three.\r\n", "Last line without a break")
    assert [issue.code for issue in writing_issues(back_to_frozen, FROZEN, returned)] == ["writing.no_change"]
    assert writing_issues(_output("Line two.\r\nLine 3, revised.", "Last line"), FROZEN, returned) == []


def test_edits_are_built_only_for_changed_targets_with_the_frozen_text_as_before():
    output = _output("Line two.\r\nLine 3.", "Last line without a break")
    edits = writing_edits(output, ASSIGNMENT, FROZEN, DIGESTS)
    assert [edit.model_dump(mode="json") for edit in edits] == [
        {
            "finding_ids": [],
            "path": "sections/results.tex",
            "source_digest": "a" * 64,
            "start_line": 2,
            "end_line": 3,
            "before": "Line two.\r\nLine three.\r\n",
            "after": "Line two.\r\nLine 3.\r\n",
            "rationale": "Assignment results, target 1.",
        }
    ]
    deleted = writing_edits(_output("", "Last line without a break"), ASSIGNMENT, FROZEN, DIGESTS)
    assert deleted[0].after == ""
