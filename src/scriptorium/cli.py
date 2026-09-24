from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
import json
import math
from pathlib import Path
import sys
from typing import Any

from .config import find_repo, initialize_project
from .domain import Attempt, Run, Task
from .errors import ConfigurationError, InfrastructureError, ScriptoriumError


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="scriptorium")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="initialize a manuscript repository")
    init_parser.add_argument("path", nargs="?", default=".")
    init_parser.add_argument("--main", default="main.tex")
    init_parser.add_argument("--engine", default="pdflatex")

    doctor_parser = commands.add_parser("doctor", help="check local configuration and dependencies")
    doctor_parser.add_argument("--revision", default="HEAD")
    doctor_parser.add_argument("--profile")
    doctor_parser.add_argument("--budget-usd")

    run_parser = commands.add_parser("run", help="manage review runs")
    run_commands = run_parser.add_subparsers(dest="run_command", required=True)

    start_parser = run_commands.add_parser("start", help="start a review run")
    start_parser.add_argument("--revision", default="HEAD")
    start_parser.add_argument("--profile", default="full")
    start_parser.add_argument("--budget-usd")

    status_parser = run_commands.add_parser("status", help="show a run")
    status_parser.add_argument("run_id")

    resume_parser = run_commands.add_parser("resume", help="resume a run")
    resume_parser.add_argument("run_id")

    retry_parser = run_commands.add_parser("retry", help="retry one task")
    retry_parser.add_argument("run_id")
    retry_parser.add_argument("--task", required=True, dest="task_id")
    retry_parser.add_argument("--abandon-attempt")
    retry_parser.add_argument("--reason")
    retry_parser.add_argument("--route")

    continue_parser = run_commands.add_parser("continue", help="continue an accepted partial review")
    continue_parser.add_argument("run_id")
    continue_parser.add_argument("--task", required=True, dest="task_id")

    cancel_parser = run_commands.add_parser("cancel", help="cancel a run")
    cancel_parser.add_argument("run_id")
    cancel_parser.add_argument("--reason", required=True, type=_nonempty)

    report_parser = run_commands.add_parser("report", help="render a run report")
    report_parser.add_argument("run_id")
    report_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")

    gate_parser = run_commands.add_parser("gate", help="evaluate the release gate")
    gate_parser.add_argument("run_id")

    task_parser = commands.add_parser("task", help="use a frozen task from an external model session")
    task_commands = task_parser.add_subparsers(dest="task_command", required=True)
    task_commands.add_parser("list").add_argument("run_id")
    claim_parser = task_commands.add_parser("claim")
    claim_parser.add_argument("task_id")
    claim_parser.add_argument("--client", choices=("codex", "claude_code", "antigravity"), required=True)
    claim_parser.add_argument("--model", required=True)
    claim_parser.add_argument("--effort", required=True)
    claim_parser.add_argument("--session-id", required=True)
    claim_parser.add_argument("--session-source", choices=("host", "declared"), required=True)
    task_commands.add_parser("show").add_argument("attempt_id")
    submit_parser = task_commands.add_parser("submit")
    submit_parser.add_argument("attempt_id")
    submit_parser.add_argument("--input-digest", required=True)
    submit_parser.add_argument("--file", required=True)
    read_parser = task_commands.add_parser("read")
    read_parser.add_argument("attempt_id")
    read_parser.add_argument("--path", required=True)
    read_parser.add_argument("--start-line", type=int, default=1)
    read_parser.add_argument("--max-lines", type=int, default=40)
    read_parser.add_argument("--offset", type=int, default=0)
    read_parser.add_argument("--max-chars", type=int, default=6000)
    search_parser = task_commands.add_parser("search")
    search_parser.add_argument("attempt_id")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--path")
    search_parser.add_argument("--cursor", type=int, default=0)
    search_parser.add_argument("--limit", type=int, default=20)
    page_parser = task_commands.add_parser("page")
    page_parser.add_argument("attempt_id")
    page_parser.add_argument("--number", type=int, required=True)

    finding_parser = commands.add_parser("finding", help="inspect and decide findings")
    finding_commands = finding_parser.add_subparsers(dest="finding_command", required=True)

    finding_list_parser = finding_commands.add_parser("list", help="list findings")
    finding_list_parser.add_argument("run_id")

    finding_show_parser = finding_commands.add_parser("show", help="show a finding")
    finding_show_parser.add_argument("finding_id")

    finding_decide_parser = finding_commands.add_parser("decide", help="record a finding decision")
    finding_decide_parser.add_argument("finding_id")
    finding_decisions = finding_decide_parser.add_mutually_exclusive_group(required=True)
    finding_decisions.add_argument("--confirm", action="store_const", const="confirm", dest="decision")
    finding_decisions.add_argument("--reject", action="store_const", const="reject", dest="decision")
    finding_decisions.add_argument("--waive", action="store_const", const="waive", dest="decision")
    finding_decide_parser.add_argument("--reason", required=True, type=_nonempty)

    patch_parser = commands.add_parser("patch", help="inspect, decide, and apply patches")
    patch_commands = patch_parser.add_subparsers(dest="patch_command", required=True)

    patch_show_parser = patch_commands.add_parser("show", help="show a patch")
    patch_show_parser.add_argument("patch_id")

    patch_decide_parser = patch_commands.add_parser("decide", help="record a patch decision")
    patch_decide_parser.add_argument("patch_id")
    patch_decisions = patch_decide_parser.add_mutually_exclusive_group(required=True)
    patch_decisions.add_argument("--approve", action="store_const", const="approve", dest="decision")
    patch_decisions.add_argument("--reject", action="store_const", const="reject", dest="decision")
    patch_decide_parser.add_argument("--reason", required=True, type=_nonempty)

    patch_apply_parser = patch_commands.add_parser("apply", help="apply a verified patch")
    patch_apply_parser.add_argument("patch_id")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in raw_arguments
    arguments = [argument for argument in raw_arguments if argument != "--json"]
    parser = build_parser()
    try:
        parsed = parser.parse_args(arguments)
        payload, exit_code, output_format = _dispatch(parsed)
    except _UsageError as exc:
        _emit_error("invalid_arguments", str(exc), json_output)
        return 2
    except ScriptoriumError as exc:
        _emit_error(exc.code, str(exc), json_output)
        return exc.exit_code
    except (KeyboardInterrupt, asyncio.CancelledError):
        _emit_error("interrupted", "operation interrupted", json_output)
        return 3
    except Exception as exc:
        _emit_error("infrastructure_error", str(exc), json_output)
        return 3

    _emit_success(payload, json_output, output_format)
    return exit_code


