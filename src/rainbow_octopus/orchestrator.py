"""The build pipeline: plan → generate → verify → repair, with checkpoints.

ADR-005: the pipeline used to be one linear function. Anything that stopped
it — Ctrl+C, a closed laptop, a rate limit, a verifier that crashed — left
``run.json`` claiming a phase that was no longer running, and the only way
forward was a fresh build in a fresh directory, paying for the plan again.

Every stage now ends in a checkpoint written to ``.rocto/run.json``: the
specification in ``task.json``, and one record per generation attempt in
``RunState.attempts``. :meth:`Orchestrator.resume` reads those back and
continues from the first stage that did not finish — re-verifying a page
that was written but never checked, rather than paying to write it again.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
import json
import re
import shutil
import time
import traceback

from .contract import check_contract
from .executor import GENERATED_FILES, make_executor
from .models import AcceptanceReport, SpecValidationError, TaskSpec
from .planner import PlanningError, make_planner
from .state import RunState, StateStore, utc_now, write_json_atomic
from .verifier import BrowserVerifier


class Planner(Protocol):
    def plan(self, idea: str) -> TaskSpec: ...


class Executor(Protocol):
    def execute(
        self,
        project_dir: Path,
        spec: TaskSpec,
        attempt: int,
        previous_failure: str | None = None,
    ): ...


class Verifier(Protocol):
    def verify(self, project_dir: Path, spec: TaskSpec) -> AcceptanceReport: ...


#: Exit codes. Documented in the README; the GitHub workflows rely on them.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PLANNING = 3
EXIT_FAILED = 4
EXIT_BUDGET = 5
EXIT_CANCELLED = 6
EXIT_INTERRUPTED = 130

#: Upper bound for --max-retries. Escalation (ADR-006) makes a fourth and
#: fifth attempt worth having: they can come from a different model.
MAX_RETRIES_LIMIT = 4


class BuildError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


#: Hook for a human gate between planning and generation. Returns the spec to
#: build (possibly edited), or None to cancel.
PlanApprover = Callable[[TaskSpec, tuple[str, ...]], "TaskSpec | None"]


class Orchestrator:
    def __init__(
        self,
        planner: Planner,
        executor: Executor,
        verifier: Verifier,
        max_retries: int = 2,
        on_event: Callable[[str, str], None] | None = None,
        *,
        on_record: Callable[[dict[str, Any]], None] | None = None,
        approve_plan: PlanApprover | None = None,
        max_minutes: float | None = None,
        max_cost_usd: float | None = None,
        ledger: Any = None,
        executor_choice: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not 0 <= max_retries <= MAX_RETRIES_LIMIT:
            raise ValueError(f"max_retries must be between 0 and {MAX_RETRIES_LIMIT}")
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.max_retries = max_retries
        #: Progress callback. A build spends minutes inside one blocking call
        #: to a model, so without this the CLI looks frozen.
        self.on_event = on_event or (lambda phase, detail: None)
        #: Structured twin of on_event, for --json-events and integrations.
        self.on_record = on_record
        self.approve_plan = approve_plan
        self.max_minutes = max_minutes
        self.max_cost_usd = max_cost_usd
        self.ledger = ledger
        self.executor_choice = executor_choice
        self.clock = clock
        self._started = clock()
        self._events_path: Path | None = None
        # Retries inside a planner or executor request surface as progress.
        for component in (planner, executor):
            if hasattr(component, "notify"):
                try:
                    component.notify = lambda message: self._emit("retry", message)
                except AttributeError:
                    pass

    # ------------------------------------------------------------------ events

    def _emit(self, phase: str, detail: str, **data: Any) -> None:
        try:
            self.on_event(phase, detail)
        except Exception:  # noqa: BLE001 - progress reporting must never break a build
            pass
        record = {
            "at": utc_now(),
            "elapsed": round(self.clock() - self._started, 1) if hasattr(self, "clock") else 0,
            "phase": phase,
            "detail": detail,
            **data,
        }
        if getattr(self, "on_record", None):
            try:
                self.on_record(record)
            except Exception:  # noqa: BLE001
                pass
        path = getattr(self, "_events_path", None)
        if path is not None:
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass

    # -------------------------------------------------------------- entrypoints

    def build(
        self,
        idea: str,
        output: Path,
        model: str,
        spec: TaskSpec | None = None,
    ) -> AcceptanceReport:
        """Start a new build in an empty (or new) directory."""
        self._started = self.clock()
        self._session_started_at = utc_now()
        project_dir = prepare_output_directory(output)
        store = StateStore(project_dir)
        state = RunState(
            idea=idea,
            max_retries=self.max_retries,
            model=model,
            executor=self.executor_choice,
        )
        store.initialize(state)
        self._events_path = store.internal_dir / "events.jsonl"
        self._emit("start", f"output: {project_dir}", output=str(project_dir))
        return self._run(project_dir, store, state, spec=spec, fresh=True)

    def resume(self, project: Path) -> AcceptanceReport:
        """Continue a build from its last checkpoint."""
        self._started = self.clock()
        self._session_started_at = utc_now()
        project_dir = project.expanduser().resolve()
        store = StateStore(project_dir)
        if not store.path.is_file():
            raise BuildError(f"No Rainbow Octopus build at {project_dir}", EXIT_USAGE)
        try:
            state = store.load()
        except (OSError, ValueError, TypeError) as exc:
            raise BuildError(f"Cannot read run state: {exc}", EXIT_USAGE) from exc
        self._events_path = store.internal_dir / "events.jsonl"

        if state.phase == "completed":
            report = load_report(project_dir)
            if report is not None and report.passed:
                self._emit("completed", "already complete — nothing to resume")
                return report

        state.max_retries = self.max_retries
        if self.executor_choice:
            state.executor = self.executor_choice
        self._emit(
            "start",
            f"resuming {project_dir} from {state.phase} (attempt {state.attempt})",
            output=str(project_dir),
            resumed_from=state.phase,
        )
        spec = load_spec(project_dir)
        self._replay_strikes(state)
        return self._run(
            project_dir, store, state, spec=spec, fresh=False,
            change_request=state.pending_change,
        )

    def refine(
        self, project: Path, request: str, keep_failed: bool = False
    ) -> AcceptanceReport:
        """Apply a change request to a finished build (ADR-008).

        The last good version is copied to ``.rocto/history/rev-<n>/`` first.
        The planner extends the existing contract — old tests stay, so they
        guard what already worked — the executor edits the site, and the
        whole contract is verified again. If the change cannot be made to
        pass, the last good version is put back: a refine never leaves a
        worse page than it found, unless ``keep_failed`` asks it to.
        """
        self._started = self.clock()
        self._session_started_at = utc_now()
        request = request.strip()
        if not request:
            raise BuildError("The change request is empty", EXIT_USAGE)
        project_dir = project.expanduser().resolve()
        store = StateStore(project_dir)
        if not store.path.is_file():
            raise BuildError(f"No Rainbow Octopus build at {project_dir}", EXIT_USAGE)
        state = store.load()
        spec = load_spec(project_dir)
        report = load_report(project_dir)
        if state.phase != "completed" or spec is None or report is None or not report.passed:
            raise BuildError(
                "Only a build that passed can be refined. Finish it first with: "
                f"rocto resume {project_dir}",
                EXIT_USAGE,
            )
        self._events_path = store.internal_dir / "events.jsonl"
        backup = snapshot_revision(project_dir, state.revision)
        state.max_retries = self.max_retries
        state.kind = "refine"
        state.pending_change = request
        state.last_failure = None
        if self.executor_choice:
            state.executor = self.executor_choice
        self._emit(
            "refining",
            f"revision {state.revision + 1}: {request[:120]} (backup: {backup.name})",
            output=str(project_dir),
        )

        outcome = "failed"
        try:
            try:
                state.transition("planning", f"Planning change: {request[:200]}")
                store.save(state)
                self._emit("planning", f"asking {_planner_label(self.planner)} to extend the plan")
                refine_plan = getattr(self.planner, "refine", None)
                if refine_plan is None:
                    raise PlanningError("this planner cannot plan a change")
                new_spec = refine_plan(spec, request)
                state.add_cost(getattr(self.planner, "last_cost_usd", None))
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                state.add_cost(getattr(self.planner, "last_cost_usd", None))
                state.pending_change = None
                state.transition("completed", "Change planning failed; nothing was modified", str(exc))
                store.save(state)
                self._emit("failed", f"planning the change failed: {exc}")
                outcome = "failed"
                raise BuildError(f"Planning the change failed: {exc}", EXIT_PLANNING) from exc

            warnings = tuple(getattr(self.planner, "last_warnings", ()) or ())
            try:
                new_spec = self._checkpoint_spec(project_dir, store, state, new_spec, warnings)
            except BuildError as exc:
                if exc.exit_code == EXIT_CANCELLED:
                    # Nothing was written yet; the build is still the good one.
                    state.pending_change = None
                    state.transition("completed", "Change cancelled at plan review")
                    store.save(state)
                    outcome = "cancelled"
                raise
            try:
                result = self._attempt_loop(
                    project_dir, store, state, new_spec, change_request=request,
                    verify_first=False,
                )
            except BuildError as exc:
                if keep_failed or exc.exit_code not in {EXIT_FAILED, EXIT_BUDGET}:
                    raise
                restore_revision(project_dir, backup)
                state.pending_change = None
                state.transition(
                    "completed",
                    f"Change failed; restored revision {state.revision}",
                    f"The change request failed and was rolled back: {exc}",
                )
                store.save(state)
                self._emit("restored", f"the change did not pass; restored revision {state.revision}")
                outcome = "rolled_back"
                raise BuildError(
                    f"{exc}\nThe previous version was restored (backup kept in {backup}).",
                    exc.exit_code,
                ) from exc
            state.revision += 1
            state.changes.append(request)
            state.pending_change = None
            state.transition("completed", f"Revision {state.revision} passed")
            store.save(state)
            outcome = "completed"
            return result
        except KeyboardInterrupt:
            outcome = "interrupted"
            previous = state.phase
            state.transition("interrupted", f"Interrupted during {previous}")
            _close_running_attempt(state, "interrupted")
            store.save(state)
            self._emit("interrupted", f"stopped during {previous}; resume with: rocto resume")
            raise
        finally:
            self._write_report(project_dir)
            self._record_ledger(project_dir, state, outcome)

    # ---------------------------------------------------------------- pipeline

    def _run(
        self,
        project_dir: Path,
        store: StateStore,
        state: RunState,
        *,
        spec: TaskSpec | None,
        fresh: bool,
        verify_first: bool | None = None,
        change_request: str | None = None,
    ) -> AcceptanceReport:
        outcome = "failed"
        try:
            if spec is None:
                spec = self._plan(project_dir, store, state)
            elif fresh:
                spec = self._accept_provided_spec(project_dir, store, state, spec)
            report = self._attempt_loop(
                project_dir, store, state, spec,
                verify_first=verify_first, change_request=change_request,
            )
            if change_request and state.pending_change:
                state.revision += 1
                state.changes.append(state.pending_change)
                state.pending_change = None
                store.save(state)
            outcome = "completed"
            return report
        except KeyboardInterrupt:
            outcome = "interrupted"
            previous = state.phase
            state.transition("interrupted", f"Interrupted during {previous}")
            _close_running_attempt(state, "interrupted")
            store.save(state)
            self._emit("interrupted", f"stopped during {previous}; resume with: rocto resume")
            raise
        except BuildError as exc:
            outcome = {
                EXIT_BUDGET: "stopped",
                EXIT_CANCELLED: "cancelled",
            }.get(exc.exit_code, "failed")
            raise
        except Exception as exc:  # noqa: BLE001 - turn a crash into a resumable state
            outcome = "failed"
            crash = store.internal_dir / "logs" / "crash.txt"
            try:
                crash.parent.mkdir(parents=True, exist_ok=True)
                crash.write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
            phase = state.phase
            state.transition("failed", f"Internal error during {phase}", str(exc))
            _close_running_attempt(state, "crashed")
            store.save(state)
            self._emit("failed", f"internal error during {phase}: {exc}")
            raise BuildError(
                f"Internal error during {phase}: {exc} (traceback in {crash})",
                EXIT_FAILED,
            ) from exc
        finally:
            self._write_report(project_dir)
            self._record_ledger(project_dir, state, outcome)

    def _plan(self, project_dir: Path, store: StateStore, state: RunState) -> TaskSpec:
        label = _planner_label(self.planner)
        state.planner = getattr(self.planner, "label", None) or state.planner
        try:
            state.transition("planning", "Requesting structured task specification")
            store.save(state)
            self._emit("planning", f"asking {label} for a task specification")
            spec = self.planner.plan(state.idea)
            state.add_cost(getattr(self.planner, "last_cost_usd", None))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            state.add_cost(getattr(self.planner, "last_cost_usd", None))
            state.transition("failed", "Planning failed", str(exc))
            store.save(state)
            self._emit("failed", f"planning failed: {exc}")
            raise BuildError(f"Planning failed: {exc}", EXIT_PLANNING) from exc

        warnings = tuple(getattr(self.planner, "last_warnings", ()) or ())
        return self._checkpoint_spec(project_dir, store, state, spec, warnings)

    def _accept_provided_spec(
        self, project_dir: Path, store: StateStore, state: RunState, spec: TaskSpec
    ) -> TaskSpec:
        report = check_contract(spec)
        if not report.ok:
            state.transition("failed", "Provided specification rejected", report.feedback())
            store.save(state)
            raise BuildError(
                "The provided specification is not a usable contract:\n" + report.feedback(),
                EXIT_USAGE,
            )
        state.planner = "provided"
        return self._checkpoint_spec(project_dir, store, state, spec, report.warnings)

    def _checkpoint_spec(
        self,
        project_dir: Path,
        store: StateStore,
        state: RunState,
        spec: TaskSpec,
        warnings: tuple[str, ...],
    ) -> TaskSpec:
        if self.approve_plan is not None:
            self._emit("review", "waiting for plan approval")
            approved = self.approve_plan(spec, warnings)
            if approved is None:
                state.transition("cancelled", "Plan rejected at review")
                store.save(state)
                self._emit("cancelled", "plan rejected; nothing was generated")
                raise BuildError("Plan rejected at review", EXIT_CANCELLED)
            if approved is not spec:
                spec = approved
                warnings = check_contract(spec).warnings
        write_json_atomic(store.internal_dir / "task.json", spec.to_dict())
        # Coverage the contract does not have. Recorded beside the spec so
        # that a green report cannot imply more than it actually verified.
        warnings_path = store.internal_dir / "contract-warnings.json"
        if warnings:
            write_json_atomic(warnings_path, {"warnings": list(warnings)})
        elif warnings_path.exists():
            warnings_path.unlink()
        state.transition("planned", f"{len(spec.tests)} acceptance tests")
        store.save(state)
        self._emit(
            "planned",
            f"{len(spec.ui_contract)} contract elements, "
            f"{sum(len(t.steps) for t in spec.tests)} assertions",
            title=spec.title,
        )
        for warning in warnings:
            self._emit("contract", warning)
        return spec

    def _attempt_loop(
        self,
        project_dir: Path,
        store: StateStore,
        state: RunState,
        spec: TaskSpec,
        verify_first: bool | None = None,
        change_request: str | None = None,
    ) -> AcceptanceReport:
        last_failure = state.last_failure
        total = self.max_retries + 1
        first = state.attempt + 1

        # On resume, a complete page already on disk is checked before any
        # model is paid to write another: it may never have been verified
        # (interrupted between the two), or it may have failed a check that
        # was wrong (KI-011) or flaky. Verifying costs seconds and nothing.
        if verify_first is None:
            verify_first = _page_on_disk(state, project_dir)
        if verify_first:
            report = self._verify(project_dir, store, state, spec, state.attempt)
            if report.passed:
                return report
            last_failure = state.last_failure

        last_report: AcceptanceReport | None = None
        for number in range(total):
            attempt = first + number
            self._check_budget(store, state)
            state.attempt = attempt
            executor_started = self.clock()
            record: dict[str, Any] = {
                "attempt": attempt,
                "executor": None,
                "outcome": "running",
                "started_at": utc_now(),
                "seconds": None,
                "cost_usd": None,
            }
            state.attempts.append(record)
            state.transition("executing", f"Generation attempt {attempt}")
            store.save(state)
            label = f"attempt {number + 1}/{total}"
            if attempt != number + 1:
                label += f" (#{attempt} overall)"
            self._emit("executing", f"{label} — writing the site", attempt=attempt)
            extra = {"change_request": change_request} if change_request else {}
            try:
                self.executor.execute(project_dir, spec, attempt, last_failure, **extra)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_failure = str(exc)
                cost = getattr(self.executor, "last_cost_usd", None)
                record.update(
                    outcome="execution_failed",
                    seconds=round(self.clock() - executor_started, 1),
                    cost_usd=cost,
                    executor=getattr(self.executor, "last_used", None),
                    failure=last_failure[:2000],
                )
                state.add_cost(cost)
                state.last_failure = last_failure
                state.transition("execution_failed", f"Attempt {attempt}", last_failure)
                store.save(state)
                self._emit("execution_failed", last_failure[:200], attempt=attempt)
                continue

            chosen = getattr(self.executor, "last_used", None)
            cost = getattr(self.executor, "last_cost_usd", None)
            record.update(
                outcome="executed",
                executor=chosen,
                seconds=round(self.clock() - executor_started, 1),
                cost_usd=cost,
            )
            state.add_cost(cost)
            state.transition("executed", f"Attempt {attempt} wrote the site")
            store.save(state)
            self._emit(
                "executed",
                f"site written{f' by {chosen}' if chosen else ''}",
                attempt=attempt,
                executor=chosen,
            )

            report = self._verify(project_dir, store, state, spec, attempt)
            last_report = report
            if report.passed:
                return report
            last_failure = state.last_failure

        last_failure = state.last_failure or last_failure
        state.transition("failed", "Retries exhausted", last_failure)
        store.save(state)
        if last_report is None:
            raise BuildError(f"Generation failed: {last_failure}", EXIT_FAILED)
        raise BuildError(
            f"Verification failed after {total} attempt{'s' if total != 1 else ''}:\n"
            f"{last_failure}",
            EXIT_FAILED,
        )

    def _verify(
        self,
        project_dir: Path,
        store: StateStore,
        state: RunState,
        spec: TaskSpec,
        attempt: int,
    ) -> AcceptanceReport:
        record = _attempt_record(state, attempt)
        state.transition("verifying", f"Verification after attempt {attempt}")
        store.save(state)
        self._emit("verifying", "checking files, contract, then driving a browser")
        report = self.verifier.verify(project_dir, spec)
        write_json_atomic(project_dir / "acceptance-report.json", report.to_dict())
        passed_count = report.passed_count
        failed = [check.name for check in report.checks if not check.passed]
        executor_name = record.get("executor") if record else None
        recorder = getattr(self.executor, "record_verification", None)
        if recorder and executor_name:
            recorder(executor_name, report.passed)
        if record is not None:
            record.update(
                outcome="passed" if report.passed else "verification_failed",
                checks_passed=passed_count,
                checks_total=len(report.checks),
                failed_checks=failed[:20],
            )
        if report.passed:
            state.last_failure = None
            state.transition("completed", f"Passed on attempt {attempt}")
            store.save(state)
            self._emit(
                "completed",
                f"{passed_count}/{len(report.checks)} checks passed",
                passed=passed_count,
                total=len(report.checks),
            )
            return report

        failure = report.failure_summary()
        state.last_failure = failure
        if record is not None:
            record["failure"] = failure[:2000]
        state.transition("verification_failed", f"Attempt {attempt}", failure)
        store.save(state)
        self._emit(
            "verification_failed",
            f"{passed_count}/{len(report.checks)} passed — {failure[:160]}",
            passed=passed_count,
            total=len(report.checks),
        )
        return report

    # ------------------------------------------------------------------ guards

    def _check_budget(self, store: StateStore, state: RunState) -> None:
        reason = None
        if self.max_minutes is not None:
            minutes = (self.clock() - self._started) / 60
            if minutes >= self.max_minutes:
                reason = f"time budget reached ({minutes:.1f} of {self.max_minutes:g} min)"
        if reason is None and self.max_cost_usd is not None and state.cost_usd is not None:
            if state.cost_usd >= self.max_cost_usd:
                reason = (
                    f"cost budget reached (${state.cost_usd:.2f} of "
                    f"${self.max_cost_usd:.2f})"
                )
        if reason:
            state.transition("stopped", reason, state.last_failure)
            store.save(state)
            self._emit("stopped", f"{reason}; resume with: rocto resume")
            raise BuildError(f"Stopped: {reason}", EXIT_BUDGET)

    def _replay_strikes(self, state: RunState) -> None:
        recorder = getattr(self.executor, "record_verification", None)
        if not recorder:
            return
        for record in state.attempts:
            if record.get("outcome") in {"passed", "verification_failed"}:
                recorder(record.get("executor"), record.get("outcome") == "passed")

    # --------------------------------------------------------------- artifacts

    def _write_report(self, project_dir: Path) -> None:
        try:
            from .report import write_html_report

            write_html_report(project_dir)
        except Exception:  # noqa: BLE001 - a report must never fail a build
            pass

    def _record_ledger(self, project_dir: Path, state: RunState, outcome: str) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.record(
                project_dir,
                state,
                outcome,
                self.clock() - self._started,
                since=getattr(self, "_session_started_at", None),
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a build
            pass


# ---------------------------------------------------------------------- helpers


def _planner_label(planner: Any) -> str:
    describe = getattr(planner, "describe", None)
    if callable(describe):
        try:
            return str(describe())
        except Exception:  # noqa: BLE001
            pass
    return str(getattr(planner, "model", "the planner"))


def _attempt_record(state: RunState, attempt: int) -> dict[str, Any] | None:
    for record in reversed(state.attempts):
        if record.get("attempt") == attempt:
            return record
    return None


def _close_running_attempt(state: RunState, outcome: str) -> None:
    if state.attempts and state.attempts[-1].get("outcome") == "running":
        state.attempts[-1]["outcome"] = outcome


def _page_on_disk(state: RunState, project_dir: Path) -> bool:
    if not state.attempts:
        return False
    return all((project_dir / name).is_file() for name in GENERATED_FILES)


#: What a revision snapshot holds: the site, its contract and its evidence.
SNAPSHOT_FILES = (
    *GENERATED_FILES,
    "screenshot.png",
    "acceptance-report.json",
)
SNAPSHOT_INTERNAL = ("task.json", "contract-warnings.json")


def snapshot_revision(project_dir: Path, revision: int) -> Path:
    target = project_dir / ".rocto" / "history" / f"rev-{revision}"
    if target.exists():
        shutil.rmtree(target)
    (target / ".rocto").mkdir(parents=True)
    for name in SNAPSHOT_FILES:
        source = project_dir / name
        if source.is_file():
            shutil.copy2(source, target / name)
    for name in SNAPSHOT_INTERNAL:
        source = project_dir / ".rocto" / name
        if source.is_file():
            shutil.copy2(source, target / ".rocto" / name)
    return target


def restore_revision(project_dir: Path, snapshot: Path) -> None:
    for name in SNAPSHOT_FILES:
        current = project_dir / name
        saved = snapshot / name
        if saved.is_file():
            shutil.copy2(saved, current)
        elif current.exists():
            current.unlink()
    for name in SNAPSHOT_INTERNAL:
        current = project_dir / ".rocto" / name
        saved = snapshot / ".rocto" / name
        if saved.is_file():
            shutil.copy2(saved, current)
        elif current.exists():
            current.unlink()


def load_spec(project_dir: Path) -> TaskSpec | None:
    path = project_dir / ".rocto" / "task.json"
    if not path.is_file():
        return None
    try:
        return TaskSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, SpecValidationError, TypeError):
        return None


def load_report(project_dir: Path) -> AcceptanceReport | None:
    path = project_dir / "acceptance-report.json"
    if not path.is_file():
        return None
    try:
        return AcceptanceReport.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def prepare_output_directory(output: Path) -> Path:
    project_dir = output.expanduser().resolve()
    dangerous = {Path(project_dir.anchor).resolve(), Path.home().resolve()}
    if project_dir in dangerous:
        raise BuildError(f"Refusing unsafe output directory: {project_dir}", EXIT_USAGE)
    if project_dir.exists():
        if not project_dir.is_dir():
            raise BuildError(f"Output path is not a directory: {project_dir}", EXIT_USAGE)
        if any(project_dir.iterdir()):
            raise BuildError(f"Output directory is not empty: {project_dir}", EXIT_USAGE)
    else:
        project_dir.mkdir(parents=True)
    return project_dir


def slugify(text: str, limit: int = 40) -> str:
    """ASCII words from an idea, for a directory name. May be empty."""
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    slug = "-".join(words)
    return slug[:limit].strip("-")


def default_output_dir(idea: str, root: Path, now: datetime | None = None) -> Path:
    """``<root>/<YYYYmmdd-HHMMSS>-<slug>``: unique, sortable, shell-safe.

    Choosing and emptying an output directory was the one step of a build a
    person always had to do by hand. Timestamp first so a directory listing
    is a history; ASCII only so the path survives every shell and console
    encoding on Windows.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    slug = slugify(idea) or "build"
    candidate = root / f"{stamp}-{slug}"
    counter = 2
    while candidate.exists():
        candidate = root / f"{stamp}-{slug}-{counter}"
        counter += 1
    return candidate


def default_orchestrator(
    model: str = "deepseek-v4-flash",
    max_retries: int = 2,
    timeout: int = 1200,
    backend: str = "auto",
    on_event: Callable[[str, str], None] | None = None,
    *,
    planner: str | None = None,
    escalate_after: int | None = None,
    **kwargs: Any,
) -> Orchestrator:
    return Orchestrator(
        planner=make_planner(planner, model=model),
        executor=make_executor(backend, timeout=timeout, escalate_after=escalate_after),
        verifier=BrowserVerifier(),
        max_retries=max_retries,
        on_event=on_event,
        executor_choice=backend,
        **kwargs,
    )
