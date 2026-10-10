from __future__ import annotations

from collections import Counter
import json
import shlex

from .domain import canonical_json, digest_json
from .errors import ConfigurationError, ExampleUnavailableError

MAX_TOOL_RESPONSE_BYTES = 7000
REPORT_PARTS = (
    "run",
    "tasks",
    "finding_ids",
    "patch_ids",
    "findings",
    "findings_grouped",
    "decision_stats",
    "patches",
    "events",
    "validation_reports",
    "review_scopes",
    "review_claim_checks",
    "review_tool_access",
    "review_coverage_audit",
    "gate",
)


EXPORT_NOTE = (
    "This command wrote read-only copies of the frozen bundle files into the directory; it is the only task "
    "command whose output is a side effect on disk. An export is for reading convenience: findings still need "
    "frozen anchors (source_path, source_digest, lines), and task read --anchor remains the way to obtain a "
    "verified anchor. Rendered pages are not exported; inspect them with task page. If listing_truncated is "
    "true, source-map.json and manifest.json in the directory list every path and digest."
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


def brief_summary(brief):
    content = brief["content"]
    return {
        "digest": brief["digest"],
        "venue_family": content["venue_family"],
        "venue": content["venue"],
        "stage": content["stage"],
        "priority_claims": len(content["priority_claims"]),
        "known_weaknesses": len(content["known_weaknesses"]),
        "ignore": len(content["ignore"]),
        "has_prior_reviews": content["prior_reviews"] is not None,
        "has_severity_notes": content["severity_notes"] is not None,
    }


def run_overview(view, next_actions=None):
    run = view["run"]
    external = run.frozen_config.get("execution") == "external"
    optional = {}
    if "review_brief" in view:
        optional["review_brief"] = brief_summary(view["review_brief"])
    if "detected_template" in view:
        optional["detected_template"] = view["detected_template"]
    return require_bounded(
        {
            "run": {"id": run.id, "status": run.status.value, "commit_sha": run.commit_sha},
            **optional,
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


def task_view(context, part, offset, example_digest=None):
    attempt, task = context["attempt"], context["task"]
    brief = context.get("brief")
    digests = {
        "prompt": attempt.prompt_digest,
        "schema": attempt.schema_digest,
        "source-map": context["source_map_digest"],
        **({} if brief is None else {"brief": brief["digest"]}),
        **({} if context.get("example") is None else {"example": context["example_digest"]}),
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
    if part == "brief" and brief is None:
        raise ConfigurationError("this run was started without a review brief")
    if example_digest is not None and part != "example":
        raise ConfigurationError("--example-digest requires --part example")
    if part == "example" and context.get("example") is None:
        raise ExampleUnavailableError(context.get("example_error") or "no example is available for this attempt")
    # The example is built by the installed code rather than frozen, so its fragments are bound to its digest.
    if part == "example" and example_digest not in {None, digests["example"]}:
        raise ConfigurationError("example changed; restart at offset 0 without --example-digest")
    if part == "example" and offset != 0 and example_digest is None:
        raise ConfigurationError("a nonzero example offset requires --example-digest from the previous fragment")
    if part not in digests:
        raise ConfigurationError("part must be prompt, schema, source-map, brief, or example")
    content = {
        "prompt": context["prompt"],
        "schema": canonical_json(context["schema"]),
        "source-map": context["source_map_text"],
        "brief": None if brief is None else brief["text"],
        "example": context.get("example"),
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
                tool_command(
                    "show",
                    attempt.id,
                    "--part",
                    part,
                    "--offset",
                    end,
                    *(("--example-digest", digests[part]) if part == "example" else ()),
                )
                if end < len(content)
                else None
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
    if fits_response(full) or not pieces:
        # With no text to cut, metadata that cannot fit fails explicitly.
        return require_bounded(full)

    def whole(count):
        return build(pieces[:count], pieces[count]["line"], pieces[count]["offset"])

    # The full window does not fit, so at most every piece but the last is returned whole.
    count, result = _fit_prefix(len(pieces) - 1, whole)
    returned, piece = pieces[:count], pieces[count]

    def fragment(length):
        prefix = [*returned, {**piece, "text": piece["text"][:length]}] if length else returned
        return build(prefix, piece["line"], piece["offset"] + length)

    length, result = _fit_prefix(len(piece["text"]), fragment)
    if not returned and length == 0:
        raise ConfigurationError("response metadata leaves no room for source text")
    return result


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


def bound_nav(entries, total, counts, attempt_id, filters, cursor, limit, source_indexes):
    def build(page):
        next_cursor = cursor + len(page) if cursor + len(page) < total else None
        arguments = ["nav", attempt_id]
        for command in filters["commands"]:
            arguments.append(f"--command={command}")
        if filters["query"] is not None:
            arguments.append(f"--query={filters['query']}")
        if filters["path"] is not None:
            arguments.append(f"--path={filters['path']}")
        arguments.extend(["--cursor", next_cursor, "--limit", limit])
        return {
            "entries": page,
            "total_entries": total,
            "command_counts": counts,
            "next_cursor": next_cursor,
            "next_command": tool_command(*arguments) if next_cursor is not None else None,
        }

    full = build(entries)
    if not entries or fits_response(full):
        return require_bounded(full)
    path_filtered = filters["path"] is not None
    if len(entries) == 1 and cursor + 1 >= total:
        # The final entry needs no continuation, so only its own fields have to give way.
        return _fit_lone_nav_entry(entries[0], build, path_filtered, source_indexes)
    if not fits_response(build([])):
        # A filter can outgrow the continuation that repeats it, e.g. a path full of characters that need quoting.
        raise ConfigurationError(
            "navigation filters are too long to repeat in a bounded continuation; "
            "retry without --path and match entries by source_path"
        )
    count, response = _fit_prefix(len(entries), lambda count: build(entries[:count]))
    return response if count else _fit_lone_nav_entry(entries[0], build, path_filtered, source_indexes)


def _nav_candidates(entry, keep):
    candidates = entry.get("candidate_paths") or []
    if keep == len(candidates):
        return entry
    total = entry.get("candidate_count", len(candidates))
    return {**entry, "candidate_paths": candidates[:keep], "candidate_count": total, "candidate_paths_truncated": True}


def _without_source_path(entry):
    return {**{key: value for key, value in entry.items() if key != "source_path"}, "source_path_omitted": True}


def _fit_lone_nav_entry(entry, build, path_filtered, source_indexes):
    # Long or escape-heavy paths can still crowd out a lone entry. Keep the candidate paths that fit, then drop
    # the source path --path already names, then the target path the candidate count and source map still
    # identify, then cut the value by encoded size. Without --path, a source path that still does not fit
    # finally gives way to its position in the source map. Each change is flagged.
    def fitted(entry):
        def shrunk(keep):
            return build([_nav_candidates(entry, keep)])

        return _fit_prefix(len(entry.get("candidate_paths") or []), shrunk)[1] if fits_response(shrunk(0)) else None

    def value_cut(entry):
        bare, value = _nav_candidates(entry, 0), entry["value"]

        def cut(length):
            return build([{**bare, "value": value[:length], "value_truncated": True}])

        return _fit_prefix(len(value), cut)[1] if fits_response(cut(0)) else None

    response = fitted(entry)
    if response is None and path_filtered:
        entry = _without_source_path(entry)
        response = fitted(entry)
    if response is None and entry.get("target_path") is not None:
        entry = {**entry, "target_path": None, "target_path_omitted": True}
        response = fitted(entry)
    response = response or value_cut(entry)
    if response is None and not path_filtered:
        entry = {**_without_source_path(entry), "source_index": source_indexes[entry["source_path"]]}
        response = fitted(entry) or value_cut(entry)
    if response is None:
        raise ConfigurationError("response metadata leaves no room for a navigation entry")
    return response


def page_text_fragment(location, content, offset, attempt_id, number, document, expected_digest):
    # Extraction is derived, not frozen, so continuations must come from the same text.
    text_digest = digest_json(content)
    if expected_digest is not None and expected_digest != text_digest:
        raise ConfigurationError("page text layer changed; restart at offset 0 without --text-digest")
    if offset != 0 and expected_digest is None:
        raise ConfigurationError("a nonzero text offset requires --text-digest from the previous fragment")
    if not 0 <= offset <= len(content):
        raise ConfigurationError("offset is outside the page text layer")

    def fragment(length):
        end = offset + length
        arguments = ["page", attempt_id, "--number", number]
        if document is not None:
            arguments.append(f"--document={document}")
        arguments.extend(["--text", "--offset", end, "--text-digest", text_digest])
        return {
            **location,
            "text_layer": "pdf",
            "evidence": False,
            "text_digest": text_digest,
            "offset": offset,
            "total_chars": len(content),
            "text": content[offset:end],
            "next_offset": end if end < len(content) else None,
            "next_command": tool_command(*arguments) if end < len(content) else None,
        }

    length, response = _fit_prefix(len(content) - offset, fragment)
    if length == 0 and offset < len(content):
        raise ConfigurationError("response metadata leaves no room for page text")
    return response


def bound_export(attempt_id, directory, listing, total_bytes):
    def build(count):
        return {
            "attempt_id": attempt_id,
            "directory": directory,
            "file_count": len(listing),
            "total_bytes": total_bytes,
            "files_digest": digest_json(listing),
            "files": listing[:count],
            "listing_truncated": count < len(listing),
            "note": EXPORT_NOTE,
        }

    return _fit_prefix(len(listing), build)[1]
