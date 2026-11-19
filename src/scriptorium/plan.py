"""The author-approved plan: reading and validating a plan directory, and turning returned passages into exact edits.

Everything here is a pure function of the plan directory, the run's frozen facts, and a writer's output;
``workflow.py`` owns task preparation and state changes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence

from .domain import digest_json
from .errors import ConfigurationError
from .schemas import UNIVERSAL_LINE_TERMINATORS, ExactEdit, SourceAnchorRecord, ValidationIssue, WritingOutput

PLAN_FILE = "plan.md"
ASSIGNMENTS_DIRECTORY = "assignments"
PLAN_LIMIT = 64 * 1024
ASSIGNMENT_LIMIT = 32 * 1024
MAX_ASSIGNMENTS = 50
_ASSIGNMENT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_RANGE = re.compile(r"([0-9]+)-([0-9]+)")
_FRONT_MATTER_FENCE = "---"


@dataclass(frozen=True)
class Target:
    path: str
    start_line: int
    end_line: int

    def record(self) -> dict[str, Any]:
        return {"path": self.path, "start_line": self.start_line, "end_line": self.end_line}


@dataclass(frozen=True)
class Assignment:
    id: str
    data: bytes
    body: str
    targets: tuple[Target, ...]
    finding_ids: tuple[str, ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def listing(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "artifact_digest": self.digest,
            "targets": [target.record() for target in self.targets],
            "finding_ids": list(self.finding_ids),
        }


@dataclass(frozen=True)
class PlanDirectory:
    plan: bytes
    assignments: tuple[Assignment, ...]

    @property
    def plan_digest(self) -> str:
        return hashlib.sha256(self.plan).hexdigest()

    @property
    def digest(self) -> str:
        """Identify the plan by its text digest and each assignment's digest, targets, and finding IDs."""
        return digest_json({"plan": self.plan_digest, "assignments": [item.listing() for item in self.assignments]})


def _issue(code: str, path: str, message: str, *, expected: Any = None, actual: Any = None) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=message, expected=expected, actual=actual)


