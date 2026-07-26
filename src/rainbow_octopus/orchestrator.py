from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from .executor import make_executor
from .models import AcceptanceReport, TaskSpec
from .planner import DeepSeekPlanner
from .state import RunState, StateStore, write_json_atomic
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


class BuildError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class Orchestrator:
    def __init__(
        self,
        planner: Planner,
        executor: Executor,
        verifier: Verifier,
        max_retries: int = 2,
        on_event: Callable[[str, str], None] | None = None,
    ):
        if not 0 <= max_retries <= 2:
            raise ValueError("max_retries must be between 0 and 2")
        self.planner = planner
        self.executor = executor
        self.verifier = verifier
        self.max_retries = max_retries
        #: Progress callback. A build spends minutes inside one blocking call
        #: to a model, so without this the CLI looks frozen.
        self.on_event = on_event or (lambda phase, detail: None)

    def _emit(self, phase: str, detail: str) -> None:
        try:
            self.on_event(phase, detail)
        except Exception:  # noqa: BLE001 - progress reporting must never break a build
            pass

    def build(self, idea: str, output: Path, model: str) -> AcceptanceReport:
        project_dir = prepare_output_directory(output)
        store = StateStore(project_dir)
        state = RunState(idea=idea, max_retries=self.max_retries, model=model)
        store.initialize(state)
        self._emit("start", f"output: {project_dir}")

        try:
            state.transition("planning", "Requesting structured task specification")
            store.save(state)
            self._emit("planning", f"asking {model} for a task specification")
            spec = self.planner.plan(idea)
            write_json_atomic(store.internal_dir / "task.json", spec.to_dict())
            state.transition("planned", f"{len(spec.tests)} acceptance tests")
            store.save(state)
            self._emit(
                "planned",
                f"{len(spec.ui_contract)} contract elements, "
                f"{sum(len(t.steps) for t in spec.tests)} assertions",
            )
            # Coverage the contract does not have. Recorded beside the spec so
            # that a green report cannot imply more than it actually verified.
            warnings = tuple(getattr(self.planner, "last_warnings", ()) or ())
            if warnings:
                write_json_atomic(
                    store.internal_dir / "contract-warnings.json",
                    {"warnings": list(warnings)},
                )
                for warning in warnings:
                    self._emit("contract", warning)
        except Exception as exc:
            state.transition("failed", "Planning failed", str(exc))
            store.save(state)
            self._emit("failed", f"planning failed: {exc}")
            raise BuildError(f"Planning failed: {exc}", 3) from exc

        total = self.max_retries + 1
        last_report: AcceptanceReport | None = None
        last_failure: str | None = None
        for attempt in range(1, total + 1):
            state.attempt = attempt
            state.transition("executing", f"Generation attempt {attempt}")
            store.save(state)
            self._emit("executing", f"attempt {attempt}/{total} — writing the site")
            try:
                self.executor.execute(project_dir, spec, attempt, last_failure)
            except Exception as exc:
                last_failure = str(exc)
                state.transition("execution_failed", f"Attempt {attempt}", last_failure)
                store.save(state)
                self._emit("execution_failed", last_failure[:200])
                if attempt > self.max_retries:
                    state.transition("failed", "Generation retries exhausted", last_failure)
                    store.save(state)
                    raise BuildError(f"Generation failed: {last_failure}", 4) from exc
                continue

            chosen = getattr(self.executor, "last_used", None)
            self._emit("executed", f"site written{f' by {chosen}' if chosen else ''}")

            state.transition("verifying", f"Verification after attempt {attempt}")
            store.save(state)
            self._emit("verifying", "checking files, contract, then driving Edge")
            report = self.verifier.verify(project_dir, spec)
            write_json_atomic(project_dir / "acceptance-report.json", report.to_dict())
            last_report = report
            passed_count = sum(check.passed for check in report.checks)
            if report.passed:
                state.transition("completed", f"Passed on attempt {attempt}")
                store.save(state)
                self._emit("completed", f"{passed_count}/{len(report.checks)} checks passed")
                return report

            last_failure = report.failure_summary()
            state.transition("verification_failed", f"Attempt {attempt}", last_failure)
            store.save(state)
            self._emit(
                "verification_failed",
                f"{passed_count}/{len(report.checks)} passed — {last_failure[:160]}",
            )
            if attempt > self.max_retries:
                break

        assert last_report is not None
        state.transition("failed", "Verification retries exhausted", last_failure)
        store.save(state)
        raise BuildError(
            f"Verification failed after {self.max_retries + 1} attempts:\n{last_failure}",
            4,
        )


def prepare_output_directory(output: Path) -> Path:
    project_dir = output.expanduser().resolve()
    dangerous = {Path(project_dir.anchor).resolve(), Path.home().resolve()}
    if project_dir in dangerous:
        raise BuildError(f"Refusing unsafe output directory: {project_dir}", 2)
    if project_dir.exists():
        if not project_dir.is_dir():
            raise BuildError(f"Output path is not a directory: {project_dir}", 2)
        if any(project_dir.iterdir()):
            raise BuildError(f"Output directory is not empty: {project_dir}", 2)
    else:
        project_dir.mkdir(parents=True)
    return project_dir


def default_orchestrator(
    model: str = "deepseek-v4-flash",
    max_retries: int = 2,
    timeout: int = 1200,
    backend: str = "auto",
    on_event: Callable[[str, str], None] | None = None,
) -> Orchestrator:
    return Orchestrator(
        planner=DeepSeekPlanner(model=model),
        executor=make_executor(backend, timeout=timeout),
        verifier=BrowserVerifier(),
        max_retries=max_retries,
        on_event=on_event,
    )

