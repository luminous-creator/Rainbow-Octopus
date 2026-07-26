import unittest

from rainbow_octopus.models import SpecValidationError, TaskSpec
from tests.helpers import sample_spec


class TaskSpecTests(unittest.TestCase):
    def test_valid_spec_adds_fixed_constraints(self):
        spec = sample_spec()
        self.assertIn("Create index.html, styles.css, script.js, and README.md", spec.constraints)

    def test_rejects_unknown_action(self):
        data = sample_spec().to_dict()
        data["tests"][0]["steps"][0]["action"] = "run_shell"
        with self.assertRaises(SpecValidationError):
            TaskSpec.from_dict(data)

    def test_rejects_general_css_selector(self):
        data = sample_spec().to_dict()
        data["tests"][0]["steps"][0]["selector"] = "button.primary"
        with self.assertRaises(SpecValidationError):
            TaskSpec.from_dict(data)

    def test_rejects_selector_absent_from_contract(self):
        data = sample_spec().to_dict()
        data["tests"][0]["steps"][0]["selector"] = '[data-testid="unknown"]'
        with self.assertRaises(SpecValidationError):
            TaskSpec.from_dict(data)


if __name__ == "__main__":
    unittest.main()

