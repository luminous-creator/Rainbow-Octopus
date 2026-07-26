import json
import unittest

from rainbow_octopus.planner import DeepSeekPlanner, PlanningError
from tests.helpers import sample_spec


class PlannerTests(unittest.TestCase):
    def test_parses_structured_response_without_leaking_key(self):
        captured = {}

        def transport(url, headers, body, timeout):
            captured.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
            response = {
                "choices": [{"message": {"content": json.dumps(sample_spec().to_dict())}}]
            }
            return json.dumps(response).encode()

        planner = DeepSeekPlanner(api_key="secret-value", transport=transport)
        result = planner.plan("counter")
        self.assertEqual(result.title, "Counter")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-value")
        self.assertNotIn("secret-value", json.dumps(captured["body"]))

    def test_requires_api_key(self):
        planner = DeepSeekPlanner(api_key="")
        planner.api_key = None
        with self.assertRaises(PlanningError):
            planner.plan("counter")


if __name__ == "__main__":
    unittest.main()

