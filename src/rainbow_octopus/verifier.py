from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from html import unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .models import AcceptanceCheck, AcceptanceReport, TaskSpec


REQUIRED_FILES = ("index.html", "styles.css", "script.js", "README.md")

#: KI-004: the old pattern was `(?:https?:)?//|fetch\(|...`, and `//` matches
#: every ordinary JavaScript line comment. Practically every model-written
#: script.js contains one, so `offline_only` failed on almost every real build
#: and burned all repair attempts on a page that was perfectly fine.
#:
#: Comments are now stripped before scanning — but *string-aware*, because a
#: naive stripper would treat the `//` inside "http://evil.com" as the start of
#: a comment and delete the rest of the line, hiding a genuine external URL.
EXTERNAL_PATTERN = re.compile(
    r"""(?:https?:)?//|fetch\s*\(|XMLHttpRequest|WebSocket\s*\(|
        \bimport\s*\(|navigator\.sendBeacon|EventSource\s*\(""",
    re.IGNORECASE | re.VERBOSE,
)
#: The harness still appends <pre id="rocto-result"> for human debugging, but
#: the verdict now travels over POST (KI-003), so nothing scrapes the DOM.
RESULT_ELEMENT_ID = "rocto-result"


def _blank_keeping_newlines(chunk: str) -> str:
    """Blank a span but keep its newlines, so line numbers stay meaningful."""
    return "".join("\n" if char == "\n" else " " for char in chunk)


