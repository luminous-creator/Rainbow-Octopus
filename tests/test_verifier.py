from pathlib import Path
import os
import tempfile
import unittest

from rainbow_octopus.models import TaskSpec
from rainbow_octopus.verifier import BrowserVerifier, _declares_testid, find_browser
from tests.helpers import sample_spec, write_sample_site


def browser_test_reason() -> str | None:
    """Why the live browser test cannot run here, or None if it can.

    `ROCTO_SKIP_BROWSER_TESTS=1` exists for coding agents. An agent that runs
    this suite inside its own sandbox can usually see the Edge binary but
    cannot actually launch it, so the test hangs until the timeout and the
    agent concludes the project is broken. That has now happened twice — once
    with file writes (KI-002) and once with the browser — and both times the
    real code was fine and the sandbox was the constraint. Set the variable and
    the test is skipped honestly instead of failing misleadingly.
    """
    if os.environ.get("ROCTO_SKIP_BROWSER_TESTS") == "1":
        return "ROCTO_SKIP_BROWSER_TESTS=1 (agent sandbox: cannot launch a browser)"
    if not find_browser():
        return "no supported Chromium browser is installed on this host"
    return None


class VerifierTests(unittest.TestCase):
    def test_missing_files_fail_without_browser(self):
        with tempfile.TemporaryDirectory() as temp_name:
            report = BrowserVerifier(browser_path=Path("missing.exe")).verify(
                Path(temp_name), sample_spec()
            )
        self.assertFalse(report.passed)
        self.assertTrue(any(check.name == "required_file:index.html" for check in report.checks))

    def test_rejects_external_network_access(self):
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name)
            write_sample_site(project)
            (project / "script.js").write_text("fetch('https://example.com')", encoding="utf-8")
            report = BrowserVerifier(browser_path=Path("missing.exe")).verify(
                project, sample_spec()
            )
        offline = next(check for check in report.checks if check.name == "offline_only")
        self.assertFalse(offline.passed)

    def test_rejects_missing_declared_testid_before_browser(self):
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name)
            write_sample_site(project)
            html = (project / "index.html").read_text(encoding="utf-8")
            (project / "index.html").write_text(
                html.replace('data-testid="increment"', ""),
                encoding="utf-8",
            )
            report = BrowserVerifier(browser_path=Path("missing.exe")).verify(
                project, sample_spec()
            )
        contract = next(
            check for check in report.checks if check.name == "testid_contract"
        )
        self.assertFalse(contract.passed)
        self.assertIn("increment", contract.detail)

    @unittest.skipIf(browser_test_reason(), browser_test_reason() or "")
    def test_real_browser_interaction_and_screenshot(self):
        """The one test that proves the whole interaction loop.

        Un-skipped now that KI-001 (--headless=old) and KI-003 (harness posts
        its verdict back instead of relying on --dump-dom) are fixed. It runs
        automatically wherever a supported Chromium browser exists and is
        skipped only when none can be found.
        """
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name)
            write_sample_site(project)
            report = BrowserVerifier().verify(project, sample_spec())
            details = [(check.name, check.passed, check.detail) for check in report.checks]
            self.assertTrue(report.passed, details)
            self.assertTrue((project / "screenshot.png").is_file())
            names = {check.name for check in report.checks}
            self.assertIn("browser_run", names)
            self.assertIn("increments:click", names)

    @unittest.skipIf(browser_test_reason(), browser_test_reason() or "")
    def test_every_test_starts_from_a_fresh_page(self):
        """KI-011: tests are independent cases, so each gets its own page load.

        The page keeps its count in localStorage and renders list items from
        script.js. Both tests click once and expect exactly one item and a
        count of 1 — satisfiable only if neither memory nor storage leaks from
        the first test into the second.
        """
        step = lambda action, testid=None, **kw: {  # noqa: E731
            "action": action,
            **({"selector": f'[data-testid="{testid}"]'} if testid else {}),
            "timeout_ms": 0,
            **kw,
        }
        independent = [
            step("fill", "name", value="milk"),
            step("click", "add"),
            step("text_visible", "count", expected="1"),
            step("text_visible", "item", expected="milk"),
            step("attribute_equals", "name", attribute="value", expected=""),
            step("text_visible", "empty", expected="nothing"),
        ]
        spec = TaskSpec.from_dict(
            {
                "title": "List",
                "goal": "Add items",
                "features": ["Add"],
                "constraints": ["None"],
                "ui_contract": [
                    {"test_id": t, "purpose": t}
                    for t in ("name", "add", "count", "item", "empty")
                ],
                "tests": [
                    {"name": "first", "steps": independent},
                    {"name": "second", "steps": independent},
                ],
            }
        )
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name)
            (project / "index.html").write_text(
                """<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body>
<input data-testid="name"><button data-testid="add">Add</button>
<output data-testid="count">0</output><ul id="list"></ul>
<p data-testid="empty" hidden>nothing</p>
<script src="script.js"></script></body></html>""",
                encoding="utf-8",
            )
            (project / "styles.css").write_text("body{}", encoding="utf-8")
            (project / "script.js").write_text(
                """let n = Number(localStorage.getItem("n") || 0);
const count = document.querySelector('[data-testid="count"]');
const input = document.querySelector('[data-testid="name"]');
count.textContent = String(n);
document.querySelector('[data-testid="add"]').addEventListener("click", () => {
  const li = document.createElement("li");
  li.dataset.testid = "item";
  li.textContent = input.value;
  document.getElementById("list").appendChild(li);
  input.value = "";
  n += 1; localStorage.setItem("n", String(n)); count.textContent = String(n);
});""",
                encoding="utf-8",
            )
            (project / "README.md").write_text("# List", encoding="utf-8")
            report = BrowserVerifier().verify(project, spec)
        checks = {(c.name, c.passed): c.detail for c in report.checks}
        details = [(c.name, c.passed, c.detail) for c in report.checks]
        self.assertTrue(
            next(c for c in report.checks if c.name == "testid_contract").passed,
            "items created by script.js count as declared",
        )
        counts = [c for c in report.checks if c.name.endswith(":text_visible") and "actual=1" in c.detail]
        self.assertEqual(len(counts), 2, details)
        self.assertTrue(all(c.passed for c in counts), details)
        values = [c for c in report.checks if c.name.endswith(":attribute_equals")]
        self.assertTrue(all(c.passed for c in values), "value reads the live property: " + str(details))
        hidden = [c for c in report.checks if "expected=nothing" in c.detail]
        self.assertTrue(hidden and all(not c.passed and "element is hidden" in c.detail for c in hidden), details)
        self.assertEqual(len([c for c in report.checks if c.name.startswith("second:")]), 6, details)

    @unittest.skipIf(browser_test_reason(), browser_test_reason() or "")
    def test_phone_screenshots_lay_out_at_phone_width(self):
        """Headless Chromium's 500 px minimum window cropped phone captures."""
        from rainbow_octopus.verifier import capture
        import struct

        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name) / "site"
            project.mkdir()
            write_sample_site(project)
            (project / "index.html").write_text(
                '<!doctype html><meta name="viewport" content="width=device-width">'
                '<link rel="stylesheet" href="styles.css"><body>'
                '<div id="w" data-testid="count"></div><script src="script.js"></script>',
                encoding="utf-8",
            )
            target = Path(temp_name) / "phone.png"
            self.assertTrue(capture(project, target, window_size="390,844"))
            width, height = struct.unpack(">II", target.read_bytes()[16:24])
        self.assertGreaterEqual(width, 390 + 2 * 28)
        self.assertGreaterEqual(height, 844)


class TestIdDeclarationTests(unittest.TestCase):
    def test_script_created_ids_count_but_lookups_do_not(self):
        lookup = "document.querySelector('[data-testid=\"row\"]')"
        self.assertFalse(_declares_testid("row", "<body></body>", lookup))
        self.assertTrue(_declares_testid("row", "", 'li.dataset.testid = "row";'))
        self.assertTrue(_declares_testid("row", "", "el.setAttribute('data-testid', 'row')"))
        self.assertTrue(_declares_testid("row", "", "html += `<li data-testid=\"row\">`"))
        self.assertTrue(_declares_testid("row", '<li data-testid="row">', ""))


if __name__ == "__main__":
    unittest.main()
