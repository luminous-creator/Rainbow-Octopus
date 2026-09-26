"""ADR-005 checkpoints and resume, ADR-006 escalation and budgets, ADR-008 refine."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from helpers import sample_spec, write_sample_site
from rainbow_octopus.executor import ExecutionError, RouterExecutor
from rainbow_octopus.ledger import Ledger, summarize
from rainbow_octopus.models import AcceptanceCheck, AcceptanceReport, TaskSpec
from rainbow_octopus.orchestrator import (
    EXIT_BUDGET,
    EXIT_CANCELLED,
    EXIT_FAILED,
    EXIT_USAGE,
    BuildError,
    Orchestrator,
    default_output_dir,
    slugify,
)
from rainbow_octopus.state import StateStore


PASS = AcceptanceReport(True, [AcceptanceCheck("increments:text_visible", True, "expected=1; actual=1")])
FAIL = AcceptanceReport(False, [AcceptanceCheck("increments:text_visible", False, "expected=1; actual=0")])


class Planner:
    label = "api"

    def __init__(self, spec: TaskSpec | None = None):
        self.calls = 0
        self.refined: list[str] = []
        self.spec = spec or sample_spec()
        self.last_cost_usd = 0.01

    def plan(self, idea):
        self.calls += 1
        return self.spec

    def refine(self, spec, request):
        self.refined.append(request)
        data = spec.to_dict()
        data["title"] = "Counter v2"
        return TaskSpec.from_dict(data)


class Executor:
    """Writes the sample site; scripted to raise on chosen attempts."""

    def __init__(self, raise_on=None, name="deepseek"):
        self.raise_on = raise_on or {}
        self.calls: list[tuple] = []
        self.last_used = name
        self.last_cost_usd = 0.1

    def execute(self, project_dir, spec, attempt, previous_failure=None, **extra):
        self.calls.append((attempt, previous_failure, extra))
        error = self.raise_on.get(attempt)
        if error is not None:
            raise error
        write_sample_site(project_dir)
        (project_dir / "index.html").write_text(
            (project_dir / "index.html").read_text(encoding="utf-8") + f"<!-- attempt {attempt} -->",
            encoding="utf-8",
        )


class Verifier:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def verify(self, project_dir, spec):
        self.calls += 1
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "site"

    def tearDown(self):
        self._tmp.cleanup()

    def state(self):
        return StateStore(self.out).load()


class ResumeTests(PipelineTestCase):
    def test_interrupt_is_recorded_and_resume_finishes_the_job(self):
        planner = Planner()
        executor = Executor(raise_on={2: KeyboardInterrupt()})
        first = Orchestrator(planner, executor, Verifier([FAIL]), max_retries=2)
        with self.assertRaises(KeyboardInterrupt):
            first.build("counter", self.out, "m")
        state = self.state()
        self.assertEqual(state.phase, "interrupted")
        self.assertEqual([a["outcome"] for a in state.attempts], ["verification_failed", "interrupted"])

        executor2 = Executor()
        second = Orchestrator(planner, executor2, Verifier([PASS]), max_retries=2)
        report = second.resume(self.out)
        self.assertTrue(report.passed)
        self.assertEqual(planner.calls, 1, "resume must not pay for the plan again")
        attempt, failure, _ = executor2.calls[0]
        self.assertEqual(attempt, 3, "attempt numbers continue, so logs are not overwritten")
        self.assertIn("actual=0", failure, "the repair still gets the last failure evidence")
        self.assertEqual(self.state().phase, "completed")

    def test_a_written_but_unverified_page_is_verified_before_regenerating(self):
        executor = Executor()
        crashing = Orchestrator(Planner(), executor, Verifier([KeyboardInterrupt()]), max_retries=0)
        with self.assertRaises(KeyboardInterrupt):
            crashing.build("counter", self.out, "m")
        self.assertEqual(self.state().attempts[-1]["outcome"], "executed")

        executor2 = Executor()
        verifier = Verifier([PASS])
        Orchestrator(Planner(), executor2, verifier, max_retries=0).resume(self.out)
        self.assertEqual(executor2.calls, [], "the existing page passed; nothing is regenerated")
        self.assertEqual(verifier.calls, 1)

    def test_resume_replans_when_planning_never_finished(self):
        class BrokenPlanner(Planner):
            def plan(self, idea):
                raise RuntimeError("provider down")

        with self.assertRaises(BuildError) as caught:
            Orchestrator(BrokenPlanner(), Executor(), Verifier([]), 0).build("counter", self.out, "m")
        self.assertEqual(caught.exception.exit_code, 3)

        planner = Planner()
        Orchestrator(planner, Executor(), Verifier([PASS]), 0).resume(self.out)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(self.state().idea, "counter")

    def test_resuming_a_completed_build_is_a_no_op(self):
        Orchestrator(Planner(), Executor(), Verifier([PASS]), 0).build("counter", self.out, "m")
        executor = Executor()
        report = Orchestrator(Planner(), executor, Verifier([]), 0).resume(self.out)
        self.assertTrue(report.passed)
        self.assertEqual(executor.calls, [])

    def test_resume_of_a_directory_that_is_not_a_build(self):
        self.out.mkdir()
        with self.assertRaises(BuildError) as caught:
            Orchestrator(Planner(), Executor(), Verifier([]), 0).resume(self.out)
        self.assertEqual(caught.exception.exit_code, EXIT_USAGE)

    def test_a_verifier_crash_becomes_a_resumable_failure(self):
        orchestrator = Orchestrator(Planner(), Executor(), Verifier([ValueError("boom")]), 0)
        with self.assertRaises(BuildError) as caught:
            orchestrator.build("counter", self.out, "m")
        self.assertIn("boom", str(caught.exception))
        self.assertTrue((self.out / ".rocto" / "logs" / "crash.txt").is_file())
        self.assertEqual(self.state().phase, "failed")


class ArtifactTests(PipelineTestCase):
    def test_events_report_and_costs_are_recorded(self):
        records = []
        orchestrator = Orchestrator(
            Planner(), Executor(), Verifier([FAIL, PASS]), 2, on_record=records.append
        )
        orchestrator.build("counter", self.out, "m")
        lines = (self.out / ".rocto" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        phases = [json.loads(line)["phase"] for line in lines]
        self.assertEqual(phases, [r["phase"] for r in records])
        self.assertIn("verification_failed", phases)
        self.assertEqual(phases[-1], "completed")
        self.assertTrue((self.out / "report.html").is_file())
        state = self.state()
        self.assertAlmostEqual(state.cost_usd, 0.21)  # planner 0.01 + two attempts
        self.assertEqual(state.planner, "api")
        self.assertEqual(state.attempts[0]["failed_checks"], ["increments:text_visible"])

    def test_provided_spec_skips_the_planner(self):
        planner = Planner()
        Orchestrator(planner, Executor(), Verifier([PASS]), 0).build(
            "counter", self.out, "m", spec=sample_spec()
        )
        self.assertEqual(planner.calls, 0)
        self.assertEqual(self.state().planner, "provided")

    def test_ledger_records_each_session_once(self):
        ledger = Ledger(self.root / "ledger.jsonl")
        executor = Executor(raise_on={2: KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            Orchestrator(Planner(), executor, Verifier([FAIL]), 2, ledger=ledger).build("c", self.out, "m")
        Orchestrator(Planner(), Executor(), Verifier([PASS]), 2, ledger=ledger).resume(self.out)
        entries = ledger.entries()
        self.assertEqual([e["outcome"] for e in entries], ["interrupted", "completed"])
        self.assertEqual([a["attempt"] for a in entries[1]["attempts"]], [3])
        self.assertNotIn("idea", entries[0])
        summary = summarize(entries)
        self.assertEqual(summary["builds"], 2)
        self.assertEqual(summary["executors"]["deepseek"]["verified"], 2)
        self.assertEqual(summary["executors"]["deepseek"]["pass_rate"], 0.5)


class GuardTests(PipelineTestCase):
    def test_time_budget_stops_before_a_new_attempt(self):
        clock = Clock()

        class SlowVerifier(Verifier):
            def verify(self, project_dir, spec):
                clock.now += 120
                return super().verify(project_dir, spec)

        orchestrator = Orchestrator(
            Planner(), Executor(), SlowVerifier([FAIL, FAIL, FAIL]), 2, max_minutes=1.5, clock=clock
        )
        with self.assertRaises(BuildError) as caught:
            orchestrator.build("counter", self.out, "m")
        self.assertEqual(caught.exception.exit_code, EXIT_BUDGET)
        state = self.state()
        self.assertEqual(state.phase, "stopped")
        self.assertEqual(len(state.attempts), 1)

    def test_cost_budget(self):
        orchestrator = Orchestrator(Planner(), Executor(), Verifier([FAIL] * 3), 2, max_cost_usd=0.15)
        with self.assertRaises(BuildError) as caught:
            orchestrator.build("counter", self.out, "m")
        self.assertEqual(caught.exception.exit_code, EXIT_BUDGET)
        self.assertEqual(len(self.state().attempts), 2)

    def test_plan_review_can_cancel_before_anything_is_generated(self):
        executor = Executor()
        seen = []
        orchestrator = Orchestrator(
            Planner(), executor, Verifier([]), 0,
            approve_plan=lambda spec, warnings: seen.append(spec) or None,
        )
        with self.assertRaises(BuildError) as caught:
            orchestrator.build("counter", self.out, "m")
        self.assertEqual(caught.exception.exit_code, EXIT_CANCELLED)
        self.assertEqual(executor.calls, [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.state().phase, "cancelled")

    def test_plan_review_can_replace_the_spec(self):
        data = sample_spec().to_dict()
        data["title"] = "Edited"
        edited = TaskSpec.from_dict(data)
        Orchestrator(
            Planner(), Executor(), Verifier([PASS]), 0, approve_plan=lambda spec, w: edited
        ).build("counter", self.out, "m")
        saved = json.loads((self.out / ".rocto" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["title"], "Edited")

    def test_retry_limit_is_four(self):
        Orchestrator(Planner(), Executor(), Verifier([]), max_retries=4)
        with self.assertRaises(ValueError):
            Orchestrator(Planner(), Executor(), Verifier([]), max_retries=5)


class EscalationTests(PipelineTestCase):
    def _router(self, escalate_after):
        claude = Executor(name="claude")
        deepseek = Executor(name="deepseek")
        for backend in (claude, deepseek):
            backend.healthcheck = lambda: (True, "ok")
        router = RouterExecutor([("claude", claude), ("deepseek", deepseek)], escalate_after=escalate_after)
        return router, claude, deepseek

    def test_repeated_verification_failures_hand_the_repair_to_the_next_backend(self):
        router, claude, deepseek = self._router(escalate_after=2)
        Orchestrator(Planner(), router, Verifier([FAIL, FAIL, PASS]), 2).build("c", self.out, "m")
        self.assertEqual([c[0] for c in claude.calls], [1, 2])
        self.assertEqual([c[0] for c in deepseek.calls], [3])
        self.assertIn("actual=0", deepseek.calls[0][1])
        log = json.loads((self.out / ".rocto" / "logs" / "router-attempt-3.json").read_text(encoding="utf-8"))
        self.assertEqual(log["winner"], "deepseek")
        self.assertIn("claude (2 failed verifications in a row)", log["demoted"])
        self.assertEqual([a["executor"] for a in self.state().attempts], ["claude", "claude", "deepseek"])

    def test_zero_disables_escalation(self):
        router, claude, deepseek = self._router(escalate_after=0)
        Orchestrator(Planner(), router, Verifier([FAIL, FAIL, PASS]), 2).build("c", self.out, "m")
        self.assertEqual(len(claude.calls), 3)
        self.assertEqual(deepseek.calls, [])

    def test_a_demoted_backend_still_runs_when_it_is_the_only_one_left(self):
        router, claude, deepseek = self._router(escalate_after=1)
        deepseek.healthcheck = lambda: (False, "no key")
        Orchestrator(Planner(), router, Verifier([FAIL, PASS]), 1).build("c", self.out, "m")
        self.assertEqual(len(claude.calls), 2)

    def test_resume_remembers_strikes(self):
        router, claude, deepseek = self._router(escalate_after=1)
        orchestrator = Orchestrator(Planner(), router, Verifier([FAIL]), 0)
        with self.assertRaises(BuildError):
            orchestrator.build("c", self.out, "m")
        router2, claude2, deepseek2 = self._router(escalate_after=1)
        Orchestrator(Planner(), router2, Verifier([PASS]), 0).resume(self.out)
        self.assertEqual(claude2.calls, [])
        self.assertEqual(len(deepseek2.calls), 1)

    def test_router_propagates_notify_and_cost(self):
        router, claude, deepseek = self._router(escalate_after=2)
        claude.notify = None
        def sink(message):
            pass

        router.notify = sink
        self.assertIs(claude.notify, sink)
        with tempfile.TemporaryDirectory() as tmp:
            router.execute(Path(tmp), sample_spec(), 1)
        self.assertAlmostEqual(router.last_cost_usd, 0.1)


class RefineTests(PipelineTestCase):
    def build(self):
        Orchestrator(Planner(), Executor(), Verifier([PASS]), 0).build("counter", self.out, "m")

    def test_a_passing_change_becomes_a_new_revision(self):
        self.build()
        planner = Planner()
        executor = Executor()
        Orchestrator(planner, executor, Verifier([PASS]), 0).refine(self.out, "add dark mode")
        state = self.state()
        self.assertEqual(state.revision, 1)
        self.assertEqual(state.changes, ["add dark mode"])
        self.assertEqual(state.kind, "refine")
        self.assertEqual(executor.calls[0][2], {"change_request": "add dark mode"})
        self.assertEqual(executor.calls[0][0], 2, "attempt numbers continue across revisions")
        snapshot = self.out / ".rocto" / "history" / "rev-0"
        self.assertIn("attempt 1", (snapshot / "index.html").read_text(encoding="utf-8"))
        self.assertIn("Counter v2", (self.out / ".rocto" / "task.json").read_text(encoding="utf-8"))

    def test_a_failing_change_is_rolled_back(self):
        self.build()
        original = (self.out / "index.html").read_text(encoding="utf-8")
        orchestrator = Orchestrator(Planner(), Executor(), Verifier([FAIL, FAIL]), 1)
        with self.assertRaises(BuildError) as caught:
            orchestrator.refine(self.out, "break it")
        self.assertEqual(caught.exception.exit_code, EXIT_FAILED)
        self.assertIn("restored", str(caught.exception))
        self.assertEqual((self.out / "index.html").read_text(encoding="utf-8"), original)
        self.assertNotIn("v2", (self.out / ".rocto" / "task.json").read_text(encoding="utf-8"))
        report = json.loads((self.out / "acceptance-report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["passed"])
        state = self.state()
        self.assertEqual(state.phase, "completed")
        self.assertEqual(state.revision, 0)
        self.assertIn("rolled back", state.error)

    def test_keep_failed_leaves_the_change_in_place(self):
        self.build()
        orchestrator = Orchestrator(Planner(), Executor(), Verifier([FAIL]), 0)
        with self.assertRaises(BuildError):
            orchestrator.refine(self.out, "break it", keep_failed=True)
        self.assertEqual(self.state().phase, "failed")
        self.assertIn("attempt 2", (self.out / "index.html").read_text(encoding="utf-8"))

    def test_only_a_passing_build_can_be_refined(self):
        with self.assertRaises(BuildError):
            Orchestrator(Planner(), Executor(), Verifier([FAIL]), 0).build("c", self.out, "m")
        with self.assertRaises(BuildError) as caught:
            Orchestrator(Planner(), Executor(), Verifier([]), 0).refine(self.out, "x")
        self.assertEqual(caught.exception.exit_code, EXIT_USAGE)
        self.assertIn("rocto resume", str(caught.exception))

    def test_cancelled_review_keeps_the_build_refinable(self):
        self.build()
        orchestrator = Orchestrator(
            Planner(), Executor(), Verifier([]), 0, approve_plan=lambda s, w: None
        )
        with self.assertRaises(BuildError):
            orchestrator.refine(self.out, "x")
        self.assertEqual(self.state().phase, "completed")


class OutputDirTests(unittest.TestCase):
    def test_slug_keeps_ascii_words_only(self):
        self.assertEqual(slugify("A Pomodoro timer, with STATS!"), "a-pomodoro-timer-with-stats")
        self.assertEqual(slugify("做一个番茄钟"), "")

    def test_default_dir_is_timestamped_and_unique(self):
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 9, 26, 8, 30, 0)
            first = default_output_dir("做一个番茄钟", root, now)
            self.assertEqual(first.name, "20260926-083000-build")
            first.mkdir()
            second = default_output_dir("做一个番茄钟", root, now)
            self.assertEqual(second.name, "20260926-083000-build-2")


if __name__ == "__main__":
    unittest.main()
