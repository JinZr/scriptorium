import json
from pathlib import Path


def successful_accesses(runtime: str, trace: str) -> tuple[list[str], list[str]]:
    records = [json.loads(line) for line in trace.splitlines() if line]
    assert all(isinstance(record, dict) for record in records), "Malformed native trace"
    if runtime == "codex":
        return _codex(records)
    if runtime == "claude_code":
        return _claude(records)
    if runtime == "antigravity":
        return _antigravity(records)
    raise AssertionError(f"Unknown runtime: {runtime}")


def _codex(records):
    outputs, images = [], []
    for record in records:
        if record.get("kind") == "notification" and record.get("method") == "item/completed":
            item = record.get("payload", {}).get("item", {})
        elif record.get("kind") == "item":
            item = record.get("item", {})
        else:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "commandExecution" and item.get("status") == "completed" and item.get("exitCode") == 0:
            output = item.get("aggregatedOutput")
            if isinstance(output, str):
                outputs.append(output)
        elif item.get("type") == "imageView" and isinstance(item.get("path"), str):
            images.append(item["path"])
    return outputs, images


def _claude(records):
    outputs, images, calls = [], [], {}
    for record in records:
        if record.get("kind") != "message":
            continue
        content = record.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("name") in {"Read", "Grep", "Glob"} and isinstance(block.get("id"), str):
                calls[block["id"]] = block
            call = calls.get(block.get("tool_use_id"))
            if call is None or block.get("is_error") not in (None, False):
                continue
            result = block.get("content")
            if isinstance(result, str):
                outputs.append(result)
            elif isinstance(result, list):
                for part in result:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        outputs.append(part["text"])
                    elif part.get("type") == "image" and call["name"] == "Read":
                        path = call.get("input", {}).get("file_path")
                        if isinstance(path, str):
                            images.append(path)
    return outputs, images


def _antigravity(records):
    outputs, images = [], []
    for record in records:
        step = record.get("step", {})
        if record.get("kind") != "step" or not isinstance(step, dict):
            continue
        if step.get("type") != "TOOL_CALL" or step.get("status") != "DONE" or step.get("error"):
            continue
        content = step.get("content")
        if not isinstance(content, str) or not content:
            continue
        for call in step.get("tool_calls", []):
            if call.get("name") in {"view_file", "search_directory", "find_file", "list_directory"}:
                outputs.append(content)
            if call.get("name") == "view_file":
                path = call.get("canonical_path") or call.get("args", {}).get("file_path")
                if isinstance(path, str) and Path(path).suffix.lower() == ".png":
                    images.append(path)
    return outputs, images


def assert_retrieval(case, result, workspace: Path) -> None:
    assert result.status == "completed", result.error
    assert result.final_response is not None
    assert json.loads(result.final_response) == case.expected, "Incorrect retrieval answer"
    assert result.usage.input_tokens + result.usage.output_tokens > 0, "Missing native usage"
    outputs, images = successful_accesses(result.runtime_name, result.trace_jsonl)
    for marker in case.markers:
        assert any(marker in output for output in outputs), "Successful tool-result evidence is missing or unrecognized"
    if case.image:
        expected = (workspace / case.image).resolve()
        assert any((workspace / path).resolve() == expected for path in images), "Missing completed image access"
