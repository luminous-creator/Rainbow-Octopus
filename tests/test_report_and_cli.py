"""ADR-007 reports, and the CLI surface that automation depends on."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
import io
import json
import os
import tempfile
import unittest

from helpers import sample_spec
from rainbow_octopus import cli
from rainbow_octopus.gallery import build_gallery
from rainbow_octopus.batch import load_cases
from rainbow_octopus.models import AcceptanceCheck, AcceptanceReport
from rainbow_octopus.orchestrator import Orchestrator
from rainbow_octopus.report import collect, describe_step, render_html, render_markdown, render_text
from test_pipeline import FAIL, PASS, Executor, Planner, Verifier


def browser_report(passed: bool) -> AcceptanceReport:
    checks = [
        AcceptanceCheck("required_file:index.html", True, "present"),
        AcceptanceCheck("offline_only", True, "no external access detected"),
        AcceptanceCheck("browser_run", True, "completed"),
        AcceptanceCheck("increments:selector_exists", True, '[data-testid="increment"]'),
        AcceptanceCheck("increments:click", True, '[data-testid="increment"]'),
        AcceptanceCheck(
            "increments:text_visible", passed, f"expected=1; actual={'1' if passed else '0'}"
        ),
        AcceptanceCheck("increments:no_console_errors", True, "none"),
    ]
    return AcceptanceReport(passed, checks)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "site"

    def tearDown(self):
        self._tmp.cleanup()

    def _build(self, passed: bool):
        results = [browser_report(passed)]
        orchestrator = Orchestrator(Planner(), Executor(), Verifier(results), 0)
        try:
            orchestrator.build("做一个计数器 <script>", self.out, "m")
        except Exception:  # noqa: BLE001 - a failed build still has a report
            pass
        return collect(self.out)

    def test_steps_are_described_in_plain_words(self):
        summary = self._build(False)
        lines = {line.name: line for group in summary.groups for line in group.lines}
        self.assertEqual(lines["increments:click"].description, "Click [increment]")
        self.assertEqual(lines["increments:text_visible"].description, "[count] shows “1”")
        self.assertEqual(lines["offline_only"].description, "Makes no network requests")
        self.assertEqual(summary.groups[0].title, "Files and safety")
        self.assertEqual(summary.verdict, "failed")
        self.assertIn("rocto resume", summary.next_step())

    def test_text_summary_leads_with_the_verdict_and_what_failed(self):
        text = render_text(self._build(False))
        self.assertIn("FAIL", text.splitlines()[1])
        self.assertIn("What failed", text)
        self.assertIn("actual=0", text)
        self.assertIn("rocto resume", text)

    def test_html_report_is_self_contained_and_escaped(self):
        summary = self._build(True)
        (self.out / "screenshot.png").write_bytes(b"\x89PNG fake")
        html = render_html(collect(self.out))
        self.assertIn("Works — every check passed", html)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn("<script>", html.split("<body>", 1)[1])  # the idea is escaped
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertTrue((self.out / "report.html").is_file(), "the pipeline wrote one too")
        self.assertEqual(summary.verdict, "passed")

    def test_markdown_for_pull_requests(self):
        self._build(True)
        (self.out / "screenshot.png").write_bytes(b"\x89PNG fake")
        markdown = render_markdown(collect(self.out), "https://example.test/shot.png")
        self.assertTrue(markdown.startswith("### ✅"))
        self.assertIn("![screenshot](https://example.test/shot.png)", markdown)
        self.assertIn("<details>", markdown)

    def test_warnings_are_shown_as_not_verified(self):
        self._build(True)
        (self.out / ".rocto" / "contract-warnings.json").write_text(
            json.dumps({"warnings": ["'count' is only ever asserted as '1'"]}), encoding="utf-8"
        )
        summary = collect(self.out)
        self.assertIn("Not verified", render_text(summary))
        self.assertIn("Not verified", render_html(summary))

    def test_describe_step_covers_every_action(self):
        self.assertEqual(describe_step({"action": "wait", "timeout_ms": 500}), "Wait 500 ms")
        self.assertIn("Type", describe_step({"action": "fill", "selector": '[data-testid="a"]', "value": "x"}))
        self.assertIn("aria-pressed", describe_step({
            "action": "attribute_equals", "selector": '[data-testid="a"]',
            "attribute": "aria-pressed", "expected": "true",
        }))
        self.assertEqual(describe_step({"action": "no_console_errors"}), "No errors in the browser console")


class CliTestCase(unittest.TestCase):
    """Runs cli.main() with the real parser and fake pipeline components."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.builds = self.root / "builds"
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {"ROCTO_HOME": str(self.home), "ROCTO_OUTPUT_ROOT": str(self.builds)},
        )
        self._env.start()
        for name in ("ROCTO_EXECUTOR", "ROCTO_MAX_RETRIES", "ROCTO_TIMEOUT", "ROCTO_OPEN", "ROCTO_CONFIG"):
            os.environ.pop(name, None)
        self._old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.results = [PASS]
        self.executor = Executor()
        self.kwargs = {}

        def fake_orchestrator(**kwargs):
            self.kwargs = kwargs
            verifier = Verifier([])
            verifier.results = self.results  # shared queue across a batch
            return Orchestrator(
                Planner(),
                self.executor,
                verifier,
                kwargs.get("max_retries", 0),
                on_event=kwargs.get("on_event"),
                on_record=kwargs.get("on_record"),
                ledger=kwargs.get("ledger"),
                approve_plan=kwargs.get("approve_plan"),
            )

        self._patch = mock.patch("rainbow_octopus.cli.default_orchestrator", fake_orchestrator)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        os.chdir(self._old_cwd)
        self._env.stop()
        self._tmp.cleanup()

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class BuildCommandTests(CliTestCase):
    def test_build_without_output_picks_a_directory_and_remembers_it(self):
        code, out, _ = self.run_cli("build", "a counter app", "-q")
        self.assertEqual(code, 0)
        [project] = list(self.builds.iterdir())
        self.assertTrue(project.name.endswith("-a-counter-app"))
        self.assertIn("PASS", out)
        self.assertIn("report.html", out)
        # status, report and resume default to the last build
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn(str(project), out)
        self.assertIn("next:", out)
        code, out, _ = self.run_cli("report", "--format", "markdown")
        self.assertIn("### ✅", out)

    def test_failed_build_exits_4_and_says_how_to_continue(self):
        self.results = [FAIL]
        code, out, err = self.run_cli("build", "x", "-q", "--max-retries", "0")
        self.assertEqual(code, 4)
        self.assertIn("rocto resume", out)
        self.assertIn("Verification failed", err)

    def test_resume_continues_the_last_build(self):
        self.results = [FAIL]
        self.run_cli("build", "x", "-q", "--max-retries", "0")
        self.results = [PASS]
        code, out, _ = self.run_cli("resume", "-q", "--max-retries", "0")
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_json_events_are_one_object_per_line(self):
        code, out, _ = self.run_cli("build", "x", "--json-events")
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(lines[0]["type"], "event")
        self.assertEqual(lines[0]["phase"], "start")
        self.assertEqual(lines[-1]["type"], "result")
        self.assertEqual(lines[-1]["verdict"], "passed")

    def test_spec_file_skips_planning(self):
        spec_path = self.cwd / "task.json"
        spec_path.write_text(json.dumps(sample_spec().to_dict()), encoding="utf-8")
        code, _, _ = self.run_cli("build", "x", "-q", "--spec", str(spec_path))
        self.assertEqual(code, 0)
        [project] = list(self.builds.iterdir())
        state = json.loads((project / ".rocto" / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(state["planner"], "provided")

    def test_bad_spec_file_is_a_usage_error(self):
        spec_path = self.cwd / "task.json"
        spec_path.write_text("{}", encoding="utf-8")
        code, _, err = self.run_cli("build", "x", "-q", "--spec", str(spec_path))
        self.assertEqual(code, 2)
        self.assertIn("Cannot use --spec", err)

    def test_non_empty_output_is_refused(self):
        target = self.cwd / "taken"
        target.mkdir()
        (target / "keep.txt").write_text("mine", encoding="utf-8")
        code, _, err = self.run_cli("build", "x", "-q", "-o", str(target))
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)

    def test_refine_through_the_cli(self):
        self.run_cli("build", "x", "-q")
        self.results = [PASS]
        code, out, _ = self.run_cli("refine", "add a reset button", "-q")
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_review_plan_without_a_terminal_continues(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self.run_cli("build", "x", "-q", "--review-plan")
        self.assertEqual(code, 0)
        self.assertIn("needs an interactive terminal", err)

    def test_ledger_feeds_stats(self):
        self.run_cli("build", "x", "-q")
        code, out, _ = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("deepseek", out)
        code, out, _ = self.run_cli("stats", "--json")
        self.assertEqual(json.loads(out)["builds"], 1)

    def test_config_files_feed_defaults_and_config_show_explains(self):
        (self.cwd / "rocto.toml").write_text("max_retries = 0\n", encoding="utf-8")
        self.results = [FAIL]
        code, _, _ = self.run_cli("build", "x", "-q")
        self.assertEqual(code, 4)
        self.assertEqual(self.kwargs["max_retries"], 0)
        code, out, _ = self.run_cli("config")
        self.assertIn("max_retries", out)
        self.assertIn("rocto.toml", out)

    def test_config_set_init_and_bad_config(self):
        code, out, _ = self.run_cli("config", "set", "executor", "deepseek")
        self.assertEqual(code, 0)
        self.assertIn('executor = "deepseek"', (self.home / "config.toml").read_text(encoding="utf-8"))
        code, _, err = self.run_cli("config", "set", "api_key", "sk-x")
        self.assertEqual(code, 2)
        code, _, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertTrue((self.cwd / "rocto.toml").is_file())
        code, _, _ = self.run_cli("init")
        self.assertEqual(code, 2)
        (self.cwd / "rocto.toml").write_text('api_key = "sk"\n', encoding="utf-8")
        code, _, err = self.run_cli("doctor")
        self.assertEqual(code, 2)
        self.assertIn("never reads API keys", err)

    def test_status_without_any_build(self):
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("No build directory", err)


class AutomationCommandTests(CliTestCase):
    def test_batch_runs_every_case_and_applies_the_gate(self):
        cases = self.cwd / "cases.json"
        cases.write_text(json.dumps([{"id": "one", "idea": "a"}, {"id": "two", "idea": "b"}]), encoding="utf-8")
        self.results = [PASS, FAIL]
        code, out, _ = self.run_cli("batch", str(cases), "--max-retries", "0", "--min-verified", "1")
        self.assertEqual(code, 0)
        self.assertIn("GATE: PASS", out)
        [batch_dir] = list(self.builds.iterdir())
        summary = json.loads((batch_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["verified"], "1/2")
        self.assertTrue((batch_dir / "summary.md").is_file())

    def test_batch_gate_fails_without_min_verified_when_any_case_fails(self):
        cases = self.cwd / "ideas.txt"
        cases.write_text("# ideas\na counter\n\na timer\n", encoding="utf-8")
        self.results = [PASS, FAIL]
        code, _, _ = self.run_cli("batch", str(cases), "--max-retries", "0", "--json")
        self.assertEqual(code, 1)

    def test_gallery_publishes_only_passing_builds_and_no_logs(self):
        self.run_cli("build", "passing one", "-q")
        self.results = [FAIL]
        self.run_cli("build", "failing one", "-q", "--max-retries", "0")
        site = self.root / "site"
        code, out, _ = self.run_cli("gallery", str(self.builds), "-o", str(site))
        self.assertEqual(code, 0)
        published = [p for p in site.iterdir() if p.is_dir()]
        self.assertEqual(len(published), 1)
        self.assertTrue((published[0] / "index.html").is_file())
        self.assertTrue((published[0] / "report.html").is_file())
        self.assertFalse((published[0] / ".rocto").exists())
        self.assertIn("passing one", (site / "index.html").read_text(encoding="utf-8"))

    def test_gallery_refuses_to_write_inside_its_source(self):
        with self.assertRaises(ValueError):
            build_gallery(self.root, self.root / "inside")

    def test_case_loading(self):
        path = self.cwd / "c.json"
        path.write_text(json.dumps(["做一个番茄钟", "做一个番茄钟", {"idea": "Todo list"}]), encoding="utf-8")
        cases = load_cases(path)
        self.assertEqual([c["id"] for c in cases], ["case-1", "case-2", "todo-list"])
        path.write_text(json.dumps([{"nope": 1}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_cases(path)


if __name__ == "__main__":
    unittest.main()
