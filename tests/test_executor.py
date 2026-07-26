from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from rainbow_octopus.executor import CodexExecutor, ExecutionError
from rainbow_octopus.state import write_json_atomic
from tests.helpers import sample_spec, write_sample_site


class ExecutorTests(unittest.TestCase):
    def test_uses_workspace_sandbox_and_stdin(self):
        captured = {}

        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name)
            write_json_atomic(project / ".rocto" / "task.json", sample_spec().to_dict())
            fake_codex = project / "codex.exe"
            fake_codex.write_bytes(b"stub")

            def runner(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                write_sample_site(project)
                return SimpleNamespace(
                    returncode=0, stdout='{"type":"done"}\n', stderr=""
                )

            result = CodexExecutor(fake_codex, runner=runner).execute(
                project, sample_spec(), 1
            )
            self.assertTrue((project / "index.html").is_file())
            # the stub binary itself is a stray and must be swept away
            self.assertFalse(fake_codex.is_file())
        self.assertEqual(result.returncode, 0)
        self.assertIn("workspace-write", captured["command"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", captured["command"])
        self.assertTrue(captured["command"][-1] == "-")
        self.assertIn("TASK SPECIFICATION", captured["kwargs"]["input"])
        self.assertEqual(captured["kwargs"]["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertTrue(captured["kwargs"]["env"]["HOME"])
        self.assertTrue(captured["kwargs"]["env"]["CODEX_HOME"])

    def test_rejects_protected_task_mutation(self):
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name)
            task = project / ".rocto" / "task.json"
            write_json_atomic(task, sample_spec().to_dict())
            fake_codex = project / "codex.exe"
            fake_codex.write_bytes(b"stub")

            def runner(command, **kwargs):
                task.write_text("{}", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(ExecutionError):
                CodexExecutor(fake_codex, runner=runner).execute(
                    project, sample_spec(), 1
                )


if __name__ == "__main__":
    unittest.main()