def _dispatch(arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.command == "init":
        repo = Path(arguments.path).expanduser().resolve()
        initialize_project(repo, arguments.main, arguments.engine)
        return {"repository": str(repo), "initialized": True}, 0, None

    repo = find_repo(Path.cwd())
    service = _build_service(repo)

    if arguments.command == "doctor":
        if arguments.budget_usd is not None:
            raise ConfigurationError("--budget-usd belongs to the retired internal model runner")
        result = service.doctor(
            profile=arguments.profile,
            revision=arguments.revision,
        )
        exit_code = _doctor_exit_code(result)
        if exit_code:
            error = InfrastructureError if exit_code == 3 else ConfigurationError
            raise error(_doctor_failure_message(result))
        return result, 0, None

    if arguments.command == "run":
        return _dispatch_run(service, arguments)
    if arguments.command == "task":
        return _dispatch_task(service, arguments)
    if arguments.command == "finding":
        return _dispatch_finding(service, arguments)
    if arguments.command == "patch":
        return _dispatch_patch(service, arguments)

    raise _UsageError("missing command")


def _dispatch_run(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.run_command == "start":
        if arguments.budget_usd is not None:
            raise ConfigurationError("--budget-usd belongs to the retired internal model runner")
        result = asyncio.run(
            service.start_run(
                revision=arguments.revision,
                profile=arguments.profile,
            )
        )
        return result, 0, None
    if arguments.run_command == "status":
        return service.get_run(arguments.run_id), 0, None
    if arguments.run_command == "resume":
        return asyncio.run(service.resume_run(arguments.run_id)), 0, None
    if arguments.run_command == "retry":
        if arguments.route is not None:
            raise ConfigurationError("--route belongs to the retired internal model runner")
        result = asyncio.run(
            service.retry_task(arguments.run_id, arguments.task_id, arguments.abandon_attempt, arguments.reason)
        )
        return result, 0, None
    if arguments.run_command == "continue":
        return asyncio.run(service.continue_review(arguments.run_id, arguments.task_id)), 0, None
    if arguments.run_command == "cancel":
        return service.cancel_run(arguments.run_id, arguments.reason), 0, None
    if arguments.run_command == "report":
        result = service.render_report(arguments.run_id, arguments.format)
        return result, 0, arguments.format
    if arguments.run_command == "gate":
        result = service.evaluate_gate(arguments.run_id)
        return result, 0 if _gate_passed(result) else 1, None

    raise _UsageError("missing run command")


def _dispatch_task(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.task_command == "list":
        return service.list_tasks(arguments.run_id), 0, None
    if arguments.task_command == "claim":
        return (
            service.claim_task(
                arguments.task_id,
                arguments.client,
                arguments.model,
                arguments.effort,
                arguments.session_id,
                arguments.session_source,
            ),
            0,
            None,
        )
    if arguments.task_command == "show":
        return service.show_task(arguments.attempt_id), 0, None
    if arguments.task_command == "submit":
        if arguments.file == "-":
            raw = sys.stdin.buffer.read(2_000_001)
        else:
            with Path(arguments.file).open("rb") as submitted:
                raw = submitted.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ConfigurationError("submission exceeds the 2 MB limit")
        try:
            contents = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigurationError("submission must be UTF-8") from exc
        return asyncio.run(service.submit_task(arguments.attempt_id, arguments.input_digest, contents)), 0, None
    if arguments.task_command == "read":
        return (
            service.read_task(
                arguments.attempt_id,
                arguments.path,
                arguments.start_line,
                arguments.max_lines,
                arguments.offset,
                arguments.max_chars,
            ),
            0,
            None,
        )
    if arguments.task_command == "search":
        return (
            service.search_task(
                arguments.attempt_id, arguments.query, arguments.path, arguments.cursor, arguments.limit
            ),
            0,
            None,
        )
    if arguments.task_command == "page":
        return service.page_task(arguments.attempt_id, arguments.number), 0, None

    raise _UsageError("missing task command")


def _dispatch_finding(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.finding_command == "list":
        return service.list_findings(arguments.run_id), 0, None
    if arguments.finding_command == "show":
        return service.get_finding(arguments.finding_id), 0, None
    if arguments.finding_command == "decide":
        result = service.decide_finding(arguments.finding_id, arguments.decision, arguments.reason)
        return result, 0, None

    raise _UsageError("missing finding command")


def _dispatch_patch(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.patch_command == "show":
        return service.get_patch(arguments.patch_id), 0, None
    if arguments.patch_command == "decide":
        result = service.decide_patch(arguments.patch_id, arguments.decision, arguments.reason)
        return result, 0, None
    if arguments.patch_command == "apply":
        result = service.apply_patch(arguments.patch_id)
        return result, 1 if _status_value(result) == "stale" else 0, None

    raise _UsageError("missing patch command")


def _build_service(repo: Path) -> Any:
    from .service import ScriptoriumService

    return ScriptoriumService(repo)


def _doctor_exit_code(result: Any) -> int:
    if not isinstance(result, Mapping):
        return 0
    if "exit_code" in result:
        return int(result["exit_code"])
    ready = result.get("ok", result.get("ready", True))
    return 0 if ready else 2


def _doctor_failure_message(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "doctor checks failed"
    messages = []
    for check in result.get("checks", []):
        if isinstance(check, Mapping):
            if not check.get("ok", False):
                messages.append(str(check.get("message", check.get("name", "check failed"))))
        else:
            messages.append(str(check))
    return "; ".join(messages) or str(result.get("message", "doctor checks failed"))


def _gate_passed(result: Any) -> bool:
    if isinstance(result, Mapping):
        return bool(result.get("passed", result.get("ok", False)))
    return bool(getattr(result, "passed", False))


def _status_value(result: Any) -> str | None:
    status = result.get("status") if isinstance(result, Mapping) else getattr(result, "status", None)
    return status.value if isinstance(status, Enum) else status


def _emit_success(payload: Any, json_output: bool, output_format: str | None) -> None:
    value = _jsonable(payload)
    if json_output:
        print(json.dumps({"ok": True, "data": value}, ensure_ascii=False, separators=(",", ":")))
        return
    if output_format == "markdown" and isinstance(payload, str):
        print(payload)
        return
    if output_format == "json" or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if value is not None:
        print(value)


def _emit_error(code: str, message: str, json_output: bool) -> None:
    if json_output:
        envelope = {"ok": False, "error": {"code": code, "message": message}}
        print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"error: {message}", file=sys.stderr)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Run) and value.frozen_config.get("execution") == "external":
        return {
            **{field.name: _jsonable(getattr(value, field.name)) for field in fields(value)},
            "estimated_cost_usd": None,
        }
    if isinstance(value, Attempt) and value.external_client is not None:
        return {
            **{field.name: _jsonable(getattr(value, field.name)) for field in fields(value)},
            "input_tokens": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "estimated_cost_usd": None,
        }
    if isinstance(value, Task) and value.route == "":
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value) if field.name != "route"}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return number


def _nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value
