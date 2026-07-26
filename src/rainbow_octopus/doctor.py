from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import platform
import sys

from .executor import (
    AUTO_ORDER,
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
    for name in AUTO_ORDER:
        try:
            ok, detail = probes[name]()
        except Exception as exc:  # noqa: BLE001 - doctor must never crash
            ok, detail = False, str(exc)
        if name == "codex" and ok and is_app_bundled_codex(find_codex()):
            detail += " [app-bundled: --sandbox falls back automatically, see KI-002]"
        checks.append(DoctorCheck(f"executor:{name}", ok, detail, required=False))
    return checks


def run_doctor() -> list[DoctorCheck]:
    key = resolve_api_key()
    base_url = resolve_base_url()
    where = "DeepSeek (default)" if is_default_provider(base_url) else base_url
    checks = [
        DoctorCheck("python", sys.version_info >= (3, 10), platform.python_version()),
        DoctorCheck(
            "planner_api",
            bool(key),
            f"key configured, endpoint {where}"
            if key
            else f"set {API_KEY_ENV} or {LEGACY_API_KEY_ENV}",
        ),
    ]

    backends = _backend_checks()
    usable = [check.name.split(":", 1)[1] for check in backends if check.passed]
    selected = os.environ.get("ROCTO_EXECUTOR", "auto")
    if selected == "auto":
        detail = (
            f"auto -> {', '.join(usable)}" if usable else "no usable executor backend"
        )
        checks.append(DoctorCheck("executor", bool(usable), detail))
    else:
        ok = selected in usable
        checks.append(
            DoctorCheck(
                "executor",
                ok,
                f"{selected} ({'ready' if ok else 'not available'})",
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
        )
    )
    return checks


def doctor_as_dict(checks: list[DoctorCheck]) -> dict:
    return {
        "passed": all(check.passed for check in checks if check.required),
        "checks": [asdict(check) for check in checks],
    }
