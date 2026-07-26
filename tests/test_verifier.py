from pathlib import Path
import tempfile
import unittest

from rainbow_octopus.verifier import BrowserVerifier, find_edge
from tests.helpers import sample_spec, write_sample_site


class VerifierTests(unittest.TestCase):
    def test_missing_files_fail_without_browser(self):
        with tempfile.TemporaryDirectory() as temp_name:
            report = BrowserVerifier(edge_path=Path("missing.exe")).verify(
                Path(temp_name), sample_spec()
            )
        self.assertFalse(report.passed)
        self.assertTrue(any(check.name == "required_file:index.html" for check in report.checks))

    def test_rejects_external_network_access(self):
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name)
            write_sample_site(project)
            (project / "script.js").write_text("fetch('https://example.com')", encoding="utf-8")
            report = BrowserVerifier(edge_path=Path("missing.exe")).verify(
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
            report = BrowserVerifier(edge_path=Path("missing.exe")).verify(
                project, sample_spec()
            )
        contract = next(
            check for check in report.checks if check.name == "testid_contract"
        )
        self.assertFalse(contract.passed)
        self.assertIn("increment", contract.detail)

    @unittest.skipUnless(find_edge(), "Microsoft Edge is not installed on this host")
    def test_real_edge_interaction_and_screenshot(self):
        """The one test that proves the whole interaction loop.

        Un-skipped now that KI-001 (--headless=old) and KI-003 (harness posts
        its verdict back instead of relying on --dump-dom) are fixed. It runs
        automatically wherever Edge exists and is skipped elsewhere, so CI on
        Linux stays green while a Windows host actually exercises it.
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


if __name__ == "__main__":
    unittest.main()