def strip_comments(text: str) -> str:
    """Blank out comments so the offline scan only sees real code (KI-004).

    Handles ``//``, ``/* */`` and ``<!-- -->``. Crucially it tracks string
    literals — ``'``, ``"`` and backticks, with backslash escapes — so that the
    ``//`` inside ``"http://evil.com"`` is *not* mistaken for a comment. Getting
    that backwards would turn a false positive into a silent bypass.

    Comment bodies are replaced with spaces rather than deleted, so offsets and
    line structure survive for error messages.
    """
    out = []
    i = 0
    end = len(text)
    quote: str | None = None

    while i < end:
        char = text[i]

        if quote:
            out.append(char)
            if char == "\\" and i + 1 < end:
                out.append(text[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue

        if char in "'\"`":
            quote = char
            out.append(char)
            i += 1
            continue

        if text.startswith("//", i):
            while i < end and text[i] != "\n":
                out.append(" ")
                i += 1
            continue

        if text.startswith("/*", i):
            close = text.find("*/", i + 2)
            close = end if close == -1 else close + 2
            out.append(_blank_keeping_newlines(text[i:close]))
            i = close
            continue

        if text.startswith("<!--", i):
            close = text.find("-->", i + 4)
            close = end if close == -1 else close + 3
            out.append(_blank_keeping_newlines(text[i:close]))
            i = close
            continue

        out.append(char)
        i += 1

    return "".join(out)


def _under(root: str | Path | None, *parts: str) -> Path | None:
    """Build a candidate only when its root is configured."""
    if not root:
        return None
    return Path(root).joinpath(*parts)


def _browser_candidates(system: str) -> tuple[list[Path | None], tuple[str, ...]]:
    """Return fixed-path and PATH candidates in platform priority order."""
    if system == "Windows":
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        program_files = os.environ.get("ProgramFiles")
        local_app_data = os.environ.get("LOCALAPPDATA")
        fixed = [
            _under(
                program_files_x86,
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe",
            ),
            _under(
                program_files,
                "Microsoft",
                "Edge",
                "Application",
                "msedge.exe",
            ),
            _under(
                program_files,
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            _under(
                program_files_x86,
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            _under(
                local_app_data,
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            _under(
                program_files,
                "Chromium",
                "Application",
                "chrome.exe",
            ),
            _under(
                program_files_x86,
                "Chromium",
                "Application",
                "chrome.exe",
            ),
            _under(
                local_app_data,
                "Chromium",
                "Application",
                "chrome.exe",
            ),
            _under(
                program_files,
                "BraveSoftware",
                "Brave-Browser",
                "Application",
                "brave.exe",
            ),
            _under(
                program_files_x86,
                "BraveSoftware",
                "Brave-Browser",
                "Application",
                "brave.exe",
            ),
            _under(
                local_app_data,
                "BraveSoftware",
                "Brave-Browser",
                "Application",
                "brave.exe",
            ),
        ]
        return fixed, ("msedge", "chrome", "chromium", "brave")

    if system == "Darwin":
        applications = (Path("/Applications"), Path.home() / "Applications")
        app_binaries = (
            ("Google Chrome.app", "Google Chrome"),
            ("Microsoft Edge.app", "Microsoft Edge"),
            ("Chromium.app", "Chromium"),
            ("Brave Browser.app", "Brave Browser"),
        )
        fixed = [
            root / app / "Contents" / "MacOS" / executable
            for root in applications
            for app, executable in app_binaries
        ]
        return fixed, ("google-chrome", "chromium", "microsoft-edge", "brave")

    # Linux and other Unix-like hosts use the conventional executable names.
    return [Path("/opt/google/chrome/chrome"), Path("/snap/bin/chromium")], (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
        "brave-browser",
    )


def find_browser() -> Path | None:
    """Find a supported Chromium browser on Windows, macOS or Linux.

    ``ROCTO_BROWSER_BIN`` is the preferred explicit override.
    ``ROCTO_EDGE_BIN`` remains supported as its legacy name.
    """
    for variable in ("ROCTO_BROWSER_BIN", "ROCTO_EDGE_BIN"):
        value = os.environ.get(variable)
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_file():
                return candidate

    system = platform.system()
    fixed, path_names = _browser_candidates(system)
    candidates: list[Path | None] = list(fixed)
    for name in path_names:
        found = shutil.which(name)
        candidates.append(Path(found) if found else None)
    # Last resort, and deliberately last: a browser that Playwright downloaded
    # for some other project. Containers and CI images often have nothing else.
    candidates.extend(_playwright_candidates(system))
    return next((path for path in candidates if path and path.is_file()), None)


#: Relative layout of a Playwright-managed Chromium, per platform. Newer
#: Playwright releases append an architecture (``chrome-linux64``,
#: ``chrome-mac-arm64``, ``chrome-win64``), hence the trailing wildcards.
_PLAYWRIGHT_LAYOUTS = {
    "Windows": ("chrome-win*", "chrome.exe"),
    "Darwin": ("chrome-mac*", "Chromium.app/Contents/MacOS/Chromium"),
    "Linux": ("chrome-linux*", "chrome"),
}


def _playwright_roots(system: str) -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        roots.append(Path(configured).expanduser())
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "ms-playwright")
    elif system == "Darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")
    return roots


def _playwright_candidates(system: str) -> list[Path]:
    """Full Chromium builds that Playwright installed, newest revision first.

    ``chromium_headless_shell-*`` is skipped on purpose: it has no
    ``--screenshot`` parity guarantees and is not a browser a user would
    recognise in ``rocto doctor`` output.
    """
    platform_dir, executable = _PLAYWRIGHT_LAYOUTS.get(
        system, _PLAYWRIGHT_LAYOUTS["Linux"]
    )
    found: list[tuple[int, Path]] = []
    for root in _playwright_roots(system):
        try:
            builds = list(root.glob("chromium-*"))
        except OSError:
            continue
        for build in builds:
            revision = build.name.rsplit("-", 1)[-1]
            if not revision.isdigit():
                continue
            for platform_root in sorted(build.glob(platform_dir)):
                found.append((int(revision), platform_root / executable))
    return [path for _, path in sorted(found, key=lambda item: -item[0])]


def find_edge() -> Path | None:
    """Backward-compatible alias for callers using the pre-KI-008 name."""
    return find_browser()


def browser_name(path: Path) -> str:
    """Return a human-facing name for a discovered executable."""
    lowered = str(path).lower()
    if "brave" in lowered:
        return "Brave"
    if "msedge" in lowered or "microsoft edge" in lowered:
        return "Microsoft Edge"
    if "chromium" in lowered:
        return "Chromium"
    if "chrome" in lowered:
        return "Google Chrome"
    return "Configured Chromium browser"


def browser_install_hint(system: str | None = None) -> str:
    """Give a practical next step when discovery finds no browser."""
    host = system or platform.system()
    if host == "Windows":
        return "install with: winget install --id Google.Chrome"
    if host == "Darwin":
        return "install with: brew install --cask google-chrome"
    return "install with: sudo apt install chromium-browser (or your distro package)"


def _headless_flag(path: Path) -> str:
    """Keep KI-001's proven Edge mode; use current headless elsewhere."""
    if browser_name(path) == "Microsoft Edge":
        return "--headless=old"
    return "--headless"


def _graphics_flags(path: Path) -> tuple[str, ...]:
    """Scope KI-001's Windows Edge workaround to the browser it fixed."""
    if browser_name(path) == "Microsoft Edge":
        return (
            "--disable-gpu",
            "--disable-gpu-sandbox",
            "--disable-software-rasterizer",
            "--disable-features=Vulkan,CanvasOopRasterization,UseSkiaRenderer",
        )
    return ()


def _declares_testid(test_id: str, html_text: str, script_text: str) -> bool:
    """Is ``test_id`` on the page, or put there by the page's script?

    KI-011: list items, rows and cards exist only after the script renders
    them, so a todo list's ``todo-item`` is legitimately absent from
    index.html. Demanding it there sent the executor to repair a page that
    was fine — or to add a hidden placeholder just to satisfy the check. The
    browser run still has the final word on whether the element appears.
    """
    quoted = re.escape(test_id)
    markup = re.compile(rf"""data-testid\s*=\s*\\?["']{quoted}\\?["']""")
    if markup.search(html_text):
        return True
    # Looking an element up is not creating it: drop every [data-testid=...]
    # selector first, then accept the id in generated markup, in
    # setAttribute/dataset calls, or as a string constant.
    creating = _TESTID_SELECTOR.sub(" ", script_text)
    if markup.search(creating):
        return True
    return re.search(rf"""["'`]{quoted}["'`]""", creating) is not None


_TESTID_SELECTOR = re.compile(r"""\[\s*data-testid\s*=\s*\\?["'][^"'\]]*\\?["']\s*\]""")


def _sandbox_flags(system: str | None = None) -> tuple[str, ...]:
    """KI-009: Chromium refuses to start as root unless its sandbox is off.

    Docker containers, most CI containers and cloud dev boxes run as root, and
    there Chromium exits immediately with "Running as root without
    --no-sandbox is not supported". The harness then waits out its full
    timeout and reports a failure that has nothing to do with the page.

    The page under test is our own generated, offline-scanned, locally served
    code, so running it without Chromium's sandbox as root costs little; not
    running it at all makes verification impossible. ``--disable-dev-shm-usage``
    is added alongside because container ``/dev/shm`` is often 64 MB, which
    crashes renderers. ``ROCTO_BROWSER_NO_SANDBOX=1`` forces the same flags
    for hosts where unprivileged sandboxes are blocked for a non-root user.
    """
    host = system or platform.system()
    if host != "Linux":
        return ()
    forced = os.environ.get("ROCTO_BROWSER_NO_SANDBOX", "").strip().lower()
    geteuid = getattr(os, "geteuid", None)
    is_root = bool(geteuid) and geteuid() == 0
    if is_root or forced in {"1", "true", "yes", "on"}:
        return ("--no-sandbox", "--disable-dev-shm-usage")
    return ()


#: Environment variables that must never reach the browser process. The page
#: under test is model-written; nothing it can reach should hold a credential.
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _browser_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in _SECRET_MARKERS)
    }


class BrowserVerifier:
    def __init__(
        self,
        browser_path: Path | None = None,
        timeout: int = 30,
        *,
        edge_path: Path | None = None,
    ):
        # edge_path is kept as a keyword-only compatibility bridge for v0.1
        # callers. New code should use browser_path.
        self.browser_path = browser_path or edge_path or find_browser()
        self.edge_path = self.browser_path
        self.timeout = timeout

    def verify(self, project_dir: Path, spec: TaskSpec) -> AcceptanceReport:
        # KI-010: a screenshot from an earlier attempt must never survive into
        # this one's report — not when this run stops before the browser, and
        # not when the new capture fails.
        stale = project_dir / "screenshot.png"
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
        checks: list[AcceptanceCheck] = []
        for name in REQUIRED_FILES:
            exists = (project_dir / name).is_file()
            checks.append(
                AcceptanceCheck(
                    f"required_file:{name}",
                    exists,
                    "present" if exists else "missing",
                )
            )
        if not all(check.passed for check in checks):
            return AcceptanceReport(False, checks)

        security_findings = self._security_findings(project_dir)
        checks.append(
            AcceptanceCheck(
                "offline_only",
                not security_findings,
                "no external access detected"
                if not security_findings
                else "; ".join(security_findings),
            )
        )
        html_text = (project_dir / "index.html").read_text(
            encoding="utf-8", errors="replace"
        )
        script_text = strip_comments(
            (project_dir / "script.js").read_text(encoding="utf-8", errors="replace")
        )
        missing_ids = [
            item.test_id
            for item in spec.ui_contract
            if not _declares_testid(item.test_id, html_text, script_text)
        ]
        checks.append(
            AcceptanceCheck(
                "testid_contract",
                not missing_ids,
                "all declared data-testid values are present"
                if not missing_ids
                else "missing: " + ", ".join(missing_ids)
                + " (neither in index.html nor created by script.js)",
            )
        )
        if missing_ids:
            return AcceptanceReport(False, checks)
        if not self.browser_path:
            checks.append(
                AcceptanceCheck(
                    "browser_available",
                    False,
                    f"supported Chromium browser not found; {browser_install_hint()}",
                )
            )
            return AcceptanceReport(False, checks)

        console_errors: list[str] = []
        with tempfile.TemporaryDirectory(prefix="rocto-verify-") as temp_name:
            temp = Path(temp_name)

            # KI-005: two staging copies, deliberately.
            #
            # "site" carries the injected harness and is what we click through.
            # "shot" is a pristine copy, and the screenshot comes from there.
            # Screenshotting the instrumented page put the harness's own
            # <pre id="rocto-result"> JSON blob at the bottom of the image the
            # user is handed — and non-deterministically, since it depends on
            # whether the async harness finished before the capture.
            staging = temp / "site"
            pristine = temp / "shot"
            staging.mkdir()
            pristine.mkdir()
            for name in ("index.html", "styles.css", "script.js"):
                shutil.copy2(project_dir / name, staging / name)
                shutil.copy2(project_dir / name, pristine / name)
            self._inject_harness(staging / "index.html", spec)

            with _serve(staging) as (url, server):
                server.rocto_timeout = self._harness_timeout(spec)
                harness = self._run_edge_harness(url, temp / "profile", server)
                checks.extend(harness["checks"])
                console_errors = harness["console_errors"]

            with _serve(pristine) as (shot_url, _):
                # KI-010: capture into a fresh path and copy only on success.
                # Waiting for "a stable, non-empty file" at a path that
                # already held last attempt's PNG returned immediately, killed
                # the browser before it wrote, and shipped the old image.
                captured = temp / "screenshot.png"
                screenshot_ok, screenshot_detail = self._take_screenshot(
                    shot_url, captured, temp / "screenshot-profile"
                )
                if screenshot_ok and captured.is_file():
                    shutil.copy2(captured, project_dir / "screenshot.png")
                checks.append(
                    AcceptanceCheck("screenshot", screenshot_ok, screenshot_detail)
                )

        return AcceptanceReport(
            passed=all(check.passed for check in checks),
            checks=checks,
            console_errors=console_errors,
            screenshot="screenshot.png"
            if (project_dir / "screenshot.png").is_file()
            else None,
        )

    def _security_findings(self, project_dir: Path) -> list[str]:
        findings = []
        for name in ("index.html", "styles.css", "script.js"):
            text = (project_dir / name).read_text(encoding="utf-8", errors="replace")
            stripped = strip_comments(text)
            match = EXTERNAL_PATTERN.search(stripped)
            if match:
                findings.append(
                    f"{name} contains external/network access: {match.group(0)!r}"
                )
        return findings

    def _inject_harness(self, index_path: Path, spec: TaskSpec) -> None:
        """Inject the test runner.

        KI-011: every test runs in its own page load. The planner writes each
        test as an independent case — "add a task, delete it, the list is
        empty" — and the harness used to run them back to back in one page,
        so each test inherited whatever the previous ones left behind. A todo
        contract whose fourth test assumed an empty list was unsatisfiable by
        any correct page, and all three attempts were spent on it. Now test
        ``i`` loads ``index.html?rocto_test=i`` with localStorage and
        sessionStorage cleared before the page's own scripts run, posts its
        part of the verdict, and navigates to the next test.
        """
        html = index_path.read_text(encoding="utf-8")
        tests = [
            {
                "name": test.name,
                "steps": [{"test": test.name, **step.__dict__} for step in test.steps],
            }
            for test in spec.tests
        ]
        early = """
<script>
window.__roctoErrors = [];
try { localStorage.clear(); } catch (e) {}
try { sessionStorage.clear(); } catch (e) {}
window.addEventListener("error", e => window.__roctoErrors.push(String(e.message)));
window.addEventListener("unhandledrejection", e => window.__roctoErrors.push(String(e.reason)));
const __roctoOriginalError = console.error;
console.error = (...args) => {
  window.__roctoErrors.push(args.map(String).join(" "));
  __roctoOriginalError.apply(console, args);
};
</script>
"""
        result_path = RESULT_PATH
        watchdog_ms = max(2000, (self._harness_timeout(spec) - 5) * 1000)
        runner = f"""
<script>
window.addEventListener("DOMContentLoaded", async () => {{
  const tests = {json.dumps(tests, ensure_ascii=False)};
  const params = new URLSearchParams(location.search);
  const index = Number(params.get("rocto_test") || "0");
  const test = tests[index];
  const checks = [];
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const record = (name, passed, detail) => checks.push({{name, passed, detail}});
  const isVisible = el => {{
    if (!el) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && !el.closest("[hidden]");
  }};
  const where = (el) => !el ? "; element not found" : (isVisible(el) ? "" : "; element is hidden");
  // attribute_equals on value/checked means the live state, not the markup.
  const readAttribute = (el, name) => {{
    if (!el) return null;
    if (name === "value" && "value" in el) return String(el.value);
    if (name === "checked" && typeof el.checked === "boolean") return String(el.checked);
    return el.getAttribute(name);
  }};
  for (const step of test ? test.steps : []) {{
    try {{
      const el = step.selector ? document.querySelector(step.selector) : null;
      if (step.action === "click") {{
        if (!el) throw new Error("selector not found");
        el.click();
        record(step.test + ":click", true, step.selector);
      }} else if (step.action === "fill") {{
        if (!el) throw new Error("selector not found");
        el.value = step.value;
        el.dispatchEvent(new Event("input", {{bubbles: true}}));
        el.dispatchEvent(new Event("change", {{bubbles: true}}));
        record(step.test + ":fill", true, step.selector);
      }} else if (step.action === "wait") {{
        await sleep(step.timeout_ms || 0);
        record(step.test + ":wait", true, String(step.timeout_ms || 0));
      }} else if (step.action === "selector_exists") {{
        record(step.test + ":selector_exists", Boolean(el),
          step.selector + (el ? "" : " not found"));
      }} else if (step.action === "text_visible") {{
        const actual = el ? (el.textContent || "").trim() : "";
        record(step.test + ":text_visible", isVisible(el) && actual.includes(step.expected),
          "expected=" + step.expected + "; actual=" + actual + where(el));
      }} else if (step.action === "attribute_equals") {{
        const actual = readAttribute(el, step.attribute);
        record(step.test + ":attribute_equals", actual === step.expected,
          "expected=" + step.expected + "; actual=" + actual + (el ? "" : "; element not found"));
      }} else if (step.action === "no_console_errors") {{
        record(step.test + ":no_console_errors", window.__roctoErrors.length === 0,
          window.__roctoErrors.join(" | ") || "none");
      }}
      if (step.timeout_ms && step.action !== "wait") await sleep(step.timeout_ms);
    }} catch (error) {{
      record(step.test + ":" + step.action, false, String(error));
    }}
  }}
  __roctoPost({{part: index, parts: tests.length, checks: checks,
    console_errors: window.__roctoErrors}});
  if (index + 1 < tests.length) {{
    location.replace(location.pathname + "?rocto_test=" + (index + 1));
  }} else {{
    __roctoShowResult(checks);
  }}
}});

function __roctoPost(verdict) {{
  if (window.__roctoPosted) return;
  window.__roctoPosted = true;
  const payload = JSON.stringify(verdict);
  try {{
    const blob = new Blob([payload], {{type: "application/json"}});
    if (!navigator.sendBeacon("{result_path}", blob)) {{
      fetch("{result_path}", {{method: "POST", body: payload, keepalive: true}});
    }}
  }} catch (error) {{
    fetch("{result_path}", {{method: "POST", body: payload, keepalive: true}});
  }}
}}

// Kept for humans debugging a staged copy; nothing reads it (KI-003).
function __roctoShowResult(checks) {{
  const result = document.createElement("pre");
  result.id = "rocto-result";
  result.textContent = JSON.stringify(checks);
  document.body.appendChild(result);
}}

// Watchdog: never let a hung page stall the verifier silently.
setTimeout(() => __roctoPost({{checks: [{{
  name: "harness:watchdog",
  passed: false,
  detail: "harness did not finish within {watchdog_ms}ms",
}}]}}), {watchdog_ms});
</script>
"""
        if re.search(r"<head[^>]*>", html, re.IGNORECASE):
            html = re.sub(
                r"(<head[^>]*>)",
                lambda match: match.group(1) + early,
                html,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            html = early + html
        if re.search(r"</body>", html, re.IGNORECASE):
            html = re.sub(
                r"</body>", runner + "</body>", html, count=1, flags=re.IGNORECASE
            )
        else:
            html += runner
        index_path.write_text(html, encoding="utf-8")

    def _harness_timeout(self, spec: TaskSpec | None) -> int:
        """Seconds to wait for every test's verdict.

        One page load per test (KI-011) and up to 3 s of waiting per step
        means a large contract can legitimately need more than the flat
        default. The budget is the configured timeout or the contract's own
        worst case plus a few seconds per page load, whichever is larger.
        """
        if spec is None:
            return self.timeout
        waits = sum(
            (step.timeout_ms or 0) for test in spec.tests for step in test.steps
        ) / 1000
        return int(max(self.timeout, waits + 4 * len(spec.tests) + 10))

    def _run_edge_harness(self, url: str, profile: Path, server) -> dict:
        """Run the page in headless Edge and wait for the harness to POST back.

        KI-003: the previous implementation used ``--dump-dom``, which snapshots
        the DOM around the load event. The harness is asynchronous (it clicks,
        waits, then asserts), so the ``#rocto-result`` node did not exist yet and
        every run reported "result missing" even though the page was fine.

        Edge is now launched as a long-running process; the harness posts its
        JSON verdict to the local server and we tear the browser down as soon as
        it arrives. No timing guesswork, no ``--virtual-time-budget``.
        """
        command = [
            str(self.browser_path),
            # KI-001: --headless=new crashes the GPU process on Windows 11 and
            # then blocks forever. Old headless is stable.
            _headless_flag(self.browser_path),
            *_graphics_flags(self.browser_path),
            *_sandbox_flags(),
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            f"--user-data-dir={profile}",
            url,
        ]
        # Edge's own stderr is chatty (GPU warnings, task-provider notices) and
        # we never read it while it runs. A PIPE would leak a file object and,
        # worse, could fill its buffer and block the browser, so it goes to a
        # file we only look at when something went wrong.
        profile.parent.mkdir(parents=True, exist_ok=True)
        log_path = profile.parent / "edge-stderr.log"
        try:
            log_file = log_path.open("w", encoding="utf-8", errors="replace")
        except OSError:
            log_file = None

        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdout=subprocess.DEVNULL,
                stderr=log_file or subprocess.DEVNULL,
                env=_browser_env(),
            )
        except OSError as exc:
            if log_file:
                log_file.close()
            return {
                "checks": [AcceptanceCheck("browser_run", False, str(exc))],
                "console_errors": [],
            }

        budget = getattr(server, "rocto_timeout", None) or self.timeout
        try:
            delivered = server.rocto_ready.wait(budget)
        finally:
            _terminate(process)
            if log_file:
                log_file.close()

        if not delivered:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:]
            except OSError:
                pass
            detail = f"harness did not report within {budget}s"
            if server.rocto_parts:
                detail += (
                    f" (finished {len(server.rocto_parts)} of "
                    f"{server.rocto_expected} tests)"
                )
            if tail.strip():
                detail = f"{detail}; edge stderr: {tail.strip()}"
            return {
                "checks": [AcceptanceCheck("browser_run", False, detail)],
                "console_errors": [],
            }
        return _parse_harness_payload(server.rocto_result)

    def _take_screenshot(
        self, url: str, screenshot: Path, profile: Path
    ) -> tuple[bool, str]:
        command = [
            str(self.browser_path),
            # KI-001: see _run_edge_harness — old headless required on Windows 11.
            _headless_flag(self.browser_path),
            *_graphics_flags(self.browser_path),
            *_sandbox_flags(),
            "--hide-scrollbars",
            "--no-first-run",
            f"--user-data-dir={profile}",
            "--window-size=1440,1000",
            f"--screenshot={screenshot}",
            url,
        ]
        if browser_name(self.browser_path) != "Microsoft Edge":
            return self._take_screenshot_until_written(command, screenshot, profile)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
                env=_browser_env(),
            )
            ok = (
                result.returncode == 0
                and screenshot.is_file()
                and screenshot.stat().st_size > 0
            )
            return (
                ok,
                "screenshot.png created"
                if ok
                else (result.stderr[-500:] or "failed"),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)

    def _take_screenshot_until_written(
        self, command: list[str], screenshot: Path, profile: Path
    ) -> tuple[bool, str]:
        """Stop unified Chromium headless once its screenshot is complete.

        Chrome on macOS can leave its parent process alive after writing the
        requested screenshot. Waiting for process exit turns a valid capture
        into a timeout, so non-Edge browsers are watched for a stable, non-empty
        output file and then torn down explicitly. Edge keeps KI-001's proven
        ``subprocess.run`` path above.
        """
        log_path = profile.parent / "browser-screenshot-stderr.log"
        try:
            log_file = log_path.open("w", encoding="utf-8", errors="replace")
        except OSError:
            log_file = None

        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdout=subprocess.DEVNULL,
                stderr=log_file or subprocess.DEVNULL,
                env=_browser_env(),
            )
        except OSError as exc:
            if log_file:
                log_file.close()
            return False, str(exc)

        deadline = time.monotonic() + self.timeout
        last_size = -1
        stable_since: float | None = None
        ready = False
        try:
            while time.monotonic() < deadline:
                try:
                    size = screenshot.stat().st_size if screenshot.is_file() else 0
                except OSError:
                    size = 0
                if size > 0:
                    now = time.monotonic()
                    if size != last_size:
                        last_size = size
                        stable_since = now
                    elif stable_since is not None and now - stable_since >= 0.2:
                        ready = True
                        break
                if process.poll() is not None:
                    ready = size > 0
                    break
                time.sleep(0.05)
        finally:
            _terminate(process)
            if log_file:
                log_file.close()

        if ready:
            return True, "screenshot.png created"

        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        except OSError:
            pass
        detail = f"screenshot was not created within {self.timeout}s"
        if tail.strip():
            detail = f"{detail}; browser stderr: {tail.strip()}"
        return False, detail


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _parse_harness_payload(raw: str | None) -> dict:
    if not raw:
        return {
            "checks": [AcceptanceCheck("browser_run", False, "empty harness payload")],
            "console_errors": [],
        }
    try:
        payload = json.loads(unescape(raw))
        checks = [
            AcceptanceCheck(
                str(item.get("name", "browser_check")),
                bool(item.get("passed")),
                str(item.get("detail", ""))[:1000],
            )
            for item in payload.get("checks", [])
        ]
        checks.insert(0, AcceptanceCheck("browser_run", True, "completed"))
        return {
            "checks": checks,
            "console_errors": [
                str(error)[:1000] for error in payload.get("console_errors", [])
            ],
        }
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "checks": [AcceptanceCheck("browser_result", False, str(exc))],
            "console_errors": [],
        }


