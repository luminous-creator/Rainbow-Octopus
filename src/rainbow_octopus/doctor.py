from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import platform
import sys

from .executor import (
    KNOWN_BACKENDS,
    auto_order,
    ClaudeCodeExecutor,
    CodexExecutor,
    DeepSeekExecutor,
    find_claude,
    find_codex,
    is_app_bundled_codex,
)
from .provider import (
    API_KEY_ENV,
    LEGACY_API_KEY_ENV,
    is_default_provider,
    resolve_api_key,
    resolve_base_url,
)
from .verifier import browser_install_hint, browser_name, find_browser


@dataclass
class DoctorCheck:
    name: str
    passed: bool
    detail: str
    required: bool = True
    #: One concrete next step when the check fails.
    fix: str = ""


def _backend_checks() -> list[DoctorCheck]:
    """One line per executor.

    Individually optional: a build only needs *one* of them. The aggregate
    `executor` check is what actually gates `rocto build`.
    """
    probes = {
        "claude": lambda: ClaudeCodeExecutor(find_claude()).healthcheck(),
        "codex": lambda: CodexExecutor(find_codex()).healthcheck(),
        "deepseek": lambda: DeepSeekExecutor().healthcheck(),
    }
    checks: list[DoctorCheck] = []
    order = auto_order()
    for name in (*order, *(n for n in KNOWN_BACKENDS if n not in order)):
        try:
            ok, detail = probes[name]()
        except Exception as exc:  # noqa: BLE001 - doctor must never crash
            ok, detail = False, str(exc)
        if name == "codex" and ok and is_app_bundled_codex(find_codex()):
            detail += " [app-bundled: --sandbox falls back automatically, see KI-002]"
        checks.append(DoctorCheck(f"executor:{name}", ok, detail, required=False))
    return checks


_KEY_FIX = (
    f"set {API_KEY_ENV} (or {LEGACY_API_KEY_ENV}) to an API key, "
    "or install Claude Code and run: claude auth login"
)


def _planner_check(key: str | None, where: str, claude_ok: bool) -> DoctorCheck:
    """Planning needs an API key *or* a signed-in Claude Code (ADR-004)."""
    choice = (os.environ.get("ROCTO_PLANNER") or "auto").strip().lower()
    if choice == "claude":
        return DoctorCheck(
            "planner", claude_ok,
            "claude (Claude Code CLI)" if claude_ok else "claude selected but Claude Code is not ready",
            fix="" if claude_ok else "install Claude Code and run: claude auth login",
        )
    if choice == "api" or key:
        return DoctorCheck(
            "planner", bool(key),
            f"api, key configured, endpoint {where}" if key
            else f"api selected but no key: set {API_KEY_ENV} or {LEGACY_API_KEY_ENV}",
            fix="" if key else _KEY_FIX,
        )
    if claude_ok:
        return DoctorCheck("planner", True, "auto -> claude (no API key; using Claude Code)")
    return DoctorCheck(
        "planner", False,
        f"no API key and no signed-in Claude Code; set {API_KEY_ENV} or {LEGACY_API_KEY_ENV}",
        fix=_KEY_FIX,
    )


def run_doctor() -> list[DoctorCheck]:
    key = resolve_api_key()
    base_url = resolve_base_url()
    where = "DeepSeek (default)" if is_default_provider(base_url) else base_url
    backends = _backend_checks()
    usable = [check.name.split(":", 1)[1] for check in backends if check.passed]
    checks = [
        DoctorCheck(
            "python", sys.version_info >= (3, 10), platform.python_version(),
            fix="install Python 3.10 or newer",
        ),
        _planner_check(key, where, "claude" in usable),
    ]

    selected = os.environ.get("ROCTO_EXECUTOR", "auto")
    if selected == "auto":
        routed = [name for name in auto_order() if name in usable]
        detail = (
            f"auto -> {', '.join(routed)}" if routed else "no usable executor backend"
        )
        checks.append(DoctorCheck("executor", bool(routed), detail, fix=_KEY_FIX))
    else:
        ok = selected in usable
        checks.append(
            DoctorCheck(
                "executor",
                ok,
                f"{selected} ({'ready' if ok else 'not available'})",
                fix=f"see the executor:{selected} line below, or use --executor auto",
            )
        )
    checks.extend(backends)

    browser = find_browser()
    checks.append(
        DoctorCheck(
            "browser",
            browser is not None,
            f"{browser_name(browser)}: {browser}"
            if browser
            else f"not found; {browser_install_hint()}",
            fix=f"{browser_install_hint()}, or set ROCTO_BROWSER_BIN",
        )
    )
    return checks


def doctor_as_dict(checks: list[DoctorCheck]) -> dict:
    return {
        "passed": all(check.passed for check in checks if check.required),
        "checks": [asdict(check) for check in checks],
    }
