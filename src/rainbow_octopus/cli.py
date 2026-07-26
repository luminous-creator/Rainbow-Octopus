from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import sys
import time

from . import __version__
from .doctor import doctor_as_dict, run_doctor
from .orchestrator import BuildError, default_orchestrator
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rocto",
        description="Turn an idea into a verified static web demo.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local prerequisites")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    build = subparsers.add_parser("build", help="Build and verify a static web demo")
    build.add_argument("idea", help="One-sentence product idea")
    build.add_argument(
        "--output", "-o", required=True, type=Path, help="New or empty output directory"
    )
    build.add_argument(
        "--model",
        default=os.environ.get("ROCTO_DEEPSEEK_MODEL", "deepseek-v4-flash"),
        help="DeepSeek planning model",
    )
    build.add_argument(
        "--max-retries",
        type=int,
        choices=range(0, 3),
        default=2,
        help="Maximum repair retries after the first attempt (0-2)",
    )
    build.add_argument(
        "--executor",
        choices=("auto", "claude", "codex", "deepseek"),
        default=os.environ.get("ROCTO_EXECUTOR", "auto"),
        help=(
            "Code generator backend. 'auto' (default) tries Claude Code, then "
            "Codex, then DeepSeek, skipping any that is not installed or signed in"
        ),
    )
    build.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help="Executor timeout per attempt in seconds",
    )
    build.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress per-phase progress lines",
    )

    status = subparsers.add_parser("status", help="Show a project's latest run state")
    status.add_argument("project", type=Path)
    status.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor(args.json)
    if args.command == "status":
        return _status(args.project, args.json)
    if args.command == "build":
        return _build(args)
    return 2


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
        print("Ready." if payload["passed"] else "Fix failed checks before running build.")
    return 0 if payload["passed"] else 1


def _status(project: Path, as_json: bool) -> int:
    store = StateStore(project.expanduser().resolve())
    if not store.path.is_file():
        print(f"No Rainbow Octopus state found at {store.path}", file=sys.stderr)
        return 2
    try:
        state = store.load()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot read run state: {exc}", file=sys.stderr)
        return 2
    payload = state.to_dict()
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"phase:       {state.phase}")
        print(f"attempt:     {state.attempt}/{state.max_retries + 1}")
        print(f"model:       {state.model}")
        print(f"started_at:  {state.started_at}")
        print(f"updated_at:  {state.updated_at}")
        if state.error:
            print(f"error:       {state.error}")
    return 0


_PHASE_LABEL = {
    "start": "start",
    "planning": "plan",
    "planned": "plan",
    "executing": "build",
    "executed": "build",
    "execution_failed": "build",
    "verifying": "verify",
    "verification_failed": "verify",
    "completed": "done",
    "failed": "failed",
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
        print(f"[{elapsed:5.1f}s] {label:7} {detail}", flush=True)

    return report


def _build(args: argparse.Namespace) -> int:
    if args.timeout < 30:
        print("--timeout must be at least 30 seconds", file=sys.stderr)
        return 2
    try:
        orchestrator = default_orchestrator(
            model=args.model,
            max_retries=args.max_retries,
            timeout=args.timeout,
            backend=args.executor,
            on_event=_make_reporter(args.quiet),
        )
        report = orchestrator.build(args.idea, args.output, args.model)
    except BuildError as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Build interrupted.", file=sys.stderr)
        return 130
    print(f"Build completed: {args.output.resolve()}")
    print(
        f"Checks passed: {sum(check.passed for check in report.checks)}/{len(report.checks)}"
    )
    if report.screenshot:
        print(f"Screenshot: {(args.output / report.screenshot).resolve()}")
    print(f"Report: {(args.output / 'acceptance-report.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

