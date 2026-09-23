from dataclasses import replace
import json

import fitz
import pytest

from scriptorium.runtime import AgentResult, AgentUsage

from ._access import assert_retrieval, successful_accesses
from ._retrieval import directory_digest, make_fixture


def _trace(runtime, text, image, *, failed=False):
    if runtime == "codex":
        items = [
            {
                "type": "commandExecution",
                "status": "failed" if failed else "completed",
                "exitCode": 1 if failed else 0,
                "aggregatedOutput": text,
            },
            {"type": "imageView", "path": image},
        ]
        records = [
            {
                "kind": "notification",
                "method": "item/started" if failed else "item/completed",
                "payload": {"item": item},
            }
            for item in items
        ]
    elif runtime == "claude_code":
        blocks = [
            {"id": "text", "name": "Read", "input": {"file_path": "sources/records.txt"}},
            {"tool_use_id": "text", "is_error": failed, "content": text},
            {"id": "image", "name": "Read", "input": {"file_path": image}},
            {"tool_use_id": "image", "is_error": failed, "content": [{"type": "image", "source": {"data": "pixels"}}]},
        ]
        records = [{"kind": "message", "message": {"content": blocks}}]
    else:
        records = [
            {
                "kind": "step",
                "step": {
                    "type": "TOOL_CALL",
                    "status": "ERROR" if failed else "DONE",
                    "error": "denied" if failed else "",
                    "content": text,
                    "tool_calls": [{"name": "view_file", "canonical_path": image}],
                },
            }
        ]
    return "\n".join(json.dumps(record) for record in records)


def _result(case, runtime, trace):
    return AgentResult(
        thread_id="session",
        status="completed",
        final_response=json.dumps(case.expected),
        usage=AgentUsage(input_tokens=10, output_tokens=10),
        trace_jsonl=trace,
        runtime_name=runtime,
        runtime_version="fixture",
        model="fixture",
        model_provider="fixture",
        duration_ms=5,
        error=None,
    )


def test_fixture_answers_are_not_in_requests_or_metadata(tmp_path):
    workspace = tmp_path / "bundle"
    first, resumed = make_fixture(workspace)
    public = first.prompt + resumed.prompt + json.dumps(first.schema) + json.dumps(resumed.schema)
    public += (workspace / "manifest.json").read_text()
    for value in (*first.expected.values(), *resumed.expected.values()):
        assert value not in public
        assert all(value not in path.name for path in workspace.rglob("*"))
    assert first.expected["visual"] not in "".join(path.read_text() for path in (workspace / "sources").glob("*"))
    png = (workspace / first.image).read_bytes()
    assert first.expected["visual"].encode() not in png
    pixmap = fitz.Pixmap(png)
    assert pixmap.width == 480 and pixmap.height == 180
    assert len(set(pixmap.samples)) > 1
    lines = (workspace / "sources/records.txt").read_text().splitlines()
    assert first.expected["tail"] not in "\n".join(lines[:2000])
    assert first.expected["tail"] in lines[2400]
    assert first.expected["linked"] not in (workspace / "sources/selection.txt").read_text()
    assert first.markers[2] in (workspace / "sources/registry.txt").read_text()
    other, _ = make_fixture(tmp_path / "other")
    assert other.expected != first.expected


def test_workspace_digest_detects_content_paths_and_links(tmp_path):
    root = tmp_path / "bundle"
    make_fixture(root)
    before = directory_digest(root)
    path = root / "sources/appendix.txt"
    path.rename(root / "renamed.txt")
    assert directory_digest(root) != before
    (root / "renamed.txt").rename(path)
    assert directory_digest(root) == before
    (root / "external").symlink_to(tmp_path / "missing")
    assert directory_digest(root) != before


@pytest.mark.parametrize("runtime", ["codex", "claude_code", "antigravity"])
def test_exact_answers_require_completed_tool_evidence(tmp_path, runtime):
    first, resumed = make_fixture(tmp_path / "bundle")
    workspace = tmp_path / "bundle"
    trace = _trace(runtime, "\n".join(first.markers), str(workspace / first.image))
    result = _result(first, runtime, trace)
    assert_retrieval(first, result, workspace)
    for bad_trace in (
        "",
        _trace(runtime, "\n".join(first.markers), first.image, failed=True),
        json.dumps({"kind": "normalized_result", "answer": first.expected}),
    ):
        with pytest.raises(AssertionError, match="tool-result"):
            assert_retrieval(first, replace(result, trace_jsonl=bad_trace), workspace)
    with pytest.raises(AssertionError, match="Incorrect retrieval"):
        assert_retrieval(
            first, replace(result, final_response=json.dumps({**first.expected, "tail": "wrong"})), workspace
        )
    with pytest.raises(AssertionError, match="image access"):
        assert_retrieval(
            first, replace(result, trace_jsonl=_trace(runtime, "\n".join(first.markers), "elsewhere.png")), workspace
        )
    with pytest.raises(AssertionError, match="tool-result"):
        assert_retrieval(resumed, _result(resumed, runtime, trace), workspace)


