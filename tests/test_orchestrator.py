from pathlib import Path
import tempfile
import unittest

from rainbow_octopus.models import AcceptanceCheck, AcceptanceReport
from rainbow_octopus.orchestrator import Orchestrator
from rainbow_octopus.state import StateStore
from tests.helpers import sample_spec, write_sample_site


class FakePlanner:
    def plan(self, idea):
        return sample_spec()


class FakeExecutor:
    def __init__(self):
        self.attempts = []

    def execute(self, project_dir, spec, attempt, previous_failure=None):
        self.attempts.append((attempt, previous_failure))
        write_sample_site(project_dir)


class SequenceVerifier:
    def __init__(self, results):
        self.results = iter(results)

    def verify(self, project_dir, spec):
        return next(self.results)


class OrchestratorTests(unittest.TestCase):
    def test_retries_with_failure_evidence_then_completes(self):
        failed = AcceptanceReport(
            False, [AcceptanceCheck("behavior", False, "count stayed zero")]
        )
        passed = AcceptanceReport(True, [AcceptanceCheck("behavior", True, "ok")])
        executor = FakeExecutor()
        orchestrator = Orchestrator(
            FakePlanner(), executor, SequenceVerifier([failed, passed]), max_retries=2
        )
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name) / "site"
            report = orchestrator.build("counter", output, "test-model")
            state = StateStore(output).load()
        self.assertTrue(report.passed)
        self.assertEqual(len(executor.attempts), 2)
        self.assertIn("count stayed zero", executor.attempts[1][1])
        self.assertEqual(state.phase, "completed")


if __name__ == "__main__":
    unittest.main()



class ProgressTests(unittest.TestCase):
    """KI-007: a build blocks for minutes; silence looks like a hang."""

    def _run(self, verifier_passes=True):
        from types import SimpleNamespace
        from rainbow_octopus.models import AcceptanceCheck, AcceptanceReport
        from rainbow_octopus.orchestrator import BuildError, Orchestrator
        from tests.helpers import sample_spec, write_sample_site

        events = []

        class Planner:
            def plan(self, idea):
                return sample_spec()

        class Executor:
            last_used = "deepseek"

            def execute(self, project_dir, spec, attempt, previous_failure=None):
                write_sample_site(project_dir)
                return SimpleNamespace(returncode=0)

        class Verifier:
            def verify(self, project_dir, spec):
                check = AcceptanceCheck("x", verifier_passes, "d")
                return AcceptanceReport(verifier_passes, [check])

        with tempfile.TemporaryDirectory() as tmp:
            orch = Orchestrator(
                Planner(), Executor(), Verifier(), 0,
                on_event=lambda phase, detail: events.append((phase, detail)),
            )
            try:
                orch.build("idea", Path(tmp) / "out", "m")
            except BuildError:
                pass
        return events

    def test_emits_a_phase_for_every_stage(self):
        phases = [phase for phase, _ in self._run()]
        self.assertEqual(
            phases,
            ["start", "planning", "planned", "executing", "executed",
             "verifying", "completed"],
        )

    def test_reports_which_executor_won(self):
        events = dict(self._run())
        self.assertIn("deepseek", events["executed"])

    def test_reports_failure_detail(self):
        events = dict(self._run(verifier_passes=False))
        self.assertIn("verification_failed", events)
        self.assertIn("0/1", events["verification_failed"])

    def test_a_broken_callback_cannot_break_the_build(self):
        from rainbow_octopus.orchestrator import Orchestrator

        def explode(phase, detail):
            raise RuntimeError("reporter is broken")

        orch = Orchestrator.__new__(Orchestrator)
        orch.on_event = explode
        orch._emit("start", "x")  # must not raise


class AutoOrderTests(unittest.TestCase):
    """Quota control: which subscription gets spent first."""

    def _order(self, value):
        import os
        from rainbow_octopus.executor import auto_order

        old = os.environ.get("ROCTO_EXECUTOR_ORDER")
        if value is None:
            os.environ.pop("ROCTO_EXECUTOR_ORDER", None)
        else:
            os.environ["ROCTO_EXECUTOR_ORDER"] = value
        try:
            return auto_order()
        finally:
            os.environ.pop("ROCTO_EXECUTOR_ORDER", None)
            if old is not None:
                os.environ["ROCTO_EXECUTOR_ORDER"] = old

    def test_default_is_claude_first(self):
        self.assertEqual(self._order(None), ("claude", "codex", "deepseek"))

    def test_can_put_the_free_backend_first(self):
        self.assertEqual(self._order("deepseek,codex,claude"),
                         ("deepseek", "codex", "claude"))

    def test_can_drop_a_backend_entirely(self):
        self.assertEqual(self._order("deepseek"), ("deepseek",))

    def test_ignores_unknown_names_and_dedupes(self):
        self.assertEqual(self._order("deepseek, nope, deepseek ,claude"),
                         ("deepseek", "claude"))

    def test_falls_back_when_nothing_valid(self):
        self.assertEqual(self._order("garbage"), ("claude", "codex", "deepseek"))
