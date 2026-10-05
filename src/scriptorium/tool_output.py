from __future__ import annotations

from collections import Counter
import json
import shlex

from .domain import canonical_json, digest_json
from .errors import ConfigurationError

MAX_TOOL_RESPONSE_BYTES = 7000
REPORT_PARTS = (
    "run",
    "tasks",
    "finding_ids",
    "patch_ids",
    "findings",
    "patches",
    "events",
    "validation_reports",
    "review_scopes",
    "review_claim_checks",
    "review_tool_access",
    "review_coverage_audit",
    "gate",
)


def success_json(payload):
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False, separators=(",", ":"))


def pretty_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)


def fits_response(payload):
    return all(
        len(serialized.encode("utf-8")) + 1 <= MAX_TOOL_RESPONSE_BYTES
        for serialized in (success_json(payload), pretty_json(payload))
    )


def require_bounded(payload):
    if not fits_response(payload):
        raise ConfigurationError("response metadata exceeds the 7000-byte tool response limit")
    return payload


def _fit_prefix(length, build):
    full = build(length)
    if fits_response(full):
        return length, full
    low, high = 0, length - 1
    result = require_bounded(build(0))
    while low < high:
        middle = (low + high + 1) // 2
        candidate = build(middle)
        if fits_response(candidate):
            low, result = middle, candidate
        else:
            high = middle - 1
    return low, result


def tool_command(*arguments):
    return shlex.join(["scriptorium", "--json", "task", *map(str, arguments)])


def report_command(run_id, part, *arguments):
    return shlex.join(["scriptorium", "--json", "run", "report", run_id, "--part", part, *map(str, arguments)])


def run_overview(view, next_actions=None):
    run = view["run"]
    external = run.frozen_config.get("execution") == "external"
    return require_bounded(
        {
            "run": {"id": run.id, "status": run.status.value, "commit_sha": run.commit_sha},
            "execution": "external" if external else "legacy_read_only",
            "task_counts": dict(Counter(item["task"].status.value for item in view["tasks"])),
            "finding_count": len(view["finding_ids"]),
            "patch_count": len(view["patch_ids"]),
            "has_error": run.error is not None,
            "estimated_cost_usd": None if external else run.estimated_cost_usd,
            "next_actions": next_actions if next_actions is not None else [{"command": "run status", "run_id": run.id}],
            "report_parts": {part: report_command(run.id, part) for part in REPORT_PARTS},
        }
    )


def report_fragment(run_id, report, part, offset, expected_digest):
    report_digest = digest_json(report)
    if expected_digest is not None and expected_digest != report_digest:
        raise ConfigurationError("report changed; restart at offset 0 without --report-digest")
    if offset != 0 and expected_digest is None:
        raise ConfigurationError("a nonzero report offset requires --report-digest from the previous fragment")
    content = canonical_json(report[part])
    part_digest = digest_json(report[part])
    if not 0 <= offset <= len(content):
        raise ConfigurationError("offset is outside the report part")

    def fragment(length):
        end = offset + length
        return {
            "run_id": run_id,
            "part": part,
            "report_digest": report_digest,
            "digest": part_digest,
            "offset": offset,
            "total_chars": len(content),
            "text": content[offset:end],
            "next_offset": end if end < len(content) else None,
            "next_command": (
                report_command(run_id, part, "--offset", end, "--report-digest", report_digest)
                if end < len(content)
                else None
            ),
        }

    length, response = _fit_prefix(len(content) - offset, fragment)
    if length == 0 and offset < len(content):
        raise ConfigurationError("report metadata leaves no room for text in the 7000-byte response limit")
    return response


def task_view(context, part, offset):
    attempt, task = context["attempt"], context["task"]
    digests = {
        "prompt": attempt.prompt_digest,
        "schema": attempt.schema_digest,
        "source-map": context["source_map_digest"],
    }
    if part is None:
        if offset != 0:
            raise ConfigurationError("--offset requires --part")
        return require_bounded(
            {
                "run_id": context["run_id"],
                "task": {"id": task.id, "role": task.role.value, "stage": task.stage, "status": task.status.value},
                "attempt": {
                    "id": attempt.id,
                    "ordinal": attempt.ordinal,
                    "status": attempt.status.value,
                    "external_client": attempt.external_client,
                    "model": attempt.model,
                    "effort": attempt.effort,
                    "thread_id": attempt.thread_id,
                    "session_source": attempt.session_source,
                },
                "input_digest": context["input_digest"],
                "bundle_digest": context["bundle_digest"],
                "navigation_digest": context["navigation_digest"],
                "inputs": {
                    name: {
                        "digest": digest,
                        "command": tool_command("show", attempt.id, "--part", name),
                    }
                    for name, digest in digests.items()
                },
                "source_map_command": tool_command("show", attempt.id, "--part", "source-map"),
            }
        )
    if part not in digests:
        raise ConfigurationError("part must be prompt, schema, or source-map")
    content = {
        "prompt": context["prompt"],
        "schema": canonical_json(context["schema"]),
        "source-map": context["source_map_text"],
    }[part]
    if not 0 <= offset <= len(content):
        raise ConfigurationError("offset is outside the frozen input")

    def fragment(length):
        end = offset + length
        return {
            "attempt_id": attempt.id,
            "input_digest": context["input_digest"],
            "part": part,
            "digest": digests[part],
            "offset": offset,
            "total_chars": len(content),
            "text": content[offset:end],
            "next_offset": end if end < len(content) else None,
            "next_command": (
                tool_command("show", attempt.id, "--part", part, "--offset", end) if end < len(content) else None
            ),
        }

    length, response = _fit_prefix(len(content) - offset, fragment)
    if length == 0 and offset < len(content):
        raise ConfigurationError("response metadata leaves no room for input text")
    return response