RESULT_PATH = "/__rocto_result"


class _HarnessServer(ThreadingHTTPServer):
    """Local static server that also collects the harness result via POST."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rocto_result: str | None = None
        self.rocto_ready = threading.Event()
        self.rocto_timeout: int | None = None
        #: KI-011: one verdict part per test, keyed by test index.
        self.rocto_parts: dict[int, dict] = {}
        self.rocto_expected: int | None = None
        self._rocto_lock = threading.Lock()

    def accept_verdict(self, body: str) -> None:
        """Store a posted verdict; finish once every part has arrived.

        A payload without ``part`` (the watchdog, or a single-shot harness)
        is a complete verdict on its own, and as before the first complete
        verdict wins.
        """
        with self._rocto_lock:
            if self.rocto_ready.is_set():
                return
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                payload = None
            if not (
                isinstance(payload, dict)
                and isinstance(payload.get("part"), int)
                and isinstance(payload.get("parts"), int)
            ):
                self.rocto_result = body
                self.rocto_ready.set()
                return
            self.rocto_expected = payload["parts"]
            self.rocto_parts.setdefault(payload["part"], payload)
            if len(self.rocto_parts) >= self.rocto_expected:
                ordered = [self.rocto_parts[key] for key in sorted(self.rocto_parts)]
                self.rocto_result = json.dumps(
                    {
                        "checks": [c for part in ordered for c in part.get("checks", [])],
                        "console_errors": [
                            e for part in ordered for e in part.get("console_errors", [])
                        ],
                    },
                    ensure_ascii=False,
                )
                self.rocto_ready.set()

    def handle_error(self, request, client_address) -> None:
        """Stay quiet when the browser is killed mid-connection.

        We terminate Edge the instant the verdict arrives, which resets any
        socket it still had open. On Windows that surfaces as WinError 10054 and
        socketserver prints a full traceback per connection. It is expected
        teardown, not a failure, and the noise buried an otherwise passing run.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class _QuietHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != RESULT_PATH:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.accept_verdict(body.decode("utf-8", errors="replace"))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _serve(directory: Path) -> Iterator[tuple[str, _HarnessServer]]:
    handler = partial(_QuietHandler, directory=str(directory))
    server = _HarnessServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/index.html", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
