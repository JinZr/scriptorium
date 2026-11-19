import os

import pytest

from scriptorium.errors import ConfigurationError
from scriptorium.plan import MAX_ASSIGNMENTS, read_plan_directory, validate_plan
from scriptorium.schemas import SourceAnchorRecord

SOURCES = [
    SourceAnchorRecord(
        source_path="sections/results.tex",
        read_path="sources/sections/results.tex",
        source_digest="a" * 64,
        line_count=60,
        text_anchorable=True,
    ),
    SourceAnchorRecord(
        source_path="main.tex",
        read_path="sources/main.tex",
        source_digest="b" * 64,
        line_count=10,
        text_anchorable=True,
    ),
    SourceAnchorRecord(
        source_path="figure.pdf",
        read_path="sources/figure.pdf",
        source_digest="c" * 64,
        line_count=None,
        text_anchorable=False,
    ),
]
ASSIGNMENT = """---
target: sections/results.tex:41-58
target: main.tex:6-7
findings: finding_a, finding_b
---
Limit the main conclusion to benchmark one. Keep the table numbers and the tab:main label.
"""


def _plan(tmp_path, assignments=None, plan="# Story\n\nThe method helps on benchmark one.\n"):
    root = tmp_path / "plan"
    (root / "assignments").mkdir(parents=True)
    if plan is not None:
        (root / "plan.md").write_text(plan, encoding="utf-8")
    for name, text in (assignments if assignments is not None else {"results.md": ASSIGNMENT}).items():
        (root / "assignments" / name).write_text(text, encoding="utf-8")
    return root


def _codes(issues):
    return [issue.code for issue in issues]


def test_a_valid_plan_is_read_in_sorted_order_with_its_targets_and_findings(tmp_path):
    root = _plan(tmp_path, {"results.md": ASSIGNMENT, "abstract.md": "---\ntarget: main.tex:1-1\n---\nTighten.\n"})
    (root / "notes.txt").write_text("other entries in DIR are ignored", encoding="utf-8")
    plan, issues = read_plan_directory(root)
    assert issues == []
    assert [item.id for item in plan.assignments] == ["abstract", "results"]
    results = plan.assignments[1]
    assert [target.record() for target in results.targets] == [
        {"path": "sections/results.tex", "start_line": 41, "end_line": 58},
        {"path": "main.tex", "start_line": 6, "end_line": 7},
    ]
    assert results.finding_ids == ("finding_a", "finding_b")
    assert results.body.startswith("Limit the main conclusion")
    assert plan.assignments[0].finding_ids == ()
    assert validate_plan(plan, SOURCES, {"finding_a", "finding_b"}, None) == []


def test_the_plan_digest_changes_with_any_file_or_listing(tmp_path):
    first, _ = read_plan_directory(_plan(tmp_path / "1"))
    same, _ = read_plan_directory(_plan(tmp_path / "2"))
    other, _ = read_plan_directory(_plan(tmp_path / "3", plan="# Story\n\nA different story.\n"))
    assert first.digest == same.digest != other.digest


def test_a_missing_directory_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError, match="existing directory"):
        read_plan_directory(tmp_path / "missing")


@pytest.mark.parametrize("plan", [None, "  \n", b"\xff"])
def test_plan_md_must_be_a_non_blank_utf8_file(tmp_path, plan):
    root = _plan(tmp_path, plan=None if plan is None or isinstance(plan, bytes) else plan)
    if isinstance(plan, bytes):
        (root / "plan.md").write_bytes(plan)
    assert _codes(read_plan_directory(root)[1]) == ["plan.missing"]


def test_plan_md_has_a_size_limit_and_may_not_be_a_link(tmp_path):
    root = _plan(tmp_path, plan="x" * (64 * 1024 + 1))
    assert _codes(read_plan_directory(root)[1]) == ["plan.missing"]
    (root / "plan.md").unlink()
    (tmp_path / "elsewhere.md").write_text("# Story\n", encoding="utf-8")
    os.symlink(tmp_path / "elsewhere.md", root / "plan.md")
    assert _codes(read_plan_directory(root)[1]) == ["plan.missing"]


def test_a_plan_needs_assignments(tmp_path):
    assert _codes(read_plan_directory(_plan(tmp_path, {}))[1]) == ["plan.no_assignments"]
    root = tmp_path / "bare"
    root.mkdir()
    (root / "plan.md").write_text("# Story\n", encoding="utf-8")
    assert _codes(read_plan_directory(root)[1]) == ["plan.no_assignments"]


def test_a_plan_has_at_most_fifty_assignments(tmp_path):
    many = {f"part-{index:02d}.md": ASSIGNMENT for index in range(MAX_ASSIGNMENTS + 1)}
    assert _codes(read_plan_directory(_plan(tmp_path, many))[1]) == ["plan.too_many_assignments"]


@pytest.mark.parametrize("name", ["Upper.md", "-lead.md", "notes.txt", "a_b.md", f"{'a' * 64}.md"])
def test_every_entry_of_assignments_must_be_a_named_markdown_file(tmp_path, name):
    issues = read_plan_directory(_plan(tmp_path, {"results.md": ASSIGNMENT, name: ASSIGNMENT}))[1]
    assert _codes(issues) == ["plan.assignment_name_invalid"]
    assert issues[0].path == f"assignments/{name}"


