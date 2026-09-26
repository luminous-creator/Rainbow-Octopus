from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser

from . import __version__
from .config import (
    ConfigError,
    SETTINGS,
    apply_config,
    describe,
    load_config,
    project_config_path,
    rocto_home,
    set_config_value,
    template,
    user_config_path,
)
from .contract import check_contract
from .doctor import doctor_as_dict, run_doctor
from .executor import auto_order
from .ledger import Ledger, summarize
from .models import SpecValidationError, TaskSpec
from .orchestrator import (
    EXIT_INTERRUPTED,
    EXIT_USAGE,
    MAX_RETRIES_LIMIT,
    BuildError,
    default_orchestrator,
    default_output_dir,
)
from .planner import PLANNER_CHOICES, PlanningError
from .report import REPORT_NAME, collect, describe_step, render_markdown, render_text, write_html_report
from .state import StateStore

EXECUTOR_CHOICES = ("auto", "claude", "codex", "deepseek")
LAST_BUILD_FILE = "last-build"

#: Filled in by main() once config files have been applied.
_CONFIG_SOURCES: dict[str, str] = {}


# ---------------------------------------------------------------------- parser


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str) -> float | None:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _add_run_options(parser: argparse.ArgumentParser, *, planning: bool) -> None:
    """Options shared by build, resume and refine."""
    order = ", ".join(auto_order())
    parser.add_argument(
        "--executor",
        choices=EXECUTOR_CHOICES,
        default=os.environ.get("ROCTO_EXECUTOR", "auto"),
        help=f"Who writes the site. 'auto' tries {order} (ROCTO_EXECUTOR_ORDER), "
        "skipping any that is not installed or signed in",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        choices=range(0, MAX_RETRIES_LIMIT + 1),
        default=min(MAX_RETRIES_LIMIT, max(0, _env_int("ROCTO_MAX_RETRIES", 2))),
        metavar=f"0..{MAX_RETRIES_LIMIT}",
        help="Repair attempts after the first one (default 2)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_env_int("ROCTO_TIMEOUT", 1200),
        help="Executor timeout per attempt in seconds",
    )
    parser.add_argument(
        "--escalate-after",
        type=int,
        default=_env_int("ROCTO_ESCALATE_AFTER", 2),
        metavar="N",
        help="With --executor auto: after N failed verifications in a row, hand "
        "the repair to the next executor (0 = never; default 2)",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=_env_float("ROCTO_MAX_MINUTES"),
        help="Do not start a new attempt after this many minutes",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=_env_float("ROCTO_MAX_COST_USD"),
        help="Do not start a new attempt once reported spend reaches this",
    )
    if planning:
        parser.add_argument(
            "--planner",
            choices=PLANNER_CHOICES,
            default=os.environ.get("ROCTO_PLANNER", "auto"),
            help="Who writes the task specification. 'auto' uses the API when a "
            "key is set, otherwise a signed-in Claude Code",
        )
        parser.add_argument(
            "--model",
            default=os.environ.get("ROCTO_DEEPSEEK_MODEL", "deepseek-v4-flash"),
            help="Model for the API planner",
        )
        parser.add_argument(
            "--review-plan",
            action="store_true",
            help="Show the plan and ask before spending anything on generation",
        )
    parser.add_argument(
        "--open",
        action="store_true",
        default=_env_bool("ROCTO_OPEN"),
        help="Open report.html in your browser when finished",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("-q", "--quiet", action="store_true", help="Only print the result")
    output.add_argument(
        "--json-events",
        action="store_true",
        help="Print one JSON object per event (NDJSON) instead of text",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rocto",
        description="Turn an idea into a verified static web demo.",
        epilog="Typical use:  rocto build \"a pomodoro timer with a daily counter\"",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    doctor = sub.add_parser("doctor", help="Check what is installed and what to fix")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    build = sub.add_parser("build", help="Build and verify a static web demo")
    build.add_argument("idea", help="One-sentence product idea")
    build.add_argument(
        "--output", "-o", type=Path,
        help="New or empty directory (default: <output_root>/<timestamp>-<slug>)",
    )
    build.add_argument(
        "--spec", type=Path,
        help="Use this task.json instead of planning (skips the planner)",
    )
    _add_run_options(build, planning=True)

    resume = sub.add_parser("resume", help="Continue a build from its last checkpoint")
    resume.add_argument("project", nargs="?", type=Path, help="Build directory (default: the last build)")
    _add_run_options(resume, planning=False)

    refine = sub.add_parser("refine", help="Apply a change request to a finished build")
    refine.add_argument("request", help="What to change, in one sentence")
    refine.add_argument("project", nargs="?", type=Path, help="Build directory (default: the last build)")
    refine.add_argument(
        "--keep-failed", action="store_true",
        help="Leave a failed change in place instead of restoring the last good version",
    )
    _add_run_options(refine, planning=True)

    status = sub.add_parser("status", help="Show a build's state and what to do next")
    status.add_argument("project", nargs="?", type=Path)
    status.add_argument("--json", action="store_true")

    report = sub.add_parser("report", help="Summarise a build (text, Markdown or HTML)")
    report.add_argument("project", nargs="?", type=Path)
    report.add_argument("--format", choices=("text", "markdown", "html"), default="text")
    report.add_argument("--screenshot-url", help="Image URL to embed in Markdown output")

    open_cmd = sub.add_parser("open", help="Open a build's report (or the page itself)")
    open_cmd.add_argument("project", nargs="?", type=Path)
    open_cmd.add_argument("--site", action="store_true", help="Open index.html instead of the report")

    serve = sub.add_parser("serve", help="Preview a build on http://127.0.0.1")
    serve.add_argument("project", nargs="?", type=Path)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true", help="Do not open a browser")

    config = sub.add_parser("config", help="Show or change settings")
    config_sub = config.add_subparsers(dest="config_command", metavar="ACTION")
    config_sub.add_parser("show", help="Every setting, its value and where it came from")
    config_set = config_sub.add_parser("set", help="Set a value in the user (or project) config")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.add_argument("--project", action="store_true", help="Write ./rocto.toml instead")
    config_unset = config_sub.add_parser("unset", help="Remove a value")
    config_unset.add_argument("key")
    config_unset.add_argument("--project", action="store_true")
    config_sub.add_parser("path", help="Print the config file locations")

    init = sub.add_parser("init", help="Write a commented rocto.toml in this directory")
    init.add_argument("--force", action="store_true", help="Overwrite an existing rocto.toml")

    stats = sub.add_parser("stats", help="Pass rates, time and spend from past builds")
    stats.add_argument("--json", action="store_true")

    batch = sub.add_parser("batch", help="Build many ideas, one after another")
    batch.add_argument("file", type=Path, help="JSON list of {id, idea} or a text file, one idea per line")
    batch.add_argument("--output-root", type=Path, help="Where the batch directory goes")
    batch.add_argument(
        "--min-verified", type=int,
        help="Exit 1 unless every page is openable and at least this many pass",
    )
    batch.add_argument("--json", action="store_true", help="Print the summary as JSON")
    _add_run_options(batch, planning=True)

    gallery = sub.add_parser("gallery", help="Collect passing builds into one static site")
    gallery.add_argument("source", type=Path, help="Directory that contains build directories")
    gallery.add_argument("--output", "-o", type=Path, required=True)
    gallery.add_argument("--title", default="Rainbow Octopus gallery")
    gallery.add_argument("--include-failed", action="store_true")
    return parser


# ------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    global _CONFIG_SOURCES
    try:
        loaded = load_config()
        _CONFIG_SOURCES = apply_config(loaded)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    args = build_parser().parse_args(argv)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "doctor": lambda a: _doctor(a.json),
        "build": _build,
        "resume": _resume,
        "refine": _refine,
        "status": _status,
        "report": _report,
        "open": _open,
        "serve": _serve,
        "config": _config,
        "init": _init,
        "stats": _stats,
        "batch": _batch,
        "gallery": _gallery,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


# ---------------------------------------------------------------------- doctor


def _doctor(as_json: bool) -> int:
    checks = run_doctor()
    payload = doctor_as_dict(checks)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            if check.passed:
                icon = "OK"
            else:
                icon = "FAIL" if check.required else "--"
            print(f"[{icon:4}] {check.name:18} {check.detail}")
        fixes = [c for c in checks if c.required and not c.passed and c.fix]
        if fixes:
            print("\nTo fix:")
            for check in fixes:
                print(f"  {check.name}: {check.fix}")
        print(
            "\nReady. Try:  rocto build \"a pomodoro timer with a daily counter\""
            if payload["passed"]
            else "\nFix the failed checks above before running build."
        )
    return 0 if payload["passed"] else 1


# ----------------------------------------------------------------------- runs


_PHASE_LABEL = {
    "start": "start",
    "planning": "plan",
    "planned": "plan",
    "review": "review",
    "contract": "contract",
    "executing": "build",
    "executed": "build",
    "execution_failed": "build",
    "verifying": "verify",
    "verification_failed": "verify",
    "completed": "done",
    "failed": "failed",
    "retry": "retry",
    "stopped": "stopped",
    "interrupted": "stopped",
    "cancelled": "stopped",
    "refining": "refine",
    "restored": "restore",
}


def _make_reporter(quiet: bool):
    """Print one line per phase with elapsed time.

    A build blocks for minutes inside a single model call. Without this the
    terminal shows nothing at all and looks hung — which is exactly what it
    looked like the first time it was run for real.
    """
    started = time.monotonic()

    def report(phase: str, detail: str) -> None:
        if quiet:
            return
        label = _PHASE_LABEL.get(phase, phase)
        elapsed = time.monotonic() - started
        print(f"[{elapsed:5.1f}s] {label:8} {detail}", flush=True)

    return report


def _json_printer(record: dict[str, Any]) -> None:
    print(json.dumps({"type": "event", **record}, ensure_ascii=False), flush=True)


def _orchestrator_for(args: argparse.Namespace, *, planning: bool, **extra: Any):
    json_mode = getattr(args, "json_events", False)
    quiet = getattr(args, "quiet", False)
    kwargs: dict[str, Any] = {
        "max_retries": args.max_retries,
        "timeout": args.timeout,
        "backend": args.executor,
        "on_event": _make_reporter(quiet or json_mode),
        "on_record": _json_printer if json_mode else None,
        "escalate_after": args.escalate_after,
        "max_minutes": args.max_minutes,
        "max_cost_usd": args.max_cost_usd,
        "ledger": Ledger.default(),
        **extra,
    }
    if planning:
        kwargs["model"] = args.model
        kwargs["planner"] = args.planner
        if getattr(args, "review_plan", False):
            kwargs["approve_plan"] = _review_plan
    return default_orchestrator(**kwargs)


def _validate_run_args(args: argparse.Namespace) -> str | None:
    if args.timeout < 30:
        return "--timeout must be at least 30 seconds"
    if args.escalate_after < 0:
        return "--escalate-after cannot be negative"
    if args.max_minutes is not None and args.max_minutes <= 0:
        return "--max-minutes must be positive"
    if args.max_cost_usd is not None and args.max_cost_usd <= 0:
        return "--max-cost-usd must be positive"
    return None


def _build(args: argparse.Namespace) -> int:
    problem = _validate_run_args(args)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    spec = None
    if args.spec:
        try:
            spec = TaskSpec.from_dict(json.loads(args.spec.read_text(encoding="utf-8")))
        except (OSError, ValueError, SpecValidationError) as exc:
            print(f"Cannot use --spec {args.spec}: {exc}", file=sys.stderr)
            return EXIT_USAGE
    output = args.output
    if output is None:
        root = Path(os.environ.get("ROCTO_OUTPUT_ROOT") or "rocto-builds")
        output = default_output_dir(args.idea, root)
    try:
        orchestrator = _orchestrator_for(args, planning=True)
    except PlanningError as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return _run(args, lambda: orchestrator.build(args.idea, output, args.model, spec=spec), output)


def _resume(args: argparse.Namespace) -> int:
    problem = _validate_run_args(args)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    project = _project_or_last(args.project)
    if project is None:
        return EXIT_USAGE
    planner = _stored_planner(project)
    try:
        orchestrator = default_orchestrator(
            max_retries=args.max_retries,
            timeout=args.timeout,
            backend=args.executor,
            on_event=_make_reporter(args.quiet or args.json_events),
            on_record=_json_printer if args.json_events else None,
            escalate_after=args.escalate_after,
            max_minutes=args.max_minutes,
            max_cost_usd=args.max_cost_usd,
            ledger=Ledger.default(),
            planner=planner,
        )
    except PlanningError as exc:
        print(f"Resume failed: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return _run(args, lambda: orchestrator.resume(project), project)


def _refine(args: argparse.Namespace) -> int:
    problem = _validate_run_args(args)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    project = _project_or_last(args.project)
    if project is None:
        return EXIT_USAGE
    try:
        orchestrator = _orchestrator_for(args, planning=True)
    except PlanningError as exc:
        print(f"Refine failed: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return _run(
        args,
        lambda: orchestrator.refine(project, args.request, keep_failed=args.keep_failed),
        project,
    )


def _stored_planner(project: Path) -> str | None:
    """Re-plan with the same kind of planner the build started with."""
    try:
        state = StateStore(project.expanduser().resolve()).load()
    except (OSError, ValueError, TypeError):
        return None
    return state.planner if state.planner in PLANNER_CHOICES else None


def _run(args: argparse.Namespace, action: Callable[[], Any], project: Path) -> int:
    project = project.expanduser().resolve()
    exit_code = 0
    message = ""
    try:
        action()
    except BuildError as exc:
        exit_code = exc.exit_code
        message = str(exc)
    except KeyboardInterrupt:
        exit_code = EXIT_INTERRUPTED
        message = "Interrupted."
    finally:
        if (project / ".rocto" / "run.json").is_file():
            _remember_last_build(project)

    has_state = (project / ".rocto" / "run.json").is_file()
    if args.json_events:
        payload: dict[str, Any] = {"type": "result", "exit_code": exit_code, "project": str(project)}
        if has_state:
            summary = collect(project)
            payload.update(
                verdict=summary.verdict,
                checks_passed=summary.checks_passed,
                checks_total=summary.checks_total,
                report=str(project / REPORT_NAME),
                next_step=summary.next_step(),
            )
        if message:
            payload["message"] = message
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    else:
        if has_state:
            print(render_text(collect(project)))
        if message and (exit_code != 0):
            print(f"\n{message}", file=sys.stderr)
    if args.open and has_state and (project / REPORT_NAME).is_file():
        _open_path(project / REPORT_NAME)
    return exit_code


def _review_plan(spec: TaskSpec, warnings: tuple[str, ...]) -> TaskSpec | None:
    """Human gate between planning and generation (--review-plan)."""
    if not sys.stdin.isatty():
        print(
            "--review-plan needs an interactive terminal; continuing without review.",
            file=sys.stderr,
        )
        return spec
    while True:
        _print_plan(spec, warnings)
        try:
            answer = input("\nBuild this? [Y]es / [e]dit / [n]o: ").strip().lower()
        except EOFError:
            return None
        if answer in {"", "y", "yes"}:
            return spec
        if answer in {"n", "no", "q"}:
            return None
        if answer in {"e", "edit"}:
            edited = _edit_spec(spec)
            if edited is not None:
                spec = edited
                warnings = check_contract(spec).warnings


def _print_plan(spec: TaskSpec, warnings: tuple[str, ...]) -> None:
    print(f"\n=== Plan: {spec.title} ===")
    print(spec.goal)
    print("\nFeatures:")
    for feature in spec.features:
        print(f"  - {feature}")
    print("\nTests:")
    for test in spec.tests:
        print(f"  {test.name}")
        for step in test.steps:
            print(f"    · {describe_step(step.__dict__)}")
    if warnings:
        print("\nNot verified by these tests:")
        for warning in warnings:
            print(f"  ! {warning}")


def _edit_spec(spec: TaskSpec) -> TaskSpec | None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if os.name == "nt" else ("nano" if shutil.which("nano") else "vi")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(spec.to_dict(), handle, ensure_ascii=False, indent=2)
        path = Path(handle.name)
    try:
        subprocess.run([*shlex.split(editor, posix=os.name != "nt"), str(path)], check=False)
        data = json.loads(path.read_text(encoding="utf-8"))
        edited = TaskSpec.from_dict(data)
    except (OSError, ValueError, SpecValidationError) as exc:
        print(f"\nThat edit cannot be used: {exc}", file=sys.stderr)
        return None
    finally:
        path.unlink(missing_ok=True)
    report = check_contract(edited)
    if not report.ok:
        print("\nThe edited plan is not a usable contract:\n" + report.feedback(), file=sys.stderr)
        return None
    return edited


# ------------------------------------------------------------- build pointers


def _remember_last_build(project: Path) -> None:
    try:
        home = rocto_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / LAST_BUILD_FILE).write_text(str(project), encoding="utf-8")
    except OSError:
        pass


def _last_build() -> Path | None:
    try:
        text = (rocto_home() / LAST_BUILD_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def _project_or_last(project: Path | None) -> Path | None:
    if project is not None:
        return project.expanduser().resolve()
    last = _last_build()
    if last is None or not last.is_dir():
        print(
            "No build directory given and no previous build recorded. "
            "Pass the directory, e.g. rocto status rocto-builds/<name>",
            file=sys.stderr,
        )
        return None
    return last


# ------------------------------------------------------------ inspect builds


def _status(args: argparse.Namespace) -> int:
    project = _project_or_last(args.project)
    if project is None:
        return EXIT_USAGE
    store = StateStore(project)
    if not store.path.is_file():
        print(f"No Rainbow Octopus state found at {store.path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        state = store.load()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Cannot read run state: {exc}", file=sys.stderr)
        return EXIT_USAGE
    summary = collect(project)
    if args.json:
        payload = state.to_dict()
        payload["next_step"] = summary.next_step()
        payload["verdict"] = summary.verdict
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"project:     {project}")
    print(f"result:      {summary.headline}")
    print(f"phase:       {state.phase}")
    print(f"attempt:     {state.attempt}")
    print(f"planner:     {state.planner or '-'}")
    if state.cost_usd:
        print(f"spent:       ${state.cost_usd:.2f}")
    print(f"started_at:  {state.started_at}")
    print(f"updated_at:  {state.updated_at}")
    if state.error and summary.verdict != "passed":
        first = state.error.strip().splitlines()[0] if state.error.strip() else ""
        print(f"error:       {first[:200]}")
    for record in state.attempts:
        print(
            f"  #{record.get('attempt')}: {record.get('executor') or '-':9} "
            f"{record.get('outcome')}"
            + (
                f" ({record.get('checks_passed')}/{record.get('checks_total')})"
                if record.get("checks_total") else ""
            )
        )
    print(f"next:        {summary.next_step()}")
    return 0


def _report(args: argparse.Namespace) -> int:
    project = _project_or_last(args.project)
    if project is None:
        return EXIT_USAGE
    if not (project / ".rocto" / "run.json").is_file():
        print(f"No Rainbow Octopus build at {project}", file=sys.stderr)
        return EXIT_USAGE
    if args.format == "html":
        print(write_html_report(project))
        return 0
    summary = collect(project)
    if args.format == "markdown":
        print(render_markdown(summary, args.screenshot_url))
    else:
        print(render_text(summary))
    return 0


def _open(args: argparse.Namespace) -> int:
    project = _project_or_last(args.project)
    if project is None:
        return EXIT_USAGE
    if args.site:
        target = project / "index.html"
    else:
        target = project / REPORT_NAME
        if not target.is_file() and (project / ".rocto" / "run.json").is_file():
            target = write_html_report(project)
    if not target.is_file():
        print(f"Nothing to open at {target}", file=sys.stderr)
        return EXIT_USAGE
    _open_path(target)
    print(target)
    return 0


def _open_path(path: Path) -> None:
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception:  # noqa: BLE001 - no browser is not an error worth failing on
        pass


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def _serve(args: argparse.Namespace) -> int:
    project = _project_or_last(args.project)
    if project is None:
        return EXIT_USAGE
    if not (project / "index.html").is_file():
        print(f"No index.html in {project}", file=sys.stderr)
        return EXIT_USAGE
    handler = partial(_QuietStaticHandler, directory=str(project))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    print(f"Serving {project}\n  {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------- config


def _config(args: argparse.Namespace) -> int:
    action = args.config_command or "show"
    if action == "path":
        print(f"user:    {user_config_path()}")
        print(f"project: {project_config_path()}")
        print(f"data:    {rocto_home()}")
        return 0
    if action in {"set", "unset"}:
        path = project_config_path() if args.project else user_config_path()
        try:
            set_config_value(path, args.key, args.value if action == "set" else None)
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(f"{'Set' if action == 'set' else 'Removed'} {args.key} in {path}")
        return 0
    try:
        loaded = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    rows = describe(loaded, applied=_CONFIG_SOURCES)
    width = max(len(key) for key, _, _ in rows)
    for key, value, source in rows:
        print(f"{key:<{width}}  {value or '-':<34}  {source}")
    files = ", ".join(str(p) for p in loaded.files) or "none"
    print(f"\nconfig files: {files}")
    return 0


def _init(args: argparse.Namespace) -> int:
    path = project_config_path()
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_USAGE
    path.write_text(template(), encoding="utf-8")
    print(f"Wrote {path}. Uncomment the settings you want to change.")
    return 0


# ----------------------------------------------------------------------- stats


def _stats(args: argparse.Namespace) -> int:
    ledger = Ledger.default()
    summary = summarize(ledger.entries())
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not summary["builds"]:
        print(f"No builds recorded yet ({ledger.path}).")
        return 0

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value * 100:.0f}%"

    print(f"builds:                  {summary['builds']} ({summary['completed']} completed, {pct(summary['success_rate'])})")
    print(f"first attempt passes:    {pct(summary['first_attempt_pass_rate'])}")
    if summary["median_build_seconds"] is not None:
        print(f"median build time:       {summary['median_build_seconds']:.0f}s")
    if summary["total_cost_usd"]:
        print(f"reported spend:          ${summary['total_cost_usd']:.2f}")
    print()
    print(f"{'executor':<10} {'attempts':>8} {'checked':>8} {'passed':>8} {'pass rate':>9} {'crashed':>8} {'median':>8} {'cost':>8}")
    for name, row in summary["executors"].items():
        median_text = "-" if row["median_seconds"] is None else f"{row['median_seconds']:.0f}s"
        print(
            f"{name:<10} {row['attempts']:>8} {row['verified']:>8} {row['passed']:>8} "
            f"{pct(row['pass_rate']):>9} {row['execution_failures']:>8} "
            f"{median_text:>8} {'$%.2f' % row['cost_usd']:>8}"
        )
    print(f"\nledger: {ledger.path}")
    return 0


# ---------------------------------------------------------------- automation


def _batch(args: argparse.Namespace) -> int:
    from .batch import load_cases, run_batch

    problem = _validate_run_args(args)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    try:
        cases = load_cases(args.file)
    except (OSError, ValueError) as exc:
        print(f"Cannot read {args.file}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    root = args.output_root or Path(os.environ.get("ROCTO_OUTPUT_ROOT") or "rocto-builds")
    summary = run_batch(
        cases,
        root,
        lambda: _orchestrator_for(args, planning=True),
        model=args.model,
        min_verified=args.min_verified,
        echo=not (args.json or args.json_events),
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["gate_passed"] else 1


def _gallery(args: argparse.Namespace) -> int:
    from .gallery import build_gallery

    try:
        index = build_gallery(
            args.source, args.output, title=args.title, include_failed=args.include_failed
        )
    except (OSError, ValueError) as exc:
        print(f"Cannot build gallery: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
