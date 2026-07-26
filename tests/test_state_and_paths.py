from pathlib import Path
import tempfile
import unittest

from rainbow_octopus.orchestrator import BuildError, prepare_output_directory
from rainbow_octopus.state import RunState, StateStore


class StateAndPathTests(unittest.TestCase):
    def test_state_round_trip_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as temp_name:
            store = StateStore(Path(temp_name))
            state = RunState(idea="做一个番茄钟")
            state.transition("planning", "开始")
            store.initialize(state)
            loaded = store.load()
        self.assertEqual(loaded.idea, "做一个番茄钟")
        self.assertEqual(loaded.phase, "planning")

    def test_rejects_nonempty_output(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name)
            (output / "keep.txt").write_text("user data", encoding="utf-8")
            with self.assertRaises(BuildError):
                prepare_output_directory(output)

    def test_creates_missing_output(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name) / "new-project"
            result = prepare_output_directory(output)
            self.assertTrue(result.is_dir())


if __name__ == "__main__":
    unittest.main()

