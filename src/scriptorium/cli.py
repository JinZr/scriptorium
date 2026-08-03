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
    doctor_parser.add_argument("--budget-usd", type=_nonnegative_float)

    run_parser = commands.add_parser("run", help="manage review runs")
    run_commands = run_parser.add_subparsers(dest="run_command", required=True)

    start_parser = run_commands.add_parser("start", help="start a review run")
    start_parser.add_argument("--revision", default="HEAD")
    start_parser.add_argument("--profile", default="full")
    start_parser.add_argument("--budget-usd", type=_nonnegative_float)

    status_parser = run_commands.add_parser("status", help="show a run")
    status_parser.add_argument("run_id")

    resume_parser = run_commands.add_parser("resume", help="resume a run")
    resume_parser.add_argument("run_id")

    retry_parser = run_commands.add_parser("retry", help="retry one task")
    retry_parser.add_argument("run_id")
    retry_parser.add_argument("--task", required=True, dest="task_id")
    retry_parser.add_argument("--route")

    cancel_parser = run_commands.add_parser("cancel", help="cancel a run")
    cancel_parser.add_argument("run_id")
    cancel_parser.add_argument("--reason", required=True, type=_nonempty)

    report_parser = run_commands.add_parser("report", help="render a run report")
    report_parser.add_argument("run_id")
    report_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")

    gate_parser = run_commands.add_parser("gate", help="evaluate the release gate")
    gate_parser.add_argument("run_id")

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
        result = service.doctor(
            profile=arguments.profile,
            budget_usd=arguments.budget_usd,
            revision=arguments.revision,
        )
        exit_code = _doctor_exit_code(result)
        if exit_code:
            error = InfrastructureError if exit_code == 3 else ConfigurationError
            raise error(_doctor_failure_message(result))
        return result, 0, None

    if arguments.command == "run":
        if arguments.run_command == "start":
            result = asyncio.run(
                service.start_run(
                    revision=arguments.revision,
                    profile=arguments.profile,
                    budget_usd=arguments.budget_usd,
                )
            )
            return result, 0, None
        if arguments.run_command == "status":
            return service.get_run(arguments.run_id), 0, None
        if arguments.run_command == "resume":
            return asyncio.run(service.resume_run(arguments.run_id)), 0, None
        if arguments.run_command == "retry":
            result = asyncio.run(service.retry_task(arguments.run_id, arguments.task_id, route=arguments.route))
            return result, 0, None
        if arguments.run_command == "cancel":
            return service.cancel_run(arguments.run_id, arguments.reason), 0, None
        if arguments.run_command == "report":
            result = service.render_report(arguments.run_id, arguments.format)
            return result, 0, arguments.format
        if arguments.run_command == "gate":
            result = service.evaluate_gate(arguments.run_id)
            return result, 0 if _gate_passed(result) else 1, None

    if arguments.command == "finding":
        if arguments.finding_command == "list":
            return service.list_findings(arguments.run_id), 0, None
        if arguments.finding_command == "show":
            return service.get_finding(arguments.finding_id), 0, None
        if arguments.finding_command == "decide":
            result = service.decide_finding(arguments.finding_id, arguments.decision, arguments.reason)
            return result, 0, None

    if arguments.command == "patch":
        if arguments.patch_command == "show":
            return service.get_patch(arguments.patch_id), 0, None
        if arguments.patch_command == "decide":
            result = service.decide_patch(arguments.patch_id, arguments.decision, arguments.reason)
            return result, 0, None
        if arguments.patch_command == "apply":
            result = service.apply_patch(arguments.patch_id)
            return result, 1 if _status_value(result) == "stale" else 0, None

    raise _UsageError("missing command")


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
