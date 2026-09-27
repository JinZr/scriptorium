from __future__ import annotations

import json
import shlex

from .domain import canonical_json
from .errors import ConfigurationError

MAX_TOOL_RESPONSE_BYTES = 7000


def success_json(payload):
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False, separators=(",", ":"))


def fits_response(payload):
    return len(success_json(payload).encode("utf-8")) + 1 <= MAX_TOOL_RESPONSE_BYTES


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


def bound_read(response, attempt_id, max_lines, max_chars):
    def build(pieces, line, offset):
        return {
            **response,
            "lines": pieces,
            "next_line": line,
            "next_offset": offset,
            "next_command": (
                tool_command(
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
                )
                if line is not None
                else None
            ),
        }

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


def bound_search(matches, total, attempt_id, query, path, cursor, limit):
    def build(count):
        next_cursor = cursor + count if cursor + count < total else None
        arguments = ["search", attempt_id, f"--query={query}", "--cursor", next_cursor, "--limit", limit]
        if path is not None:
            arguments.append(f"--path={path}")
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
