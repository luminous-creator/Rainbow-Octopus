from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from helpers import sample_spec, write_sample_site
from rainbow_octopus.executor import (
    ClaudeCodeExecutor,
    CodexExecutor,
    ExecutionError,
    RouterExecutor,
    _enforce_output_boundary,
    find_codex,
    is_app_bundled_codex,
    make_executor,
)
from rainbow_octopus.state import write_json_atomic


def _project(tmp: str) -> Path:
    project = Path(tmp)
    write_json_atomic(project / ".rocto" / "task.json", sample_spec().to_dict())
    return project


class _StubBackend:
    def __init__(self, name, *, available=True, fails=False, writes=True):
        self.name = name
        self.available = available
        self.fails = fails
        self.writes = writes
        self.calls = 0

    def healthcheck(self):
        return self.available, f"{self.name} stub"

    def execute(self, project_dir, spec, attempt, previous_failure=None):
        self.calls += 1
        if self.fails:
            raise ExecutionError(f"{self.name} blew up")
        if self.writes:
            write_sample_site(project_dir)
        return SimpleNamespace(
            returncode=0, stdout="", stderr="", command=[f"<{self.name}>"]
        )


class ClaudeCodeExecutorTests(unittest.TestCase):
    def test_command_disables_bash_and_caps_spend(self):
        executor = ClaudeCodeExecutor(claude_path=Path("claude"), max_budget_usd=2.5)
        command = executor._command()
        self.assertIn("-p", command)
        self.assertIn("--output-format", command)
        self.assertIn("json", command)
        self.assertIn("acceptEdits", command)
        self.assertIn("--no-session-persistence", command)
        # Bash must not be reachable: this is how "no LLM-generated shell" is
        # enforced for this backend.
        tools = command[command.index("--tools") + 1]
        self.assertNotIn("Bash", tools)
        self.assertIn("Write", tools)
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "2.5")

    def test_runs_in_output_dir_and_sweeps_strays(self):
        captured = {}

        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            claude = project / "claude.exe"
            claude.write_bytes(b"stub")

            def runner(command, **kwargs):
                captured["cwd"] = kwargs.get("cwd")
                captured["input"] = kwargs.get("input")
                write_sample_site(project)
                (project / "scratch.txt").write_text("junk", encoding="utf-8")
                (project / "notes").mkdir()
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"total_cost_usd": 0.42}',
                    stderr="",
                )

            ClaudeCodeExecutor(claude, runner=runner).execute(project, sample_spec(), 1)

            self.assertEqual(captured["cwd"], str(project))
            self.assertIn("TASK SPECIFICATION", captured["input"])
            self.assertFalse((project / "scratch.txt").exists())
            self.assertFalse((project / "notes").exists())
            self.assertTrue((project / "index.html").is_file())
            self.assertTrue((project / ".rocto" / "task.json").is_file())

            log = json.loads(
                (project / ".rocto" / "logs" / "claude-attempt-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(log["cost_usd"], 0.42)
            self.assertIn("scratch.txt", log["removed_stray_paths"])

    def test_missing_output_is_a_failure_even_on_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            claude = project / "claude.exe"
            claude.write_bytes(b"stub")

            def runner(command, **kwargs):
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(ExecutionError) as ctx:
                ClaudeCodeExecutor(claude, runner=runner).execute(
                    project, sample_spec(), 1
                )
            self.assertIn("did not produce", str(ctx.exception))

    def test_missing_binary_reports_clearly(self):
        executor = ClaudeCodeExecutor(claude_path=Path("/nonexistent/claude"))
        executor.claude_path = None
        ok, detail = executor.healthcheck()
        self.assertFalse(ok)
        self.assertIn("Claude Code", detail)

    def test_logged_out_cli_is_reported_unavailable(self):
        def runner(command, **kwargs):
            if "--version" in command:
                return SimpleNamespace(
                    returncode=0, stdout="2.1.219 (Claude Code)", stderr=""
                )
            return SimpleNamespace(
                returncode=1,
                stdout='{"loggedIn": false, "authMethod": "none"}',
                stderr="",
            )

        ok, detail = ClaudeCodeExecutor(Path("claude"), runner=runner).healthcheck()
        self.assertFalse(ok, "a logged-out CLI must not be routed to")
        self.assertIn("not signed in", detail)

    def test_signed_in_cli_is_available(self):
        def runner(command, **kwargs):
            if "--version" in command:
                return SimpleNamespace(returncode=0, stdout="2.1.219", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout='{"loggedIn": true, "authMethod": "subscription"}',
                stderr="",
            )

        ok, detail = ClaudeCodeExecutor(Path("claude"), runner=runner).healthcheck()
        self.assertTrue(ok)
        self.assertIn("subscription", detail)

    def test_surfaces_the_result_message_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            claude = project / "claude.exe"
            claude.write_bytes(b"stub")

            def runner(command, **kwargs):
                return SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps(
                        {
                            "type": "result",
                            "is_error": True,
                            "result": "Not logged in · Please run /login",
                        }
                    ),
                    stderr="",
                )

            with self.assertRaises(ExecutionError) as ctx:
                ClaudeCodeExecutor(claude, runner=runner).execute(
                    project, sample_spec(), 1
                )
            self.assertIn("Not logged in", str(ctx.exception))


class CodexSandboxFallbackTests(unittest.TestCase):
    def test_retries_without_sandbox_when_helper_is_missing(self):
        sandboxes = []

        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            codex = project / "codex.exe"
            codex.write_bytes(b"stub")

            def runner(command, **kwargs):
                sandbox = command[command.index("--sandbox") + 1]
                sandboxes.append(sandbox)
                if sandbox == "workspace-write":
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            '{"type":"item.completed","item":{"aggregated_output":'
                            '"windows sandbox: orchestrator_helper_launch_failed: '
                            'helper=codex-windows-sandbox-setup.exe"}}'
                        ),
                        stderr="",
                    )
                write_sample_site(project)
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")

            CodexExecutor(codex, runner=runner).execute(project, sample_spec(), 1)

        self.assertEqual(sandboxes, ["workspace-write", "danger-full-access"])

    def test_fallback_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            codex = project / "codex.exe"
            codex.write_bytes(b"stub")

            def runner(command, **kwargs):
                return SimpleNamespace(
                    returncode=0,
                    stdout="orchestrator_helper_launch_failed",
                    stderr="",
                )

            with self.assertRaises(ExecutionError):
                CodexExecutor(
                    codex, runner=runner, allow_sandbox_fallback=False
                ).execute(project, sample_spec(), 1)

    def test_app_bundled_binary_is_recognised(self):
        self.assertTrue(
            is_app_bundled_codex(Path.home() / ".codex" / ".sandbox-bin" / "codex.exe")
        )
        self.assertFalse(is_app_bundled_codex(Path("/usr/local/bin/codex")))
        self.assertFalse(is_app_bundled_codex(None))

    def test_find_codex_prefers_path_over_app_bundle(self):
        # Not asserting a specific result (depends on the host); just that the
        # helper never raises and returns None or an existing file.
        found = find_codex()
        self.assertTrue(found is None or found.is_file())


