from __future__ import annotations

import argparse
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
from .tool_output import REPORT_PARTS, pretty_json, run_overview, success_json


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


_BRIEF_LIMIT_BYTES = 256_000
_TASK_DESCRIPTION = """Use a frozen task from the current host model session.

Typical order: claim, export (frozen sources, navigation, source map, PDF, and this attempt's inputs under task/),
page for rendered pages, then submit --check and submit. Read the exported files with the host's own tools;
evidence anchors cite frozen source paths, digests, and lines. A writing task rewrites the exported
task/passages/ files and submits the export directory with --dir."""


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="scriptorium")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="initialize a manuscript repository")
    init_parser.add_argument("path", nargs="?", default=".", help="manuscript Git repository (default: .)")
    init_parser.add_argument("--main", default="main.tex", help="main LaTeX entrypoint (default: main.tex)")
    init_parser.add_argument("--engine", default="pdflatex", help="LaTeX engine (default: pdflatex)")

    doctor_parser = commands.add_parser("doctor", help="check local configuration and dependencies")
    doctor_parser.add_argument("--revision", default="HEAD", help="committed revision to check (default: HEAD)")
    doctor_parser.add_argument("--profile", help="review profile from scriptorium.toml")

    run_parser = commands.add_parser("run", help="manage review runs")
    run_commands = run_parser.add_subparsers(dest="run_command", required=True)

    start_parser = run_commands.add_parser("start", help="freeze a committed revision and prepare review tasks")
    start_parser.add_argument("--revision", default="HEAD", help="committed revision to freeze (default: HEAD)")
    start_parser.add_argument("--profile", default="full", help="review profile from scriptorium.toml (default: full)")
    start_parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="start even if a non-terminal run already exists on the same commit",
    )
    start_parser.add_argument(
        "--brief", help="review brief JSON file agreed with the authors, frozen into every review prompt"
    )

    status_parser = run_commands.add_parser("status", help="show a compact run overview and next actions")
    status_parser.add_argument("run_id", help="run ID")

    list_parser = run_commands.add_parser("list", help="list this project's runs, newest first")
    list_parser.add_argument("--status", metavar="STATUS", help="only runs in this status (a run status value)")
    list_parser.add_argument("--limit", type=int, default=20, help="maximum runs to return (default: 20, at most 100)")

    resume_parser = run_commands.add_parser("resume", help="replay accepted work and prepare the next stage")
    resume_parser.add_argument("run_id", help="run ID")

    retry_parser = run_commands.add_parser("retry", help="make a failed or abandoned task claimable again")
    retry_parser.add_argument("run_id", help="run ID")
    retry_parser.add_argument("--task", required=True, dest="task_id", help="task ID to retry")
    retry_parser.add_argument("--abandon-attempt", help="active attempt ID to interrupt before retrying")
    retry_parser.add_argument("--reason", help="why the active attempt is abandoned")

    cancel_parser = run_commands.add_parser("cancel", help="cancel a run and invalidate active attempts")
    cancel_parser.add_argument("run_id", help="run ID")
    cancel_parser.add_argument("--reason", required=True, type=_nonempty, help="non-empty cancellation reason")

    report_parser = run_commands.add_parser("report", help="render a run report or one report section")
    report_parser.add_argument("run_id", help="run ID")
    report_mode = report_parser.add_mutually_exclusive_group()
    report_mode.add_argument("--format", choices=("markdown", "json"), help="full report (default: markdown)")
    report_mode.add_argument("--part", choices=REPORT_PARTS, help="print one whole report section as JSON")

    gate_parser = run_commands.add_parser("gate", help="evaluate the release gate")
    gate_parser.add_argument("run_id", help="run ID")

    task_parser = commands.add_parser(
        "task",
        help="use a frozen task from an external model session",
        description=_TASK_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    task_commands = task_parser.add_subparsers(dest="task_command", required=True)
    list_parser = task_commands.add_parser("list", help="list tasks and attempts in detail")
    list_parser.add_argument("run_id", help="run ID")
    claim_parser = task_commands.add_parser("claim", help="claim a pending task for this host session")
    claim_parser.add_argument("task_id", help="pending task ID from run status next_actions")
    claim_parser.add_argument(
        "--client", choices=("codex", "claude_code", "antigravity"), required=True, help="host client"
    )
    claim_parser.add_argument("--model", required=True, help="model actually selected by the host")
    claim_parser.add_argument("--effort", required=True, help="reasoning effort actually selected by the host")
    claim_parser.add_argument("--session-id", required=True, help="host conversation ID")
    claim_parser.add_argument(
        "--session-source",
        choices=("host", "declared"),
        required=True,
        help="host if the ID came from the host's own state, otherwise declared",
    )
    show_parser = task_commands.add_parser("show", help="show a compact attempt overview")
    show_parser.add_argument("attempt_id", help="attempt ID from claim")
    submit_parser = task_commands.add_parser("submit", help="submit one complete output for validation")
    submit_parser.add_argument("attempt_id", help="active attempt ID")
    submit_parser.add_argument("--input-digest", required=True, help="input_digest returned by claim or show")
    submit_source = submit_parser.add_mutually_exclusive_group(required=True)
    submit_source.add_argument("--file", help="UTF-8 JSON file up to 2 MB, or - for stdin")
    submit_source.add_argument(
        "--dir", help="writing attempts: the export directory whose task/passages/ files and task/note.md to submit"
    )
    submit_parser.add_argument(
        "--check",
        action="store_true",
        help="validate exactly as submit would, without recording the output or ending the attempt",
    )
    page_parser = task_commands.add_parser("page", help="return a rendered page image path and digest")
    page_parser.add_argument("attempt_id", help="active attempt ID")
    page_parser.add_argument(
        "--number", type=int, required=True, help="1-based global page, or local page with --document"
    )
    page_parser.add_argument("--document", help="frozen LaTeX entrypoint; --number is then relative to this document")
    page_parser.add_argument(
        "--scale", type=float, help="render a view from the frozen PDF at this scale, 0.5-4.0 (frozen pages use 1.5)"
    )
    page_parser.add_argument(
        "--crop", type=_crop, help="render only x0,y0,x1,y1 as fractions of the page width and height"
    )
    page_parser.add_argument(
        "--text",
        action="store_true",
        help="write the page's PDF text layer to a file as a reading aid, never evidence",
    )

    export_parser = task_commands.add_parser(
        "export",
        help="write the frozen bundle and this attempt's task inputs (no page images) to a new or empty directory",
    )
    export_parser.add_argument("attempt_id", help="attempt ID, active or finished")
    export_parser.add_argument("--dir", required=True, help="directory that must not exist or must be empty")

    finding_parser = commands.add_parser("finding", help="inspect and decide findings")
    finding_commands = finding_parser.add_subparsers(dest="finding_command", required=True)

    finding_list_parser = finding_commands.add_parser("list", help="list findings")
    finding_list_parser.add_argument("run_id", help="run ID")

    finding_show_parser = finding_commands.add_parser("show", help="show a finding")
    finding_show_parser.add_argument("finding_id", help="finding ID")

    finding_decide_parser = finding_commands.add_parser("decide", help="record a human finding decision")
    finding_decide_parser.add_argument(
        "finding_ids", nargs="+", metavar="FINDING_ID", help="finding ID(s) of one run; one decision applies to each"
    )
    finding_decisions = finding_decide_parser.add_mutually_exclusive_group(required=True)
    finding_decisions.add_argument(
        "--confirm", action="store_const", const="confirm", dest="decision", help="confirm it for the plan"
    )
    finding_decisions.add_argument(
        "--reject", action="store_const", const="reject", dest="decision", help="reject the finding"
    )
    finding_decisions.add_argument(
        "--waive", action="store_const", const="waive", dest="decision", help="accept it without revision"
    )
    finding_decide_parser.add_argument("--reason", required=True, type=_nonempty, help="non-empty decision reason")

    plan_parser = commands.add_parser("plan", help="check and approve the author's plan for the writing tasks")
    plan_commands = plan_parser.add_subparsers(dest="plan_command", required=True)
    plan_check_parser = plan_commands.add_parser("check", help="validate a plan directory, recording nothing")
    plan_check_parser.add_argument("run_id", help="run ID")
    plan_check_parser.add_argument("--dir", required=True, help="plan directory with plan.md and assignments/")
    plan_approve_parser = plan_commands.add_parser(
        "approve", help="record the author's approval of a plan and prepare one writing task per assignment"
    )
    plan_approve_parser.add_argument("run_id", help="run ID")
    plan_approve_parser.add_argument("--dir", required=True, help="plan directory with plan.md and assignments/")
    plan_approve_parser.add_argument("--reason", required=True, type=_nonempty, help="the author's approval, in words")

    patch_parser = commands.add_parser("patch", help="inspect, decide, and apply patches")
    patch_commands = patch_parser.add_subparsers(dest="patch_command", required=True)

    patch_show_parser = patch_commands.add_parser("show", help="show a patch")
    patch_show_parser.add_argument("patch_id", help="patch ID")

    patch_decide_parser = patch_commands.add_parser("decide", help="record a human patch decision")
    patch_decide_parser.add_argument("patch_id", help="patch ID")
    patch_decisions = patch_decide_parser.add_mutually_exclusive_group(required=True)
    patch_decisions.add_argument(
        "--approve", action="store_const", const="approve", dest="decision", help="approve for verification"
    )
    patch_decisions.add_argument(
        "--reject", action="store_const", const="reject", dest="decision", help="reject the patch"
    )
    patch_decide_parser.add_argument("--reason", required=True, type=_nonempty, help="non-empty decision reason")

    patch_apply_parser = patch_commands.add_parser("apply", help="apply a verified patch to the worktree")
    patch_apply_parser.add_argument("patch_id", help="patch ID")

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
    except Exception as exc:
        _emit_error("infrastructure_error", str(exc), json_output)
        return 3
    except BaseException as exc:
        if not _interrupted(exc):
            raise
        _emit_error("interrupted", "operation interrupted", json_output)
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
    if arguments.command == "plan":
        return _dispatch_plan(service, arguments)
    if arguments.command == "patch":
        return _dispatch_patch(service, arguments)

    raise _UsageError("missing command")


def _dispatch_run(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.run_command == "start":
        result = _run_async(
            service.start_run(
                revision=arguments.revision,
                profile=arguments.profile,
                allow_duplicate=arguments.allow_duplicate,
                brief=None if arguments.brief is None else _read_brief(arguments.brief),
            )
        )
        return run_overview(result), 0, None
    if arguments.run_command == "status":
        return service.run_status(arguments.run_id), 0, None
    if arguments.run_command == "list":
        return service.list_runs(arguments.status, arguments.limit), 0, None
    if arguments.run_command == "resume":
        return run_overview(_run_async(service.resume_run(arguments.run_id))), 0, None
    if arguments.run_command == "retry":
        result = _run_async(
            service.retry_task(arguments.run_id, arguments.task_id, arguments.abandon_attempt, arguments.reason)
        )
        return run_overview(result), 0, None
    if arguments.run_command == "cancel":
        return run_overview(service.cancel_run(arguments.run_id, arguments.reason)), 0, None
    if arguments.run_command == "report":
        if arguments.part is not None:
            return service.read_report(arguments.run_id, arguments.part), 0, "json"
        report_format = arguments.format or "markdown"
        result = service.render_report(arguments.run_id, report_format)
        return result, 0, report_format
    if arguments.run_command == "gate":
        result = service.evaluate_gate(arguments.run_id)
        return result, 0 if _gate_passed(result) else 1, None

    raise _UsageError("missing run command")


def _dispatch_task(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.task_command == "list":
        return service.list_tasks(arguments.run_id), 0, None
    if arguments.task_command == "claim":
        return (
            service.task_view(
                service.claim_task(
                    arguments.task_id,
                    arguments.client,
                    arguments.model,
                    arguments.effort,
                    arguments.session_id,
                    arguments.session_source,
                )
            ),
            0,
            None,
        )
    if arguments.task_command == "show":
        return service.task_view(service.show_task(arguments.attempt_id)), 0, None
    if arguments.task_command == "submit":
        if arguments.dir is not None:
            contents = service.pack_writing_output(arguments.attempt_id, arguments.dir)
        else:
            contents = _read_submission(arguments.file)
        if arguments.check:
            return service.check_submission(arguments.attempt_id, arguments.input_digest, contents), 0, None
        return _run_async(service.submit_task(arguments.attempt_id, arguments.input_digest, contents)), 0, None
    if arguments.task_command == "export":
        return service.export_task(arguments.attempt_id, arguments.dir), 0, None
    if arguments.task_command == "page":
        return (
            service.page_task(
                arguments.attempt_id,
                arguments.number,
                arguments.document,
                arguments.scale,
                arguments.crop,
                arguments.text,
            ),
            0,
            None,
        )

    raise _UsageError("missing task command")


def _dispatch_finding(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.finding_command == "list":
        return service.list_findings(arguments.run_id), 0, None
    if arguments.finding_command == "show":
        return service.get_finding(arguments.finding_id), 0, None
    if arguments.finding_command == "decide":
        result = service.decide_findings(arguments.finding_ids, arguments.decision, arguments.reason)
        return result, 0, None

    raise _UsageError("missing finding command")


def _dispatch_plan(service: Any, arguments: argparse.Namespace) -> tuple[Any, int, str | None]:
    if arguments.plan_command == "check":
        return service.check_plan(arguments.run_id, arguments.dir), 0, None
    if arguments.plan_command == "approve":
        result = _run_async(service.approve_plan(arguments.run_id, arguments.dir, arguments.reason))
        return run_overview(result), 0, None

    raise _UsageError("missing plan command")


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


def _run_async(awaitable: Any) -> Any:
    # Retrieval commands never run a coroutine, so they skip the asyncio import.
    import asyncio

    return asyncio.run(awaitable)


def _interrupted(exc: BaseException) -> bool:
    asyncio = sys.modules.get("asyncio")
    return isinstance(exc, KeyboardInterrupt) or (asyncio is not None and isinstance(exc, asyncio.CancelledError))


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
        print(success_json(value))
        return
    if output_format == "markdown" and isinstance(payload, str):
        print(payload)
        return
    if output_format == "json" or isinstance(value, (dict, list)):
        print(pretty_json(value))
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


def _read_submission(path: str) -> str:
    if path == "-":
        raw = sys.stdin.buffer.read(2_000_001)
    else:
        with Path(path).open("rb") as submitted:
            raw = submitted.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ConfigurationError("submission exceeds the 2 MB limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("submission must be UTF-8") from exc


def _read_brief(path: str) -> str:
    try:
        with Path(path).open("rb") as brief:
            raw = brief.read(_BRIEF_LIMIT_BYTES + 1)
    except OSError as exc:
        raise ConfigurationError(f"cannot read review brief: {exc}") from exc
    if len(raw) > _BRIEF_LIMIT_BYTES:
        raise ConfigurationError("review brief exceeds the 256 KB limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("review brief must be UTF-8") from exc


def _nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return number


def _crop(value: str) -> tuple[float, float, float, float]:
    try:
        numbers = tuple(float(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be four comma-separated numbers") from exc
    if len(numbers) != 4 or not all(math.isfinite(number) for number in numbers):
        raise argparse.ArgumentTypeError("must be four comma-separated numbers")
    return numbers


def _nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value
