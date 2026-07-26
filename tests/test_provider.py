"""The chat-completions endpoint is configuration, not a constant.

Pinning DeepSeek's URL made a DeepSeek account mandatory for everyone,
including a user who already has Claude Code signed in and only needs
something to write the task specification.
"""

import json
import os
import unittest

from rainbow_octopus import provider
from rainbow_octopus.executor import DeepSeekExecutor
from rainbow_octopus.planner import DeepSeekPlanner, PlanningError

RELEVANT_VARS = (
    provider.BASE_URL_ENV,
    provider.API_KEY_ENV,
    provider.LEGACY_API_KEY_ENV,
)


class IsolatedEnvTestCase(unittest.TestCase):
    """Run with every relevant variable unset, and restore afterwards."""

    def setUp(self):
        self._saved = {name: os.environ.pop(name, None) for name in RELEVANT_VARS}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ResolutionTests(IsolatedEnvTestCase):
    def test_defaults_to_deepseek(self):
        self.assertEqual(provider.resolve_base_url(), provider.DEFAULT_BASE_URL)
        self.assertTrue(provider.is_default_provider())
        self.assertIsNone(provider.resolve_api_key())

    def test_legacy_key_still_works(self):
        os.environ[provider.LEGACY_API_KEY_ENV] = "legacy"
        self.assertEqual(provider.resolve_api_key(), "legacy")

    def test_new_key_wins_over_legacy(self):
        os.environ[provider.LEGACY_API_KEY_ENV] = "legacy"
        os.environ[provider.API_KEY_ENV] = "new"
        self.assertEqual(provider.resolve_api_key(), "new")

    def test_explicit_argument_wins_over_everything(self):
        os.environ[provider.API_KEY_ENV] = "from-env"
        os.environ[provider.BASE_URL_ENV] = "https://from-env.example"
        self.assertEqual(provider.resolve_api_key("explicit"), "explicit")
        self.assertEqual(
            provider.resolve_base_url("https://explicit.example"),
            "https://explicit.example",
        )

    def test_trailing_slash_is_normalised(self):
        os.environ[provider.BASE_URL_ENV] = "https://api.openai.com/v1/"
        self.assertEqual(provider.resolve_base_url(), "https://api.openai.com/v1")
        self.assertFalse(provider.is_default_provider())

    def test_completions_url_is_appended(self):
        self.assertEqual(
            provider.completions_url("https://api.openai.com/v1"),
            "https://api.openai.com/v1/chat/completions",
        )
        # DeepSeek has no /v1 segment; the default must not grow one.
        self.assertEqual(
            provider.completions_url(provider.DEFAULT_BASE_URL),
            "https://api.deepseek.com/chat/completions",
        )


class PlannerEndpointTests(IsolatedEnvTestCase):
    def test_planner_calls_the_configured_endpoint(self):
        seen = {}

        def transport(url, headers, body, timeout):
            seen["url"] = url
            raise AssertionError("stop here — the URL is what this test is about")

        planner = DeepSeekPlanner(
            api_key="k", base_url="https://openrouter.ai/api/v1", transport=transport
        )
        self.assertEqual(
            planner.api_url, "https://openrouter.ai/api/v1/chat/completions"
        )
        with self.assertRaises(AssertionError):
            planner.plan("idea")
        self.assertEqual(seen["url"], "https://openrouter.ai/api/v1/chat/completions")

    def test_planner_reads_the_environment(self):
        os.environ[provider.BASE_URL_ENV] = "https://api.openai.com/v1"
        os.environ[provider.API_KEY_ENV] = "sk-test"
        planner = DeepSeekPlanner()
        self.assertEqual(planner.api_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(planner.api_key, "sk-test")

    def test_missing_key_message_names_both_variables(self):
        planner = DeepSeekPlanner(api_key="x")
        planner.api_key = None
        with self.assertRaises(PlanningError) as caught:
            planner.plan("idea")
        message = str(caught.exception)
        self.assertIn(provider.API_KEY_ENV, message)
        self.assertIn(provider.LEGACY_API_KEY_ENV, message)

    def test_errors_name_the_endpoint_actually_used(self):
        def transport(url, headers, body, timeout):
            return json.dumps({"choices": []}).encode()

        planner = DeepSeekPlanner(
            api_key="k", base_url="https://local.example/v1", transport=transport
        )
        with self.assertRaises(PlanningError) as caught:
            planner.plan("idea")
        self.assertIn("https://local.example/v1", str(caught.exception))


class ExecutorEndpointTests(IsolatedEnvTestCase):
    def test_executor_calls_the_configured_endpoint(self):
        executor = DeepSeekExecutor(api_key="k", base_url="https://api.openai.com/v1")
        self.assertEqual(executor.api_url, "https://api.openai.com/v1/chat/completions")

    def test_healthcheck_mentions_a_non_default_endpoint_only(self):
        default = DeepSeekExecutor(api_key="k")
        custom = DeepSeekExecutor(api_key="k", base_url="https://api.openai.com/v1")
        self.assertNotIn("http", default.healthcheck()[1])
        self.assertIn("https://api.openai.com/v1", custom.healthcheck()[1])

    def test_healthcheck_without_a_key_names_both_variables(self):
        executor = DeepSeekExecutor(api_key="x")
        executor.api_key = None
        ok, detail = executor.healthcheck()
        self.assertFalse(ok)
        self.assertIn(provider.API_KEY_ENV, detail)
        self.assertIn(provider.LEGACY_API_KEY_ENV, detail)


if __name__ == "__main__":
    unittest.main()