def read_regular_file(path: Path, limit: int) -> tuple[bytes | None, str | None]:
    """Return a regular file's bytes, or the reason it cannot be used; symbolic links are refused."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, "is missing"
    except OSError as exc:
        return None, f"cannot be read: {exc.strerror}"
    if not stat.S_ISREG(metadata.st_mode):
        return None, "is not a regular file"
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        return None, f"cannot be read: {exc.strerror}"
    if len(data) > limit:
        return None, f"exceeds {limit // 1024} KB"
    return data, None


def _text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_plan_directory(directory: str | Path) -> tuple[PlanDirectory | None, list[ValidationIssue]]:
    """Read DIR/plan.md and DIR/assignments/*.md; other entries in DIR are ignored."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError("the plan directory must be an existing directory, not a symbolic link")
    issues: list[ValidationIssue] = []
    plan = _read_plan(root, issues)
    assignments = _read_assignments(root, issues)
    if issues or plan is None:
        return None, issues
    return PlanDirectory(plan, tuple(assignments)), issues


def _read_plan(root: Path, issues: list[ValidationIssue]) -> bytes | None:
    data, problem = read_regular_file(root / PLAN_FILE, PLAN_LIMIT)
    if data is not None:
        text = _text(data)
        problem = "is not UTF-8" if text is None else "is blank" if not text.strip() else None
    if problem is None:
        return data
    issues.append(
        _issue(
            "plan.missing",
            PLAN_FILE,
            f"{PLAN_FILE} {problem}.",
            expected={"file": "a regular, non-blank UTF-8 file", "max_bytes": PLAN_LIMIT},
        )
    )
    return None


def _read_assignments(root: Path, issues: list[ValidationIssue]) -> list[Assignment]:
    directory = root / ASSIGNMENTS_DIRECTORY
    if directory.is_symlink() or not directory.is_dir():
        issues.append(_issue("plan.no_assignments", ASSIGNMENTS_DIRECTORY, "The plan has no assignments directory."))
        return []
    with os.scandir(directory) as entries:
        names = sorted(entry.name for entry in entries)
    files = []
    for name in names:
        stem = name[: -len(".md")] if name.endswith(".md") else ""
        path = directory / name
        if not _ASSIGNMENT_NAME.fullmatch(stem) or path.is_symlink() or not path.is_file():
            issues.append(
                _issue(
                    "plan.assignment_name_invalid",
                    f"{ASSIGNMENTS_DIRECTORY}/{name}",
                    "Every entry of assignments/ must be a regular file NAME.md, with NAME matching "
                    "[a-z0-9][a-z0-9-]{0,62}.",
                    actual=name,
                )
            )
            continue
        files.append((stem, path))
    if not files:
        issues.append(_issue("plan.no_assignments", ASSIGNMENTS_DIRECTORY, "The plan has no assignment files."))
        return []
    if len(files) > MAX_ASSIGNMENTS:
        issues.append(
            _issue(
                "plan.too_many_assignments",
                ASSIGNMENTS_DIRECTORY,
                f"A plan has at most {MAX_ASSIGNMENTS} assignments.",
                expected={"max": MAX_ASSIGNMENTS},
                actual={"count": len(files)},
            )
        )
        return []
    parsed = [_read_assignment(stem, path, issues) for stem, path in sorted(files)]
    return [item for item in parsed if item is not None]


def _read_assignment(stem: str, path: Path, issues: list[ValidationIssue]) -> Assignment | None:
    label = f"{ASSIGNMENTS_DIRECTORY}/{path.name}"
    data, problem = read_regular_file(path, ASSIGNMENT_LIMIT)
    text = None if data is None else _text(data)
    if data is not None and text is None:
        problem = "is not UTF-8"
    if problem is not None or data is None or text is None:
        issues.append(
            _issue(
                "plan.front_matter_invalid",
                label,
                f"The assignment file {problem}.",
                expected={"file": "a regular UTF-8 file", "max_bytes": ASSIGNMENT_LIMIT},
            )
        )
        return None
    return parse_assignment(stem, data, text, label, issues)


def parse_assignment(stem: str, data: bytes, text: str, label: str, issues: list[ValidationIssue]) -> Assignment | None:
    """Parse flat front matter (target lines, at most one findings line) and a non-blank Markdown body."""
    split = front_matter(text)
    if split is None:
        issues.append(
            _issue(
                "plan.front_matter_invalid",
                label,
                "An assignment starts with a line ---, then key: value lines, then a closing line ---.",
            )
        )
        return None
    targets: list[Target] = []
    finding_ids: tuple[str, ...] | None = None
    problems = []
    entries, body = split
    for number, key, value in entries:
        if key == "target" and value is not None:
            target = _parse_target(value)
            if target is None:
                problems.append(f"line {number}: target must be PATH:START-END with 1 <= START <= END")
            else:
                targets.append(target)
        elif key == "findings" and value is not None:
            if finding_ids is not None:
                problems.append(f"line {number}: findings appears more than once")
            finding_ids = _parse_findings(value, number, problems)
        else:
            problems.append(f"line {number}: expected 'target: PATH:START-END' or 'findings: ID, ID'")
    if not targets:
        problems.append("at least one target line is required")
    if problems:
        issues.append(_issue("plan.front_matter_invalid", label, "; ".join(problems)))
        return None
    if not body.strip():
        issues.append(_issue("plan.assignment_empty", label, "No instructions follow the front matter."))
        return None
    return Assignment(stem, data, body.strip("\n"), tuple(targets), finding_ids or ())


def front_matter(text: str) -> tuple[list[tuple[int, str, str | None]], str] | None:
    """Split a Markdown file into flat front matter and its body, or return None without both --- fences.

    Each line between the fences becomes (line number, key, value), stripped of surrounding whitespace; the value is
    None for a line without a colon. The body is the text after the closing fence, with line endings as ``\\n``.
    """
    lines = re.split(r"\r\n|\r|\n", text)
    close = next((index for index in range(1, len(lines)) if lines[index].strip() == _FRONT_MATTER_FENCE), None)
    if lines[0].strip() != _FRONT_MATTER_FENCE or close is None:
        return None
    entries = []
    for number, line in enumerate(lines[1:close], start=2):
        key, separator, value = line.partition(":")
        entries.append((number, key.strip(), value.strip() if separator else None))
    return entries, "\n".join(lines[close + 1 :])


def assignment_body(data: bytes) -> str | None:
    """Return the instructions of an approved assignment file, or None when it no longer parses."""
    text = _text(data)
    parsed = None if text is None else parse_assignment("assignment", data, text, "assignment", [])
    return None if parsed is None else parsed.body


def _parse_target(value: str) -> Target | None:
    path, separator, span = value.rpartition(":")
    match = _RANGE.fullmatch(span.strip())
    if not separator or not path.strip() or match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if not 1 <= start <= end:
        return None
    return Target(path.strip(), start, end)


def _parse_findings(value: str, number: int, problems: list[str]) -> tuple[str, ...]:
    if not value:
        return ()
    items = tuple(item.strip() for item in value.split(","))
    if any(not item for item in items) or len(set(items)) != len(items):
        problems.append(f"line {number}: findings is a comma-separated list of distinct finding IDs")
    return tuple(item for item in items if item)


def validate_plan(
    plan: PlanDirectory,
    sources: Sequence[SourceAnchorRecord],
    confirmed_ids: set[str],
    latest_digest: str | None,
) -> list[ValidationIssue]:
    """Check targets against the frozen source map and the assignments against the confirmed findings.

    ``latest_digest`` is the digest of the run's latest approved plan, or None before the first approval.
    """
    index = {source.source_path: source for source in sources}
    issues: list[ValidationIssue] = []
    placed: dict[str, list[tuple[Target, str]]] = {}
    for assignment in plan.assignments:
        label = f"{ASSIGNMENTS_DIRECTORY}/{assignment.id}.md"
        for number, target in enumerate(assignment.targets, start=1):
            problem = _target_issue(target, index.get(target.path), label, number)
            if problem is not None:
                issues.append(problem)
            else:
                placed.setdefault(target.path, []).append((target, f"{label} target {number}"))
        unknown = [finding_id for finding_id in assignment.finding_ids if finding_id not in confirmed_ids]
        if unknown:
            issues.append(
                _issue(
                    "plan.unknown_finding_ids",
                    label,
                    "The assignment lists findings that are not confirmed findings of this run.",
                    expected={"confirmed_finding_ids": sorted(confirmed_ids)},
                    actual={"unknown_finding_ids": unknown},
                )
            )
    issues.extend(_overlap_issues(placed))
    unassigned = sorted(confirmed_ids - {item for assignment in plan.assignments for item in assignment.finding_ids})
    if unassigned:
        issues.append(
            _issue(
                "plan.findings_unassigned",
                ASSIGNMENTS_DIRECTORY,
                "Every confirmed finding must be listed by an assignment; waive a finding the author will not change.",
                actual={"unassigned_finding_ids": unassigned},
            )
        )
    if latest_digest is not None and plan.digest == latest_digest:
        # The run is planning again because that plan's patch was rejected, failed to compile, or lost a finding.
        issues.append(
            _issue(
                "plan.unchanged",
                "",
                "The plan is identical to the latest approved plan and would reproduce its result; change it.",
                actual={"plan_digest": plan.digest},
            )
        )
    return issues


def _target_issue(target: Target, source: SourceAnchorRecord | None, label: str, number: int):
    actual = {"target": number, "value": f"{target.path}:{target.start_line}-{target.end_line}"}
    if source is None:
        return _issue(
            "plan.target_path_unknown",
            label,
            "The target path is not a bare source_path of the run's frozen source map.",
            actual=actual,
        )
    if not source.text_anchorable or source.line_count is None:
        return _issue("plan.target_not_editable", label, "The target path is not a text source.", actual=actual)
    if target.end_line > source.line_count:
        return _issue(
            "plan.target_out_of_bounds",
            label,
            f"The target range is outside {target.path}.",
            expected={"start_line": 1, "end_line": source.line_count},
            actual=actual,
        )
    return None


def _overlap_issues(placed: dict[str, list[tuple[Target, str]]]) -> list[ValidationIssue]:
    issues = []
    for path, targets in sorted(placed.items()):
        ordered = sorted(targets, key=lambda item: (item[0].start_line, item[0].end_line, item[1]))
        widest = ordered[0]
        for current in ordered[1:]:
            if current[0].start_line <= widest[0].end_line:
                issues.append(
                    _issue(
                        "plan.targets_overlap",
                        current[1].split(" target ")[0],
                        f"Targets overlap in {path}.",
                        actual={
                            "first": {"target": widest[1], **widest[0].record()},
                            "second": {"target": current[1], **current[0].record()},
                        },
                    )
                )
            if current[0].end_line > widest[0].end_line:
                widest = current
    return issues


def passage_file(index: int, path: str) -> str:
    """Name a target's passage file inside the export's task/passages/ directory."""
    return f"{index}{Path(path).suffix}"


def replacement_text(frozen: str, submitted: str) -> str:
    """Keep the frozen final line terminator when a non-empty replacement drops it; empty text deletes the lines."""
    if not submitted or submitted.endswith(UNIVERSAL_LINE_TERMINATORS):
        return submitted
    ending = next((item for item in UNIVERSAL_LINE_TERMINATORS if frozen.endswith(item)), "")
    return submitted + ending


def effective_passages(output: WritingOutput, frozen: Sequence[str]) -> list[str]:
    """Return the text each target of an accepted output takes, after the line-terminator rule, in target order."""
    texts = {passage.index: passage.text for passage in output.passages}
    return [replacement_text(before, texts[index]) for index, before in enumerate(frozen, start=1)]


def writing_issues(
    output: WritingOutput, frozen: Sequence[str], returned: Sequence[str] | None = None
) -> list[ValidationIssue]:
    """Require one passage per target and a change to at least one target.

    ``returned`` is the effective text of the output a check returned, for a redo task: the redo must differ from it.
    """
    expected = list(range(1, len(frozen) + 1))
    indexes = [passage.index for passage in output.passages]
    if sorted(indexes) != expected:
        return [
            _issue(
                "writing.passages_mismatch",
                "/passages",
                "Return exactly one passage for each target of the assignment.",
                expected={"indexes": expected},
                actual={"indexes": indexes},
            )
        ]
    effective = effective_passages(output, frozen)
    if effective == list(frozen):
        return [
            _issue(
                "writing.no_change",
                "/passages",
                "Every passage equals its frozen text; change at least one target.",
            )
        ]
    if returned is not None and effective == list(returned):
        return [
            _issue(
                "writing.unchanged_after_return",
                "/passages",
                "Every passage equals the text the check returned; revise it as the feedback asks.",
            )
        ]
    return []


def writing_edits(
    output: WritingOutput,
    assignment: Mapping[str, Any],
    frozen: Sequence[str],
    source_digests: Mapping[str, str],
) -> list[ExactEdit]:
    """Build one exact edit per changed target of an assignment whose output passed ``writing_issues``."""
    texts = {passage.index: passage.text for passage in output.passages}
    edits = []
    for index, (target, before) in enumerate(zip(assignment["targets"], frozen), start=1):
        after = replacement_text(before, texts[index])
        if after == before:
            continue
        edits.append(
            ExactEdit(
                finding_ids=list(assignment["finding_ids"]),
                path=target["path"],
                source_digest=source_digests[target["path"]],
                start_line=target["start_line"],
                end_line=target["end_line"],
                before=before,
                after=after,
                rationale=f"Assignment {assignment['id']}, target {index}.",
            )
        )
    return edits
