from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from rainbow_octopus import doctor
from rainbow_octopus.doctor import DoctorCheck
from rainbow_octopus.verifier import (
    _browser_env,
    _graphics_flags,
    _headless_flag,
    _playwright_candidates,
    _sandbox_flags,
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

    def test_windows_supports_each_documented_path_name(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for executable in ("msedge", "chrome", "chromium", "brave"):
                with self.subTest(executable=executable):
                    browser = touch(root / f"{executable}.exe")
                    with (
                        mock.patch.dict(os.environ, {}, clear=True),
                        mock.patch(
                            "rainbow_octopus.verifier.platform.system",
                            return_value="Windows",
                        ),
                        mock.patch(
                            "rainbow_octopus.verifier.Path.is_file",
                            autospec=True,
                            side_effect=lambda path, target=browser: path == target,
                        ),
                        mock.patch(
                            "rainbow_octopus.verifier.shutil.which",
                            side_effect=lambda name, expected=executable, target=browser: (
                                str(target) if name == expected else None
                            ),
                        ),
                    ):
                        self.assertEqual(find_browser(), browser)

    def test_macos_supports_each_documented_user_application(self):
        with tempfile.TemporaryDirectory() as temp_name:
            home = Path(temp_name)
            applications = (
                ("Google Chrome.app", "Google Chrome"),
                ("Microsoft Edge.app", "Microsoft Edge"),
                ("Chromium.app", "Chromium"),
                ("Brave Browser.app", "Brave Browser"),
            )
            for app, executable in applications:
                with self.subTest(app=app):
                    browser = touch(
                        home
                        / "Applications"
                        / app
                        / "Contents"
                        / "MacOS"
                        / executable
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
                            "rainbow_octopus.verifier.Path.is_file",
                            autospec=True,
                            side_effect=lambda path, target=browser: path == target,
                        ),
                        mock.patch(
                            "rainbow_octopus.verifier.shutil.which",
                            return_value=None,
                        ),
                    ):
                        self.assertEqual(find_browser(), browser)

    def test_linux_supports_each_documented_path_name(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            path_names = (
                "google-chrome",
                "google-chrome-stable",
                "chromium",
                "chromium-browser",
                "microsoft-edge",
                "microsoft-edge-stable",
                "brave-browser",
            )
            for executable in path_names:
                with self.subTest(executable=executable):
                    browser = touch(root / executable)
                    with (
                        mock.patch.dict(os.environ, {}, clear=True),
                        mock.patch(
                            "rainbow_octopus.verifier.platform.system",
                            return_value="Linux",
                        ),
                        mock.patch(
                            "rainbow_octopus.verifier.Path.is_file",
                            autospec=True,
                            side_effect=lambda path, target=browser: path == target,
                        ),
                        mock.patch(
                            "rainbow_octopus.verifier.shutil.which",
                            side_effect=lambda name, expected=executable, target=browser: (
                                str(target) if name == expected else None
                            ),
                        ),
                    ):
                        self.assertEqual(find_browser(), browser)

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
        self.assertIn(
            "--disable-software-rasterizer",
            _graphics_flags(Path("C:/msedge.exe")),
        )
        self.assertEqual(
            _graphics_flags(Path("/Applications/Google Chrome")),
            (),
        )


class ContainerBrowserTests(unittest.TestCase):
    """KI-009: containers run as root and often only have Playwright's Chromium."""

    def test_playwright_chromium_is_found_newest_first_and_last_in_priority(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            old = touch(root / "chromium-1100" / "chrome-linux" / "chrome")
            new = touch(root / "chromium-1194" / "chrome-linux64" / "chrome")
            touch(root / "chromium_headless_shell-1194" / "chrome-linux" / "headless_shell")
            with (
                mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(root)}, clear=True),
                mock.patch("rainbow_octopus.verifier.Path.home", return_value=root / "nohome"),
            ):
                self.assertEqual(_playwright_candidates("Linux"), [new, old])
                with (
                    mock.patch("rainbow_octopus.verifier.platform.system", return_value="Linux"),
                    mock.patch("rainbow_octopus.verifier.shutil.which", return_value=None),
                    # The CI runner really has /opt/google/chrome; this test
                    # is about what happens when no system browser exists.
                    mock.patch(
                        "rainbow_octopus.verifier._browser_candidates",
                        return_value=([], ("chromium",)),
                    ),
                ):
                    self.assertEqual(find_browser(), new)
                system_chrome = touch(root / "usr-bin-chromium")
                with (
                    mock.patch("rainbow_octopus.verifier.platform.system", return_value="Linux"),
                    mock.patch(
                        "rainbow_octopus.verifier.shutil.which",
                        side_effect=lambda name: str(system_chrome) if name == "chromium" else None,
                    ),
                    mock.patch(
                        "rainbow_octopus.verifier._browser_candidates",
                        return_value=([], ("chromium",)),
                    ),
                ):
                    self.assertEqual(find_browser(), system_chrome)

    def test_a_missing_home_directory_is_not_a_crash(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("rainbow_octopus.verifier.Path.home", side_effect=RuntimeError("no home")),
        ):
            self.assertEqual(_playwright_candidates("Linux"), [])
            self.assertEqual(_playwright_candidates("Darwin"), [])

    def test_playwright_layouts_per_platform(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            win = touch(root / "chromium-1" / "chrome-win" / "chrome.exe")
            mac = touch(root / "chromium-1" / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium")
            with (
                mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(root)}, clear=True),
                mock.patch("rainbow_octopus.verifier.Path.home", return_value=root / "nohome"),
            ):
                self.assertEqual(_playwright_candidates("Windows"), [win])
                self.assertEqual(_playwright_candidates("Darwin"), [mac])

    def test_root_on_linux_disables_the_sandbox(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("rainbow_octopus.verifier.os.geteuid", return_value=0, create=True):
                self.assertIn("--no-sandbox", _sandbox_flags("Linux"))
                self.assertEqual(_sandbox_flags("Darwin"), ())
                self.assertEqual(_sandbox_flags("Windows"), ())
            with mock.patch("rainbow_octopus.verifier.os.geteuid", return_value=1000, create=True):
                self.assertEqual(_sandbox_flags("Linux"), ())

    def test_sandbox_can_be_forced_off_for_non_root(self):
        with mock.patch.dict(os.environ, {"ROCTO_BROWSER_NO_SANDBOX": "1"}, clear=True):
            with mock.patch("rainbow_octopus.verifier.os.geteuid", return_value=1000, create=True):
                self.assertIn("--no-sandbox", _sandbox_flags("Linux"))

    def test_the_browser_never_sees_credentials(self):
        env = {
            "PATH": "/bin",
            "HOME": "/home/x",
            "DEEPSEEK_API_KEY": "sk-1",
            "ROCTO_API_KEY": "sk-2",
            "GITHUB_TOKEN": "ghs",
            "AWS_SECRET_ACCESS_KEY": "a",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            scrubbed = _browser_env()
        self.assertEqual(scrubbed, {"PATH": "/bin", "HOME": "/home/x"})


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