def test_codex_failed_commands_and_unfinished_image_views_do_not_count():
    trace = json.dumps(
        {
            "kind": "notification",
            "method": "item/completed",
            "payload": {
                "item": {
                    "type": "commandExecution",
                    "status": "completed",
                    "exitCode": 1,
                    "aggregatedOutput": "partial",
                }
            },
        }
    )
    assert successful_accesses("codex", trace) == ([], [])
    trace = json.dumps(
        {
            "kind": "notification",
            "method": "item/started",
            "payload": {
                "item": {
                    "type": "imageView",
                    "path": "plate.png",
                }
            },
        }
    )
    assert successful_accesses("codex", trace) == ([], [])


def test_claude_requires_correlated_read_search_results():
    records = [
        {"tool_use_id": "absent", "content": "orphan", "is_error": False},
        {"id": "denied", "name": "Read", "input": {}},
        {"tool_use_id": "denied", "content": "denied", "is_error": True},
        {"id": "other", "name": "Bash", "input": {}},
        {"tool_use_id": "other", "content": "not an allowed reader", "is_error": False},
    ]
    trace = json.dumps({"kind": "message", "message": {"content": records}})
    assert successful_accesses("claude_code", trace) == ([], [])


@pytest.mark.parametrize("status,error", [("ACTIVE", ""), ("DONE", "denied"), ("UNKNOWN", "")])
def test_antigravity_requires_completed_nonerror_tool_steps(status, error):
    trace = json.dumps(
        {
            "kind": "step",
            "step": {
                "type": "TOOL_CALL",
                "status": status,
                "error": error,
                "content": "partial",
                "tool_calls": [{"name": "view_file", "canonical_path": "plate.png"}],
            },
        }
    )
    assert successful_accesses("antigravity", trace) == ([], [])


@pytest.mark.parametrize("trace", ["not json", "[]", "null"])
def test_malformed_trace_is_not_access_evidence(trace):
    with pytest.raises((ValueError, AssertionError)):
        successful_accesses("codex", trace)


@pytest.mark.parametrize("failure", [None, "exception", "mutation", "session"])
def test_harness_resume_and_failure_evidence(tmp_path, monkeypatch, failure):
    import asyncio

    from scriptorium.runtime import RUNTIME_SDK_VERSIONS

    from . import test_native_harnesses as harness

    cases = make_fixture(tmp_path / "prepared")
    (tmp_path / "prepared").rename(tmp_path / "bundle")
    monkeypatch.setattr(harness, "make_fixture", lambda workspace: cases)
    instances = []
    calls = []

    class Runtime:
        async def run_agent(self, prompt, role, workspace, schema, session_dir):
            calls.append((self, session_dir, None))
            if failure == "exception":
                raise RuntimeError("private provider details")
            if failure == "mutation":
                (workspace / "unexpected.txt").write_text("changed")
            return response(cases[0], workspace)

        async def resume_agent(self, thread_id, prompt, role, workspace, schema, session_dir):
            calls.append((self, session_dir, thread_id))
            result = response(cases[1], workspace)
            return replace(result, thread_id="unrelated") if failure == "session" else result

    def response(case, workspace):
        trace = _trace("codex", "\n".join(case.markers), str(workspace / (case.image or "unused.png")))
        return replace(
            _result(case, "codex", trace),
            runtime_version=RUNTIME_SDK_VERSIONS["codex"],
        )

    def factory(*args):
        instance = Runtime()
        instances.append(instance)
        return instance

    monkeypatch.setattr(harness, "_runtime", factory)
    if failure:
        expected = RuntimeError if failure == "exception" else AssertionError
        match = {"exception": "private", "mutation": "modified", "session": "unrelated"}[failure]
        with pytest.raises(expected, match=match):
            asyncio.run(harness._exercise_runtime("codex", "fixture", "fixture", tmp_path))
    else:
        asyncio.run(harness._exercise_runtime("codex", "fixture", "fixture", tmp_path))
        assert len(instances) == 2 and instances[0] is not instances[1]
        assert calls[0][1] == calls[1][1] == tmp_path / "session"
        assert calls[1][2] == "session"
        assert (tmp_path / "evidence/resumed.trace.jsonl").exists()
    report = json.loads((tmp_path / "evidence/first.json").read_text())
    assert report["requested_sdk_version"] == RUNTIME_SDK_VERSIONS["codex"]
    assert report["elapsed_seconds"] >= 0
    assert (report["workspace_digest_before"] != report["workspace_digest_after"]) == (failure == "mutation")
    if failure == "exception":
        assert report["exception_type"] == "RuntimeError"
        assert report["capability"] == "unverified"
        assert "private provider details" not in json.dumps(report)
        assert not (tmp_path / "evidence/first.trace.jsonl").exists()
