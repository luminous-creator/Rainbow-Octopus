from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from helpers import sample_spec
from rainbow_octopus.executor import (
    DeepSeekExecutor,
    ExecutionError,
    GENERATED_FILES,
    make_executor,
)


def _response(files: dict) -> bytes:
    payload = {
        "choices": [
            {"message": {"content": json.dumps({"files": files}, ensure_ascii=False)}}
        ]
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _good_files() -> dict:
    return {
        "index.html": '<!doctype html><html><body>'
        '<output data-testid="count">0</output>'
        '<button data-testid="increment">加一</button>'
        '<script src="script.js"></script></body></html>',
        "styles.css": "body{font-family:sans-serif}",
        "script.js": "console.log('中文 ok')",
        "README.md": "# 计数器",
    }


class DeepSeekExecutorTests(unittest.TestCase):
    def _run(self, files, **kwargs):
        captured = {}

        def transport(url, headers, body, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json.loads(body.decode("utf-8"))
            return _response(files)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            (project / ".rocto" / "task.json").write_text("{}", encoding="utf-8")
            executor = DeepSeekExecutor(
                api_key="sk-test", transport=transport, **kwargs
            )
            result = executor.execute(project, sample_spec(), 1, None)
            return project, result, captured

    def test_writes_exactly_the_four_files(self):
        files = _good_files()
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            (project / ".rocto" / "task.json").write_text("{}", encoding="utf-8")
            executor = DeepSeekExecutor(
                api_key="sk-test", transport=lambda *a: _response(files)
            )
            executor.execute(project, sample_spec(), 1, None)
            written = sorted(p.name for p in project.iterdir() if p.is_file())
            self.assertEqual(written, sorted(GENERATED_FILES))
            self.assertIn("计数器", (project / "README.md").read_text(encoding="utf-8"))

    def test_rejects_file_outside_allowlist(self):
        files = {**_good_files(), "../evil.txt": "boom"}
        with self.assertRaises(ExecutionError) as ctx:
            self._run(files)
        self.assertIn("outside the allowed set", str(ctx.exception))

    def test_rejects_nested_path(self):
        files = {**_good_files(), "sub/dir/x.js": "boom"}
        with self.assertRaises(ExecutionError):
            self._run(files)

    def test_rejects_missing_required_file(self):
        files = _good_files()
        del files["script.js"]
        with self.assertRaises(ExecutionError) as ctx:
            self._run(files)
        self.assertIn("script.js", str(ctx.exception))

    def test_rejects_oversized_file(self):
        files = {**_good_files(), "styles.css": "x" * 500_000}
        with self.assertRaises(ExecutionError) as ctx:
            self._run(files)
        self.assertIn("too large", str(ctx.exception))

    def test_never_writes_when_any_file_is_rejected(self):
        files = {**_good_files(), "hack.sh": "rm -rf /"}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            (project / ".rocto" / "task.json").write_text("{}", encoding="utf-8")
            executor = DeepSeekExecutor(
                api_key="sk-test", transport=lambda *a: _response(files)
            )
            with self.assertRaises(ExecutionError):
                executor.execute(project, sample_spec(), 1, None)
            leftovers = [p.name for p in project.iterdir() if p.is_file()]
            self.assertEqual(leftovers, [], "nothing may be written on rejection")

    def test_does_not_leak_api_key_into_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            (project / ".rocto" / "task.json").write_text("{}", encoding="utf-8")
            DeepSeekExecutor(
                api_key="sk-test", transport=lambda *a: _response(_good_files())
            ).execute(project, sample_spec(), 1, None)
            log = (project / ".rocto" / "logs" / "deepseek-attempt-1.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("sk-test", log)

    def test_sends_spec_and_retry_evidence(self):
        captured = {}

        def transport(url, headers, body, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            return _response(_good_files())

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            (project / ".rocto" / "task.json").write_text("{}", encoding="utf-8")
            DeepSeekExecutor(api_key="sk-test", transport=transport).execute(
                project, sample_spec(), 2, "text_visible expected 1, saw 0"
            )
        prompt = captured["body"]["messages"][1]["content"]
        self.assertIn("data-testid", prompt)
        self.assertIn("text_visible expected 1, saw 0", prompt)
        self.assertEqual(captured["body"]["response_format"]["type"], "json_object")

    def test_requires_api_key(self):
        executor = DeepSeekExecutor(api_key=None, transport=lambda *a: b"{}")
        executor.api_key = None
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExecutionError):
                executor.execute(Path(tmp), sample_spec(), 1, None)

    def test_rejects_non_json_content(self):
        payload = {"choices": [{"message": {"content": "sorry, I cannot"}}]}
        raw = json.dumps(payload).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            (project / ".rocto" / "task.json").write_text("{}", encoding="utf-8")
            executor = DeepSeekExecutor(api_key="sk-test", transport=lambda *a: raw)
            with self.assertRaises(ExecutionError):
                executor.execute(project, sample_spec(), 1, None)

    def test_make_executor_backends(self):
        from rainbow_octopus.executor import RouterExecutor

        self.assertIsInstance(make_executor("deepseek"), DeepSeekExecutor)
        self.assertIsInstance(make_executor(), RouterExecutor)
        self.assertIsInstance(make_executor("auto"), RouterExecutor)
        with self.assertRaises(ExecutionError):
            make_executor("nope")


if __name__ == "__main__":
    unittest.main()
