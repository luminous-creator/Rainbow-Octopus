"""Tests for the harness transport that replaced --dump-dom (KI-003).

These run without a browser: a stub process stands in for Edge and posts the
verdict the injected JavaScript would have posted.
"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from helpers import sample_spec, write_sample_site
from rainbow_octopus.verifier import (
    RESULT_PATH,
    BrowserVerifier,
    _parse_harness_payload,
    _serve,
)


GOOD_PAYLOAD = {
    "checks": [
        {"name": "increments:click", "passed": True, "detail": "ok"},
        {"name": "increments:text_visible", "passed": True, "detail": "actual=1"},
    ],
    "console_errors": [],
}


class ServeTests(unittest.TestCase):
    def test_serves_files_and_collects_the_posted_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_sample_site(site)
            with _serve(site) as (url, server):
                body = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
                self.assertIn("data-testid", body)

                self.assertFalse(server.rocto_ready.is_set())
                base = url.rsplit("/", 1)[0]
                request = urllib.request.Request(
                    base + RESULT_PATH,
                    data=json.dumps(GOOD_PAYLOAD).encode("utf-8"),
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=5).read()

                self.assertTrue(server.rocto_ready.wait(5))
                self.assertEqual(json.loads(server.rocto_result), GOOD_PAYLOAD)

    def test_only_the_first_result_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_sample_site(site)
            with _serve(site) as (url, server):
                base = url.rsplit("/", 1)[0]
                for marker in ("first", "second"):
                    request = urllib.request.Request(
                        base + RESULT_PATH,
                        data=json.dumps({"checks": [], "marker": marker}).encode(),
                        method="POST",
                    )
                    urllib.request.urlopen(request, timeout=5).read()
                self.assertEqual(json.loads(server.rocto_result)["marker"], "first")

    def test_unknown_post_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_sample_site(site)
            with _serve(site) as (url, server):
                base = url.rsplit("/", 1)[0]
                request = urllib.request.Request(
                    base + "/somewhere-else", data=b"{}", method="POST"
                )
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(request, timeout=5)
                self.assertFalse(server.rocto_ready.is_set())


class PayloadTests(unittest.TestCase):
    def test_parses_checks_and_marks_the_run_completed(self):
        parsed = _parse_harness_payload(json.dumps(GOOD_PAYLOAD))
        names = [check.name for check in parsed["checks"]]
        self.assertEqual(names[0], "browser_run")
        self.assertTrue(parsed["checks"][0].passed)
        self.assertIn("increments:click", names)

    def test_empty_payload_fails_loudly(self):
        parsed = _parse_harness_payload(None)
        self.assertFalse(parsed["checks"][0].passed)

    def test_garbage_payload_fails_loudly(self):
        parsed = _parse_harness_payload("not json")
        self.assertFalse(parsed["checks"][0].passed)

    def test_console_errors_are_carried_through(self):
        parsed = _parse_harness_payload(
            json.dumps({"checks": [], "console_errors": ["boom"]})
        )
        self.assertEqual(parsed["console_errors"], ["boom"])


class _StubProcess:
    """Stands in for headless Edge."""

    def __init__(self, on_start=None):
        self.terminated = False
        self.killed = False
        self._alive = True
        if on_start:
            threading.Thread(target=on_start, daemon=True).start()

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


class HarnessRunTests(unittest.TestCase):
    def _verify_with_stub(self, on_start, timeout=5):
        verifier = BrowserVerifier(edge_path=Path("/fake/msedge"), timeout=timeout)
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_sample_site(site)
            with _serve(site) as (url, server):
                started = None

                def factory(*args, **kwargs):
                    nonlocal started
                    base = url.rsplit("/", 1)[0]
                    started = _StubProcess(on_start=lambda: on_start(base, server))
                    return started

                with mock.patch(
                    "rainbow_octopus.verifier.subprocess.Popen", factory
                ):
                    result = verifier._run_edge_harness(
                        url, site / "profile", server
                    )
                return result, started

    def test_result_is_collected_and_browser_is_torn_down(self):
        def post(base, server):
            request = urllib.request.Request(
                base + RESULT_PATH,
                data=json.dumps(GOOD_PAYLOAD).encode("utf-8"),
                method="POST",
            )
            urllib.request.urlopen(request, timeout=5).read()

        result, process = self._verify_with_stub(post)
        self.assertTrue(result["checks"][0].passed)
        self.assertTrue(process.terminated, "Edge must be closed once we have a verdict")

    def test_silent_browser_times_out_instead_of_hanging(self):
        result, process = self._verify_with_stub(lambda base, server: None, timeout=1)
        self.assertFalse(result["checks"][0].passed)
        self.assertIn("did not report", result["checks"][0].detail)
        self.assertTrue(process.terminated)

    def test_unlaunchable_browser_is_reported(self):
        verifier = BrowserVerifier(edge_path=Path("/fake/msedge"), timeout=1)
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_sample_site(site)
            with _serve(site) as (url, server):
                with mock.patch(
                    "rainbow_octopus.verifier.subprocess.Popen",
                    side_effect=OSError("no such file"),
                ):
                    result = verifier._run_edge_harness(url, site / "p", server)
        self.assertFalse(result["checks"][0].passed)
        self.assertIn("no such file", result["checks"][0].detail)


class InjectionTests(unittest.TestCase):
    def test_harness_posts_to_the_result_endpoint_and_has_a_watchdog(self):
        verifier = BrowserVerifier(edge_path=Path("/fake/msedge"), timeout=30)
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_sample_site(site)
            index = site / "index.html"
            verifier._inject_harness(index, sample_spec())
            html = index.read_text(encoding="utf-8")

        self.assertIn(RESULT_PATH, html)
        self.assertIn("sendBeacon", html)
        self.assertIn("harness:watchdog", html)
        self.assertIn("__roctoErrors", html)
        # the page's own markup must survive injection
        self.assertIn('data-testid="increment"', html)

    def test_injection_does_not_touch_the_delivered_project(self):
        """verify() stages a copy; the shipped index.html stays harness-free."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_sample_site(project)
            original = (project / "index.html").read_text(encoding="utf-8")
            verifier = BrowserVerifier(edge_path=None)
            verifier.verify(project, sample_spec())
            self.assertEqual(
                original, (project / "index.html").read_text(encoding="utf-8")
            )