def test_a_directory_or_link_inside_assignments_is_refused(tmp_path):
    root = _plan(tmp_path)
    (root / "assignments" / "nested.md").mkdir()
    os.symlink(root / "assignments" / "results.md", root / "assignments" / "linked.md")
    assert _codes(read_plan_directory(root)[1]) == ["plan.assignment_name_invalid"] * 2


@pytest.mark.parametrize(
    "text",
    [
        "target: main.tex:1-2\nDo it.\n",
        "---\ntarget: main.tex:1-2\nDo it.\n",
        "---\nfindings: finding_a\n---\nNo target.\n",
        "---\ntarget: main.tex:3-2\n---\nBackwards.\n",
        "---\ntarget: main.tex:0-2\n---\nZero.\n",
        "---\ntarget: main.tex\n---\nNo range.\n",
        "---\ntarget: main.tex:1-2\nscope: all\n---\nUnknown key.\n",
        "---\ntarget: main.tex:1-2\n\n---\nBlank front matter line.\n",
        "---\ntarget: main.tex:1-2\nfindings: a\nfindings: b\n---\nTwice.\n",
        "---\ntarget: main.tex:1-2\nfindings: a, , b\n---\nEmpty item.\n",
        "---\ntarget: main.tex:1-2\nfindings: a, a\n---\nRepeated.\n",
    ],
)
def test_front_matter_is_flat_and_strict(tmp_path, text):
    assert _codes(read_plan_directory(_plan(tmp_path, {"part.md": text}))[1]) == ["plan.front_matter_invalid"]


def test_an_assignment_file_must_be_utf8_and_small(tmp_path):
    root = _plan(tmp_path)
    (root / "assignments" / "results.md").write_bytes(b"---\ntarget: main.tex:1-1\n---\n\xff\n")
    assert _codes(read_plan_directory(root)[1]) == ["plan.front_matter_invalid"]
    (root / "assignments" / "results.md").write_text(ASSIGNMENT + "x" * 32 * 1024, encoding="utf-8")
    assert _codes(read_plan_directory(root)[1]) == ["plan.front_matter_invalid"]


def test_an_assignment_needs_instructions(tmp_path):
    issues = read_plan_directory(_plan(tmp_path, {"part.md": "---\ntarget: main.tex:1-2\n---\n \n\n"}))[1]
    assert _codes(issues) == ["plan.assignment_empty"]


def test_an_empty_findings_line_and_crlf_lines_are_accepted(tmp_path):
    text = "---\r\ntarget: main.tex:1-2\r\nfindings:\r\n---\r\nRewrite the opening.\r\n"
    plan, issues = read_plan_directory(_plan(tmp_path, {"opening.md": text}))
    assert issues == [] and plan.assignments[0].finding_ids == ()


def test_a_target_path_splits_at_the_last_colon(tmp_path):
    plan, _ = read_plan_directory(_plan(tmp_path, {"part.md": "---\ntarget: odd:name.tex:4-5\n---\nDo it.\n"}))
    assert plan.assignments[0].targets[0].path == "odd:name.tex"


def _validated(tmp_path, assignments, confirmed=("finding_a",), rejected=None):
    plan, issues = read_plan_directory(_plan(tmp_path, assignments))
    assert issues == []
    return _codes(validate_plan(plan, SOURCES, set(confirmed), rejected)), plan


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("sources/main.tex:1-1", "plan.target_path_unknown"),
        ("figure.pdf:1-1", "plan.target_not_editable"),
        ("main.tex:9-11", "plan.target_out_of_bounds"),
    ],
)
def test_targets_must_name_lines_of_a_frozen_text_source(tmp_path, target, code):
    codes, _ = _validated(tmp_path, {"part.md": f"---\ntarget: {target}\nfindings: finding_a\n---\nDo it.\n"})
    assert codes == [code]


def test_targets_may_not_overlap_within_or_across_assignments(tmp_path):
    within = "---\ntarget: main.tex:1-5\ntarget: main.tex:5-6\nfindings: finding_a\n---\nDo it.\n"
    assert _validated(tmp_path / "1", {"part.md": within})[0] == ["plan.targets_overlap"]
    across = {
        "one.md": "---\ntarget: main.tex:1-8\nfindings: finding_a\n---\nDo it.\n",
        "two.md": "---\ntarget: main.tex:3-4\n---\nNested inside the first.\n",
        "three.md": "---\ntarget: main.tex:9-10\n---\nAdjacent is fine.\n",
    }
    assert _validated(tmp_path / "2", across)[0] == ["plan.targets_overlap"]


def test_findings_must_be_confirmed_and_every_confirmed_finding_assigned(tmp_path):
    text = "---\ntarget: main.tex:1-1\nfindings: finding_a, finding_x\n---\nDo it.\n"
    codes, _ = _validated(tmp_path, {"part.md": text}, confirmed=("finding_a", "finding_b"))
    assert codes == ["plan.unknown_finding_ids", "plan.findings_unassigned"]


def test_a_plan_equal_to_the_latest_approved_plan_is_refused(tmp_path):
    text = "---\ntarget: main.tex:1-1\nfindings: finding_a\n---\nDo it.\n"
    codes, plan = _validated(tmp_path, {"part.md": text})
    assert codes == []
    assert _codes(validate_plan(plan, SOURCES, {"finding_a"}, plan.digest)) == ["plan.unchanged"]
    assert _codes(validate_plan(plan, SOURCES, {"finding_a"}, "0" * 64)) == []
