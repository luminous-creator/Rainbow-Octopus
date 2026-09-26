"""ADR-005 transient retries, and the Claude Code planner (ADR-004)."""

from __future__ import annotations

from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import io
import json
import os
import tempfile
import unittest
import urllib.error

from helpers import sample_spec
from rainbow_octopus.executor import DeepSeekExecutor, ExecutionError
from rainbow_octopus.planner import (
    ClaudeCodePlanner,
    DeepSeekPlanner,
    PlanningError,
    make_planner,
)
from rainbow_octopus.provider import MAX_BACKOFF_SECONDS, with_retries


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "err", headers, io.BytesIO(b"body"))


def chat(content) -> bytes:
    text = content if isinstance(content, str) else json.dumps(content)
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


class WithRetriesTests(unittest.TestCase):
    def test_retries_transient_failures_then_succeeds(self):
        calls, waits, notes = [], [], []

        def call():
            calls.append(1)
            if len(calls) < 3:
                raise http_error(503)
            return b"ok"

        result = with_retries(call, attempts=3, sleep=waits.append, notify=notes.append)
        self.assertEqual(result, b"ok")
        self.assertEqual(waits, [2.0, 4.0])
        self.assertEqual(len(notes), 2)
        self.assertIn("HTTP 503", notes[0])

    def test_does_not_retry_a_bad_key(self):
        waits = []
        with self.assertRaises(urllib.error.HTTPError):
            with_retries(lambda: (_ for _ in ()).throw(http_error(401)), sleep=waits.append)
        self.assertEqual(waits, [])

    def test_respects_retry_after_but_caps_it(self):
        waits = []
        sequence = [http_error(429, "7"), http_error(429, "9999"), b"ok"]

        def call():
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with_retries(call, attempts=3, sleep=waits.append)
        self.assertEqual(waits, [7.0, MAX_BACKOFF_SECONDS])

    def test_gives_up_after_the_last_attempt(self):
        with self.assertRaises(urllib.error.URLError):
            with_retries(
                lambda: (_ for _ in ()).throw(urllib.error.URLError("down")),
                attempts=2,
                sleep=lambda _: None,
            )

    def test_programming_errors_are_never_retried(self):
        calls = []

        def call():
            calls.append(1)
            raise AssertionError("bug")

        with self.assertRaises(AssertionError):
            with_retries(call, sleep=lambda _: None)
        self.assertEqual(len(calls), 1)


class PlannerRetryTests(unittest.TestCase):
    def test_planner_survives_two_503s(self):
        responses = [http_error(503), http_error(502), chat(sample_spec().to_dict())]

        def transport(url, headers, body, timeout):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        planner = DeepSeekPlanner(api_key="k", transport=transport, sleep=lambda _: None)
        notes = []
        planner.notify = notes.append
        self.assertEqual(planner.plan("counter").title, "Counter")
        self.assertEqual(len(notes), 2)

    def test_repair_request_quotes_the_rejected_reply(self):
        sent = []
        replies = ["not json at all", json.dumps(sample_spec().to_dict())]

        def transport(url, headers, body, timeout):
            sent.append(json.loads(body)["messages"])
            return chat(replies[len(sent) - 1])

        DeepSeekPlanner(api_key="k", transport=transport).plan("counter")
        second = sent[1]
        self.assertEqual(second[-2], {"role": "assistant", "content": "not json at all"})
        self.assertIn("not a single JSON object", second[-1]["content"])


class ExecutorRetryTests(unittest.TestCase):
    def test_executor_survives_a_timeout(self):
        from test_deepseek_executor import _good_files, _response

        responses = [TimeoutError("slow"), _response(_good_files())]

        def transport(url, headers, body, timeout):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            (project / ".rocto" / "task.json").write_text("{}", encoding="utf-8")
            executor = DeepSeekExecutor(api_key="k", transport=transport, sleep=lambda _: None)
            executor.execute(project, sample_spec(), 1, None)
            self.assertTrue((project / "index.html").is_file())

    def test_executor_reports_a_persistent_timeout_cleanly(self):
        def transport(url, headers, body, timeout):
            raise TimeoutError("slow")

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            executor = DeepSeekExecutor(api_key="k", transport=transport, sleep=lambda _: None)
            with self.assertRaises(ExecutionError) as caught:
                executor.execute(project, sample_spec(), 1, None)
        self.assertIn("did not answer", str(caught.exception))