def read_anchor(response, pieces, next_line):
    """Return an evidence anchor covering the completely returned lines of a text source, if any."""
    if response.get("source_path") is None:
        return None
    complete = [piece for piece in pieces if piece["offset"] == 0 and piece["line"] != next_line]
    quoted = "\n".join(piece["text"] for piece in complete)
    if not quoted:
        return None
    return {
        "source_path": response["source_path"],
        "start_line": complete[0]["line"],
        "end_line": complete[-1]["line"],
        "source_digest": response["source_digest"],
        "quoted_text": quoted,
    }


def bound_read(response, attempt_id, max_lines, max_chars, end_line=None, anchor=False, anchor_lines_match=True):
    def build(pieces, line, offset):
        arguments = [
            "read",
            attempt_id,
            f"--path={response['path']}",
            "--start-line",
            line,
            "--offset",
            offset,
            "--max-lines",
            max_lines,
            "--max-chars",
            max_chars,
        ]
        if end_line is not None:
            arguments.extend(["--end-line", end_line])
        if anchor:
            arguments.append("--anchor")
        result = {
            **response,
            "lines": pieces,
            "next_line": line,
            "next_offset": offset,
            "next_command": tool_command(*arguments) if line is not None else None,
        }
        if anchor:
            result["anchor"] = read_anchor(response, pieces, line) if anchor_lines_match else None
        return result

    pieces = response["lines"]
    full = build(pieces, response["next_line"], response["next_offset"])
    if fits_response(full):
        return full
    returned = []
    for index, piece in enumerate(pieces):
        following = pieces[index + 1] if index + 1 < len(pieces) else None
        line = following["line"] if following else response["next_line"]
        offset = following["offset"] if following else response["next_offset"]
        if fits_response(build([*returned, piece], line, offset)):
            returned.append(piece)
            continue

        def fragment(length):
            prefix = [*returned, {**piece, "text": piece["text"][:length]}] if length else returned
            return build(prefix, piece["line"], piece["offset"] + length)

        length, result = _fit_prefix(len(piece["text"]), fragment)
        if not returned and length == 0:
            raise ConfigurationError("response metadata leaves no room for source text")
        return result
    return require_bounded(full)


def bound_search(matches, total, attempt_id, query, path, cursor, limit, context=0, include_metadata=False):
    def build(count):
        next_cursor = cursor + count if cursor + count < total else None
        arguments = ["search", attempt_id, f"--query={query}", "--cursor", next_cursor, "--limit", limit]
        if path is not None:
            arguments.append(f"--path={path}")
        if context:
            arguments.extend(["--context", context])
        if include_metadata:
            arguments.append("--include-metadata")
        return {
            "matches": matches[:count],
            "total_matches": total,
            "next_cursor": next_cursor,
            "next_command": tool_command(*arguments) if next_cursor is not None else None,
        }

    count, response = _fit_prefix(len(matches), build)
    if matches and count == 0:
        raise ConfigurationError("response metadata leaves no room for a search match")
    return response


def bound_nav(entries, total, counts, attempt_id, filters, cursor, limit):
    def build(count):
        next_cursor = cursor + count if cursor + count < total else None
        arguments = ["nav", attempt_id]
        for command in filters["commands"]:
            arguments.append(f"--command={command}")
        if filters["query"] is not None:
            arguments.append(f"--query={filters['query']}")
        if filters["path"] is not None:
            arguments.append(f"--path={filters['path']}")
        arguments.extend(["--cursor", next_cursor, "--limit", limit])
        return {
            "entries": entries[:count],
            "total_entries": total,
            "command_counts": counts,
            "next_cursor": next_cursor,
            "next_command": tool_command(*arguments) if next_cursor is not None else None,
        }

    count, response = _fit_prefix(len(entries), build)
    if entries and count == 0:
        raise ConfigurationError("response metadata leaves no room for a navigation entry")
    return response
