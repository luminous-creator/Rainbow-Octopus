"""Checks on the acceptance contract itself.

The scenario driving every test here is real and is recorded as KI-007:
``demo-output/pomodoro-2`` passed 31 of 31 assertions while shipping a timer
whose clock had been bent to fit one of those assertions, and while never
observing the counter that the request had asked for.
"""

import json
import unittest

from rainbow_octopus.contract import check_contract
from rainbow_octopus.models import TaskSpec
from rainbow_octopus.planner import DeepSeekPlanner, PlanningError


def spec_with(tests, ui_contract=None) -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "title": "Timer",
            "goal": "Build a timer",
            "features": ["Counts down"],
            "constraints": ["Vanilla only"],
            "ui_contract": ui_contract
            or [
                {"test_id": "clock", "purpose": "Time remaining"},
                {"test_id": "start", "purpose": "Start button"},
            ],
            "tests": tests,
        }
    )


def step(action, selector=None, expected=None, timeout_ms=1000):
    data = {"action": action, "timeout_ms": timeout_ms}
    if selector:
        data["selector"] = f'[data-testid="{selector}"]'
    if expected is not None:
        data["expected"] = expected
    return data


class WallClockAssertionTests(unittest.TestCase):
    """Rule 1: a clock value derived from elapsed time is not a behaviour test."""

    def test_rejects_clock_value_asserted_after_a_wait(self):
        # Exactly the pomodoro-2 contract: start, wait 2s, pause, expect 24:58.
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Start and pause",
                        "steps": [
                            step("click", "start"),
                            step("wait", timeout_ms=2000),
                            step("text_visible", "clock", "24:58"),
                        ],
                    }
                ]
            )
        )
        self.assertFalse(report.ok)
        self.assertIn("24:58", report.feedback())

    def test_allows_clock_value_asserted_with_no_preceding_wait(self):
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Initial state",
                        "steps": [
                            step("text_visible", "clock", "25:00"),
                            step("click", "start"),
                        ],
                    }
                ]
            )
        )
        self.assertTrue(report.ok, report.feedback())

    def test_allows_a_resting_value_reasserted_after_a_wait(self):
        """25:00 after Reset is fine: it is a state the page returns to."""
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Initial state",
                        "steps": [step("text_visible", "clock", "25:00")],
                    },
                    {
                        "name": "Reset",
                        "steps": [
                            step("click", "start"),
                            step("wait", timeout_ms=1000),
                            step("click", "start"),
                            step("text_visible", "clock", "25:00"),
                        ],
                    },
                ]
            )
        )
        self.assertTrue(report.ok, report.feedback())

    def test_ignores_non_clock_values_after_a_wait(self):
        """A counter changes on a click, not with elapsed time."""
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Counts",
                        "steps": [
                            step("click", "start"),
                            step("wait", timeout_ms=1000),
                            step("text_visible", "clock", "3"),
                        ],
                    }
                ]
            )
        )
        self.assertTrue(report.ok, report.feedback())

    def test_rejects_hour_shaped_values_too(self):
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Elapsed",
                        "steps": [
                            step("click", "start"),
                            step("wait", timeout_ms=3000),
                            step("text_visible", "clock", "00:59:57"),
                        ],
                    }
                ]
            )
        )
        self.assertFalse(report.ok)


class ContractCoverageTests(unittest.TestCase):
    """Rule 2: a declared element that is never selected is phantom coverage."""

    def test_rejects_declared_but_untested_element(self):
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Shows time",
                        "steps": [step("text_visible", "clock", "25:00")],
                    }
                ],
                ui_contract=[
                    {"test_id": "clock", "purpose": "Time remaining"},
                    {"test_id": "progress-bar", "purpose": "Progress indicator"},
                ],
            )
        )
        self.assertFalse(report.ok)
        self.assertIn("progress-bar", report.feedback())

    def test_accepts_when_every_element_is_selected(self):
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Shows time",
                        "steps": [
                            step("text_visible", "clock", "25:00"),
                            step("click", "start"),
                        ],
                    }
                ]
            )
        )
        self.assertTrue(report.ok, report.feedback())


class ConstantAssertionWarningTests(unittest.TestCase):
    """The counter in 'a pomodoro timer with stats' was only ever asserted as 0."""

    def test_warns_when_an_element_never_changes(self):
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Counter starts and stays at zero",
                        "steps": [
                            step("text_visible", "clock", "0"),
                            step("click", "start"),
                            step("text_visible", "clock", "0"),
                        ],
                    }
                ]
            )
        )
        self.assertTrue(report.ok, "a thin contract must not block the build")
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("hard-codes", report.warnings[0])

    def test_no_warning_when_a_value_is_observed_changing(self):
        report = check_contract(
            spec_with(
                [
                    {
                        "name": "Counter increments",
                        "steps": [
                            step("text_visible", "clock", "0"),
                            step("click", "start"),
                            step("text_visible", "clock", "1"),
                        ],
                    }
                ]
            )
        )
        self.assertEqual(report.warnings, ())