class ClaudePlannerTests(unittest.TestCase):
    def _planner(self, results):
        seen = []

        def runner(command, **kwargs):
            seen.append((command, kwargs))
            result = results[len(seen) - 1]
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

        return ClaudeCodePlanner(claude_path=Path("claude"), runner=runner), seen

    def test_runs_claude_with_every_tool_disabled(self):
        planner, seen = self._planner(
            [{"type": "result", "result": json.dumps(sample_spec().to_dict()), "total_cost_usd": 0.02}]
        )
        spec = planner.plan("a counter")
        command, kwargs = seen[0]
        self.assertEqual(spec.title, "Counter")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--system-prompt", command)
        self.assertEqual(kwargs["input"], "a counter")
        self.assertAlmostEqual(planner.last_cost_usd, 0.02)

    def test_repairs_through_a_flattened_conversation(self):
        planner, seen = self._planner(
            [
                {"result": "```\nnope\n```", "total_cost_usd": 0.01},
                {"result": json.dumps(sample_spec().to_dict()), "total_cost_usd": 0.01},
            ]
        )
        planner.plan("a counter")
        second_prompt = seen[1][1]["input"]
        self.assertIn("Your previous reply", second_prompt)
        self.assertIn("rejected", second_prompt)
        self.assertAlmostEqual(planner.last_cost_usd, 0.02)

    def test_signed_out_cli_is_a_planning_error(self):
        planner, _ = self._planner([{"is_error": True, "result": "Not logged in · Please run /login"}])
        with self.assertRaises(PlanningError) as caught:
            planner.plan("x")
        self.assertIn("Not logged in", str(caught.exception))

    def test_missing_cli_is_reported_before_any_call(self):
        planner = ClaudeCodePlanner.__new__(ClaudeCodePlanner)
        planner.claude_path = None
        with self.assertRaises(PlanningError):
            planner.ensure_ready()

    def test_refine_sends_the_current_spec_and_the_request(self):
        planner, seen = self._planner([{"result": json.dumps(sample_spec().to_dict())}])
        planner.refine(sample_spec(), "add a reset button")
        prompt = seen[0][1]["input"]
        self.assertIn("add a reset button", prompt)
        self.assertIn('"increment"', prompt)


class MakePlannerTests(unittest.TestCase):
    def _make(self, env, claude_ok):
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("rainbow_octopus.executor.find_claude", return_value=Path("claude") if claude_ok is not None else None),
            mock.patch(
                "rainbow_octopus.executor.ClaudeCodeExecutor.healthcheck",
                return_value=(bool(claude_ok), "x"),
            ),
        ):
            return make_planner()

    def test_auto_prefers_the_api_when_a_key_exists(self):
        self.assertIsInstance(self._make({"DEEPSEEK_API_KEY": "k"}, True), DeepSeekPlanner)

    def test_auto_falls_back_to_signed_in_claude(self):
        self.assertIsInstance(self._make({}, True), ClaudeCodePlanner)

    def test_auto_with_nothing_names_the_api_key(self):
        planner = self._make({}, None)
        self.assertIsInstance(planner, DeepSeekPlanner)
        with self.assertRaises(PlanningError) as caught:
            planner.plan("x")
        self.assertIn("ROCTO_API_KEY", str(caught.exception))

    def test_explicit_choice_and_unknown_choice(self):
        self.assertIsInstance(self._make({"ROCTO_PLANNER": "claude"}, None), ClaudeCodePlanner)
        with self.assertRaises(PlanningError):
            self._make({"ROCTO_PLANNER": "gpt"}, None)


if __name__ == "__main__":
    unittest.main()
