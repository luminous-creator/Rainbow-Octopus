from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from rainbow_octopus import doctor
from rainbow_octopus.doctor import DoctorCheck
from rainbow_octopus.verifier import (
    _headless_flag,
    browser_install_hint,
    browser_name,
    find_browser,
)


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"browser")
    return path


class BrowserDiscoveryTests(unittest.TestCase):
    def test_browser_override_wins_over_legacy_name_and_path(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            preferred = touch(root / "preferred-chrome")
            legacy = touch(root / "legacy-msedge")
            path_edge = touch(root / "path-msedge")
            env = {
                "ROCTO_BROWSER_BIN": str(preferred),
                "ROCTO_EDGE_BIN": str(legacy),
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch(
                    "rainbow_octopus.verifier.platform.system",
                    return_value="Linux",
                ),
                mock.patch(
                    "rainbow_octopus.verifier.shutil.which",
                    return_value=str(path_edge),
                ),
            ):
                self.assertEqual(find_browser(), preferred)

    def test_legacy_edge_override_still_works(self):
        with tempfile.TemporaryDirectory() as temp_name:
            legacy = touch(Path(temp_name) / "msedge")
            with (
                mock.patch.dict(
                    os.environ,
                    {"ROCTO_EDGE_BIN": str(legacy)},
                    clear=True,
                ),
                mock.patch(
                    "rainbow_octopus.verifier.platform.system",
                    return_value="Linux",
                ),
                mock.patch(
                    "rainbow_octopus.verifier.shutil.which",
                    return_value=None,
                ),
            ):
                self.assertEqual(find_browser(), legacy)

    def test_windows_prefers_edge_then_finds_chrome_without_edge(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            x86 = root / "Program Files (x86)"
            regular = root / "Program Files"
            edge = touch(
                x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
            chrome = touch(
                regular / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
            env = {
                "ProgramFiles(x86)": str(x86),
                "ProgramFiles": str(regular),
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch(
                    "rainbow_octopus.verifier.platform.system",
                    return_value="Windows",
                ),
                mock.patch(
                    "rainbow_octopus.verifier.shutil.which",
                    return_value=None,
                ),
            ):
                self.assertEqual(find_browser(), edge)
                edge.unlink()
                self.assertEqual(find_browser(), chrome)

    def test_windows_can_find_a_browser_on_fake_path(self):
        with tempfile.TemporaryDirectory() as temp_name:
            brave = touch(Path(temp_name) / "brave.exe")
            found = {"brave": str(brave)}
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch(
                    "rainbow_octopus.verifier.platform.system",
                    return_value="Windows",
                ),
                mock.patch(
                    "rainbow_octopus.verifier.shutil.which",
                    side_effect=lambda name: found.get(name),
                ),
            ):
                self.assertEqual(find_browser(), brave)

    def test_macos_searches_user_applications_after_system_applications(self):
        with tempfile.TemporaryDirectory() as temp_name:
            home = Path(temp_name)
            chrome = touch(
                home
                / "Applications"
                / "Google Chrome.app"
                / "Contents"
                / "MacOS"
                / "Google Chrome"
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch(
                    "rainbow_octopus.verifier.platform.system",
                    return_value="Darwin",
                ),
                mock.patch(
                    "rainbow_octopus.verifier.Path.home",
                    return_value=home,
                ),
                mock.patch(
                    "rainbow_octopus.verifier.shutil.which",
                    return_value=None,
                ),
            ):
                self.assertEqual(find_browser(), chrome)

    def test_linux_uses_documented_path_priority(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            chromium = touch(root / "chromium")
            edge = touch(root / "microsoft-edge")
            found = {
                "chromium": str(chromium),
                "microsoft-edge": str(edge),
            }
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch(
                    "rainbow_octopus.verifier.platform.system",
                    return_value="Linux",
                ),
                mock.patch(
                    "rainbow_octopus.verifier.shutil.which",
                    side_effect=lambda name: found.get(name),
                ),
            ):
                self.assertEqual(find_browser(), chromium)

    def test_returns_none_when_no_candidate_exists_on_each_platform(self):
        for system in ("Windows", "Darwin", "Linux"):
            with self.subTest(system=system):
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch(
                        "rainbow_octopus.verifier.platform.system",
                        return_value=system,
                    ),
                    mock.patch(
                        "rainbow_octopus.verifier.Path.home",
                        return_value=Path("/missing-home"),
                    ),
                    mock.patch(
                        "rainbow_octopus.verifier.shutil.which",
                        return_value=None,
                    ),
                    mock.patch(
                        "rainbow_octopus.verifier.Path.is_file",
                        return_value=False,
                    ),
                ):
                    self.assertIsNone(find_browser())

    def test_names_supported_browsers_and_keeps_edge_headless_mode(self):
        self.assertEqual(browser_name(Path("/opt/chromium")), "Chromium")
        self.assertEqual(browser_name(Path("/opt/brave-browser")), "Brave")
        self.assertEqual(browser_name(Path("C:/msedge.exe")), "Microsoft Edge")
        self.assertEqual(_headless_flag(Path("C:/msedge.exe")), "--headless=old")
        self.assertEqual(_headless_flag(Path("/usr/bin/chromium")), "--headless")


class BrowserDoctorTests(unittest.TestCase):
    def _run_with_browser(self, browser: Path | None):
        backend = DoctorCheck(
            "executor:deepseek",
            True,
            "ready",
            required=False,
        )
        with (
            mock.patch.dict(os.environ, {"ROCTO_API_KEY": "secret"}, clear=True),
            mock.patch("rainbow_octopus.doctor._backend_checks", return_value=[backend]),
            mock.patch("rainbow_octopus.doctor.find_browser", return_value=browser),
        ):
            return doctor.run_doctor()

    def test_doctor_names_the_browser_and_path(self):
        path = Path("/usr/bin/chromium")
        check = next(
            item for item in self._run_with_browser(path) if item.name == "browser"
        )
        self.assertTrue(check.passed)
        self.assertIn("Chromium", check.detail)
        self.assertIn(str(path), check.detail)

    def test_doctor_prints_a_platform_install_command_when_missing(self):
        for system, command in (
            ("Windows", "winget install"),
            ("Darwin", "brew install"),
            ("Linux", "apt install"),
        ):
            with self.subTest(system=system), mock.patch(
                "rainbow_octopus.doctor.browser_install_hint",
                return_value=browser_install_hint(system),
            ):
                check = next(
                    item
                    for item in self._run_with_browser(None)
                    if item.name == "browser"
                )
                self.assertFalse(check.passed)
                self.assertIn(command, check.detail)


if __name__ == "__main__":
    unittest.main()