class PlannerRepairTests(unittest.TestCase):
    """The planner gets its failures back as evidence, like the executor does."""

    @staticmethod
    def _spec_payload(expected):
        return {
            "title": "Timer",
            "goal": "Build a timer",
            "features": ["Counts down"],
            "constraints": ["Vanilla only"],
            "ui_contract": [
                {"test_id": "clock", "purpose": "Time remaining"},
                {"test_id": "start", "purpose": "Start button"},
            ],
            "tests": [
                {
                    "name": "Start and pause",
                    "steps": [
                        step("click", "start"),
                        step("wait", timeout_ms=2000),
                        step("text_visible", "clock", expected),
                    ],
                }
            ],
        }

    def _planner(self, expectations, **kwargs):
        sent = []

        def transport(url, headers, body, timeout):
            payload = json.loads(body)
            sent.append(payload["messages"])
            spec = self._spec_payload(expectations[len(sent) - 1])
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps(spec)}}]}
            ).encode()

        return DeepSeekPlanner(api_key="k", transport=transport, **kwargs), sent

    def test_repairs_a_rejected_contract_and_feeds_back_the_reason(self):
        planner, sent = self._planner(["24:58", "3"])
        spec = planner.plan("pomodoro")

        self.assertEqual(planner.last_attempts, 2)
        self.assertEqual(len(sent), 2)
        # The second request carries the first attempt's specific violation.
        repair_text = sent[1][-1]["content"]
        self.assertIn("24:58", repair_text)
        self.assertIn("rejected", repair_text)
        self.assertEqual(spec.tests[0].steps[-1].expected, "3")

    def test_gives_up_after_max_attempts(self):
        planner, sent = self._planner(["24:58"] * 3)
        with self.assertRaises(PlanningError) as caught:
            planner.plan("pomodoro")
        self.assertEqual(len(sent), 3)
        self.assertIn("24:58", str(caught.exception))

    def test_accepts_immediately_when_the_contract_is_sound(self):
        planner, sent = self._planner(["3"])
        planner.plan("pomodoro")
        self.assertEqual(len(sent), 1)
        self.assertEqual(planner.last_attempts, 1)

    def test_exposes_warnings_from_the_accepted_plan(self):
        planner, _ = self._planner(["3"])
        planner.plan("pomodoro")
        # 'clock' is asserted exactly once, so it is never observed changing.
        self.assertTrue(any("clock" in w for w in planner.last_warnings))


class RealPomodoroRegressionTests(unittest.TestCase):
    """The contract that shipped a bent clock must not pass unchanged."""

    def test_the_shipped_pomodoro_contract_is_now_rejected(self):
        report = check_contract(
            TaskSpec.from_dict(
                {
                    "title": "Pomodoro Timer with Stats",
                    "goal": "Track completed work sessions",
                    "features": ["Counts tomatoes"],
                    "constraints": ["Vanilla only"],
                    "ui_contract": [
                        {"test_id": "pomodoro-timer", "purpose": "mm:ss display"},
                        {"test_id": "start-button", "purpose": "Start"},
                        {"test_id": "pause-button", "purpose": "Pause"},
                        {"test_id": "tomato-count", "purpose": "Completed count"},
                        {"test_id": "progress-bar", "purpose": "Progress"},
                    ],
                    "tests": [
                        {
                            "name": "Initial state",
                            "steps": [
                                step("text_visible", "pomodoro-timer", "25:00"),
                                step("text_visible", "tomato-count", "0"),
                            ],
                        },
                        {
                            "name": "Start and pause timer",
                            "steps": [
                                step("click", "start-button"),
                                step("wait", timeout_ms=2000),
                                step("click", "pause-button"),
                                step("text_visible", "pomodoro-timer", "24:58"),
                            ],
                        },
                    ],
                }
            )
        )
        self.assertFalse(report.ok)
        feedback = report.feedback()
        self.assertIn("24:58", feedback)       # the bent clock
        self.assertIn("progress-bar", feedback)  # declared, never tested
        # And the counter that the request was actually about is called out.
        self.assertTrue(any("tomato-count" in w for w in report.warnings))


if __name__ == "__main__":
    unittest.main()