class RouterTests(unittest.TestCase):
    def test_falls_through_to_the_first_working_backend(self):
        broken = _StubBackend("claude", fails=True)
        unavailable = _StubBackend("codex", available=False)
        good = _StubBackend("deepseek")
        router = RouterExecutor(
            [("claude", broken), ("codex", unavailable), ("deepseek", good)]
        )

        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            router.execute(project, sample_spec(), 1)
            log = json.loads(
                (project / ".rocto" / "logs" / "router-attempt-1.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(router.last_used, "deepseek")
        self.assertEqual(broken.calls, 1)
        self.assertEqual(unavailable.calls, 0, "unavailable backend must not run")
        self.assertEqual(good.calls, 1)
        self.assertEqual(log["winner"], "deepseek")
        self.assertIn("claude: claude blew up", log["skipped_or_failed"])

    def test_partial_output_is_cleared_before_the_next_backend(self):
        class HalfWriter(_StubBackend):
            def execute(self, project_dir, spec, attempt, previous_failure=None):
                (project_dir / "index.html").write_text("half", encoding="utf-8")
                raise ExecutionError("gave up halfway")

        good = _StubBackend("deepseek")
        router = RouterExecutor(
            [("claude", HalfWriter("claude")), ("deepseek", good)]
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            router.execute(project, sample_spec(), 1)
            text = (project / "index.html").read_text(encoding="utf-8")
        self.assertNotEqual(text, "half", "stale partial output must be removed")

    def test_all_failed_reports_every_reason(self):
        router = RouterExecutor(
            [
                ("claude", _StubBackend("claude", fails=True)),
                ("deepseek", _StubBackend("deepseek", fails=True)),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            with self.assertRaises(ExecutionError) as ctx:
                router.execute(project, sample_spec(), 1)
        message = str(ctx.exception)
        self.assertIn("claude blew up", message)
        self.assertIn("deepseek blew up", message)

    def test_auto_order_is_claude_codex_deepseek(self):
        router = make_executor("auto")
        self.assertEqual([name for name, _ in router.backends],
                         ["claude", "codex", "deepseek"])

    def test_router_needs_a_backend(self):
        with self.assertRaises(ExecutionError):
            RouterExecutor([])


class BoundaryTests(unittest.TestCase):
    def test_keeps_contract_files_and_verifier_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".rocto").mkdir()
            write_sample_site(project)
            (project / "screenshot.png").write_bytes(b"png")
            (project / "acceptance-report.json").write_text("{}", encoding="utf-8")
            (project / "junk.py").write_text("x", encoding="utf-8")
            (project / "node_modules").mkdir()

            removed = _enforce_output_boundary(project)

            self.assertEqual(sorted(removed), ["junk.py", "node_modules"])
            for keeper in (
                "index.html",
                "styles.css",
                "script.js",
                "README.md",
                "screenshot.png",
                "acceptance-report.json",
            ):
                self.assertTrue((project / keeper).exists(), keeper)
            self.assertTrue((project / ".rocto").is_dir())


if __name__ == "__main__":
    unittest.main()


class HealthCacheTests(unittest.TestCase):
    def test_backends_are_probed_once_per_build(self):
        class Counting(_StubBackend):
            probes = 0

            def healthcheck(self):
                Counting.probes += 1
                return True, "ok"

        backend = Counting("deepseek")
        router = RouterExecutor([("deepseek", backend)])
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            for attempt in (1, 2, 3):
                router.execute(project, sample_spec(), attempt)
        self.assertEqual(Counting.probes, 1, "health checks must be cached")
        self.assertEqual(backend.calls, 3)

    def test_a_crashing_health_check_never_aborts_the_build(self):
        class Exploding(_StubBackend):
            def healthcheck(self):
                raise RuntimeError("probe exploded")

        good = _StubBackend("deepseek")
        router = RouterExecutor([("claude", Exploding("claude")), ("deepseek", good)])
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            router.execute(project, sample_spec(), 1)
        self.assertEqual(router.last_used, "deepseek")