if __name__ == "__main__":
    unittest.main()


class OfflineScanTests(unittest.TestCase):
    """KI-004: `//` in a JS comment is not a network call."""

    def _scan(self, script: str) -> list:
        from rainbow_octopus.verifier import BrowserVerifier

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_sample_site(project)
            (project / "script.js").write_text(script, encoding="utf-8")
            return BrowserVerifier(edge_path=None)._security_findings(project)

    def test_line_comments_are_not_network_access(self):
        self.assertEqual(self._scan("const T = 25*60; // seconds\nlet a=1; // 备注"), [])

    def test_block_and_html_comments_are_ignored(self):
        self.assertEqual(self._scan("/* see http://example.com for context */"), [])

    def test_comment_shaped_division_is_ignored(self):
        self.assertEqual(self._scan("// a = 10 // 2"), [])

    def test_a_real_url_in_a_string_is_still_caught(self):
        self.assertTrue(self._scan('const u = "https://evil.com";'))

    def test_escaped_quote_cannot_smuggle_a_url(self):
        # A naive stripper would see the // and blank the rest of the line,
        # hiding the URL. The scanner must stay inside the string literal.
        self.assertTrue(self._scan('const s = "a\\" // https://evil.com";'))

    def test_protocol_relative_url_is_caught(self):
        self.assertTrue(self._scan('el.src = "//cdn.example.com/a.js";'))

    def test_template_literal_url_is_caught(self):
        self.assertTrue(self._scan("const u = `https://${host}/a`;"))

    def test_fetch_and_friends_are_caught(self):
        for snippet in (
            'fetch("/api")',
            'new WebSocket("ws://x")',
            'navigator.sendBeacon("/x")',
            'import("./m.js")',
            "new XMLHttpRequest()",
        ):
            self.assertTrue(self._scan(snippet), snippet)

    def test_strip_comments_preserves_line_count(self):
        from rainbow_octopus.verifier import strip_comments

        text = "a\n// gone\nb\n/* also\ngone */\nc"
        self.assertEqual(text.count("\n"), strip_comments(text).count("\n"))


class ScreenshotIsolationTests(unittest.TestCase):
    """KI-005: the delivered screenshot must not show the test harness."""

    def test_screenshot_is_taken_from_an_uninstrumented_copy(self):
        seen = {}

        class Recorder(BrowserVerifier):
            def _run_edge_harness(self, url, profile, server):
                seen["harness_url"] = url
                return {
                    "checks": [],
                    "console_errors": [],
                }

            def _take_screenshot(self, url, screenshot, profile):
                seen["shot_url"] = url
                seen["shot_html"] = urllib.request.urlopen(url, timeout=5).read().decode()
                screenshot.write_bytes(b"png")
                return True, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_sample_site(project)
            Recorder(edge_path=Path("/fake/msedge")).verify(project, sample_spec())

        self.assertNotEqual(seen["harness_url"], seen["shot_url"], "must be two servers")
        self.assertNotIn(RESULT_PATH, seen["shot_html"], "harness leaked into screenshot")
        self.assertNotIn("__roctoErrors", seen["shot_html"])
        self.assertIn('data-testid="increment"', seen["shot_html"])


class TeardownNoiseTests(unittest.TestCase):
    """KI-006: killing Edge mid-connection must not print tracebacks."""

    def _handle_error_with(self, exc):
        import io
        import sys as _sys
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_sample_site(site)
            with _serve(site) as (_url, server):
                captured = io.StringIO()
                try:
                    raise exc
                except type(exc):
                    with redirect_stderr(captured):
                        server.handle_error(None, ("127.0.0.1", 1234))
                    del _sys
                return captured.getvalue()

    def test_connection_reset_is_swallowed(self):
        # WinError 10054 on Windows: expected teardown, not a failure.
        self.assertEqual(self._handle_error_with(ConnectionResetError(10054, "reset")), "")

    def test_broken_pipe_is_swallowed(self):
        self.assertEqual(self._handle_error_with(BrokenPipeError()), "")

    def test_real_errors_are_still_reported(self):
        noise = self._handle_error_with(ValueError("something actually wrong"))
        self.assertIn("something actually wrong", noise)

    def test_browser_pipes_are_not_leaked(self):
        """Edge's output must not go to a PIPE we never drain."""
        import inspect

        source = inspect.getsource(BrowserVerifier._run_edge_harness)
        self.assertIn("stdout=subprocess.DEVNULL", source)
        self.assertNotIn("stderr=subprocess.PIPE", source)
